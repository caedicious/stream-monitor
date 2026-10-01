"""1.12.0: the Settings window's Slot mode controls (DESIGN 13.1, plan WP5).

The form-to-config mapping, the dialog's validation and the summary text are
plain functions, so most of this runs without a window. The tests that build
the real window skip when Tk cannot start (no display)."""
import json
import gc
import tkinter as tk

import pytest

import settings_editor as se

OLD_CONFIG = {
    "client_id": "id",
    "client_secret": "secret",
    "streamers": ["alice", "bob"],
    "pinned_streamers": ["alice"],
    "check_interval": 30,
    "own_channel": "me",
    "im_live_pause": True,
    "vod_fallback": True,
    "usage_ping": False,
    "install_id": "abc123",
    "last_run_version": "1.11.2",
}

FIVE_KEYS = ("slot_mode", "keep_open_slots", "cycle_slots", "slot_minutes", "auto_save_streaks")

O6_SENTENCE = (
    "A Keep Open slot with no Keep Open streamer live serves turns too, and goes "
    "back to a Keep Open streamer when the current turn ends."
)


@pytest.fixture(scope="module")
def tk_app():
    # One Tk interpreter for the module. Creating a new one per test fails
    # now and then on Windows ("Can't find a usable tk.tcl"), so the first
    # creation is retried and every test builds into its own Toplevel.
    error = None
    for _ in range(3):
        try:
            app = tk.Tk()
            break
        except tk.TclError as e:
            error = e
    else:
        pytest.skip(f"Tk is not available: {error}")
    app.withdraw()
    try:
        yield app
    finally:
        try:
            app.destroy()
        except tk.TclError:
            pass
        # Free the windows' widget and variable cycles here, on the thread
        # that made the interpreter. Left to a later collection they can be
        # freed on another test's HTTP server thread, and Tcl then aborts
        # the whole run (Windows fatal exception 0x80000003, CI on Python
        # 3.11, 2026-10-01, in tests/test_slot_mode_http.py).
        gc.collect()


@pytest.fixture
def tk_root(tk_app):
    root = tk.Toplevel(tk_app)
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _build(root, config):
    try:
        return se.build_settings_window(root, config)
    except tk.TclError as e:
        pytest.skip(f"Tk could not build the window: {e}")


def _full_form(**overrides):
    form = {
        "streamers": ["alice", "bob", "carol"],
        "pinned": {"bob", "zed"},
        "client_id": "  new-id ",
        "client_secret": " new-secret  ",
        "check_interval": "45",
        "own_channel": " me ",
        "im_live_pause": False,
        "vod_fallback": True,
        "usage_ping": True,
        "slot_mode": True,
        "keep_open_slots": 1,
        "cycle_slots": 2,
        "slot_minutes": 45,
        "auto_save_streaks": True,
    }
    form.update(overrides)
    return form


def test_r01_o10_defaults_from_an_old_config():
    # A 1.11 config.json has none of the five keys: Slot mode is off and
    # automatic saves are off (O10), with 2 + 1 slots of 30 minutes.
    assert se.slot_settings(OLD_CONFIG) == {
        "slot_mode": False,
        "keep_open_slots": 2,
        "cycle_slots": 1,
        "slot_minutes": 30,
        "auto_save_streaks": False,
    }
    assert se.slot_summary(OLD_CONFIG) == "Slot mode: off"
    # Saving from an old config without touching the Slot mode controls
    # writes the defaults.
    form = {k: v for k, v in _full_form().items() if k not in FIVE_KEYS}
    saved = se.form_to_config(OLD_CONFIG, form)
    assert {k: saved[k] for k in FIVE_KEYS} == se.SLOT_DEFAULTS
    assert saved["auto_save_streaks"] is False
    assert saved["slot_mode"] is False


def test_r01_o10_window_starts_from_an_old_config(tk_root):
    w = _build(tk_root, dict(OLD_CONFIG))
    assert w["auto_save_var"].get() is False
    assert w["slot_values"] == se.SLOT_DEFAULTS
    assert w["slot_summary_label"].cget("text") == "Slot mode: off"
    assert str(w["auto_save_check"].cget("text")) == (
        "Save broken streaks automatically (opens Twitch's save-streak page for one turn)"
    )
    assert str(w["slot_button"].cget("text")) == "Slot mode..."


