"""Slot mode inside the tray (1.12.0): the monitor feeds the scheduler,
applies what it returns, and stands the 1.11 opening paths down while Slot
mode runs the plan.

TwitchMonitor with Helix answers canned through _api_get and tab opens
caught at open_stream or webbrowser.open. Reports come in through
record_extension_open_tabs, as POST /open_tabs stores them.
"""
import ast
import errno
import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import slot_scheduler as ss
import streak_saves as sv
import stream_monitor_tray as sm

INSTANCE = "1a2b3c4d"
EXECUTOR = "chrome-" + INSTANCE
STREAMERS = ["alice", "bob", "carol", "dave", "erin"]
START = "2026-09-29T10:00:00Z"


@pytest.fixture
def events(monkeypatch):
    """Every activity event, in order, as (event, fields)."""
    calls = []
    monkeypatch.setattr(sm, "log_activity", lambda event, **fields: calls.append((event, fields)))
    return calls


@pytest.fixture
def mon(monitor, tmp_config_dir, monkeypatch):
    """A monitor with five listed streamers, Slot mode and automatic saves
    on, Chrome as the default browser, and no state from other tests."""
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    monkeypatch.setattr(sm, "default_browser_family", lambda: "chrome")
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset())
    monkeypatch.setattr(sm, "_tray_notifier", None)
    monitor.config.streamers = list(STREAMERS)
    monitor.streamers = {name: sm.StreamerState(name=name) for name in STREAMERS}
    monitor.config.slot_mode = True
    monitor.config.auto_save_streaks = True
    return monitor


def _report(streamers=(), plan_seq=0, busy=None, gone=None, instance=INSTANCE, browser="chrome"):
    assert sm.record_extension_open_tabs(browser, list(streamers), "refresh", instance=instance,
                                         plan_seq=plan_seq, busy=busy, gone=gone)


def _helix(live):
    data = []
    for login, started in live.items():
        stream = {"user_login": login, "user_id": "1"}
        if started is not None:
            stream["started_at"] = started
        data.append(stream)
    return {"data": data}


def _poll(mon, live, api=None):
    """One poll: the canned Helix answer through check_streams, then
    process_state_changes (which ends with the scheduler tick). Returns the
    open_stream mock."""
    answer = _helix(live) if api is None else api
    with patch.object(mon, "_api_get", return_value=answer), \
         patch.object(mon, "open_stream") as mopen:
        status = mon.check_streams()
        if status:
            mon.process_state_changes(status)
    return mopen


def _plan():
    return sm.ConfigRequestHandler.config_data["slot_plan"]


def _slots():
    return {s["id"]: s["streamer"] for s in _plan()["slots"]}


def _named(events, name):
    return [fields for event, fields in events if event == name]


def _capture_inputs(mon, monkeypatch):
    """Record every scheduler input the monitor builds."""
    seen = []
    real = mon.slot.tick

    def tick(inp):
        seen.append(inp)
        return real(inp)

    monkeypatch.setattr(mon.slot, "tick", tick)
    return seen


