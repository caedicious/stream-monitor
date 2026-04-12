#!/usr/bin/env python3
"""
Twitch Stream Monitor - System Tray Application
Runs in the background and opens streams when monitored streamers go live.
"""

import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

# Version
VERSION = "1.4.0"
GITHUB_REPO = "caedicious/stream-monitor"
CONFIG_SERVER_PORT = 52832  # Arbitrary high port for localhost config server

# Configuration paths
APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()

CONFIG_FILE = CONFIG_DIR / "config.json"


def _get_about_html_path() -> Path:
    """Return the path to about.html, works both frozen (exe) and as script."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "about.html"
    return Path(__file__).parent / "about.html"


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


def start_config_server(config: "Config"):
    """Start a localhost HTTP server to serve config to the browser extension."""
    ConfigRequestHandler.config_data = {
        "streamers": config.streamers,
        "version": VERSION,
        "live_streamers": [],
        "paused": config.paused,
        "auto_paused": False
    }
    
    try:
        server = HTTPServer(("127.0.0.1", CONFIG_SERVER_PORT), ConfigRequestHandler)
        server.serve_forever()
    except OSError as e:
        print(f"Config server failed to start: {e}")


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""
    streamers: list = None
    check_interval: int = 60
    last_run_version: str = ""
    paused: bool = False
    own_channel: str = ""
    im_live_pause: bool = False
    vod_fallback: bool = False
    
    def __post_init__(self):
        if self.streamers is None:
            self.streamers = []
    
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
                print(f"Config load error: {e}")
                print(f"Config file path: {CONFIG_FILE}")
        else:
            print(f"Config file not found: {CONFIG_FILE}")
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
        
    def _get_oauth_token(self) -> bool:
        try:
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
            return bool(self.oauth_token)
        except requests.RequestException as e:
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
            self.status_callback("Token expired, re-authenticating...")
            if self._get_oauth_token():
                response = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
            else:
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
            data = self._api_get(self.TWITCH_API_URL, params)
            if data is None:
                return {name: False for name in self.streamers}

            live_set = set()
            for stream in data.get("data", []):
                login = stream["user_login"].lower()
                live_set.add(login)
                # Cache user IDs from stream responses
                if "user_id" in stream:
                    self.user_ids[login] = stream["user_id"]

            # Update auto-pause based on user's own channel
            if own_channel and self.config.im_live_pause:
                was_auto_paused = self.auto_paused
                self.auto_paused = own_channel in live_set
                if self.auto_paused and not was_auto_paused:
                    self.status_callback("Auto-paused (you're live)")
                    self.notify_callback("Stream Monitor", "Auto-paused because you're live!")
                elif not self.auto_paused and was_auto_paused:
                    self.status_callback("Resumed (you went offline)")
                    self.notify_callback("Stream Monitor", "You went offline, resuming monitoring!")

            # Update live streamers list for config server
            self.live_streamers = [name for name in self.streamers if name in live_set]

            return {name: name in live_set for name in self.streamers}

        except requests.RequestException as e:
            self.status_callback(f"API error: {e}")
            return {name: False for name in self.streamers}

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
        webbrowser.open(url)
    
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
                    # Desktop notification for all live events
                    self.notify_callback(
                        "Stream Monitor",
                        f"{username} is now live on Twitch!"
                    )

                    if self.effectively_paused:
                        self.status_callback(f"{username} went LIVE! (paused)")
                        # Track missed streams while paused
                        self.missed_while_paused[username] = time.strftime("%H:%M:%S")
                    else:
                        self.status_callback(f"{username} went LIVE!")
                        self.open_stream(username)
                        state.browser_opened = True
                state.was_live = True
            else:
                if state.was_live:
                    self.status_callback(f"{username} went offline")

                    # VOD fallback: if stream was missed, open the VOD
                    if not state.browser_opened and self.config.vod_fallback:
                        vod_url = self.get_latest_vod_url(username)
                        if vod_url:
                            self.status_callback(f"Opening VOD for {username}")
                            webbrowser.open(vod_url)

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
        while self.running:
            current_status = self.check_streams()
            if current_status:
                self.process_state_changes(current_status)
            
            for _ in range(self.config.check_interval):
                if not self.running:
                    break
                time.sleep(1)
    
    def start(self) -> bool:
        if not self.config.is_valid():
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
        
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self.status_callback("Monitoring...")
        return True
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.status_callback("Stopped")
    
    def restart(self):
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
            self.icon.title = f"Stream Monitor - {status}"

    def send_notification(self, title: str, message: str):
        """Send a system tray notification."""
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception as e:
                print(f"Notification failed: {e}")
    
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
                        "version": VERSION
                    })
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
            print(f"Update check failed: {e}")
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
        if self.monitor:
            self.monitor.stop()
        icon.stop()
    
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
        # If config is missing or has no credentials, run first-time setup
        if not self.config.is_valid():
            if not self._run_first_time_setup():
                return

        # Start config server for browser extension
        threading.Thread(target=lambda: start_config_server(self.config), daemon=True).start()

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
