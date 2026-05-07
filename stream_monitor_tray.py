#!/usr/bin/env python3
"""
Twitch Stream Monitor - System Tray Application
Runs in the background and opens streams when monitored streamers go live.
"""

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler

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
VERSION = "1.5.6"
GITHUB_REPO = "caedicious/stream-monitor"
CONFIG_SERVER_PORT = 52832  # Arbitrary high port for localhost config server

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


def read_activity_log(limit: Optional[int] = None) -> list:
    """Read the activity log into a list of dicts. Reverse-chronological.

    Tolerates partial last lines from concurrent writes.
    """
    if not STREAM_ACTIVITY_FILE.exists():
        return []
    events = []
    try:
        with open(STREAM_ACTIVITY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Partial line from a concurrent write, skip it.
                    continue
    except OSError as e:
        log.warning("Failed to read activity log: %s", e)
        return []
    events.reverse()
    if limit is not None:
        events = events[:limit]
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
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def log_message(self, format, *args):
        """Suppress logging."""
        pass


class _SingletonHTTPServer(HTTPServer):
    """HTTPServer subclass with SO_REUSEADDR disabled.

    Python's stock HTTPServer sets allow_reuse_address = 1, which on Windows
    has SO_REUSEADDR semantics that *permit two processes to bind the same
    port simultaneously*. We rely on the bind failing when another instance
    holds the port to enforce single-instance startup, so we have to
    explicitly opt out of address reuse here.
    """
    allow_reuse_address = False


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
        "auto_paused": False
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
        self.oauth_token: Optional[str] = None
        self.streamers: dict[str, StreamerState] = {}
        self.running = False
        self.paused = False
        self.auto_paused = False  # True when user's own channel is live
        self.thread: Optional[threading.Thread] = None
        self.status_callback = status_callback or (lambda x: None)
        self.notify_callback = notify_callback or (lambda t, m: None)
        self.live_streamers: list[str] = []  # Current live streamers for config server
        self.missed_while_paused: dict[str, str] = {}  # { streamer: went_live_time }
        self.user_ids: dict[str, str] = {}  # { username: user_id } cache
        self.consecutive_errors: int = 0  # Track consecutive API failures
        # Per-streamer metadata captured on the latest "live" check, used to
        # enrich the activity log (title, game, viewer count at the moment
        # the offline->live transition was detected).
        self.live_stream_meta: dict[str, dict] = {}
        
    def _get_oauth_token(self) -> bool:
        try:
            log.info("Requesting OAuth token")
            response = requests.post(
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
        response = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
        if response.status_code == 401:
            log.warning("API returned 401, token expired. Re-authenticating...")
            self.status_callback("Token expired, re-authenticating...")
            if self._get_oauth_token():
                response = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
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

            # Update live streamers list for config server
            self.live_streamers = [name for name in self.streamers if name in live_set]

            return {name: name in live_set for name in self.streamers}

        except requests.RequestException as e:
            log.error("API request failed: %s", e)
            # Truncate error message for tray tooltip (128 char Windows limit)
            err_short = str(e)[:80]
            self.status_callback(f"API error: {err_short}")
            raise  # Let _monitor_loop handle error counting and notifications

    def get_latest_vod_url(self, username: str) -> Optional[str]:
        """Fetch the most recent VOD URL for a user."""
        user_id = self.user_ids.get(username.lower())
        if not user_id:
            # Need to look up user ID first
            try:
                data = self._api_get(self.USERS_API_URL, {"login": username})
                if data and data.get("data"):
                    user_id = data["data"][0]["id"]
                    self.user_ids[username.lower()] = user_id
                else:
                    return None
            except requests.RequestException:
                return None

        try:
            data = self._api_get(self.VIDEOS_API_URL, {
                "user_id": user_id, "type": "archive", "first": "1"
            })
            if data and data.get("data"):
                return data["data"][0].get("url")
        except requests.RequestException:
            pass
        return None
    
    def open_stream(self, username: str):
        url = f"https://twitch.tv/{username}?sm=1"
        log.info("Opening stream tab: %s", url)
        try:
            success = webbrowser.open(url)
        except Exception as e:
            log.error("webbrowser.open raised for %s: %s", url, e)
            success = False
        log_activity(
            "tab_open_attempt",
            kind="stream",
            streamer=username,
            url=url,
            success=bool(success),
        )
    
    @property
    def effectively_paused(self) -> bool:
        """True if paused manually or auto-paused because user is live."""
        return self.paused or self.auto_paused

    def process_state_changes(self, current_status: dict[str, bool]):
        live_count = 0
        # Update config server with live status
        ConfigRequestHandler.config_data["live_streamers"] = self.live_streamers
        ConfigRequestHandler.config_data["paused"] = self.paused
        ConfigRequestHandler.config_data["auto_paused"] = self.auto_paused

        for username, is_live in current_status.items():
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

                    if self.effectively_paused:
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

                    # VOD fallback: if stream was missed, open the VOD
                    if not state.browser_opened and self.config.vod_fallback:
                        log.info("VOD fallback triggered for %s", username)
                        vod_url = self.get_latest_vod_url(username)
                        if vod_url:
                            self.status_callback(f"Opening VOD for {username}")
                            log.info("Opening VOD: %s", vod_url)
                            try:
                                vod_success = webbrowser.open(vod_url)
                            except Exception as e:
                                log.error("webbrowser.open raised for VOD %s: %s", vod_url, e)
                                vod_success = False
                            log_activity(
                                "tab_open_attempt",
                                kind="vod",
                                streamer=username,
                                url=vod_url,
                                success=bool(vod_success),
                            )
                        else:
                            log.info("No VOD found for %s", username)

                    # Track missed streaks: went live + offline while paused
                    if username in self.missed_while_paused:
                        # Will be shown when user unpauses
                        pass  # Keep in missed_while_paused for alert

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

            for _ in range(self.config.check_interval):
                if not self.running:
                    break
                time.sleep(1)
    
    def start(self) -> bool:
        log.info("Starting monitor...")
        if not self.config.is_valid():
            log.error("Cannot start: config is invalid (missing client_id, client_secret, or streamers)")
            self.status_callback("Invalid config")
            return False

        self.status_callback("Authenticating...")
        if not self._get_oauth_token():
            self.status_callback("Auth failed")
            return False

        self.streamers = {
            name.lower(): StreamerState(name=name.lower())
            for name in self.config.streamers
        }
        log.info("Monitoring %d streamer(s): %s", len(self.streamers), list(self.streamers.keys()))

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

    def restart(self):
        log.info("Restarting monitor")
        self.stop()
        time.sleep(0.5)
        self.start()


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
        
        # Start a thread to watch for config changes
        def watch_for_changes():
            import time
            old_config = json.dumps(asdict(self.config), sort_keys=True)
            for _ in range(120):  # Watch for 2 minutes
                time.sleep(2)
                new_config_obj = Config.load()
                new_config = json.dumps(asdict(new_config_obj), sort_keys=True)
                if new_config != old_config:
                    self.config = new_config_obj
                    # Update config server for browser extension
                    ConfigRequestHandler.config_data.update({
                        "streamers": self.config.streamers,
                        "pinned_streamers": self.config.pinned_streamers,
                        "version": VERSION
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
                    break
        
        threading.Thread(target=watch_for_changes, daemon=True).start()
    
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
        """Check for updates with UI feedback."""
        self.update_status("Checking for updates...")
        update_available, latest_version, download_url = self.check_for_updates()
        
        if update_available:
            self.update_status(f"Update available: v{latest_version}")
            # Open browser to releases page
            webbrowser.open(download_url)
        else:
            self.update_status("Up to date!")
            time.sleep(3)
            if self.monitor and self.monitor.running:
                self.update_status("Monitoring...")
            else:
                self.update_status("Stopped")
    
    def check_for_updates(self) -> tuple[bool, str, str]:
        """
        Check GitHub for newer releases.
        Returns: (update_available, latest_version, download_url)
        """
        try:
            response = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            latest_version = data.get("tag_name", "").lstrip("v")
            download_url = data.get("html_url", f"https://github.com/{GITHUB_REPO}/releases")
            
            # Compare versions
            if self._is_newer_version(latest_version, VERSION):
                return True, latest_version, download_url
            
            return False, latest_version, download_url
            
        except requests.RequestException as e:
            log.error("Update check failed: %s", e)
            return False, VERSION, f"https://github.com/{GITHUB_REPO}/releases"
    
    def _is_newer_version(self, latest: str, current: str) -> bool:
        """Compare version strings (e.g., '1.2.0' > '1.1.0')."""
        try:
            latest_parts = [int(x) for x in latest.split(".")]
            current_parts = [int(x) for x in current.split(".")]
            
            # Pad to same length
            while len(latest_parts) < len(current_parts):
                latest_parts.append(0)
            while len(current_parts) < len(latest_parts):
                current_parts.append(0)
            
            return latest_parts > current_parts
        except ValueError:
            return False
    
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
            Item("Start", self.on_start, checked=lambda item: self._is_running()),
            Item("Stop", self.on_stop, checked=lambda item: not self._is_running()),
            pystray.Menu.SEPARATOR,
            Item("View Logs", self.on_view_logs),
            Item("About (CaedVT)", self.on_about),
            Item("Exit", self.on_exit)
        )
    
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
        """Check for updates silently on startup."""
        time.sleep(5)  # Wait a bit after startup
        update_available, latest_version, download_url = self.check_for_updates()
        if update_available:
            self.update_status(f"Update available: v{latest_version}")


def main():
    app = StreamMonitorApp()
    app.run()


if __name__ == "__main__":
    main()
