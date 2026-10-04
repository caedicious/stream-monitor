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

VERSION = "1.12.1"

APP_NAME = "StreamMonitor"
if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME.lower()
CONFIG_FILE = CONFIG_DIR / "config.json"

# Slot mode (1.12.0). The bounds are the effective ranges the app's
# Config.__post_init__ produces: Keep Open 0..2, Rotating 1..3, at most 3
# stream tabs in total (so there is always a rotating slot), 5..120 minutes
# per turn. tests/test_cross_contracts.py compares them with the app.
SLOT_MAX_TOTAL = 3
KEEP_OPEN_RANGE = (0, 2)
ROTATING_RANGE = (1, 3)
MINUTES_RANGE = (5, 120)
SLOT_DEFAULTS = {
    "slot_mode": False,
    "keep_open_slots": 2,
    "cycle_slots": 1,
    "slot_minutes": 30,
    "auto_save_streaks": False,
}

AUTO_SAVE_LABEL = "Save broken streaks automatically (opens Twitch's save-streak page for one turn)"
VOD_LABEL = "Auto-open VOD if stream missed"
VOD_SLOT_SUFFIX = " (Slot mode uses Save broken streaks)"
SLOT_MODE_LABEL = "Slot mode: a few stream tabs, the rest take turns (needs browser extension 1.12)"
SLOT_DIALOG_TEXT = (
    "Your list order is the priority. Keep Open slots show your highest-ranked "
    "Keep Open streamers who are live; a higher-ranked one takes a slot only "
    "after the current one has had its minutes. Everyone else who is live, and "
    "extra Keep Open streamers, gets one turn in the rotating slot; then the "
    "slot stays on your highest-ranked live stream until someone new goes live "
    "or a broken streak needs its turn. A Keep Open slot with no Keep Open "
    "streamer live serves turns too, and goes back to a Keep Open streamer when "
    "the current turn ends."
)
SLOT_TIP_TEXT = (
    "Tip: turn on Save broken streaks so streams that end before their turn "
    "still get one."
)
SLOT_TOTAL_ERROR = "Slot mode can use at most 3 stream tabs in total"
SLOT_MINUTES_ERROR = "Minutes per turn must be between 5 and 120"
SLOT_KEEP_ERROR = "Keep Open slots must be between 0 and 2"
SLOT_ROTATING_ERROR = "Rotating slots must be between 1 and 3"


def _clamp(value, bounds):
    return min(max(value, bounds[0]), bounds[1])


def _config_int(value, default):
    # The app's rule: a bool is not a number, anything int() rejects is
    # the default.
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def slot_settings(config: dict) -> dict:
    """The five Slot mode fields of a config dict, with the defaults and
    clamps the app applies when it loads the file, so the editor shows
    what the app will run with. An older config.json has none of them."""
    out = {}
    for key in ("slot_mode", "auto_save_streaks"):
        value = config.get(key, SLOT_DEFAULTS[key])
        out[key] = value if isinstance(value, bool) else SLOT_DEFAULTS[key]
    keep = _config_int(config.get("keep_open_slots"), SLOT_DEFAULTS["keep_open_slots"])
    cycle = _config_int(config.get("cycle_slots"), SLOT_DEFAULTS["cycle_slots"])
    minutes = _config_int(config.get("slot_minutes"), SLOT_DEFAULTS["slot_minutes"])
    cycle = _clamp(cycle, ROTATING_RANGE)
    keep = _clamp(keep, KEEP_OPEN_RANGE)
    if keep + cycle > SLOT_MAX_TOTAL:
        keep = SLOT_MAX_TOTAL - cycle
    out["keep_open_slots"] = keep
    out["cycle_slots"] = cycle
    out["slot_minutes"] = _clamp(minutes, MINUTES_RANGE)
    return out


def slot_summary(config: dict) -> str:
    """The text of the Slot mode row in the main window."""
    s = slot_settings(config)
    if not s["slot_mode"]:
        return "Slot mode: off"
    return (
        f"Slot mode: {s['keep_open_slots']} Keep Open + "
        f"{s['cycle_slots']} rotating, {s['slot_minutes']} min"
    )


