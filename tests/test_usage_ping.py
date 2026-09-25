"""v1.9.0: anonymous install counter. The install id is minted once and
persisted, the ping carries exactly {install_id, version, os}, a failed send
reports False without raising, and the loop never runs in a non-frozen (dev)
process."""
from unittest.mock import MagicMock, patch

import stream_monitor_tray as sm


def test_send_usage_ping_payload_and_success():
    resp = MagicMock()
    resp.status_code = 204
    with patch.object(sm.requests, "post", return_value=resp) as post:
        assert sm._send_usage_ping("abc-id", "1.9.0") is True
    assert post.call_args.args[0] == sm.USAGE_PING_URL
    body = post.call_args.kwargs["json"]
    assert set(body) == {"install_id", "version", "os"}
    assert body["install_id"] == "abc-id" and body["version"] == "1.9.0"
    assert post.call_args.kwargs["timeout"] == 10


def test_send_usage_ping_never_raises():
    with patch.object(sm.requests, "post", side_effect=sm.requests.RequestException("down")):
        assert sm._send_usage_ping("abc-id", "1.9.0") is False
    resp = MagicMock()
    resp.status_code = 500
    with patch.object(sm.requests, "post", return_value=resp):
        assert sm._send_usage_ping("abc-id", "1.9.0") is False


def test_install_id_minted_once_and_persisted(tmp_config_dir):
    cfg = sm.Config(client_id="a", client_secret="b", streamers=["x"])
    app = MagicMock()
    app.config = cfg
    app._ensure_install_id = sm.StreamMonitorApp._ensure_install_id.__get__(app)
    app._ensure_install_id()
    first = cfg.install_id
    assert len(first) == 36
    assert sm.Config.load().install_id == first  # persisted to config.json
    app._ensure_install_id()
    assert cfg.install_id == first  # never re-minted


def test_ping_loop_is_inert_in_dev_runs(monkeypatch):
    monkeypatch.setattr(sm.sys, "frozen", False, raising=False)
    app = MagicMock()
    app._usage_ping_loop = sm.StreamMonitorApp._usage_ping_loop.__get__(app)
    with patch.object(sm, "_send_usage_ping") as send:
        app._usage_ping_loop()  # returns at once: no sleep, no send
    send.assert_not_called()
