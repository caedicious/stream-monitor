"""
Shared pytest fixtures for Stream Monitor tests.

Isolates the test environment so that:
  * APPDATA points to a temp dir (prevents writing to the real config)
  * Logging doesn't touch real files
  * The module-level _stable_ca_bundle() side effect is benign
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest


# Point APPDATA at a temp directory BEFORE stream_monitor_tray is imported,
# so its module-level CONFIG_DIR / LOG_FILE / setup_logging() all land in
# throwaway locations. Conftest.py runs before test modules so this is safe.
_test_appdata = tempfile.mkdtemp(prefix="stream_monitor_tests_")
os.environ["APPDATA"] = _test_appdata

# Make the project root importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """
    Redirect CONFIG_DIR / CONFIG_FILE / LOG_FILE to a pytest tmp_path.
    Use in any test that touches config save/load.
    """
    import stream_monitor_tray as sm
    config_dir = tmp_path / "StreamMonitor"
    config_dir.mkdir()
    monkeypatch.setattr(sm, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(sm, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setattr(sm, "LOG_FILE", config_dir / "stream_monitor.log")
    return config_dir


@pytest.fixture
def fresh_config():
    """A valid minimal Config object for tests that don't load from disk."""
    import stream_monitor_tray as sm
    return sm.Config(
        client_id="test_client",
        client_secret="test_secret",
        streamers=["alice", "bob"],
        check_interval=30,
    )


@pytest.fixture
def monitor(fresh_config):
    """A TwitchMonitor with captured callbacks and streamer state pre-populated."""
    import stream_monitor_tray as sm

    status_calls = []
    notify_calls = []

    mon = sm.TwitchMonitor(
        fresh_config,
        status_callback=lambda s: status_calls.append(s),
        notify_callback=lambda t, m: notify_calls.append((t, m)),
    )
    # Populate streamer state map (normally done in start())
    mon.streamers = {name: sm.StreamerState(name=name) for name in fresh_config.streamers}

    # Expose captured calls on the monitor for convenience
    mon._status_calls = status_calls
    mon._notify_calls = notify_calls
    return mon
