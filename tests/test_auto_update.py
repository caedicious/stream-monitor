"""Tests for the launch-time auto-update flow (v1.8.0).

The GUI dialog and the installer handoff run in separate processes and
are not unit-testable here; these tests cover every decision the tray
process makes around them: version comparison (including suffixed tags,
which crashed the pre-1.8 comparator), SHA256SUMS parsing, the update
check's return contract, and the prompt-outcome handling.
"""
from unittest.mock import MagicMock, patch

import stream_monitor_tray as sm


def _bound(method_name, **attrs):
    """A MagicMock app with the real StreamMonitorApp method bound on."""
    app = MagicMock()
    for key, value in attrs.items():
        setattr(app, key, value)
    setattr(app, method_name, getattr(sm.StreamMonitorApp, method_name).__get__(app))
    return app


# --- version comparison ----------------------------------------------------

def test_newer_version_detected():
    app = _bound("_is_newer_version")
    assert app._is_newer_version("1.8.0", "1.7.3") is True
    assert app._is_newer_version("1.7.3", "1.8.0") is False
    assert app._is_newer_version("1.7.3", "1.7.3") is False
    assert app._is_newer_version("2.0", "1.9.9") is True


def test_suffixed_tags_do_not_crash_comparison():
    """Pre-1.8 this raised ValueError on int('2-pre') and silently
    reported no update. Suffixes now count as their numeric part."""
    app = _bound("_is_newer_version")
    assert app._is_newer_version("1.7.2-pre", "1.6.12") is True
    assert app._is_newer_version("1.7.2-pre", "1.7.2") is False
    assert app._is_newer_version("", "1.7.3") is False
    assert app._is_newer_version("garbage", "1.7.3") is False


# --- SHA256SUMS parsing ------------------------------------------------------

SUMS = """# Stream Monitor v9.9.9 release artifacts
# Generated: 2026-09-15T00:00:00+00:00
# Verify a download with: Get-FileHash <file> -Algorithm SHA256
#
{h1}  StreamMonitorInstaller.exe
{h2}  stream_monitor_chrome_9.9.9.zip
"""


def test_installer_hash_extracted():
    h1 = "a" * 64
    text = SUMS.format(h1=h1, h2="b" * 64)
    assert sm._installer_hash_from_sums(text) == h1


def test_installer_hash_missing_or_malformed():
    assert sm._installer_hash_from_sums("") is None
    assert sm._installer_hash_from_sums("# only comments\n") is None
    # Wrong length hash is rejected rather than trusted
    text = SUMS.format(h1="abc123", h2="b" * 64)
    assert sm._installer_hash_from_sums(text) is None


# --- check_for_updates contract ---------------------------------------------

def test_check_for_updates_returns_release_data():
    app = _bound("check_for_updates")
    app._is_newer_version = sm.StreamMonitorApp._is_newer_version.__get__(app)
    release = {"tag_name": "v9.9.9", "assets": [{"name": "StreamMonitorInstaller.exe"}]}
    resp = MagicMock()
    resp.json.return_value = release
    with patch.object(sm.requests, "get", return_value=resp):
        available, version, data = app.check_for_updates()
    assert available is True
    assert version == "9.9.9"
    assert data == release


def test_check_for_updates_failure_is_quiet():
    app = _bound("check_for_updates")
    with patch.object(sm.requests, "get", side_effect=sm.requests.RequestException("down")):
        available, version, data = app.check_for_updates()
    assert available is False
    assert version == sm.VERSION
    assert data == {}


# --- prompt outcome handling --------------------------------------------------

def test_offer_update_yes_installs():
    app = _bound("_offer_update")
    app._prompt_update_dialog.return_value = "yes"
    release = {"assets": []}
    app._offer_update("9.9.9", release)
    app._download_and_install_update.assert_called_once_with("9.9.9", release)
    app.config.save.assert_not_called()


def test_offer_update_later_does_nothing_persistent():
    app = _bound("_offer_update")
    app._prompt_update_dialog.return_value = "later"
    app._offer_update("9.9.9", {})
    app._download_and_install_update.assert_not_called()
    app.config.save.assert_not_called()


def test_offer_update_skip_persists_muted_version():
    app = _bound("_offer_update")
    app._prompt_update_dialog.return_value = "later_skip"
    app._offer_update("9.9.9", {})
    app._download_and_install_update.assert_not_called()
    assert app.config.skip_update_version == "9.9.9"
    app.config.save.assert_called_once()


# --- launch check gating -------------------------------------------------------

def _launch_app(monkeypatch, frozen, skip_version):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    monkeypatch.setattr(sm.sys, "frozen", frozen, raising=False)
    app = _bound("_startup_update_check")
    app.check_for_updates.return_value = (True, "9.9.9", {"assets": []})
    app.config.skip_update_version = skip_version
    return app


def test_startup_check_prompts_when_frozen(monkeypatch):
    app = _launch_app(monkeypatch, frozen=True, skip_version="")
    app._startup_update_check()
    app._offer_update.assert_called_once_with("9.9.9", {"assets": []})


def test_startup_check_respects_muted_version(monkeypatch):
    app = _launch_app(monkeypatch, frozen=True, skip_version="9.9.9")
    app._startup_update_check()
    app._offer_update.assert_not_called()


def test_startup_check_never_prompts_in_dev_runs(monkeypatch):
    app = _launch_app(monkeypatch, frozen=False, skip_version="")
    app._startup_update_check()
    app._offer_update.assert_not_called()


# --- download retry helpers --------------------------------------------------

def test_get_text_retries_then_succeeds(monkeypatch):
    """A transient failure (e.g. a just-published asset still returning
    404) is retried and the eventual success is returned."""
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    ok = MagicMock()
    ok.text = "hash  StreamMonitorInstaller.exe"
    ok.raise_for_status = lambda: None
    calls = [sm.requests.RequestException("404"), sm.requests.RequestException("reset"), ok]
    with patch.object(sm.requests, "get", side_effect=calls):
        assert sm._http_get_text_with_retry("http://x", attempts=4) == ok.text


def test_get_text_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    with patch.object(sm.requests, "get", side_effect=sm.requests.RequestException("down")):
        try:
            sm._http_get_text_with_retry("http://x", attempts=3)
            assert False, "expected RequestException"
        except sm.requests.RequestException:
            pass


def test_download_file_retries_then_returns_hash(monkeypatch, tmp_path):
    """The installer download restarts cleanly on a transient failure and
    returns the sha256 of the bytes actually written."""
    import hashlib
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    payload = b"installer-bytes-" * 1000
    expected = hashlib.sha256(payload).hexdigest()

    def good_ctx():
        cm = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.iter_content = lambda n: iter([payload])
        cm.__enter__ = lambda self: resp
        cm.__exit__ = lambda self, *a: False
        return cm

    calls = [sm.requests.RequestException("timeout"), good_ctx()]
    dest = tmp_path / "installer.exe"
    with patch.object(sm.requests, "get", side_effect=calls):
        got = sm._download_file_with_retry("http://x", dest, attempts=3)
    assert got == expected
    assert dest.read_bytes() == payload
