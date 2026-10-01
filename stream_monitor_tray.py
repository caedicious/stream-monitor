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
from dataclasses import dataclass, asdict, field
from urllib.parse import quote as url_quote
from typing import Optional, Callable, NamedTuple
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

import certifi
import shutil
import requests
import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

# Slot mode and automatic streak saves (1.12.0). Pure modules: this file
# does their I/O, threads and logging. Static imports, so PyInstaller
# bundles them.
import slot_scheduler
import streak_saves

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
VERSION = "1.12.0"
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

# Slot mode (1.12.0). The scheduler re-evaluates at least this often between
# polls, so turns end on time and the published plan stays fresh (the
# extension treats a plan older than 300 s as inactive).
SLOT_TICK_MAX_GAP_SECONDS = 30
# How long an HTTP handler waits for the monitor thread to replan after a
# streak event or a tab report that changes the plan (a gone entry, a busy
# change), before answering anyway.
SLOT_REPLAN_WAIT_SECONDS = 2.0
# slot_state.json is rewritten at least this often while the monitor ticks
# (and when a running monitor stops), not only on a change, so its saved_at
# measures how long the desktop was down: a quick restart then keeps the
# slots and the executor (DESIGN 6.1, rule 38).
SLOT_STATE_REFRESH_SECONDS = 300
# Failed polls for longer than max(3 x check_interval, this) end the run of
# unbroken watching the card verdict relies on (_watch_start).
WATCH_GAP_MIN_SECONDS = 180
# Helix accepts at most 100 user_login parameters per request.
HELIX_MAX_LOGINS = 100
# Streak cards seen this run are remembered this long after their last
# sighting (the per-run dedup).
STREAK_DEDUP_TTL_SECONDS = 48 * 3600
# A claimant that acked a rescue offer gets 204 again for this long when it
# re-sends the ack (its first answer may have been lost).
RESCUE_REACK_WINDOW_SECONDS = 600
LAST_ACKED_OFFER_TTL_SECONDS = 24 * 3600
# On Windows a reader that holds a file open (antivirus, the search indexer,
# a backup tool) makes os.replace onto it fail with PermissionError
# (WinError 5) until it lets go. Atomic writes retry that for this long in
# all, sleeping 50 ms at first and doubling up to 250 ms between tries.
# The startup log scrub (scrub_secrets_from_logs) is the one os.replace left
# outside the retry: it runs at import, where a retry would hold up startup
# by up to REPLACE_RETRY_SECONDS for each of the 4 log files, and a log that
# another running instance holds open stays refused for as long as it runs.
# The scrub skips a file it cannot replace, which is still redacted when
# served.
REPLACE_RETRY_SECONDS = 2.0
REPLACE_RETRY_FIRST_DELAY = 0.05
REPLACE_RETRY_MAX_DELAY = 0.25

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


# ---------------------------------------------------------------------------
# Secret redaction
#
# The Twitch app credentials used to travel in the token request's URL, and
# requests puts the full URL in HTTPError and ConnectionError text, so a
# failed token request could write the client secret into the debug log,
# which the desktop serves at /debug.log and /debug.log.json and users share
# in issues. The request now sends them in the form body. These patterns are
# the safety net for any secret that still reaches a log line, and they also
# clean lines written before the fix.
# ---------------------------------------------------------------------------
REDACTED = "REDACTED"
_SECRET_KEYS = "client_secret|access_token|refresh_token"
# No pattern crosses a line break: the filter sees one record at a time, and
# the served log and the startup scrub work line by line, so all of them
# agree and a rewrite can never merge or drop lines.
_SECRET_PATTERNS = [
    # client_secret=abc, access_token: abc, refresh_token='abc' and the
    # URL-encoded client_secret%3Dabc, in URLs, form bodies, reprs and text.
    (re.compile(r"(?i)\b(" + _SECRET_KEYS + r")([ \t]*(?:=|%3D|:)[ \t]*)(\\?[\"']?)[^&\s\"'<>\\,;)}\]]+"),
     r"\1\2\3" + REDACTED),
    # "client_secret": "abc" in JSON (also escaped inside a JSON string),
    # 'client_secret': 'abc' in Python reprs.
    (re.compile(r"(?i)(\\?[\"'](?:" + _SECRET_KEYS + r")\\?[\"'][ \t]*:[ \t]*\\?[\"'])[^\"'\\\r\n]*"),
     r"\1" + REDACTED),
    # Authorization: Bearer <token> and OAuth <token>, only when a token-shaped
    # value follows (Twitch app tokens are 30 characters), so "Bearer of the
    # Curse" in a stream title stays as it is.
    (re.compile(r"(?i)\b(Bearer|OAuth)[ \t]+(?=[A-Za-z0-9._~+/=-]{20,})[A-Za-z0-9._~+/=-]+"),
     r"\1 " + REDACTED),
]
# Live secret values (the client secret, the current app token), removed
# wherever they appear, including where no key names them: a bare log
# argument, a repr, a traceback. Values under 16 characters are not kept,
# since they could match ordinary text.
_SECRET_VALUES_MAX = 8
_secret_values: list = []
_secret_values_lock = threading.Lock()


def register_secret(value) -> None:
    """Remember a live secret value for redact_secrets (see above)."""
    if not isinstance(value, str) or len(value) < 16:
        return
    with _secret_values_lock:
        if value in _secret_values:
            return
        _secret_values.append(value)
        del _secret_values[:-_SECRET_VALUES_MAX]