def _form_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate_slot_form(keep, cycle, minutes) -> str | None:
    """The Slot mode dialog's OK check. Takes the spinbox values (text or
    int) and returns the message to show, or None when they are valid."""
    k = _form_int(keep)
    c = _form_int(cycle)
    m = _form_int(minutes)
    if k is None:
        return SLOT_KEEP_ERROR
    if c is None:
        return SLOT_ROTATING_ERROR
    if k + c > SLOT_MAX_TOTAL:
        return SLOT_TOTAL_ERROR
    if not KEEP_OPEN_RANGE[0] <= k <= KEEP_OPEN_RANGE[1]:
        return SLOT_KEEP_ERROR
    if not ROTATING_RANGE[0] <= c <= ROTATING_RANGE[1]:
        return SLOT_ROTATING_ERROR
    if m is None or not MINUTES_RANGE[0] <= m <= MINUTES_RANGE[1]:
        return SLOT_MINUTES_ERROR
    return None


def vod_toggle_state(slot_mode: bool) -> tuple:
    """(label, enabled) for "Auto-open VOD if stream missed". Slot mode
    gives streams that end before their turn a save-streak turn under
    Save broken streaks instead, so the VOD toggle does nothing there."""
    if slot_mode:
        return VOD_LABEL + VOD_SLOT_SUFFIX, False
    return VOD_LABEL, True


def form_to_config(config: dict, form: dict) -> dict:
    """The config dict Save writes: a copy of `config` (fields the form
    does not show, such as install_id, are kept) with the form's values.
    Pure, so it is testable without a window.

    form keys: streamers (list, in priority order), pinned (the Keep Open
    names), client_id, client_secret, check_interval (text or int),
    own_channel, im_live_pause, vod_fallback, usage_ping, and the five
    Slot mode keys. A missing key keeps the config's value."""
    new = dict(config)
    streamers = list(form.get("streamers", config.get("streamers", [])))
    pinned_set = set(form.get("pinned", config.get("pinned_streamers", [])))
    try:
        interval = int(form.get("check_interval", config.get("check_interval", 60)))
        if interval < 10:
            interval = 10
    except (TypeError, ValueError):
        interval = 60
    new["streamers"] = streamers
    new["pinned_streamers"] = [name for name in streamers if name in pinned_set]
    new["client_id"] = str(form.get("client_id", config.get("client_id", ""))).strip()
    new["client_secret"] = str(form.get("client_secret", config.get("client_secret", ""))).strip()
    new["check_interval"] = interval
    new["own_channel"] = str(form.get("own_channel", config.get("own_channel", ""))).strip()
    for key, default in (("im_live_pause", False), ("vod_fallback", False), ("usage_ping", True)):
        new[key] = bool(form.get(key, config.get(key, default)))
    new.update(slot_settings({key: form.get(key, config.get(key)) for key in SLOT_DEFAULTS}))
    return new


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


# The Settings window's client height (its geometry is 500x700), and the
# frame Windows adds around it when Tk cannot measure it: a 31 px title bar
# on top and an 8 px border below (measured on Windows 11 at 100% scaling).
WINDOW_HEIGHT = 700
TITLE_BAR_FALLBACK = 31
BOTTOM_BORDER = 8


def settings_window_y(screen_h, work_top, work_bottom, outer_h) -> int:
    """The top edge of the Settings window. Centered on the screen, but
    moved up when the centered frame (outer_h tall) would reach past the
    bottom of the work area, where the taskbar would cover the Save button.
    Never above the work area's top, so the title bar stays reachable.
    At 1366x768 with a 48 px taskbar this is 0."""
    y = (screen_h - WINDOW_HEIGHT) // 2
    return max(work_top, min(y, work_bottom - outer_h))