def test_o10_auto_save_checkbox_defaults_off_and_writes_auto_save_streaks(tk_root):
    # "Save broken streaks automatically" is off for every install until
    # the owner turns it on (O10), and it is its own key, apart from Slot
    # mode: it also drives normal-mode saves.
    assert se.SLOT_DEFAULTS["auto_save_streaks"] is False
    on = se.form_to_config(OLD_CONFIG, _full_form(auto_save_streaks=True, slot_mode=False))
    assert on["auto_save_streaks"] is True and on["slot_mode"] is False
    off = se.form_to_config(dict(OLD_CONFIG, auto_save_streaks=True), _full_form(auto_save_streaks=False))
    assert off["auto_save_streaks"] is False
    w = _build(tk_root, dict(OLD_CONFIG))
    assert w["auto_save_var"].get() is False
    assert not w["auto_save_check"].instate(["disabled"])
    # A second window, withdrawn like tk_root, so the test never shows a
    # topmost window on the desktop. tk_root's teardown destroys it too.
    top2 = tk.Toplevel(tk_root)
    top2.withdraw()
    w2 = _build(top2, dict(OLD_CONFIG, auto_save_streaks=True))
    assert not top2.winfo_ismapped()
    assert w2["auto_save_var"].get() is True


def test_r03_editor_reads_values_with_the_app_clamps():
    # Same steps as Config.__post_init__ (plan 3.1): a non-bool flag and a
    # bool count become the default, int() failures become the default,
    # then the clamps, then K + C <= 3.
    odd = {
        "slot_mode": "yes",
        "auto_save_streaks": 1,
        "keep_open_slots": True,
        "cycle_slots": "x",
        "slot_minutes": 500,
    }
    assert se.slot_settings(odd) == {
        "slot_mode": False,
        "auto_save_streaks": False,
        "keep_open_slots": 2,
        "cycle_slots": 1,
        "slot_minutes": 120,
    }
    assert se.slot_settings({"keep_open_slots": 2, "cycle_slots": 3})["keep_open_slots"] == 0
    assert se.slot_settings({"keep_open_slots": 9, "cycle_slots": 0}) == {
        **se.SLOT_DEFAULTS, "keep_open_slots": 2, "cycle_slots": 1,
    }
    assert se.slot_settings({"keep_open_slots": -3, "slot_minutes": 1})["keep_open_slots"] == 0
    assert se.slot_settings({"slot_minutes": 1})["slot_minutes"] == 5
    assert se.slot_settings({"slot_minutes": "45"})["slot_minutes"] == 45
    for k in range(-1, 5):
        for c in range(-1, 5):
            s = se.slot_settings({"keep_open_slots": k, "cycle_slots": c})
            assert se.KEEP_OPEN_RANGE[0] <= s["keep_open_slots"] <= se.KEEP_OPEN_RANGE[1]
            assert se.ROTATING_RANGE[0] <= s["cycle_slots"] <= se.ROTATING_RANGE[1]
            assert s["keep_open_slots"] + s["cycle_slots"] <= se.SLOT_MAX_TOTAL


