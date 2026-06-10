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
    save-streak URL, not open a browser tab on top of the user's live stream."""
    monitor.config.vod_fallback = True
    monitor.auto_paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    monitor.missed_while_paused["alice"] = "12:00:00"

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()
    assert monitor.queued_vods == {"alice": "https://www.twitch.tv/save-streak/alice?sm=1"}
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
    assert monitor.queued_vods == {"alice": "https://www.twitch.tv/save-streak/alice?sm=1"}


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


def test_queue_flushes_when_auto_pause_lifts(monitor):
    """When the user's own stream ends and auto_paused goes True->False,
    every queued VOD (now save-streak URLs) should open in a browser tab."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.queued_vods = {
        "alice": "https://www.twitch.tv/save-streak/alice?sm=1",
        "bob": "https://www.twitch.tv/save-streak/bob?sm=1",
    }

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}  # own channel offline -> lifts pause
        monitor.check_streams()
        assert monitor.wait_for_pending_opens(timeout=5)

    assert monitor.auto_paused is False
    assert monitor.queued_vods == {}
    assert mopen.call_count == 2
    opened_urls = {c.args[0] for c in mopen.call_args_list}
    assert opened_urls == {
        "https://www.twitch.tv/save-streak/alice?sm=1",
        "https://www.twitch.tv/save-streak/bob?sm=1",
    }
    # Tray notification about the count
    assert any("queued vod" in n[1].lower() for n in monitor._notify_calls)


def test_queue_does_not_flush_if_manual_pause_still_active(monitor):
    """If auto_pause lifts but the manual pause is still on, the queue
    should stay intact — only opening would break the manual pause's
    promise."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.paused = True  # manual pause also engaged
    monitor.queued_vods = {"alice": "https://www.twitch.tv/save-streak/alice?sm=1"}

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}
        monitor.check_streams()

    assert monitor.auto_paused is False
    mopen.assert_not_called()
    assert monitor.queued_vods == {"alice": "https://www.twitch.tv/save-streak/alice?sm=1"}


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
        "alice": "https://www.twitch.tv/save-streak/alice?sm=1",
        "bob": "https://www.twitch.tv/save-streak/bob?sm=1",
        "carol": "https://www.twitch.tv/save-streak/carol?sm=1",
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
