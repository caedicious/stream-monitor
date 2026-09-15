"""Tests for process_state_changes — the core live/offline state machine."""
from unittest.mock import patch

import stream_monitor_tray as sm


def test_going_live_opens_stream_when_not_paused(monitor):
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})

    mopen.assert_called_once_with("alice")
    assert monitor.streamers["alice"].was_live is True
    assert monitor.streamers["alice"].browser_opened is True
    assert any("alice" in n[1] for n in monitor._notify_calls)


def test_going_live_does_not_open_when_paused(monitor):
    monitor.paused = True
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})

    mopen.assert_not_called()
    assert monitor.streamers["alice"].was_live is True
    assert monitor.streamers["alice"].browser_opened is False
    # User still gets the "went LIVE" notification
    assert any("alice" in n[1] for n in monitor._notify_calls)
    # And the missed-while-paused set tracks it
    assert "alice" in monitor.missed_while_paused


def test_going_live_does_not_open_when_auto_paused(monitor):
    monitor.auto_paused = True
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_not_called()
    assert monitor.streamers["alice"].browser_opened is False


def test_already_live_does_not_reopen(monitor):
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_not_called()


def test_going_offline_resets_state(monitor):
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
    mopen.assert_not_called()
    assert monitor.streamers["alice"].was_live is False
    assert monitor.streamers["alice"].browser_opened is False


def test_vod_fallback_opens_when_missed_due_to_pause(monitor):
    # Stream went live while paused -> tab open was skipped and the
    # streamer entered missed_while_paused. Then they go offline -> VOD
    # fallback fires the save-streak deep link.
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
        assert monitor.wait_for_pending_opens(timeout=5)

    mopen.assert_called_once_with("https://www.twitch.tv/save-streak/alice?sm=1")
    # The streamer is removed from missed_while_paused so we don't
    # double-fire on subsequent live/offline cycles.
    assert "alice" not in monitor.missed_while_paused


def test_vod_fallback_does_NOT_fire_when_not_missed_due_to_pause(monitor):
    # Stream went live and offline but Stream Monitor never marked it
    # missed_while_paused (e.g. it happened during downtime or a network
    # blip — not a pause-induced skip). VOD fallback must not fire.
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    # NOT in missed_while_paused.

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()


def test_vod_fallback_disabled_by_default(monitor):
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"
    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
    mopen.assert_not_called()


def test_vod_fallback_skipped_when_browser_was_opened(monitor):
    # If we successfully opened the tab live, the streamer was never
    # added to missed_while_paused, so the fallback skips.
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
    mopen.assert_not_called()


def test_status_callback_reflects_live_count(monitor):
    with patch.object(monitor, "open_stream"):
        monitor.process_state_changes({"alice": True, "bob": True})
    # The final status callback (after iterating) should mention 2 live
    final = monitor._status_calls[-1]
    assert "2" in final and "live" in final.lower()


def test_status_callback_paused_label(monitor):
    monitor.paused = True
    monitor.process_state_changes({"alice": False, "bob": False})
    assert monitor._status_calls[-1] == "Paused"


def test_status_callback_auto_paused_label(monitor):
    monitor.auto_paused = True
    monitor.process_state_changes({"alice": False, "bob": False})
    assert "Auto-paused" in monitor._status_calls[-1]


def test_status_callback_monitoring_when_idle(monitor):
    monitor.process_state_changes({"alice": False, "bob": False})
    assert monitor._status_calls[-1] == "Monitoring..."


# ---------------------------------------------------------------------------
# Queued-VOD behavior (new in v1.6.5)
# ---------------------------------------------------------------------------


def test_vod_queued_when_auto_paused_instead_of_opened(monitor):
    """A missed stream that goes offline while auto-paused should QUEUE the
    save-streak URL, not open a browser tab on top of the user's live stream.
    As of v1.7.0 the queue entry carries the ended_at timestamp used for
    rescue-rotation priority ordering."""
    monitor.config.vod_fallback = True
    monitor.auto_paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()
    entry = monitor.queued_vods["alice"]
    assert entry["url"] == "https://www.twitch.tv/save-streak/alice?sm=1"
    assert entry["ended_at"]  # ISO timestamp recorded for priority sorting
    # User gets a notification about the queue
    assert any("queued" in n[1].lower() for n in monitor._notify_calls)
    # Streamer removed from missed_while_paused once queued
    assert "alice" not in monitor.missed_while_paused


