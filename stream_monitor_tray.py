#!/usr/bin/env python3
"""
Twitch Stream Monitor - System Tray Application
Runs in the background and opens streams when monitored streamers go live.
"""

import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

import certifi
import shutil
import requests
import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

# Fix for PyInstaller --onefile: the _MEI temp extraction folder can be cleaned
# up by Windows or a new exe instance while this process is still running. This
# breaks certifi's CA bundle path. Copy it to a stable location at startup.
def _stable_ca_bundle():
    if getattr(sys, 'frozen', False):
        stable_path = Path(os.environ.get("APPDATA", "")) / "StreamMonitor" / "cacert.pem"
        try:
            src = certifi.where()
            # Always refresh on startup in case certifi was updated
            stable_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, stable_path)
            os.environ["REQUESTS_CA_BUNDLE"] = str(stable_path)
        except Exception:
            pass  # Fall back to default certifi path

_stable_ca_bundle()

# Version
VERSION = "1.10.0"
GITHUB_REPO = "caedicious/stream-monitor"

# Anonymous install counter (v1.9.0): a random install id, the version, and
# the OS name, once a day, to the developer's API so active installs can be
# counted. Nothing else is sent and the server keeps no IP address; see
# PRIVACY.md. Off via usage_ping in config.json (Settings has the checkbox).
# Never sent from a non-frozen (dev) run, so local testing cannot inflate
# the count.
USAGE_PING_URL = "https://schedapi.caedvt.com/api/stream-monitor/ping"
USAGE_PING_INTERVAL_SECONDS = 24 * 60 * 60
USAGE_PING_RETRY_SECONDS = 60 * 60
CONFIG_SERVER_PORT = 52832  # Arbitrary high port for localhost config server

# Minimum spacing between consecutive browser-tab opens. Opening many tabs
# simultaneously (several streamers going live in one poll, the queued-VOD
# flush after un-pausing, or startup with multiple streamers already live)
# starves the browser: video players never start, and the extension's
# content scripts don't get a chance to apply low-quality / mute / keepalive
# before the next tab lands. All stream/VOD tab opens flow through a single
# paced queue with this many seconds between opens.
TAB_OPEN_SPACING_SECONDS = 10
# Fresh-start handling of tabs the extension reports as already open
# (v1.10.0). A report older than this is not trusted as a seed, and once
# seeded the monitor waits this long for a report made after the start
# before deciding the browser is gone and opening the skipped streams.
STARTUP_OPEN_TABS_MAX_AGE_SECONDS = 10 * 60
STARTUP_FRESH_REPORT_TIMEOUT_SECONDS = 90

# Configuration paths
APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()

CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "stream_monitor.log"
# Activity log: structured JSONL record of stream transitions and tab-open
# attempts. Intended for cross-checking against streamer broadcast histories
# to identify cases where Stream Monitor missed a live event.
STREAM_ACTIVITY_FILE = CONFIG_DIR / "stream_activity.jsonl"