def _work_area():
    """(top, bottom) of the primary screen's work area (the screen less the
    taskbar), or None off Windows or when it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x30
        if not ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        if rect.bottom <= rect.top:
            return None
        return rect.top, rect.bottom
    except Exception:
        return None


def build_settings_window(root, config: dict) -> dict:
    """Builds the Settings window into `root` (a Tk or Toplevel) for the
    config dict `config` and returns its widgets and working state in a
    dict. main() runs it; the tests build it headless."""
    dialog = root
    dialog.title(f"Stream Monitor Settings - v{VERSION}")
    dialog.geometry("500x700")
    dialog.resizable(False, False)

    # Center window
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() - 500) // 2
    y = (dialog.winfo_screenheight() - 700) // 2
    dialog.geometry(f"+{x}+{y}")
    # Keep Save above the taskbar: on a 768 px screen the centered window
    # reaches under it, so move it up into the work area.
    work = _work_area()
    if work is not None:
        try:
            dialog.update_idletasks()
            title_bar = dialog.winfo_rooty() - y
            if not 0 < title_bar < 100:
                title_bar = TITLE_BAR_FALLBACK
            top = settings_window_y(
                dialog.winfo_screenheight(), work[0], work[1],
                WINDOW_HEIGHT + title_bar + BOTTOM_BORDER,
            )
            if top != y:
                dialog.geometry(f"+{x}+{top}")
        except tk.TclError:
            pass

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
             "right below the selected row. When max tabs is reached, the lowest open "
             "stream in the list closes first. Keep Open protects a stream from that; in "
             "Slot mode it also decides who gets the Keep Open slots, in list order.",
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
        list_frame, height=7, font=("", 10), activestyle="dotbox", exportselection=False
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

    ttk.Button(list_buttons, text="Move Up", command=lambda: _move_selected(-1), width=11).pack(pady=2)
    ttk.Button(list_buttons, text="Move Down", command=lambda: _move_selected(1), width=11).pack(pady=2)
    ttk.Button(list_buttons, text="Keep Open", command=_toggle_keep_open, width=11).pack(pady=2)
    ttk.Button(list_buttons, text="Remove", command=_remove_selected, width=11).pack(pady=2)

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
    ttk.Button(add_frame, text="Add", command=_add_streamer, width=11).pack(side=tk.LEFT, padx=(6, 0))

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
    vod_check = ttk.Checkbutton(toggle_frame, text=VOD_LABEL, variable=vod_var)
    vod_check.pack(anchor=tk.W)

    # The working Slot mode values. The Slot mode dialog's OK writes them
    # here; Save writes them to the file with everything else.
    slot_values = slot_settings(config)

    auto_save_var = tk.BooleanVar(value=slot_values["auto_save_streaks"])
    auto_save_check = ttk.Checkbutton(toggle_frame, text=AUTO_SAVE_LABEL, variable=auto_save_var)
    auto_save_check.pack(anchor=tk.W)

    ping_var = tk.BooleanVar(value=config.get("usage_ping", True))
    ttk.Checkbutton(
        toggle_frame,
        text="Send anonymous install ping (counts installs, nothing else)",
        variable=ping_var,
    ).pack(anchor=tk.W)

    # Slot mode row: the current setting and the button that opens its dialog.
    slot_row = ttk.Frame(main_frame)
    slot_row.pack(fill=tk.X, pady=(8, 0))
    slot_summary_label = ttk.Label(slot_row, text="")
    slot_summary_label.pack(side=tk.LEFT)

    def _refresh_slot_row():
        slot_summary_label.config(text=slot_summary(slot_values))
        text, enabled = vod_toggle_state(slot_values["slot_mode"])
        vod_check.config(text=text)
        vod_check.state(["!disabled"] if enabled else ["disabled"])

    def open_slot_dialog():
        """Opens the modal Slot mode dialog and returns its widgets."""
        win = tk.Toplevel(dialog)
        win.title("Slot mode")
        win.resizable(False, False)
        win.transient(dialog)

        frame = ttk.Frame(win, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        mode_var = tk.BooleanVar(value=slot_values["slot_mode"])
        keep_var = tk.StringVar(value=str(slot_values["keep_open_slots"]))
        cycle_var = tk.StringVar(value=str(slot_values["cycle_slots"]))
        minutes_var = tk.StringVar(value=str(slot_values["slot_minutes"]))

        mode_check = ttk.Checkbutton(frame, text=SLOT_MODE_LABEL, variable=mode_var)
        mode_check.pack(anchor=tk.W)

        spin_frame = ttk.Frame(frame)
        spin_frame.pack(anchor=tk.W, padx=(20, 0), pady=(6, 0))
        spins = []
        for column, (label, var, bounds) in enumerate((
            ("Keep Open slots", keep_var, KEEP_OPEN_RANGE),
            ("Rotating slots", cycle_var, ROTATING_RANGE),
            ("Minutes per turn", minutes_var, MINUTES_RANGE),
        )):
            ttk.Label(spin_frame, text=label).grid(row=0, column=column * 2, sticky=tk.W, padx=(0 if column == 0 else 12, 4))
            spin = ttk.Spinbox(spin_frame, from_=bounds[0], to=bounds[1], width=4, textvariable=var)
            spin.grid(row=0, column=column * 2 + 1, sticky=tk.W)
            spins.append(spin)

        text_label = ttk.Label(frame, text=SLOT_DIALOG_TEXT, wraplength=440, justify=tk.LEFT)
        text_label.pack(anchor=tk.W, padx=(20, 0), pady=(10, 0))
        tip_label = ttk.Label(frame, text=SLOT_TIP_TEXT, foreground="gray", wraplength=440, justify=tk.LEFT)

        def _sync():
            on = mode_var.get()
            for spin in spins:
                spin.state(["!disabled"] if on else ["disabled"])
            if on and not auto_save_var.get():
                tip_label.pack(after=text_label, anchor=tk.W, padx=(20, 0), pady=(8, 0))
            else:
                tip_label.pack_forget()

        mode_check.config(command=_sync)

        def _ok():
            on = mode_var.get()
            error = validate_slot_form(keep_var.get(), cycle_var.get(), minutes_var.get())
            if error and on:
                messagebox.showerror("Slot mode", error, parent=win)
                return
            if not error:
                slot_values["keep_open_slots"] = int(keep_var.get())
                slot_values["cycle_slots"] = int(cycle_var.get())
                slot_values["slot_minutes"] = int(minutes_var.get())
            slot_values["slot_mode"] = on
            _refresh_slot_row()
            win.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=(12, 0))
        ok_button = ttk.Button(button_frame, text="OK", command=_ok)
        ok_button.pack(side=tk.RIGHT, padx=(10, 0))
        ttk.Button(button_frame, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)

        _sync()
        win.grab_set()
        win.focus_set()
        return {
            "window": win, "mode_check": mode_check, "mode_var": mode_var, "keep_var": keep_var,
            "cycle_var": cycle_var, "minutes_var": minutes_var, "spins": spins,
            "text_label": text_label, "tip_label": tip_label, "ok": _ok,
        }

    slot_button = ttk.Button(slot_row, text="Slot mode...", command=open_slot_dialog)
    slot_button.pack(side=tk.RIGHT)
    _refresh_slot_row()

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
        
        form = {
            "streamers": streamers,
            "pinned": pinned,
            "client_id": client_id_entry.get(),
            "client_secret": client_secret_entry.get(),
            "check_interval": interval_entry.get(),
            "own_channel": own_channel_entry.get(),
            "im_live_pause": im_live_var.get(),
            "vod_fallback": vod_var.get(),
            "usage_ping": ping_var.get(),
            "auto_save_streaks": auto_save_var.get(),
            "slot_mode": slot_values["slot_mode"],
            "keep_open_slots": slot_values["keep_open_slots"],
            "cycle_slots": slot_values["cycle_slots"],
            "slot_minutes": slot_values["slot_minutes"],
        }
        config.update(form_to_config(config, form))
        save_config(config)

        status_label.config(text="✓ Settings saved! Restart Stream Monitor to apply.", foreground="green")
        dialog.after(2000, dialog.destroy)
    
    save_button = ttk.Button(btn_frame, text="Save", command=save_settings)
    save_button.pack(side=tk.RIGHT, padx=(10, 0))
    ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

    return {
        "root": dialog,
        "listbox": streamers_listbox,
        "add_entry": add_entry,
        "vod_check": vod_check,
        "vod_var": vod_var,
        "auto_save_check": auto_save_check,
        "auto_save_var": auto_save_var,
        "slot_values": slot_values,
        "slot_summary_label": slot_summary_label,
        "slot_button": slot_button,
        "open_slot_dialog": open_slot_dialog,
        "status_label": status_label,
        "save_settings": save_settings,
        "save_button": save_button,
    }


def main():
    config = load_config()
    dialog = tk.Tk()
    widgets = build_settings_window(dialog, config)
    # Focus on the add-streamer entry so the user can start typing immediately
    dialog.after(100, lambda: widgets["add_entry"].focus_set())
    dialog.mainloop()


if __name__ == "__main__":
    main()
