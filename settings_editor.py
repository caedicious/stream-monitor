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

VERSION = "1.8.6"

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
    dialog.geometry("500x700")
    dialog.resizable(False, False)

    # Center window
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() - 500) // 2
    y = (dialog.winfo_screenheight() - 700) // 2
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
    ttk.Label(
        main_frame,
        text="The list order is your priority: streams higher in the list open first "
             "when several go live at once and come first in the streak rescue queue. "
             "Drag a row or use Move Up / Move Down to reorder. Add puts a new name "
             "right below the selected row. Keep Open protects a stream from being "
             "closed when max tabs is reached.",
        font=("", 8),
        foreground="gray",
        wraplength=460,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 5))

    # Working copies. Lowercased everywhere so the extension (which lowercases
    # incoming names) matches against the same strings.
    streamer_list = [s.strip().lower() for s in config.get("streamers", []) if s.strip()]
    pinned_set = {
        s.strip().lower() for s in config.get("pinned_streamers", []) if s.strip()
    }

    list_frame = ttk.Frame(main_frame)
    list_frame.pack(fill=tk.X, pady=(0, 5))

    # exportselection=False keeps the row selection when you click into the
    # Add box, so "Add inserts below the selected row" is reliable.
    streamers_listbox = tk.Listbox(
        list_frame, height=8, font=("", 10), activestyle="dotbox", exportselection=False
    )
    streamers_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    list_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=streamers_listbox.yview)
    list_scroll.pack(side=tk.LEFT, fill=tk.Y)
    streamers_listbox.config(yscrollcommand=list_scroll.set)

    list_buttons = ttk.Frame(list_frame)
    list_buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))

    PIN_PREFIX = "📌 "
    NO_PIN_PREFIX = "    "

    def _render_streamers(select_index=None):
        streamers_listbox.delete(0, tk.END)
        for rank, name in enumerate(streamer_list, 1):
            prefix = PIN_PREFIX if name in pinned_set else NO_PIN_PREFIX
            streamers_listbox.insert(tk.END, f"{rank:>2}. {prefix}{name}")
        if select_index is not None and 0 <= select_index < len(streamer_list):
            streamers_listbox.selection_set(select_index)
            streamers_listbox.see(select_index)

    def _toggle_keep_open():
        sel = streamers_listbox.curselection()
        if not sel:
            return
        name = streamer_list[sel[0]]
        if name in pinned_set:
            pinned_set.discard(name)
        else:
            pinned_set.add(name)
        _render_streamers(sel[0])

    def _remove_selected():
        sel = streamers_listbox.curselection()
        if not sel:
            return
        i = sel[0]
        name = streamer_list[i]
        del streamer_list[i]
        pinned_set.discard(name)
        if streamer_list:
            _render_streamers(min(i, len(streamer_list) - 1))
        else:
            _render_streamers()

    def _move_selected(delta):
        sel = streamers_listbox.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + delta
        if not (0 <= j < len(streamer_list)):
            return
        streamer_list.insert(j, streamer_list.pop(i))
        _render_streamers(j)

    ttk.Button(list_buttons, text="Move Up", command=lambda: _move_selected(-1), width=10).pack(pady=2)
    ttk.Button(list_buttons, text="Move Down", command=lambda: _move_selected(1), width=10).pack(pady=2)
    ttk.Button(list_buttons, text="Keep Open", command=_toggle_keep_open, width=10).pack(pady=2)
    ttk.Button(list_buttons, text="Remove", command=_remove_selected, width=10).pack(pady=2)

    add_frame = ttk.Frame(main_frame)
    add_frame.pack(fill=tk.X, pady=(0, 15))

    add_entry = ttk.Entry(add_frame)
    add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _add_streamer(_event=None):
        name = add_entry.get().strip().lower()
        if not name:
            return
        if name in streamer_list:
            messagebox.showinfo("Already added", f"'{name}' is already in the list.")
            return
        # Land the new name right below the selected row so it takes the
        # priority you picked; with nothing selected it goes to the bottom.
        sel = streamers_listbox.curselection()
        at = sel[0] + 1 if sel else len(streamer_list)
        streamer_list.insert(at, name)
        add_entry.delete(0, tk.END)
        _render_streamers(at)

    add_entry.bind("<Return>", _add_streamer)
    ttk.Button(add_frame, text="Add", command=_add_streamer, width=10).pack(side=tk.LEFT, padx=(6, 0))

    _render_streamers()

    # Drag to reorder. The list order is the priority (see the hint above):
    # the grabbed row follows the pointer and the working list is reordered
    # live; Save writes it out in this order. A press in the blank area
    # below the last row starts nothing, and dragging past the top or
    # bottom edge scrolls the list so one drag can travel the whole list.
    drag_state = {"from": None, "y": 0, "job": None}
    AUTOSCROLL_MS = 120

    def _row_at(y):
        """Index of the row under y, or None when y is in the blank area."""
        if streamers_listbox.size() == 0:
            return None
        idx = streamers_listbox.nearest(y)
        box = streamers_listbox.bbox(idx)
        if box is None or y < box[1] or y > box[1] + box[3]:
            return None
        return idx

    def _cancel_autoscroll():
        if drag_state["job"] is not None:
            streamers_listbox.after_cancel(drag_state["job"])
            drag_state["job"] = None

    def _move_dragged_to(dst):
        src = drag_state["from"]
        if src is None or dst is None or dst == src \
                or not (0 <= src < len(streamer_list)) or not (0 <= dst < len(streamer_list)):
            return
        streamer_list.insert(dst, streamer_list.pop(src))
        drag_state["from"] = dst
        _render_streamers(dst)

    def _autoscroll_tick():
        drag_state["job"] = None
        if drag_state["from"] is None:
            return
        y = drag_state["y"]
        if y < 0:
            streamers_listbox.yview_scroll(-1, "units")
        elif y > streamers_listbox.winfo_height():
            streamers_listbox.yview_scroll(1, "units")
        else:
            return
        _move_dragged_to(streamers_listbox.nearest(y))
        drag_state["job"] = streamers_listbox.after(AUTOSCROLL_MS, _autoscroll_tick)

    def _drag_start(event):
        drag_state["from"] = _row_at(event.y)
        drag_state["y"] = event.y

    def _drag_motion(event):
        if drag_state["from"] is None:
            return
        drag_state["y"] = event.y
        if event.y < 0 or event.y > streamers_listbox.winfo_height():
            if drag_state["job"] is None:
                _autoscroll_tick()
            return
        _cancel_autoscroll()
        _move_dragged_to(streamers_listbox.nearest(event.y))

    def _drag_end(_event):
        _cancel_autoscroll()
        drag_state["from"] = None

    streamers_listbox.bind("<ButtonPress-1>", _drag_start)
    streamers_listbox.bind("<B1-Motion>", _drag_motion)
    streamers_listbox.bind("<ButtonRelease-1>", _drag_end)

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
        # streamer_list is the working list maintained by the listbox UI.
        # pinned_set holds the subset marked "Keep Open"; only names that
        # are still in streamer_list are persisted (defensive against any
        # logic gap).
        streamers = list(streamer_list)
        pinned = [name for name in streamers if name in pinned_set]

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
        config["pinned_streamers"] = pinned
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
    
    # Focus on the add-streamer entry so the user can start typing immediately
    dialog.after(100, lambda: add_entry.focus_set())
    
    dialog.mainloop()


if __name__ == "__main__":
    main()
