"""Save items from streak cards (1.12.0, DESIGN 11): where a card that
passed the verdict goes, through handle_streak_event and the monitor inbox.

Normal mode (Slot mode off): the item rides a rescue offer (queued_vods),
or waits for the offline edge while the streamer is live with a tab. Slot
mode: the item goes to the scheduler, which gives it a rotating turn.

The inbox is drained on the calling thread here (the monitor is not
running), so every answer already reflects the monitor's work.
"""
import json
import time
from unittest.mock import patch

import pytest

import slot_scheduler as ss
import streak_saves as sv
import stream_monitor_tray as sm

NOW = "2026-09-29T12:00:00.000Z"
NOW_EPOCH = sm._iso_to_epoch(NOW)
HOUR = 3600
INSTANCE = "1a2b3c4d"
STREAMERS = ["alice", "bob", "carol", "dave", "erin"]


@pytest.fixture
def toasts():
    return []


@pytest.fixture
def events(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "log_activity", lambda event, **fields: calls.append((event, fields)))
    return calls


@pytest.fixture
def mon(monitor, tmp_config_dir, monkeypatch, toasts):
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    monkeypatch.setattr(sm, "default_browser_family", lambda: "chrome")
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset(STREAMERS))
    monkeypatch.setattr(sm, "_streak_clock", lambda: NOW_EPOCH)
    monkeypatch.setattr(sm, "_tray_notifier", lambda title, msg: toasts.append((title, msg)))
    monitor.config.streamers = list(STREAMERS)
    monitor.streamers = {name: sm.StreamerState(name=name) for name in STREAMERS}
    monitor.config.auto_save_streaks = True
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = True

    def submit(kind, payload):
        item = monitor.submit(kind, payload)
        monitor._drain_inbox()
        return item

    monkeypatch.setattr(sm, "_monitor_submitter", submit)
    return monitor


def _card(streamer="zed", status="broke", count=5, age=None, unit=None, **over):
    payload = {"status": status, "streamer": streamer, "count": count, "detected_at": NOW,
               "source": "bell"}
    if age is not None:
        payload["card_age_s"] = age
    if unit is not None:
        payload["card_age_unit_s"] = unit
    payload.update(over)
    return payload


def _report(streamers=(), plan_seq=None, instance=None, browser="chrome"):
    assert sm.record_extension_open_tabs(browser, list(streamers), "refresh", instance=instance,
                                         plan_seq=plan_seq)


def _poll(mon, live):
    data = {"data": [{"user_login": name, "user_id": "1", "started_at": started}
                     for name, started in live.items()]}
    with patch.object(mon, "_api_get", return_value=data), patch.object(mon, "open_stream"):
        mon.process_state_changes(mon.check_streams())


def _named(events, name):
    return [fields for event, fields in events if event == name]


def _slot_mode(mon, live=None):
    """Slot mode active with a capable Chrome profile and fresh live data."""
    mon.config.slot_mode = True
    _report(plan_seq=0, instance=INSTANCE)
    mon._slot_tick()
    _poll(mon, live or {})
    assert mon.slot_state == "alive"


def _plan():
    return sm.ConfigRequestHandler.config_data["slot_plan"]


# ---------------------------------------------------------------------------
# Normal mode (DESIGN 11.5, A32)
# ---------------------------------------------------------------------------