def setup_logging():
    """Set up file logging with rotation."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("StreamMonitor")
    logger.setLevel(logging.DEBUG)

    # Rotate at 2 MB, keep 3 old files
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Activity log (separate from the debug log). Structured, append-only JSONL.
# ---------------------------------------------------------------------------

_activity_lock = threading.Lock()


def _activity_timestamp() -> str:
    """ISO 8601 UTC with millisecond precision, e.g. 2026-04-17T18:30:05.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def log_activity(event: str, **fields):
    """Append a structured event to the activity log (JSONL, append-only).

    Wrapped in a try/except: a failure here must never crash the monitor.
    """
    try:
        record = {"ts": _activity_timestamp(), "event": event, **fields}
        line = json.dumps(record, separators=(",", ":")) + "\n"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with _activity_lock:
            with open(STREAM_ACTIVITY_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        log.warning("Failed to write activity log: %s", e)


# ---------------------------------------------------------------------------
# Streak events: incoming from the browser extension via POST /streak_event
# ---------------------------------------------------------------------------

# Module-level notifier set by StreamMonitorApp at startup. Allows the HTTP
# handler (a separate class with no app reference) to raise tray
# notifications without needing dependency injection through every layer.
_tray_notifier: Optional[Callable[[str, str], None]] = None

# Streak-rescue handoff (v1.7.0). When auto-pause lifts, the desktop no
# longer opens every missed stream itself. It publishes a rescue offer in
# /config and waits for the extension to acknowledge via POST /rescue_ack;
# the extension then runs a 3-slot rotation (30 minutes per turn) with the
# tabs it controls. If no acknowledgment arrives within this window (old
# extension version, extension disabled, browser closed), the monitor loop
# falls back to the pre-1.7 behavior of opening everything at once through
# the paced queue.
RESCUE_ACK_TIMEOUT_SECONDS = 180

# The 180s window assumes the extension is absent (browser closed, old
# version). If the extension is demonstrably alive (it polls GET /config
# every minute) but the ack has not arrived (transient POST failure, an
# extension-side bug), flushing at 180s floods the browser for nothing.
# While polls keep arriving the offer stays published until the hard
# deadline below; the blind 180s fallback applies only when nothing has
# polled recently.
RESCUE_ACK_HARD_TIMEOUT_SECONDS = 600
EXTENSION_ALIVE_WINDOW_SECONDS = 150

_extension_last_seen_monotonic: Optional[float] = None
_extension_seen_lock = threading.Lock()


def note_extension_contact() -> None:
    """Record that something (the browser extension) just fetched /config."""
    global _extension_last_seen_monotonic
    with _extension_seen_lock:
        _extension_last_seen_monotonic = time.monotonic()


def extension_seen_within(seconds: float) -> bool:
    """True if /config was fetched within the last `seconds` seconds."""
    with _extension_seen_lock:
        last = _extension_last_seen_monotonic
    return last is not None and (time.monotonic() - last) <= seconds


_rescue_ack_handler: Optional[Callable[[str], bool]] = None


# ---------------------------------------------------------------------------
# Open-tab reports from the browser extension (v1.10.0)
#
# Each browser extension POSTs /open_tabs with the monitored streamers that
# have a Stream Monitor tab open in that browser (rescue tabs included),
# after every config refresh and whenever the set changes. The latest
# report per browser is kept in memory and mirrored to a small file next to
# config.json, so a fresh start (a relaunch, or Stop then Start from the
# tray) can see what was open a moment ago and not open it a second time.
# Names are lowercase Twitch logins; nothing else is stored.
# ---------------------------------------------------------------------------
OPEN_TABS_MAX_STREAMERS = 500
_OPEN_TABS_BROWSER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_OPEN_TABS_LOGIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_open_tabs_lock = threading.Lock()
# browser -> {"streamers": frozenset[str], "epoch": float, "mono": float}
_open_tabs_reports: dict[str, dict] = {}


def _extension_tabs_path() -> Path:
    return CONFIG_DIR / "extension_tabs.json"


def record_extension_open_tabs(browser, streamers, reason: str = "", *,
                               now_epoch: Optional[float] = None,
                               now_mono: Optional[float] = None) -> bool:
    """Store one report. Returns False (storing nothing) for a malformed
    payload; malformed streamer names inside a good payload are dropped."""
    if not isinstance(browser, str) or not isinstance(streamers, list):
        return False
    browser = browser.strip().lower()
    if not _OPEN_TABS_BROWSER_RE.match(browser):
        return False
    names = set()
    for raw in streamers[:OPEN_TABS_MAX_STREAMERS]:
        if isinstance(raw, str):
            name = raw.strip().lower()
            if _OPEN_TABS_LOGIN_RE.match(name):
                names.add(name)
    epoch = time.time() if now_epoch is None else now_epoch
    mono = time.monotonic() if now_mono is None else now_mono
    with _open_tabs_lock:
        previous = _open_tabs_reports.get(browser)
        _open_tabs_reports[browser] = {"streamers": frozenset(names), "epoch": epoch, "mono": mono}
        snapshot = {b: dict(rep) for b, rep in _open_tabs_reports.items()}
    if previous is None or previous["streamers"] != frozenset(names):
        log.info("Open-tabs report from %s (%s): %s", browser, reason or "update",
                 ", ".join(sorted(names)) or "none")
    _persist_open_tabs_reports(snapshot)
    return True


def _persist_open_tabs_reports(snapshot: dict[str, dict]) -> None:
    """Mirror the reports to disk (best effort, atomic replace) so the next
    process can seed its start from them."""
    path = _extension_tabs_path()
    data = {b: {"ts": rep["epoch"], "streamers": sorted(rep["streamers"])}
            for b, rep in snapshot.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.debug("Could not persist the open-tabs report: %s", e)


def extension_open_tabs_snapshot() -> dict[str, dict]:
    """Copy of the latest report per browser received by this process."""
    with _open_tabs_lock:
        return {b: dict(rep) for b, rep in _open_tabs_reports.items()}


def load_persisted_open_tabs(max_age_seconds: float,
                             now_epoch: Optional[float] = None) -> dict[str, frozenset]:
    """Reports a previous process mirrored to disk, for browsers whose
    report is at most max_age_seconds old. Malformed content is ignored."""
    path = _extension_tabs_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time() if now_epoch is None else now_epoch
    result: dict[str, frozenset] = {}
    for browser, rep in data.items():
        if not isinstance(browser, str) or not isinstance(rep, dict):
            continue
        ts = rep.get("ts")
        names = rep.get("streamers")
        if not isinstance(ts, (int, float)) or not isinstance(names, list):
            continue
        if now - ts > max_age_seconds:
            continue
        result[browser] = frozenset(
            n for n in names if isinstance(n, str) and _OPEN_TABS_LOGIN_RE.match(n)
        )
    return result


def set_rescue_ack_handler(fn: Callable[[str], bool]) -> None:
    """Register the callable invoked when the extension POSTs /rescue_ack."""
    global _rescue_ack_handler
    _rescue_ack_handler = fn

# Per-streak dedup keyed by (status, streamer, count) so a refresh of the
# notifications page doesn't re-notify the same broken streak twice. Reset
# on app restart, which is fine: the activity log persists across restarts
# so the user still has a record.
_streak_event_seen: set = set()
_streak_event_lock = threading.Lock()


def set_tray_notifier(fn: Callable[[str, str], None]) -> None:
    """Register the callable used to raise tray notifications."""
    global _tray_notifier
    _tray_notifier = fn


def _format_streak_message(event: dict) -> tuple:
    streamer = event.get("streamer", "unknown")
    count = event.get("count", 0)
    if event.get("status") == "broke":
        title = f"{streamer}: {count}-stream streak broke"
        msg = f"Watch a clip, VOD or stream within 24h to save it."
    else:
        hours = event.get("deadline_hours", "?")
        title = f"{streamer}: {count}-stream streak in danger"
        msg = f"Ends in ~{hours}h. Watch to keep the streak alive."
    return title, msg


def handle_streak_event(payload: dict) -> None:
    """Validate, dedup, log, and notify on an incoming streak event."""
    if not isinstance(payload, dict):
        raise ValueError("payload not a dict")
    status = payload.get("status")
    if status not in ("broke", "in_danger"):
        raise ValueError(f"unknown status: {status!r}")
    streamer = payload.get("streamer")
    if not isinstance(streamer, str) or not streamer:
        raise ValueError("missing streamer")
    count = payload.get("count")
    if not isinstance(count, int) or count < 0 or count > 100000:
        raise ValueError(f"bad count: {count!r}")
    deadline_hours = payload.get("deadline_hours")
    if deadline_hours is not None and (
        not isinstance(deadline_hours, int) or deadline_hours < 0 or deadline_hours > 24 * 365
    ):
        raise ValueError(f"bad deadline_hours: {deadline_hours!r}")

    key = (status, streamer.lower(), int(count))
    with _streak_event_lock:
        if key in _streak_event_seen:
            log.debug("Streak event %s already seen this session, skipping", key)
            return
        _streak_event_seen.add(key)

    event_name = "streak_broke" if status == "broke" else "streak_in_danger"
    log_activity(
        event_name,
        streamer=streamer.lower(),
        count=int(count),
        deadline_hours=int(deadline_hours) if deadline_hours is not None else None,
        detected_at=payload.get("detected_at"),
        page_url=payload.get("page_url"),
    )
    title, msg = _format_streak_message(payload)
    log.info("Streak event: %s - %s", title, msg)
    if _tray_notifier:
        try:
            _tray_notifier(title, msg)
        except Exception as e:
            log.warning("Tray notifier raised on streak event: %s", e)


# Cap on entries served by /activity.json. The activity file is append-only
# and never rotates, so without a bound every request would JSON-parse the
# entire history (months of events). The raw /activity.jsonl download still
# returns the complete file for anyone who needs full history.
MAX_ACTIVITY_EVENTS_SERVED = 10000


def read_activity_log(limit: Optional[int] = None) -> list:
    """Read the activity log into a list of dicts. Reverse-chronological.

    Tolerates partial last lines from concurrent writes. Only the newest
    MAX_ACTIVITY_EVENTS_SERVED lines are parsed. Older history stays on
    disk and is available via the raw .jsonl download.
    """
    if not STREAM_ACTIVITY_FILE.exists():
        return []
    cap = limit if limit is not None else MAX_ACTIVITY_EVENTS_SERVED
    try:
        with open(STREAM_ACTIVITY_FILE, "r", encoding="utf-8") as f:
            # deque(maxlen) keeps only the newest `cap` lines; we then
            # JSON-parse just that tail instead of the whole file.
            tail = deque(f, maxlen=cap)
    except OSError as e:
        log.warning("Failed to read activity log: %s", e)
        return []
    events = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Partial line from a concurrent write, skip it.
            continue
    events.reverse()
    return events


def _get_about_html_path() -> Path:
    """Return the path to about.html, works both frozen (exe) and as script."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "about.html"
    return Path(__file__).parent / "about.html"


def _get_logs_html_path() -> Path:
    """Return the path to logs.html, works both frozen (exe) and as script."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "logs.html"
    return Path(__file__).parent / "logs.html"


# Pattern for parsing a line of stream_monitor.log written by setup_logging().
# Format: "YYYY-MM-DD HH:MM:SS [LEVEL] message"
import re as _re
_DEBUG_LOG_LINE_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.+)$")


def parse_debug_log() -> list:
    """Read and parse stream_monitor.log into a list of structured entries.

    Multi-line entries (e.g. tracebacks) are folded into the prior entry's
    msg field. Returns reverse-chronological (newest first) for symmetry with
    read_activity_log().
    """
    if not LOG_FILE.exists():
        return []
    entries = []
    current = None
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = _DEBUG_LOG_LINE_RE.match(line)
                if m:
                    if current:
                        entries.append(current)
                    current = {"ts": m.group(1), "level": m.group(2), "msg": m.group(3)}
                elif current:
                    current["msg"] += "\n" + line
        if current:
            entries.append(current)
    except OSError as e:
        log.warning("Failed to read debug log: %s", e)
        return []
    entries.reverse()
    return entries


class ConfigRequestHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to serve config and about page."""

    config_data = {}

    def do_GET(self):
        if self.path == "/config":
            note_extension_contact()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "moz-extension://*")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(self.config_data).encode())
        elif self.path.startswith("/about"):
            about_file = _get_about_html_path()
            if about_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(about_file.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"about.html not found")
        elif self.path == "/logs" or self.path == "/activity":
            # Serve the unified log viewer HTML page. /activity kept as an
            # alias so existing bookmarks still work.
            logs_file = _get_logs_html_path()
            if logs_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(logs_file.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"logs.html not found")
        elif self.path == "/activity.json":
            # Parsed activity events as JSON, reverse-chronological
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(read_activity_log()).encode("utf-8"))
        elif self.path == "/activity.jsonl":
            # Raw JSONL file for download / spreadsheet import
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header(
                "Content-Disposition", "attachment; filename=stream_activity.jsonl"
            )
            self.end_headers()
            if STREAM_ACTIVITY_FILE.exists():
                self.wfile.write(STREAM_ACTIVITY_FILE.read_bytes())
        elif self.path == "/debug.log":
            # Raw debug log file for download
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header(
                "Content-Disposition", "attachment; filename=stream_monitor.log"
            )
            self.end_headers()
            if LOG_FILE.exists():
                self.wfile.write(LOG_FILE.read_bytes())
        elif self.path == "/debug.log.json":
            # Parsed debug log entries as JSON, reverse-chronological
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(parse_debug_log()).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        """Handle inbound events from the browser extension.

        /streak_event: the extension detected a Twitch "your N-stream
        streak on X broke / ends in Yh" notification card in the page DOM
        (Twitch has no public API for viewing streaks, so the extension
        scrapes them from the bell dropdown / notifications page).
        /open_tabs: the monitored streamers with a Stream Monitor tab open
        in that browser (see record_extension_open_tabs).
        /rescue_ack: the extension claims the published rescue offer.
        """
        if self.path == "/streak_event":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 8192:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"missing or oversize body")
                return
            try:
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"invalid JSON: {e}".encode("utf-8"))
                return
            try:
                handle_streak_event(payload)
            except Exception as e:
                log.warning("Streak event handler raised: %s", e)
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        if self.path == "/open_tabs":
            # 204 = stored, 400 = malformed. A body of 16 KB covers any
            # realistic tab count many times over.
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 16384:
                self.send_response(400)
                self.end_headers()
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            ok = isinstance(payload, dict) and record_extension_open_tabs(
                payload.get("browser"), payload.get("streamers"),
                str(payload.get("reason", ""))[:32],
            )
            self.send_response(204 if ok else 400)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        if self.path == "/rescue_ack":
            # Extension claims ownership of the currently-published rescue
            # offer. 204 = handed over, the extension runs the rotation.
            # 409 = no such offer (already acked, already fell back, or a
            # stale id), the extension must NOT start a session.
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 1024:
                self.send_response(400)
                self.end_headers()
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                offer_id = str(payload.get("id", ""))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            handler = _rescue_ack_handler
            accepted = bool(handler and offer_id and handler(offer_id))
            self.send_response(204 if accepted else 409)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress logging."""
        pass


class _SingletonHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with SO_REUSEADDR disabled.

    Python's stock HTTPServer sets allow_reuse_address = 1, which on Windows
    has SO_REUSEADDR semantics that *permit two processes to bind the same
    port simultaneously*. We rely on the bind failing when another instance
    holds the port to enforce single-instance startup, so we have to
    explicitly opt out of address reuse here.

    ThreadingHTTPServer (one daemon thread per request) keeps a slow
    request (a large /activity.json read, a wedged client) from blocking
    every other endpoint behind it. The handlers are safe for this: the
    activity log writes go through _activity_lock, streak events through
    _streak_event_lock, and config_data reads are GIL-atomic dict lookups.
    """
    allow_reuse_address = False
    daemon_threads = True


def create_config_server(config: "Config") -> Optional[HTTPServer]:
    """Bind the config server port and prepare the handler. Returns the
    server if the bind succeeded, or None if the port is already in use
    (which means another Stream Monitor instance is already running and
    this process should exit).
    """
    ConfigRequestHandler.config_data = {
        "streamers": config.streamers,
        "pinned_streamers": config.pinned_streamers,
        "version": VERSION,
        "live_streamers": [],
        "paused": config.paused,
        "auto_paused": False,
        "rescue": None,
    }
    try:
        return _SingletonHTTPServer(("127.0.0.1", CONFIG_SERVER_PORT), ConfigRequestHandler)
    except OSError as e:
        log.error(
            "Config server bind failed on port %d (likely another instance running): %s",
            CONFIG_SERVER_PORT, e,
        )
        return None


def run_config_server(server: HTTPServer):
    """Drive the already-bound HTTPServer until it stops."""
    try:
        server.serve_forever()
    except Exception as e:
        log.error("Config server stopped unexpectedly: %s", e)


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""
    streamers: list = None
    # Subset of `streamers` marked as "Keep Open" by the user. Tabs for these
    # streamers are protected from being closed when max_tabs is hit; they
    # only close on user action, raid, or navigate-away.
    pinned_streamers: list = None
    check_interval: int = 60
    last_run_version: str = ""
    paused: bool = False
    own_channel: str = ""
    im_live_pause: bool = False
    vod_fallback: bool = False
    # Release tag the user asked not to be reminded about again (the
    # "do not remind me" checkbox on the update prompt). Cleared
    # implicitly when a newer version than the skipped one appears,
    # because the comparison is against this exact string.
    skip_update_version: str = ""
    # Anonymous install counter (v1.9.0): install_id is a random UUID minted
    # on first run and sent once a day with the version. usage_ping False
    # turns the ping off entirely.
    install_id: str = ""
    usage_ping: bool = True

    def __post_init__(self):
        if self.streamers is None:
            self.streamers = []
        if self.pinned_streamers is None:
            self.pinned_streamers = []
    
    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    raw = f.read()
                data = json.loads(raw)
                # Filter to only known fields so unknown keys don't cause TypeError
                valid_fields = set(cls.__dataclass_fields__.keys())
                filtered = {k: v for k, v in data.items() if k in valid_fields}
                return cls(**filtered)
            except Exception as e:
                log.error("Config load error: %s (path: %s)", e, CONFIG_FILE)
        else:
            log.info("Config file not found: %s", CONFIG_FILE)
        return cls()
    
    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)
    
    def is_valid(self) -> bool:
        return bool(self.client_id and self.client_secret and self.streamers)


@dataclass
class StreamerState:
    name: str
    was_live: bool = False
    browser_opened: bool = False


class TwitchMonitor:
    TWITCH_API_URL = "https://api.twitch.tv/helix/streams"
    VIDEOS_API_URL = "https://api.twitch.tv/helix/videos"
    USERS_API_URL = "https://api.twitch.tv/helix/users"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"

    def __init__(self, config: Config, status_callback: Callable[[str], None] = None,
                 notify_callback: Callable[[str, str], None] = None):
        self.config = config
        # Reused HTTP session: keeps the TLS connection to Twitch's API
        # alive across polls instead of paying a fresh TCP + TLS handshake
        # every check_interval. requests.Session also pools connections,
        # so the 401-refresh retry reuses the same socket.
        self._http = requests.Session()
        self.oauth_token: Optional[str] = None
        self.streamers: dict[str, StreamerState] = {}
        # Fresh-start bookkeeping for streams the extension reported as
        # already open in the browser (see _seed_startup_open_tabs).
        self._startup_seed: dict[str, set] = {}
        self._startup_claims: dict[str, set] = {}
        self._startup_skipped: set = set()
        self._startup_mono: float = float("-inf")
        self.running = False
        self.paused = False
        self.auto_paused = False  # True when user's own channel is live
        self.thread: Optional[threading.Thread] = None
        self.status_callback = status_callback or (lambda x: None)
        self.notify_callback = notify_callback or (lambda t, m: None)
        self.live_streamers: list[str] = []  # Current live streamers for config server
        self.missed_while_paused: dict[str, str] = {}  # { streamer: went_live_time }
        # VODs whose live-then-offline transition was detected while we were
        # paused (manually OR auto-paused for "I'm live"). Held here until
        # the pause lifts, at which point each queued VOD opens in a new
        # browser tab so the user can catch up without it interrupting
        # their own stream. Schema: { streamer: vod_url }.
        self.queued_vods: dict[str, str] = {}
        self.user_ids: dict[str, str] = {}  # { username: user_id } cache
        self.consecutive_errors: int = 0  # Track consecutive API failures
        # Streak-rescue handoff state (v1.7.0). rescue_pending holds the
        # offer published in /config while we wait for the extension's
        # /rescue_ack. The lock guards the offer against the HTTP thread
        # (ack) and the monitor thread (fallback timeout) racing.
        self.rescue_pending: Optional[dict] = None
        self._rescue_deadline_monotonic: float = 0.0
        self._rescue_offer_started_monotonic: float = 0.0
        self._rescue_overdue_logged: bool = False
        self._rescue_lock = threading.Lock()
        # Per-streamer metadata captured on the latest "live" check, used to
        # enrich the activity log (title, game, viewer count at the moment
        # the offline->live transition was detected).
        self.live_stream_meta: dict[str, dict] = {}

        # Paced tab-open queue. Every stream/VOD tab open goes through this
        # queue so consecutive opens are spaced tab_open_spacing seconds
        # apart, giving the browser time to start each video player and the
        # extension's content scripts time to apply mute / low-quality /
        # keepalive before the next tab arrives. The worker is a daemon
        # thread: it dies with the process, and any queued-but-unopened
        # entries are simply lost on exit (acceptable: the next poll
        # cycle re-detects still-live streamers).
        self.tab_open_spacing: float = TAB_OPEN_SPACING_SECONDS
        self._open_queue: queue.Queue = queue.Queue()
        self._last_open_monotonic: float = float("-inf")
        self._open_worker_thread = threading.Thread(
            target=self._tab_open_worker, daemon=True, name="tab-open-worker"
        )
        self._open_worker_thread.start()

    def _enqueue_tab_open(self, kind: str, streamer: str, url: str, **extra) -> int:
        """Queue a browser-tab open. Returns the queue position (0 = will
        open immediately, subject only to spacing from the previous open).

        The actual webbrowser.open and its tab_open_attempt activity-log
        entry happen on the worker thread when this entry's turn comes.
        """
        position = self._open_queue.qsize()
        if position > 0:
            # Only log the queued event when the open will actually wait
            # behind others, so the common single-open case stays one log line.
            log_activity(
                "tab_open_queued",
                kind=kind,
                streamer=streamer,
                url=url,
                queue_position=position,
                **extra,
            )
            log.info(
                "Tab open for %s queued at position %d (paced %ss apart)",
                streamer, position, self.tab_open_spacing,
            )
        self._open_queue.put({"kind": kind, "streamer": streamer, "url": url, "extra": extra})
        return position

    def _tab_open_worker(self):
        """Daemon worker: drains the open queue one entry at a time with
        tab_open_spacing seconds between consecutive opens. The first open
        after an idle period fires immediately."""
        while True:
            item = self._open_queue.get()
            try:
                wait = self.tab_open_spacing - (time.monotonic() - self._last_open_monotonic)
                if wait > 0:
                    log.info(
                        "Pacing: waiting %.1fs before opening %s",
                        wait, item["streamer"],
                    )
                    time.sleep(wait)
                url = item["url"]
                log.info("Opening tab (%s) for %s: %s", item["kind"], item["streamer"], url)
                try:
                    success = bool(webbrowser.open(url))
                except Exception as e:
                    log.error("webbrowser.open raised for %s: %s", url, e)
                    success = False
                self._last_open_monotonic = time.monotonic()
                log_activity(
                    "tab_open_attempt",
                    kind=item["kind"],
                    streamer=item["streamer"],
                    url=url,
                    success=success,
                    **item["extra"],
                )
            except Exception as e:
                # The worker must never die: a dead worker would silently
                # strand every future tab open.
                log.error("Tab-open worker iteration failed: %s", e)
            finally:
                self._open_queue.task_done()

    def wait_for_pending_opens(self, timeout: float = 30.0) -> bool:
        """Block until every queued tab open has been processed, or the
        timeout elapses. Returns True if the queue fully drained. Used by
        tests; also handy for debugging."""
        deadline = time.monotonic() + timeout
        with self._open_queue.all_tasks_done:
            while self._open_queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._open_queue.all_tasks_done.wait(remaining)
        return True
        
    def _get_oauth_token(self) -> bool:
        try:
            log.info("Requesting OAuth token")
            response = self._http.post(
                self.TOKEN_URL,
                params={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials"
                },
                timeout=10
            )
            response.raise_for_status()
            self.oauth_token = response.json().get("access_token")
            if self.oauth_token:
                log.info("OAuth token obtained successfully")
            else:
                log.error("OAuth response OK but no access_token in body")
            return bool(self.oauth_token)
        except requests.RequestException as e:
            log.error("OAuth token request failed: %s", e)
            self.status_callback(f"Auth error: {e}")
            return False
    
    def _get_headers(self) -> dict:
        return {
            "Client-ID": self.config.client_id,
            "Authorization": f"Bearer {self.oauth_token}"
        }
    
    def _api_get(self, url, params):
        """Make a GET request with automatic token refresh on 401."""
        response = self._http.get(url, headers=self._get_headers(), params=params, timeout=10)
        if response.status_code == 401:
            log.warning("API returned 401, token expired. Re-authenticating...")
            self.status_callback("Token expired, re-authenticating...")
            if self._get_oauth_token():
                response = self._http.get(url, headers=self._get_headers(), params=params, timeout=10)
            else:
                log.error("Re-authentication failed after 401")
                self.status_callback("Re-authentication failed")
                return None
        response.raise_for_status()
        return response.json()

    def check_streams(self) -> dict[str, bool]:
        if not self.streamers:
            return {}

        params = [("user_login", name) for name in self.streamers.keys()]

        # Also check user's own channel if "I'm live" pause is enabled
        own_channel = self.config.own_channel.lower().strip() if self.config.own_channel else ""
        if own_channel and self.config.im_live_pause:
            params.append(("user_login", own_channel))

        try:
            log.debug("Checking streams for: %s", list(self.streamers.keys()))
            data = self._api_get(self.TWITCH_API_URL, params)
            if data is None:
                log.warning("API returned None, treating all streamers as offline")
                return {name: False for name in self.streamers}

            live_set = set()
            new_meta: dict[str, dict] = {}
            for stream in data.get("data", []):
                login = stream["user_login"].lower()
                live_set.add(login)
                # Cache user IDs from stream responses
                if "user_id" in stream:
                    self.user_ids[login] = stream["user_id"]
                # Capture per-stream metadata for the activity log
                new_meta[login] = {
                    "title": stream.get("title", ""),
                    "game": stream.get("game_name", ""),
                    "viewers": stream.get("viewer_count", 0),
                }
            self.live_stream_meta = new_meta

            if live_set:
                log.info("Live streamers: %s", live_set)
            else:
                log.debug("No monitored streamers are live")

            # Update auto-pause based on user's own channel
            if own_channel and self.config.im_live_pause:
                was_auto_paused = self.auto_paused
                self.auto_paused = own_channel in live_set
                if self.auto_paused and not was_auto_paused:
                    log.info("Auto-paused: own channel '%s' is live", own_channel)
                    self.status_callback("Auto-paused (you're live)")
                    self.notify_callback("Stream Monitor", "Auto-paused because you're live!")
                    log_activity("auto_paused_started", own_channel=own_channel)
                elif not self.auto_paused and was_auto_paused:
                    log.info("Auto-pause lifted: own channel '%s' went offline", own_channel)
                    self.status_callback("Resumed (you went offline)")
                    self.notify_callback("Stream Monitor", "You went offline, resuming monitoring!")
                    log_activity("auto_paused_ended", own_channel=own_channel)
                    # Only resume opening if no other pause keeps us paused
                    # (manual `paused` toggle still suppresses).
                    if not self.paused:
                        # v1.7.0: instead of opening everything at once,
                        # publish a rescue offer for the extension's 3-slot
                        # rotation. Falls back to the open-everything path
                        # if the extension does not acknowledge in time.
                        self._offer_rescue_or_flush(live_set)

            # Update live streamers list for config server
            self.live_streamers = [name for name in self.streamers if name in live_set]

            return {name: name in live_set for name in self.streamers}

        except requests.RequestException as e:
            log.error("API request failed: %s", e)
            # Truncate error message for tray tooltip (128 char Windows limit)
            err_short = str(e)[:80]
            self.status_callback(f"API error: {err_short}")
            raise  # Let _monitor_loop handle error counting and notifications

    def open_stream(self, username: str):
        url = f"https://twitch.tv/{username}?sm=1"
        position = self._enqueue_tab_open("stream", username, url)
        if position > 0:
            self.status_callback(
                f"{username} queued to open (~{int(position * self.tab_open_spacing)}s)"
            )
    
    @property
    def effectively_paused(self) -> bool:
        """True if paused manually or auto-paused because user is live."""
        return self.paused or self.auto_paused

    def _flush_queued_vods(self, reason: str = "unpause") -> int:
        """Hand every queued VOD to the paced open queue and clear the
        VOD queue. Returns the count enqueued.

        Called when the pause that gated VOD-queueing lifts. The actual
        opens happen on the tab-open worker, spaced tab_open_spacing
        seconds apart, so a multi-VOD flush doesn't slam the browser
        with simultaneous tabs.
        """
        if not self.queued_vods:
            return 0
        items = list(self.queued_vods.items())
        for streamer, entry in items:
            self._enqueue_tab_open(
                "vod", streamer, entry["url"],
                from_queue=True, queue_reason=reason,
            )
        self.queued_vods.clear()
        count = len(items)
        log.info("Flushed %d queued VOD(s) into the paced open queue (reason=%s)", count, reason)
        self.notify_callback(
            "Stream Monitor",
            f"Opening {count} queued VOD(s), {int(self.tab_open_spacing)}s apart"
            if count > 1 else
            f"Opening queued VOD for {items[0][0]}"
        )
        return count

    def _list_rank(self, name: str) -> int:
        """Position of a streamer in the settings list (0 = top). The list
        order is the user's priority for OPEN order: rescue queue order
        within each tier and which live streams open first when several
        go live at once. It never decides which tab gets closed; max-tabs
        displacement stays oldest-first plus Keep Open. Unlisted names
        (bell/sidebar rescue finds) sort after every listed one."""
        try:
            return [s.lower() for s in self.config.streamers].index(name.lower())
        except ValueError:
            return len(self.config.streamers)

    def _build_rescue_candidates(self, live_set: set) -> list:
        """Assemble the rescue queue in priority order: streams that ended
        while we were paused first (save-streak URLs), then streams that
        are still live right now. Within each tier the settings list order
        is the priority (top of the list first), with earliest-ended as
        the tiebreak for the ended tier."""
        live_names = sorted(
            (name for name in list(self.missed_while_paused) if name in live_set),
            key=self._list_rank,
        )
        live_name_set = set(live_names)
        # A streamer can be in both lists: ended during the pause (VOD
        # queued) and live again by the time the pause lifted. Watching
        # the live stream saves the streak, so the live candidate wins
        # and the save-streak candidate is dropped.
        ended = [
            {
                "streamer": streamer,
                "url": entry["url"],
                "kind": "ended",
                "ended_at": entry.get("ended_at"),
            }
            for streamer, entry in self.queued_vods.items()
            if streamer not in live_name_set
        ]
        ended.sort(key=lambda c: (self._list_rank(c["streamer"]), c.get("ended_at") or ""))
        live = [
            {
                "streamer": name,
                "url": f"https://twitch.tv/{name}?sm=1",
                "kind": "live",
                "ended_at": None,
            }
            for name in live_names
        ]
        return ended + live

    def _offer_rescue_or_flush(self, live_set: set):
        """Publish a rescue offer in /config for the extension to claim.
        Ownership of queued_vods / missed_while_paused entries stays with
        the desktop until the extension acks; _maybe_fallback_rescue opens
        everything the old way if no ack arrives in time."""
        candidates = self._build_rescue_candidates(live_set)
        if not candidates:
            return
        offer = {
            "id": f"rescue-{int(time.time() * 1000)}",
            "created_at": _activity_timestamp(),
            "batch_size": 3,
            "rotate_minutes": 30,
            "candidates": candidates,
        }
        with self._rescue_lock:
            self.rescue_pending = offer
            self._rescue_offer_started_monotonic = time.monotonic()
            self._rescue_deadline_monotonic = time.monotonic() + RESCUE_ACK_TIMEOUT_SECONDS
            self._rescue_overdue_logged = False
        ConfigRequestHandler.config_data["rescue"] = offer
        log.info(
            "Rescue offer %s published: %d candidate(s), extension has %ds to acknowledge",
            offer["id"], len(candidates), RESCUE_ACK_TIMEOUT_SECONDS,
        )
        log_activity("rescue_offered", offer_id=offer["id"], count=len(candidates))

    def acknowledge_rescue(self, offer_id: str) -> bool:
        """Called from the HTTP thread when the extension POSTs /rescue_ack.
        Hands ownership of the offered candidates to the extension so the
        desktop neither flushes them later nor re-opens the live ones."""
        with self._rescue_lock:
            offer = self.rescue_pending
            if not offer or offer["id"] != offer_id:
                return False
            self.rescue_pending = None
        for cand in offer["candidates"]:
            name = cand["streamer"]
            if cand["kind"] == "ended":
                self.queued_vods.pop(name, None)
            else:
                self.missed_while_paused.pop(name, None)
                # A VOD queued for the same streamer (ended during the
                # pause, live again now) is covered by the live tab the
                # extension is about to open; drop it so it can't flush
                # a duplicate save-streak tab later.
                self.queued_vods.pop(name, None)
                state = self.streamers.get(name)
                if state is not None:
                    state.browser_opened = True
        ConfigRequestHandler.config_data["rescue"] = None
        ConfigRequestHandler.config_data["queued_vods"] = dict(self.queued_vods)
        log.info(
            "Rescue offer %s acknowledged; extension is rotating %d stream(s)",
            offer_id, len(offer["candidates"]),
        )
        log_activity("rescue_acked", offer_id=offer_id, count=len(offer["candidates"]))
        self.notify_callback(
            "Stream Monitor",
            f"Streak rescue started: rotating {len(offer['candidates'])} stream(s), 3 at a time, 30 min per turn."
        )
        return True

    def _maybe_fallback_rescue(self):
        """Monitor-loop tick: if a published rescue offer was never acked,
        open everything the pre-1.7 way.

        The blind 180s deadline is meant for an absent extension (browser
        closed, pre-1.7 version). If /config polls are still arriving the
        extension is alive and merely failing to ack, so the offer stays
        published (it will retry on every poll) until the hard deadline."""
        offer_to_flush = None
        overdue_offer_id = None
        now = time.monotonic()
        with self._rescue_lock:
            offer = self.rescue_pending
            if not offer or now < self._rescue_deadline_monotonic:
                return
            hard_deadline = (
                self._rescue_offer_started_monotonic + RESCUE_ACK_HARD_TIMEOUT_SECONDS
            )
            if now < hard_deadline and extension_seen_within(EXTENSION_ALIVE_WINDOW_SECONDS):
                if not self._rescue_overdue_logged:
                    self._rescue_overdue_logged = True
                    overdue_offer_id = offer["id"]
            else:
                self.rescue_pending = None
                offer_to_flush = offer
        if overdue_offer_id is not None:
            log.warning(
                "Rescue offer %s unacked after %ds but the extension is still polling /config; "
                "holding the offer up to %ds before falling back",
                overdue_offer_id, RESCUE_ACK_TIMEOUT_SECONDS, RESCUE_ACK_HARD_TIMEOUT_SECONDS,
            )
            log_activity("rescue_ack_overdue", offer_id=overdue_offer_id)
        if offer_to_flush is None:
            return
        ConfigRequestHandler.config_data["rescue"] = None
        log.warning(
            "Rescue offer %s not acknowledged; falling back to paced open of everything",
            offer_to_flush["id"],
        )
        log_activity("rescue_fallback_flush", offer_id=offer_to_flush["id"])
        opened_live = self._open_still_live_missed_streams(
            set(self.live_streamers), reason="rescue_fallback"
        )
        for name in opened_live:
            # Their live tab was just opened; a queued save-streak link
            # for the same streamer would only open a duplicate tab.
            if self.queued_vods.pop(name, None) is not None:
                log.info("Dropped queued VOD for %s: their live stream was just opened", name)
        self._flush_queued_vods(reason="rescue_fallback")

    def _open_still_live_missed_streams(self, live_set: set, reason: str = "unpause") -> list:
        """When a pause lifts, open the LIVE stream for every streamer that
        was skipped while paused and is still live right now.

        Without this, a streamer who went live during the pause and is
        still broadcasting when the pause ends would never get opened:
        process_state_changes only opens on the offline->live transition
        (not state.was_live), and that transition already happened (and was
        skipped) earlier. They'd otherwise only surface via the VOD
        fallback once they finally ended their stream.

        Streamers that are no longer live were already handled by the VOD
        fallback when they went offline during the pause (queued + flushed),
        so we only act on the still-live ones here. Opens route through the
        paced queue. Returns the list of streamer names opened.
        """
        if not self.missed_while_paused:
            return []
        still_live = sorted(
            (name for name in list(self.missed_while_paused) if name in live_set),
            key=self._list_rank,
        )
        for name in still_live:
            self.missed_while_paused.pop(name, None)
            state = self.streamers.get(name)
            if state is not None:
                state.browser_opened = True  # so process_state_changes won't re-open/skip-confuse
            self.open_stream(name)
        if still_live:
            log.info(
                "Opening %d still-live missed stream(s) on pause lift (reason=%s): %s",
                len(still_live), reason, still_live,
            )
            self.notify_callback(
                "Stream Monitor",
                f"Opening {len(still_live)} live stream(s) you missed, {int(self.tab_open_spacing)}s apart"
                if len(still_live) > 1 else
                f"Opening {still_live[0]}'s live stream"
            )
        return still_live

    def process_state_changes(self, current_status: dict[str, bool]):
        live_count = 0
        # Update config server with live status
        ConfigRequestHandler.config_data["live_streamers"] = self.live_streamers
        ConfigRequestHandler.config_data["paused"] = self.paused
        ConfigRequestHandler.config_data["auto_paused"] = self.auto_paused
        # Surface the queue so the extension popup and any future UI can
        # show which VODs are waiting for the pause to lift.
        ConfigRequestHandler.config_data["queued_vods"] = dict(self.queued_vods)

        # List order is the priority when several streamers go live on the
        # same poll: opens are enqueued top-of-list first.
        for username, is_live in sorted(
            current_status.items(), key=lambda kv: self._list_rank(kv[0])
        ):
            state = self.streamers[username]

            if is_live:
                live_count += 1
                if not state.was_live and not state.browser_opened:
                    log.info("State change: %s went LIVE (was_live=%s, browser_opened=%s, paused=%s, auto_paused=%s)",
                             username, state.was_live, state.browser_opened, self.paused, self.auto_paused)

                    # Activity log: structured record of the live transition
                    meta = self.live_stream_meta.get(username, {})
                    log_activity("stream_live", streamer=username, **meta)

                    # Desktop notification for all live events
                    self.notify_callback(
                        "Stream Monitor",
                        f"{username} is now live on Twitch!"
                    )

                    claimed_by = self._browsers_with_tab_open(username)
                    if claimed_by:
                        # Already open in the browser (per the extension's
                        # last report): no second tab. Settled later by
                        # _reconcile_startup_skips.
                        log.info(
                            "Skipping tab open for %s: already open in %s",
                            username, ", ".join(sorted(claimed_by)),
                        )
                        log_activity(
                            "tab_open_skipped",
                            streamer=username,
                            reason="already_open",
                            browsers=sorted(claimed_by),
                        )
                        state.browser_opened = True
                        self._startup_claims[username] = set(claimed_by)
                        self._startup_skipped.add(username)
                    elif self.effectively_paused:
                        log.info("Skipping tab open for %s (effectively paused)", username)
                        self.status_callback(f"{username} went LIVE! (paused)")
                        # Track missed streams while paused
                        self.missed_while_paused[username] = time.strftime("%H:%M:%S")
                        log_activity(
                            "tab_open_skipped",
                            streamer=username,
                            reason="auto_paused" if self.auto_paused else "paused",
                        )
                    else:
                        self.status_callback(f"{username} went LIVE!")
                        self.open_stream(username)
                        state.browser_opened = True
                elif is_live and state.was_live:
                    log.debug("Already tracking %s as live (was_live=%s, browser_opened=%s)",
                              username, state.was_live, state.browser_opened)
                state.was_live = True
            else:
                if state.was_live:
                    log.info("State change: %s went OFFLINE", username)
                    self.status_callback(f"{username} went offline")
                    log_activity("stream_offline", streamer=username)

                    # VOD fallback now fires ONLY when this exact stream was
                    # skipped earlier because Stream Monitor was paused or
                    # auto-paused (im_live_pause for "I'm live"). Previously
                    # it fired whenever browser_opened was false, which
                    # included unrelated cases (Stream Monitor downtime,
                    # webbrowser.open failure, etc.) and opened VODs the
                    # user never wanted.
                    #
                    # The opened URL is the canonical Twitch save-streak
                    # deep link with ?sm=1 so the extension tracks the tab
                    # and applies the same auto-mute / low-quality /
                    # player-keepalive treatment as a normal stream tab.
                    was_skipped_due_to_pause = username in self.missed_while_paused
                    if was_skipped_due_to_pause:
                        # Always leave the missed-while-paused list on the
                        # offline transition, even when VOD fallback is off.
                        # Stale entries used to survive here when the
                        # fallback was disabled and could trigger a
                        # duplicate open on a much later pause lift.
                        self.missed_while_paused.pop(username, None)
                    if was_skipped_due_to_pause and self.config.vod_fallback:
                        save_streak_url = f"https://www.twitch.tv/save-streak/{username}?sm=1"
                        if self.effectively_paused:
                            self.queued_vods[username] = {
                                "url": save_streak_url,
                                # Rescue-queue priority key: earliest-ended
                                # streams have the least save window left.
                                "ended_at": _activity_timestamp(),
                            }
                            reason = "auto_paused" if self.auto_paused else "paused"
                            log.info(
                                "Save-streak URL for %s queued (reason=%s, queue size now %d)",
                                username, reason, len(self.queued_vods),
                            )
                            log_activity(
                                "vod_queued",
                                streamer=username,
                                url=save_streak_url,
                                reason=reason,
                            )
                            self.notify_callback(
                                "Stream Monitor",
                                f"Save-streak link for {username} queued (opens when you're no longer paused)"
                            )
                        else:
                            self.status_callback(f"Opening save-streak page for {username}")
                            self._enqueue_tab_open("vod", username, save_streak_url)

                    state.was_live = False
                    state.browser_opened = False

        if self.auto_paused:
            if live_count > 0:
                self.status_callback(f"AUTO-PAUSED (you're live) - {live_count} streamer(s) live")
            else:
                self.status_callback("Auto-paused (you're live)")
        elif self.paused:
            if live_count > 0:
                self.status_callback(f"PAUSED - {live_count} streamer(s) live")
            else:
                self.status_callback("Paused")
        elif live_count > 0:
            self.status_callback(f"{live_count} streamer(s) live")
        else:
            self.status_callback("Monitoring...")

        # The seed only ever applies to the first poll after a fresh start;
        # a stream that goes live later gets a tab as usual.
        self._startup_seed = {}
        self._reconcile_startup_skips(current_status)
    
    def _monitor_loop(self):
        log.info("Monitor loop started (interval: %ds)", self.config.check_interval)
        last_iteration_mono = time.monotonic()
        while self.running:
            # Detect long gaps that suggest the system was asleep/hibernating.
            # Helpful for cross-checking missed streams against power events.
            now_mono = time.monotonic()
            gap = now_mono - last_iteration_mono
            last_iteration_mono = now_mono
            expected_gap = self.config.check_interval
            if gap > expected_gap * 2:
                log.warning(
                    "Long loop gap: %.1fs (expected ~%ds, system likely slept)",
                    gap, expected_gap,
                )
                log_activity(
                    "wake_detected",
                    gap_seconds=round(gap, 1),
                    expected_seconds=expected_gap,
                )

            try:
                current_status = self.check_streams()
                if current_status:
                    self.process_state_changes(current_status)
                    if self.consecutive_errors > 0:
                        log.info("API recovered after %d consecutive error(s)", self.consecutive_errors)
                        self.notify_callback("Stream Monitor", "Connection restored! Monitoring is working again.")
                        log_activity(
                            "api_recovered",
                            recovered_after=self.consecutive_errors,
                        )
                    self.consecutive_errors = 0
            except Exception as e:
                self.consecutive_errors += 1
                log.error("Unexpected error in monitor loop (streak: %d): %s", self.consecutive_errors, e, exc_info=True)
                log_activity(
                    "api_error",
                    error=str(e)[:200],
                    consecutive_errors_streak=self.consecutive_errors,
                )
                if self.consecutive_errors == 1:
                    self.status_callback("Error: API connection failed")
                    self.notify_callback(
                        "Stream Monitor - Error",
                        f"API calls are failing: {e}\nStreams won't open until this is resolved. Try restarting Stream Monitor."
                    )
                elif self.consecutive_errors == 5:
                    self.status_callback("Error: API still failing")
                    self.notify_callback(
                        "Stream Monitor - Error",
                        "API has been failing for 5 minutes. Stream Monitor needs to be restarted."
                    )

            # Rescue-offer watchdog: runs even when the API check above
            # failed, so a network blip can't strand an unacked offer.
            try:
                self._maybe_fallback_rescue()
            except Exception as e:
                log.error("Rescue fallback check failed: %s", e)

            for _ in range(self.config.check_interval):
                if not self.running:
                    break
                time.sleep(1)
    
    def start(self, preserve_state: bool = False) -> bool:
        """Begin monitoring. With preserve_state, streamers still on the
        list keep their live/opened state across the restart a settings
        change triggers, so a stream that is live with a tab open is not
        opened a second time (the old restart reset every streamer to
        "never seen" and re-opened everything live). A manual Start from
        the tray still begins from a clean slate."""
        log.info("Starting monitor...")
        if not self.config.is_valid():
            log.error("Cannot start: config is invalid (missing client_id, client_secret, or streamers)")
            self.status_callback("Invalid config")
            return False

        self.status_callback("Authenticating...")
        if not self._get_oauth_token():
            # On a restart (a settings save) the refresh can fail on a
            # transient blip. The existing app token stays valid for weeks
            # and the API layer re-authenticates on a 401, so keep
            # monitoring on it rather than leaving the monitor stopped with
            # only a tooltip saying so. A manual Start with no token still
            # fails, as before.
            if preserve_state and getattr(self, "oauth_token", None):
                log.warning("Token refresh failed on restart; continuing with the existing token")
            else:
                self.status_callback("Auth failed")
                return False

        previous = getattr(self, "streamers", None) or {}
        self.streamers = {
            name.lower(): (previous.get(name.lower()) if preserve_state else None)
            or StreamerState(name=name.lower())
            for name in self.config.streamers
        }
        log.info("Monitoring %d streamer(s): %s", len(self.streamers), list(self.streamers.keys()))
        if not preserve_state:
            self._seed_startup_open_tabs()

        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self.status_callback("Monitoring...")
        return True

    def stop(self):
        log.info("Stopping monitor")
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.status_callback("Stopped")

    def _seed_startup_open_tabs(self) -> None:
        """On a fresh start, take the newest open-tab report per browser
        (received by this process, else the copy the previous process
        mirrored to disk) that is at most STARTUP_OPEN_TABS_MAX_AGE_SECONDS
        old. A streamer in it who is live on the first poll is treated as
        already open, so no second tab is opened. A report made after this
        start (or a timeout) then settles each skip for good in
        _reconcile_startup_skips."""
        now_epoch = time.time()
        self._startup_mono = time.monotonic()
        self._startup_claims = {}
        self._startup_skipped = set()
        seed = {
            browser: set(names)
            for browser, names in load_persisted_open_tabs(
                STARTUP_OPEN_TABS_MAX_AGE_SECONDS, now_epoch
            ).items()
        }
        for browser, rep in extension_open_tabs_snapshot().items():
            if now_epoch - rep["epoch"] <= STARTUP_OPEN_TABS_MAX_AGE_SECONDS:
                seed[browser] = set(rep["streamers"])
        self._startup_seed = seed
        if any(seed.values()):
            log.info(
                "Extension reports stream tabs already open: %s",
                {b: sorted(s) for b, s in seed.items() if s},
            )

    def _browsers_with_tab_open(self, name: str) -> set:
        return {b for b, names in self._startup_seed.items() if name in names}

    def _reconcile_startup_skips(self, current_status: dict[str, bool]) -> None:
        """Settle streams skipped at start because a tab was reported open.
        A report received after this start that lists the streamer confirms
        the skip. Once every browser that claimed the tab has reported
        again without it, or no report has arrived within
        STARTUP_FRESH_REPORT_TIMEOUT_SECONDS (browser closed, extension
        gone), the stream is opened after all if it is still live."""
        if not self._startup_skipped:
            return
        fresh = {
            b: rep for b, rep in extension_open_tabs_snapshot().items()
            if rep["mono"] >= self._startup_mono
        }
        timed_out = (
            time.monotonic() - self._startup_mono >= STARTUP_FRESH_REPORT_TIMEOUT_SECONDS
        )
        for name in sorted(self._startup_skipped, key=self._list_rank):
            if any(name in rep["streamers"] for rep in fresh.values()):
                log.info("%s: tab confirmed open by the extension", name)
                self._startup_skipped.discard(name)
                continue
            claims = self._startup_claims.get(name, set())
            gone = bool(claims) and claims <= set(fresh)
            if not (gone or timed_out):
                continue  # still waiting on a browser that claimed it
            self._startup_skipped.discard(name)
            state = self.streamers.get(name)
            if state is None or not current_status.get(name):
                continue  # off the list, or offline now: nothing to open
            why = "the tab is gone" if gone else "no report arrived in time"
            if self.effectively_paused:
                state.browser_opened = False
                self.missed_while_paused[name] = time.strftime("%H:%M:%S")
                log.info("%s: %s, but paused, so not opening (tracked as missed)", name, why)
                log_activity(
                    "tab_open_skipped",
                    streamer=name,
                    reason="auto_paused" if self.auto_paused else "paused",
                )
            else:
                log.info("%s: %s, opening it now", name, why)
                self.status_callback(f"{name} went LIVE!")
                self.open_stream(name)

    def restart(self):
        """Restart after a settings change, keeping the state of streamers
        still on the list so nothing already open is opened again."""
        log.info("Restarting monitor")
        self.stop()
        time.sleep(0.5)
        self.start(preserve_state=True)


def create_icon_image(color="green"):
    """Create a simple colored circle icon."""
    colors = {
        "green": "#00ff00",
        "red": "#ff0000",
        "gray": "#808080",
        "purple": "#9146FF"  # Twitch purple
    }
    
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Draw circle
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=colors.get(color, color)
    )
    
    return image


class StreamMonitorApp:
    def __init__(self):
        self.config = Config.load()
        self._ensure_install_id()
        self.monitor: Optional[TwitchMonitor] = None
        self.icon: Optional[pystray.Icon] = None
        self.status = "Starting..."
        
    def update_status(self, status: str):
        self.status = status
        if self.icon:
            # Windows tray tooltip is limited to 128 characters
            title = f"Stream Monitor - {status}"
            if len(title) > 127:
                title = title[:124] + "..."
            self.icon.title = title

    def send_notification(self, title: str, message: str):
        """Send a system tray notification."""
        log.info("Notification: [%s] %s", title, message)
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception as e:
                log.error("Notification failed: %s", e)
    
    def on_settings(self, icon, item):
        """Open settings dialog by launching the settings editor."""
        import subprocess
        
        if getattr(sys, 'frozen', False):
            # Running as exe - launch the settings exe
            settings_exe = Path(sys.executable).parent / "StreamMonitorSettings.exe"
            if settings_exe.exists():
                subprocess.Popen([str(settings_exe)])
            else:
                self.update_status("Settings exe not found")
        else:
            # Running as script - launch the settings editor script
            settings_script = Path(__file__).parent / "settings_editor.py"
            if settings_script.exists():
                subprocess.Popen([sys.executable, str(settings_script)])
            else:
                # Fall back to setup wizard
                setup_script = Path(__file__).parent / "setup_wizard.py"
                subprocess.Popen([sys.executable, str(setup_script)])
        
        # Saved changes are picked up by the always-on config watcher
        # (_config_watch_loop), not by a timer tied to this window.
    
    def _ensure_install_id(self) -> None:
        """Mint the random install id on first run and persist it. Saving
        from here is safe with the config watcher: self.config already holds
        the new value, so the watcher sees no difference and does nothing."""
        if self.config.install_id:
            return
        self.config.install_id = str(uuid.uuid4())
        try:
            self.config.save()
        except OSError as e:
            log.warning("Could not persist install_id: %s", e)

    def _usage_ping_loop(self):
        """Daily anonymous install ping. Dev (non-frozen) runs never ping. The
        usage_ping setting is re-read before every send, so turning it off in
        Settings stops the next ping without a restart. A failed send retries
        in an hour instead of a day, so a server outage does not cost a whole
        day of signal."""
        if not getattr(sys, "frozen", False):
            return
        time.sleep(15)
        while True:
            if not self.config.usage_ping or not self.config.install_id:
                time.sleep(USAGE_PING_INTERVAL_SECONDS)
                continue
            ok = _send_usage_ping(self.config.install_id, VERSION)
            time.sleep(USAGE_PING_INTERVAL_SECONDS if ok else USAGE_PING_RETRY_SECONDS)

    def _config_watch_loop(self):
        """Apply settings saved by the editor for the life of the process.
        Polls the config file's mtime every 2s. The old watcher only ran
        for 4 minutes after Settings was opened, so a later Save silently
        did nothing until the next app start."""
        def mtime():
            try:
                return CONFIG_FILE.stat().st_mtime_ns
            except OSError:
                return None

        last_seen = mtime()
        while True:
            time.sleep(2)
            current = mtime()
            if current == last_seen:
                continue
            new_config = _read_config_if_parseable()
            if new_config is None:
                continue  # mid-write or missing: look again next tick
            last_seen = current
            before = json.dumps(asdict(self.config), sort_keys=True)
            after = json.dumps(asdict(new_config), sort_keys=True)
            if before != after:
                self._apply_config_change(new_config)

    def _apply_config_change(self, new_config: "Config") -> None:
        self.config = new_config
        ConfigRequestHandler.config_data.update({
            "streamers": self.config.streamers,
            "pinned_streamers": self.config.pinned_streamers,
            "version": VERSION,
        })
        log_activity(
            "config_loaded",
            streamers=list(self.config.streamers),
            pinned_streamers=list(self.config.pinned_streamers),
            interval=self.config.check_interval,
            reason="settings_changed",
        )
        if self.monitor:
            self.monitor.config = self.config
            self.monitor.restart()

    def on_start(self, icon, item):
        if self.monitor and not self.monitor.running:
            self.monitor.start()
    
    def on_stop(self, icon, item):
        if self.monitor and self.monitor.running:
            self.monitor.stop()
    
    def on_check_updates(self, icon, item):
        """Check for updates and notify user."""
        threading.Thread(target=self._check_for_updates_ui, daemon=True).start()
    
    def _check_for_updates_ui(self):
        """Manual tray-menu check: prompt and install like the launch
        check, but ignore a saved "do not remind me" for the version,
        since the user explicitly asked."""
        self.update_status("Checking for updates...")
        update_available, latest_version, release = self.check_for_updates()

        if update_available:
            self.update_status(f"Update available: v{latest_version}")
            self._offer_update(latest_version, release)
        else:
            self.update_status("Up to date!")
            time.sleep(3)
            if self.monitor and self.monitor.running:
                self.update_status("Monitoring...")
            else:
                self.update_status("Stopped")

    def _offer_update(self, latest_version: str, release: dict) -> None:
        """Prompt the user and act on their answer."""
        choice = self._prompt_update_dialog(latest_version)
        if choice == "yes":
            self._download_and_install_update(latest_version, release)
        elif choice == "later_skip":
            self.config.skip_update_version = latest_version
            self.config.save()
            log.info("User muted update reminders for v%s", latest_version)
        else:
            log.info("User deferred update to v%s", latest_version)
    
    def check_for_updates(self) -> tuple[bool, str, dict]:
        """
        Check GitHub for newer releases.
        Returns: (update_available, latest_version, release_data). The
        release_data dict is GitHub's release JSON (assets included) so
        the installer can be downloaded without a second API call; it is
        empty when the check failed.
        """
        try:
            response = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            latest_version = data.get("tag_name", "").lstrip("v")
            if self._is_newer_version(latest_version, VERSION):
                return True, latest_version, data

            return False, latest_version, data

        except requests.RequestException as e:
            log.error("Update check failed: %s", e)
            return False, VERSION, {}

    def _is_newer_version(self, latest: str, current: str) -> bool:
        """Compare version strings (e.g., '1.2.0' > '1.1.0')."""
        if not latest:
            return False
        latest_parts = _version_parts(latest)
        current_parts = _version_parts(current)

        # Pad to same length
        while len(latest_parts) < len(current_parts):
            latest_parts.append(0)
        while len(current_parts) < len(latest_parts):
            current_parts.append(0)

        return latest_parts > current_parts

    def _prompt_update_dialog(self, latest_version: str) -> str:
        """Show the update prompt in a SEPARATE process and return the
        user's choice: "yes", "later", or "later_skip". pystray and
        tkinter cannot share this process without breaking keyboard
        input, the same reason the settings editor is its own process."""
        import subprocess
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--update-dialog", VERSION, latest_version]
            else:
                cmd = [
                    sys.executable, str(Path(__file__).resolve()),
                    "--update-dialog", VERSION, latest_version,
                ]
            proc = subprocess.run(cmd)
            return {10: "yes", 11: "later", 12: "later_skip"}.get(proc.returncode, "later")
        except Exception as e:
            log.error("Update dialog failed: %s", e)
            return "later"

    def _download_and_install_update(self, latest_version: str, release: dict) -> None:
        """Download the installer from the release, verify it against the
        release's own SHA256SUMS.txt, then hand off to a detached script
        that runs the silent install and relaunches the app, and exit."""
        import subprocess
        try:
            assets = {
                a.get("name"): a.get("browser_download_url")
                for a in release.get("assets", [])
            }
            installer_url = assets.get("StreamMonitorInstaller.exe")
            sums_url = assets.get("SHA256SUMS.txt")
            if not installer_url or not sums_url:
                raise RuntimeError("release is missing the installer or SHA256SUMS.txt asset")

            self.update_status(f"Downloading update v{latest_version}...")
            self.send_notification("Stream Monitor", f"Downloading update v{latest_version}...")

            expected = _installer_hash_from_sums(_http_get_text_with_retry(sums_url))
            if not expected:
                raise RuntimeError("installer hash not found in SHA256SUMS.txt")

            update_dir = CONFIG_DIR / "update"
            update_dir.mkdir(parents=True, exist_ok=True)
            target = update_dir / f"StreamMonitorInstaller-{latest_version}.exe"
            actual = _download_file_with_retry(installer_url, target)
            if actual.lower() != expected:
                target.unlink(missing_ok=True)
                raise RuntimeError("downloaded installer failed SHA256 verification")

            # The installer cannot replace a running exe, so a detached
            # cmd script waits for this process to exit, installs
            # silently, relaunches the app, and cleans up after itself.
            #
            # Relaunch uses explorer.exe, not "start". Launching the new
            # onefile exe as a descendant of THIS exiting onefile app made
            # its bootloader fail to load its extracted python DLL
            # ("Failed to load Python DLL ... _MEI...\python312.dll ... The
            # specified module could not be found"), reproducibly, on a
            # clean self-update. Handing the path to explorer.exe launches
            # it as a child of the shell instead, with a clean process and
            # environment, which loads correctly. A bounded retry that
            # polls the config server still guards against a transient miss.
            #
            # System tools are called by full path, never by bare name. cmd
            # resolves a bare name through the PATH this script inherits from
            # the app. When that PATH lists Git for Windows' GNU tools
            # (Git\usr\bin) before System32, as it does for anything started
            # from Git Bash, a bare "timeout" is GNU timeout, which rejects
            # "/t 3 /nobreak" and exits at once, so every wait in this script
            # would be skipped.
            bat = update_dir / "apply_update.bat"
            exe = sys.executable
            config_url = f"http://127.0.0.1:{CONFIG_SERVER_PORT}/config"
            timeout_exe = r'"%SystemRoot%\System32\timeout.exe"'
            curl_exe = r'"%SystemRoot%\System32\curl.exe"'
            explorer_exe = r'"%SystemRoot%\explorer.exe"'
            bat.write_text(
                "@echo off\r\n"
                'set "_MEIPASS2="\r\n'
                f"{timeout_exe} /t 3 /nobreak >nul\r\n"
                f'"{target}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART\r\n'
                f"{timeout_exe} /t 5 /nobreak >nul\r\n"
                f'{explorer_exe} "{exe}"\r\n'
                "for /L %%i in (1,1,6) do (\r\n"
                f"  {timeout_exe} /t 3 /nobreak >nul\r\n"
                f'  {curl_exe} -s -m 2 "{config_url}" >nul 2>&1 && goto smdone\r\n'
                ")\r\n"
                f'{explorer_exe} "{exe}"\r\n'
                "for /L %%i in (1,1,6) do (\r\n"
                f"  {timeout_exe} /t 3 /nobreak >nul\r\n"
                f'  {curl_exe} -s -m 2 "{config_url}" >nul 2>&1 && goto smdone\r\n'
                ")\r\n"
                ":smdone\r\n"
                f'del "{target}"\r\n'
                'del "%~f0"\r\n',
                encoding="utf-8",
            )
            log.info("Update v%s verified; handing off to the installer and exiting", latest_version)
            log_activity(
                "update_install_started",
                from_version=VERSION, to_version=latest_version,
            )
            # Strip _MEIPASS2 from the child environment. A PyInstaller
            # onefile app sets it to its own temp extraction dir; a child
            # that is also a onefile exe (the relaunched app) would inherit
            # it and try to load its python DLL from THIS app's dir, which
            # is deleted moments later on exit, failing with "Failed to
            # load Python DLL". Clearing it lets the installer and the
            # relaunched app extract their own bundles cleanly.
            CREATE_NO_WINDOW = 0x08000000
            child_env = os.environ.copy()
            child_env.pop("_MEIPASS2", None)
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=CREATE_NO_WINDOW,
                close_fds=True,
                env=child_env,
            )
            if self.monitor:
                self.monitor.stop()
            if self.icon:
                self.icon.stop()
        except Exception as e:
            log.error("Update install failed: %s", e)
            log_activity(
                "update_install_failed",
                to_version=latest_version, error=str(e),
            )
            self.update_status(f"Update v{latest_version} failed; will retry next launch")
            self.send_notification(
                "Stream Monitor",
                f"Update to v{latest_version} failed ({e}). It will be offered again on the next launch.",
            )
    
    def on_about(self, icon, item):
        """Open the about page in the browser."""
        webbrowser.open(f"http://127.0.0.1:{CONFIG_SERVER_PORT}/about?v={VERSION}")

    def on_exit(self, icon, item):
        log.info("Exiting Stream Monitor")
        log_activity("app_stopped", reason="user_exit")
        if self.monitor:
            self.monitor.stop()
        icon.stop()

    def on_view_logs(self, icon, item):
        """Open the unified log viewer (Stream Activity + Debug Log) in the browser."""
        webbrowser.open(f"http://127.0.0.1:{CONFIG_SERVER_PORT}/logs")
    
    def _is_running(self):
        return self.monitor and self.monitor.running

    def create_menu(self):
        return pystray.Menu(
            Item("Settings", self.on_settings),
            Item("Check for Updates", self.on_check_updates),
            pystray.Menu.SEPARATOR,
            Item(
                lambda item: f"Queued VODs ({len(self.monitor.queued_vods) if self.monitor else 0})",
                pystray.Menu(lambda: tuple(self._iter_queued_vod_menu_items())),
                visible=lambda item: bool(self.monitor and self.monitor.queued_vods),
            ),
            Item("Start", self.on_start, checked=lambda item: self._is_running()),
            Item("Stop", self.on_stop, checked=lambda item: not self._is_running()),
            pystray.Menu.SEPARATOR,
            Item("View Logs", self.on_view_logs),
            Item("About (CaedVT)", self.on_about),
            Item("Exit", self.on_exit)
        )

    def _iter_queued_vod_menu_items(self):
        """Yield one menu item per queued VOD, plus a separator and a clear-all
        item at the bottom. pystray re-evaluates this every time the submenu
        opens, so we always show the current queue state.
        """
        if not self.monitor or not self.monitor.queued_vods:
            yield Item("(none queued)", None, enabled=False)
            return
        # Snapshot to avoid mutation during iteration if a flush fires.
        for streamer, entry in list(self.monitor.queued_vods.items()):
            yield Item(
                f"Open {streamer}'s VOD",
                # Bind streamer and url in default args; closure-over-loop-var
                # would otherwise capture only the final pair.
                lambda icon, item, s=streamer, u=entry["url"]: self._open_queued_vod_now(s, u),
            )
        yield pystray.Menu.SEPARATOR
        yield Item("Clear all queued", self._clear_all_queued_vods)

    def _open_queued_vod_now(self, streamer: str, vod_url: str):
        """User clicked a queued-VOD row in the tray submenu: hand it to
        the paced open queue and remove it from the VOD queue. (Still
        paced: if another tab opened within the last few seconds, this one
        waits out the remainder of the spacing window so the browser isn't
        slammed.)"""
        log.info("User manually opening queued VOD for %s from tray", streamer)
        if self.monitor:
            self.monitor._enqueue_tab_open(
                "vod", streamer, vod_url,
                from_queue=True, queue_reason="manual_tray",
            )
            self.monitor.queued_vods.pop(streamer, None)
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _clear_all_queued_vods(self, icon, item):
        """User picked 'Clear all queued': drop every entry without opening
        anything."""
        if not self.monitor:
            return
        count = len(self.monitor.queued_vods)
        for streamer in list(self.monitor.queued_vods.keys()):
            log_activity("vod_queue_cleared", streamer=streamer)
        self.monitor.queued_vods.clear()
        log.info("Cleared %d queued VOD(s) from tray", count)
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass
    
    def _run_first_time_setup(self):
        """Show in-process first-time setup dialog. Returns True if config is now valid."""
        import tkinter as tk
        from tkinter import ttk, messagebox

        result = {"completed": False}

        dialog = tk.Tk()
        dialog.title(f"Stream Monitor Setup - v{VERSION}")
        dialog.geometry("550x620")
        dialog.resizable(False, False)

        # Center window
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 550) // 2
        y = (dialog.winfo_screenheight() - 620) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.lift()
        dialog.attributes('-topmost', True)
        dialog.after(100, lambda: dialog.attributes('-topmost', False))

        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="Welcome to Stream Monitor!", font=("", 16, "bold")).pack(pady=(0, 5))
        ttk.Label(main_frame, text="Let's get you set up. This only takes a couple minutes.", font=("", 10)).pack(pady=(0, 15))

        # Streamers
        ttk.Label(main_frame, text="Streamers to Monitor:", font=("", 10, "bold")).pack(anchor=tk.W)
        ttk.Label(main_frame, text="(One per line)", font=("", 8)).pack(anchor=tk.W)
        streamers_text = tk.Text(main_frame, height=5, width=50)
        streamers_text.pack(fill=tk.X, pady=(5, 10))

        # Twitch credentials
        ttk.Label(main_frame, text="Twitch API Credentials:", font=("", 10, "bold")).pack(anchor=tk.W)

        help_frame = ttk.Frame(main_frame)
        help_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(help_frame, text="Need credentials?", font=("", 9)).pack(side=tk.LEFT)
        help_btn = ttk.Button(help_frame, text="Open Twitch Developer Portal",
                              command=lambda: webbrowser.open("https://dev.twitch.tv/console/apps/create"))
        help_btn.pack(side=tk.LEFT, padx=(10, 0))

        instructions = ttk.Label(main_frame, font=("", 8), foreground="gray", justify=tk.LEFT,
                                 text="1. Name: anything  2. OAuth Redirect: http://localhost  "
                                      "3. Category: Other  4. Client Type: Confidential\n"
                                      "After creating, copy the Client ID and generate a Client Secret.")
        instructions.pack(anchor=tk.W, pady=(0, 8))

        cred_frame = ttk.Frame(main_frame)
        cred_frame.pack(fill=tk.X, pady=5)

        ttk.Label(cred_frame, text="Client ID:").grid(row=0, column=0, sticky=tk.W, pady=2)
        client_id_entry = ttk.Entry(cred_frame, width=50)
        client_id_entry.grid(row=0, column=1, pady=2, padx=(10, 0))

        ttk.Label(cred_frame, text="Client Secret:").grid(row=1, column=0, sticky=tk.W, pady=2)
        client_secret_entry = ttk.Entry(cred_frame, width=50, show="*")
        client_secret_entry.grid(row=1, column=1, pady=2, padx=(10, 0))

        show_var = tk.BooleanVar()
        def toggle_show():
            client_secret_entry.config(show="" if show_var.get() else "*")
        ttk.Checkbutton(cred_frame, text="Show", variable=show_var, command=toggle_show).grid(row=1, column=2, padx=(5, 0))

        # Test connection button
        test_label = ttk.Label(main_frame, text="", font=("", 9))

        def test_connection():
            cid = client_id_entry.get().strip()
            csec = client_secret_entry.get().strip()
            if not cid or not csec:
                test_label.config(text="Enter both credentials first.", foreground="red")
                return
            test_label.config(text="Testing...", foreground="gray")
            dialog.update()
            try:
                resp = requests.post(
                    "https://id.twitch.tv/oauth2/token",
                    params={"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"},
                    timeout=10
                )
                if resp.status_code == 200:
                    test_label.config(text="Connection successful!", foreground="green")
                else:
                    test_label.config(text="Authentication failed. Check credentials.", foreground="red")
            except Exception as e:
                test_label.config(text=f"Connection error: {e}", foreground="red")

        ttk.Button(main_frame, text="Test Connection", command=test_connection).pack(pady=(10, 0))
        test_label.pack(pady=(5, 10))

        # Status
        status_label = ttk.Label(main_frame, text="", font=("", 9))
        status_label.pack(pady=(5, 0))

        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        def save_and_close():
            streamers = [s.strip() for s in streamers_text.get("1.0", tk.END).strip().split("\n") if s.strip()]
            cid = client_id_entry.get().strip()
            csec = client_secret_entry.get().strip()

            if not streamers:
                messagebox.showerror("Error", "Please enter at least one streamer.")
                return
            if not cid:
                messagebox.showerror("Error", "Please enter your Client ID.")
                return
            if not csec:
                messagebox.showerror("Error", "Please enter your Client Secret.")
                return

            self.config.client_id = cid
            self.config.client_secret = csec
            self.config.streamers = streamers
            self.config.save()

            result["completed"] = True
            dialog.destroy()

        def cancel():
            dialog.destroy()

        ttk.Button(btn_frame, text="Cancel", command=cancel).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Save & Start Monitoring", command=save_and_close).pack(side=tk.RIGHT, padx=(0, 10))

        dialog.after(100, lambda: streamers_text.focus_set())
        dialog.mainloop()

        return result["completed"]

    def run(self):
        log.info("Stream Monitor v%s starting", VERSION)
        log.info("Config path: %s", CONFIG_FILE)
        log.info("Log path: %s", LOG_FILE)
        log_activity("app_started", version=VERSION)

        # If config is missing or has no credentials, run first-time setup
        if not self.config.is_valid():
            log.info("Config invalid or missing, launching first-time setup")
            if not self._run_first_time_setup():
                log.info("First-time setup cancelled, exiting")
                log_activity("app_stopped", reason="setup_cancelled")
                return

        log_activity(
            "config_loaded",
            streamers=list(self.config.streamers),
            pinned_streamers=list(self.config.pinned_streamers),
            interval=self.config.check_interval,
        )

        # Single-instance enforcement: try to bind the config server port
        # synchronously. If another Stream Monitor instance is already
        # running (e.g. the installer's CloseApplications missed a
        # PyInstaller runtime, or the user double-launched the tray app),
        # the bind fails and we exit silently to avoid duplicate monitor
        # loops, duplicate tab opens, and duplicate notifications.
        config_server = create_config_server(self.config)
        if config_server is None:
            log.error("Another instance is already running on port %d. Exiting.", CONFIG_SERVER_PORT)
            log_activity("app_stopped", reason="port_in_use")
            return

        threading.Thread(target=lambda: run_config_server(config_server), daemon=True).start()

        # Create monitor with notification callback
        self.monitor = TwitchMonitor(self.config, self.update_status, self.send_notification)

        # Allow the HTTP handler (POST /streak_event) to raise tray
        # notifications without holding a direct reference to the app.
        set_tray_notifier(self.send_notification)

        # Allow POST /rescue_ack to hand rescue ownership to the extension.
        set_rescue_ack_handler(
            lambda offer_id: bool(self.monitor and self.monitor.acknowledge_rescue(offer_id))
        )

        # Create system tray icon
        self.icon = pystray.Icon(
            "stream_monitor",
            create_icon_image("purple"),
            "Stream Monitor",
            self.create_menu()
        )

        # Auto-start monitoring
        threading.Thread(target=lambda: time.sleep(1) or self.monitor.start(), daemon=True).start()

        # Show welcome page on first run or after update
        if self.config.last_run_version != VERSION:
            self.config.last_run_version = VERSION
            self.config.save()
            # Delay slightly so the config server is ready
            threading.Thread(
                target=lambda: (
                    time.sleep(2),
                    webbrowser.open(
                        f"http://127.0.0.1:{CONFIG_SERVER_PORT}/about?v={VERSION}&welcome=1"
                    ),
                ),
                daemon=True,
            ).start()

        # Check for updates on startup (silently)
        threading.Thread(target=self._startup_update_check, daemon=True).start()
        threading.Thread(target=self._config_watch_loop, daemon=True).start()
        threading.Thread(target=self._usage_ping_loop, daemon=True).start()

        # Run the icon (blocking)
        self.icon.run()
    
    def _show_missed_streak_alert(self, missed: dict[str, str]):
        """Show a dismissible alert about missed streams while paused."""
        import ctypes
        streamer_lines = "\n".join(
            f"  - {name} (went live at {t})" for name, t in missed.items()
        )
        ctypes.windll.user32.MessageBoxW(
            0,
            f"While Stream Monitor was paused, the following streamers went live:\n\n"
            f"{streamer_lines}\n\n"
            f"You may have missed a stream streak!\n"
            f"Consider watching their latest VOD or a clip to keep your streak.",
            "Stream Monitor - Missed Streams",
            0x30  # MB_ICONWARNING
        )

    def _startup_update_check(self):
        """Launch-time update check: prompt to download and install when a
        newer release exists, unless the user muted reminders for exactly
        that version. Dev (non-frozen) runs never prompt or install."""
        time.sleep(5)  # Wait a bit after startup
        update_available, latest_version, release = self.check_for_updates()
        if not update_available:
            return
        self.update_status(f"Update available: v{latest_version}")
        if not getattr(sys, "frozen", False):
            log.info("Update v%s available; prompt skipped in a non-frozen run", latest_version)
            return
        if self.config.skip_update_version == latest_version:
            log.info("Update v%s available but reminders for it are muted", latest_version)
            return
        self._offer_update(latest_version, release)


def _send_usage_ping(install_id: str, version: str) -> bool:
    """POST the anonymous install ping. True on a 2xx. Never raises."""
    try:
        resp = requests.post(
            USAGE_PING_URL,
            json={"install_id": install_id, "version": version, "os": platform.system() or "unknown"},
            timeout=10,
        )
        if 200 <= resp.status_code < 300:
            log.debug("Usage ping sent (v%s)", version)
            return True
        log.debug("Usage ping rejected: HTTP %s", resp.status_code)
        return False
    except requests.RequestException as e:
        log.debug("Usage ping failed: %s", e)
        return False


def _read_config_if_parseable() -> Optional["Config"]:
    """Load the config file only if it currently parses as JSON. The
    settings editor writes the file in place, so a read that lands
    mid-write would come back as a default (empty) Config and, applied,
    would drop every streamer. Callers retry later on None."""
    try:
        json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Config.load()


def _version_parts(value: str) -> list:
    """Leading digits of each dot segment; suffixed segments like '2-pre'
    count as their numeric part, so a stray tag suffix can never crash
    the comparison."""
    parts = []
    for piece in value.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return parts


def _installer_hash_from_sums(sums_text: str) -> Optional[str]:
    """Extract the installer's SHA256 from a release's SHA256SUMS.txt
    (GNU coreutils format: "<hash>  <filename>", comment lines start
    with '#'). Returns the lowercase hex digest or None."""
    for line in sums_text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1] == "StreamMonitorInstaller.exe":
            candidate = parts[0].lower()
            if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate):
                return candidate
    return None


def _http_get_text_with_retry(url: str, attempts: int = 4) -> str:
    """GET text (the checksum file) with a few retries. Covers transient
    network failures and the brief window after a release is published
    where GitHub returns 404 for an asset that is still propagating.
    Raises the last error if every attempt fails."""
    last = None
    for i in range(attempts):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last = e
            log.warning("Fetch attempt %d/%d failed for %s: %s", i + 1, attempts, url, e)
            time.sleep(min(2 ** i, 15))
    raise last


def _download_file_with_retry(url: str, dest, attempts: int = 4) -> str:
    """Download url to dest, returning the sha256 hex digest. Each attempt
    restarts the download from scratch; retries cover transient network
    failures and a just-published asset still propagating on GitHub's CDN.
    Raises the last error if every attempt fails."""
    import hashlib
    last = None
    for i in range(attempts):
        try:
            digest = hashlib.sha256()
            with requests.get(url, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(1024 * 1024):
                        f.write(chunk)
                        digest.update(chunk)
            return digest.hexdigest()
        except requests.RequestException as e:
            last = e
            log.warning("Download attempt %d/%d failed for %s: %s", i + 1, attempts, url, e)
            time.sleep(min(2 ** i, 15))
    raise last


def run_update_dialog(current_version: str, latest_version: str) -> int:
    """The update prompt. Runs in its OWN process (main() dispatches on
    --update-dialog): pystray and tkinter cannot share the tray process
    without breaking keyboard input, the same reason the settings editor
    is a separate process.

    Exit codes: 10 install now, 11 not right now, 12 not right now and
    do not remind again for this version."""
    import tkinter as tk
    from tkinter import ttk

    result = {"code": 11}
    root = tk.Tk()
    root.title("Stream Monitor Update")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text="New update available", font=("", 12, "bold")).pack(anchor=tk.W)
    ttk.Label(
        frame,
        text=(
            f"Stream Monitor v{latest_version} is available (you have v{current_version}).\n"
            "Would you like to install it? The app will restart itself when it finishes."
        ),
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(8, 12))

    remind_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        frame, text="Do not remind me about this version", variable=remind_var
    ).pack(anchor=tk.W)

    def choose(code: int):
        result["code"] = code
        root.destroy()

    def not_now():
        choose(12 if remind_var.get() else 11)

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X, pady=(14, 0))
    ttk.Button(buttons, text="Yes", width=12, command=lambda: choose(10)).pack(
        side=tk.RIGHT, padx=(8, 0)
    )
    ttk.Button(buttons, text="Not right now", width=14, command=not_now).pack(side=tk.RIGHT)
    root.protocol("WM_DELETE_WINDOW", not_now)

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result["code"]


def main():
    # Dialog mode: spawned by the tray process (which cannot host tkinter
    # next to pystray). Must be handled before any tray/server startup so
    # the single-instance port is never touched.
    if len(sys.argv) >= 2 and sys.argv[1] == "--update-dialog":
        current = sys.argv[2] if len(sys.argv) > 2 else VERSION
        latest = sys.argv[3] if len(sys.argv) > 3 else ""
        sys.exit(run_update_dialog(current, latest))
    app = StreamMonitorApp()
    app.run()


if __name__ == "__main__":
    main()
