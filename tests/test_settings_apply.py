"""v1.8.6: settings changes apply reliably. restart() keeps the state of
streamers still on the list (no duplicate tabs after Save), a manual
start() still begins clean, and the config watcher only reads a save once
the file parses (a read that lands mid-write must not become an empty
config)."""
import json

import stream_monitor_tray as sm


def _startable(monitor, monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(monitor, "_monitor_loop", lambda: None)
    monitor.thread = None
    return monitor


def test_restart_keeps_state_of_streamers_still_listed(monitor, monkeypatch):
    _startable(monitor, monkeypatch)
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    monitor.config.streamers = ["alice", "carol"]  # bob removed, carol added
    monitor.restart()
    assert set(monitor.streamers) == {"alice", "carol"}
    assert monitor.streamers["alice"].was_live is True
    assert monitor.streamers["alice"].browser_opened is True
    assert monitor.streamers["carol"].was_live is False


def test_manual_start_still_begins_clean(monitor, monkeypatch):
    _startable(monitor, monkeypatch)
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    assert monitor.start() is True
    assert monitor.streamers["alice"].was_live is False
    assert monitor.streamers["alice"].browser_opened is False


def test_read_config_if_parseable(tmp_config_dir):
    cfg = tmp_config_dir / "config.json"
    # A save caught mid-write is not valid JSON yet: must not be applied.
    cfg.write_text('{"client_id": "a", "client_secret": "b", "streamers": ["x", "y"', encoding="utf-8")
    assert sm._read_config_if_parseable() is None
    cfg.write_text(json.dumps({"client_id": "a", "client_secret": "b",
                               "streamers": ["y", "x"], "check_interval": 60}), encoding="utf-8")
    loaded = sm._read_config_if_parseable()
    assert loaded is not None and loaded.streamers == ["y", "x"]
    cfg.unlink()
    assert sm._read_config_if_parseable() is None


def test_restart_keeps_old_token_when_refresh_fails(monitor, monkeypatch):
    """v1.9.1: a settings save restarts the monitor; a transient token
    refresh failure must not leave monitoring stopped. The old token is
    kept (the API layer re-authenticates on a 401 anyway)."""
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(monitor, "_monitor_loop", lambda: None)
    monitor.thread = None
    monitor.oauth_token = "old-token"
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: False)
    monitor.restart()
    assert monitor.running is True
    assert monitor.oauth_token == "old-token"


def test_manual_start_still_fails_without_a_token(monitor, monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monitor.thread = None
    monitor.oauth_token = None
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: False)
    assert monitor.start() is False
    assert monitor.running is False