def test_as02_a_card_becomes_a_queued_vod_with_a_deadline_and_an_offer(mon, events):
    with patch("stream_monitor_tray.webbrowser.open") as mweb:
        answer = sm.handle_streak_event(_card(age=3 * HOUR, unit=HOUR))

    assert answer == {"verdict": "fresh", "item": True}
    entry = mon.queued_vods["zed"]
    break_at = NOW_EPOCH - 4 * HOUR
    assert entry["url"] == sv.save_url("zed")
    assert entry["ended_at"] == sm._epoch_to_iso(break_at)
    assert entry["deadline_at"] == sm._epoch_to_iso(break_at + 24 * HOUR)
    assert (entry["age_unit_s"], entry["origin"], entry["verify"]) == (HOUR, "card", False)
    assert entry["item"]["login"] == "zed"
    (added,) = _named(events, "streak_item_added")
    assert (added["mode"], added["origin"], added["merged"]) == ("normal", "card", False)
    assert _named(events, "vod_queued") == [{"streamer": "zed", "url": sv.save_url("zed"), "reason": "card"}]
    offer = mon.rescue_pending
    assert offer["candidates"] == [{"streamer": "zed", "url": sv.save_url("zed"), "kind": "ended",
                                    "ended_at": entry["ended_at"]}]
    assert sm.ConfigRequestHandler.config_data["rescue"] == offer
    mweb.assert_not_called()  # the extension opens it (or the fallback, later)


def test_as02_while_paused_no_offer_until_the_lift(mon, events):
    mon.config.own_channel = "me"
    mon.config.im_live_pause = True
    mon.auto_paused = True
    sm.handle_streak_event(_card())
    assert "zed" in mon.queued_vods
    assert mon.rescue_pending is None and not _named(events, "rescue_offered")

    _poll(mon, {})  # "me" is offline now: the auto-pause lifts

    assert mon.auto_paused is False
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["zed"]


def test_o01_a_live_streamer_with_a_tab_is_held_then_checked_at_the_offline_edge(mon, events, tmp_config_dir):
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    _report(["alice"])  # alice has a Stream Monitor tab
    answer = sm.handle_streak_event(_card("alice", age=HOUR, unit=HOUR))

    assert answer["item"] is True
    assert "alice" in mon.held_save_items and "alice" not in mon.queued_vods
    assert mon.rescue_pending is None
    assert _named(events, "streak_item_added")[0]["mode"] == "normal"
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    on_disk = json.loads((tmp_config_dir / "slot_state.json").read_text(encoding="utf-8"))
    assert set(on_disk["held"]) == {"alice"}

    _poll(mon, {})  # alice's broadcast ends

    entry = mon.queued_vods["alice"]
    assert entry["verify"] is True and entry["item"]["verify"] is True
    assert {"streamer": "alice", "url": sv.save_url("alice"), "reason": "held_check"} in _named(events, "vod_queued")
    assert mon.held_save_items == {}
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def test_as02_live_without_a_tab_goes_at_once(mon):
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    _report([])  # the extension reports, but alice has no tab (Max open streams closed it)

    sm.handle_streak_event(_card("alice"))

    assert mon.held_save_items == {}
    assert "alice" in mon.queued_vods
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def test_as02_am32_an_offline_streamer_with_a_tab_left_open_goes_at_once(mon):
    """A Stream Monitor tab left on bob's channel page does not hold his
    item: he is not live, so no offline edge is coming."""
    _poll(mon, {})
    _report(["bob"])

    sm.handle_streak_event(_card("bob"))

    assert mon.held_save_items == {}
    assert "bob" in mon.queued_vods
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["bob"]


def test_as02_am32_a_report_older_than_the_alive_window_does_not_hold(mon):
    """The last report lists alice's tab, but it is older than
    SLOT_EXECUTOR_ALIVE_SECONDS: that browser may be gone."""
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    _report(["alice"])
    sm._open_tabs_reports["chrome"]["mono"] -= ss.SLOT_EXECUTOR_ALIVE_SECONDS + 1

    sm.handle_streak_event(_card("alice"))

    assert mon.held_save_items == {}
    assert "alice" in mon.queued_vods
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def _held_item(login):
    return sv.make_card_item(login, "broke", 5, NOW_EPOCH - 2 * HOUR, None,
                             {"card_age_s": HOUR, "card_age_unit_s": HOUR, "source": "bell"},
                             NOW_EPOCH - 2 * HOUR)