def test_vod_queued_when_manually_paused(monitor):
    """Same behavior for the manual paused flag."""
    monitor.config.vod_fallback = True
    monitor.paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()
    assert monitor.queued_vods["alice"]["url"] == "https://www.twitch.tv/save-streak/alice?sm=1"


def test_missed_entry_cleared_on_offline_even_without_vod_fallback(monitor):
    """Regression: with vod_fallback OFF, a streamer that went live and
    offline during a pause used to stay in missed_while_paused forever,
    which could trigger a duplicate open on a much later pause lift."""
    monitor.config.vod_fallback = False
    monitor.auto_paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()
    assert monitor.queued_vods == {}
    assert "alice" not in monitor.missed_while_paused


def test_vod_opens_immediately_when_not_paused(monitor):
    """When the pause is OFF, the existing behavior must still apply —
    save-streak URL opens immediately, queue stays empty."""
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
        assert monitor.wait_for_pending_opens(timeout=5)

    mopen.assert_called_once_with("https://www.twitch.tv/save-streak/alice?sm=1")
    assert monitor.queued_vods == {}
    # Streamer removed from missed_while_paused once opened
    assert "alice" not in monitor.missed_while_paused


def test_pause_lift_publishes_rescue_offer_instead_of_flushing(monitor):
    """v1.7.0: when auto-pause lifts, the desktop publishes a rescue offer
    for the extension's 3-slot rotation instead of opening everything at
    once. Nothing opens until either the extension acks (extension opens)
    or the ack deadline passes (desktop falls back)."""
    import stream_monitor_tray as sm

    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.queued_vods = {
        "alice": {"url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "2026-09-12T02:00:00.000Z"},
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "2026-09-12T01:00:00.000Z"},
    }

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}  # own channel offline -> lifts pause
        monitor.check_streams()
        assert monitor.wait_for_pending_opens(timeout=5)

    assert monitor.auto_paused is False
    mopen.assert_not_called()  # nothing opens while the offer is pending
    offer = monitor.rescue_pending
    assert offer is not None
    assert sm.ConfigRequestHandler.config_data["rescue"] == offer
    # Ownership not transferred yet: queue intact until ack or fallback.
    assert set(monitor.queued_vods) == {"alice", "bob"}
    # Priority order: ended entries sorted earliest-ended first.
    streamers_in_order = [c["streamer"] for c in offer["candidates"]]
    assert streamers_in_order == ["bob", "alice"]
    assert all(c["kind"] == "ended" for c in offer["candidates"])


def test_rescue_ack_transfers_ownership(monitor):
    """A matching /rescue_ack clears the offer and the desktop-side queue
    state; live candidates get browser_opened so nothing reopens them."""
    import stream_monitor_tray as sm

    monitor.queued_vods = {
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "2026-09-12T01:00:00.000Z"},
    }
    monitor.missed_while_paused["alice"] = "12:00:00"
    monitor.streamers["alice"].browser_opened = False
    monitor._offer_rescue_or_flush({"alice"})
    offer = monitor.rescue_pending
    assert offer is not None

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        assert monitor.acknowledge_rescue(offer["id"]) is True

    mopen.assert_not_called()  # extension owns the opens now
    assert monitor.rescue_pending is None
    assert monitor.queued_vods == {}
    assert "alice" not in monitor.missed_while_paused
    assert monitor.streamers["alice"].browser_opened is True
    assert sm.ConfigRequestHandler.config_data["rescue"] is None
    # Replay of the same id is rejected.
    assert monitor.acknowledge_rescue(offer["id"]) is False


def test_rescue_ack_wrong_id_rejected(monitor):
    monitor.queued_vods = {
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "x"},
    }
    monitor._offer_rescue_or_flush(set())
    assert monitor.acknowledge_rescue("rescue-nope") is False
    assert monitor.rescue_pending is not None  # offer still live


