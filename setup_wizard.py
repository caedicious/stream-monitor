#!/usr/bin/env python3
"""
Stream Monitor Setup Wizard
Guides users through setting up the Stream Monitor application.
"""

import json
import os
import sys
import webbrowser
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import requests

VERSION = "1.5.4"

# Configuration
APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
    STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()
    STARTUP_DIR = Path.home() / ".config/autostart"

CONFIG_FILE = CONFIG_DIR / "config.json"


class SetupWizard:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"Stream Monitor Setup - v{VERSION}")
        self.root.geometry("600x500")
        self.root.resizable(False, False)
        
        # Center window
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 600) // 2
        y = (self.root.winfo_screenheight() - 500) // 2
        self.root.geometry(f"+{x}+{y}")
        
        # Data
        self.streamers = []
        self.client_id = ""
        self.client_secret = ""
        
        # Current page
        self.current_page = 0
        self.pages = [
            self.create_welcome_page,
            self.create_streamers_page,
            self.create_twitch_intro_page,
            self.create_twitch_step1_page,
            self.create_twitch_step2_page,
            self.create_credentials_page,
            self.create_finish_page,
        ]
        
        # Main container
        self.container = ttk.Frame(self.root, padding=20)
        self.container.pack(fill=tk.BOTH, expand=True)
        
        # Page frame
        self.page_frame = ttk.Frame(self.container)
        self.page_frame.pack(fill=tk.BOTH, expand=True)
        
        # Navigation buttons
        self.nav_frame = ttk.Frame(self.container)
        self.nav_frame.pack(fill=tk.X, pady=(20, 0))
        
        self.back_btn = ttk.Button(self.nav_frame, text="← Back", command=self.prev_page)
        self.back_btn.pack(side=tk.LEFT)
        
        self.next_btn = ttk.Button(self.nav_frame, text="Next →", command=self.next_page)
        self.next_btn.pack(side=tk.RIGHT)
        
        # Show first page
        self.show_page(0)
    
    def clear_page(self):
        for widget in self.page_frame.winfo_children():
            widget.destroy()
    
    def show_page(self, index):
        self.current_page = index
        self.clear_page()
        self.pages[index]()
        
        # Update navigation buttons
        self.back_btn.config(state=tk.NORMAL if index > 0 else tk.DISABLED)
        
        if index == len(self.pages) - 1:
            self.next_btn.config(text="Finish", command=self.finish)
        else:
            self.next_btn.config(text="Next →", command=self.next_page)
    
    def next_page(self):
        # Validate current page before moving
        if self.current_page == 1:  # Streamers page
            if not self.validate_streamers():
                return
        elif self.current_page == 5:  # Credentials page
            if not self.validate_credentials():
                return
        
        if self.current_page < len(self.pages) - 1:
            self.show_page(self.current_page + 1)
    
    def prev_page(self):
        if self.current_page > 0:
            self.show_page(self.current_page - 1)
    
    # ========== PAGE 0: Welcome ==========
    def create_welcome_page(self):
        ttk.Label(
            self.page_frame,
            text="Welcome to Stream Monitor!",
            font=("", 18, "bold")
        ).pack(pady=(20, 10))
        
        welcome_text = """
This wizard will help you set up Stream Monitor, a tool that 
automatically opens Twitch streams when your favorite 
streamers go live.

Here's what we'll do:

  1. Choose which streamers you want to monitor
  
  2. Create a free Twitch Developer application
     (required for the app to check stream status)
  
  3. Enter your Twitch API credentials

  4. Set up the app to run automatically when you log in

The setup takes about 5 minutes. Let's get started!
        """
        
        text_label = ttk.Label(
            self.page_frame,
            text=welcome_text,
            font=("", 11),
            justify=tk.LEFT
        )
        text_label.pack(pady=20, padx=20, anchor=tk.W)
    
    # ========== PAGE 1: Streamers ==========
    def create_streamers_page(self):
        ttk.Label(
            self.page_frame,
            text="Who do you want to monitor?",
            font=("", 16, "bold")
        ).pack(pady=(10, 5))
        
        ttk.Label(
            self.page_frame,
            text="Enter the Twitch usernames of streamers you want to watch.\nOne username per line.",
            font=("", 10)
        ).pack(pady=(0, 15))
        
        self.streamers_text = tk.Text(self.page_frame, height=12, width=40, font=("", 11))
        self.streamers_text.pack(pady=10)
        
        # Pre-fill if we have data
        if self.streamers:
            self.streamers_text.insert("1.0", "\n".join(self.streamers))
        
        ttk.Label(
            self.page_frame,
            text="Example: ninja, shroud, pokimane",
            font=("", 9),
            foreground="gray"
        ).pack()
    
    def validate_streamers(self):
        text = self.streamers_text.get("1.0", tk.END).strip()
        streamers = [s.strip() for s in text.replace(",", "\n").split("\n") if s.strip()]
        
        if not streamers:
            messagebox.showerror("Error", "Please enter at least one streamer username.")
            return False
        
        self.streamers = streamers
        return True
    
    # ========== PAGE 2: Twitch Intro ==========
    def create_twitch_intro_page(self):
        ttk.Label(
            self.page_frame,
            text="Setting Up Twitch Access",
            font=("", 16, "bold")
        ).pack(pady=(10, 5))
        
        intro_text = """
To check if streamers are live, Stream Monitor needs to connect 
to Twitch's API. This requires creating a free "application" on 
Twitch's developer portal.

Don't worry - this is easy and takes about 2 minutes!

You'll need:
  • A Twitch account (create one free at twitch.tv if needed)
  • To register an "app" (just filling out a simple form)

Your credentials are stored locally on your computer and are 
only used to check stream status - nothing else.

Click Next to begin the registration process.
        """
        
        ttk.Label(
            self.page_frame,
            text=intro_text,
            font=("", 11),
            justify=tk.LEFT
        ).pack(pady=20, padx=10, anchor=tk.W)
    
    # ========== PAGE 3: Twitch Step 1 ==========
    def create_twitch_step1_page(self):
        ttk.Label(
            self.page_frame,
            text="Step 1: Open Twitch Developer Portal",
            font=("", 16, "bold")
        ).pack(pady=(10, 5))
        
        step_text = """
Click the button below to open the Twitch application 
registration page in your browser.

If you're not logged in to Twitch, you'll need to log in first.
        """
        
        ttk.Label(
            self.page_frame,
            text=step_text,
            font=("", 11),
            justify=tk.LEFT
        ).pack(pady=15, padx=10, anchor=tk.W)
        
        open_btn = ttk.Button(
            self.page_frame,
            text="Open Twitch Developer Portal",
            command=lambda: webbrowser.open("https://dev.twitch.tv/console/apps/create")
        )
        open_btn.pack(pady=20)
        
        ttk.Label(
            self.page_frame,
            text="Once the page opens, click Next to continue.",
            font=("", 10),
            foreground="gray"
        ).pack()
    
    # ========== PAGE 4: Twitch Step 2 ==========
    def create_twitch_step2_page(self):
        ttk.Label(
            self.page_frame,
            text="Step 2: Fill Out the Form",
            font=("", 16, "bold")
        ).pack(pady=(10, 10))
        
        # Create a frame with instructions
        instructions_frame = ttk.Frame(self.page_frame)
        instructions_frame.pack(fill=tk.BOTH, expand=True, padx=10)
        
        instructions = [
            ("Name:", "Stream Monitor\n(or any name you like)"),
            ("OAuth Redirect URLs:", "http://localhost\n(type this exactly, then click Add)"),
            ("Category:", "Other"),
            ("Client Type:", "Confidential"),
        ]
        
        for i, (field, value) in enumerate(instructions):
            row_frame = ttk.Frame(instructions_frame)
            row_frame.pack(fill=tk.X, pady=8)
            
            ttk.Label(
                row_frame,
                text=f"{i+1}. {field}",
                font=("", 11, "bold"),
                width=25
            ).pack(side=tk.LEFT, anchor=tk.N)
            
            ttk.Label(
                row_frame,
                text=value,
                font=("", 11),
                foreground="#0066cc"
            ).pack(side=tk.LEFT, anchor=tk.N)
        
        ttk.Separator(instructions_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=15)
        
        ttk.Label(
            instructions_frame,
            text="After filling out the form:",
            font=("", 11, "bold")
        ).pack(anchor=tk.W)
        
        ttk.Label(
            instructions_frame,
            text='1. Click "Create"\n2. You\'ll see your new app with a "Client ID"\n3. Click "New Secret" to generate a Client Secret\n4. Copy both values - you\'ll need them on the next page',
            font=("", 11),
            justify=tk.LEFT
        ).pack(anchor=tk.W, pady=10)
    
    # ========== PAGE 5: Credentials ==========
    def create_credentials_page(self):
        ttk.Label(
            self.page_frame,
            text="Step 3: Enter Your Credentials",
            font=("", 16, "bold")
        ).pack(pady=(10, 5))
        
        ttk.Label(
            self.page_frame,
            text="Copy and paste your Client ID and Client Secret from the Twitch page.",
            font=("", 10)
        ).pack(pady=(0, 20))
        
        # Credentials form
        form_frame = ttk.Frame(self.page_frame)
        form_frame.pack(pady=10)
        
        ttk.Label(form_frame, text="Client ID:", font=("", 11)).grid(row=0, column=0, sticky=tk.W, pady=10)
        self.client_id_entry = ttk.Entry(form_frame, width=45, font=("", 11))
        self.client_id_entry.grid(row=0, column=1, padx=(15, 0), pady=10)
        if self.client_id:
            self.client_id_entry.insert(0, self.client_id)
        
        ttk.Label(form_frame, text="Client Secret:", font=("", 11)).grid(row=1, column=0, sticky=tk.W, pady=10)
        self.client_secret_entry = ttk.Entry(form_frame, width=45, font=("", 11), show="*")
        self.client_secret_entry.grid(row=1, column=1, padx=(15, 0), pady=10)
        if self.client_secret:
            self.client_secret_entry.insert(0, self.client_secret)
        
        # Show/hide secret
        self.show_secret_var = tk.BooleanVar()
        ttk.Checkbutton(
            form_frame,
            text="Show secret",
            variable=self.show_secret_var,
            command=self.toggle_secret_visibility
        ).grid(row=2, column=1, sticky=tk.W, padx=(15, 0))
        
        # Test button
        ttk.Button(
            self.page_frame,
            text="Test Connection",
            command=self.test_credentials
        ).pack(pady=20)
        
        self.test_result_label = ttk.Label(self.page_frame, text="", font=("", 10))
        self.test_result_label.pack()
    
    def toggle_secret_visibility(self):
        if self.show_secret_var.get():
            self.client_secret_entry.config(show="")
        else:
            self.client_secret_entry.config(show="*")
    
    def test_credentials(self):
        client_id = self.client_id_entry.get().strip()
        client_secret = self.client_secret_entry.get().strip()
        
        if not client_id or not client_secret:
            self.test_result_label.config(text="Please enter both credentials.", foreground="red")
            return
        
        self.test_result_label.config(text="Testing...", foreground="gray")
        self.root.update()
        
        try:
            response = requests.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials"
                },
                timeout=10
            )
            
            if response.status_code == 200:
                self.test_result_label.config(text="✓ Connection successful!", foreground="green")
                self.client_id = client_id
                self.client_secret = client_secret
            else:
                self.test_result_label.config(
                    text=f"✗ Authentication failed. Check your credentials.",
                    foreground="red"
                )
        except Exception as e:
            self.test_result_label.config(text=f"✗ Connection error: {e}", foreground="red")
    
    def validate_credentials(self):
        client_id = self.client_id_entry.get().strip()
        client_secret = self.client_secret_entry.get().strip()
        
        if not client_id:
            messagebox.showerror("Error", "Please enter your Client ID.")
            return False
        
        if not client_secret:
            messagebox.showerror("Error", "Please enter your Client Secret.")
            return False
        
        self.client_id = client_id
        self.client_secret = client_secret
        return True
    
    # ========== PAGE 6: Finish ==========
    def create_finish_page(self):
        ttk.Label(
            self.page_frame,
            text="You're All Set!",
            font=("", 18, "bold")
        ).pack(pady=(20, 10))
        
        streamers_str = ", ".join(self.streamers[:5])
        if len(self.streamers) > 5:
            streamers_str += f" and {len(self.streamers) - 5} more"
        
        summary_text = f"""
Setup Summary:

  Monitoring: {streamers_str}
  
  Check interval: Every 60 seconds

When you click Finish:

  ✓ Your settings will be saved
  
  ✓ Stream Monitor will be added to your startup programs
  
  ✓ The app will start running in your system tray

You can right-click the tray icon anytime to access Settings 
or exit the app.

Enjoy!
        """
        
        ttk.Label(
            self.page_frame,
            text=summary_text,
            font=("", 11),
            justify=tk.LEFT
        ).pack(pady=20, padx=20, anchor=tk.W)
    
    def finish(self):
        # Save configuration
        config = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "streamers": self.streamers,
            "check_interval": 60
        }

        # Preserve any existing config fields (from a previous install/upgrade)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    existing = json.load(f)
                existing.update(config)
                config = existing
            except (json.JSONDecodeError, ValueError):
                pass

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

        # Create startup shortcut (Windows)
        if sys.platform == "win32":
            self.create_windows_startup()

        messagebox.showinfo(
            "Setup Complete",
            "Stream Monitor is configured and will now start monitoring!\n\n"
            "Look for the purple circle in your system tray."
        )

        self.root.destroy()
    
    def create_windows_startup(self):
        """Create a startup shortcut on Windows."""
        try:
            # Get the path to the executable or script
            if getattr(sys, 'frozen', False):
                app_path = Path(sys.executable).parent / "StreamMonitor.exe"
                target = str(app_path)
                arguments = ""
            else:
                app_path = Path(__file__).parent / "stream_monitor_tray.py"
                target = sys.executable
                arguments = f'"{app_path}"'
            
            # Create shortcut using PowerShell
            shortcut_path = STARTUP_DIR / "Stream Monitor.lnk"
            working_dir = str(app_path.parent)
            
            # Escape backslashes for PowerShell
            shortcut_path_escaped = str(shortcut_path).replace("\\", "\\\\")
            target_escaped = target.replace("\\", "\\\\")
            working_dir_escaped = working_dir.replace("\\", "\\\\")
            arguments_escaped = arguments.replace("\\", "\\\\")
            
            ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path_escaped}")
$Shortcut.TargetPath = "{target_escaped}"
$Shortcut.Arguments = "{arguments_escaped}"
$Shortcut.WorkingDirectory = "{working_dir_escaped}"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
'''
            
            import subprocess
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(f"PowerShell error: {result.stderr}")
                
        except Exception as e:
            print(f"Could not create startup shortcut: {e}")
    
    def run(self):
        self.root.mainloop()


def main():
    wizard = SetupWizard()
    wizard.run()


if __name__ == "__main__":
    main()