def _fresh_start_with_held(mon, tmp_config_dir, held, **state):
    """What a relaunch starts from: slot_state.json with `held`, and every
    streamer as never seen (their broadcast may have ended meanwhile)."""
    data = {"v": 1, "saved_at": NOW_EPOCH - 2 * HOUR, "held": held}
    data.update(state)
    (tmp_config_dir / "slot_state.json").write_text(json.dumps(data), encoding="utf-8")
    mon.streamers = {name: sm.StreamerState(name=name) for name in STREAMERS}
    mon.live_streamers = []
    mon._load_slot_state()
    assert set(mon.held_save_items) == set(held)


def _hold(mon, login="alice"):
    """login is live with a Stream Monitor tab and a card arrives: held."""
    _poll(mon, {login: "2026-09-29T10:00:00Z"})
    _report([login])
    assert sm.handle_streak_event(_card(login, age=HOUR, unit=HOUR))["item"] is True
    assert login in mon.held_save_items and login not in mon.queued_vods


def test_o01_a_held_item_restored_after_a_restart_is_checked_when_the_broadcast_is_over(
        mon, events, tmp_config_dir):
    """alice's broadcast ended while the desktop was off, so no offline edge
    is ever seen. The first poll that finds her offline releases the held
    item as the check; a failed re-auth answer is not a poll (rule 7)."""
    _fresh_start_with_held(mon, tmp_config_dir, {"alice": _held_item("alice")})

    with patch.object(mon, "_api_get", return_value=None):
        mon.process_state_changes(mon.check_streams())
    assert set(mon.held_save_items) == {"alice"}
    assert mon.queued_vods == {} and mon.rescue_pending is None

    _poll(mon, {})

    assert mon.held_save_items == {}
    entry = mon.queued_vods["alice"]
    assert entry["verify"] is True and entry["item"]["verify"] is True
    assert {"streamer": "alice", "url": sv.save_url("alice"), "reason": "held_check"} in _named(
        events, "vod_queued")
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]
    on_disk = json.loads((tmp_config_dir / "slot_state.json").read_text(encoding="utf-8"))
    assert on_disk["held"] == {}


def test_o01_a_held_item_restored_while_the_streamer_is_still_live_waits_for_the_edge(
        mon, events, tmp_config_dir):
    _fresh_start_with_held(mon, tmp_config_dir, {"alice": _held_item("alice")})
    _report(["alice"])

    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    assert set(mon.held_save_items) == {"alice"} and mon.queued_vods == {}

    _poll(mon, {})  # the real offline edge

    assert mon.held_save_items == {}
    assert mon.queued_vods["alice"]["verify"] is True
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def test_o01_a_held_item_of_a_streamer_taken_off_the_list_is_checked(mon, events):
    _hold(mon, "alice")
    del mon.streamers["alice"]  # a settings save took alice off the list

    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})

    assert mon.held_save_items == {}
    assert mon.queued_vods["alice"]["verify"] is True
    assert {"streamer": "alice", "url": sv.save_url("alice"), "reason": "held_check"} in _named(
        events, "vod_queued")
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def test_o01_a_held_item_restored_while_slot_mode_waited_is_checked_after_turning_off(
        mon, events, tmp_config_dir, monkeypatch):
    """Slot mode was waiting for the extension (it takes held items in only
    when it becomes alive), so alice's offline edge passed it by. Turned
    off, the next poll that finds her offline releases the held item."""
    # alice's live edge opens her stream the 1.11 way while Slot mode waits;
    # record that open instead of handing it to the default browser.
    opened = []
    monkeypatch.setattr(sm.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    mon.config.slot_mode = True
    _fresh_start_with_held(mon, tmp_config_dir, {"alice": _held_item("alice")},
                           saved_at=NOW_EPOCH - 30, executor_seen=True)
    mon._slot_tick()
    assert mon.slot_state == "waiting"
    _poll(mon, {"alice": "2026-09-29T10:00:00Z"})
    _poll(mon, {})  # alice's offline edge, while Slot mode runs the plan
    assert set(mon.held_save_items) == {"alice"} and mon.queued_vods == {}

    mon.config.slot_mode = False
    mon._slot_tick()
    assert mon.slot_state == "off"
    _poll(mon, {})

    assert mon.held_save_items == {}
    assert mon.queued_vods["alice"]["verify"] is True
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]
    # Let the paced open worker finish while the recorder is still in place.
    assert mon.wait_for_pending_opens(timeout=10)
    assert all("alice" in url for url in opened)


