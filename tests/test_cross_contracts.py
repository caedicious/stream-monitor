"""Contracts that span the desktop app, the settings editor and both
extensions (1.12.0 build plan, WP6 task 5).

Each side has its own tests. These compare one side with the other, so a
value changed in one file and not in its partner fails here: the reserved
logins, the streak-event sources, the report browser names, the plan
staleness and at-risk expiry windows, the card deadline slack, the Slot mode
ranges, the plan version and the gone reasons of the open-tabs report.

The JavaScript side is read as text, the same way tests/test_extension_parity.py
reads it, so no browser and no Node.js is needed.
"""
import gc
import http.client
import json
import re
import threading
import tkinter as tk
from pathlib import Path

import pytest

import settings_editor as se
import slot_scheduler
import streak_saves
import stream_monitor_tray as sm

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = ("chrome", "firefox")


def _path(browser: str, name: str) -> Path:
    return ROOT / f"{browser}_extension" / name


def _read(browser: str, name: str) -> str:
    return _path(browser, name).read_text(encoding="utf-8")


# --- Reading JavaScript as text ---------------------------------------------

def _const_values(text: str, name: str) -> list:
    """Every `const NAME = value;` of a file, top level or indented."""
    return re.findall(r"^[ \t]*const " + re.escape(name) + r" = (.+?);", text, re.M)


def _one_const(text: str, name: str, where: str) -> str:
    values = _const_values(text, name)
    assert len(values) == 1, f"{where} defines {name} {len(values)} times"
    return values[0].strip()


def _number(expr: str) -> int:
    """A numeric literal or a product of them ("4 * 60 * 60 * 1000")."""
    expr = expr.strip()
    assert re.fullmatch(r"[0-9]+(?:\s*\*\s*[0-9]+)*", expr), f"not a plain number: {expr}"
    result = 1
    for part in expr.split("*"):
        result *= int(part)
    return result


def _string(expr: str) -> str:
    m = re.fullmatch(r"\"([^\"\\]*)\"|'([^'\\]*)'", expr.strip())
    assert m, f"not a plain string: {expr}"
    return m.group(1) if m.group(1) is not None else m.group(2)


def _frozen_list(text: str, name: str, where: str) -> list:
    found = re.findall(r"const " + re.escape(name) + r" = Object\.freeze\(\[(.*?)\]\)", text, re.S)
    assert len(found) == 1, f"{where} defines {name} {len(found)} times"
    return [a or b for a, b in re.findall(r"\"([^\"]*)\"|'([^']*)'", found[0])]


