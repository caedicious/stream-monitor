"""Tests for StreamMonitorApp.update_status tooltip-length truncation.

The Windows system tray tooltip has a hard 128-character limit.
update_status() must truncate longer strings or pystray will raise
ValueError and crash the monitor loop.
"""
from unittest.mock import MagicMock

import stream_monitor_tray as sm


def _app_with_icon():
    """Return a minimal object exposing update_status bound to a fake icon."""
    app = MagicMock()
    app.icon = MagicMock()
    app.icon.title = ""
    # Bind the real method to our mock
    app.update_status = sm.StreamMonitorApp.update_status.__get__(app)
    return app


def test_short_status_passes_through():
    app = _app_with_icon()
    app.update_status("Monitoring...")
    assert app.icon.title == "Stream Monitor - Monitoring..."


def test_long_status_is_truncated_to_128_chars():
    app = _app_with_icon()
    huge = "x" * 500
    app.update_status(huge)
    # pystray's cap is 128 — we allow up to 127 + safety margin
    assert len(app.icon.title) <= 128
    # Truncation should be obvious (ends with ellipsis)
    assert app.icon.title.endswith("...")


def test_status_is_stored_even_if_icon_missing():
    """If icon hasn't been created yet (startup race), update_status should
    still update the stored string without raising."""
    app = MagicMock()
    app.icon = None
    app.status = ""
    app.update_status = sm.StreamMonitorApp.update_status.__get__(app)
    # Should not raise
    app.update_status("Starting...")
    assert app.status == "Starting..."


def test_exact_boundary_status_is_not_truncated():
    """A status that fits exactly within the limit should be unchanged."""
    app = _app_with_icon()
    # 127 - len("Stream Monitor - ") = 127 - 17 = 110
    boundary = "x" * 110
    app.update_status(boundary)
    # No truncation needed; title should not end with "..."
    assert not app.icon.title.endswith("...")
    assert len(app.icon.title) <= 128
