#!/usr/bin/env python3
"""
Stream Monitor Settings Editor
A simple settings dialog that can be launched from the tray app.
"""

import json
import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

VERSION = "1.4.0"

APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            # Return full dict so we preserve all fields when saving back
            return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"client_id": "", "client_secret": "", "streamers": [], "check_interval": 60}


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def main():
    config = load_config()
    
    dialog = tk.Tk()
    dialog.title(f"Stream Monitor Settings - v{VERSION}")
    dialog.geometry("450x580")
    dialog.resizable(False, False)
    
    # Center window
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() - 450) // 2
    y = (dialog.winfo_screenheight() - 420) // 2
    dialog.geometry(f"+{x}+{y}")
    
    # Make sure window gets focus
    dialog.lift()
    dialog.attributes('-topmost', True)
    dialog.after(100, lambda: dialog.attributes('-topmost', False))
    
    # Main frame with padding
    main_frame = ttk.Frame(dialog, padding=20)
    main_frame.pack(fill=tk.BOTH, expand=True)
    
    # Streamers section
    ttk.Label(main_frame, text="Streamers to Monitor:", font=("", 10, "bold")).pack(anchor=tk.W)
    ttk.Label(main_frame, text="(One per line)", font=("", 8)).pack(anchor=tk.W)
    
    streamers_text = tk.Text(main_frame, height=8, width=40)
    streamers_text.pack(fill=tk.X, pady=(5, 15))
    streamers_text.insert("1.0", "\n".join(config.get("streamers", [])))
    
    # Credentials section
    ttk.Label(main_frame, text="Twitch API Credentials:", font=("", 10, "bold")).pack(anchor=tk.W)
    
    cred_frame = ttk.Frame(main_frame)
    cred_frame.pack(fill=tk.X, pady=5)
    
    ttk.Label(cred_frame, text="Client ID:").grid(row=0, column=0, sticky=tk.W, pady=2)
    client_id_entry = ttk.Entry(cred_frame, width=45)
    client_id_entry.grid(row=0, column=1, pady=2, padx=(10, 0))
    client_id_entry.insert(0, config.get("client_id", ""))
    
    ttk.Label(cred_frame, text="Client Secret:").grid(row=1, column=0, sticky=tk.W, pady=2)
    client_secret_entry = ttk.Entry(cred_frame, width=45, show="*")
    client_secret_entry.grid(row=1, column=1, pady=2, padx=(10, 0))
    client_secret_entry.insert(0, config.get("client_secret", ""))
    
    # Show/hide secret checkbox
    show_var = tk.BooleanVar()
    def toggle_show():
        client_secret_entry.config(show="" if show_var.get() else "*")
    ttk.Checkbutton(cred_frame, text="Show", variable=show_var, command=toggle_show).grid(row=1, column=2, padx=(5, 0))
    
    # Interval section
    interval_frame = ttk.Frame(main_frame)
    interval_frame.pack(fill=tk.X, pady=15)
    
    ttk.Label(interval_frame, text="Check interval (seconds):").pack(side=tk.LEFT)
    interval_entry = ttk.Entry(interval_frame, width=10)
    interval_entry.pack(side=tk.LEFT, padx=(10, 0))
    interval_entry.insert(0, str(config.get("check_interval", 60)))
    
    # Your channel section
    ttk.Label(main_frame, text="Your Twitch Channel:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
    own_channel_entry = ttk.Entry(main_frame, width=45)
    own_channel_entry.pack(fill=tk.X, pady=(5, 0))
    own_channel_entry.insert(0, config.get("own_channel", ""))

    # Toggles section
    toggle_frame = ttk.Frame(main_frame)
    toggle_frame.pack(fill=tk.X, pady=(10, 0))

    im_live_var = tk.BooleanVar(value=config.get("im_live_pause", False))
    ttk.Checkbutton(toggle_frame, text="Auto-pause when I'm live", variable=im_live_var).pack(anchor=tk.W)

    vod_var = tk.BooleanVar(value=config.get("vod_fallback", False))
    ttk.Checkbutton(toggle_frame, text="Auto-open VOD if stream missed", variable=vod_var).pack(anchor=tk.W)

    # Status label
    status_label = ttk.Label(main_frame, text="", font=("", 9))
    status_label.pack(pady=(5, 0))
    
    # Buttons
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill=tk.X, pady=(15, 0))
    
    def save_settings():
        streamers = [s.strip() for s in streamers_text.get("1.0", tk.END).strip().split("\n") if s.strip()]
        
        if not streamers:
            messagebox.showerror("Error", "Please enter at least one streamer.")
            return
        
        if not client_id_entry.get().strip():
            messagebox.showerror("Error", "Please enter your Client ID.")
            return
            
        if not client_secret_entry.get().strip():
            messagebox.showerror("Error", "Please enter your Client Secret.")
            return
        
        try:
            interval = int(interval_entry.get())
            if interval < 10:
                interval = 10
        except ValueError:
            interval = 60
        
        config["streamers"] = streamers
        config["client_id"] = client_id_entry.get().strip()
        config["client_secret"] = client_secret_entry.get().strip()
        config["check_interval"] = interval
        config["own_channel"] = own_channel_entry.get().strip()
        config["im_live_pause"] = im_live_var.get()
        config["vod_fallback"] = vod_var.get()
        save_config(config)
        
        status_label.config(text="✓ Settings saved! Restart Stream Monitor to apply.", foreground="green")
        dialog.after(2000, dialog.destroy)
    
    ttk.Button(btn_frame, text="Save", command=save_settings).pack(side=tk.RIGHT, padx=(10, 0))
    ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
    
    # Focus on streamers text
    dialog.after(100, lambda: streamers_text.focus_set())
    
    dialog.mainloop()


if __name__ == "__main__":
    main()
