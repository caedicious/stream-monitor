"""v1.10.0: streams that already have a tab open in the browser are not
opened again on a fresh start.

The extension POSTs /open_tabs with the monitored streamers that have a
Stream Monitor tab open in that browser. The newest report per browser
(in memory, else the copy the previous process mirrored to disk) seeds a
fresh start: those streams are skipped on the first poll. A report that
arrives after the start confirms or reverses each skip; with no report at
all the skip times out and the stream opens after all.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

import pytest

import stream_monitor_tray as sm


@pytest.fixture(autouse=True)
def _isolated_reports(tmp_config_dir, monkeypatch):
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    yield


def _report_from_previous_process(browser, streamers, age=30):
    """A report the extension made `age` seconds ago (before this start)."""
    sm.record_extension_open_tabs(
        browser, streamers, "refresh",
        now_epoch=time.time() - age, now_mono=time.monotonic() - age,
    )


def _advance_clock(monkeypatch, seconds):
    """Make time.monotonic() read `seconds` later for the rest of the test."""
    real = time.monotonic
    monkeypatch.setattr(sm.time, "monotonic", lambda: real() + seconds)


def _fresh_start(monitor, monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(monitor, "_monitor_loop", lambda: None)
    monitor.thread = None
    assert monitor.start() is True


def test_record_report_validates_and_persists(tmp_config_dir):
    ok = sm.record_extension_open_tabs(
        "Firefox", ["Alice", " bob ", "bad name!", 7], "init", now_epoch=1000.0, now_mono=5.0,
    )
    assert ok is True
    snap = sm.extension_open_tabs_snapshot()
    assert snap["firefox"]["streamers"] == frozenset({"alice", "bob"})
    assert snap["firefox"]["epoch"] == 1000.0
    assert snap["firefox"]["mono"] == 5.0
    on_disk = json.loads((tmp_config_dir / "extension_tabs.json").read_text(encoding="utf-8"))
    assert on_disk == {"firefox": {"ts": 1000.0, "streamers": ["alice", "bob"]}}
    # Malformed payloads store nothing.
    assert sm.record_extension_open_tabs("", ["alice"]) is False
    assert sm.record_extension_open_tabs("chrome", "alice") is False
    assert sm.record_extension_open_tabs(None, []) is False
    assert set(sm.extension_open_tabs_snapshot()) == {"firefox"}


def test_persisted_reports_respect_max_age(tmp_config_dir):
    path = tmp_config_dir / "extension_tabs.json"
    path.write_text(json.dumps({
        "firefox": {"ts": 1000.0, "streamers": ["alice", "Bad Name"]},
        "chrome": {"ts": 100.0, "streamers": ["bob"]},  # too old
        "junk": "nope",
    }), encoding="utf-8")
    assert sm.load_persisted_open_tabs(600, now_epoch=1500.0) == {"firefox": frozenset({"alice"})}
    path.write_text("{not json", encoding="utf-8")
    assert sm.load_persisted_open_tabs(600, now_epoch=1500.0) == {}
    path.unlink()
    assert sm.load_persisted_open_tabs(600, now_epoch=1500.0) == {}


def test_fresh_start_skips_streams_already_open(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": True})
    mopen.assert_called_once_with("bob")
    alice = monitor.streamers["alice"]
    assert alice.was_live is True
    assert alice.browser_opened is True
    assert monitor._startup_skipped == {"alice"}
    # The user still hears that alice is live; only the tab open is skipped.
    assert any("alice" in n[1] for n in monitor._notify_calls)
    # The seed applies to the first poll only: a later live transition opens as usual.
    monitor._startup_skipped.clear()
    alice.was_live = False
    alice.browser_opened = False
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": True})
    mopen.assert_called_once_with("alice")


def test_skip_is_confirmed_by_a_report_after_the_start(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        sm.record_extension_open_tabs("firefox", ["alice"], "refresh")
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_not_called()
    assert monitor._startup_skipped == set()


def test_skip_is_reversed_when_the_tab_is_gone(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        mopen.assert_not_called()
        # Firefox reports again after the start, without alice: her tab was closed.
        sm.record_extension_open_tabs("firefox", [], "tabs-changed")
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_called_once_with("alice")
    assert monitor._startup_skipped == set()
    assert monitor.streamers["alice"].browser_opened is True


def test_reversed_skip_does_nothing_if_the_stream_ended(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        sm.record_extension_open_tabs("firefox", [], "tabs-changed")
        monitor.process_state_changes({"alice": False, "bob": False})
    mopen.assert_not_called()
    assert monitor._startup_skipped == set()
    assert monitor.streamers["alice"].was_live is False


def test_another_browsers_report_does_not_decide_a_skip(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        # Chrome reports (no alice) while Firefox has not yet: keep waiting.
        sm.record_extension_open_tabs("chrome", ["bob"], "refresh")
        monitor.process_state_changes({"alice": True, "bob": False})
        mopen.assert_not_called()
        assert monitor._startup_skipped == {"alice"}
        # A tab in any browser confirms the skip, whichever browser claimed it first.
        sm.record_extension_open_tabs("chrome", ["alice"], "tabs-changed")
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_not_called()
    assert monitor._startup_skipped == set()


def test_skip_times_out_when_no_report_arrives(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        monitor.process_state_changes({"alice": True, "bob": False})
        mopen.assert_not_called()
        # Browser closed, extension gone: past the timeout the stream opens after all.
        _advance_clock(monkeypatch, sm.STARTUP_FRESH_REPORT_TIMEOUT_SECONDS + 1)
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_called_once_with("alice")
    assert monitor._startup_skipped == set()


def test_timed_out_skip_while_paused_counts_as_missed(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"])
    _fresh_start(monitor, monkeypatch)
    monitor.paused = True
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
        # A tab is open, so nothing was missed while paused.
        assert "alice" not in monitor.missed_while_paused
        _advance_clock(monkeypatch, sm.STARTUP_FRESH_REPORT_TIMEOUT_SECONDS + 1)
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_not_called()
    assert "alice" in monitor.missed_while_paused
    assert monitor.streamers["alice"].browser_opened is False


def test_stale_seed_is_ignored(monitor, monkeypatch):
    _report_from_previous_process("firefox", ["alice"], age=sm.STARTUP_OPEN_TABS_MAX_AGE_SECONDS + 60)
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": False})
    mopen.assert_called_once_with("alice")
    assert monitor._startup_skipped == set()


def test_seed_comes_from_disk_when_this_process_has_no_report(monitor, monkeypatch, tmp_config_dir):
    # The previous process mirrored its last report; this process has seen none yet.
    (tmp_config_dir / "extension_tabs.json").write_text(
        json.dumps({"firefox": {"ts": time.time() - 45, "streamers": ["bob"]}}), encoding="utf-8",
    )
    _fresh_start(monitor, monkeypatch)
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": True})
    mopen.assert_called_once_with("alice")
    assert monitor._startup_skipped == {"bob"}


def test_settings_restart_does_not_reseed(monitor, monkeypatch):
    """A settings-change restart keeps its own state (v1.8.6); the seed is
    for fresh starts only, so nothing already decided is revisited."""
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(monitor, "_monitor_loop", lambda: None)
    monitor.thread = None
    monitor.streamers["alice"].was_live = True
    monitor.streamers["alice"].browser_opened = True
    _report_from_previous_process("firefox", ["bob"])
    monitor.restart()
    assert monitor._startup_seed == {}
    with patch.object(monitor, "open_stream") as mopen:
        monitor.process_state_changes({"alice": True, "bob": True})
    mopen.assert_called_once_with("bob")


def test_open_tabs_http_endpoint(tmp_config_dir):
    server = HTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def post(body, raw=False):
        data = body if raw else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/open_tabs", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert post({"browser": "firefox", "streamers": ["Alice"], "reason": "init"}) == 204
        assert sm.extension_open_tabs_snapshot()["firefox"]["streamers"] == frozenset({"alice"})
        assert post({"browser": "", "streamers": []}) == 400
        assert post(b"{nope", raw=True) == 400
        assert post([1, 2]) == 400
        assert post({"browser": "firefox", "streamers": []}) == 204
        assert sm.extension_open_tabs_snapshot()["firefox"]["streamers"] == frozenset()
    finally:
        server.shutdown()
        server.server_close()