def redact_secrets(text: str) -> str:
    """Replace the live secret values, and any client secret, access or
    refresh token, or bearer token found by the patterns above, with
    REDACTED. Idempotent."""
    with _secret_values_lock:
        values = list(_secret_values)
    for value in values:
        for form in (value, url_quote(value, safe="")):
            text = text.replace(form, REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_bytes(data: bytes) -> bytes:
    """redact_secrets for file contents, line by line (see above).
    surrogateescape keeps any bytes that are not valid UTF-8 exactly as they
    were."""
    text = data.decode("utf-8", "surrogateescape")
    return "".join(redact_secrets(line) for line in text.splitlines(keepends=True)).encode(
        "utf-8", "surrogateescape")


def _redact_value(value, key=None):
    """redact_secrets applied to a structure before it is serialized: a
    value under a secret-named key becomes REDACTED, strings are redacted,
    dicts and lists are walked."""
    if key is not None and re.fullmatch(r"(?i)" + _SECRET_KEYS, str(key)):
        return REDACTED
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _token_failure_summary(error: Exception) -> str:
    """"HTTPError (HTTP 400)" or "ConnectionError": the exception type and,
    when Twitch answered, the HTTP status. Never the exception text, which
    for requests includes the request URL."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return f"{type(error).__name__} (HTTP {status})" if status else type(error).__name__


class SecretRedactingFilter(logging.Filter):
    """Handler filter: rewrites a record's message, traceback text and stack
    text with redact_secrets before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace(src, dst) for an atomic write, retried on PermissionError
    until REPLACE_RETRY_SECONDS have passed on the wall clock or in its own
    sleeps, whichever comes first. When it gives up, or on any other
    OSError, it tries once to remove src and raises that error, so the
    caller keeps its own give-up path. That removal is best effort: a src
    that a scanner holds open refuses it too, and stays until the next
    write of that name overwrites it. Holds no lock of its own: a caller
    that holds one keeps it through the retries, and callers that share a
    src name must hold one, or a give-up removes another writer's src."""
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    slept = 0.0
    delay = REPLACE_RETRY_FIRST_DELAY
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            left = min(deadline - time.monotonic(), REPLACE_RETRY_SECONDS - slept)
            if left <= 0:
                _discard_temp_file(src)
                raise
            pause = min(delay, left)
            time.sleep(pause)
            slept += pause
            delay = min(delay * 2, REPLACE_RETRY_MAX_DELAY)
        except OSError:
            _discard_temp_file(src)
            raise


def _discard_temp_file(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def scrub_secrets_from_logs(log_file: Optional[Path] = None) -> list:
    """Redact secrets in the debug log and its rotations (stream_monitor.log,
    stream_monitor.log.1 and so on) once, at startup. It runs before the file
    handler opens the log, because Windows cannot replace a file that is
    open. A file is rewritten only when it holds a match, through a temporary
    file that is flushed to disk before os.replace, so an interruption or a
    power loss never loses it. Never raises (it runs at import, and the app
    must start regardless): a file it cannot rewrite is still redacted when
    served. Returns the paths it rewrote."""
    base = LOG_FILE if log_file is None else log_file
    name_re = re.compile(re.escape(base.name) + r"(\.\d+)?")
    rewritten = []
    try:
        candidates = sorted(p for p in base.parent.iterdir() if name_re.fullmatch(p.name))
    except Exception:
        return rewritten
    for path in candidates:
        tmp = path.with_name("." + path.name + ".scrub-tmp")
        try:
            data = path.read_bytes()
            cleaned = _redact_bytes(data)
            if cleaned == data:
                continue
            with open(tmp, "wb") as f:
                f.write(cleaned)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            rewritten.append(path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
    return rewritten


def setup_logging():
    """Set up file logging with rotation."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    scrubbed = scrub_secrets_from_logs(LOG_FILE)

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
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)
    if scrubbed:
        logger.info("Redacted secrets in %d earlier log file(s): %s",
                    len(scrubbed), ", ".join(p.name for p in scrubbed))

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Activity log (separate from the debug log). Structured, append-only JSONL.
# ---------------------------------------------------------------------------

_activity_lock = threading.Lock()


def _activity_timestamp() -> str:
    """ISO 8601 UTC with millisecond precision, e.g. 2026-04-17T18:30:05.123Z.

    The seconds and the milliseconds come from the same clock reading.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def log_activity(event: str, **fields):
    """Append a structured event to the activity log (JSONL, append-only).

    Wrapped in a try/except: a failure here must never crash the monitor.
    """
    try:
        # Error text is stored verbatim (api_error), so apply the same safety
        # net as the debug log (/activity.json serves this file). Values are
        # redacted before serializing, so a line always stays valid JSON.
        record = {"ts": _activity_timestamp(), "event": event, **_redact_value(fields)}
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
# version). If the extension is demonstrably alive but the ack has not
# arrived (transient POST failure, an extension-side bug), flushing at 180s
# floods the browser for nothing. Only a guarded POST proves the extension
# is alive: a stored /open_tabs report (1.10 and newer send one after every
# config fetch), a parsed /rescue_ack or a valid /streak_event, each past
# the Origin and Content-Type guard. A GET /config does not, because any web
# page the browser lets reach loopback can send that GET. While guarded
# POSTs keep arriving the offer stays published until the hard deadline
# below; the blind 180s fallback applies when none has arrived recently.
RESCUE_ACK_HARD_TIMEOUT_SECONDS = 600
EXTENSION_ALIVE_WINDOW_SECONDS = 150

_extension_last_seen_monotonic: Optional[float] = None
_extension_seen_lock = threading.Lock()


def note_extension_contact() -> None:
    """Record that the browser extension just made a guarded POST (a stored
    /open_tabs report, a parsed /rescue_ack or a valid /streak_event). A
    GET /config never calls this: any web page can send that GET."""
    global _extension_last_seen_monotonic
    with _extension_seen_lock:
        _extension_last_seen_monotonic = time.monotonic()


def extension_seen_within(seconds: float) -> bool:
    """True if the extension made a guarded POST within the last `seconds`
    seconds (see note_extension_contact); a /config poll does not count."""
    with _extension_seen_lock:
        last = _extension_last_seen_monotonic
    return last is not None and (time.monotonic() - last) <= seconds


_rescue_ack_handler: Optional[Callable[[str], bool]] = None
# 1.12.0: POST /rescue_ack calls this with (offer_id, claimant) when set,
# else the one-argument handler above. The claimant is "<browser>-<instance>"
# from the ack body, or None.
_rescue_claim_handler: Optional[Callable[[str, Optional[str]], bool]] = None


def set_rescue_claim_handler(fn: Optional[Callable[[str, Optional[str]], bool]]) -> None:
    """Register the two-argument /rescue_ack handler (offer id, claimant)."""
    global _rescue_claim_handler
    _rescue_claim_handler = fn


def _rescue_claimant(payload: dict) -> Optional[str]:
    """"<browser>-<instance>" from a /rescue_ack body when both are valid,
    else None. Only a named claimant can re-ack an offer (A11)."""
    browser = payload.get("browser")
    instance = payload.get("instance")
    if not isinstance(browser, str) or not isinstance(instance, str):
        return None
    browser = browser.strip().lower()
    instance = instance.strip().lower()
    if not _OPEN_TABS_BROWSER_RE.match(browser) or not _OPEN_TABS_INSTANCE_RE.match(instance):
        return None
    return f"{browser}-{instance}"


# ---------------------------------------------------------------------------
# The monitor inbox (1.12.0).
#
# HTTP handler threads never touch the scheduler, the save items or, in Slot
# mode, queued_vods. They put an item on the monitor's inbox through the
# registered submitter and, when the monitor loop is running, wait up to
# SLOT_REPLAN_WAIT_SECONDS for it to drain the inbox and replan, so the
# extension's answer already reflects the new plan. A submit always enqueues;
# with the monitor stopped the item waits for the next start.
# ---------------------------------------------------------------------------


class _InboxItem:
    """One inbox entry: kind "streak_item", "item_done" or "report", its
    payload, and the event the monitor sets once it has processed the item
    and replanned. result is True when the item created, merged or completed
    something."""

    __slots__ = ("kind", "payload", "done", "result", "waitable")

    def __init__(self, kind: str, payload: dict, waitable: bool = False):
        self.kind = kind
        self.payload = payload
        self.done = threading.Event()
        self.result: Optional[bool] = None
        self.waitable = waitable


_monitor_submitter: Optional[Callable[[str, dict], Optional[_InboxItem]]] = None
# Returns the epoch at which the current unbroken run of successful polls
# that included a login began, or None (TwitchMonitor._watch_start).
_watch_start_provider: Optional[Callable[[str], Optional[float]]] = None


def set_monitor_submitter(fn: Optional[Callable[[str, dict], Optional[_InboxItem]]]) -> None:
    """Register the callable HTTP handlers use to reach the monitor inbox."""
    global _monitor_submitter
    _monitor_submitter = fn


def set_watch_start_provider(fn: Optional[Callable[[str], Optional[float]]]) -> None:
    """Register the callable the card verdict reads _watch_start through."""
    global _watch_start_provider
    _watch_start_provider = fn


def _watch_start_for(name: str) -> Optional[float]:
    provider = _watch_start_provider
    if provider is None:
        return None
    try:
        value = provider(name)
    except Exception as e:
        log.warning("Watch-start provider raised: %s", e)
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _submit_to_monitor(kind: str, payload: dict) -> Optional[_InboxItem]:
    """Put an item on the monitor inbox. Waits up to
    SLOT_REPLAN_WAIT_SECONDS when the monitor loop is running with a current
    loop generation (A6); otherwise answers at once. None when no monitor is
    registered. Never called with a lock held (A34)."""
    submitter = _monitor_submitter
    if submitter is None:
        return None
    try:
        item = submitter(kind, payload)
    except Exception as e:
        log.warning("Monitor submitter raised on %s: %s", kind, e)
        return None
    if item is not None and item.waitable:
        item.done.wait(SLOT_REPLAN_WAIT_SECONDS)
    return item


def _monitor_changed(item: Optional[_InboxItem]) -> bool:
    """Whether the monitor reported, within the wait, that the item created,
    merged or completed something."""
    return item is not None and item.done.is_set() and item.result is True


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
# 1.12.0 report fields: a per-profile instance id, the plan seq the
# extension applied (its presence marks a Slot-mode-capable extension), the
# slot tabs that went away since the last report, and whether the profile
# is paused.
_OPEN_TABS_INSTANCE_RE = re.compile(r"^[0-9a-f]{8}$")
OPEN_TABS_PLAN_SEQ_MAX = 2147483647
OPEN_TABS_GONE_MAX = 100
OPEN_TABS_GONE_REASONS = frozenset(slot_scheduler.GONE_REASONS)
# Bounds for the gone queue while the monitor is not draining it, and for
# the memory that drops a re-sent gone entry.
_OPEN_TABS_GONE_QUEUE_MAX = 1000
_OPEN_TABS_GONE_MEMORY_SECONDS = 24 * 3600
_open_tabs_lock = threading.Lock()
# Held through the extension_tabs.json write and its replace retries. POST
# /open_tabs handler threads write it at the same time through one temp
# file. Taken after _open_tabs_lock is released, never inside it.
_open_tabs_persist_lock = threading.Lock()
# key -> {"streamers": frozenset[str], "epoch": float, "mono": float,
#         "browser": str, "instance": str or None, "plan_seq": int or None,
#         "busy": "paused" or None}. The key is the browser, or
# "<browser>-<instance>" when the report carries a valid instance.
_open_tabs_reports: dict[str, dict] = {}
# Valid gone entries not yet drained by the monitor thread:
# [{"key", "streamer", "reason", "at"}], and the (key, streamer, reason, at)
# tuples already queued, with the time each was queued.
_open_tabs_gone: list = []
_open_tabs_gone_keys: dict = {}


def _extension_tabs_path() -> Path:
    return CONFIG_DIR / "extension_tabs.json"


def _valid_gone_entries(gone) -> list:
    """The valid entries of a report's gone list (plan 3.4); a bad entry is
    dropped and a non-list is ignored."""
    if not isinstance(gone, list):
        return []
    out = []
    for raw in gone[:OPEN_TABS_GONE_MAX]:
        if not isinstance(raw, dict):
            continue
        streamer = raw.get("streamer")
        reason = raw.get("reason")
        at = raw.get("at")
        if not isinstance(streamer, str):
            continue
        streamer = streamer.strip().lower()
        if not _OPEN_TABS_LOGIN_RE.match(streamer):
            continue
        if reason not in OPEN_TABS_GONE_REASONS:
            continue
        if not isinstance(at, int) or isinstance(at, bool):
            continue
        out.append({"streamer": streamer, "reason": reason, "at": at})
    return out


def _record_open_tabs(browser, streamers, reason: str = "", *,
                      now_epoch: Optional[float] = None,
                      now_mono: Optional[float] = None,
                      instance=None, plan_seq=None, gone=None, busy=None) -> tuple:
    """Store one report. Returns (stored, replan): stored is False (nothing
    stored) for a malformed payload; replan is True when the report carried
    a valid gone entry or its busy state differs from the previous report
    under the same key, so the monitor should replan before the answer."""
    if not isinstance(browser, str) or not isinstance(streamers, list):
        return False, False
    browser = browser.strip().lower()
    if not _OPEN_TABS_BROWSER_RE.match(browser):
        return False, False
    names = set()
    for raw in streamers[:OPEN_TABS_MAX_STREAMERS]:
        if isinstance(raw, str):
            name = raw.strip().lower()
            if _OPEN_TABS_LOGIN_RE.match(name):
                names.add(name)
    inst = instance.strip().lower() if isinstance(instance, str) else None
    if inst is not None and not _OPEN_TABS_INSTANCE_RE.match(inst):
        inst = None
    seq = plan_seq if (isinstance(plan_seq, int) and not isinstance(plan_seq, bool)
                       and 0 <= plan_seq <= OPEN_TABS_PLAN_SEQ_MAX) else None
    busy_value = "paused" if busy == "paused" else None
    key = f"{browser}-{inst}" if inst else browser
    entries = _valid_gone_entries(gone)
    epoch = time.time() if now_epoch is None else now_epoch
    mono = time.monotonic() if now_mono is None else now_mono
    report = {"streamers": frozenset(names), "epoch": epoch, "mono": mono,
              "browser": browser, "instance": inst, "plan_seq": seq, "busy": busy_value}
    with _open_tabs_lock:
        previous = _open_tabs_reports.get(key)
        _open_tabs_reports[key] = report
        snapshot = {b: dict(rep) for b, rep in _open_tabs_reports.items()}
        _queue_gone_entries_locked(key, entries)
    if previous is None or previous["streamers"] != frozenset(names):
        log.info("Open-tabs report from %s (%s): %s", key, reason or "update",
                 ", ".join(sorted(names)) or "none")
    for entry in entries:
        log.info("Open-tabs report from %s: %s gone (%s)", key, entry["streamer"], entry["reason"])
    previous_busy = previous.get("busy") if previous is not None else None
    _persist_open_tabs_reports(snapshot)
    return True, bool(entries) or previous_busy != busy_value


def record_extension_open_tabs(browser, streamers, reason: str = "", *,
                               now_epoch: Optional[float] = None,
                               now_mono: Optional[float] = None,
                               instance=None, plan_seq=None, gone=None, busy=None) -> bool:
    """Store one report. Returns False (storing nothing) for a malformed
    payload; malformed streamer names inside a good payload are dropped, and
    so are malformed optional 1.12 fields (instance, plan_seq, gone, busy)."""
    stored, _ = _record_open_tabs(browser, streamers, reason, now_epoch=now_epoch,
                                  now_mono=now_mono, instance=instance, plan_seq=plan_seq,
                                  gone=gone, busy=busy)
    return stored


def _queue_gone_entries_locked(key: str, entries: list) -> None:
    """Append valid gone entries for the monitor, dropping one already
    queued with the same (key, streamer, reason, at). The caller holds
    _open_tabs_lock."""
    now = time.time()
    for stale in [k for k, t in _open_tabs_gone_keys.items()
                  if now - t > _OPEN_TABS_GONE_MEMORY_SECONDS]:
        del _open_tabs_gone_keys[stale]
    for entry in entries:
        ident = (key, entry["streamer"], entry["reason"], entry["at"])
        if ident in _open_tabs_gone_keys:
            continue
        _open_tabs_gone_keys[ident] = now
        _open_tabs_gone.append(dict(entry, key=key))
    if len(_open_tabs_gone) > _OPEN_TABS_GONE_QUEUE_MAX:
        del _open_tabs_gone[:len(_open_tabs_gone) - _OPEN_TABS_GONE_QUEUE_MAX]


def drain_open_tabs_gone() -> list:
    """The gone entries reported since the last drain (monitor thread)."""
    with _open_tabs_lock:
        entries = list(_open_tabs_gone)
        del _open_tabs_gone[:]
    return entries


def _persist_open_tabs_reports(snapshot: dict[str, dict]) -> None:
    """Mirror the reports to disk (best effort, atomic replace) so the next
    process can seed its start from them. plan_seq is written only when the
    report carried one. Report threads run this at the same time, so the
    write and the replace retries hold _open_tabs_persist_lock: a writer
    that gives up removes only its own temp file, never a newer report's."""
    path = _extension_tabs_path()
    data = {}
    for b, rep in snapshot.items():
        entry = {"ts": rep["epoch"], "streamers": sorted(rep["streamers"])}
        if rep.get("plan_seq") is not None:
            entry["plan_seq"] = rep["plan_seq"]
        data[b] = entry
    with _open_tabs_persist_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            _replace_with_retry(tmp, path)
        except OSError as e:
            log.debug("Could not persist the open-tabs report: %s", e)


def extension_open_tabs_snapshot() -> dict[str, dict]:
    """Copy of the latest report per key received by this process."""
    with _open_tabs_lock:
        return {b: dict(rep) for b, rep in _open_tabs_reports.items()}


def persisted_capable_report_within(max_age_seconds: float,
                                    now_epoch: Optional[float] = None) -> bool:
    """Whether extension_tabs.json holds a report at most max_age_seconds
    old that carried a plan_seq (a Slot-mode-capable extension, A24)."""
    try:
        data = json.loads(_extension_tabs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    now = time.time() if now_epoch is None else now_epoch
    for rep in data.values():
        if not isinstance(rep, dict):
            continue
        ts = rep.get("ts")
        seq = rep.get("plan_seq")
        if (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and isinstance(seq, int) and not isinstance(seq, bool)
                and now - ts <= max_age_seconds):
            return True
    return False


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
# so the user still has a record. A recorded "already saved" also forgets
# the card keys it covers (see _handle_already_saved).
#
# 1.12.0 (card identity, plan 3.5.3): a key stands for one card only while
# the cards read under it can be the same card. _streak_event_seen_at keeps
# a record {seen_at, break_at, unit, deadline_at} per key; a card whose
# possible posting time lies after the record's is a newer card and is
# announced again. Records drive the 48-hour prune, counted from the last
# sighting. _streak_item_keys holds the card that produced a save item this
# run, with the latest escalated deadline, so a card whose item is done
# never makes a second one. Its records have no clock of their own: they go
# only with their card's seen record (the prune), at a go-live, when a save
# covers them, or when a card ends the save. Link events use
# ("broke", login, None).
_streak_event_seen: set = set()
_streak_event_seen_at: dict = {}
_streak_item_keys: dict = {}
_streak_event_lock = threading.Lock()
_CARD_STATUSES = ("broke", "in_danger")


def _prune_streak_dedup_locked(now: float) -> None:
    """Forget card keys last seen more than STREAK_DEDUP_TTL_SECONDS ago,
    with their item records. An item record is never pruned on its own: its
    seen_at is the time the item was made, and a card still being read
    would then make a second item (plan 3.5.3). The caller holds
    _streak_event_lock."""
    cutoff = now - STREAK_DEDUP_TTL_SECONDS
    for key in [k for k, rec in _streak_event_seen_at.items() if rec.get("seen_at", now) < cutoff]:
        _streak_event_seen_at.pop(key, None)
        _streak_event_seen.discard(key)
        _streak_item_keys.pop(key, None)


def _forget_card_keys_locked(name: str, keep=None) -> None:
    """Forget a streamer's card keys (not the already_saved toast key), all
    but `keep`. The caller holds _streak_event_lock."""
    for store in (_streak_event_seen, _streak_event_seen_at, _streak_item_keys):
        for key in [k for k in store
                    if k != keep and k[0] in _CARD_STATUSES and k[1] == name]:
            if isinstance(store, set):
                store.discard(key)
            else:
                store.pop(key, None)


def _announce_card_locked(key: tuple, record: dict, now: float) -> bool:
    """Whether a card was already announced this run under its key (the
    incoming card is the same card as the key's record, or an older one; a
    key with no record counts as the same card). Adds the key; a newer card
    replaces the record, the same or an older one refreshes its sighting and
    takes an escalated deadline. The caller holds _streak_event_lock."""
    existing = _streak_event_seen_at.get(key)
    announced = False
    relation = None
    if key in _streak_event_seen:
        relation = streak_saves.card_relation(existing, record) if existing else "same"
        announced = relation in ("same", "older")
    _streak_event_seen.add(key)
    if existing is None or not announced:
        _streak_event_seen_at[key] = dict(record)
    else:
        if streak_saves.is_escalation(existing, record):
            existing["deadline_at"] = record["deadline_at"]
        existing["seen_at"] = now
    return announced


# ---------------------------------------------------------------------------
# Saved-streak memory (v1.11.2).
#
# Twitch keeps a "your N-stream streak on X broke" card in the bell inbox
# after the streak has already been kept, and the card's own "Save your
# streak" link then lands on "No Content Eligible: You've already maintained
# your N-stream streak with X". The extension reports that page as an
# "already_saved" streak event, stamped with the moment the page was seen.
# While the save counts, later "broke" and "in danger" cards for X are
# logged but not notified, and no save-streak tab is queued, flushed or
# offered for rescue for X.
#
# A save stops counting at the first of:
#   - X goes live (Helix started_at) after it: a new broadcast is a new
#     chance to break the streak;
#   - the broadcast already running when it was seen ends, if Stream
#     Monitor did not have it open (skipped for a pause, or over before a
#     fresh start looked): Twitch's answer could only speak for the
#     broadcasts before that one, and the owner missed this one. A
#     broadcast it had open keeps the save until the next go-live;
#   - a card for X with a higher count arrives: the streak grew since the
#     save, so the card is about a newer break; or (1.12.0) a card whose
#     age shows it was posted after the save (the card verdict,
#     streak_saves.card_verdict);
#   - 24 hours pass when the monitor does not poll X (no go-live of X can
#     ever be seen), or 7 days pass in any case (bounds a broadcast the
#     desktop missed while it was off).
# Persisted in streak_state.json next to config.json so a restart does not
# forget.
# ---------------------------------------------------------------------------
STREAK_STATE_MAX_AGE_SECONDS = 30 * 24 * 3600
SAVED_STREAK_UNPOLLED_TTL_SECONDS = 24 * 3600
SAVED_STREAK_MAX_AGE_SECONDS = 7 * 24 * 3600
# A stored save dated further ahead than this is distrusted: the clock was
# stepped back since it was recorded, so its real time is unknown.
STREAK_CLOCK_SKEW_SECONDS = 10 * 60
_streak_state_lock = threading.Lock()


def _empty_streak_state() -> dict:
    # "saved": {streamer: {"at": ISO 8601, "count": int}}. "last_live" and
    # "last_offline": {streamer: ISO 8601}, the latest broadcast start and
    # the latest broadcast end the monitor observed. "missed_end": the
    # latest end of a broadcast Stream Monitor did not have open; only that
    # end also ends a save seen during the broadcast.
    return {"saved": {}, "last_live": {}, "last_offline": {}, "missed_end": {}}


_streak_state: dict = _empty_streak_state()
# Logins the monitor polls on Helix (set when it starts). A save for any
# other login can never be ended by a go-live, so it only lasts 24 hours.
_polled_streamers: frozenset = frozenset()


def _streak_clock() -> float:
    """Wall-clock seconds for the saved-streak rules (tests pin it)."""
    return time.time()


def _epoch_to_iso(epoch: float) -> str:
    """ISO 8601 UTC with milliseconds, the shape _activity_timestamp uses."""
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _streak_state_path() -> Path:
    return CONFIG_DIR / "streak_state.json"


def _iso_to_epoch(value) -> Optional[float]:
    """Epoch seconds for an ISO 8601 timestamp (Z or offset), else None.
    One without an offset is read as UTC: local-time conversion raises
    OSError on Windows for early dates, and a hand-edited state file must
    not stop the app from starting."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def load_streak_state(now_epoch: Optional[float] = None) -> None:
    """Read streak_state.json into memory, dropping malformed entries,
    entries older than STREAK_STATE_MAX_AGE_SECONDS, and saves dated more
    than STREAK_CLOCK_SKEW_SECONDS ahead of now. A broadcast time ahead of
    now is pulled back to now, so it cannot hide every later save. A missing
    or unreadable file means an empty state."""
    global _streak_state
    now = _streak_clock() if now_epoch is None else now_epoch
    try:
        raw = json.loads(_streak_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cutoff = now - STREAK_STATE_MAX_AGE_SECONDS
    state = _empty_streak_state()
    raw_saved = raw.get("saved") if isinstance(raw.get("saved"), dict) else {}
    for name, entry in raw_saved.items():
        if not (isinstance(name, str) and _OPEN_TABS_LOGIN_RE.match(name) and isinstance(entry, dict)):
            continue
        at = _iso_to_epoch(entry.get("at"))
        if at is None or at < cutoff or at > now + STREAK_CLOCK_SKEW_SECONDS:
            continue
        count = entry.get("count")
        state["saved"][name] = {"at": entry["at"], "count": count if isinstance(count, int) else 0}
    for key in ("last_live", "last_offline", "missed_end"):
        raw_times = raw.get(key) if isinstance(raw.get(key), dict) else {}
        for name, at_iso in raw_times.items():
            if not (isinstance(name, str) and _OPEN_TABS_LOGIN_RE.match(name)):
                continue
            at = _iso_to_epoch(at_iso)
            if at is None or at < cutoff:
                continue
            state[key][name] = at_iso if at <= now else _epoch_to_iso(now)
    with _streak_state_lock:
        _streak_state = state
        _publish_saved_streaks_locked(now)


def _prune_streak_state_locked(now: float) -> None:
    """Drop entries older than STREAK_STATE_MAX_AGE_SECONDS from the
    in-memory state, the rule load_streak_state applies at start, so the
    file stays bounded in a process that runs for weeks. The caller holds
    _streak_state_lock."""
    cutoff = now - STREAK_STATE_MAX_AGE_SECONDS
    for name, entry in list(_streak_state["saved"].items()):
        at = _iso_to_epoch(entry.get("at")) if isinstance(entry, dict) else None
        if at is None or at < cutoff:
            del _streak_state["saved"][name]
    for key in ("last_live", "last_offline", "missed_end"):
        for name, at_iso in list(_streak_state[key].items()):
            at = _iso_to_epoch(at_iso)
            if at is None or at < cutoff:
                del _streak_state[key][name]


def _write_streak_state_locked() -> None:
    """Persist the in-memory state, after the load-time prune. The caller
    holds _streak_state_lock. Never raises: losing this file only costs a
    stale notification."""
    _prune_streak_state_locked(_streak_clock())
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = _streak_state_path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_streak_state, indent=2), encoding="utf-8")
        _replace_with_retry(tmp, path)
    except OSError as e:
        log.warning("Could not write %s: %s", _streak_state_path(), e)


def _broadcast_boundary_locked(name: str) -> Optional[float]:
    """Epoch of the latest broadcast start, or end of a missed broadcast, on
    record for the streamer: a save seen at or before it no longer counts.
    The caller holds _streak_state_lock."""
    times = [
        t for t in (_iso_to_epoch(_streak_state["last_live"].get(name)),
                    _iso_to_epoch(_streak_state["missed_end"].get(name)))
        if t is not None
    ]
    return max(times) if times else None


def _save_counts_locked(name: str, entry: dict, now: float) -> bool:
    """Whether a save still counts at `now`, under the rules in the block
    comment above. The caller holds _streak_state_lock."""
    saved_at = _iso_to_epoch(entry.get("at"))
    if saved_at is None or saved_at > now + STREAK_CLOCK_SKEW_SECONDS:
        return False
    age = now - saved_at
    if age > SAVED_STREAK_MAX_AGE_SECONDS:
        return False
    if age > SAVED_STREAK_UNPOLLED_TTL_SECONDS and name not in _polled_streamers:
        return False
    boundary = _broadcast_boundary_locked(name)
    return boundary is None or boundary < saved_at


def _saved_streaks_locked(now: float) -> dict:
    return {
        name: entry["at"]
        for name, entry in _streak_state["saved"].items()
        if _save_counts_locked(name, entry, now)
    }


def _publish_saved_streaks_locked(now: Optional[float] = None) -> None:
    """Put the saves that count in /config. Every publish happens under
    _streak_state_lock, so publishes from the HTTP threads and the monitor
    thread land in the order the state changed and an older snapshot can
    never overwrite a newer one."""
    ConfigRequestHandler.config_data["saved_streaks"] = _saved_streaks_locked(
        _streak_clock() if now is None else now
    )


def publish_saved_streaks() -> None:
    """Refresh /config's saved_streaks; saves also run out with time."""
    with _streak_state_lock:
        _publish_saved_streaks_locked()


def saved_streaks_for_config(now_epoch: Optional[float] = None) -> dict:
    """{streamer: saved_at} for every save that counts right now, as /config
    publishes it to the extension."""
    with _streak_state_lock:
        return _saved_streaks_locked(_streak_clock() if now_epoch is None else now_epoch)


def set_polled_streamers(names) -> None:
    """Record the logins the monitor polls on Helix (see _polled_streamers)."""
    global _polled_streamers
    polled = frozenset(n.lower() for n in names if isinstance(n, str))
    with _streak_state_lock:
        _polled_streamers = polled
        _publish_saved_streaks_locked()


def record_stream_live(streamer: str, at: Optional[str] = None, *,
                       fallback_to_now: bool = True,
                       now_epoch: Optional[float] = None) -> bool:
    """Remember when a streamer's broadcast started: Helix started_at, or
    now when that is missing or implausible (in the future, or older than
    this state is ever kept). With fallback_to_now=False such a start
    records nothing: a poll of a stream that stayed live cannot tell when
    a new broadcast would have begun. A save seen before the start stops
    counting. A relaunch, a tray Start or a one-poll gap re-records the
    same start, so a save seen during the broadcast survives them; an end
    recorded at or after the start was that gap, not a real end, and is
    dropped. Returns True when the start on record changed."""
    name = streamer.lower()
    if not _OPEN_TABS_LOGIN_RE.match(name):
        return False
    now = _streak_clock() if now_epoch is None else now_epoch
    live_epoch = _iso_to_epoch(at)
    if live_epoch is None or not now - STREAK_STATE_MAX_AGE_SECONDS <= live_epoch <= now:
        if not fallback_to_now:
            return False
        at = _epoch_to_iso(now)
        live_epoch = _iso_to_epoch(at)
    with _streak_state_lock:
        new_start = _streak_state["last_live"].get(name) != at
        changed = new_start
        _streak_state["last_live"][name] = at
        for key in ("last_offline", "missed_end"):
            end = _iso_to_epoch(_streak_state[key].get(name))
            if end is not None and end >= live_epoch:
                del _streak_state[key][name]
                changed = True
        # The new broadcast makes an older "already saved" moot; drop it so
        # the file stays small.
        entry = _streak_state["saved"].get(name)
        if entry and (_iso_to_epoch(entry.get("at")) or 0) <= live_epoch:
            del _streak_state["saved"][name]
            changed = True
        if changed:
            _write_streak_state_locked()
            _publish_saved_streaks_locked(now)
    if new_start:
        # A new broadcast: this streamer's cards are judged afresh. Taken
        # after _streak_state_lock is released (the card lock comes first).
        with _streak_event_lock:
            _forget_card_keys_locked(name)
    return new_start


def record_stream_offline(streamer: str, *, missed: bool = True,
                          only_if_open: bool = False,
                          now_epoch: Optional[float] = None) -> bool:
    """Remember, as of now, that the streamer's broadcast has ended. With
    missed (Stream Monitor skipped it for a pause, or a fresh start finds it
    over) the end also ends a save seen during that broadcast. Without it
    Stream Monitor had the broadcast open, so such a save lasts until the
    next go-live; the end is still noted, so a later fresh start does not
    take it for an end it never saw. With only_if_open (a fresh start,
    which cannot have seen an end that happened before it) this acts only
    when a go-live is on record with no end after it, so it is cheap to
    call on every poll. Returns True if an end was recorded."""
    name = streamer.lower()
    if not _OPEN_TABS_LOGIN_RE.match(name):
        return False
    now = _streak_clock() if now_epoch is None else now_epoch
    with _streak_state_lock:
        live_iso = _streak_state["last_live"].get(name)
        live = _iso_to_epoch(live_iso)
        ends = [
            t for t in (_iso_to_epoch(_streak_state["last_offline"].get(name)),
                        _iso_to_epoch(_streak_state["missed_end"].get(name)))
            if t is not None
        ]
        if only_if_open and (live is None or (ends and max(ends) >= live)):
            return False
        # Never before the start, even with a clock stepped back.
        at = live_iso if live is not None and now <= live + 1 else _epoch_to_iso(now)
        _streak_state["last_offline"][name] = at
        if missed:
            _streak_state["missed_end"][name] = at
        _write_streak_state_locked()
        _publish_saved_streaks_locked(now)
    return True


class SaveReport(NamedTuple):
    """What record_streak_saved did with a report (see its docstring)."""
    outcome: str
    at: Optional[str] = None
    boundary: Optional[str] = None


def record_streak_saved(streamer: str, count: int, at: Optional[str] = None, *,
                        now_epoch: Optional[float] = None) -> SaveReport:
    """Remember that Twitch reported the streak as already kept. `at` is the
    moment the page was seen; a missing, unreadable or future value becomes
    now. The outcome is one of:
      "recorded"            stored as the streamer's save
      "older"               a later save is already stored and stays
      "superseded_by_live"  a broadcast start or end (`boundary`) is at or
                            after `at`, so the report no longer holds
      "expired"             already too old to count
      "bad_login"           not a Twitch login; nothing stored
    """
    name = streamer.strip().lower() if isinstance(streamer, str) else ""
    if not _OPEN_TABS_LOGIN_RE.match(name):
        return SaveReport("bad_login")
    now = _streak_clock() if now_epoch is None else now_epoch
    at_epoch = _iso_to_epoch(at)
    if at_epoch is None or at_epoch > now:
        at = _epoch_to_iso(now)
        at_epoch = _iso_to_epoch(at)
    report = {"at": at, "count": int(count)}
    with _streak_state_lock:
        boundary = _broadcast_boundary_locked(name)
        if boundary is not None and at_epoch <= boundary:
            return SaveReport("superseded_by_live", at, _epoch_to_iso(boundary))
        if not _save_counts_locked(name, report, now):
            return SaveReport("expired", at)
        stored = _streak_state["saved"].get(name)
        # Keep the later of the two, but only while the stored one still
        # counts: one dated ahead of a stepped-back clock must not block
        # every new report.
        if (stored and _save_counts_locked(name, stored, now)
                and _iso_to_epoch(stored.get("at")) > at_epoch):
            outcome = "older"
        else:
            _streak_state["saved"][name] = report
            _write_streak_state_locked()
            outcome = "recorded"
        _publish_saved_streaks_locked(now)
    return SaveReport(outcome, at)


def _end_saved_streak(name: str, at: str, reason: str, **fields) -> bool:
    """End the save recorded at `at`. A newer save stored meanwhile stays.
    Returns True if the save was removed."""
    with _streak_state_lock:
        entry = _streak_state["saved"].get(name)
        if not entry or entry.get("at") != at:
            return False
        del _streak_state["saved"][name]
        _write_streak_state_locked()
        _publish_saved_streaks_locked()
    log_activity("streak_save_ended", streamer=name, reason=reason, saved_at=at, **fields)
    log.info("Save for %s seen at %s ended: %s", name, at, reason)
    return True


def _counted_save(name: str, now_epoch: Optional[float] = None) -> Optional[dict]:
    """A copy of the streamer's save if it counts right now, else None."""
    now = _streak_clock() if now_epoch is None else now_epoch
    with _streak_state_lock:
        entry = _streak_state["saved"].get(name)
        if entry and _save_counts_locked(name, entry, now):
            return dict(entry)
    return None


def _lapsed_save(name: str, now_epoch: Optional[float] = None) -> Optional[dict]:
    """A copy of the streamer's stored save when it no longer counts only
    because of the age caps (SAVED_STREAK_MAX_AGE_SECONDS, or
    SAVED_STREAK_UNPOLLED_TTL_SECONDS): no broadcast start or missed end
    came after it and its time is not distrusted. Such a save still feeds
    the card verdict (A4). None otherwise, and None while it still counts."""
    now = _streak_clock() if now_epoch is None else now_epoch
    with _streak_state_lock:
        entry = _streak_state["saved"].get(name)
        if not entry or _save_counts_locked(name, entry, now):
            return None
        saved_at = _iso_to_epoch(entry.get("at"))
        if saved_at is None or saved_at > now + STREAK_CLOCK_SKEW_SECONDS:
            return None
        boundary = _broadcast_boundary_locked(name)
        if boundary is not None and boundary >= saved_at:
            return None
        return dict(entry)


def _counting_saves(now_epoch: Optional[float] = None) -> dict:
    """{login: {"at": epoch, "count": int}} for every save that counts right
    now: the scheduler's `saves` input."""
    now = _streak_clock() if now_epoch is None else now_epoch
    out = {}
    with _streak_state_lock:
        for name, entry in _streak_state["saved"].items():
            if _save_counts_locked(name, entry, now):
                out[name] = {"at": _iso_to_epoch(entry.get("at")), "count": entry.get("count", 0)}
    return out


def _verdict_save(entry: Optional[dict]) -> Optional[dict]:
    """A stored save {at: ISO, count} in the shape card_verdict, save_covers
    and queued_vod_covered take: {at: epoch, count}."""
    if not entry:
        return None
    at = _iso_to_epoch(entry.get("at"))
    if at is None:
        return None
    return {"at": at, "count": entry.get("count")}


def streak_saved_since_last_live(streamer: str, now_epoch: Optional[float] = None) -> Optional[str]:
    """The saved-at timestamp if the streamer's streak counts as saved right
    now (the rules in the block comment above), else None."""
    entry = _counted_save(streamer.lower(), now_epoch)
    return entry["at"] if entry else None


def set_tray_notifier(fn: Callable[[str, str], None]) -> None:
    """Register the callable used to raise tray notifications."""
    global _tray_notifier
    _tray_notifier = fn


def _notify_tray(title: str, msg: str) -> None:
    if _tray_notifier:
        try:
            _tray_notifier(title, msg)
        except Exception as e:
            log.warning("Tray notifier raised on streak event: %s", e)


def _format_streak_message(event: dict, deadline_at: Optional[float] = None,
                           age_unit_s: int = 0, now: Optional[float] = None) -> tuple:
    """Toast title and message for a card. Titles are unchanged since 1.6;
    a link event (no count) reads "<login>: streak broke". With deadline_at
    the message states the hours actually left (A37); without it (a card
    whose login is not verified) the message is the one 1.11 showed."""
    streamer = event.get("streamer", "unknown")
    count = event.get("count", 0)
    broke = event.get("status") == "broke"
    if count is None:
        title = f"{streamer}: streak broke" if broke else f"{streamer}: streak in danger"
    elif broke:
        title = f"{streamer}: {count}-stream streak broke"
    else:
        title = f"{streamer}: {count}-stream streak in danger"
    if deadline_at is None:
        if broke:
            msg = "Watch a clip, VOD or stream within 24h to save it."
        else:
            msg = f"Ends in ~{event.get('deadline_hours', '?')}h. Watch to keep the streak alive."
        return title, msg
    now = _streak_clock() if now is None else now
    left = deadline_at + (age_unit_s or 0) - now
    hours = max(1, int(left // 3600))
    if broke:
        if left <= 0:
            msg = "Its save window may already be over."
        else:
            msg = f"Watch a clip, VOD or stream within ~{hours}h to save it."
    else:
        msg = f"Ends in ~{hours}h. Watch to keep the streak alive."
    return title, msg


def _handle_already_saved(name: str, count: int, payload: dict) -> SaveReport:
    """Evaluate every report, with no session dedup on recording: a save
    ended by a broadcast must be recordable again from the same page text.
    Only the toast and the streak_already_saved activity entry are deduped,
    per streamer and count for the session. Returns what record_streak_saved
    did."""
    page_url = payload.get("page_url")
    key = ("already_saved", name, count)
    announced = False
    # Recorded under the lock a card is judged under (handle_streak_event),
    # so no card can add its key between the save and the forgetting below.
    with _streak_event_lock:
        result = record_streak_saved(name, count, payload.get("detected_at"))
        if result.outcome == "recorded":
            # The stale card that led here was usually announced before the
            # save. A real break once the save ends reads exactly the same,
            # so forget the cards this save covers (its count or lower). A
            # link event's key has no count and is not covered.
            covered = [
                seen for seen in list(_streak_event_seen) + list(_streak_event_seen_at)
                + list(_streak_item_keys)
                if seen[0] in _CARD_STATUSES and seen[1] == name
                and isinstance(seen[2], int) and not isinstance(seen[2], bool) and seen[2] <= count
            ]
            for seen in covered:
                _streak_event_seen.discard(seen)
                _streak_event_seen_at.pop(seen, None)
                _streak_item_keys.pop(seen, None)
            announced = key in _streak_event_seen
            _streak_event_seen.add(key)
    if result.outcome in ("superseded_by_live", "expired"):
        extra = {"superseded_at": result.boundary} if result.boundary else {}
        log_activity(
            "streak_event_ignored",
            streamer=name,
            status="already_saved",
            count=count,
            reason=result.outcome,
            detected_at=result.at,
            page_url=page_url,
            **extra,
        )
        log.info(
            "Ignoring already_saved for %s seen at %s: %s", name, result.at,
            f"the broadcast start or end at {result.boundary} came after it"
            if result.boundary else "too old to count",
        )
        return result
    if result.outcome != "recorded":
        log.info("already_saved for %s seen at %s: a later save is on record", name, result.at)
        return result
    if announced:
        log.info("Save for %s recorded again at %s (already announced this session)", name, result.at)
        return result
    log_activity(
        "streak_already_saved",
        streamer=name,
        count=count,
        saved_at=result.at,
        page_url=page_url,
    )
    title = f"{name}: {count}-stream streak is already safe"
    msg = "Twitch says it was kept. Nothing to rescue for now."
    log.info("Streak event: %s - %s", title, msg)
    _notify_tray(title, msg)
    return result


def handle_streak_event(payload: dict) -> dict:
    """Validate, judge, dedup, log and notify on an incoming streak event,
    and hand any save item to the monitor. Returns the answer body
    {"verdict", "item"} (plan 3.5). Raises ValueError for a malformed
    payload (POST /streak_event answers 400).

    "broke" and "in_danger" come from Twitch's notification cards, a
    save-streak link event (source "link", no count; /config no longer
    lists "link", so the extensions send none, plan A41) or a Streaks at
    Risk row in the popup (source "manual"). "already_saved" comes from a
    save-streak page, or the dialog Twitch shows after moving it on, that
    says the streak was already kept; see the saved-streak memory above
    for what it changes."""
    if not isinstance(payload, dict):
        raise ValueError("payload not a dict")
    status = payload.get("status")
    if status not in ("broke", "in_danger", "already_saved"):
        raise ValueError(f"unknown status: {status!r}")
    streamer = payload.get("streamer")
    if not isinstance(streamer, str) or not streamer.strip():
        raise ValueError("missing streamer")
    count = payload.get("count")
    source = payload.get("source")
    if count is None:
        # A link event carries no count (then it is a broke event), and
        # neither does a popup row made from one.
        if source == "link":
            if status != "broke":
                raise ValueError(f"a link event must be broke, not {status!r}")
        elif source == "manual":
            if status not in _CARD_STATUSES:
                raise ValueError(f"bad manual status: {status!r}")
        else:
            raise ValueError("bad count: None")
    elif not isinstance(count, int) or count < 0 or count > 100000:
        raise ValueError(f"bad count: {count!r}")
    deadline_hours = payload.get("deadline_hours")
    if deadline_hours is not None and (
        not isinstance(deadline_hours, int) or deadline_hours < 0 or deadline_hours > 24 * 365
    ):
        raise ValueError(f"bad deadline_hours: {deadline_hours!r}")

    name = streamer.strip().lower()
    count = int(count) if count is not None else None
    now = _streak_clock()
    extras = streak_saves.parse_card_extras(payload, now)
    if status == "already_saved":
        # The save is stored under this name and published to the
        # extension, so it must be a real login. Card names are left alone:
        # they can be display names parsed from the card text.
        if not _OPEN_TABS_LOGIN_RE.match(name):
            raise ValueError(f"bad streamer login: {streamer[:64]!r}")
        result = _handle_already_saved(name, count, payload)
        item = False
        if result.outcome in ("recorded", "older"):
            # The streamer's save turn is done: the monitor completes the
            # item and frees its slot before this answer (DESIGN 12.3).
            submitted = _submit_to_monitor("item_done", {
                "login": name, "reason": "already_saved", "saved_at": result.at,
            })
            item = _monitor_changed(submitted)
        return {"verdict": "saved", "item": item}
    if extras["source"] == "manual":
        return _handle_manual_streak_event(name, status, count, payload, extras, now)
    return _handle_streak_card(name, status, count, deadline_hours, payload, extras, now)


def _card_detected_at(payload: dict, now: float) -> float:
    """The card's detected_at as an epoch; now when it is missing,
    unreadable or later than now."""
    detected = _iso_to_epoch(payload.get("detected_at"))
    if detected is None or detected > now:
        return now
    return detected


def _handle_manual_streak_event(name: str, status: str, count, payload: dict,
                                extras: dict, now: float) -> dict:
    """A Streaks at Risk row the owner clicked (A12, O16): always accepted
    for a real login, whatever the auto-save setting; no card verdict, no
    toast, no activity line and no dedup key. The monitor logs the item."""
    if not streak_saves.login_ok(name):
        log.debug("Manual save for %r not taken: not a Twitch login", name[:64])
        return {"verdict": "fresh", "item": False}
    item = streak_saves.make_manual_item(name, status, count, _card_detected_at(payload, now),
                                         extras["deadline_at"], now)
    submitted = _submit_to_monitor("streak_item", {"item": item, "merge_only": False})
    return {"verdict": "fresh", "item": submitted is not None}


def _card_activity(status: str, name: str, count, deadline_hours, payload: dict,
                   extras: dict, verified: bool, deadline_at: float, verdict: str) -> None:
    log_activity(
        "streak_broke" if status == "broke" else "streak_in_danger",
        streamer=name,
        count=count,
        deadline_hours=int(deadline_hours) if deadline_hours is not None else None,
        detected_at=payload.get("detected_at"),
        page_url=payload.get("page_url"),
        login_verified=verified,
        source=extras["source"],
        card_age_s=extras["card_age_s"],
        card_age_unit_s=extras["card_age_unit_s"],
        deadline_at=_epoch_to_iso(deadline_at),
        verdict=verdict,
    )


def _handle_streak_card(name: str, status: str, count, deadline_hours, payload: dict,
                        extras: dict, now: float) -> dict:
    """A broke or in-danger card (or a link event), per plan 3.5.1 step 5."""
    detected = _card_detected_at(payload, now)
    card = {"detected_at": detected, "card_age_s": extras["card_age_s"],
            "card_age_unit_s": extras["card_age_unit_s"], "count": count}
    item = streak_saves.make_card_item(name, status, count, detected, deadline_hours, extras, now)
    record = streak_saves.card_record(card, item["deadline_at"], now)
    key = streak_saves.dedup_key(status, name, count)
    event = {"status": status, "streamer": name, "count": count, "deadline_hours": deadline_hours}
    auto_save = ConfigRequestHandler.config_data.get("auto_save_streaks") is True

    verified = payload.get("login_verified") is not False and streak_saves.login_ok(name)
    if not verified:
        # Logged and toasted as in 1.11, never a save item (AUDIT P5).
        with _streak_event_lock:
            _prune_streak_dedup_locked(now)
            announced = _announce_card_locked(key, record, now)
        if announced:
            log.debug("Streak event %s already seen this session, skipping", key)
            return {"verdict": "fresh", "item": False}
        _card_activity(status, name, count, deadline_hours, payload, extras, False,
                       item["deadline_at"], "fresh")
        title, msg = _format_streak_message(event)
        log.info("Streak event (login not verified): %s - %s", title, msg)
        _notify_tray(title, msg)
        return {"verdict": "fresh", "item": False}

    # Judged under the lock a save is recorded under (_handle_already_saved),
    # so a card cannot add its key just after a save forgot the keys it
    # covers.
    with _streak_event_lock:
        _prune_streak_dedup_locked(now)
        counted = _counted_save(name, now)
        lapsed = _lapsed_save(name, now) if counted is None else None
        entry = counted if counted is not None else lapsed
        verdict, row = streak_saves.card_verdict(
            card, _verdict_save(entry),
            lapsed_by_age=counted is None and lapsed is not None,
            watch_start=_watch_start_for(name),
        )
        announced = False
        if verdict == "fresh":
            announced = _announce_card_locked(key, record, now)
        item_record = _streak_item_keys.get(key)
        item_record = dict(item_record) if item_record is not None else None

    if verdict in ("stale", "verify"):
        # A card Twitch has since confirmed as kept (stale), or one only the
        # save-streak page can settle (verify, O1 (c)). Exactly one activity
        # event, no toast, and the key is not added: once the save ends, this
        # identical card is judged afresh.
        saved_at = entry["at"] if entry else None
        log_activity(
            "streak_event_ignored",
            streamer=name,
            status=status,
            count=count,
            reason="already_saved",
            saved_at=saved_at,
            detected_at=payload.get("detected_at"),
            verdict=verdict,
        )
        log.info("Ignoring %s card for %s (%s): streak already saved at %s",
                 status, name, verdict, saved_at)
        changed = False
        if verdict == "verify" and auto_save and item_record is None:
            submitted = _submit_to_monitor("streak_item", {"item": dict(item, verify=True),
                                                           "merge_only": False})
            if submitted is not None:
                with _streak_event_lock:
                    _streak_item_keys[key] = dict(record)
            changed = _monitor_changed(submitted)
        return {"verdict": verdict, "item": changed}

    if row in (1, 3) and entry is not None:
        # A break newer than the save (row 1), or a streak that grew since
        # it (row 3): the save ends, and so do this streamer's other keys.
        ended = _end_saved_streak(
            name, entry["at"], "newer_card" if row == 1 else "count_grew",
            saved_count=entry.get("count"), card_status=status, card_count=count,
        )
        if ended:
            with _streak_event_lock:
                _forget_card_keys_locked(name, keep=key)

    if announced:
        # The same card (or an older one) as one already announced this run.
        changed = False
        if auto_save:
            if item_record is None:
                # First seen while automatic saves were off.
                submitted = _submit_to_monitor("streak_item", {"item": item, "merge_only": False})
                if submitted is not None:
                    with _streak_event_lock:
                        _streak_item_keys[key] = dict(record)
                changed = _monitor_changed(submitted)
            elif streak_saves.is_escalation(item_record, record):
                # A shorter deadline: merge it into the pending item, never
                # create one (the item may be done).
                submitted = _submit_to_monitor("streak_item", {"item": item, "merge_only": True})
                if submitted is not None:
                    with _streak_event_lock:
                        stored = _streak_item_keys.get(key)
                        if stored is not None:
                            stored["deadline_at"] = record["deadline_at"]
                            stored["seen_at"] = now
                changed = _monitor_changed(submitted)
        log.debug("Streak event %s already seen this session, skipping", key)
        return {"verdict": "duplicate", "item": changed}

    _card_activity(status, name, count, deadline_hours, payload, extras, True,
                   item["deadline_at"], "fresh")
    title, msg = _format_streak_message(event, item["deadline_at"], item["age_unit_s"], now)
    log.info("Streak event: %s - %s", title, msg)
    _notify_tray(title, msg)
    changed = False
    if auto_save:
        submitted = _submit_to_monitor("streak_item", {"item": item, "merge_only": False})
        if submitted is not None:
            with _streak_event_lock:
                _streak_item_keys[key] = dict(record)
        changed = _monitor_changed(submitted)
    return {"verdict": "fresh", "item": changed}


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
    read_activity_log(). Secrets are redacted line by line, so a line written
    before the log filter existed is never served as it was.
    """
    if not LOG_FILE.exists():
        return []
    entries = []
    current = None
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = redact_secrets(line.rstrip("\n"))
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


# Refusal logging budget. Any web page the user visits can send requests
# that the server refuses (a foreign Host, a web Origin, a non-JSON body), as
# fast as it likes and with attacker-chosen values. One line per refusal
# would let it flood the debug log and rotate its history away, and a budget
# for the whole process would let one burst hide every later refusal. So
# each distinct refusal is logged once per window, at most
# REFUSAL_LOG_LIMIT of them; the rest are counted, and the count is logged
# when the next window starts.
REFUSAL_LOG_WINDOW_SECONDS = 3600
REFUSAL_LOG_LIMIT = 50
_refusal_clock = time.monotonic
_refusal_log_lock = threading.Lock()


def _new_refusal_window(start: float) -> dict:
    return {"start": start, "keys": set(), "suppressed": 0, "cap_noted": False}


_refusal_log = _new_refusal_window(float("-inf"))


def _log_refusal(key: str, message: str, *args) -> None:
    """log.warning(message, *args) the first time `key` is refused in the
    current window, within the budget above. Keys and arguments must
    already be bounded in length by the caller."""
    global _refusal_log
    now = _refusal_clock()
    with _refusal_log_lock:
        state = _refusal_log
        carried = 0
        if now - state["start"] >= REFUSAL_LOG_WINDOW_SECONDS:
            carried = state["suppressed"]
            state = _refusal_log = _new_refusal_window(now)
        if key in state["keys"]:
            state["suppressed"] += 1
            emit = None
        elif len(state["keys"]) >= REFUSAL_LOG_LIMIT:
            state["suppressed"] += 1
            emit = None if state["cap_noted"] else "cap"
            state["cap_noted"] = True
        else:
            state["keys"].add(key)
            emit = "line"
    if carried:
        log.warning("Refused %d more request(s) in the previous hour without logging each one", carried)
    if emit == "line":
        log.warning(message, *args)
    elif emit == "cap":
        log.warning("Refused requests of %d different kinds this hour; counting the rest instead of logging them",
                    REFUSAL_LOG_LIMIT)


class ConfigRequestHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to serve config and about page."""

    config_data = {}

    def parse_request(self) -> bool:
        """Parse as usual, then refuse any request whose Host is not this
        server's own address, before any method runs (GET, POST, OPTIONS,
        and unsupported methods alike).

        DNS rebinding: a page at http://x.attacker.example:52832 whose name
        first resolves to the attacker's server and then to 127.0.0.1 is
        same origin with its own requests, so the browser delivers them here
        and lets the page read every answer without any CORS header, and its
        JSON POSTs need no preflight and can carry "Origin: null". The
        browser always sends the page's own hostname as Host, and a page
        cannot change or remove that header, so the Host check stops it.

        Accepted: 127.0.0.1, localhost and [::1] with the bound port,
        compared case-insensitively. Every legitimate caller uses
        http://127.0.0.1:52832 (both extensions, their popups, the tray's
        /about and /logs links, the updater's curl), and logs.html fetches
        relative paths, so it inherits the Host it was opened with. A
        request with no Host header at all (any HTTP version) is allowed:
        no browser sends a request without one, so it cannot come from a
        rebinding page, and any other program on this PC could just as well
        send an allowed Host, so refusing it would stop nothing.
        """
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host") or []
        if not hosts:
            return True
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if len(hosts) == 1 and hosts[0].strip().lower() in allowed:
            return True
        # Bounded before logging or storing: every value here is the sender's.
        host = ", ".join(h.strip() for h in hosts)[:100]
        _log_refusal(f"host {host}", "Refused %s %s for host %r: not this app's address (DNS rebinding?)",
                     self.command[:16], self.path[:64], host)
        self._discard_body()
        self.close_connection = True
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        return False

    def do_GET(self):
        # No GET answer carries Access-Control-Allow-Origin. Without it a web
        # page on another site can still send the request but cannot read the
        # answer, which holds the watch list, the activity history and the
        # debug log. The readers that matter need no CORS header: both
        # extensions read /config from their background scripts with the
        # http://127.0.0.1/* host permission, and logs.html reads the JSON
        # endpoints from the page the desktop itself serves (same origin).
        # A page that makes itself same origin through DNS rebinding never
        # gets here: parse_request refuses its Host first.
        if self.path == "/config":
            # Not a sign of the extension: any web page can send this GET
            # (see note_extension_contact). Saves also run out with time,
            # not only on a poll or an event.
            publish_saved_streaks()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
            # Parsed activity events as JSON, reverse-chronological, for
            # logs.html only (same origin, so no CORS header).
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
                # Redacted like /debug.log.json, for lines written before
                # the log filter existed.
                self.wfile.write(_redact_bytes(LOG_FILE.read_bytes()))
        elif self.path == "/debug.log.json":
            # Parsed debug log entries as JSON, reverse-chronological, for
            # logs.html only (same origin, so no CORS header).
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
        scrapes them from the bell dropdown / notifications page). Answers
        200 with {"verdict", "item"} (see handle_streak_event).
        /open_tabs: the monitored streamers with a Stream Monitor tab open
        in that browser (see record_extension_open_tabs).
        /rescue_ack: the extension claims the published rescue offer.

        Only these guarded POSTs mark the extension as alive
        (note_extension_contact), and only once they pass the checks below.

        Every route answers 403 to a web page. Browsers put the page's
        http(s) origin on a cross-site POST, and a text/plain body needs no
        preflight, so without this any site could write streak, tab or
        rescue state. A page can also send "null" instead (a sandboxed
        frame, a no-referrer request, an https page posting to http), so
        every route also needs Content-Type application/json, which both
        extensions send: a page can only send that after a CORS preflight,
        and do_OPTIONS refuses a page's preflight. The extension's own
        requests carry no Origin, an extension origin, or "null", and need
        no preflight (host permission). A DNS-rebound page, which is same
        origin and can send "null" with a JSON body, is refused earlier by
        the Host check in parse_request.
        """
        origin = self.headers.get("Origin") or ""
        if origin.strip().lower().startswith(("http://", "https://")):
            _log_refusal(f"origin POST {self.path[:64]} {origin[:100]}",
                         "Refused POST %s from a web page (Origin %s)", self.path[:64], origin[:100])
            self._discard_body()
            self.send_response(403)
            self.end_headers()
            return
        if not self._has_json_content_type():
            content_type = (self.headers.get("Content-Type") or "")[:100]
            _log_refusal(
                f"content-type POST {self.path[:64]} {content_type}",
                "Refused POST %s: Content-Type %r is not application/json",
                self.path[:64], content_type,
            )
            self._discard_body()
            self.send_response(415)
            self.end_headers()
            return

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
                answer = handle_streak_event(payload)
            except ValueError as e:
                log.warning("Rejected streak event: %s", e)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"bad streak event: {e}".encode("utf-8"))
                return
            except Exception as e:
                note_extension_contact()
                log.warning("Streak event handler raised: %s", e)
                self.send_response(500)
                self.end_headers()
                return
            note_extension_contact()
            # 200 with the verdict (plan 3.5); an extension older than 1.12
            # only checks resp.ok, which a 200 satisfies.
            body = json.dumps(answer).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            ok, replan = False, False
            if isinstance(payload, dict):
                ok, replan = _record_open_tabs(
                    payload.get("browser"), payload.get("streamers"),
                    str(payload.get("reason", ""))[:32],
                    instance=payload.get("instance"), plan_seq=payload.get("plan_seq"),
                    gone=payload.get("gone"), busy=payload.get("busy"),
                )
            if ok:
                note_extension_contact()
            if ok and replan:
                # A tab the owner closed, or a pause in the browser, changes
                # the plan now: the answer waits for the replan (9.4).
                _submit_to_monitor("report", {})
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
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if not isinstance(payload, dict):
                self.send_response(400)
                self.end_headers()
                return
            if isinstance(payload.get("id"), str):
                note_extension_contact()
            offer_id = str(payload.get("id", ""))
            claimant = _rescue_claimant(payload)
            claim_handler = _rescue_claim_handler
            if claim_handler is not None:
                accepted = bool(offer_id and claim_handler(offer_id, claimant))
            else:
                handler = _rescue_ack_handler
                accepted = bool(handler and offer_id and handler(offer_id))
            self.send_response(204 if accepted else 409)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def _discard_body(self, limit: int = 16384) -> None:
        """Read a small unused request body before answering, so closing the
        connection does not reset it before the client reads the reply."""
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return
        if 0 < length <= limit:
            self.rfile.read(length)

    def _has_json_content_type(self) -> bool:
        """Whether the request declares a JSON body. Only the media type
        counts, so "text/plain; x=application/json" is not JSON."""
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        return media_type.strip().lower() == "application/json"

    def do_OPTIONS(self):
        """Handle CORS preflight. A web page's preflight (an http(s) origin,
        or "null" from an opaque one such as a sandboxed frame) is refused,
        so a page never gets to send the JSON body every POST route needs.
        The extension's requests skip the preflight (host permission); one
        from an extension origin is still answered as before."""
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin == "null" or origin.startswith(("http://", "https://")):
            _log_refusal(f"preflight {self.path[:64]} {origin[:100]}",
                         "Refused a preflight for %s from a web page (Origin %s)",
                         self.path[:64], origin[:100])
            self.send_response(403)
            self.end_headers()
            return
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
    _streak_event_lock, saved streaks and their /config publish through
    _streak_state_lock, and config_data reads are GIL-atomic dict lookups.
    Save items, tab reports that change the plan and completed saves reach
    the monitor only through its inbox (_submit_to_monitor); a handler
    waiting up to SLOT_REPLAN_WAIT_SECONDS for the replan blocks nobody.
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
        "saved_streaks": saved_streaks_for_config(),
        # 1.12.0: the Slot mode plan (null while Slot mode is off; the
        # monitor publishes it from its first tick), the automatic-save
        # setting and the streak-event sources the extensions may send
        # (no "link" since the 2026-10-01 live check, plan A41).
        "slot_plan": None,
        "auto_save_streaks": config.auto_save_streaks,
        "streak_sources": list(streak_saves.STREAK_SOURCES),
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
    # repr=False: printing a Config (a log line, a traceback) never shows it.
    client_secret: str = field(default="", repr=False)
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
    # Slot mode (1.12.0): at most K Keep Open slots and C rotating slots
    # (K + C <= 3, C >= 1), M minutes per turn. Off by default.
    slot_mode: bool = False
    keep_open_slots: int = 2
    cycle_slots: int = 1
    slot_minutes: int = 30
    # Save broken streaks automatically: a broke card, or a broadcast that
    # ended before it was watched, gets one turn on the save-streak page.
    auto_save_streaks: bool = False

    def __post_init__(self):
        if self.streamers is None:
            self.streamers = []
        if self.pinned_streamers is None:
            self.pinned_streamers = []
        self._clamp_slot_fields()

    def _clamp_slot_fields(self) -> None:
        """Coerce and clamp the Slot mode fields (plan 3.1): bools must be
        bools, ints convert (a bool, or a value that does not convert, takes
        the default), Rotating 1..3, Keep Open 0..2, K + C <= 3 (Keep Open
        gives way), Minutes 5..120."""
        defaults = {f.name: f.default for f in self.__dataclass_fields__.values()}
        for name in ("slot_mode", "auto_save_streaks"):
            if not isinstance(getattr(self, name), bool):
                setattr(self, name, defaults[name])
        for name in ("keep_open_slots", "cycle_slots", "slot_minutes"):
            value = getattr(self, name)
            if isinstance(value, bool):
                value = defaults[name]
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    value = defaults[name]
            setattr(self, name, value)
        total = slot_scheduler.SLOT_MAX_TOTAL
        self.cycle_slots = min(max(self.cycle_slots, 1), total)
        self.keep_open_slots = min(max(self.keep_open_slots, 0), total - 1)
        if self.keep_open_slots + self.cycle_slots > total:
            self.keep_open_slots = total - self.cycle_slots
        self.slot_minutes = min(max(self.slot_minutes, 5), 120)

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


def _slot_config_fields(config: "Config") -> dict:
    """The five Slot mode config fields, for the config_loaded event."""
    return {
        "slot_mode": config.slot_mode,
        "keep_open_slots": config.keep_open_slots,
        "cycle_slots": config.cycle_slots,
        "slot_minutes": config.slot_minutes,
        "auto_save_streaks": config.auto_save_streaks,
    }


# ProgId prefixes of the https handler that mean the Chrome extension
# (every Chromium browser reports itself as "chrome"), and of Firefox.
_CHROMIUM_PROGIDS = ("ChromeHTML", "MSEdgeHTM", "BraveHTML", "BraveBHTML", "ChromiumHTM",
                     "OperaStable", "OperaGX", "VivaldiHTM")
_UNSET = object()
_default_browser_family_cache = _UNSET


def browser_family_for_progid(prog_id) -> Optional[str]:
    """"firefox", "chrome" or None for the ProgId Windows opens https links
    with."""
    if not isinstance(prog_id, str):
        return None
    if prog_id.startswith("FirefoxURL"):
        return "firefox"
    if prog_id.startswith(_CHROMIUM_PROGIDS):
        return "chrome"
    return None


def default_browser_family() -> Optional[str]:
    """The extension family of the Windows default browser (the browser a
    desktop open lands in): read once per process from the https
    UserChoice ProgId. None on any error or an unknown browser. Slot mode
    prefers a reporting profile of this browser as its executor (rule 37)."""
    global _default_browser_family_cache
    if _default_browser_family_cache is not _UNSET:
        return _default_browser_family_cache
    family = None
    try:
        import winreg
        path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        family = browser_family_for_progid(prog_id)
    except Exception:
        family = None
    _default_browser_family_cache = family
    return family


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
        # The last rescue offer acknowledged, {offer, claimant, acked_at},
        # kept LAST_ACKED_OFFER_TTL_SECONDS: the same claimant may re-ack it
        # for RESCUE_REACK_WINDOW_SECONDS, and Slot mode absorbs its
        # unfinished save-streak entries when it becomes active. Guarded by
        # _rescue_lock.
        self._last_acked_offer: Optional[dict] = None
        # Per-streamer metadata captured on the latest "live" check, used to
        # enrich the activity log (title, game, viewer count at the moment
        # the offline->live transition was detected).
        self.live_stream_meta: dict[str, dict] = {}

        # Slot mode (1.12.0). The scheduler holds every rule; this class
        # feeds it (_slot_inputs), applies what it returns (_slot_tick) and
        # owns every thread, lock and file around it. slot_state mirrors the
        # scheduler state after each tick; slot_active is true in waiting,
        # alive and absent, where the plan decides what opens and the 1.11
        # opening paths stand down.
        self.slot = slot_scheduler.SlotScheduler()
        self.slot_state: str = "off"
        # HTTP threads put work here (see _InboxItem); the monitor thread
        # drains it. _wake interrupts the loop's waits. Both survive stop()
        # and start(), so an item submitted while stopped waits for the
        # next start.
        self._inbox: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        # Held by _slot_tick and the inbox drain (re-entrant: a drain ends
        # with a replan).
        self._slot_lock = threading.RLock()
        # Every read or write of queued_vods, missed_while_paused and
        # held_save_items, on every thread (acknowledge_rescue still pops
        # the first two on an HTTP thread). Re-entrant: the flush and the
        # offer call the drop helpers. Lock order: _slot_lock, _vod_lock,
        # _rescue_lock, then the module locks.
        self._vod_lock = threading.RLock()
        # Incremented by every start(). A loop whose generation is no longer
        # current (a restart that outlived stop()'s 2 s join) exits without
        # polling or ticking.
        self._loop_gen: int = 0
        self._loop_alive_gen: Optional[int] = None
        # When the current unbroken run of successful polls that included a
        # login began (the card verdict's row 5, 12.5 C2).
        self._watch_start: dict[str, float] = {}
        self._last_poll_ok_mono: Optional[float] = None
        # Saved streamers not on the list, polled so their saves lift at
        # their go-live (12.5 C4).
        self._saved_extra_logins: list[str] = []
        # {login: started_at ISO} for every login live in the last successful
        # poll; a login Helix gave no started_at for keeps the time of the
        # first poll that saw it until it leaves the live set.
        self.live_started_at: dict[str, str] = {}
        self._live_as_of: Optional[float] = None
        self._poll_authoritative: bool = False
        # Normal-mode save items waiting for a live streamer's offline edge
        # (DESIGN 11.5), persisted in slot_state.json as "held".
        self.held_save_items: dict[str, dict] = {}
        self._held_dirty: bool = False
        # Seconds of sleep for the next tick to shift the slot clocks by.
        self._pending_wake_gap: float = 0.0
        self._seed_capable_pending: bool = False
        self._last_slot_tick_mono: float = float("-inf")
        # Desktop epoch of the last successful slot_state.json write.
        self._last_slot_persist: Optional[float] = None
        # The none_ever notice shows once per process (A36).
        self._never_seen_notified: bool = False

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
            register_secret(self.config.client_secret)
            # Credentials go in the form body, never the URL: requests puts
            # the request URL in HTTPError and ConnectionError text.
            response = self._http.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials"
                },
                timeout=10
            )
            response.raise_for_status()
            self.oauth_token = response.json().get("access_token")
            register_secret(self.oauth_token)
            if self.oauth_token:
                log.info("OAuth token obtained successfully")
            else:
                log.error("OAuth response OK but no access_token in body")
            return bool(self.oauth_token)
        except requests.RequestException as e:
            # Type and HTTP status only, never the exception text.
            failure = _token_failure_summary(e)
            log.error("OAuth token request failed: %s", failure)
            self.status_callback(f"Auth error: {failure}")
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

        monitored = list(self.streamers.keys())
        params = [("user_login", name) for name in monitored]

        # Also check user's own channel if "I'm live" pause is enabled
        own_channel = self.config.own_channel.lower().strip() if self.config.own_channel else ""
        if own_channel and self.config.im_live_pause:
            params.append(("user_login", own_channel))

        # Saved streamers not on the list ride the same request (newest
        # saves first, 100 logins at most), so their saves lift at their
        # next go-live (12.5 C4). They never get a tab.
        extras = self._saved_extra_logins_for_poll(len(params), own_channel)
        params += [("user_login", name) for name in extras]
        self._saved_extra_logins = extras
        self._poll_authoritative = False

        try:
            log.debug("Checking streams for: %s", list(self.streamers.keys()))
            data = self._api_get(self.TWITCH_API_URL, params)
            if data is None:
                # A failed re-auth: not a poll (rule 7), so the scheduler
                # adds no offline strikes from it.
                log.warning("API returned None, treating all streamers as offline")
                return {name: False for name in self.streamers}

            now = _streak_clock()
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
                # The broadcast's own start (UTC). Recorded as the go-live
                # instead of the poll time, so a relaunch or a one-poll gap
                # mid-broadcast does not look like a new broadcast.
                started_at = stream.get("started_at")
                if isinstance(started_at, str) and started_at:
                    new_meta[login]["started_at"] = started_at
            self.live_stream_meta = new_meta

            if live_set:
                log.info("Live streamers: %s", live_set)
            else:
                log.debug("No monitored streamers are live")

            polled = monitored + extras
            self._note_successful_poll(now, polled, live_set, new_meta)
            self._lift_saves_of_live_extras(extras, live_set, now)

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
                    if self.slot_active:
                        # Slot mode has no rescue offers: the plan resumes
                        # opening on its own (rule 33).
                        log.info("Auto-pause lifted in Slot mode: no rescue offer, the plan resumes")
                        log_activity("rescue_skipped", reason="slot_mode")
                    elif not self.paused:
                        # v1.7.0: instead of opening everything at once,
                        # publish a rescue offer for the extension's 3-slot
                        # rotation. Falls back to the open-everything path
                        # if the extension does not acknowledge in time.
                        self._offer_rescue_or_flush(live_set)

            # Update live streamers list for config server
            self.live_streamers = [name for name in self.streamers if name in live_set]
            self._poll_authoritative = True

            return {name: name in live_set for name in self.streamers}

        except requests.RequestException as e:
            log.error("API request failed: %s", e)
            # Truncate error message for tray tooltip (128 char Windows limit)
            err_short = str(e)[:80]
            self.status_callback(f"API error: {err_short}")
            raise  # Let _monitor_loop handle error counting and notifications

    def _saved_extra_logins_for_poll(self, used: int, own_channel: str) -> list:
        """Saved streamers not on the list and not the own channel, newest
        saves first, as many as fit next to `used` logins in one Helix
        request (HELIX_MAX_LOGINS)."""
        room = HELIX_MAX_LOGINS - used
        if room <= 0:
            return []
        with _streak_state_lock:
            saved = [(name, _iso_to_epoch(entry.get("at")) or 0.0)
                     for name, entry in _streak_state["saved"].items() if isinstance(entry, dict)]
        extras = [(name, at) for name, at in saved
                  if name not in self.streamers and name != own_channel]
        extras.sort(key=lambda pair: (-pair[1], pair[0]))
        return [name for name, _ in extras[:room]]

    def _note_successful_poll(self, now: float, polled: list, live_set: set,
                              meta: dict) -> None:
        """Bookkeeping after a successful poll: the polled set (the list plus
        the extras actually sent), the broadcast starts the scheduler reads
        (with a substitute when Helix gives none), and the runs of unbroken
        watching (a run restarts after failed polls for longer than
        max(3 x check_interval, WATCH_GAP_MIN_SECONDS))."""
        mono = time.monotonic()
        gap_limit = max(3 * self.config.check_interval, WATCH_GAP_MIN_SECONDS)
        watch = self._watch_start
        if self._last_poll_ok_mono is not None and mono - self._last_poll_ok_mono > gap_limit:
            watch = {}
        self._watch_start = {name: watch.get(name, now) for name in polled}
        self._last_poll_ok_mono = mono
        self._live_as_of = now
        started = {}
        for name in polled:
            if name not in live_set:
                continue
            value = meta.get(name, {}).get("started_at")
            if isinstance(value, str) and value:
                started[name] = value
            else:
                started[name] = self.live_started_at.get(name) or _epoch_to_iso(now)
        self.live_started_at = started
        set_polled_streamers(polled)

    def _lift_saves_of_live_extras(self, extras: list, live_set: set, now: float) -> None:
        """A saved streamer not on the list who is live now starts a new
        broadcast on record, which ends a save seen before it (12.5 C4)."""
        for name in extras:
            if name not in live_set:
                continue
            started = self.live_started_at.get(name)
            counted_before = _counted_save(name, now) is not None
            record_stream_live(name, started, fallback_to_now=False, now_epoch=now)
            if counted_before and _counted_save(name, now) is None:
                log.info("Save for %s lifted: live since %s", name, started)
                log_activity("streak_save_lifted", streamer=name, started_at=started)

    def watch_start_for(self, name: str) -> Optional[float]:
        """When the current unbroken run of successful polls that included
        `name` began, or None (the watch-start provider)."""
        return self._watch_start.get(name)

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

    @property
    def slot_active(self) -> bool:
        """True while Slot mode runs the plan (waiting, alive or absent). In
        none_ever, and with Slot mode off, every 1.11 path runs."""
        return self.slot_state in ("waiting", "alive", "absent")

    def _publish_queued_vods(self) -> None:
        with self._vod_lock:
            snapshot = dict(self.queued_vods)
        ConfigRequestHandler.config_data["queued_vods"] = snapshot

    def _flush_queued_vods(self, reason: str = "unpause") -> int:
        """Hand every queued VOD to the paced open queue and clear the
        VOD queue. Returns the count enqueued.

        Called when the pause that gated VOD-queueing lifts. The actual
        opens happen on the tab-open worker, spaced tab_open_spacing
        seconds apart, so a multi-VOD flush doesn't slam the browser
        with simultaneous tabs. In Slot mode the plan owns every open, so
        nothing is flushed.
        """
        if self.slot_active:
            with self._vod_lock:
                pending = bool(self.queued_vods)
            if pending:
                log.info("Not flushing queued VODs (reason=%s): Slot mode runs the plan", reason)
            return 0
        with self._vod_lock:
            if not self.queued_vods:
                return 0
            self._drop_expired_queued_vods()
            self._drop_saved_queued_vods()
            items = list(self.queued_vods.items())
            self.queued_vods.clear()
        ConfigRequestHandler.config_data["queued_vods"] = {}
        if not items:
            return 0
        for streamer, entry in items:
            self._enqueue_tab_open(
                "vod", streamer, entry["url"],
                from_queue=True, queue_reason=reason,
            )
        count = len(items)
        log.info("Flushed %d queued VOD(s) into the paced open queue (reason=%s)", count, reason)
        self.notify_callback(
            "Stream Monitor",
            f"Opening {count} queued VOD(s), {int(self.tab_open_spacing)}s apart"
            if count > 1 else
            f"Opening queued VOD for {items[0][0]}"
        )
        return count

    def _drop_expired_queued_vods(self) -> None:
        """Take entries past their save window out of the VOD queue (A8).
        Only an entry with a parseable deadline_at ever expires."""
        now = _streak_clock()
        dropped = False
        with self._vod_lock:
            for streamer, entry in list(self.queued_vods.items()):
                if isinstance(entry, dict) and streak_saves.queued_vod_expired(entry, now):
                    self.queued_vods.pop(streamer, None)
                    dropped = True
                    log.info("Dropping queued save-streak link for %s: its save window closed at %s",
                             streamer, entry.get("deadline_at"))
                    log_activity("streak_item_expired", streamer=streamer,
                                 deadline_at=entry.get("deadline_at"))
        if dropped:
            self._publish_queued_vods()

    def _drop_saved_queued_vods(self) -> None:
        """Take save-streak links out of the VOD queue for streaks Twitch has
        since confirmed as kept (the page would only say "No Content
        Eligible"), log each as vod_skipped, and republish the queue. Runs
        before every flush and rescue offer, so no path leaves such a link
        waiting in the queue or the tray menu. A link is covered by the save
        per save_covers: a card's by its card, a missed broadcast's when the
        save was seen after the broadcast ended."""
        dropped = False
        with self._vod_lock:
            for streamer in list(self.queued_vods):
                entry = self.queued_vods[streamer]
                saved = _counted_save(streamer)
                if not saved:
                    continue
                if not streak_saves.queued_vod_covered(
                        entry if isinstance(entry, dict) else {}, _verdict_save(saved),
                        self._watch_start.get(streamer)):
                    continue
                self.queued_vods.pop(streamer, None)
                dropped = True
                log.info("Dropping queued save-streak link for %s: streak already saved at %s",
                         streamer, saved["at"])
                log_activity("vod_skipped", streamer=streamer, reason="streak_already_saved",
                             saved_at=saved["at"])
        if dropped:
            self._publish_queued_vods()

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
        the tiebreak for the ended tier. Entries past their save window are
        dropped first (A8). A candidate is exactly {streamer, url, kind,
        ended_at}."""
        with self._vod_lock:
            self._drop_expired_queued_vods()
            live_names = sorted(
                (name for name in list(self.missed_while_paused) if name in live_set),
                key=self._list_rank,
            )
            live_name_set = set(live_names)
            # A streamer can be in both lists: ended during the pause (VOD
            # queued) and live again by the time the pause lifted. Watching
            # the live stream saves the streak, so the live candidate wins
            # and the save-streak candidate is dropped.
            ended = []
            for streamer, entry in self.queued_vods.items():
                if streamer in live_name_set:
                    continue
                ended.append({
                    "streamer": streamer,
                    "url": entry["url"],
                    "kind": "ended",
                    "ended_at": entry.get("ended_at"),
                })
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
        # A kept streak's link would land on "No Content Eligible" and waste
        # a rotation slot; it leaves the queue instead of lingering there.
        with self._vod_lock:
            self._drop_saved_queued_vods()
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

    def acknowledge_rescue(self, offer_id: str, claimant: Optional[str] = None) -> bool:
        """Called from the HTTP thread when the extension POSTs /rescue_ack.
        Hands ownership of the offered candidates to the extension so the
        desktop neither flushes them later nor re-opens the live ones.

        The last acknowledged offer is remembered with its claimant (the
        "<browser>-<instance>" key, or None). The same named claimant may
        ack it again within RESCUE_REACK_WINDOW_SECONDS (its first answer
        may have been lost, AUDIT S7); anyone else gets False (409)."""
        now = _streak_clock()
        with self._rescue_lock:
            offer = self.rescue_pending
            if offer and offer["id"] == offer_id:
                self.rescue_pending = None
                self._last_acked_offer = {"offer": offer, "claimant": claimant, "acked_at": now}
            else:
                last = self._last_acked_offer
                reack = (
                    claimant is not None and last is not None
                    and last["offer"].get("id") == offer_id
                    and last["claimant"] == claimant
                    and now < last["acked_at"] + RESCUE_REACK_WINDOW_SECONDS
                )
                if not reack:
                    return False
                offer = None
        if offer is None:
            log.info("Rescue offer %s acknowledged again by %s", offer_id, claimant)
            log_activity("rescue_reacked", offer_id=offer_id, claimant=claimant)
            return True
        with self._vod_lock:
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
            queued = dict(self.queued_vods)
        ConfigRequestHandler.config_data["rescue"] = None
        ConfigRequestHandler.config_data["queued_vods"] = queued
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
        closed, pre-1.7 version). If the extension is still making guarded
        POSTs (its /open_tabs reports; a /config poll does not count, see
        note_extension_contact) it is alive and merely failing to ack, so
        the offer stays published (it will retry on every poll) until the
        hard deadline."""
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
                "Rescue offer %s unacked after %ds but the extension is still reporting; "
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
        with self._vod_lock:
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
        paced queue. Returns the list of streamer names opened. In Slot mode
        the plan owns every open, so this opens nothing.
        """
        if self.slot_active:
            with self._vod_lock:
                pending = bool(self.missed_while_paused)
            if pending:
                log.info("Not opening missed live streams (reason=%s): Slot mode runs the plan", reason)
            return []
        with self._vod_lock:
            if not self.missed_while_paused:
                return []
            still_live = sorted(
                (name for name in list(self.missed_while_paused) if name in live_set),
                key=self._list_rank,
            )
            for name in still_live:
                self.missed_while_paused.pop(name, None)
        for name in still_live:
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

    # -- normal-mode save items (DESIGN 11.5, A32) ----------------------------

    def _live_with_tab(self, login: str) -> bool:
        """The streamer is live and a report received within
        SLOT_EXECUTOR_ALIVE_SECONDS lists their tab."""
        if login not in self.live_streamers:
            return False
        mono = time.monotonic()
        for rep in extension_open_tabs_snapshot().values():
            if (mono - rep.get("mono", float("-inf")) <= slot_scheduler.SLOT_EXECUTOR_ALIVE_SECONDS
                    and login in rep.get("streamers", ())):
                return True
        return False

    @staticmethod
    def _merge_queued_vod(existing: dict, item: dict, now: float) -> tuple:
        """(entry, changed): a save item merged into a queued_vods entry,
        the earlier open deadline winning. An entry with an embedded item
        merges item by item (3.8); an entry without one (a VOD-fallback
        link) takes the item, keeping its own deadline when that is earlier
        and still open."""
        login = item["login"]
        embedded = streak_saves.clean_item(existing.get("item"), login) if isinstance(existing, dict) else None
        if embedded is not None:
            merged, _ = streak_saves.merge_items(embedded, item, now)
            entry = streak_saves.item_to_queued_vod(merged)
            return entry, entry != existing
        entry = streak_saves.item_to_queued_vod(item)
        old_deadline = _iso_to_epoch(existing.get("deadline_at")) if isinstance(existing, dict) else None
        if (old_deadline is not None and old_deadline < item["deadline_at"]
                and not streak_saves.queued_vod_expired(existing, now)):
            entry["deadline_at"] = existing["deadline_at"]
            entry["item"]["deadline_at"] = old_deadline
        return entry, entry != existing

    def _normal_save_item(self, item: dict, merge_only: bool = False, reason: str = "card",
                          offer: bool = True) -> bool:
        """Slot mode off (or no capable extension yet): a save item rides a
        rescue offer (A32). Held until the offline edge while the streamer
        is live with a tab; otherwise queued in queued_vods and offered
        unless paused (a pending unacked offer is republished with a new
        id; offer False leaves that to the caller). A held item whose
        streamer is no longer live with a tab becomes its page check first,
        and the incoming item merges into that entry. merge_only merges into
        an existing entry or held item and never creates one. Returns True
        when something was created or changed."""
        now = _streak_clock()
        login = item["login"]
        if item.get("origin") != "manual" and not self.config.auto_save_streaks:
            return False
        if streak_saves.item_expired(item, now):
            log_activity("streak_item_expired", streamer=login,
                         deadline_at=_epoch_to_iso(item["deadline_at"]))
            return False
        saved = _counted_save(login, now)
        if streak_saves.save_covers(item, _verdict_save(saved), self._watch_start.get(login)):
            log_activity("vod_skipped", streamer=login, reason="streak_already_saved",
                         saved_at=saved["at"])
            return False
        live_tab = self._live_with_tab(login)
        created_entry = False
        moved = False
        with self._vod_lock:
            held = self.held_save_items.get(login)
            queued = self.queued_vods.get(login)
            if merge_only and held is None and queued is None:
                return False
            if held is not None and not live_tab:
                # The broadcast it waited for is over (its end fell in a
                # downtime) or the tab is gone: the held item becomes the
                # page check now, as at the offline edge (O1 (a)).
                del self.held_save_items[login]
                self._held_dirty = True
                check = dict(held, verify=True)
                if queued is not None:
                    queued, _ = self._merge_queued_vod(queued, check, now)
                else:
                    queued = streak_saves.item_to_queued_vod(check)
                    created_entry = True
                self.queued_vods[login] = queued
                held = None
                moved = True
            if held is not None or (queued is None and live_tab):
                # Their live tab is the remedy for now; the save-streak page
                # gets a check once the broadcast ends (O1 (a)).
                if held is not None:
                    stored, changed = streak_saves.merge_items(held, item, now)
                    if not changed:
                        return False
                else:
                    stored = item
                self.held_save_items[login] = stored
                self._held_dirty = True
                entry = None
            else:
                if queued is not None:
                    entry, changed = self._merge_queued_vod(queued, item, now)
                    if not changed and not moved:
                        return False
                    stored = entry["item"]
                else:
                    entry = streak_saves.item_to_queued_vod(item)
                    stored = item
                    created_entry = True
                self.queued_vods[login] = entry
            snapshot = dict(self.queued_vods)
        log_activity(
            "streak_item_added",
            streamer=login,
            kind=stored["kind"],
            deadline_at=_epoch_to_iso(stored["deadline_at"]),
            origin=stored["origin"],
            verify=bool(stored.get("verify")),
            merged=held is not None or queued is not None,
            mode="normal",
        )
        if entry is None:
            log.info("Save item for %s held until their broadcast ends (their tab is open)", login)
            return True
        ConfigRequestHandler.config_data["queued_vods"] = snapshot
        log.info("Save-streak link for %s queued (reason=%s)", login, reason)
        log_activity("vod_queued", streamer=login, url=entry["url"], reason=reason)
        if offer and created_entry and not self.effectively_paused:
            self._offer_rescue_or_flush(set(self.live_streamers))
        return True

    def _normal_item_done(self, login: str, saved_at: Optional[str]) -> bool:
        """Twitch says the streamer's streak is kept: their held item and
        their queued save-streak link go (Slot mode off)."""
        with self._vod_lock:
            held = self.held_save_items.pop(login, None)
            queued = self.queued_vods.pop(login, None)
            if held is not None:
                self._held_dirty = True
            snapshot = dict(self.queued_vods)
        if held is None and queued is None:
            return False
        if queued is not None:
            ConfigRequestHandler.config_data["queued_vods"] = snapshot
        log.info("Dropping %s's pending save: streak already saved at %s", login, saved_at)
        log_activity("vod_skipped", streamer=login, reason="streak_already_saved", saved_at=saved_at)
        return True

    def process_state_changes(self, current_status: dict[str, bool]):
        live_count = 0
        # Update config server with live status
        ConfigRequestHandler.config_data["live_streamers"] = self.live_streamers
        ConfigRequestHandler.config_data["paused"] = self.paused
        ConfigRequestHandler.config_data["auto_paused"] = self.auto_paused
        # Surface the queue so the extension popup and any future UI can
        # show which VODs are waiting for the pause to lift.
        self._publish_queued_vods()
        publish_saved_streaks()
        offer_held_checks = False

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
                    # A new broadcast: an "already saved" verdict from before
                    # it no longer covers this streamer's streak. The Helix
                    # start keeps a relaunch mid-broadcast from counting as one.
                    record_stream_live(username, meta.get("started_at"))

                    # Desktop notification for all live events
                    self.notify_callback(
                        "Stream Monitor",
                        f"{username} is now live on Twitch!"
                    )

                    claimed_by = self._browsers_with_tab_open(username)
                    if self.slot_active:
                        # Slot mode: the plan decides when this stream gets
                        # a tab (a Keep Open slot, a turn, or idle).
                        log.info("Skipping tab open for %s: Slot mode runs the plan", username)
                        log_activity("tab_open_skipped", streamer=username, reason="slot_mode")
                        self.status_callback(f"{username} went LIVE! (Slot mode)")
                        state.browser_opened = True
                    elif claimed_by:
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
                        with self._vod_lock:
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
                else:
                    if state.was_live:
                        log.debug("Already tracking %s as live (was_live=%s, browser_opened=%s)",
                                  username, state.was_live, state.browser_opened)
                    # A broadcast can also start without the transition
                    # above: an end and a restart between two polls, or a
                    # go-live with browser_opened left set (a late rescue
                    # ack). A changed Helix start is a new broadcast (the
                    # same start writes nothing); back from offline, it is
                    # a go-live even without one.
                    started_at = self.live_stream_meta.get(username, {}).get("started_at")
                    if record_stream_live(username, started_at,
                                          fallback_to_now=not state.was_live):
                        log.info("New broadcast start on record for %s: %s",
                                 username, started_at or "now")
                state.was_live = True
            else:
                if state.was_live:
                    log.info("State change: %s went OFFLINE", username)
                    self.status_callback(f"{username} went offline")
                    log_activity("stream_offline", streamer=username)

                    if self.slot_active:
                        # Slot mode: an unserved broadcast gets its save turn
                        # from the scheduler (rule 24), never a link here.
                        # Nothing is ever missed while paused in Slot mode,
                        # so the end ends a save seen during the broadcast
                        # exactly when the scheduler did not serve it (A31).
                        with self._vod_lock:
                            self.missed_while_paused.pop(username, None)
                        with self._slot_lock:
                            served = self.slot.is_broadcast_served(username)
                        record_stream_offline(username, missed=not served)
                        state.was_live = False
                        state.browser_opened = False
                        continue

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
                    with self._vod_lock:
                        was_skipped_due_to_pause = username in self.missed_while_paused
                    # An "already maintained" seen while a broadcast skipped
                    # for a pause ran could only speak for the broadcasts
                    # before it, and the owner missed this one, so that save
                    # stops counting now and the save-streak logic below runs
                    # as usual. A broadcast Stream Monitor had open keeps the
                    # save until the next go-live, the owner's rule.
                    record_stream_offline(username, missed=was_skipped_due_to_pause)
                    if was_skipped_due_to_pause:
                        # Always leave the missed-while-paused list on the
                        # offline transition, even when VOD fallback is off.
                        # Stale entries used to survive here when the
                        # fallback was disabled and could trigger a
                        # duplicate open on a much later pause lift.
                        with self._vod_lock:
                            self.missed_while_paused.pop(username, None)
                    if was_skipped_due_to_pause and self.config.vod_fallback:
                        save_streak_url = f"https://www.twitch.tv/save-streak/{username}?sm=1"
                        if self.effectively_paused:
                            ended_at = _streak_clock()
                            with self._vod_lock:
                                existing = self.queued_vods.get(username)
                                if existing is not None:
                                    # One entry per streamer (plan 3.8, A32): a
                                    # queued card or check keeps the earlier
                                    # open deadline.
                                    missed_item = streak_saves.make_missed_item(
                                        username, ended_at, self.config.check_interval,
                                        None, ended_at)
                                    self.queued_vods[username], _ = self._merge_queued_vod(
                                        existing, missed_item, ended_at)
                                else:
                                    self.queued_vods[username] = {
                                        "url": save_streak_url,
                                        # Rescue-queue priority key: earliest-ended
                                        # streams have the least save window left.
                                        "ended_at": _epoch_to_iso(ended_at),
                                        # Past this the link is dropped (A8).
                                        "deadline_at": _epoch_to_iso(
                                            ended_at + streak_saves.SAVE_WINDOW_HOURS * 3600),
                                        "origin": "offline_edge",
                                    }
                                queue_size = len(self.queued_vods)
                            reason = "auto_paused" if self.auto_paused else "paused"
                            log.info(
                                "Save-streak URL for %s queued (reason=%s, queue size now %d)",
                                username, reason, queue_size,
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
                    if self._queue_held_check(username):
                        offer_held_checks = True

                    state.was_live = False
                    state.browser_opened = False
                else:
                    # A fresh start (relaunch, tray Start) cannot see an end
                    # that happened while it was not running; if a go-live
                    # is on record with no end after it, the broadcast is
                    # over now. Nobody knows if it was watched, so it counts
                    # as missed: a lost alert costs more than a stale one.
                    record_stream_offline(username, only_if_open=True)
                    # A held item waits for its streamer's offline edge (O1
                    # (a)). A fresh start, or Slot mode turned off after an
                    # offline edge it took, never sees that edge, so a poll
                    # that finds the streamer offline releases it as the
                    # check. A failed re-auth poll is not a poll (rule 7).
                    if (not self.slot_active and self._poll_authoritative
                            and self._queue_held_check(username)):
                        offer_held_checks = True

        if not self.slot_active and self._poll_authoritative:
            # A streamer taken off the list has no offline edge to wait for.
            with self._vod_lock:
                unlisted = [login for login in self.held_save_items if login not in self.streamers]
            for login in sorted(unlisted):
                if self._queue_held_check(login):
                    offer_held_checks = True

        if offer_held_checks and not self.effectively_paused:
            self._offer_rescue_or_flush(set(self.live_streamers))

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
        # The scheduler runs after every poll; a poll that failed re-auth is
        # not authoritative (rule 7). A scheduler error is logged as such,
        # never counted as an API failure by the loop.
        authoritative = self._poll_authoritative
        self._poll_authoritative = False
        self._safe_slot_tick(authoritative=authoritative)

    def _queue_held_check(self, username: str) -> bool:
        """At a streamer's offline edge (Slot mode off), their held save item
        becomes a check of the save-streak page (O1 (a)): queued with
        verify true. Returns True when an entry was queued."""
        now = _streak_clock()
        with self._vod_lock:
            held = self.held_save_items.pop(username, None)
            if held is None:
                return False
            self._held_dirty = True
            item = dict(held, verify=True)
            existing = self.queued_vods.get(username)
            if existing is not None:
                entry, _ = self._merge_queued_vod(existing, item, now)
                entry["verify"] = True
                entry["item"]["verify"] = True
            else:
                entry = streak_saves.item_to_queued_vod(item)
            self.queued_vods[username] = entry
            snapshot = dict(self.queued_vods)
        ConfigRequestHandler.config_data["queued_vods"] = snapshot
        log.info("Save-streak check for %s queued: their broadcast ended", username)
        log_activity("vod_queued", streamer=username, url=entry["url"], reason="held_check")
        return True

    # -- the monitor loop -----------------------------------------------------

    def _loop_current(self, gen: int) -> bool:
        return self.running and self._loop_gen == gen

    def _note_loop_gap(self, gap: float) -> None:
        """A loop iteration took more than twice check_interval: the system
        likely slept. The sleep (the gap less one interval) shifts the slot
        clocks on the next tick, and every run of unbroken watching
        restarts (sleep never counts as watch time)."""
        expected_gap = self.config.check_interval
        log.warning(
            "Long loop gap: %.1fs (expected ~%ds, system likely slept)",
            gap, expected_gap,
        )
        log_activity(
            "wake_detected",
            gap_seconds=round(gap, 1),
            expected_seconds=expected_gap,
        )
        self._pending_wake_gap += max(0.0, gap - expected_gap)
        self._watch_start = {}

    def _monitor_loop(self):
        # The generation this loop belongs to: start() stamps it on the
        # thread; a loop from an older start() stops at its next check.
        gen = getattr(threading.current_thread(), "_sm_loop_gen", self._loop_gen)
        self._loop_alive_gen = gen
        log.info("Monitor loop started (interval: %ds)", self.config.check_interval)
        last_iteration_mono = time.monotonic()
        try:
            while self._loop_current(gen):
                # Detect long gaps that suggest the system was asleep/hibernating.
                # Helpful for cross-checking missed streams against power events.
                now_mono = time.monotonic()
                gap = now_mono - last_iteration_mono
                last_iteration_mono = now_mono
                if gap > self.config.check_interval * 2:
                    self._note_loop_gap(gap)

                # Items submitted while this loop polled, or while the
                # monitor was stopped, first.
                self._drain_inbox()
                if not self._loop_current(gen):
                    break

                try:
                    current_status = self.check_streams()
                    if not self._loop_current(gen):
                        break
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

                if not self._loop_current(gen):
                    break
                # Rescue-offer watchdog: runs even when the API check above
                # failed, so a network blip can't strand an unacked offer.
                try:
                    self._maybe_fallback_rescue()
                except Exception as e:
                    log.error("Rescue fallback check failed: %s", e)
                # The slot watchdog: executor liveness, the startup grace and
                # absent-mode opens need no fresh poll.
                self._safe_slot_tick()

                self._wait_between_polls(gen)
        finally:
            if self._loop_alive_gen == gen:
                self._loop_alive_gen = None

    def _wait_between_polls(self, gen: int) -> None:
        """Wait check_interval seconds in slices of at most 1 s. A wake
        (an inbox item, stop()) drains the inbox and replans at once, and
        the scheduler re-evaluates at least every SLOT_TICK_MAX_GAP_SECONDS
        (rule 45)."""
        deadline = time.monotonic() + self.config.check_interval
        while self._loop_current(gen):
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                return
            next_tick_in = self._last_slot_tick_mono + SLOT_TICK_MAX_GAP_SECONDS - now
            if next_tick_in <= 0:
                self._safe_slot_tick()
                continue
            if self._wake.wait(max(0.01, min(1.0, remaining, next_tick_in))):
                self._wake.clear()
                if not self._loop_current(gen):
                    return
                self._drain_inbox()

    def _safe_slot_tick(self, authoritative: bool = False, quiet: bool = False) -> None:
        try:
            if quiet:
                self._slot_tick(authoritative=authoritative, quiet=True)
            else:
                self._slot_tick(authoritative=authoritative)
        except Exception as e:
            # Counted as a tick, so the wait loop does not retry at once.
            self._last_slot_tick_mono = time.monotonic()
            log.error("Slot tick failed: %s", e, exc_info=True)

    def submit(self, kind: str, payload: dict) -> _InboxItem:
        """Called on an HTTP thread (through the registered submitter): put
        an item on the inbox and wake the loop. The handler may wait only
        while this monitor's loop runs with a current generation (A6)."""
        item = _InboxItem(kind, payload,
                          waitable=self.running and self._loop_alive_gen == self._loop_gen)
        self._inbox.put(item)
        self._wake.set()
        return item

    def _drain_inbox(self) -> None:
        """Process every queued inbox item on this (the monitor) thread,
        replan once, then release the waiting handlers."""
        items = []
        while True:
            try:
                items.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        if not items:
            return
        try:
            with self._slot_lock:
                for item in items:
                    try:
                        item.result = self._process_inbox_item(item)
                    except Exception as e:
                        log.error("Inbox item %s failed: %s", item.kind, e, exc_info=True)
                        item.result = False
                self._safe_slot_tick()
        finally:
            for item in items:
                item.done.set()

    def _process_inbox_item(self, item: _InboxItem) -> bool:
        payload = item.payload if isinstance(item.payload, dict) else {}
        if item.kind == "streak_item":
            save_item = streak_saves.clean_item(payload.get("item"))
            if save_item is None:
                return False
            merge_only = payload.get("merge_only") is True
            if self.slot_active:
                changed, events = self.slot.add_item(
                    save_item, self._slot_inputs(False, for_tick=False), merge_only)
                self._log_slot_events(events)
                return bool(changed)
            return self._normal_save_item(save_item, merge_only=merge_only, reason="card")
        if item.kind == "item_done":
            login = payload.get("login")
            if not isinstance(login, str):
                return False
            if self.slot_active:
                completed, events = self.slot.complete_item(
                    login, str(payload.get("reason") or "already_saved"), _streak_clock())
                self._log_slot_events(events)
                return bool(completed)
            return self._normal_item_done(login, payload.get("saved_at"))
        if item.kind == "report":
            return True
        log.warning("Unknown inbox item kind: %r", item.kind)
        return False

    # -- the Slot mode tick ----------------------------------------------------

    def _slot_inputs(self, authoritative: bool, for_tick: bool = True) -> dict:
        """The scheduler's inputs (plan 3.13). for_tick False builds them for
        add_item: nothing is drained or consumed."""
        now = _streak_clock()
        mono = time.monotonic()
        rank: dict[str, int] = {}
        for name in self.config.streamers:
            if isinstance(name, str):
                rank.setdefault(name.lower(), len(rank))
        listed = frozenset(rank)
        pinned = frozenset(
            name.lower() for name in (self.config.pinned_streamers or []) if isinstance(name, str)
        ) & listed
        poll = None
        if authoritative:
            poll = {name: started for name, started in self.live_started_at.items()
                    if name in self.streamers}
        reports = {}
        for key, rep in extension_open_tabs_snapshot().items():
            reports[key] = {
                "browser": rep.get("browser", key),
                "streamers": rep.get("streamers", frozenset()),
                "epoch": rep.get("epoch"),
                "mono": rep.get("mono"),
                "plan_seq": rep.get("plan_seq"),
                "busy": rep.get("busy"),
            }
        gone = []
        wake_gap = 0.0
        seed_capable = False
        absorb = {}
        if for_tick:
            gone = drain_open_tabs_gone()
            wake_gap = self._pending_wake_gap
            self._pending_wake_gap = 0.0
            seed_capable = self._seed_capable_pending
            self._seed_capable_pending = False
            absorb = self._absorb_snapshot(now)
        return {
            "now": now,
            "mono": mono,
            "authoritative": bool(authoritative),
            "poll": poll,
            "live_as_of": self._live_as_of,
            "check_interval": self.config.check_interval,
            "rank": rank,
            "listed": listed,
            "pinned": pinned,
            "slot_mode": bool(self.config.slot_mode),
            "auto_save": bool(self.config.auto_save_streaks),
            "K": self.config.keep_open_slots,
            "C": self.config.cycle_slots,
            "M": self.config.slot_minutes * 60,
            "paused": self.effectively_paused,
            "auto_paused": self.auto_paused,
            "reports": reports,
            "gone": gone,
            "default_browser": default_browser_family(),
            "saves": _counting_saves(now),
            "watch_start": dict(self._watch_start),
            "wake_gap": wake_gap,
            "seed_capable": seed_capable,
            "absorb": absorb,
        }

    def _absorb_snapshot(self, now: float) -> dict:
        """What Slot mode absorbs when it becomes active (DESIGN S2a): the
        VOD queue, the held items, a pending offer, and the last acked offer
        while it is younger than LAST_ACKED_OFFER_TTL_SECONDS."""
        with self._vod_lock:
            vods = {k: dict(v) for k, v in self.queued_vods.items() if isinstance(v, dict)}
            held = {k: dict(v) for k, v in self.held_save_items.items()}
        with self._rescue_lock:
            pending = dict(self.rescue_pending) if self.rescue_pending else None
            last = self._last_acked_offer
            if last is not None and now - last["acked_at"] >= LAST_ACKED_OFFER_TTL_SECONDS:
                self._last_acked_offer = last = None
            last = dict(last) if last is not None else None
        return {"queued_vods": vods, "held": held, "pending_offer": pending,
                "last_acked_offer": last}

    def _slot_tick(self, authoritative: bool = False, quiet: bool = False):
        """Run the scheduler once and apply what it returns: log its events
        in order, withdraw and clear what an activation absorbed, run the
        off transition, enqueue absent-mode opens, publish the plan in one
        assignment, persist slot_state.json, show notices and the tooltip
        (not when quiet). A loop of an older generation does nothing.
        Returns the TickResult, or None when skipped."""
        gen = getattr(threading.current_thread(), "_sm_loop_gen", None)
        if gen is not None and gen != self._loop_gen:
            return None
        with self._slot_lock:
            inp = self._slot_inputs(authoritative)
            result = self.slot.tick(inp)
            self._last_slot_tick_mono = time.monotonic()
            self.slot_state = result.state
            if result.absorbed:
                self._absorb_for_slot_mode()
            self._log_slot_events(result.events)
            if result.off_transition is not None:
                self._apply_off_transition(result.off_transition)
            for op in result.opens:
                self._enqueue_tab_open(op["kind"], op["streamer"], op["url"],
                                       queue_reason="slot_fallback")
            ConfigRequestHandler.config_data["slot_plan"] = result.plan
            # Also refreshed while nothing changes (and after a clock step
            # back), so saved_at stays current for a quick restart.
            last = self._last_slot_persist
            if (result.persist or self._held_dirty or last is None
                    or not 0 <= inp["now"] - last < SLOT_STATE_REFRESH_SECONDS):
                self._persist_slot_state(inp["now"])
            if quiet:
                return result
            self._show_slot_notices(result.notices)
            if self.config.slot_mode and result.tooltip:
                self.status_callback(result.tooltip)
            return result

    def prime_slot_plan(self) -> None:
        """At launch, before the config server answers its first request:
        load slot_state.json and publish a plan, so /config never serves a
        null plan while Slot mode is on (3.2; a null plan tells the
        extension Slot mode is off, and it would strip its slot markers).
        Quiet: start() reloads and ticks for real before the first poll
        (A10), and its tick shows the notices and the tooltip."""
        self._load_slot_state()
        self._safe_slot_tick(quiet=True)

    def _log_slot_events(self, events) -> None:
        for name, fields in events:
            log_activity(name, **fields)
            log.debug("Slot: %s %s", name, fields)

    def _absorb_for_slot_mode(self) -> None:
        """Slot mode became active and the scheduler took in the leftovers
        (rule 33): withdraw a pending rescue offer, and clear the VOD queue,
        the missed-while-paused list, the held items and the last acked
        offer, so nothing is absorbed twice or opened the 1.11 way."""
        with self._rescue_lock:
            offer = self.rescue_pending
            self.rescue_pending = None
        ConfigRequestHandler.config_data["rescue"] = None
        if offer:
            log.info("Rescue offer %s withdrawn: Slot mode is active", offer["id"])
            log_activity("rescue_withdrawn", offer_id=offer["id"], reason="slot_mode")
        with self._vod_lock:
            self.queued_vods.clear()
            self.missed_while_paused.clear()
            if self.held_save_items:
                self.held_save_items = {}
                self._held_dirty = True
            with self._rescue_lock:
                self._last_acked_offer = None
        ConfigRequestHandler.config_data["queued_vods"] = {}

    def _apply_off_transition(self, off: dict) -> None:
        """Slot mode was turned off (rule 46): every live stream without a
        tab, except one dismissed for its current broadcast, opens the 1.11
        way (paced), or is recorded as missed while paused; pending save
        items move to the normal path."""
        opened = []
        for login in off.get("open_live") or []:
            state = self.streamers.get(login)
            if state is None:
                continue
            if self.effectively_paused:
                with self._vod_lock:
                    self.missed_while_paused[login] = time.strftime("%H:%M:%S")
                state.browser_opened = False
                log_activity("tab_open_skipped", streamer=login,
                             reason="auto_paused" if self.auto_paused else "paused")
            else:
                state.browser_opened = True
                self.open_stream(login)
                opened.append(login)
        if opened:
            log.info("Slot mode off: opening %d live stream(s) without a tab: %s",
                     len(opened), opened)
        with self._vod_lock:
            queued_before = set(self.queued_vods)
        for item in off.get("items") or []:
            clean = streak_saves.clean_item(item)
            if clean is not None:
                self._normal_save_item(clean, reason="slot_mode_off", offer=False)
        with self._vod_lock:
            queued_new = set(self.queued_vods) - queued_before
        if queued_new and not self.effectively_paused:
            self._offer_rescue_or_flush(set(self.live_streamers))

    def _show_slot_notices(self, notices) -> None:
        k, c, m = self.config.keep_open_slots, self.config.cycle_slots, self.config.slot_minutes
        for notice in notices:
            if notice == "activated":
                self.notify_callback(
                    "Stream Monitor",
                    f"Slot mode on: {k} Keep Open + {c} rotating, {m} min per turn",
                )
            elif notice == "outage":
                self.notify_callback(
                    "Stream Monitor",
                    "Slot mode: the browser extension stopped reporting. "
                    f"Opening at most {k + c} streams until it is back.",
                )
            elif notice == "none_ever":
                if self._never_seen_notified:
                    continue
                self._never_seen_notified = True
                self.notify_callback(
                    "Stream Monitor",
                    "Slot mode needs browser extension 1.12 or newer. "
                    "Opening streams normally until it connects.",
                )

    def _slot_state_path(self) -> Path:
        return CONFIG_DIR / "slot_state.json"

    def _persist_slot_state(self, now: float) -> None:
        """Write slot_state.json atomically (tmp plus _replace_with_retry):
        the scheduler's state and the held items. Never raises. The callers
        hold _slot_lock, also through the replace retries."""
        data = self.slot.to_state(now)
        with self._vod_lock:
            data["held"] = {k: dict(v) for k, v in self.held_save_items.items()}
            self._held_dirty = False
        path = self._slot_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _replace_with_retry(tmp, path)
            self._last_slot_persist = now
        except (OSError, TypeError, ValueError) as e:
            log.warning("Could not write %s: %s", path, e)

    def _load_slot_state(self) -> None:
        """A fresh start: restore the scheduler and the held items from
        slot_state.json (a missing or malformed file means an empty state),
        restart the 90 s grace, and seed the capability flag from a recent
        report of a capable extension (A24)."""
        now = _streak_clock()
        try:
            data = json.loads(self._slot_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("v") != 1:
            data = {}
        with self._slot_lock:
            self.slot.load_state(data, now)
            held = {}
            raw_held = data.get("held")
            for login, raw in (raw_held.items() if isinstance(raw_held, dict) else ()):
                if not (isinstance(login, str) and _OPEN_TABS_LOGIN_RE.match(login)):
                    continue
                item = streak_saves.clean_item(raw, login)
                if item is not None and not streak_saves.item_expired(item, now):
                    held[login] = item
            with self._vod_lock:
                self.held_save_items = held
            self._seed_capable_pending = (
                not self.slot.executor_seen
                and persisted_capable_report_within(slot_scheduler.SLOT_STATE_MAX_AGE_SECONDS)
            )
            self.slot.reset_startup(time.monotonic())
            self.slot_state = "off"

    def start(self, preserve_state: bool = False) -> bool:
        """Begin monitoring. With preserve_state, streamers still on the
        list keep their live/opened state across the restart a settings
        change triggers, so a stream that is live with a tab open is not
        opened a second time (the old restart reset every streamer to
        "never seen" and re-opened everything live). A manual Start from
        the tray still begins from a clean slate, and reloads Slot mode's
        state from slot_state.json (rule 38)."""
        log.info("Starting monitor...")
        if not self.config.is_valid():
            log.error("Cannot start: config is invalid (missing client_id, client_secret, or streamers)")
            self.status_callback("Invalid config")
            return False

        # A loop left over from before (stop() waits only 2 s for it) is no
        # longer current from here on, so it cannot tick while this start
        # reloads the Slot mode state.
        self._loop_gen += 1
        gen = self._loop_gen
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
        set_polled_streamers(self.streamers)
        if not preserve_state:
            self._seed_startup_open_tabs()
            # A fresh start (a launch, or Stop then Start from the tray):
            # nothing polled yet, so no run of watching and no live starts.
            self._watch_start = {}
            self._last_poll_ok_mono = None
            self.live_started_at = {}
            self._live_as_of = None
            self._load_slot_state()

        self.running = True
        self.status_callback("Monitoring...")
        # The first tick runs before the first poll, so that poll already
        # knows whether Slot mode is active (A10).
        self._safe_slot_tick()
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread._sm_loop_gen = gen
        self.thread.start()
        return True

    def stop(self):
        log.info("Stopping monitor")
        was_running = self.running
        self.running = False
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        if was_running:
            # Stamp slot_state.json with the stop time, so a start soon
            # after (a tray Start, the update relaunch) restores the slots.
            # A stop of a monitor that was not running writes nothing: that
            # would give a state from an earlier stop a fresh saved_at.
            try:
                with self._slot_lock:
                    self._persist_slot_state(_streak_clock())
            except Exception as e:
                log.warning("Could not save the Slot mode state at stop: %s", e)
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
        gone), the stream is opened after all if it is still live. In Slot
        mode the plan decides what opens, so the skips are dropped
        unopened."""
        if not self._startup_skipped:
            return
        if self.slot_active:
            log.info("Startup skips dropped unopened (Slot mode runs the plan): %s",
                     sorted(self._startup_skipped))
            self._startup_skipped.clear()
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
                with self._vod_lock:
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
        still on the list so nothing already open is opened again. The
        Slot mode scheduler is kept as it is (rule 39): new counts, minutes,
        ranks and pins apply at the next tick."""
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
        register_secret(self.config.client_secret)
        self._ensure_install_id()
        self.monitor: Optional[TwitchMonitor] = None
        self.icon: Optional[pystray.Icon] = None
        self.status = "Starting..."

    def update_status(self, status: str):
        # The tooltip is on screen, and the owner streams: same safety net
        # as the log.
        status = redact_secrets(status)
        self.status = status
        if self.icon:
            # Windows tray tooltip is limited to 128 characters
            title = f"Stream Monitor - {status}"
            if len(title) > 127:
                title = title[:124] + "..."
            self.icon.title = title

    def send_notification(self, title: str, message: str):
        """Send a system tray notification."""
        title, message = redact_secrets(title), redact_secrets(message)
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
        # Turning Slot mode on or off takes effect on the monitor thread at
        # the next tick (the restart below runs one), never here.
        self.config = new_config
        ConfigRequestHandler.config_data.update({
            "streamers": self.config.streamers,
            "pinned_streamers": self.config.pinned_streamers,
            "version": VERSION,
            "auto_save_streaks": self.config.auto_save_streaks,
        })
        log_activity(
            "config_loaded",
            streamers=list(self.config.streamers),
            pinned_streamers=list(self.config.pinned_streamers),
            interval=self.config.check_interval,
            reason="settings_changed",
            **_slot_config_fields(self.config),
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
                lambda item: f"Queued VODs ({self._queued_vod_count()})",
                pystray.Menu(lambda: tuple(self._iter_queued_vod_menu_items())),
                visible=lambda item: self._queued_vod_count() > 0,
            ),
            Item("Start", self.on_start, checked=lambda item: self._is_running()),
            Item("Stop", self.on_stop, checked=lambda item: not self._is_running()),
            pystray.Menu.SEPARATOR,
            Item("View Logs", self.on_view_logs),
            Item("About (CaedVT)", self.on_about),
            Item("Exit", self.on_exit)
        )

    def _queued_vod_count(self) -> int:
        """How many save-streak links wait in the VOD queue (the submenu
        hides itself at 0, as it always is in Slot mode)."""
        if not self.monitor:
            return 0
        with self.monitor._vod_lock:
            return len(self.monitor.queued_vods)

    def _iter_queued_vod_menu_items(self):
        """Yield one menu item per queued VOD, plus a separator and a clear-all
        item at the bottom. pystray re-evaluates this every time the submenu
        opens, so we always show the current queue state.
        """
        snapshot = []
        if self.monitor:
            # Snapshot to avoid mutation during iteration if a flush fires.
            with self.monitor._vod_lock:
                snapshot = list(self.monitor.queued_vods.items())
        if not snapshot:
            yield Item("(none queued)", None, enabled=False)
            return
        for streamer, entry in snapshot:
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
            with self.monitor._vod_lock:
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
        with self.monitor._vod_lock:
            cleared = list(self.monitor.queued_vods.keys())
            self.monitor.queued_vods.clear()
        count = len(cleared)
        for streamer in cleared:
            log_activity("vod_queue_cleared", streamer=streamer)
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
                # Form body, not the URL: exception text shown below would
                # otherwise carry the secret onto the screen.
                resp = requests.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"},
                    timeout=10
                )
                if resp.status_code == 200:
                    test_label.config(text="Connection successful!", foreground="green")
                else:
                    test_label.config(text="Authentication failed. Check credentials.", foreground="red")
            except Exception as e:
                # Type and status only: this label is on screen.
                test_label.config(text=f"Connection error: {_token_failure_summary(e)}", foreground="red")

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

    def start_services(self) -> bool:
        """Everything run() does before the tray icon's loop: the state
        loads, the config server, the monitor and the hooks the HTTP
        handlers use, the watcher threads, the update check and the welcome
        page. Returns False when the app must exit instead (first-time setup
        cancelled, or another instance holds the port). The real-app test
        harness calls it with no icon (A33)."""
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
                return False

        log_activity(
            "config_loaded",
            streamers=list(self.config.streamers),
            pinned_streamers=list(self.config.pinned_streamers),
            interval=self.config.check_interval,
            **_slot_config_fields(self.config),
        )

        # Streaks Twitch already confirmed as kept, and broadcast times, from
        # the previous run: /config publishes them from its first answer.
        # The monitor starts a moment later; until then the configured list
        # stands in for the polled set, so listed streamers' saves are not
        # cut to the 24-hour limit for logins nobody polls.
        set_polled_streamers(self.config.streamers)
        load_streak_state()
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
            return False

        # Create monitor with notification callback. Its start() loads
        # slot_state.json before the first poll. The port is bound already
        # (a request waits in the backlog), so the plan is published before
        # the server answers anything.
        self.monitor = TwitchMonitor(self.config, self.update_status, self.send_notification)
        self.monitor.prime_slot_plan()
        threading.Thread(target=lambda: run_config_server(config_server), daemon=True).start()

        # Allow the HTTP handler (POST /streak_event) to raise tray
        # notifications without holding a direct reference to the app.
        set_tray_notifier(self.send_notification)

        # Allow POST /rescue_ack to hand rescue ownership to the extension.
        # The claim handler also names the claimant, so the same browser
        # profile can re-ack an offer whose first answer it lost.
        set_rescue_ack_handler(
            lambda offer_id: bool(self.monitor and self.monitor.acknowledge_rescue(offer_id))
        )
        set_rescue_claim_handler(
            lambda offer_id, claimant: bool(
                self.monitor and self.monitor.acknowledge_rescue(offer_id, claimant))
        )
        # Streak events and tab reports reach the monitor thread through its
        # inbox; the card verdict reads the monitor's runs of watching.
        set_monitor_submitter(
            lambda kind, payload: self.monitor.submit(kind, payload) if self.monitor else None
        )
        set_watch_start_provider(
            lambda name: self.monitor.watch_start_for(name) if self.monitor else None
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
        return True

    def run(self):
        # The icon object exists before the monitor starts, so the first
        # status lands in its tooltip; its window is created only by
        # icon.run().
        self.icon = pystray.Icon(
            "stream_monitor",
            create_icon_image("purple"),
            "Stream Monitor",
            self.create_menu()
        )
        if not self.start_services():
            return

        # Run the icon (blocking)
        self.icon.run()

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
