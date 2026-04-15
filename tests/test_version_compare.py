"""Tests for semantic version comparison used by the update checker."""
from unittest.mock import MagicMock

import stream_monitor_tray as sm


def _version_checker():
    """Return a StreamMonitorApp-like object with just _is_newer_version bound.

    We avoid constructing the full StreamMonitorApp because it pulls in
    pystray/tkinter which is noisy in CI. The method doesn't use self.
    """
    app = MagicMock()
    app._is_newer_version = sm.StreamMonitorApp._is_newer_version.__get__(app)
    return app


def test_newer_patch_is_newer():
    c = _version_checker()
    assert c._is_newer_version("1.4.2", "1.4.1") is True


def test_newer_minor_is_newer():
    c = _version_checker()
    assert c._is_newer_version("1.5.0", "1.4.9") is True


def test_newer_major_is_newer():
    c = _version_checker()
    assert c._is_newer_version("2.0.0", "1.99.99") is True


def test_same_version_is_not_newer():
    c = _version_checker()
    assert c._is_newer_version("1.4.1", "1.4.1") is False


def test_older_is_not_newer():
    c = _version_checker()
    assert c._is_newer_version("1.3.0", "1.4.0") is False


def test_unequal_length_versions_are_padded():
    c = _version_checker()
    # "1.4" should be treated as "1.4.0"
    assert c._is_newer_version("1.4", "1.4.0") is False
    assert c._is_newer_version("1.5", "1.4.9") is True
    assert c._is_newer_version("1.4.1", "1.4") is True


def test_malformed_version_returns_false():
    c = _version_checker()
    assert c._is_newer_version("1.4.x", "1.4.0") is False
    assert c._is_newer_version("not a version", "1.0.0") is False
