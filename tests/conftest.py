"""
Shared pytest fixtures for Stream Monitor tests.

Isolates the test environment so that:
  * APPDATA points to a temp dir (prevents writing to the real config)
  * Logging doesn't touch real files
  * The module-level _stable_ca_bundle() side effect is benign
"""
import gc
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


# No test may open anything in the real default browser. A test that
# patches webbrowser.open with monkeypatch gets the real function back at
# teardown, and the tray's paced tab-open worker thread can still fire after
# that (or in a test that never patched it), which opened real Twitch pages
# on the developer's desktop. So the whole session runs with these guards:
# a per-test monkeypatch now falls back to the guard, never to the real
# function. Blocked calls are listed in the terminal summary.
import webbrowser  # noqa: E402

BLOCKED_OPENS = []


def _blocked_open(url, *args, **kwargs):
    BLOCKED_OPENS.append((os.environ.get("PYTEST_CURRENT_TEST", "(outside a test)"), str(url)))
    return True


class _BlockedController:
    def open(self, url, *args, **kwargs):
        return _blocked_open(url)

    open_new = open
    open_new_tab = open


webbrowser.open = _blocked_open
webbrowser.open_new = _blocked_open
webbrowser.open_new_tab = _blocked_open
webbrowser.get = lambda using=None: _BlockedController()
if hasattr(os, "startfile"):
    os.startfile = lambda *args, **kwargs: BLOCKED_OPENS.append(
        (os.environ.get("PYTEST_CURRENT_TEST", "(outside a test)"), "os.startfile " + " ".join(map(str, args))))


def pytest_terminal_summary(terminalreporter):
    if not BLOCKED_OPENS:
        return
    terminalreporter.section("browser opens blocked by conftest")
    for where, url in BLOCKED_OPENS:
        terminalreporter.write_line(f"{where}: {url}")



@pytest.fixture(autouse=True, scope="module")
def _collect_on_the_main_thread():
    """After each test module, collect garbage on the main thread. Objects a
    module leaves in reference cycles (Tk widgets and variables above all)
    must not be freed by a collection that happens to run on a server or
    worker thread of a later test: Tcl aborts the process when an
    interpreter's objects are freed on a thread that did not create it."""
    yield
    gc.collect()

@pytest.fixture(autouse=True)
def _slot_mode_isolation(monkeypatch):
    """1.12.0 module state never leaks between tests: no rescue claim
    handler, monitor submitter or watch-start provider stays registered,
    the card dedup records and the gone queue start empty, no extension
    contact carries over (a stored /open_tabs report now marks one), and
    /config's dict is a shallow copy, so a slot_plan or any other key a
    test writes is undone afterwards. New tests register hooks only through
    monkeypatch."""
    import stream_monitor_tray as sm
    monkeypatch.setattr(sm, "_rescue_claim_handler", None)
    monkeypatch.setattr(sm, "_monitor_submitter", None)
    monkeypatch.setattr(sm, "_watch_start_provider", None)
    monkeypatch.setattr(sm, "_streak_event_seen_at", {})
    monkeypatch.setattr(sm, "_streak_item_keys", {})
    monkeypatch.setattr(sm, "_open_tabs_gone", [])
    monkeypatch.setattr(sm, "_open_tabs_gone_keys", {})
    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", None)
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data",
                        dict(sm.ConfigRequestHandler.config_data))


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

    # Tab opens go through a paced queue (10s apart in production). Zero
    # the spacing in tests so the worker drains instantly; tests call
    # mon.wait_for_pending_opens() before asserting on webbrowser.open.
    mon.tab_open_spacing = 0

    # Expose captured calls on the monitor for convenience
    mon._status_calls = status_calls
    mon._notify_calls = notify_calls
    return mon