def test_o01_a_card_after_the_tab_is_gone_releases_the_held_item_and_goes_at_once(mon, events):
    """The held item waited for alice's offline edge because her tab was the
    remedy. Her tab is gone now: a new card goes out at once, with the held
    check merged into it (a real save, verify false)."""
    _hold(mon, "alice")
    _report([])  # the tab was closed; alice is still live

    answer = sm.handle_streak_event(_card("alice", count=6, age=60, unit=60))

    assert answer == {"verdict": "fresh", "item": True}
    assert mon.held_save_items == {}
    entry = mon.queued_vods["alice"]
    assert entry["verify"] is False and entry["item"]["count"] == 6
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]
    added = _named(events, "streak_item_added")
    assert added[-1]["merged"] is True and added[-1]["verify"] is False


def test_o01_a_card_before_the_first_poll_after_a_restart_does_not_join_the_held_item(
        mon, tmp_config_dir):
    """A card (or a Streaks at Risk click) that arrives before the first poll
    finds alice not live: the restored held item is not kept waiting."""
    _fresh_start_with_held(mon, tmp_config_dir, {"alice": _held_item("alice")})

    sm.handle_streak_event(_card("alice", count=6, age=60, unit=60))

    assert mon.held_save_items == {}
    assert mon.queued_vods["alice"]["verify"] is False
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]


def test_o01_a_card_while_the_streamer_is_live_with_a_tab_stays_held(mon, events):
    _hold(mon, "alice")

    answer = sm.handle_streak_event(_card("alice", count=6, age=60, unit=60))

    assert answer == {"verdict": "fresh", "item": True}
    assert set(mon.held_save_items) == {"alice"}
    assert mon.held_save_items["alice"]["count"] == 6
    assert mon.queued_vods == {} and mon.rescue_pending is None


def test_o10_setting_off_makes_nothing(mon, monkeypatch, toasts):
    calls = []
    monkeypatch.setattr(sm, "_monitor_submitter", lambda kind, payload: calls.append(kind))
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = False
    mon.config.auto_save_streaks = False

    answer = sm.handle_streak_event(_card(age=HOUR, unit=HOUR))

    assert answer == {"verdict": "fresh", "item": False}
    assert calls == [] and mon.queued_vods == {} and mon.rescue_pending is None
    assert len(toasts) == 1  # still logged and toasted
    # The monitor drops an item that arrives after the setting went off.
    item = sv.make_card_item("zed", "broke", 5, NOW_EPOCH, None, {"source": "bell"}, NOW_EPOCH)
    assert mon._normal_save_item(item) is False and mon.queued_vods == {}


