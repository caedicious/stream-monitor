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

import requests
import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

# Configuration paths
APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()

CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""
    streamers: list = None
    check_interval: int = 60
    
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
                    self.status_callback(f"{username} went LIVE!")
                    self.open_stream(username)
                    state.browser_opened = True
                state.was_live = True
            else:
                if state.was_live:
                    self.status_callback(f"{username} went offline")
                    state.was_live = False
                    state.browser_opened = False
        
        if live_count > 0:
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
    
    def on_exit(self, icon, item):
        if self.monitor:
            self.monitor.stop()
        icon.stop()
    
    def create_menu(self):
        return pystray.Menu(
            Item("Settings", self.on_settings),
            Item("Start", self.on_start),
            Item("Stop", self.on_stop),
            pystray.Menu.SEPARATOR,
            Item("Exit", self.on_exit)
        )
    
    def run(self):
        # Create monitor
        self.monitor = TwitchMonitor(self.config, self.update_status)
        
        # Create system tray icon
        self.icon = pystray.Icon(
            "stream_monitor",
            create_icon_image("purple"),
            "Stream Monitor",
            self.create_menu()
        )
        
        # Auto-start if config is valid
        if self.config.is_valid():
            threading.Thread(target=lambda: time.sleep(1) or self.monitor.start(), daemon=True).start()
        else:
            self.update_status("Not configured")
        
        # Run the icon (blocking)
        self.icon.run()


def main():
    app = StreamMonitorApp()
    app.run()


if __name__ == "__main__":
    main()
