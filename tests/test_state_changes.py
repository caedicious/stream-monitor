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
