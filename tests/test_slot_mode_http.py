"""The 1.12.0 wire contract of the desktop (plan 3.2 to 3.6), against a
real server on a spare port: GET /config's new keys, the new /open_tabs
fields, the /streak_event verdict answer and its new fields, the durable
/rescue_ack, and which requests prove the extension is alive (F30)."""
import http.client
import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import streak_saves as sv
import stream_monitor_tray as sm

NOW = "2026-09-29T12:00:00.000Z"
NOW_EPOCH = sm._iso_to_epoch(NOW)
INSTANCE = "1a2b3c4d"


@pytest.fixture
def toasts():
    return []


@pytest.fixture
def activity():
    return []


@pytest.fixture(autouse=True)
def isolated(tmp_config_dir, monkeypatch, toasts, activity):
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset({"alice"}))
    monkeypatch.setattr(sm, "_streak_clock", lambda: NOW_EPOCH)
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    monkeypatch.setattr(sm, "_tray_notifier", lambda title, msg: toasts.append((title, msg)))
    monkeypatch.setattr(sm, "_rescue_ack_handler", None)
    monkeypatch.setattr(sm, "log_activity", lambda event, **fields: activity.append((event, fields)))
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", {})


@pytest.fixture(scope="module")
def port():
    # A threading server, as in the app: a waiting /streak_event must not
    # block other requests.
    server = sm._SingletonHTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _send(port, method, path, body=None, headers=None):
    """(status, {lowercase header: value}, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {n.lower(): v for n, v in resp.getheaders()}, data
    finally:
        conn.close()


def _post(port, path, payload, content_type="application/json", origin=None, raw=None):
    headers = {} if content_type is None else {"Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return _send(port, "POST", path, body, headers)


def _event(port, payload):
    status, headers, body = _post(port, "/streak_event", payload)
    return status, (json.loads(body) if status == 200 else None)


def _card(**over):
    payload = {"status": "broke", "streamer": "alice", "count": 5, "detected_at": NOW,
               "page_url": "https://www.twitch.tv/alice", "source": "bell"}
    payload.update(over)
    return payload


def _named(activity, name):
    return [fields for event, fields in activity if event == name]


class Submitter:
    """A stand-in for the monitor's inbox: records every submission, and
    answers it after `delay` seconds (None: never) with `result`."""

    def __init__(self, waitable=True, delay=0.0, result=True):
        self.calls = []
        self.waitable = waitable
        self.delay = delay
        self.result = result

    def __call__(self, kind, payload):
        item = sm._InboxItem(kind, payload, waitable=self.waitable)
        self.calls.append((kind, payload))
        if self.delay is not None:
            def answer():
                time.sleep(self.delay)
                item.result = self.result
                item.done.set()
            threading.Thread(target=answer, daemon=True).start()
        return item


# ---------------------------------------------------------------------------
# GET /config (3.2)
# ---------------------------------------------------------------------------


def test_c02_config_has_slot_plan_auto_save_and_sources(monkeypatch):
    monkeypatch.setattr(sm, "CONFIG_SERVER_PORT", 0)  # never the app's own port
    config = sm.Config(client_id="a", client_secret="b", streamers=["alice"], auto_save_streaks=True)
    server = sm.create_config_server(config)
    assert server is not None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        status, headers, body = _send(server.server_address[1], "GET", "/config")
        data = json.loads(body)
        assert status == 200
        assert data["slot_plan"] is None
        assert data["auto_save_streaks"] is True
        # No "link" since the 2026-10-01 live check (A41 item 2): the
        # sidebar pills link to VODs, so the extensions send no link event.
        assert data["streak_sources"] == ["bell", "page", "manual"] == list(sv.STREAK_SOURCES)
        assert not any(name.startswith("access-control-") for name in headers)

        # A published plan is served as the object it is.
        sm.ConfigRequestHandler.config_data["slot_plan"] = {"v": 1, "seq": 3, "state": "waiting"}
        _, _, body = _send(server.server_address[1], "GET", "/config")
        assert json.loads(body)["slot_plan"] == {"v": 1, "seq": 3, "state": "waiting"}
    finally:
        server.shutdown()
        server.server_close()


def test_c01_c02_a_settings_change_updates_auto_save_in_config_and_config_loaded(monkeypatch, activity):
    """O10: automatic saves are off by default and usually turned on in
    Settings while the tray runs. The change reaches /config (the
    extension's autoSaveStreaks) and the card handler at once, and the
    config_loaded line carries the five Slot mode fields (plan 3.1)."""
    monkeypatch.setattr(sm, "CONFIG_SERVER_PORT", 0)  # never the app's own port
    config = sm.Config(client_id="a", client_secret="b", streamers=["alice"], auto_save_streaks=False)
    server = sm.create_config_server(config)
    assert server is not None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = server.server_address[1]
    try:
        monkeypatch.setattr(sm, "_monitor_submitter", Submitter(delay=0.05))
        assert _event(port, _card(streamer="bob")) == (200, {"verdict": "fresh", "item": False})

        restarts = []
        app = SimpleNamespace(config=config, monitor=SimpleNamespace(
            config=config, restart=lambda: restarts.append(1)))
        new_config = sm.Config(**dict(asdict(config), auto_save_streaks=True, slot_minutes=45))
        sm.StreamMonitorApp._apply_config_change(app, new_config)

        assert sm.ConfigRequestHandler.config_data["auto_save_streaks"] is True
        _, _, body = _send(port, "GET", "/config")
        assert json.loads(body)["auto_save_streaks"] is True
        (loaded,) = _named(activity, "config_loaded")
        assert loaded["reason"] == "settings_changed"
        assert {k: loaded[k] for k in ("slot_mode", "keep_open_slots", "cycle_slots", "slot_minutes",
                                       "auto_save_streaks")} == {
            "slot_mode": False, "keep_open_slots": 2, "cycle_slots": 1, "slot_minutes": 45,
            "auto_save_streaks": True}
        assert restarts == [1] and app.monitor.config is new_config
        assert _event(port, _card(streamer="carol")) == (200, {"verdict": "fresh", "item": True})

        # Turned off again: the next card makes no item.
        sm.StreamMonitorApp._apply_config_change(
            app, sm.Config(**dict(asdict(new_config), auto_save_streaks=False)))
        _, _, body = _send(port, "GET", "/config")
        assert json.loads(body)["auto_save_streaks"] is False
        assert _event(port, _card(streamer="dave")) == (200, {"verdict": "fresh", "item": False})
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# POST /open_tabs (3.4)
# ---------------------------------------------------------------------------


def test_c04_open_tabs_new_fields_are_stored_and_validated(port):
    gone = [{"streamer": "Erin", "reason": "user_closed", "at": 1790000100},
            {"streamer": "frank", "reason": "not_eligible", "at": 1790000101}]
    payload = {"browser": "Firefox", "instance": " 1A2B3C4D ", "streamers": ["alice", "Bob"],
               "reason": "plan-applied", "plan_seq": 42, "gone": gone, "busy": "paused"}
    assert _post(port, "/open_tabs", payload)[0] == 204

    report = sm.extension_open_tabs_snapshot()["firefox-1a2b3c4d"]
    assert report["streamers"] == frozenset({"alice", "bob"})
    assert (report["browser"], report["instance"], report["plan_seq"], report["busy"]) == (
        "firefox", "1a2b3c4d", 42, "paused")
    assert sm.drain_open_tabs_gone() == [
        {"key": "firefox-1a2b3c4d", "streamer": "erin", "reason": "user_closed", "at": 1790000100},
        {"key": "firefox-1a2b3c4d", "streamer": "frank", "reason": "not_eligible", "at": 1790000101},
    ]
    # A re-sent report (its first answer lost) queues no gone entry twice.
    assert _post(port, "/open_tabs", payload)[0] == 204
    assert sm.drain_open_tabs_gone() == []
    assert sm.drain_open_tabs_gone() == []


def test_c04_malformed_optional_fields_are_dropped_and_old_payloads_still_get_204(port):
    bad_gone = [
        "nope",
        {"streamer": "bad name!", "reason": "user_closed", "at": 1},
        {"streamer": "erin", "reason": "closed_by_owner", "at": 1},
        {"streamer": "erin", "reason": "raid", "at": True},
        {"streamer": "erin", "reason": "raid", "at": "1790000000"},
        {"streamer": "erin", "reason": "raid"},
        {"streamer": "gina", "reason": "window_closed", "at": 1790000000},
    ]
    for instance, plan_seq, busy in [("XYZ", True, "yes"), ("1a2b3c4", -1, 1),
                                     (12345678, 2 ** 31, "PAUSED"), (None, "4", None)]:
        payload = {"browser": "firefox", "instance": instance, "streamers": ["alice"],
                   "plan_seq": plan_seq, "gone": bad_gone, "busy": busy}
        assert _post(port, "/open_tabs", payload)[0] == 204
        report = sm.extension_open_tabs_snapshot()["firefox"]
        assert (report["instance"], report["plan_seq"], report["busy"]) == (None, None, None)
    # Only the one valid gone entry was kept (once).
    assert sm.drain_open_tabs_gone() == [
        {"key": "firefox", "streamer": "gina", "reason": "window_closed", "at": 1790000000}]
    # A gone list that is not a list is ignored; the report is still stored.
    assert _post(port, "/open_tabs", {"browser": "firefox", "streamers": [], "gone": {"a": 1}})[0] == 204
    # A 1.11 extension's report is unchanged.
    assert _post(port, "/open_tabs", {"browser": "chrome", "streamers": ["Alice"], "reason": "init"})[0] == 204
    assert sm.extension_open_tabs_snapshot()["chrome"]["streamers"] == frozenset({"alice"})
    assert sm.extension_open_tabs_snapshot()["chrome"]["plan_seq"] is None
    # The required fields still decide: 400 as before.
    assert _post(port, "/open_tabs", {"browser": "", "streamers": [], "instance": INSTANCE})[0] == 400
    assert _post(port, "/open_tabs", {"browser": "chrome", "streamers": "alice", "plan_seq": 1})[0] == 400


def test_c04_report_key_and_plan_seq_mirror(port, tmp_config_dir):
    assert _post(port, "/open_tabs", {"browser": "firefox", "instance": INSTANCE, "streamers": ["bob"],
                                      "plan_seq": 42})[0] == 204
    assert _post(port, "/open_tabs", {"browser": "chrome", "streamers": ["alice"]})[0] == 204

    on_disk = json.loads((tmp_config_dir / "extension_tabs.json").read_text(encoding="utf-8"))
    assert set(on_disk) == {"firefox-1a2b3c4d", "chrome"}
    assert on_disk["firefox-1a2b3c4d"]["plan_seq"] == 42
    assert on_disk["firefox-1a2b3c4d"]["streamers"] == ["bob"]
    assert "plan_seq" not in on_disk["chrome"]
    assert sm.persisted_capable_report_within(600, now_epoch=on_disk["chrome"]["ts"] + 10)
    # Two profiles of one browser keep separate reports.
    assert _post(port, "/open_tabs", {"browser": "firefox", "instance": "0f0e0d0c", "streamers": []})[0] == 204
    assert {"firefox-1a2b3c4d", "firefox-0f0e0d0c"} <= set(sm.extension_open_tabs_snapshot())


# ---------------------------------------------------------------------------
# POST /streak_event (3.5)
# ---------------------------------------------------------------------------


def test_c05_the_answer_is_200_json_with_the_verdict(port):
    status, headers, body = _post(port, "/streak_event", _card())
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert headers["access-control-allow-origin"] == "*"
    assert json.loads(body) == {"verdict": "fresh", "item": False}

    saved = {"status": "already_saved", "streamer": "alice", "count": 5, "detected_at": NOW}
    status, _, body = _post(port, "/streak_event", saved)
    assert status == 200 and json.loads(body) == {"verdict": "saved", "item": False}
    # The same card again, now under a save of the same count and no
    # watching record: not fresh any more.
    status, answer = _event(port, _card(detected_at="2026-09-29T11:00:00.000Z"))
    assert answer["verdict"] in ("stale", "verify") and answer["item"] is False


def test_c05_new_streak_fields_are_validated(port, activity):
    # Every optional field malformed: dropped, never a 400.
    status, answer = _event(port, _card(streamer="bob", card_age_s=-5, card_age_unit_s=7,
                                        login_verified="yes", source="sidebar",
                                        deadline_at="2099-01-01T00:00:00Z"))
    assert status == 200 and answer == {"verdict": "fresh", "item": False}
    (broke,) = _named(activity, "streak_broke")
    assert broke["card_age_s"] is None and broke["card_age_unit_s"] is None
    assert broke["source"] == "bell"
    assert broke["login_verified"] is True  # absent or malformed: the desktop's own check
    # bob's deadline is the 24 h rule from detected_at, not the dropped one.
    assert broke["deadline_at"] == sm._epoch_to_iso(NOW_EPOCH + 24 * 3600)

    activity.clear()
    deadline = "2026-09-29T20:00:00.000Z"
    status, answer = _event(port, _card(streamer="carol", card_age_s=7200, login_verified=True,
                                        source="page", deadline_at=deadline))
    assert status == 200 and answer["verdict"] == "fresh"
    (broke,) = _named(activity, "streak_broke")
    assert broke["card_age_s"] == 7200
    assert broke["card_age_unit_s"] == 3600  # inferred: the largest unit dividing the age
    assert broke["source"] == "page"
    assert broke["deadline_at"] == deadline
    assert broke["verdict"] == "fresh"


def test_c05_card_age_above_eight_days_is_stale(port, activity, toasts):
    status, answer = _event(port, _card(card_age_s=sv.CARD_AGE_MAX_SECONDS + 1))
    assert status == 200 and answer == {"verdict": "stale", "item": False}
    (ignored,) = _named(activity, "streak_event_ignored")
    assert ignored["verdict"] == "stale" and ignored["reason"] == "already_saved"
    assert toasts == []
    # Exactly the limit is still a card like any other.
    status, answer = _event(port, _card(streamer="bob", card_age_s=sv.CARD_AGE_MAX_SECONDS))
    assert answer["verdict"] == "fresh"


def test_ap07_c05_a_link_event_may_have_a_null_count_a_card_may_not(port, toasts):
    # /config no longer asks for link events (A41 item 2), but the desktop
    # still reads one as a link, never as a bell card (plan 3.5).
    assert "link" not in sv.STREAK_SOURCES and "link" in sv.EVENT_SOURCES
    link = _card(streamer="zed", count=None, source="link", login_verified=True)
    status, answer = _event(port, link)
    assert status == 200 and answer["verdict"] == "fresh"
    assert toasts[-1][0] == "zed: streak broke"

    assert _event(port, _card(count=None))[0] == 400  # a bell card
    assert _event(port, _card(count=None, source=None))[0] == 400
    assert _event(port, dict(link, status="in_danger"))[0] == 400
    assert _event(port, dict(link, status="already_saved"))[0] == 400
    assert _event(port, _card(streamer="yuri", count=3, source="link"))[0] == 200


def test_c05_a_malformed_payload_is_400(port):
    for payload in (
        _card(status="kept"),
        _card(streamer=""),
        _card(streamer=5),
        _card(count=-1),
        _card(count=100001),
        _card(count="5"),
        _card(status="in_danger", deadline_hours=-1),
        _card(status="in_danger", deadline_hours=24 * 365 + 1),
        {"status": "already_saved", "streamer": "not a login!", "count": 1},
        [1, 2],
    ):
        assert _event(port, payload)[0] == 400, payload
    assert _post(port, "/streak_event", None, raw=b"{nope")[0] == 400
    assert _post(port, "/streak_event", None, raw=b"")[0] == 400


def test_am06_the_answer_waits_for_the_replan_or_2_seconds(port, monkeypatch):
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = True

    # The monitor answers after 0.3 s: the handler waits for it.
    quick = Submitter(delay=0.3)
    monkeypatch.setattr(sm, "_monitor_submitter", quick)
    began = time.monotonic()
    status, answer = _event(port, _card(streamer="bob"))
    took = time.monotonic() - began
    assert answer == {"verdict": "fresh", "item": True}
    assert 0.25 <= took < 1.5
    assert [kind for kind, _ in quick.calls] == ["streak_item"]

    # The monitor is inside a Helix call: the handler gives up after 2 s.
    stuck = Submitter(delay=None)
    monkeypatch.setattr(sm, "_monitor_submitter", stuck)
    began = time.monotonic()
    status, answer = _event(port, _card(streamer="carol"))
    took = time.monotonic() - began
    assert answer == {"verdict": "fresh", "item": False}
    assert sm.SLOT_REPLAN_WAIT_SECONDS - 0.1 <= took < sm.SLOT_REPLAN_WAIT_SECONDS + 1.5

    # A monitor that is stopped (not waitable): no wait at all.
    stopped = Submitter(waitable=False, delay=None)
    monkeypatch.setattr(sm, "_monitor_submitter", stopped)
    began = time.monotonic()
    status, answer = _event(port, _card(streamer="dave"))
    assert time.monotonic() - began < 1.0
    assert answer == {"verdict": "fresh", "item": False}
    assert len(stopped.calls) == 1


def test_c05_o16_a_manual_event_may_have_a_null_count(port, monkeypatch, activity, toasts):
    sub = Submitter(waitable=False, delay=None)
    monkeypatch.setattr(sm, "_monitor_submitter", sub)
    # Automatic saves are off: a manual event is taken anyway (A12).
    status, answer = _event(port, _card(streamer="zed", count=None, source="manual",
                                        deadline_at="2026-09-29T18:00:00.000Z"))
    assert status == 200 and answer == {"verdict": "fresh", "item": True}
    ((kind, payload),) = sub.calls
    item = payload["item"]
    assert kind == "streak_item" and payload["merge_only"] is False
    assert (item["login"], item["kind"], item["count"], item["origin"]) == ("zed", "broke", None, "manual")
    assert item["deadline_at"] == sm._iso_to_epoch("2026-09-29T18:00:00.000Z")
    # No toast, no card line, no dedup key.
    assert toasts == [] and not _named(activity, "streak_broke")
    assert not [k for k in sm._streak_event_seen if k[1] == "zed"]
    # An in-danger row with a count; a manual in_danger event with no count too.
    assert _event(port, _card(streamer="zed", status="in_danger", count=4, source="manual"))[1]["item"]
    assert _event(port, _card(streamer="zed", status="in_danger", count=None, source="manual"))[0] == 200


def test_c05_a_manual_event_answers_item_true_once_queued_even_past_the_wait(port, monkeypatch):
    stuck = Submitter(delay=None)
    monkeypatch.setattr(sm, "_monitor_submitter", stuck)
    began = time.monotonic()
    status, answer = _event(port, _card(streamer="zed", source="manual"))
    took = time.monotonic() - began
    assert answer == {"verdict": "fresh", "item": True}
    assert took >= sm.SLOT_REPLAN_WAIT_SECONDS - 0.1
    # With no monitor at all the item is not queued anywhere.
    monkeypatch.setattr(sm, "_monitor_submitter", None)
    assert _event(port, _card(streamer="zed", source="manual"))[1] == {"verdict": "fresh", "item": False}


def test_ap05_a_reserved_or_bad_login_is_fresh_without_an_item(port, monkeypatch, activity, toasts):
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = True
    sub = Submitter(delay=0.0)
    monkeypatch.setattr(sm, "_monitor_submitter", sub)
    for name in ("directory", "save-streak", "x" * 26, "Not A Login", "popout"):
        status, answer = _event(port, _card(streamer=name))
        assert status == 200 and answer == {"verdict": "fresh", "item": False}, name
    # The extension says the login is not verified: the same.
    status, answer = _event(port, _card(streamer="alice", login_verified=False))
    assert answer == {"verdict": "fresh", "item": False}
    # A manual event for a reserved name: taken nowhere.
    assert _event(port, _card(streamer="videos", source="manual"))[1] == {"verdict": "fresh", "item": False}

    assert sub.calls == []
    assert len(toasts) == 6  # logged and toasted as in 1.11
    assert all(f["login_verified"] is False for f in _named(activity, "streak_broke"))


# ---------------------------------------------------------------------------
# POST /rescue_ack (3.6)
# ---------------------------------------------------------------------------


@pytest.fixture
def offer(monitor, monkeypatch):
    monitor.queued_vods = {"bob": {"url": sv.save_url("bob"), "ended_at": "2026-09-29T01:00:00.000Z"}}
    monitor._offer_rescue_or_flush(set())
    monkeypatch.setattr(sm, "_rescue_claim_handler",
                        lambda offer_id, claimant: monitor.acknowledge_rescue(offer_id, claimant))
    return SimpleNamespace(monitor=monitor, id=monitor.rescue_pending["id"])


def _ack(port, body):
    return _post(port, "/rescue_ack", body)[0]


def test_c06_as07_reack_same_claimant_within_10_minutes(port, offer, activity):
    body = {"id": offer.id, "browser": "firefox", "instance": INSTANCE}
    assert _ack(port, body) == 204
    assert offer.monitor.rescue_pending is None
    assert offer.monitor._last_acked_offer["claimant"] == "firefox-" + INSTANCE

    # The first answer was lost: the same profile acks again.
    assert _ack(port, body) == 204
    assert _named(activity, "rescue_reacked") == [{"offer_id": offer.id, "claimant": "firefox-" + INSTANCE}]
    # Another profile, or another browser, may not take it over.
    assert _ack(port, dict(body, instance="0f0e0d0c")) == 409
    assert _ack(port, dict(body, browser="chrome")) == 409
    # Ten minutes later not even the claimant.
    offer.monitor._last_acked_offer["acked_at"] -= sm.RESCUE_REACK_WINDOW_SECONDS + 1
    assert _ack(port, body) == 409
    assert len(_named(activity, "rescue_acked")) == 1


def test_c06_a_non_object_ack_body_is_400(port, offer):
    assert _post(port, "/rescue_ack", [offer.id])[0] == 400
    assert _post(port, "/rescue_ack", offer.id)[0] == 400
    assert _post(port, "/rescue_ack", None, raw=b"[nope")[0] == 400
    assert _ack(port, {}) == 409
    assert offer.monitor.rescue_pending is not None  # none of these took it


def test_am11_no_reack_without_an_instance(port, offer):
    assert _ack(port, {"id": offer.id, "browser": "chrome"}) == 204
    assert offer.monitor._last_acked_offer["claimant"] is None
    assert _ack(port, {"id": offer.id, "browser": "chrome"}) == 409
    assert _ack(port, {"id": offer.id, "browser": "chrome", "instance": "zz"}) == 409


# ---------------------------------------------------------------------------
# F30 (A38): only guarded POSTs mark the extension as alive
# ---------------------------------------------------------------------------


def test_f30_c02_a_config_get_does_not_mark_the_extension_alive(port):
    for _ in range(3):
        assert _send(port, "GET", "/config")[0] == 200
    assert sm.extension_seen_within(150) is False
    assert sm._extension_last_seen_monotonic is None


def test_f30_c04_a_stored_open_tabs_report_marks_the_extension_alive(port):
    assert _post(port, "/open_tabs", {"browser": "chrome", "instance": INSTANCE, "streamers": [],
                                      "plan_seq": 0})[0] == 204
    assert sm.extension_seen_within(150) is True


@pytest.mark.parametrize("path", ["/rescue_ack", "/streak_event"])
def test_f30_the_other_guarded_posts_mark_it_alive_too(port, path, monkeypatch):
    monkeypatch.setattr(sm, "_rescue_ack_handler", lambda offer_id: False)
    body = {"id": "rescue-1"} if path == "/rescue_ack" else _card()
    status = _post(port, path, body)[0]
    assert status in (200, 409)
    assert sm.extension_seen_within(150) is True


def test_f30_a_refused_or_malformed_post_does_not_mark_it_alive(port):
    report = {"browser": "chrome", "instance": INSTANCE, "streamers": [], "plan_seq": 0}
    # A web page (its Origin), and a body a page can send without a preflight.
    assert _post(port, "/open_tabs", report, origin="https://evil.example")[0] == 403
    assert _post(port, "/open_tabs", report, content_type="text/plain")[0] == 415
    # A malformed report, a non-object ack, and a malformed streak event.
    assert _post(port, "/open_tabs", {"browser": "", "streamers": []})[0] == 400
    assert _post(port, "/open_tabs", [report])[0] == 400
    assert _post(port, "/rescue_ack", ["rescue-1"])[0] == 400
    assert _post(port, "/rescue_ack", {"id": 5})[0] == 409
    assert _post(port, "/streak_event", _card(count="five"))[0] == 400

    assert sm.extension_seen_within(150) is False
    assert sm.extension_open_tabs_snapshot() == {}