def test_c01_form_to_config_writes_the_five_keys():
    before = json.loads(json.dumps(OLD_CONFIG))
    saved = se.form_to_config(OLD_CONFIG, _full_form())
    assert OLD_CONFIG == before, "form_to_config must not change its input"
    assert {k: saved[k] for k in FIVE_KEYS} == {
        "slot_mode": True,
        "keep_open_slots": 1,
        "cycle_slots": 2,
        "slot_minutes": 45,
        "auto_save_streaks": True,
    }
    for key in ("slot_mode", "auto_save_streaks"):
        assert type(saved[key]) is bool
    for key in ("keep_open_slots", "cycle_slots", "slot_minutes"):
        assert type(saved[key]) is int
    # The existing fields, as the old save_settings wrote them.
    assert saved["streamers"] == ["alice", "bob", "carol"]
    assert saved["pinned_streamers"] == ["bob"]  # only names still listed
    assert saved["client_id"] == "new-id"
    assert saved["client_secret"] == "new-secret"
    assert saved["check_interval"] == 45
    assert saved["own_channel"] == "me"
    assert saved["im_live_pause"] is False
    assert saved["vod_fallback"] is True
    assert saved["usage_ping"] is True
    # Fields the window does not show are kept.
    assert saved["install_id"] == "abc123"
    assert saved["last_run_version"] == "1.11.2"
    # The interval rules of the old save_settings.
    assert se.form_to_config(OLD_CONFIG, _full_form(check_interval="3"))["check_interval"] == 10
    assert se.form_to_config(OLD_CONFIG, _full_form(check_interval="soon"))["check_interval"] == 60
    # The written values always land inside the app's ranges.
    wild = se.form_to_config(OLD_CONFIG, _full_form(keep_open_slots=2, cycle_slots=3, slot_minutes=999))
    assert (wild["keep_open_slots"], wild["cycle_slots"], wild["slot_minutes"]) == (0, 3, 120)