def _code_lines(text: str) -> str:
    """The text without whole-line // comments, so comments that show an
    example object do not count as code."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _skip_string(text: str, i: int) -> int:
    """Index just past the string literal that starts at text[i]."""
    quote = text[i]
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise AssertionError("unterminated string literal")


def _function_body(text: str, name: str) -> str:
    """The body of a function declaration, from its opening to its closing
    brace (string literals and // comments skipped while counting)."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", text)
    assert m, f"no function {name}"
    i = text.index("{", text.index(")", m.end()))
    start, depth = i, 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'`":
            i = _skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = text.index("\n", i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise AssertionError(f"function {name} does not close")


def _split_args(text: str, i: int) -> list:
    """The top-level arguments of a call whose "(" ends just before i."""
    args, current, depth = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'`":
            end = _skip_string(text, i)
            current.append(text[i:end])
            i = end
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(current))
                return [a.strip() for a in args if a.strip()]
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    raise AssertionError("unterminated call")


def _calls(text: str, name: str) -> list:
    """[(position, [argument text])] for every call of `name`, leaving out
    its declaration, method calls of the same name and comment lines."""
    out = []
    for m in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "//" in text[line_start:m.start()]:
            continue
        if re.search(r"function\s*$", text[max(0, m.start() - 20):m.start()]):
            continue
        out.append((m.start(), _split_args(text, m.end())))
    return out


def _top_functions(text: str) -> list:
    """[(position, name, [parameter names])] for every top-level function."""
    out = []
    for m in re.finditer(r"^(?:async\s+)?function\s+([\w$]+)\s*\(([^)]*)\)", text, re.M):
        params = [p.split("=")[0].strip() for p in m.group(2).split(",") if p.strip()]
        out.append((m.start(), m.group(1), params))
    return out


def _enclosing_function(functions: list, pos: int):
    before = [f for f in functions if f[0] <= pos]
    return before[-1] if before else None


def _literals(expr: str) -> set:
    return {a or b for a, b in re.findall(r"\"([^\"\\]*)\"|'([^'\\]*)'", expr)}


# --- 1. Reserved logins (plan A19) -------------------------------------------

A19_RESERVED = {
    "directory", "videos", "settings", "subscriptions", "inventory", "drops", "wallet",
    "save-streak", "popout", "embed", "moderator", "team", "search",
}


def test_ap05_f28_reserved_logins_match_both_content_scripts():
    """The desktop's RESERVED_LOGINS and each content script's
    STREAK_RESERVED_PATHS are one list (A19): a name the content script
    rejects as a login is never an item on the desktop, and the reverse."""
    assert streak_saves.RESERVED_LOGINS == A19_RESERVED, (
        f"desktop only={sorted(streak_saves.RESERVED_LOGINS - A19_RESERVED)}, "
        f"A19 only={sorted(A19_RESERVED - streak_saves.RESERVED_LOGINS)}"
    )
    for browser in BROWSERS:
        paths = _frozen_list(_read(browser, "content.js"), "STREAK_RESERVED_PATHS", f"{browser} content.js")
        assert len(paths) == len(set(paths)), f"{browser}: a reserved path is listed twice"
        assert set(paths) == streak_saves.RESERVED_LOGINS, (
            f"{browser} content.js only={sorted(set(paths) - streak_saves.RESERVED_LOGINS)}, "
            f"desktop only={sorted(streak_saves.RESERVED_LOGINS - set(paths))}"
        )
        for name in paths:
            assert not streak_saves.login_ok(name), f"the desktop accepts reserved name {name}"


# --- 2. Streak-event sources (plan 3.2, 3.5, A14, A15, A41) ------------------

# 3.2 after the live check of 2026-10-01 (A41 item 2, A46): Twitch's sidebar
# "Save your Streak" pills link to VODs, so "link" left
# streak_saves.STREAK_SOURCES. The desktop still parses a link event
# (EVENT_SOURCES, plan 3.5); it just no longer asks for one.
BUILT_SOURCES = ["bell", "page", "manual"]
BUILT_EVENT_SOURCES = ["bell", "page", "link", "manual"]

# The v1.11.2 savedStreaks store keeps a `source` field of its own ("local"
# for a page this profile saw, "desktop" for a save /config listed). It is
# never sent as a streak-event source.
SAVED_STREAK_ENTRY_SOURCES = {"local", "desktop"}


def _content_sources(text: str) -> set:
    """The `source` values a content script puts on streak events: the
    candidate scope (streakCandidateSource and any scope passed in its
    place, such as the removed-subtree rule) and literal source fields
    (the link event)."""
    code = _code_lines(text)
    found = set(re.findall(r"\bsource:\s*\"(\w+)\"", code))
    found |= set(re.findall(r"\breturn\s+\"(\w+)\"", _function_body(text, "streakCandidateSource")))
    for name in set(re.findall(r"\bscope:\s*([A-Za-z_$][\w$]*)", code)):
        if name == "streakCandidateSource":
            continue
        m = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*([^;]+);", code)
        assert m, f"the scope {name} is not a const in this file"
        found |= set(re.findall(r"(?:\?|:|return)\s*\"(\w+)\"", m.group(1)))
    return found


def _background_sources(text: str) -> set:
    props = set(re.findall(r"\bsource:\s*\"(\w+)\"", _code_lines(text)))
    return props - SAVED_STREAK_ENTRY_SOURCES


def _source_gates(text: str) -> set:
    """Sources the script sends only when /config's streak_sources lists
    them: `...Sources().includes("x")` and `sources.includes("x")`."""
    return set(re.findall(r"[Ss]ources[()\s]*\.includes\(\s*\"(\w+)\"\s*\)", _code_lines(text)))


def _get_config_sources(monkeypatch) -> list:
    monkeypatch.setattr(sm, "CONFIG_SERVER_PORT", 0)  # never the app's own port
    config = sm.Config(client_id="a", client_secret="b", streamers=["alice"])
    server = sm.create_config_server(config)
    assert server is not None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            conn.request("GET", "/config")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
    return json.loads(body)["streak_sources"]


def test_c02_c05_f28_streak_sources_match_what_the_extension_sends_and_config_publishes(monkeypatch):
    """/config publishes list(STREAK_SOURCES), and the sources that can
    reach the desktop from either extension are exactly that set. A source
    the background forwards only when /config lists it (link, A14; manual,
    A15) and that the desktop does not list never reaches it, so it does
    not count. Since the 2026-10-01 live check (A41 item 2) /config leaves
    out "link", so a link event the content script still builds stops at
    the background's gate. Every source either extension can put on an
    event is one the desktop parses (EVENT_SOURCES), so none of them would
    be read as "bell"."""
    assert list(streak_saves.STREAK_SOURCES) == BUILT_SOURCES, (
        f"STREAK_SOURCES is {streak_saves.STREAK_SOURCES}; after an A41 change, update BUILT_SOURCES"
    )
    assert list(streak_saves.EVENT_SOURCES) == BUILT_EVENT_SOURCES, streak_saves.EVENT_SOURCES
    assert set(streak_saves.STREAK_SOURCES) <= set(streak_saves.EVENT_SOURCES)
    published = _get_config_sources(monkeypatch)
    assert published == list(streak_saves.STREAK_SOURCES), f"/config publishes {published}"
    assert "link" not in published, "A41 item 2: /config must not ask for link events"
    advertised = set(streak_saves.STREAK_SOURCES)
    for browser in BROWSERS:
        content = _content_sources(_read(browser, "content.js"))
        background_text = _read(browser, "background.js")
        background = _background_sources(background_text)
        gated = _source_gates(background_text)
        assert gated, f"{browser} background.js gates no source on streakSources"
        sent = content | background
        assert gated <= sent, f"{browser}: a gate names a source nothing sends: {sorted(gated - sent)}"
        assert sent <= set(streak_saves.EVENT_SOURCES), (
            f"{browser}: the desktop would read {sorted(sent - set(streak_saves.EVENT_SOURCES))} as bell"
        )
        assert "link" not in sent or "link" in gated, (
            f"{browser}: link events reach the desktop without the streakSources gate (A14, A41)"
        )
        reaching = sent - (gated - advertised)
        assert reaching == advertised, (
            f"{browser}: extension sends {sorted(reaching)}, desktop takes {sorted(advertised)}"
        )
        # The popup's row title asks the same question as the background.
        popup_gates = _source_gates(_read(browser, "popup.js"))
        assert popup_gates <= advertised | gated, f"{browser} popup.js gates on {sorted(popup_gates)}"


# --- 3. Browser names (plan 3.4, 3.15, A23, A29, rule 37) --------------------

def test_c15_am29_f28_each_popup_browser_is_its_extensions_report_browser():
    """A29: the popup computes "Run by this browser" from POPUP_BROWSER and
    instanceId, and the executor key is "<OPEN_TABS_BROWSER>-<instanceId>"
    (A23), so the two names must be one."""
    for browser in BROWSERS:
        popup = _string(_one_const(_read(browser, "popup.js"), "POPUP_BROWSER", f"{browser} popup.js"))
        report = _string(_one_const(_read(browser, "background.js"), "OPEN_TABS_BROWSER",
                                    f"{browser} background.js"))
        assert popup == report == browser, (
            f"{browser}: POPUP_BROWSER={popup!r}, OPEN_TABS_BROWSER={report!r}"
        )


def test_r37_c04_f28_the_report_browsers_are_the_desktop_default_browser_families():
    """Rule 37: the desktop prefers the executor whose report browser is the
    Windows default browser's family. Each extension's report browser must
    pass the /open_tabs browser check and be a family the desktop can
    name, or the default browser could never win the role."""
    reported = set()
    for browser in BROWSERS:
        name = _string(_one_const(_read(browser, "background.js"), "OPEN_TABS_BROWSER",
                                  f"{browser} background.js"))
        assert sm._OPEN_TABS_BROWSER_RE.match(name), name
        reported.add(name)
    families = {sm.browser_family_for_progid(p) for p in ("FirefoxURL-308046B0AF4A39CB",)}
    families |= {sm.browser_family_for_progid(p) for p in sm._CHROMIUM_PROGIDS}
    assert families == reported, f"desktop families {sorted(families)}, report browsers {sorted(reported)}"


# --- 4. Plan staleness and at-risk expiry (plan 3.15, A26, A29) --------------

def test_c15_am26_am29_f28_popup_staleness_and_ack_buffer_match_both_backgrounds():
    """The popup and the background judge one plan stale at the same moment
    (A29) and drop one at-risk row at the same moment (A26)."""
    pairs = (("POPUP_SLOT_PLAN_STALE_MS", "SLOT_PLAN_STALE_MS"), ("ACK_EXPIRY_BUFFER_MS", "ACK_EXPIRY_BUFFER_MS"))
    backgrounds = {b: _read(b, "background.js") for b in BROWSERS}
    for popup_browser in BROWSERS:
        popup = _read(popup_browser, "popup.js")
        for popup_name, background_name in pairs:
            popup_value = _number(_one_const(popup, popup_name, f"{popup_browser} popup.js"))
            for background_browser, text in backgrounds.items():
                value = _number(_one_const(text, background_name, f"{background_browser} background.js"))
                assert popup_value == value, (
                    f"{popup_browser} popup.js {popup_name}={popup_value} but "
                    f"{background_browser} background.js {background_name}={value}"
                )
    assert _number(_one_const(backgrounds["chrome"], "SLOT_PLAN_STALE_MS", "chrome background.js")) == 300000
    assert _number(_one_const(backgrounds["chrome"], "ACK_EXPIRY_BUFFER_MS", "chrome background.js")) == 14400000


# --- 5. Card deadline slack (plan 3.5.3) -------------------------------------

def test_c05_c15_f28_deadline_slack_matches_all_four_extension_scripts():
    """An escalated in-danger card is one rule on both sides (3.5.3): the
    desktop's slack in seconds and each script's slack in milliseconds."""
    want = streak_saves.STREAK_DEADLINE_SLACK_SECONDS * 1000
    assert want == 3600000
    for browser in BROWSERS:
        for name in ("background.js", "content.js"):
            value = _number(_one_const(_read(browser, name), "STREAK_DEADLINE_SLACK_MS", f"{browser} {name}"))
            assert value == want, f"{browser} {name}: STREAK_DEADLINE_SLACK_MS={value}, desktop {want}"


# --- 6. Slot mode ranges (plan 3.1) ------------------------------------------

def _effective_ranges() -> dict:
    """The ranges Config.__post_init__ lets through, found by trying values
    well outside them."""
    keep, cycle, minutes = set(), set(), set()
    for k in range(-5, 11):
        for c in range(-5, 11):
            cfg = sm.Config(keep_open_slots=k, cycle_slots=c)
            keep.add(cfg.keep_open_slots)
            cycle.add(cfg.cycle_slots)
    for m in range(-10, 241):
        minutes.add(sm.Config(slot_minutes=m).slot_minutes)
    for values in (keep, cycle, minutes):
        assert values == set(range(min(values), max(values) + 1)), "a clamp range has a gap"
    return {
        "keep": (min(keep), max(keep)),
        "cycle": (min(cycle), max(cycle)),
        "minutes": (min(minutes), max(minutes)),
    }


def test_c01_r03_f28_settings_bounds_are_the_config_effective_ranges():
    """The Settings dialog offers exactly what the app runs with: Keep Open
    0..2, Rotating 1..3, Minutes 5..120, at most 3 tabs (3.1)."""
    ranges = _effective_ranges()
    assert ranges == {"keep": (0, 2), "cycle": (1, 3), "minutes": (5, 120)}, ranges
    editor = {
        "keep": tuple(se.KEEP_OPEN_RANGE),
        "cycle": tuple(se.ROTATING_RANGE),
        "minutes": tuple(se.MINUTES_RANGE),
    }
    assert editor == ranges, f"settings_editor {editor}, Config {ranges}"
    assert se.SLOT_MAX_TOTAL == slot_scheduler.SLOT_MAX_TOTAL == 3


def test_c01_r03_f28_config_lands_inside_the_settings_bounds_for_every_k_and_c():
    """Whatever config.json says, the app runs a count the dialog can show
    and accept, and the editor reads the same count the app will use."""
    for k in range(-1, 5):
        for c in range(-1, 5):
            cfg = sm.Config(keep_open_slots=k, cycle_slots=c)
            got = (cfg.keep_open_slots, cfg.cycle_slots)
            assert se.KEEP_OPEN_RANGE[0] <= got[0] <= se.KEEP_OPEN_RANGE[1], (k, c, got)
            assert se.ROTATING_RANGE[0] <= got[1] <= se.ROTATING_RANGE[1], (k, c, got)
            assert got[0] + got[1] <= se.SLOT_MAX_TOTAL, (k, c, got)
            shown = se.slot_settings({"keep_open_slots": k, "cycle_slots": c})
            assert (shown["keep_open_slots"], shown["cycle_slots"]) == got, (k, c)
            assert se.validate_slot_form(got[0], got[1], cfg.slot_minutes) is None, (k, c, got)
    for m in (-1, 0, 4, 5, 30, 120, 121, 1000):
        app = sm.Config(slot_minutes=m).slot_minutes
        assert se.MINUTES_RANGE[0] <= app <= se.MINUTES_RANGE[1], m
        assert se.slot_settings({"slot_minutes": m})["slot_minutes"] == app, m


@pytest.fixture(scope="module")
def tk_app():
    # As in tests/test_settings_slot_mode.py: one interpreter for the module,
    # the first creation retried (Windows sometimes fails to find tk.tcl).
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
        # Collect the dialog's widget and variable cycles here, on the
        # thread that made the interpreter. Left to a later collection,
        # they can be freed on another test's HTTP server thread, and Tcl
        # then aborts the whole run (Windows fatal exception 0x80000003,
        # seen once in tests/test_slot_mode_http.py on 2026-10-01).
        gc.collect()


def test_c01_r03_f28_the_slot_dialog_spinboxes_use_those_bounds(tk_app):
    root = tk.Toplevel(tk_app)
    root.withdraw()
    try:
        try:
            widgets = se.build_settings_window(root, {"streamers": ["alice"], "slot_mode": True})
            dialog = widgets["open_slot_dialog"]()
        except tk.TclError as e:
            pytest.skip(f"Tk could not build the window: {e}")
        bounds = [(int(float(s.cget("from"))), int(float(s.cget("to")))) for s in dialog["spins"]]
        ranges = _effective_ranges()
        assert bounds == [ranges["keep"], ranges["cycle"], ranges["minutes"]]
        dialog["window"].destroy()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# --- 7. Plan version (plan 3.3) ----------------------------------------------

def test_c03_f28_slot_plan_version_matches_both_backgrounds_and_popups():
    want = slot_scheduler.SLOT_PLAN_VERSION
    assert want == 1
    for browser in BROWSERS:
        background = _read(browser, "background.js")
        version = _number(_one_const(background, "SLOT_PLAN_VERSION", f"{browser} background.js"))
        assert version == want, f"{browser} background.js SLOT_PLAN_VERSION={version}, desktop {want}"
        # The popup has no constant of its own; it compares plan.v inline.
        popup_versions = [int(v) for v in re.findall(r"\.v\s*===\s*(\d+)", _code_lines(_read(browser, "popup.js")))]
        assert popup_versions, f"{browser} popup.js never checks the plan version"
        assert set(popup_versions) == {want}, f"{browser} popup.js compares plan.v with {popup_versions}"


# --- 8. Gone reasons (plan 3.4) ----------------------------------------------

CONTRACT_GONE_REASONS = {"user_closed", "navigated", "raid", "window_closed", "already_saved", "not_eligible"}

# The background functions a gone reason passes through, with the index of
# the reason argument. untrackTab forwards its third argument to appendGone.
GONE_WRAPPERS = {"appendGone": 1, "untrackTab": 2}


def _gone_reasons(text: str) -> tuple:
    """(reasons, problems): every reason literal that reaches appendGone,
    directly, through untrackTab, or through a local const assigned just
    before the call. A reason the scan cannot trace is a problem, so a new
    call path gets looked at instead of passing silently."""
    functions = _top_functions(text)
    reasons, problems = set(), []
    for wrapper, index in GONE_WRAPPERS.items():
        for pos, args in _calls(text, wrapper):
            if len(args) <= index:
                continue
            expr = args[index]
            found = _literals(expr)
            if found:
                reasons |= found
                continue
            if expr in ("null", "undefined"):
                continue
            line = text.count("\n", 0, pos) + 1
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
                problems.append(f"line {line}: {wrapper}(..., {expr}) has no literal reason")
                continue
            enclosing = _enclosing_function(functions, pos)
            if enclosing and expr in enclosing[2]:
                if enclosing[1] not in GONE_WRAPPERS:
                    problems.append(f"line {line}: the reason comes from {enclosing[1]}'s parameter {expr}")
                continue
            region = text[enclosing[0] if enclosing else 0:pos]
            assigned = list(re.finditer(r"\b(?:const|let|var)\s+" + re.escape(expr) + r"\s*=\s*([^;]+);", region))
            found = _literals(assigned[-1].group(1)) if assigned else set()
            if not found:
                problems.append(f"line {line}: cannot trace the reason {expr}")
            reasons |= found
    return reasons, problems


def test_c04_f28_gone_reasons_are_the_contract_set_in_both_backgrounds():
    """3.4: the gone reasons each background can report are exactly the
    set the desktop accepts (anything else is dropped as a bad entry, so
    the scheduler would never see the close)."""
    assert set(slot_scheduler.GONE_REASONS) == CONTRACT_GONE_REASONS
    assert len(slot_scheduler.GONE_REASONS) == len(CONTRACT_GONE_REASONS)
    assert sm.OPEN_TABS_GONE_REASONS == CONTRACT_GONE_REASONS
    for browser in BROWSERS:
        reasons, problems = _gone_reasons(_read(browser, "background.js"))
        assert not problems, f"{browser} background.js: {problems}"
        assert reasons == CONTRACT_GONE_REASONS, (
            f"{browser} background.js only={sorted(reasons - CONTRACT_GONE_REASONS)}, "
            f"never sent={sorted(CONTRACT_GONE_REASONS - reasons)}"
        )


def test_c04_am45_f28_every_gone_entry_goes_through_append_gone():
    """A45: gone entries are written only by appendGone (it also keeps the
    reopen hold), which is also what lets the reason scan above see every
    one of them."""
    for browser in BROWSERS:
        text = _read(browser, "background.js")
        functions = _top_functions(text)
        pushes = [m.start() for m in re.finditer(r"\.gone\.push\(", text)]
        assert pushes, f"{browser} background.js never appends a gone entry"
        for pos in pushes:
            enclosing = _enclosing_function(functions, pos)
            assert enclosing and enclosing[1] == "appendGone", (
                f"{browser} background.js line {text.count(chr(10), 0, pos) + 1} "
                f"writes a gone entry outside appendGone"
            )