def _startable(mon, monkeypatch, loop=None):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(mon, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(mon, "_monitor_loop", loop or (lambda: None))
    mon.thread = None


def _alive(mon, live=None, tabs=()):
    """A capable report from the executor, then a poll with `live` streams."""
    _report(tabs)
    mon._slot_tick()
    if live is not None:
        _poll(mon, live)
    _report(tabs, plan_seq=_plan()["seq"])


def _stop(mon):
    mon.stop()
    if mon.thread is not None:
        mon.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Rules 1 and 2: Slot mode off, and no capable extension
# ---------------------------------------------------------------------------


def test_r01_slot_mode_off_keeps_the_1_11_opens(mon, events):
    mon.config.slot_mode = False
    mopen = _poll(mon, {"alice": START})

    mopen.assert_called_once_with("alice")
    assert mon.streamers["alice"].browser_opened is True
    assert mon.slot_state == "off" and not mon.slot_active
    assert _plan() is None
    assert not [f for f in _named(events, "tab_open_skipped") if f["reason"] == "slot_mode"]


def test_r02_r43_none_ever_opens_like_1_11_and_notifies_once(mon, monkeypatch):
    # A 1.11 extension reports without a plan_seq: never capable.
    assert sm.record_extension_open_tabs("chrome", [], "refresh")
    mopen = _poll(mon, {"alice": START})

    assert mon.slot_state == "none_ever" and not mon.slot_active
    mopen.assert_called_once_with("alice")
    assert _plan()["state"] == "none_ever" and _plan()["active"] is False
    assert mon._status_calls[-1] == ss.TOOLTIP_NONE_EVER
    notice = ("Stream Monitor", "Slot mode needs browser extension 1.12 or newer. "
                                "Opening streams normally until it connects.")
    assert mon._notify_calls.count(notice) == 1

    # More polls, and a tray Stop then Start, enter none_ever again: the
    # notice is once per process.
    _poll(mon, {"alice": START, "bob": START})
    _startable(mon, monkeypatch)
    assert mon.start() is True
    mon.stop()
    assert mon.start() is True
    _poll(mon, {"alice": START})
    assert mon.slot_state == "none_ever"
    assert mon._notify_calls.count(notice) == 1


def test_r02_live_edge_with_a_capable_extension_skips_the_open(mon, events):
    _report()
    mon._slot_tick()
    assert mon.slot_state == "alive"

    mopen = _poll(mon, {"alice": START})

    mopen.assert_not_called()
    assert mon.streamers["alice"].browser_opened is True
    assert mon.missed_while_paused == {}
    assert {"streamer": "alice", "reason": "slot_mode"} in _named(events, "tab_open_skipped")
    assert "alice went LIVE! (Slot mode)" in mon._status_calls
    assert "alice" in _slots().values()


# ---------------------------------------------------------------------------
# Rule 42 (O14, A9): absent after having worked
# ---------------------------------------------------------------------------


def test_r42_o14_absent_opens_capped_through_the_paced_queue(mon, monkeypatch, events, tmp_config_dir):
    (tmp_config_dir / "slot_state.json").write_text(json.dumps({
        "v": 1, "saved_at": time.time() - 3600, "seq": 7, "executor_seen": True,
    }), encoding="utf-8")
    _startable(mon, monkeypatch)
    assert mon.start() is True
    assert mon.slot_state == "waiting"
    # The 90 s grace is over and no browser reports.
    mon.slot.startup_mono -= ss.SLOT_STARTUP_GRACE_SECONDS + 10
    live = {name: START for name in STREAMERS}
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mweb:
        mopen = _poll(mon, live)
        _poll(mon, live)
        _poll(mon, live)
        assert mon.wait_for_pending_opens(timeout=5)

    assert mon.slot_state == "absent"
    mopen.assert_not_called()  # the 1.11 go-live path stood down
    opened = [c.args[0] for c in mweb.call_args_list]
    assert len(opened) == 3 == mon.config.keep_open_slots + mon.config.cycle_slots
    assert sorted(opened) == sorted(s["url"] for s in _plan()["slots"])
    attempts = _named(events, "tab_open_attempt")
    assert [a["queue_reason"] for a in attempts] == ["slot_fallback"] * 3
    assert len(_named(events, "slot_fallback_open")) == 3
    assert _plan()["state"] == "absent" and _plan()["active"] is False
    assert mon._status_calls[-1] == ss.TOOLTIP_ABSENT


# ---------------------------------------------------------------------------
# Rule 32: a pause lift in Slot mode
# ---------------------------------------------------------------------------


def test_r32_pause_lift_in_slot_mode_publishes_no_offer(mon, events):
    mon.config.own_channel = "me"
    mon.config.im_live_pause = True
    _report()
    mon._slot_tick()
    assert mon.slot_active
    mon.auto_paused = True
    with patch("stream_monitor_tray.webbrowser.open") as mweb:
        _poll(mon, {"alice": START})
        assert mon.wait_for_pending_opens(timeout=5)

    assert mon.auto_paused is False
    assert mon.rescue_pending is None
    assert sm.ConfigRequestHandler.config_data.get("rescue") is None
    assert _named(events, "rescue_skipped") == [{"reason": "slot_mode"}]
    assert not _named(events, "rescue_offered")
    mweb.assert_not_called()


# ---------------------------------------------------------------------------
# 12.5 C4: saved streamers not on the list
# ---------------------------------------------------------------------------


def test_as03_dc4_saved_unmonitored_logins_join_the_poll_within_100(mon, monkeypatch):
    now = time.time()
    monkeypatch.setattr(sm, "_streak_clock", lambda: now)
    mon.config.own_channel = "me"
    mon.config.im_live_pause = True
    saved = {f"u{i:03d}": {"at": sm._epoch_to_iso(now - 3600 + i), "count": 3} for i in range(120)}
    saved["alice"] = {"at": sm._epoch_to_iso(now - 60), "count": 3}  # on the list: not an extra
    sm._streak_state["saved"] = saved
    sent = []

    def api(url, params):
        sent.append([value for _, value in params])
        return {"data": []}

    monkeypatch.setattr(mon, "_api_get", api)
    mon.check_streams()

    (logins,) = sent
    assert len(logins) == sm.HELIX_MAX_LOGINS
    assert logins[:6] == STREAMERS + ["me"]
    newest = [f"u{i:03d}" for i in range(119, 119 - (sm.HELIX_MAX_LOGINS - 6), -1)]
    assert logins[6:] == newest
    assert sm._polled_streamers == frozenset(STREAMERS + newest)
    assert mon._saved_extra_logins == newest


def test_as03_dc4_a_live_unmonitored_save_is_lifted(mon, monkeypatch, events):
    now = time.time()
    monkeypatch.setattr(sm, "_streak_clock", lambda: now)
    saved_at = sm._epoch_to_iso(now - 3600)
    started = sm._epoch_to_iso(now - 600)
    sm._streak_state["saved"]["zed"] = {"at": saved_at, "count": 4}
    _report()
    mon._slot_tick()
    seen = _capture_inputs(mon, monkeypatch)

    _poll(mon, {"zed": started, "alice": START})

    assert sm.streak_saved_since_last_live("zed") is None
    assert "zed" not in sm._streak_state["saved"]
    assert _named(events, "streak_save_lifted") == [{"streamer": "zed", "started_at": started}]
    assert mon.live_streamers == ["alice"]
    assert "zed" not in seen[-1]["poll"] and "alice" in seen[-1]["poll"]


def test_as03_dc4_extras_count_as_polled(mon, monkeypatch):
    now = time.time()
    monkeypatch.setattr(sm, "_streak_clock", lambda: now)
    saved_at = sm._epoch_to_iso(now - 3600)
    sm._streak_state["saved"]["zed"] = {"at": saved_at, "count": 4}

    _poll(mon, {})

    assert "zed" in sm._polled_streamers
    # A day and more later the save still counts: zed is polled, so only a
    # go-live (or the 7-day cap) ends it.
    later = now + sm.SAVED_STREAK_UNPOLLED_TTL_SECONDS + 3600
    assert sm.streak_saved_since_last_live("zed", now_epoch=later) == saved_at


# ---------------------------------------------------------------------------
# Rules 6, 7, 34, 44, 45: what reaches the scheduler, and when
# ---------------------------------------------------------------------------


def test_r06_started_at_reaches_the_scheduler_and_a_missing_one_is_substituted(mon, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(sm, "_streak_clock", lambda: clock[0])
    seen = _capture_inputs(mon, monkeypatch)
    first = clock[0]

    _poll(mon, {"alice": START, "bob": None})
    assert seen[-1]["authoritative"] is True
    assert seen[-1]["poll"] == {"alice": START, "bob": sm._epoch_to_iso(first)}

    clock[0] += 60
    _poll(mon, {"alice": START, "bob": None})
    assert seen[-1]["poll"] == {"alice": START, "bob": sm._epoch_to_iso(first)}

    # bob leaves the live set; his next broadcast gets a new substitute.
    clock[0] += 60
    _poll(mon, {"alice": START})
    assert seen[-1]["poll"] == {"alice": START}
    clock[0] += 60
    _poll(mon, {"bob": None})
    assert seen[-1]["poll"] == {"bob": sm._epoch_to_iso(clock[0])}
    assert seen[-1]["live_as_of"] == clock[0]


def test_r07_a_failed_reauth_poll_is_not_authoritative(mon, monkeypatch):
    _alive(mon, {"alice": START, "bob": START}, tabs=())
    assert "alice" in _slots().values()
    seen = _capture_inputs(mon, monkeypatch)

    with patch.object(mon, "_api_get", return_value=None), patch.object(mon, "open_stream"):
        for _ in range(3):
            mon.process_state_changes(mon.check_streams())

    assert [inp["authoritative"] for inp in seen] == [False, False, False]
    assert all(inp["poll"] is None for inp in seen)
    # No offline strikes: alice keeps her slot through three failed polls.
    assert "alice" in _slots().values() and "bob" in _slots().values()


def test_r34_a_busy_change_on_open_tabs_wakes_the_monitor_and_the_answer_waits(mon, monkeypatch):
    import urllib.request

    monkeypatch.setattr(mon, "_api_get", lambda url, params: _helix({"alice": START}))
    monkeypatch.setattr(mon, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(sm, "_monitor_submitter", lambda kind, payload: mon.submit(kind, payload))
    mon.config.check_interval = 60
    _report()
    server = sm._SingletonHTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = server.server_address[1]

    def post(busy):
        body = json.dumps({"browser": "chrome", "instance": INSTANCE, "streamers": [],
                           "reason": "paused", "plan_seq": 1, "busy": busy}).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/open_tabs", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        began = time.monotonic()
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 204
        return time.monotonic() - began

    ticks = []
    real_tick = mon._slot_tick
    monkeypatch.setattr(mon, "_slot_tick", lambda authoritative=False: ticks.append(
        threading.current_thread()) or real_tick(authoritative))
    try:
        assert mon.start() is True
        deadline = time.monotonic() + 5
        while mon._loop_alive_gen != mon._loop_gen and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # the first iteration polls, ticks and starts waiting
        assert mon.slot_state == "alive"
        before = len(ticks)

        took = post("paused")

        # The answer came after the monitor thread replanned with the busy
        # report, well within the 2 s wait.
        assert took < sm.SLOT_REPLAN_WAIT_SECONDS
        assert len(ticks) > before and ticks[-1] is mon.thread
        assert mon._status_calls[-1] == ss.TOOLTIP_BUSY
        # The same busy state again changes nothing: no wake, no wait.
        before = len(ticks)
        post("paused")
        time.sleep(0.1)
        assert len(ticks) == before
    finally:
        _stop(mon)
        server.shutdown()
        server.server_close()


def test_r44_the_wake_gap_reaches_the_scheduler_once_and_restarts_watching(mon, monkeypatch, events):
    _alive(mon, {"alice": START, "bob": START}, tabs=("alice", "bob"))
    mon._slot_tick()  # confirms both slots
    mon._watch_start = {"alice": 1.0, "bob": 1.0}
    seen = _capture_inputs(mon, monkeypatch)
    interval = mon.config.check_interval

    mon._note_loop_gap(interval + 1000)
    assert mon._watch_start == {}
    mon._slot_tick()
    mon._slot_tick()

    assert [inp["wake_gap"] for inp in seen] == [1000, 0]
    assert _named(events, "wake_detected")
    assert _named(events, "slot_clock_shifted") == [{"gap_s": 1000, "reason": "sleep"}]


def test_r45_ticks_every_30_seconds_between_polls(mon, monkeypatch):
    monkeypatch.setattr(sm, "SLOT_TICK_MAX_GAP_SECONDS", 0.1)
    polls = []
    monkeypatch.setattr(mon, "_api_get", lambda url, params: polls.append(1) or {"data": []})
    monkeypatch.setattr(mon, "_get_oauth_token", lambda: True)
    mon.config.check_interval = 60
    ticks = []
    real_tick = mon._slot_tick
    monkeypatch.setattr(mon, "_slot_tick", lambda authoritative=False: ticks.append(
        authoritative) or real_tick(authoritative))
    try:
        assert mon.start() is True
        time.sleep(1.0)
    finally:
        _stop(mon)

    assert len(polls) == 1  # one poll: the next is a minute away
    assert ticks.count(False) >= 5  # the start tick, the loop tick, then every 0.1 s


def test_r45_am45_c03_every_tick_republishes_the_plan_with_a_fresh_generated_at(mon, monkeypatch):
    """A45: the extension holds a reopen while plan.generated_at is not
    after the gone entry's second, so a plan whose content does not change
    is still republished by every tick with that tick's own generated_at."""
    clock = [1_790_000_000.4]
    monkeypatch.setattr(sm, "_streak_clock", lambda: clock[0])
    _report(())
    mon._slot_tick()
    first = _plan()
    assert first["generated_at"] == 1_790_000_000

    clock[0] += sm.SLOT_TICK_MAX_GAP_SECONDS
    mon._slot_tick()
    second = _plan()

    assert second["seq"] == first["seq"]
    assert second["generated_at"] == 1_790_000_000 + sm.SLOT_TICK_MAX_GAP_SECONDS
    assert ({k: v for k, v in second.items() if k != "generated_at"}
            == {k: v for k, v in first.items() if k != "generated_at"})


# ---------------------------------------------------------------------------
# Rule 24 (O9, A31): the offline edge in Slot mode
# ---------------------------------------------------------------------------


def test_r24_o09_slot_mode_offline_edge_opens_no_vod_even_with_vod_fallback_on(mon):
    mon.config.vod_fallback = True
    _alive(mon, {"alice": START})
    mon.missed_while_paused["alice"] = "10:00:00"
    with patch("stream_monitor_tray.webbrowser.open") as mweb:
        _poll(mon, {})
        _poll(mon, {})
        assert mon.wait_for_pending_opens(timeout=5)

    mweb.assert_not_called()
    assert mon.queued_vods == {}
    assert mon.missed_while_paused == {}
    assert mon.streamers["alice"].was_live is False
    # The unserved broadcast became a save item in the scheduler instead.
    assert mon.slot.items["alice"]["origin"] == "offline_edge"


def test_r24_am31_offline_edge_records_missed_when_unserved(mon):
    _alive(mon, {"alice": START, "bob": START})
    # bob's broadcast was served (a full turn); alice's was not.
    mon.slot.served["bob"] = {"session": START, "at": time.time(), "how": "turn"}

    _poll(mon, {})

    assert "alice" in sm._streak_state["missed_end"]
    assert "bob" not in sm._streak_state["missed_end"]
    assert "bob" in sm._streak_state["last_offline"]


# ---------------------------------------------------------------------------
# The inbox (9.4) and the loop generation
# ---------------------------------------------------------------------------


def _card_item(login="zed", now=None):
    now = time.time() if now is None else now
    return sv.make_card_item(login, "broke", 5, now, None, {"card_age_s": 3600, "card_age_unit_s": 3600,
                                                            "source": "bell"}, now)


def test_c13_an_item_submitted_while_stopped_is_processed_after_start(mon, monkeypatch):
    mon.config.slot_mode = False
    monkeypatch.setattr(mon, "_api_get", lambda url, params: {"data": []})
    monkeypatch.setattr(mon, "_get_oauth_token", lambda: True)
    mon.paused = True  # no offer, so nothing opens
    item = mon.submit("streak_item", {"item": _card_item(), "merge_only": False})

    assert item.waitable is False  # the monitor is not running: nobody waits
    time.sleep(0.2)
    assert not item.done.is_set() and mon.queued_vods == {}
    try:
        assert mon.start() is True
        assert item.done.wait(5)
    finally:
        _stop(mon)
    assert item.result is True
    assert mon.queued_vods["zed"]["item"]["login"] == "zed"


def test_c13_inbox_items_are_processed_on_the_monitor_thread(mon, monkeypatch):
    mon.config.slot_mode = False
    mon.paused = True
    monkeypatch.setattr(mon, "_api_get", lambda url, params: {"data": []})
    monkeypatch.setattr(mon, "_get_oauth_token", lambda: True)
    mon.config.check_interval = 60
    threads = []
    real = mon._process_inbox_item
    monkeypatch.setattr(mon, "_process_inbox_item",
                        lambda item: threads.append(threading.current_thread()) or real(item))
    try:
        assert mon.start() is True
        deadline = time.monotonic() + 5
        while mon._loop_alive_gen != mon._loop_gen and time.monotonic() < deadline:
            time.sleep(0.01)
        result = []
        sender = threading.Thread(target=lambda: result.append(
            mon.submit("streak_item", {"item": _card_item(), "merge_only": False})))
        sender.start()
        sender.join()
        (item,) = result
        assert item.waitable is True
        assert item.done.wait(sm.SLOT_REPLAN_WAIT_SECONDS)
    finally:
        _stop(mon)
    assert threads == [mon.thread]
    assert item.result is True and "zed" in mon.queued_vods


def test_c13_a_stale_loop_generation_does_not_tick(mon, monkeypatch):
    ticked = []
    real = mon.slot.tick
    monkeypatch.setattr(mon.slot, "tick", lambda inp: ticked.append(inp) or real(inp))
    polled = []
    monkeypatch.setattr(mon, "check_streams", lambda: polled.append(1) or {})
    mon.running = True
    mon._loop_gen = 5
    out = []

    def stale():
        out.append(mon._slot_tick())
        mon._monitor_loop()

    thread = threading.Thread(target=stale)
    thread._sm_loop_gen = 4  # started by an earlier start()
    thread.start()
    thread.join(timeout=5)

    assert out == [None]
    assert ticked == [] and polled == []
    # The current generation ticks as usual.
    assert mon._slot_tick() is not None and len(ticked) == 1


# ---------------------------------------------------------------------------
# Rule 46: turning Slot mode off
# ---------------------------------------------------------------------------


def _turn_off(mon, monkeypatch):
    _startable(mon, monkeypatch)
    new_config = sm.Config(**dict(asdict(mon.config), slot_mode=False))
    app = SimpleNamespace(config=mon.config, monitor=mon)
    with patch.object(mon, "open_stream") as mopen:
        sm.StreamMonitorApp._apply_config_change(app, new_config)
    return mopen


@pytest.mark.parametrize("paused", [False, True])
def test_r46_turning_off_opens_live_streams_without_a_tab_except_dismissed(mon, monkeypatch, events, paused):
    live = {"alice": START, "bob": START, "carol": START, "dave": START}
    _alive(mon, live, tabs=("alice",))
    # carol closed her tab: dismissed for this broadcast.
    mon.slot.dismissed["carol"] = {"session": START, "at": time.time(), "reason": "user_closed"}
    mon.paused = paused

    mopen = _turn_off(mon, monkeypatch)

    assert _plan() is None and mon.slot_state == "off"
    assert _named(events, "slot_mode_inactive") == [{"reason": "setting_off"}]
    if paused:
        mopen.assert_not_called()
        assert set(mon.missed_while_paused) == {"bob", "dave"}
    else:
        assert sorted(c.args[0] for c in mopen.call_args_list) == ["bob", "dave"]
        assert mon.missed_while_paused == {}


def test_r46_pending_items_move_to_the_normal_path(mon, monkeypatch, events):
    _alive(mon, {})
    changed, _ = mon.slot.add_item(_card_item(), mon._slot_inputs(False, for_tick=False))
    assert changed

    with patch("stream_monitor_tray.webbrowser.open"):
        _turn_off(mon, monkeypatch)

    assert mon.queued_vods["zed"]["item"]["login"] == "zed"
    assert {"streamer": "zed", "url": sv.save_url("zed"), "reason": "slot_mode_off"} in _named(
        events, "vod_queued")
    assert [c["streamer"] for c in mon.rescue_pending["candidates"]] == ["zed"]


# ---------------------------------------------------------------------------
# Rules 38 and 39, A10: starts and restarts
# ---------------------------------------------------------------------------


def test_r39_a_settings_restart_keeps_the_scheduler(mon, monkeypatch, tmp_config_dir):
    _alive(mon, {"alice": START, "bob": START}, tabs=("alice", "bob"))
    mon._slot_tick()
    sched = mon.slot
    slots_before = [dict(s) for s in sched.slots]
    seq_before = sched.seq
    # A file that differs from memory: a restart must not read it.
    (tmp_config_dir / "slot_state.json").write_text(json.dumps({"v": 1, "seq": 999}), encoding="utf-8")
    _startable(mon, monkeypatch)

    mon.restart()

    assert mon.slot is sched
    assert sched.seq == seq_before
    assert [s["streamer"] for s in sched.slots] == [s["streamer"] for s in slots_before]
    assert mon.slot_state == "alive"


def test_r38_a_fresh_start_loads_slot_state_and_waits_90_seconds(mon, monkeypatch, tmp_config_dir):
    now = time.time()
    item = _card_item("zed", now)
    (tmp_config_dir / "slot_state.json").write_text(json.dumps({
        "v": 1, "saved_at": now - 30, "seq": 41, "executor": EXECUTOR, "executor_seen": True,
        "slots": [], "served": {"alice": {"session": START, "at": now - 100, "how": "turn"}},
        "excluded": {}, "dismissed": {}, "reissued": {}, "save_queue": {"zed": item},
        "draining": [], "held": {},
    }), encoding="utf-8")
    _startable(mon, monkeypatch)

    assert mon.start() is True
    assert mon.slot.seq >= 41
    assert "zed" in mon.slot.items and "alice" in mon.slot.served
    assert mon.slot_state == "waiting" and _plan()["state"] == "waiting"
    assert _plan()["assigning"] is False

    # The grace runs out with no report: absent.
    mon.slot.startup_mono -= ss.SLOT_STARTUP_GRACE_SECONDS + 1
    mon._slot_tick()
    assert mon.slot_state == "absent"

    # A tray Stop then Start is a fresh start too: state reloaded, and the
    # 90 s grace starts over.
    mon.stop()
    assert mon.start() is True
    assert mon.slot_state == "waiting"
    assert "zed" in mon.slot.items


def test_r38_c02_a_launch_in_slot_mode_never_serves_a_null_plan(mon, tmp_config_dir):
    """start_services publishes the plan before the config server answers
    (the monitor's own start waits for the Twitch token first): a null plan
    would tell the extension that Slot mode is off."""
    (tmp_config_dir / "slot_state.json").write_text(json.dumps({
        "v": 1, "saved_at": time.time() - 30, "seq": 12, "executor": EXECUTOR, "executor_seen": True,
    }), encoding="utf-8")
    sm.ConfigRequestHandler.config_data["slot_plan"] = None
    statuses = len(mon._status_calls)

    mon.prime_slot_plan()

    plan = _plan()
    assert plan is not None and plan["state"] == "waiting"
    assert plan["seq"] >= 12 and plan["assigning"] is False
    # Quiet: the monitor's start shows the notices and the tooltip.
    assert mon._notify_calls == [] and len(mon._status_calls) == statuses

    mon.config.slot_mode = False
    mon.prime_slot_plan()
    assert _plan() is None


def _saved_state(tmp_config_dir):
    return json.loads((tmp_config_dir / "slot_state.json").read_text(encoding="utf-8"))


def _stable_alive(mon, monkeypatch):
    """alice holds a confirmed slot on a controlled clock; returns the clock
    and alice's slot id."""
    clock = [time.time()]
    monkeypatch.setattr(sm, "_streak_clock", lambda: clock[0])
    _alive(mon, {"alice": START}, tabs=("alice",))
    mon._slot_tick()
    (slot_id,) = [sid for sid, login in _slots().items() if login == "alice"]
    return clock, slot_id


def test_r38_c07_a_stop_stamps_slot_state_so_a_quick_restart_keeps_the_slots(
        mon, monkeypatch, tmp_config_dir, events):
    """Nothing changed for 12 minutes, so nothing was written. A tray Stop
    (or the update relaunch) stamps the file, and a start 5 s later
    restores the slots and the executor instead of re-adopting the tabs."""
    clock, slot_id = _stable_alive(mon, monkeypatch)
    _startable(mon, monkeypatch)
    mon.running = True
    clock[0] += 12 * 60

    mon.stop()

    assert _saved_state(tmp_config_dir)["saved_at"] == clock[0]
    clock[0] += 5
    events.clear()
    assert mon.start() is True
    assert mon.slot.executor == EXECUTOR
    assert _slots()[slot_id] == "alice"
    _poll(mon, {"alice": START})
    assert _slots()[slot_id] == "alice"
    assert not _named(events, "slot_extra_adopted")


def test_r38_c07_a_stop_of_a_stopped_monitor_writes_nothing(mon, monkeypatch, tmp_config_dir):
    """Exit after a tray Stop calls stop() again. Stamping then would make a
    state frozen at the first stop look fresh to the next launch."""
    clock, slot_id = _stable_alive(mon, monkeypatch)
    _startable(mon, monkeypatch)
    mon.running = True
    clock[0] += 12 * 60
    mon.stop()
    first_stop = clock[0]

    clock[0] += 11 * 60
    mon.stop()

    assert _saved_state(tmp_config_dir)["saved_at"] == first_stop
    clock[0] += 5
    mon._load_slot_state()
    assert mon.slot.slots == [] and mon.slot.executor is None


def test_r38_c07_slot_state_is_refreshed_while_nothing_changes(mon, monkeypatch, tmp_config_dir):
    """A crash leaves no stop stamp, so a ticking monitor rewrites the file
    at least every SLOT_STATE_REFRESH_SECONDS even when no tick asks for a
    write, and not on every tick."""
    clock, slot_id = _stable_alive(mon, monkeypatch)
    real_tick = mon.slot.tick

    def tick(inp):
        result = real_tick(inp)
        result.persist = False  # isolate the refresh from real changes
        return result

    monkeypatch.setattr(mon.slot, "tick", tick)
    writes = []
    real_persist = mon._persist_slot_state
    monkeypatch.setattr(mon, "_persist_slot_state", lambda now: writes.append(now) or real_persist(now))

    for _ in range(24):  # 12 minutes of ticks, 30 s apart
        clock[0] += sm.SLOT_TICK_MAX_GAP_SECONDS
        mon._slot_tick()

    assert 1 <= len(writes) <= 3
    assert clock[0] - _saved_state(tmp_config_dir)["saved_at"] < sm.SLOT_STATE_REFRESH_SECONDS
    clock[0] += 5  # the crash, and a relaunch
    mon._load_slot_state()
    assert mon.slot.executor == EXECUTOR
    assert {s["id"]: s["streamer"] for s in mon.slot.slots}[slot_id] == "alice"


# ---------------------------------------------------------------------------
# 3.7: atomic writes outlast a reader that briefly holds the file (Windows)
# ---------------------------------------------------------------------------


class _Refusals:
    """Stands in for os.replace: the first `times` calls raise what `error`
    builds (every call when times is None), later calls replace for real.
    time.sleep only records, so a test never waits out the retry budget."""

    def __init__(self, monkeypatch, times, error=None):
        self.calls = 0
        self.sleeps = []
        self._times = times
        self._error = error or (lambda dst: PermissionError(errno.EACCES, "Access is denied", dst))
        self._real = sm.os.replace
        monkeypatch.setattr(sm.os, "replace", self)
        monkeypatch.setattr(sm.time, "sleep", self.sleeps.append)

    def __call__(self, src, dst):
        self.calls += 1
        if self._times is None or self.calls <= self._times:
            raise self._error(str(dst))
        self._real(src, dst)


def _write_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.getMessage().startswith("Could not write")]


def test_c07_a_slot_state_replace_refused_twice_is_retried_and_lands(
        mon, monkeypatch, tmp_config_dir, caplog):
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    path = tmp_config_dir / "slot_state.json"
    path.write_text('{"stale": true}', encoding="utf-8")
    refusals = _Refusals(monkeypatch, times=2)

    mon._persist_slot_state(1000.0)

    assert refusals.calls == 3
    assert refusals.sleeps == [0.05, 0.1]
    state = _saved_state(tmp_config_dir)
    assert state["v"] == 1 and state["saved_at"] == 1000.0 and state["held"] == {}
    assert mon._last_slot_persist == 1000.0
    assert not (tmp_config_dir / "slot_state.json.tmp").exists()
    assert _write_warnings(caplog) == []


def test_c07_a_slot_state_replace_refused_throughout_gives_up_within_the_bound(
        mon, monkeypatch, tmp_config_dir, caplog):
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    path = tmp_config_dir / "slot_state.json"
    path.write_text('{"stale": true}', encoding="utf-8")
    refusals = _Refusals(monkeypatch, times=None)

    mon._persist_slot_state(1000.0)

    assert refusals.sleeps[:3] == [0.05, 0.1, 0.2]
    assert max(refusals.sleeps) == sm.REPLACE_RETRY_MAX_DELAY
    assert sum(refusals.sleeps) == pytest.approx(sm.REPLACE_RETRY_SECONDS)
    assert refusals.calls == len(refusals.sleeps) + 1
    assert path.read_text(encoding="utf-8") == '{"stale": true}'
    assert not (tmp_config_dir / "slot_state.json.tmp").exists()
    assert mon._last_slot_persist is None
    (warning,) = _write_warnings(caplog)
    assert str(path) in warning and "Access is denied" in warning


@pytest.mark.parametrize("error", [
    lambda dst: OSError(errno.EIO, "I/O error", dst),
    lambda dst: FileNotFoundError(errno.ENOENT, "No such file", dst),
], ids=["eio", "not_found"])
def test_c07_a_slot_state_replace_failing_otherwise_is_not_retried(
        mon, monkeypatch, tmp_config_dir, caplog, error):
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    path = tmp_config_dir / "slot_state.json"
    path.write_text('{"stale": true}', encoding="utf-8")
    refusals = _Refusals(monkeypatch, times=None, error=error)

    mon._persist_slot_state(1000.0)

    assert refusals.calls == 1
    assert refusals.sleeps == []
    assert path.read_text(encoding="utf-8") == '{"stale": true}'
    assert not (tmp_config_dir / "slot_state.json.tmp").exists()
    assert len(_write_warnings(caplog)) == 1


def test_c07_the_replace_retry_also_stops_at_the_wall_clock_bound(monkeypatch, tmp_path):
    """A reader that makes each refused replace slow (0.15 s here) cannot
    stretch the retries past the bound: sleeps alone would allow 0.3 s plus
    four slow calls (0.9 s)."""
    monkeypatch.setattr(sm, "REPLACE_RETRY_SECONDS", 0.3)
    calls = []

    def slow_refusal(src, dst):
        calls.append(time.monotonic())
        time.sleep(0.15)
        raise PermissionError(errno.EACCES, "Access is denied", str(dst))

    monkeypatch.setattr(sm.os, "replace", slow_refusal)
    src, dst = tmp_path / "x.json.tmp", tmp_path / "x.json"
    src.write_text("{}", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(PermissionError):
        sm._replace_with_retry(src, dst)
    assert time.monotonic() - started < 0.7
    assert len(calls) >= 2
    assert not src.exists() and not dst.exists()


@pytest.mark.parametrize("writer", ["streak_state", "extension_tabs"])
def test_c07_every_atomic_write_retries_a_refused_replace(
        mon, monkeypatch, tmp_config_dir, writer):
    """streak_state.json and extension_tabs.json go through the same retry
    as slot_state.json."""
    live_at = sm._epoch_to_iso(time.time())
    if writer == "streak_state":
        path = tmp_config_dir / "streak_state.json"
        sm._streak_state["last_live"]["alice"] = live_at

        def run():
            with sm._streak_state_lock:
                sm._write_streak_state_locked()
    else:
        path = tmp_config_dir / "extension_tabs.json"

        def run():
            sm._persist_open_tabs_reports(
                {"chrome": {"epoch": 5.0, "streamers": frozenset({"alice"}), "plan_seq": 3}})
    refusals = _Refusals(monkeypatch, times=2)

    run()

    assert refusals.calls == 3
    assert refusals.sleeps == [0.05, 0.1]
    if writer == "streak_state":
        assert json.loads(path.read_text(encoding="utf-8"))["last_live"] == {"alice": live_at}
    else:
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "chrome": {"ts": 5.0, "streamers": ["alice"], "plan_seq": 3}}
    assert sorted(p.name for p in tmp_config_dir.iterdir()) == [path.name]


def test_c07_only_the_retry_helper_and_the_startup_scrub_call_os_replace():
    """Every atomic write goes through _replace_with_retry. The startup log
    scrub is the one exception (see REPLACE_RETRY_SECONDS)."""
    def os_replace_calls(root):
        return [node for node in ast.walk(root)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"]

    tree = ast.parse(Path(sm.__file__).read_text(encoding="utf-8"))
    by_function = {func.name: len(os_replace_calls(func)) for func in ast.walk(tree)
                   if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and os_replace_calls(func)}
    assert len(os_replace_calls(tree)) == 2
    assert by_function == {"_replace_with_retry": 1, "scrub_secrets_from_logs": 1}


def test_c07_the_startup_scrub_tries_a_refused_replace_once(monkeypatch, tmp_path):
    """The scrub runs at import: a refused replace is not retried, so a log
    another instance holds open does not hold up startup."""
    path = tmp_path / "stream_monitor.log"
    path.write_bytes(b'x "access_token": "abcdef123456"\n')
    refusals = _Refusals(monkeypatch, times=None)

    assert sm.scrub_secrets_from_logs(path) == []

    assert refusals.calls == 1
    assert refusals.sleeps == []
    assert b"abcdef123456" in path.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_c07_an_open_tabs_writer_that_gives_up_leaves_the_next_report_alone(
        monkeypatch, tmp_config_dir, caplog):
    """Two POST /open_tabs threads persist at once while a reader holds
    extension_tabs.json, the second one late in the first one's retries.
    The first gives up at its bound and removes its temp file, the reader
    lets go right after, and the second report (which covers both browsers)
    still lands inside its own bound."""
    caplog.set_level(logging.DEBUG, logger="StreamMonitor")
    monkeypatch.setattr(sm, "REPLACE_RETRY_SECONDS", 0.6)
    path = tmp_config_dir / "extension_tabs.json"
    path.write_text('{"stale": true}', encoding="utf-8")
    first_refusals = []
    late_in_first = threading.Event()
    reader_gone = threading.Event()
    real_replace, real_discard = sm.os.replace, sm._discard_temp_file

    def replace(src, dst):
        if not reader_gone.is_set():
            if threading.current_thread().name == "writer-a":
                first_refusals.append(src)
                if len(first_refusals) == 4:  # 0.35 s of its 0.6 s spent
                    late_in_first.set()
            raise PermissionError(errno.EACCES, "Access is denied", str(dst))
        real_replace(src, dst)

    def discard(temp):
        real_discard(temp)
        reader_gone.set()

    monkeypatch.setattr(sm.os, "replace", replace)
    monkeypatch.setattr(sm, "_discard_temp_file", discard)
    first = {"chrome": {"epoch": 5.0, "streamers": frozenset({"alice"})}}
    both = dict(first, firefox={"epoch": 6.0, "streamers": frozenset({"bob"}), "plan_seq": 4})
    a = threading.Thread(target=sm._persist_open_tabs_reports, args=(first,), name="writer-a")
    b = threading.Thread(target=sm._persist_open_tabs_reports, args=(both,), name="writer-b")
    a.start()
    assert late_in_first.wait(5)
    b.start()
    a.join(10)
    b.join(10)

    assert not a.is_alive() and not b.is_alive()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "chrome": {"ts": 5.0, "streamers": ["alice"]},
        "firefox": {"ts": 6.0, "streamers": ["bob"], "plan_seq": 4}}
    assert sorted(p.name for p in tmp_config_dir.iterdir()) == [path.name]
    assert [r.threadName for r in caplog.records
            if r.getMessage().startswith("Could not persist")] == ["writer-a"]


def test_c07_a_give_up_keeps_the_replace_error_when_the_temp_file_is_held_too(
        monkeypatch, tmp_path):
    """Removing src on a give-up is best effort: a scanner that holds the
    fresh temp file refuses the unlink as well. The caller still gets the
    replace error, and the temp file stays for the next write to overwrite."""
    _Refusals(monkeypatch, times=None)

    def refused_unlink(target):
        raise PermissionError(errno.EACCES, "Access is denied", str(target))

    monkeypatch.setattr(sm.os, "unlink", refused_unlink)
    src, dst = tmp_path / "x.json.tmp", tmp_path / "x.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError) as raised:
        sm._replace_with_retry(src, dst)
    assert raised.value.filename == str(dst)
    assert src.exists() and not dst.exists()


def test_am10_the_first_tick_runs_before_the_first_poll(mon, monkeypatch):
    _report()
    seen = {}

    def loop():
        seen["state"] = mon.slot_state
        seen["plan"] = _plan()

    _startable(mon, monkeypatch, loop=loop)
    assert mon.start() is True
    mon.thread.join(timeout=5)

    assert seen["state"] == "alive"
    assert seen["plan"] is not None and seen["plan"]["executor"] == EXECUTOR


# ---------------------------------------------------------------------------
# F30: /config polls alone do not keep an offer from falling back
# ---------------------------------------------------------------------------


def test_f30_config_polls_alone_do_not_hold_an_unacked_offer_past_180_seconds(mon, monkeypatch):
    import urllib.request

    mon.config.slot_mode = False
    mon.queued_vods = {"bob": {"url": sv.save_url("bob"), "ended_at": "2026-09-29T01:00:00.000Z"}}
    mon._offer_rescue_or_flush(set())
    assert mon.rescue_pending is not None
    server = sm._SingletonHTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        for _ in range(3):
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/config",
                                        timeout=5) as resp:
                assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()
    assert sm.extension_seen_within(sm.EXTENSION_ALIVE_WINDOW_SECONDS) is False

    mon._rescue_deadline_monotonic = 0.0  # past 180 s
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mweb:
        mon._maybe_fallback_rescue()
        assert mon.wait_for_pending_opens(timeout=5)

    assert mon.rescue_pending is None
    mweb.assert_called_once_with(sv.save_url("bob"))


# ---------------------------------------------------------------------------
# AUDIT S7: _vod_lock between the ack and the candidates
# ---------------------------------------------------------------------------


def test_as07_vod_lock_serializes_ack_and_candidates(mon, monkeypatch):
    mon.config.slot_mode = False
    mon.queued_vods = {
        name: {"url": sv.save_url(name), "ended_at": "2026-09-29T01:00:00.000Z"}
        for name in ("alice", "bob", "carol")
    }
    mon._offer_rescue_or_flush(set())
    offer_id = mon.rescue_pending["id"]
    order = []
    ack_started = threading.Event()
    real_rank = mon._list_rank

    def slow_rank(name):
        # Inside _build_rescue_candidates, with _vod_lock held: the ack on
        # the other thread must wait for the whole build.
        if not ack_started.is_set():
            ack_started.set()
            acker.start()
            time.sleep(0.2)
        order.append("rank")
        return real_rank(name)

    def ack():
        mon.acknowledge_rescue(offer_id)
        order.append("ack")

    acker = threading.Thread(target=ack)
    monkeypatch.setattr(mon, "_list_rank", slow_rank)
    cands = mon._build_rescue_candidates(set())
    acker.join(timeout=5)

    # The ack ran after the candidate build released the lock, so the build
    # never saw the queue change under it.
    assert order[-1] == "ack" and order.count("rank") >= 3
    assert {c["streamer"] for c in cands} == {"alice", "bob", "carol"}
    assert mon.queued_vods == {}


# ---------------------------------------------------------------------------
# Rule 33: activation absorbs, and withdraws the pending offer
# ---------------------------------------------------------------------------


def test_r33_activation_absorbs_and_withdraws_the_offer(mon, events):
    mon.config.slot_mode = False
    now = time.time()
    mon.queued_vods = {"zed": sv.item_to_queued_vod(_card_item("zed", now))}
    mon.missed_while_paused["bob"] = "10:00:00"
    mon._offer_rescue_or_flush(set())
    offer_id = mon.rescue_pending["id"]
    mon.config.slot_mode = True
    _report()

    mon._slot_tick()

    assert mon.slot_state == "alive"
    assert mon.rescue_pending is None
    assert sm.ConfigRequestHandler.config_data["rescue"] is None
    assert mon.queued_vods == {} and mon.missed_while_paused == {}
    assert sm.ConfigRequestHandler.config_data["queued_vods"] == {}
    assert _named(events, "rescue_withdrawn") == [{"offer_id": offer_id, "reason": "slot_mode"}]
    names = [event for event, _ in events]
    assert names.index("rescue_withdrawn") < names.index("streak_item_added") < names.index(
        "slot_mode_active")
    assert mon.slot.items["zed"]["origin"] == "absorbed"
    assert ("Stream Monitor", "Slot mode on: 2 Keep Open + 1 rotating, 30 min per turn") in mon._notify_calls


def test_r33_activation_absorbs_held_items_and_the_last_acked_offer_once(mon, events, tmp_config_dir):
    """Task 7: the activation also takes in the held items and the last
    acked offer's unfinished saves, then clears both, so neither is
    absorbed twice nor checked again the 1.11 way after Slot mode ends."""
    mon.config.slot_mode = False
    now = time.time()
    mon.queued_vods = {"cara": sv.item_to_queued_vod(_card_item("cara", now))}
    mon._offer_rescue_or_flush(set())
    acked_id = mon.rescue_pending["id"]
    assert mon.acknowledge_rescue(acked_id, claimant=EXECUTOR)
    assert mon._last_acked_offer is not None
    mon.held_save_items = {"alice": _card_item("alice", now)}
    time.sleep(0.01)  # offer ids are rescue-<ms>
    mon.queued_vods["zed"] = sv.item_to_queued_vod(_card_item("zed", now))
    mon.missed_while_paused["bob"] = "10:00:00"
    mon._offer_rescue_or_flush(set())
    pending_id = mon.rescue_pending["id"]
    assert pending_id != acked_id
    mon.config.slot_mode = True
    _report()

    mon._slot_tick()

    assert mon.slot_state == "alive"
    assert _named(events, "rescue_withdrawn") == [{"offer_id": pending_id, "reason": "slot_mode"}]
    assert mon.held_save_items == {}
    assert mon._last_acked_offer is None
    on_disk = json.loads((tmp_config_dir / "slot_state.json").read_text(encoding="utf-8"))
    assert on_disk["held"] == {}
    assert mon.slot.items["cara"]["origin"] == "absorbed"
    assert mon.slot.items["alice"]["origin"] == "absorbed"
    assert mon.slot.items["zed"]["origin"] == "absorbed"


# ---------------------------------------------------------------------------
# Tray text (A36), the default browser (rule 37)
# ---------------------------------------------------------------------------


def test_am36_tooltip_under_128_characters(mon):
    names = ["a" * 25, "b" * 25, "c" * 25]
    mon.config.streamers = names
    mon.streamers = {name: sm.StreamerState(name=name) for name in names}
    mon.config.pinned_streamers = [names[0]]
    _alive(mon, {name: START for name in names}, tabs=names)
    mon._slot_tick()

    tooltip = mon._status_calls[-1]
    assert tooltip.startswith("Slots: ")
    assert len(tooltip) <= ss.SLOT_TOOLTIP_MAX
    icon = SimpleNamespace(title=None)
    app = SimpleNamespace(status=None, icon=icon)
    sm.StreamMonitorApp.update_status(app, tooltip)
    assert icon.title == "Stream Monitor - " + tooltip
    assert len(icon.title) < 128
    for text in (ss.TOOLTIP_ABSENT, ss.TOOLTIP_NONE_EVER, ss.TOOLTIP_BUSY):
        sm.StreamMonitorApp.update_status(app, text)
        assert icon.title == "Stream Monitor - " + text


def test_r37_default_browser_family_mapping(monkeypatch):
    cases = {
        "FirefoxURL-308046B0AF4A39CB": "firefox",
        "ChromeHTML": "chrome",
        "MSEdgeHTM": "chrome",
        "BraveHTML": "chrome",
        "BraveBHTML": "chrome",
        "ChromiumHTM.ABC": "chrome",
        "OperaStable": "chrome",
        "OperaGXStable": "chrome",
        "VivaldiHTM.XYZ": "chrome",
        "IE.HTTPS": None,
        "SafariURL": None,
        "": None,
        None: None,
    }
    for prog_id, family in cases.items():
        assert sm.browser_family_for_progid(prog_id) == family, prog_id

    reads = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        OpenKey=lambda root, path: reads.append((root, path)) or Key(),
        QueryValueEx=lambda key, name: ("FirefoxURL-1234", 1),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    monkeypatch.setattr(sm, "_default_browser_family_cache", sm._UNSET)
    assert sm.default_browser_family() == "firefox"
    assert sm.default_browser_family() == "firefox"
    assert reads == [("HKCU", r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice")]

    def broken(root, path):
        raise OSError("no such key")

    monkeypatch.setattr(fake, "OpenKey", broken)
    monkeypatch.setattr(sm, "_default_browser_family_cache", sm._UNSET)
    assert sm.default_browser_family() is None


# ---------------------------------------------------------------------------
# AUDIT S8: the broke toast states the hours left; the unused alert is gone
# ---------------------------------------------------------------------------


def test_as08_the_broke_toast_states_hours_left(monkeypatch):
    now = sm._iso_to_epoch("2026-09-29T12:00:00.000Z")
    monkeypatch.setattr(sm, "_streak_clock", lambda: now)
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    toasts = []
    monkeypatch.setattr(sm, "_tray_notifier", lambda title, msg: toasts.append((title, msg)))
    base = {"status": "broke", "streamer": "alice", "count": 5,
            "detected_at": "2026-09-29T12:00:00.000Z", "source": "bell"}

    # "3 hours ago": posted at 08:00 at the earliest, so the 24 h window
    # closes 08:00 tomorrow at the earliest and 09:00 at the latest.
    sm.handle_streak_event(dict(base, card_age_s=10800, card_age_unit_s=3600))
    # "25 hours ago": the window may already be over.
    sm.handle_streak_event(dict(base, streamer="bob", card_age_s=90000, card_age_unit_s=3600))
    # An in-danger card: "Ends in ~Nh" from its deadline.
    sm.handle_streak_event(dict(base, streamer="carol", status="in_danger", deadline_hours=3,
                                card_age_s=1200, card_age_unit_s=60))

    assert toasts == [
        ("alice: 5-stream streak broke", "Watch a clip, VOD or stream within ~21h to save it."),
        ("bob: 5-stream streak broke", "Its save window may already be over."),
        ("carol: 5-stream streak in danger", "Ends in ~2h. Watch to keep the streak alive."),
    ]


def test_as08_missed_streak_alert_is_gone():
    assert not hasattr(sm.StreamMonitorApp, "_show_missed_streak_alert")
    import inspect
    assert "MessageBoxW" not in inspect.getsource(sm)


# ---------------------------------------------------------------------------
# Contract 3.12: activity timestamps stay in file order
# ---------------------------------------------------------------------------


def test_c12_the_activity_timestamp_reads_the_clock_once(monkeypatch):
    # Two readings that straddle a second boundary. Taking the seconds from
    # the first and the milliseconds from the second gives 12:00:00.002,
    # a time earlier than both.
    readings = [
        datetime(2026, 9, 29, 12, 0, 0, 999000, tzinfo=timezone.utc),
        datetime(2026, 9, 29, 12, 0, 1, 2000, tzinfo=timezone.utc),
    ]

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return readings.pop(0)

    monkeypatch.setattr(sm, "datetime", _Clock)

    assert sm._activity_timestamp() == "2026-09-29T12:00:00.999Z"
    assert len(readings) == 1
