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


def test_vod_fallback_opens_when_missed(monitor):
    # Stream was live but we never opened a tab (e.g. paused during go-live,
    # then unpause happened, but the original went-live event was missed by
    # the open logic). Then the streamer goes offline — VOD fallback kicks in.
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False

    with patch.object(monitor, "get_latest_vod_url", return_value="https://twitch.tv/videos/123") as mvod, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mvod.assert_called_once_with("alice")
    mopen.assert_called_once_with("https://twitch.tv/videos/123")


def test_vod_fallback_disabled_by_default(monitor):
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False
    with patch.object(monitor, "get_latest_vod_url") as mvod:
        monitor.process_state_changes({"alice": False, "bob": False})
    mvod.assert_not_called()


def test_vod_fallback_skipped_when_browser_was_opened(monitor):
    # If we successfully opened the tab live, we don't also open the VOD
    # when the stream ends.
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    with patch.object(monitor, "get_latest_vod_url") as mvod:
        monitor.process_state_changes({"alice": False, "bob": False})
    mvod.assert_not_called()


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
    VOD, not open a browser tab on top of the user's live stream."""
    monitor.config.vod_fallback = True
    monitor.auto_paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False

    with patch.object(monitor, "get_latest_vod_url", return_value="https://twitch.tv/videos/777") as mvod, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mvod.assert_called_once_with("alice")
    mopen.assert_not_called()
    assert monitor.queued_vods == {"alice": "https://twitch.tv/videos/777"}
    # User gets a notification about the queue
    assert any("queued" in n[1].lower() for n in monitor._notify_calls)


def test_vod_queued_when_manually_paused(monitor):
    """Same behavior for the manual paused flag."""
    monitor.config.vod_fallback = True
    monitor.paused = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False

    with patch.object(monitor, "get_latest_vod_url", return_value="https://twitch.tv/videos/888"), \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_not_called()
    assert "alice" in monitor.queued_vods


def test_vod_opens_immediately_when_not_paused(monitor):
    """When the pause is OFF, the existing behavior must still apply —
    VOD opens immediately, queue stays empty."""
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False

    with patch.object(monitor, "get_latest_vod_url", return_value="https://twitch.tv/videos/999"), \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})

    mopen.assert_called_once_with("https://twitch.tv/videos/999")
    assert monitor.queued_vods == {}


def test_queue_flushes_when_auto_pause_lifts(monitor):
    """When the user's own stream ends and auto_paused goes True->False,
    every queued VOD should open in a browser tab."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.queued_vods = {
        "alice": "https://twitch.tv/videos/1",
        "bob": "https://twitch.tv/videos/2",
    }

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}  # own channel offline -> lifts pause
        monitor.check_streams()

    assert monitor.auto_paused is False
    assert monitor.queued_vods == {}
    assert mopen.call_count == 2
    opened_urls = {c.args[0] for c in mopen.call_args_list}
    assert opened_urls == {"https://twitch.tv/videos/1", "https://twitch.tv/videos/2"}
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
    monitor.queued_vods = {"alice": "https://twitch.tv/videos/1"}

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open") as mopen:
        mapi.return_value = {"data": []}
        monitor.check_streams()

    assert monitor.auto_paused is False
    mopen.assert_not_called()
    assert monitor.queued_vods == {"alice": "https://twitch.tv/videos/1"}


def test_flush_returns_zero_when_queue_empty(monitor):
    """_flush_queued_vods is a no-op when there's nothing to flush."""
    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        n = monitor._flush_queued_vods(reason="test")
    assert n == 0
    mopen.assert_not_called()