def test_am08_expired_entries_are_dropped_only_with_a_deadline(mon, events):
    mon.queued_vods = {
        "old": {"url": sv.save_url("old"), "ended_at": sm._epoch_to_iso(NOW_EPOCH - 30 * HOUR),
                "deadline_at": sm._epoch_to_iso(NOW_EPOCH - 6 * HOUR), "age_unit_s": 0},
        "edge": {"url": sv.save_url("edge"), "ended_at": sm._epoch_to_iso(NOW_EPOCH - 25 * HOUR),
                 "deadline_at": sm._epoch_to_iso(NOW_EPOCH - 30 * 60), "age_unit_s": HOUR},
        "legacy": {"url": sv.save_url("legacy"), "ended_at": "2020-01-01T00:00:00.000Z"},
        "junk": {"url": sv.save_url("junk"), "ended_at": "x", "deadline_at": "not a date"},
        "live": {"url": sv.save_url("live"), "ended_at": sm._epoch_to_iso(NOW_EPOCH - HOUR),
                 "deadline_at": sm._epoch_to_iso(NOW_EPOCH + 23 * HOUR)},
    }

    cands = mon._build_rescue_candidates(set())

    # "edge" is past its deadline but within its age unit: kept.
    assert {c["streamer"] for c in cands} == {"edge", "legacy", "junk", "live"}
    assert all(set(c) == {"streamer", "url", "kind", "ended_at"} for c in cands)
    assert _named(events, "streak_item_expired") == [
        {"streamer": "old", "deadline_at": sm._epoch_to_iso(NOW_EPOCH - 6 * HOUR)}]

    mon.queued_vods["stale"] = {"url": sv.save_url("stale"), "ended_at": "x",
                                "deadline_at": sm._epoch_to_iso(NOW_EPOCH - 2 * HOUR)}
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mweb:
        assert mon._flush_queued_vods(reason="test") == 4
        assert mon.wait_for_pending_opens(timeout=5)
    opened = {c.args[0] for c in mweb.call_args_list}
    assert sv.save_url("stale") not in opened and sv.save_url("legacy") in opened


def _owner_live_vod_fallback(mon, monkeypatch):
    """The owner is live (auto-paused) with VOD fallback on; returns a
    settable clock."""
    clock = {"now": NOW_EPOCH}
    monkeypatch.setattr(sm, "_streak_clock", lambda: clock["now"])
    mon.config.own_channel = "me"
    mon.config.im_live_pause = True
    mon.config.vod_fallback = True
    return clock


def test_as02_am32_an_offline_edge_merges_into_a_queued_card_and_keeps_its_deadline(
        mon, events, monkeypatch):
    """alice went live while the owner was live, so she has no tab. Her
    in-danger card (2 hours left) is queued. Her offline edge, with the
    owner still live, merges into that entry: one entry, the card's
    earlier deadline."""
    clock = _owner_live_vod_fallback(mon, monkeypatch)
    _poll(mon, {"me": "2026-09-29T09:00:00Z", "alice": "2026-09-29T10:00:00Z"})
    assert mon.auto_paused and "alice" in mon.missed_while_paused
    answer = sm.handle_streak_event(_card("alice", status="in_danger", count=7, deadline_hours=2))
    assert answer["item"] is True
    assert sm._iso_to_epoch(mon.queued_vods["alice"]["deadline_at"]) == NOW_EPOCH + 2 * HOUR

    clock["now"] = NOW_EPOCH + 600
    _poll(mon, {"me": "2026-09-29T09:00:00Z"})  # alice ends; the owner is still live

    assert list(mon.queued_vods) == ["alice"]
    entry = mon.queued_vods["alice"]
    assert sm._iso_to_epoch(entry["deadline_at"]) == NOW_EPOCH + 2 * HOUR
    item = entry["item"]
    assert (item["kind"], item["origin"], item["verify"]) == ("missed", "offline_edge", False)
    assert item["deadline_at"] == NOW_EPOCH + 2 * HOUR
    assert entry["ended_at"] == sm._epoch_to_iso(NOW_EPOCH + 600)
    reasons = [f["reason"] for f in _named(events, "vod_queued") if f["streamer"] == "alice"]
    assert reasons == ["card", "auto_paused"]
    assert mon.rescue_pending is None

    _poll(mon, {})  # the owner ends: the auto-pause lifts
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["alice"]
    assert sm._iso_to_epoch(mon.queued_vods["alice"]["deadline_at"]) == NOW_EPOCH + 2 * HOUR