def test_c01_save_writes_the_five_keys_to_the_file(tk_root, tmp_path, monkeypatch):
    monkeypatch.setattr(se, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(se, "CONFIG_FILE", tmp_path / "config.json")
    config = dict(OLD_CONFIG)
    w = _build(tk_root, config)
    w["auto_save_var"].set(True)
    w["slot_values"].update(slot_mode=True, keep_open_slots=0, cycle_slots=3, slot_minutes=20)
    w["save_settings"]()
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert {k: saved[k] for k in FIVE_KEYS} == {
        "slot_mode": True,
        "keep_open_slots": 0,
        "cycle_slots": 3,
        "slot_minutes": 20,
        "auto_save_streaks": True,
    }
    assert saved["install_id"] == "abc123"
    assert saved["pinned_streamers"] == ["alice"]


def test_r03_validation_rejects_more_than_three_tabs():
    total = "Slot mode can use at most 3 stream tabs in total"
    assert se.SLOT_TOTAL_ERROR == total
    for keep, cycle in ((2, 2), (1, 3), (2, 3), ("2", "2")):
        assert se.validate_slot_form(keep, cycle, 30) == total, (keep, cycle)
    for keep, cycle in ((2, 1), (1, 2), (0, 3), (0, 1), ("1", " 1 ")):
        assert se.validate_slot_form(keep, cycle, 30) is None, (keep, cycle)
    # There is always a rotating slot, and Keep Open stays within 0..2.
    assert se.validate_slot_form(0, 0, 30) == se.SLOT_ROTATING_ERROR
    assert se.validate_slot_form(3, 0, 30) == se.SLOT_KEEP_ERROR
    assert se.validate_slot_form(-1, 1, 30) == se.SLOT_KEEP_ERROR
    assert se.validate_slot_form("x", 1, 30) == se.SLOT_KEEP_ERROR
    assert se.validate_slot_form(1, "", 30) == se.SLOT_ROTATING_ERROR


def test_r03_validation_minutes_range():
    minutes = "Minutes per turn must be between 5 and 120"
    assert se.SLOT_MINUTES_ERROR == minutes
    for value in (4, 121, 0, -5, "abc", "", "2.5", None, True):
        assert se.validate_slot_form(2, 1, value) == minutes, value
    for value in (5, 30, 120, "30", " 45 "):
        assert se.validate_slot_form(2, 1, value) is None, value


def test_r03_dialog_ok_validates_and_writes_the_working_values(tk_root, monkeypatch):
    shown = []
    monkeypatch.setattr(se.messagebox, "showerror", lambda title, msg, **kw: shown.append(msg))
    w = _build(tk_root, dict(OLD_CONFIG))
    d = w["open_slot_dialog"]()
    # Off: the spinboxes are disabled and there is no tip.
    assert all(s.instate(["disabled"]) for s in d["spins"])
    assert not d["tip_label"].winfo_manager()
    # A click turns them on, and the tip shows (Save broken streaks is off).
    d["mode_check"].invoke()
    assert d["mode_var"].get() is True
    assert all(not s.instate(["disabled"]) for s in d["spins"])
    assert d["tip_label"].winfo_manager() == "pack"
    d["keep_var"].set("2")
    d["cycle_var"].set("2")
    d["ok"]()
    assert shown == ["Slot mode can use at most 3 stream tabs in total"]
    assert w["slot_values"]["slot_mode"] is False  # nothing written
    d["cycle_var"].set("1")
    d["minutes_var"].set("200")
    d["ok"]()
    assert shown[-1] == "Minutes per turn must be between 5 and 120"
    d["keep_var"].set("1")
    d["cycle_var"].set("2")
    d["minutes_var"].set("45")
    d["ok"]()
    assert len(shown) == 2
    assert w["slot_values"] == {
        "slot_mode": True, "keep_open_slots": 1, "cycle_slots": 2,
        "slot_minutes": 45, "auto_save_streaks": False,
    }
    assert w["slot_summary_label"].cget("text") == "Slot mode: 1 Keep Open + 2 rotating, 45 min"


def test_r03_dialog_spinboxes_and_tip_follow_the_checkbox(tk_root):
    w = _build(tk_root, dict(OLD_CONFIG, slot_mode=True))
    d = w["open_slot_dialog"]()
    bounds = [(float(s.cget("from")), float(s.cget("to"))) for s in d["spins"]]
    assert bounds == [(0, 2), (1, 3), (5, 120)]
    assert all(not s.instate(["disabled"]) for s in d["spins"])
    # Slot mode on and Save broken streaks off: the gray tip shows.
    assert d["tip_label"].winfo_manager() == "pack"
    assert str(d["tip_label"].cget("text")) == (
        "Tip: turn on Save broken streaks so streams that end before their turn still get one."
    )
    d["window"].destroy()
    w["auto_save_var"].set(True)
    d = w["open_slot_dialog"]()
    assert not d["tip_label"].winfo_manager()


def test_o09_vod_toggle_disabled_in_slot_mode(tk_root):
    assert se.vod_toggle_state(True) == (
        "Auto-open VOD if stream missed (Slot mode uses Save broken streaks)", False,
    )
    assert se.vod_toggle_state(False) == ("Auto-open VOD if stream missed", True)
    w = _build(tk_root, dict(OLD_CONFIG, slot_mode=True))
    assert w["vod_check"].instate(["disabled"])
    assert str(w["vod_check"].cget("text")).endswith(" (Slot mode uses Save broken streaks)")
    # The stored value is kept, only the control is disabled.
    assert w["vod_var"].get() is True
    # Turning Slot mode off in the dialog enables it again.
    d = w["open_slot_dialog"]()
    d["mode_var"].set(False)
    d["ok"]()
    assert not w["vod_check"].instate(["disabled"])
    assert str(w["vod_check"].cget("text")) == "Auto-open VOD if stream missed"


def test_c01_summary_text():
    assert se.slot_summary({}) == "Slot mode: off"
    assert se.slot_summary({"slot_mode": False, "keep_open_slots": 0}) == "Slot mode: off"
    assert se.slot_summary({"slot_mode": True}) == "Slot mode: 2 Keep Open + 1 rotating, 30 min"
    assert se.slot_summary(
        {"slot_mode": True, "keep_open_slots": 0, "cycle_slots": 3, "slot_minutes": 45}
    ) == "Slot mode: 0 Keep Open + 3 rotating, 45 min"
    # The summary shows what the app will use, after its clamps.
    assert se.slot_summary(
        {"slot_mode": True, "keep_open_slots": 2, "cycle_slots": 3}
    ) == "Slot mode: 0 Keep Open + 3 rotating, 30 min"


def test_o06_dialog_text_mentions_lending(tk_root):
    text = se.SLOT_DIALOG_TEXT
    assert O6_SENTENCE in text
    assert text.startswith("Your list order is the priority.")
    assert "extra Keep Open streamers, gets one turn in the rotating slot" in text
    assert se.SLOT_MODE_LABEL == (
        "Slot mode: a few stream tabs, the rest take turns (needs browser extension 1.12)"
    )
    w = _build(tk_root, dict(OLD_CONFIG))
    d = w["open_slot_dialog"]()
    assert str(d["text_label"].cget("text")) == text


def test_r01_settings_window_fits_700_pixels(tk_root):
    config = dict(
        OLD_CONFIG,
        streamers=[f"streamer{i}" for i in range(40)],
        slot_mode=True,
        auto_save_streaks=False,
    )
    w = _build(tk_root, config)
    tk_root.update_idletasks()
    assert tk_root.winfo_reqheight() <= 700, tk_root.winfo_reqheight()
    assert int(w["listbox"].cget("height")) == 7


# 1366x768 at 100% scaling with the default 48 px Windows 11 taskbar: the
# work area ends at y 720 (DESIGN 13.1, plan 5.4 item 4).
SMALL_SCREEN_H = 768
SMALL_WORK_BOTTOM = 720
OUTER_H = se.WINDOW_HEIGHT + se.TITLE_BAR_FALLBACK + se.BOTTOM_BORDER


def test_r01_settings_window_y_stays_in_the_work_area():
    assert OUTER_H == 739
    # Centered, the window would start at 34 and its frame would end 19 px
    # under the taskbar, so it moves up to the top of the work area.
    assert se.settings_window_y(SMALL_SCREEN_H, 0, SMALL_WORK_BOTTOM, OUTER_H) == 0
    # Screens with room keep the centered position.
    assert se.settings_window_y(1440, 0, 1392, OUTER_H) == 370
    assert se.settings_window_y(1080, 0, 1032, OUTER_H) == 190
    # A taskbar at the top: never above the work area, so the title bar
    # stays reachable.
    assert se.settings_window_y(SMALL_SCREEN_H, 48, SMALL_SCREEN_H, OUTER_H) == 48


@pytest.mark.parametrize("slot_mode", [True, False])
def test_r01_settings_save_stays_above_the_taskbar_at_1366x768(tk_root, slot_mode):
    config = dict(
        OLD_CONFIG,
        streamers=[f"streamer{i}" for i in range(40)],
        slot_mode=slot_mode,
        auto_save_streaks=False,
    )
    w = _build(tk_root, config)
    try:
        tk_root.update_idletasks()
        save = w["save_button"]
        row = w["slot_button"]
        save_offset = save.winfo_rooty() - tk_root.winfo_rooty()
        row_offset = row.winfo_rooty() - tk_root.winfo_rooty()
        save_h = max(save.winfo_height(), save.winfo_reqheight())
    except tk.TclError as e:
        pytest.skip(f"Tk could not lay out the window: {e}")
    assert 0 < row_offset < save_offset, (row_offset, save_offset)
    assert save_offset + save_h <= se.WINDOW_HEIGHT
    y = se.settings_window_y(SMALL_SCREEN_H, 0, SMALL_WORK_BOTTOM, OUTER_H)
    bottom = y + se.TITLE_BAR_FALLBACK + save_offset + save_h
    assert bottom <= SMALL_WORK_BOTTOM, (y, save_offset, bottom)


def test_r01_settings_window_moves_up_when_the_work_area_is_short(tk_root, monkeypatch):
    # The window uses settings_window_y when it can read the work area.
    monkeypatch.setattr(se, "_work_area", lambda: (0, SMALL_WORK_BOTTOM))
    _build(tk_root, dict(OLD_CONFIG, slot_mode=True))
    tk_root.update_idletasks()
    assert tk_root.geometry().endswith("+0"), tk_root.geometry()
    assert not tk_root.winfo_ismapped()


def test_r01_settings_window_centers_without_a_work_area(tk_root, monkeypatch):
    monkeypatch.setattr(se, "_work_area", lambda: None)
    _build(tk_root, dict(OLD_CONFIG))
    tk_root.update_idletasks()
    centered = (tk_root.winfo_screenheight() - 700) // 2
    assert tk_root.geometry().endswith(f"+{centered}"), tk_root.geometry()
