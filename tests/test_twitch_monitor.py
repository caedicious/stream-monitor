"""Tests for TwitchMonitor API interactions. All HTTP is mocked."""
from unittest.mock import MagicMock, patch

import pytest
import requests

import stream_monitor_tray as sm


def _mock_response(json_data=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# OAuth token
# ---------------------------------------------------------------------------

def test_get_oauth_token_success(monitor):
    with patch.object(monitor._http, "post") as mock_post:
        mock_post.return_value = _mock_response({"access_token": "tok123"})
        assert monitor._get_oauth_token() is True
        assert monitor.oauth_token == "tok123"


def test_get_oauth_token_network_failure(monitor):
    with patch.object(monitor._http, "post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("no net")
        assert monitor._get_oauth_token() is False
        assert monitor.oauth_token is None


def test_get_oauth_token_missing_access_token(monitor):
    with patch.object(monitor._http, "post") as mock_post:
        mock_post.return_value = _mock_response({"error": "invalid_client"})
        assert monitor._get_oauth_token() is False


# ---------------------------------------------------------------------------
# _api_get: 401 triggers token refresh & retry
# ---------------------------------------------------------------------------

def test_api_get_401_refreshes_token_and_retries(monitor):
    monitor.oauth_token = "old_token"
    unauthorized = _mock_response({}, status=401)
    unauthorized.raise_for_status.side_effect = None  # don't raise on 401, we handle it
    success = _mock_response({"data": []})

    with patch.object(monitor._http, "get", side_effect=[unauthorized, success]) as mget, \
         patch.object(monitor, "_get_oauth_token", return_value=True) as mrefresh:
        result = monitor._api_get("http://x/api", {})
        assert result == {"data": []}
        assert mrefresh.call_count == 1
        assert mget.call_count == 2


def test_api_get_401_refresh_fails_returns_none(monitor):
    monitor.oauth_token = "old_token"
    unauthorized = _mock_response({}, status=401)
    unauthorized.raise_for_status.side_effect = None

    with patch.object(monitor._http, "get", return_value=unauthorized), \
         patch.object(monitor, "_get_oauth_token", return_value=False):
        assert monitor._api_get("http://x/api", {}) is None


# ---------------------------------------------------------------------------
# check_streams
# ---------------------------------------------------------------------------

def test_check_streams_empty_streamers_returns_empty(fresh_config):
    fresh_config.streamers = []
    mon = sm.TwitchMonitor(fresh_config)
    mon.streamers = {}
    assert mon.check_streams() == {}


def test_check_streams_identifies_live(monitor):
    # alice is live, bob is not
    with patch.object(monitor, "_api_get") as mapi:
        mapi.return_value = {
            "data": [{"user_login": "alice", "user_id": "111"}]
        }
        result = monitor.check_streams()
        assert result == {"alice": True, "bob": False}
        # User ID should be cached from the response
        assert monitor.user_ids.get("alice") == "111"
        assert monitor.live_streamers == ["alice"]


def test_check_streams_all_offline(monitor):
    with patch.object(monitor, "_api_get") as mapi:
        mapi.return_value = {"data": []}
        assert monitor.check_streams() == {"alice": False, "bob": False}
        assert monitor.live_streamers == []


def test_check_streams_request_exception_raises(monitor):
    with patch.object(monitor, "_api_get", side_effect=requests.ConnectionError("dns fail")):
        with pytest.raises(requests.RequestException):
            monitor.check_streams()
    # Status callback should have received the truncated error
    assert any("API error" in s for s in monitor._status_calls)
    # And the truncated message should fit the tooltip limit
    assert all(len(s) <= 127 for s in monitor._status_calls)


def test_check_streams_long_error_is_truncated(monitor):
    long_error = "x" * 1000
    with patch.object(monitor, "_api_get", side_effect=requests.ConnectionError(long_error)):
        with pytest.raises(requests.RequestException):
            monitor.check_streams()
    # The status callback message must never exceed 127 chars
    for s in monitor._status_calls:
        assert len(s) <= 127, f"status too long: {len(s)}"


# ---------------------------------------------------------------------------
# Auto-pause when user's own channel goes live
# ---------------------------------------------------------------------------

def test_auto_pause_engages_when_own_channel_live(monitor):
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True

    with patch.object(monitor, "_api_get") as mapi:
        mapi.return_value = {"data": [{"user_login": "me", "user_id": "1"}]}
        monitor.check_streams()
        assert monitor.auto_paused is True
    assert any("Auto-paused" in c[1] for c in monitor._notify_calls)


def test_auto_pause_lifts_when_own_channel_offline(monitor):
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True

    with patch.object(monitor, "_api_get") as mapi:
        mapi.return_value = {"data": []}
        monitor.check_streams()
        assert monitor.auto_paused is False
    assert any("offline" in c[1].lower() for c in monitor._notify_calls)


def test_auto_pause_ignored_when_im_live_pause_disabled(monitor):
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = False

    with patch.object(monitor, "_api_get") as mapi:
        mapi.return_value = {"data": [{"user_login": "me", "user_id": "1"}]}
        monitor.check_streams()
        assert monitor.auto_paused is False


def test_pause_lift_offers_still_live_missed_stream_as_live_candidate(monitor):
    """A streamer who went live during the pause and is STILL live when the
    user ends their own stream must appear in the rescue offer as a live
    candidate (after any ended ones). Nothing opens while the offer is
    pending; the fallback path covers the no-extension case."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.tab_open_spacing = 0
    # alice went live while we were paused: skipped, tracked as missed+live.
    monitor.missed_while_paused["alice"] = "12:00:00"
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = False

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        # own channel offline now (lifts pause); alice still live.
        mapi.return_value = {"data": [{"user_login": "alice", "user_id": "2"}]}
        monitor.check_streams()
        assert monitor.wait_for_pending_opens(timeout=5)

    assert monitor.auto_paused is False
    mopen.assert_not_called()
    offer = monitor.rescue_pending
    assert offer is not None
    assert offer["candidates"] == [{
        "streamer": "alice",
        "url": "https://twitch.tv/alice?sm=1",
        "kind": "live",
        "ended_at": None,
    }]
    # Fallback (extension never acks) opens alice's LIVE stream, not a VOD.
    monitor._rescue_deadline_monotonic = 0.0
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)
    mopen.assert_called_once_with("https://twitch.tv/alice?sm=1")
    assert monitor.streamers["alice"].browser_opened is True
    assert "alice" not in monitor.missed_while_paused


def test_pause_lift_offers_ended_stream_as_ended_candidate(monitor):
    """A streamer who went live AND offline during the pause rides the
    rescue offer as an ended candidate with the save-streak URL; the
    fallback opens only that URL, no phantom live-stream open."""
    monitor.config.own_channel = "me"
    monitor.config.im_live_pause = True
    monitor.auto_paused = True
    monitor.tab_open_spacing = 0
    # bob went live then offline during the pause: VOD got queued, and the
    # offline handler already removed bob from missed_while_paused.
    monitor.queued_vods["bob"] = {
        "url": "https://www.twitch.tv/save-streak/bob?sm=1",
        "ended_at": "2026-09-12T01:00:00.000Z",
    }

    with patch.object(monitor, "_api_get") as mapi, \
         patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        mapi.return_value = {"data": []}  # nobody live now; pause lifts
        monitor.check_streams()
        assert monitor.wait_for_pending_opens(timeout=5)

    assert monitor.auto_paused is False
    mopen.assert_not_called()
    offer = monitor.rescue_pending
    assert offer is not None
    assert [c["kind"] for c in offer["candidates"]] == ["ended"]
    # Fallback opens only the save-streak URL.
    monitor._rescue_deadline_monotonic = 0.0
    with patch("stream_monitor_tray.webbrowser.open", return_value=True) as mopen:
        monitor._maybe_fallback_rescue()
        assert monitor.wait_for_pending_opens(timeout=5)
    mopen.assert_called_once_with("https://www.twitch.tv/save-streak/bob?sm=1")


# ---------------------------------------------------------------------------
# Names Twitch would refuse stay out of the poll (v1.12.2)
# ---------------------------------------------------------------------------

def _poll_logins(mon):
    with patch.object(mon, "_api_get", return_value={"data": []}) as mapi, \
         patch("stream_monitor_tray.log_activity") as mlog:
        result = mon.check_streams()
    params = mapi.call_args.args[1]
    skipped = [(c.kwargs["what"], c.kwargs["value"]) for c in mlog.call_args_list if c.args[0] == "login_skipped"]
    return result, [v for k, v in params if k == "user_login"], skipped


def test_a_channel_link_in_your_channel_is_polled_as_the_login(fresh_config):
    """A user pasted https://www.twitch.tv/<name> into "Your Twitch Channel"
    and every poll failed with HTTP 400. The login is read out of it."""
    fresh_config.own_channel = "https://www.twitch.tv/Adayics"
    fresh_config.im_live_pause = True
    mon = sm.TwitchMonitor(fresh_config)
    mon.streamers = {name: sm.StreamerState(name=name) for name in fresh_config.streamers}
    result, logins, skipped = _poll_logins(mon)
    assert logins == ["alice", "bob", "adayics"]
    assert skipped == []
    assert result == {"alice": False, "bob": False}


def test_a_name_twitch_would_refuse_is_left_out_of_the_poll_and_logged_once(fresh_config):
    fresh_config.streamers = ["alice", "my friend"]
    fresh_config.own_channel = "my channel"
    fresh_config.im_live_pause = True
    mon = sm.TwitchMonitor(fresh_config)
    mon.streamers = {name: sm.StreamerState(name=name) for name in fresh_config.streamers}
    result, logins, skipped = _poll_logins(mon)
    assert logins == ["alice"]
    assert result == {"alice": False, "my friend": False}
    assert skipped == [("streamer", "my friend"), ("your channel", "my channel")]
    # The next poll says nothing more about them.
    _, logins2, skipped2 = _poll_logins(mon)
    assert logins2 == ["alice"] and skipped2 == []