@pytest.mark.parametrize("gap_hours, deadline_hours", [(3, 24), (30, 54)])
def test_am08_am32_a_second_missed_broadcast_keeps_the_earlier_open_deadline(
        mon, monkeypatch, gap_hours, deadline_hours):
    """Two broadcasts of alice missed while the owner is live. The second
    offline edge merges into the first one's link: its deadline stays while
    it is open, and the new one applies once it has passed."""
    clock = _owner_live_vod_fallback(mon, monkeypatch)
    _poll(mon, {"me": "2026-09-29T09:00:00Z", "alice": "2026-09-29T10:00:00Z"})
    _poll(mon, {"me": "2026-09-29T09:00:00Z"})  # the first broadcast ends at NOW
    first = mon.queued_vods["alice"]
    assert "item" not in first
    assert sm._iso_to_epoch(first["deadline_at"]) == NOW_EPOCH + 24 * HOUR

    clock["now"] = NOW_EPOCH + (gap_hours - 1) * HOUR
    _poll(mon, {"me": "2026-09-29T09:00:00Z", "alice": sm._epoch_to_iso(clock["now"])})
    assert "alice" in mon.missed_while_paused
    clock["now"] = NOW_EPOCH + gap_hours * HOUR
    _poll(mon, {"me": "2026-09-29T09:00:00Z"})  # the second broadcast ends

    assert list(mon.queued_vods) == ["alice"]
    entry = mon.queued_vods["alice"]
    assert sm._iso_to_epoch(entry["deadline_at"]) == NOW_EPOCH + deadline_hours * HOUR
    assert entry["ended_at"] == sm._epoch_to_iso(NOW_EPOCH + gap_hours * HOUR)
    assert entry["item"]["kind"] == "missed"
    assert entry["item"]["deadline_at"] == NOW_EPOCH + deadline_hours * HOUR


# ---------------------------------------------------------------------------
# Slot mode (DESIGN 11.4)
# ---------------------------------------------------------------------------


def test_as02_a_card_becomes_an_item_under_the_deadline_rules(mon):
    _slot_mode(mon)
    explicit = "2026-09-30T02:00:00.000Z"
    cards = {
        "ann": _card("ann", age=3 * HOUR, unit=HOUR),                        # broke, age known
        "ben": _card("ben", status="in_danger", deadline_hours=5, age=1200),  # in danger, age known
        "cat": _card("cat", count=None, source="link", age=None),            # a link event
        "dan": _card("dan", deadline_at=explicit),                            # an explicit deadline
        "eve": _card("eve"),                                                  # age unknown
    }
    for payload in cards.values():
        assert sm.handle_streak_event(payload) == {"verdict": "fresh", "item": True}

    items = mon.slot.items
    assert items["ann"]["break_at"] == NOW_EPOCH - 4 * HOUR
    assert items["ann"]["deadline_at"] == NOW_EPOCH + 20 * HOUR
    assert items["ann"]["age_unit_s"] == HOUR
    assert items["ben"]["kind"] == "in_danger"
    assert items["ben"]["break_at"] == NOW_EPOCH - 1200 - 60
    assert items["ben"]["deadline_at"] == NOW_EPOCH - 1260 + 5 * HOUR
    assert (items["cat"]["kind"], items["cat"]["count"], items["cat"]["origin"]) == ("broke", None, "link")
    assert items["cat"]["age_unit_s"] == 0 and items["cat"]["deadline_at"] == NOW_EPOCH + 24 * HOUR
    assert items["dan"]["deadline_at"] == sm._iso_to_epoch(explicit)
    assert (items["eve"]["break_at"], items["eve"]["age_unit_s"]) == (NOW_EPOCH, 0)
    assert all(items[n]["url"] == sv.save_url(n) for n in items)
    assert {items[n]["origin"] for n in ("ann", "ben", "dan", "eve")} == {"card"}

    # One item per streamer: a broke card merging into ben's in-danger item
    # takes the broke deadline.
    sm.handle_streak_event(_card("ben", count=6, age=60, unit=60))
    assert items["ben"]["kind"] == "broke"
    assert items["ben"]["deadline_at"] == NOW_EPOCH - 120 + 24 * HOUR