def test_rescue_fallback_flushes_after_timeout(monitor):
    """No ack within the deadline: the desktop opens everything itself via
    the paced queue, exactly like pre-1.7."""
    monitor.queued_vods = {
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "x"},
    }
    monitor.missed_while_paused["alice"] = "12:00:00"
    monitor.live_streamers = ["alice"]
    monitor._offer_rescue_or_flush({"alice"})
    monitor._rescue_deadline_monotonic = 0.0  # force the deadline into the past

    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)

    assert monitor.rescue_pending is None
    opened = {c.args[0] for c in mopen.call_args_list}
    assert opened == {
        "https://twitch.tv/alice?sm=1",
        "https://www.twitch.tv/save-streak/bob?sm=1",
    }
    assert monitor.queued_vods == {}


def test_rescue_candidates_prefer_live_over_queued_vod(monitor):
    """A streamer who ended during the pause (VOD queued) but is live again
    when the pause lifts gets ONE candidate: the live stream. Watching live
    saves the streak; the save-streak link would only duplicate the tab."""
    monitor.queued_vods = {
        "alice": {"url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "2026-09-15T01:00:00.000Z"},
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "2026-09-15T02:00:00.000Z"},
    }
    monitor.missed_while_paused["alice"] = "12:00:00"
    cands = monitor._build_rescue_candidates({"alice"})
    assert {(c["streamer"], c["kind"]) for c in cands} == {
        ("bob", "ended"),
        ("alice", "live"),
    }


def test_rescue_ack_drops_vod_for_live_candidate(monitor):
    """Acking a live candidate also clears any queued VOD for the same
    streamer so a later flush can't open a duplicate save-streak tab."""
    monitor.missed_while_paused["alice"] = "12:00:00"
    monitor.streamers["alice"].browser_opened = False
    monitor._offer_rescue_or_flush({"alice"})
    # The VOD lands after the offer went out (alice ended during the ack
    # window); the live candidate still covers her streak.
    monitor.queued_vods["alice"] = {
        "url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "x",
    }
    offer = monitor.rescue_pending
    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        assert monitor.acknowledge_rescue(offer["id"]) is True
    mopen.assert_not_called()
    assert monitor.queued_vods == {}


def test_rescue_fallback_waits_while_extension_polls(monitor, monkeypatch):
    """Past the 180s soft deadline with the extension still polling /config,
    the desktop holds the offer (logging rescue_ack_overdue once) instead of
    flooding. The hard deadline flushes even if polls continue: this is the
    v1.7.2 incident where a live extension failed every ack."""
    import time as _time
    import stream_monitor_tray as sm

    monitor.queued_vods = {
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "x"},
    }
    monitor._offer_rescue_or_flush(set())
    monitor._rescue_deadline_monotonic = 0.0  # soft deadline passed
    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", _time.monotonic())

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor._maybe_fallback_rescue()
    mopen.assert_not_called()
    assert monitor.rescue_pending is not None  # offer still published
    assert monitor._rescue_overdue_logged is True

    # Hard deadline passes: flush happens despite live polling.
    monitor._rescue_offer_started_monotonic = (
        _time.monotonic() - sm.RESCUE_ACK_HARD_TIMEOUT_SECONDS - 1
    )
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)
    assert monitor.rescue_pending is None
    assert monitor.queued_vods == {}


def test_rescue_fallback_immediate_when_no_polls(monitor, monkeypatch):
    """With nothing polling /config (browser closed), the 180s fallback
    fires exactly as pre-1.7.3."""
    import stream_monitor_tray as sm

    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", None)
    monitor.queued_vods = {
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "x"},
    }
    monitor._offer_rescue_or_flush(set())
    monitor._rescue_deadline_monotonic = 0.0
    with patch("stream_monitor_tray.webbrowser.open", return_value=True):
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)
    assert monitor.rescue_pending is None
    assert monitor.queued_vods == {}


def test_rescue_fallback_skips_vod_when_live_opened(monitor):
    """Fallback path: a streamer opened live doesn't also get their queued
    save-streak tab (the venusdawnvt double-open seen on 1.7.2)."""
    monitor.queued_vods = {
        "alice": {"url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "x"},
    }
    monitor.missed_while_paused["alice"] = "12:00:00"
    monitor.live_streamers = ["alice"]
    monitor._offer_rescue_or_flush({"alice"})
    monitor._rescue_deadline_monotonic = 0.0
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)
    opened = {c.args[0] for c in mopen.call_args_list}
    assert opened == {"https://twitch.tv/alice?sm=1"}
    assert monitor.queued_vods == {}


