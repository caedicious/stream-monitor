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
    with patch("stream_monitor_tray.requests.post") as mock_post:
        mock_post.return_value = _mock_response({"access_token": "tok123"})
        assert monitor._get_oauth_token() is True
        assert monitor.oauth_token == "tok123"


def test_get_oauth_token_network_failure(monitor):
    with patch("stream_monitor_tray.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("no net")
        assert monitor._get_oauth_token() is False
        assert monitor.oauth_token is None


def test_get_oauth_token_missing_access_token(monitor):
    with patch("stream_monitor_tray.requests.post") as mock_post:
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

    with patch("stream_monitor_tray.requests.get", side_effect=[unauthorized, success]) as mget, \
         patch.object(monitor, "_get_oauth_token", return_value=True) as mrefresh:
        result = monitor._api_get("http://x/api", {})
        assert result == {"data": []}
        assert mrefresh.call_count == 1
        assert mget.call_count == 2


def test_api_get_401_refresh_fails_returns_none(monitor):
    monitor.oauth_token = "old_token"
    unauthorized = _mock_response({}, status=401)
    unauthorized.raise_for_status.side_effect = None

    with patch("stream_monitor_tray.requests.get", return_value=unauthorized), \
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