def test_ap05_an_unverified_login_never_becomes_an_item(mon, toasts):
    _slot_mode(mon)
    for payload in (_card("zed", login_verified=False), _card("directory"), _card("Bad Name!")):
        assert sm.handle_streak_event(payload) == {"verdict": "fresh", "item": False}
    assert mon.slot.items == {}
    assert len(toasts) == 3


def test_o16_am12_a_manual_event_is_accepted_with_the_setting_off_and_heads_the_urgent_band(mon, events):
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = False
    mon.config.auto_save_streaks = False
    _slot_mode(mon, {name: "2026-09-29T10:00:00Z" for name in ("bob", "carol", "dave", "erin")})
    assert all(s["streamer"] for s in _plan()["slots"])  # every slot is taken

    answer = sm.handle_streak_event(_card("zed", source="manual"))

    assert answer == {"verdict": "fresh", "item": True}
    assert mon.slot.items["zed"]["origin"] == "manual"
    head = _plan()["queue"][0]
    assert (head["streamer"], head["entry"], head["urgent"], head["manual"]) == ("zed", "save", True, True)
    assert [q["streamer"] for q in _plan()["queue"]] == ["zed", "erin"]
    (added,) = [f for f in _named(events, "streak_item_added") if f["streamer"] == "zed"]
    assert (added["origin"], added["mode"]) == ("manual", "slot")


def test_o16_a_manual_event_without_a_count_is_accepted_in_slot_mode(mon):
    _slot_mode(mon)
    answer = sm.handle_streak_event(_card("zed", count=None, source="manual"))
    assert answer == {"verdict": "fresh", "item": True}
    item = mon.slot.items["zed"]
    assert (item["count"], item["origin"], item["kind"]) == (None, "manual", "broke")


def test_r26_a_dismissed_streamers_item_waits(mon):
    started = "2026-09-29T10:00:00Z"
    _slot_mode(mon, {"bob": started})
    assert "bob" in [s["streamer"] for s in _plan()["slots"]]
    # The owner closes bob's slot tab: dismissed for this broadcast.
    assert sm.record_extension_open_tabs(
        "chrome", [], "tabs-changed", instance=INSTANCE, plan_seq=_plan()["seq"],
        gone=[{"streamer": "bob", "reason": "user_closed", "at": int(NOW_EPOCH)}])
    mon._slot_tick()
    assert "bob" in _plan()["dismissed"]

    assert sm.handle_streak_event(_card("bob", age=HOUR, unit=HOUR))["item"] is True

    assert "bob" in mon.slot.items
    assert "bob" not in [q["streamer"] for q in _plan()["queue"]]
    assert "bob" not in [s["streamer"] for s in _plan()["slots"]]
    # The broadcast ends: the dismissal goes and the item gets its turn.
    _poll(mon, {})
    _poll(mon, {})
    assert "bob" in [s["streamer"] for s in _plan()["slots"]] + [q["streamer"] for q in _plan()["queue"]]


def test_as10_already_saved_frees_the_slot_and_advances_in_the_same_replan(mon, events):
    _slot_mode(mon)
    # zed's window closes in about 3 hours, yuri's in about 22.
    sm.handle_streak_event(_card("zed", age=20 * HOUR, unit=HOUR))
    sm.handle_streak_event(_card("yuri", age=HOUR, unit=HOUR))
    cycle = next(s for s in _plan()["slots"] if s["id"] == "cycle-1")
    assert (cycle["streamer"], cycle["entry"]) == ("zed", "save")

    answer = sm.handle_streak_event({"status": "already_saved", "streamer": "zed", "count": 5,
                                     "detected_at": NOW})

    assert answer == {"verdict": "saved", "item": True}
    assert "zed" not in mon.slot.items
    assert {"streamer": "zed", "reason": "already_saved"} in _named(events, "streak_item_done")
    # The replan that answered already put the next save in the slot.
    cycle = next(s for s in _plan()["slots"] if s["id"] == "cycle-1")
    assert (cycle["streamer"], cycle["entry"]) == ("yuri", "save")