def test_queue_does_not_flush_if_manual_pause_still_active(monitor):
    """If auto_pause lifts but the manual pause is still on, no rescue
    offer is published and the queue stays intact — only opening would
    break the manual pause's promise."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.paused = True  # manual pause also engaged
    monitor.queued_vods = {
        "alice": {"url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "x"},
    }

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}
        monitor.check_streams()

    assert monitor.auto_paused is False
    mopen.assert_not_called()
    assert monitor.rescue_pending is None
    assert set(monitor.queued_vods) == {"alice"}  # queue intact, unflushed


def test_flush_returns_zero_when_queue_empty(monitor):
    """_flush_queued_vods is a no-op when there's nothing to flush."""
    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        n = monitor._flush_queued_vods(reason="test")
        assert monitor.wait_for_pending_opens(timeout=5)
    assert n == 0
    mopen.assert_not_called()


# ---------------------------------------------------------------------------
# Paced tab-open queue (new in v1.6.11) — simultaneous go-lives, queue
# flushes, and startup must not slam the browser with parallel tabs.
# ---------------------------------------------------------------------------


def test_simultaneous_go_lives_open_through_queue_in_order(monitor):
    """Two streamers going live in the same poll pass both open, in order,
    via the paced queue (spacing zeroed by the fixture)."""
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor.process_state_changes({"alice": True, "bob": True})
        assert monitor.wait_for_pending_opens(timeout=5)

    assert mopen.call_count == 2
    opened_urls = [c.args[0] for c in mopen.call_args_list]
    assert opened_urls == [
        "https://twitch.tv/alice?sm=1",
        "https://twitch.tv/bob?sm=1",
    ]
    assert monitor.streamers["alice"].browser_opened is True
    assert monitor.streamers["bob"].browser_opened is True


def test_tab_opens_are_spaced_apart(monitor):
    """With a real (small) spacing, consecutive opens are separated by at
    least the configured interval."""
    import time as _time

    monitor.tab_open_spacing = 0.25
    open_times = []

    def record_open(url):
        open_times.append(_time.monotonic())
        return True

    with patch("stream_monitor_tray.webbrowser.open", side_effect=record_open):
        monitor.open_stream("alice")
        monitor.open_stream("bob")
        assert monitor.wait_for_pending_opens(timeout=5)

    assert len(open_times) == 2
    gap = open_times[1] - open_times[0]
    assert gap >= 0.2, f"opens only {gap:.3f}s apart, expected >= ~0.25s"


def test_queue_flush_routes_through_paced_queue(monitor):
    """A multi-VOD flush enqueues every entry; the worker opens them all."""
    monitor.queued_vods = {
        "alice": {"url": "https://www.twitch.tv/save-streak/alice?sm=1", "ended_at": "a"},
        "bob": {"url": "https://www.twitch.tv/save-streak/bob?sm=1", "ended_at": "b"},
        "carol": {"url": "https://www.twitch.tv/save-streak/carol?sm=1", "ended_at": "c"},
    }

    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        n = monitor._flush_queued_vods(reason="test")
        assert monitor.wait_for_pending_opens(timeout=5)

    assert n == 3
    assert mopen.call_count == 3
    assert monitor.queued_vods == {}


def test_worker_survives_open_failure(monitor):
    """A webbrowser.open exception on one entry must not kill the worker —
    subsequent entries still open."""
    calls = []

    def flaky_open(url):
        calls.append(url)
        if "alice" in url:
            raise RuntimeError("simulated browser failure")
        return True

    with patch("stream_monitor_tray.webbrowser.open", side_effect=flaky_open):
        monitor.open_stream("alice")
        monitor.open_stream("bob")
        assert monitor.wait_for_pending_opens(timeout=5)

    assert calls == [
        "https://twitch.tv/alice?sm=1",
        "https://twitch.tv/bob?sm=1",
    ]
