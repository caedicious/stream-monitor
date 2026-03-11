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
VERSION = "1.3.2"
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
        "version": VERSION
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
    
    def __post_init__(self):
        if self.streamers is None:
            self.streamers = []
    
    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                return cls(**data)
            except (json.JSONDecodeError, TypeError):
                pass
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
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    
    def __init__(self, config: Config, status_callback: Callable[[str], None] = None):
        self.config = config
        self.oauth_token: Optional[str] = None
        self.streamers: dict[str, StreamerState] = {}
        self.running = False
        self.paused = False
        self.thread: Optional[threading.Thread] = None
        self.status_callback = status_callback or (lambda x: None)
        
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
    
    def check_streams(self) -> dict[str, bool]:
        if not self.streamers:
            return {}
        
        params = [("user_login", name) for name in self.streamers.keys()]
        
        try:
            response = requests.get(
                self.TWITCH_API_URL,
                headers=self._get_headers(),
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            live_streamers = {
                stream["user_login"].lower() 
                for stream in data.get("data", [])
            }
            
            return {name: name in live_streamers for name in self.streamers}
            
        except requests.RequestException as e:
            self.status_callback(f"API error: {e}")
            return {name: False for name in self.streamers}
    
    def open_stream(self, username: str):
        url = f"https://twitch.tv/{username}"
        webbrowser.open(url)
    
    def process_state_changes(self, current_status: dict[str, bool]):
        live_count = 0
        for username, is_live in current_status.items():
            state = self.streamers[username]

            if is_live:
                live_count += 1
                if not state.was_live and not state.browser_opened:
                    if self.paused:
                        self.status_callback(f"{username} went LIVE! (paused)")
                    else:
                        self.status_callback(f"{username} went LIVE!")
                        self.open_stream(username)
                        state.browser_opened = True
                state.was_live = True
            else:
                if state.was_live:
                    self.status_callback(f"{username} went offline")
                    state.was_live = False
                    state.browser_opened = False

        if self.paused:
            if live_count > 0:
                self.status_callback(f"PAUSED — {live_count} streamer(s) live")
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
        self.paused = self.config.paused
        
    def update_status(self, status: str):
        self.status = status
        if self.icon:
            self.icon.title = f"Stream Monitor - {status}"
    
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
                    ConfigRequestHandler.config_data = {
                        "streamers": self.config.streamers,
                        "version": VERSION
                    }
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
    
    def on_pause_toggle(self, icon, item):
        """Toggle pause state."""
        self.paused = not self.paused
        self.config.paused = self.paused
        self.config.save()
        if self.monitor:
            self.monitor.paused = self.paused
        if self.paused:
            self.update_status("Paused")
            self.icon.icon = create_icon_image("gray")
        else:
            self.icon.icon = create_icon_image("purple")
            if self.monitor and self.monitor.running:
                self.update_status("Monitoring...")
            else:
                self.update_status("Stopped")
        # Rebuild menu to update checkmark
        self.icon.menu = self.create_menu()

    def on_about(self, icon, item):
        """Open the about page in the browser."""
        webbrowser.open(f"http://127.0.0.1:{CONFIG_SERVER_PORT}/about?v={VERSION}")

    def on_exit(self, icon, item):
        if self.monitor:
            self.monitor.stop()
        icon.stop()
    
    def create_menu(self):
        return pystray.Menu(
            Item("Settings", self.on_settings),
            Item("Check for Updates", self.on_check_updates),
            pystray.Menu.SEPARATOR,
            Item("Pause", self.on_pause_toggle, checked=lambda item: self.paused),
            Item("Start", self.on_start),
            Item("Stop", self.on_stop),
            pystray.Menu.SEPARATOR,
            Item("About (CaedVT)", self.on_about),
            Item("Exit", self.on_exit)
        )
    
    def run(self):
        # Start config server for browser extension
        threading.Thread(target=lambda: start_config_server(self.config), daemon=True).start()
        
        # Create monitor
        self.monitor = TwitchMonitor(self.config, self.update_status)
        self.monitor.paused = self.paused

        # Create system tray icon (gray if paused)
        icon_color = "gray" if self.paused else "purple"
        self.icon = pystray.Icon(
            "stream_monitor",
            create_icon_image(icon_color),
            "Stream Monitor",
            self.create_menu()
        )

        # Auto-start if config is valid
        if self.config.is_valid():
            threading.Thread(target=lambda: time.sleep(1) or self.monitor.start(), daemon=True).start()
        else:
            self.update_status("Not configured")

        # Show warning if paused
        if self.paused:
            self.update_status("Paused")
            threading.Thread(target=self._show_paused_warning, daemon=True).start()
        
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
    
    def _show_paused_warning(self):
        """Show a warning dialog that stream opening is paused."""
        import ctypes
        time.sleep(2)  # Wait for tray icon to appear
        ctypes.windll.user32.MessageBoxW(
            0,
            "Stream Monitor is currently PAUSED.\n\n"
            "Streams will NOT open automatically when monitored streamers go live.\n\n"
            "Right-click the tray icon and uncheck 'Pause' to resume.",
            "Stream Monitor — Paused",
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
