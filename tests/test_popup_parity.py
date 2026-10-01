"""Guard against the Chrome and Firefox popups drifting apart (1.12.0).

The two popups are mirrors: the same message types to the background, the
same storage keys, the same element ids and the same constants. The only
intended differences are the browser namespace (chrome.* against
browser.*), POPUP_BROWSER, and Chrome's sound-settings link, since Firefox
has no settings page that link could open (plan 0.3 rule 7).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POPUP_JS = {
    "chrome": ROOT / "chrome_extension" / "popup.js",
    "firefox": ROOT / "firefox_extension" / "popup.js",
}
POPUP_HTML = {
    "chrome": ROOT / "chrome_extension" / "popup.html",
    "firefox": ROOT / "firefox_extension" / "popup.html",
}
CHROME_ONLY_IDS = {"sound-warning-link"}

KEY_RE = r'"([A-Za-z_][A-Za-z0-9_]*)"'


def _js(name):
    return POPUP_JS[name].read_text(encoding="utf-8")


def _html(name):
    return POPUP_HTML[name].read_text(encoding="utf-8")


def _message_types(js):
    return set(re.findall(r'\btype:\s*"([a-z_]+)"', js))


def _storage_keys(js):
    keys = set()
    for m in re.finditer(r"storage\.local\.get\(([^)]*)\)", js):
        keys |= set(re.findall(KEY_RE, m.group(1)))
    for m in re.finditer(r"storage\.local\.set\(\{([^}]*)\}\)", js):
        keys |= set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))
    # Key lists passed to get() by name, which also drive the redraws.
    for m in re.finditer(r"^const [A-Z_]+_KEYS = \[([^\]]*)\]", js, re.M):
        keys |= set(re.findall(KEY_RE, m.group(1)))
    return keys


def _constants(js):
    """{NAME: value text} for every top-level ALL-CAPS const; a multi-line
    value (an object literal) is taken up to its closing line."""
    out = {}
    lines = js.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^const ([A-Z][A-Z0-9_]{2,}) = (.*)$", line)
        if not m:
            continue
        value = m.group(2)
        if value.endswith("{"):
            j = i + 1
            while not lines[j].startswith("};"):
                j += 1
            value = "\n".join(lines[i:j + 1])
        out[m.group(1)] = value
    return out


def _number(text):
    # "300000;" or "4 * 60 * 1000;"
    expr = text.strip().rstrip(";")
    assert re.fullmatch(r"[0-9 *]+", expr), expr
    result = 1
    for part in expr.split("*"):
        result *= int(part)
    return result


def test_c11_popup_message_types_match_in_both_popups():
    chrome, firefox = _message_types(_js("chrome")), _message_types(_js("firefox"))
    assert chrome == firefox, (
        f"chrome-only={sorted(chrome - firefox)}, firefox-only={sorted(firefox - chrome)}"
    )
    assert {
        "save_streak_now", "stream_window_status", "stream_window_set",
        "stream_window_clear", "slot_status", "dismiss_streak",
        "clear_acknowledged_streaks",
    } <= chrome
    # A row click only asks for the save; the background acknowledges the
    # row when the visit finishes or Twitch says the streak is kept.
    assert "ack_streak" not in chrome


def test_c09_popup_storage_keys_match_in_both_popups():
    chrome, firefox = _storage_keys(_js("chrome")), _storage_keys(_js("firefox"))
    assert chrome == firefox, (
        f"chrome-only={sorted(chrome - firefox)}, firefox-only={sorted(firefox - chrome)}"
    )
    assert {
        "atRiskStreaks", "slotPlan", "slotState", "streamWindow", "instanceId",
        "extensionPaused", "streakSources", "bellCheckOnOpen", "trackedTabs",
    } <= chrome


def test_f24_popup_element_ids_match_except_the_chrome_sound_link():
    ids = {name: set(re.findall(r'\bid="([^"]+)"', _html(name))) for name in POPUP_HTML}
    difference = ids["chrome"] ^ ids["firefox"]
    assert difference == CHROME_ONLY_IDS, sorted(difference)
    assert CHROME_ONLY_IDS <= ids["chrome"]
    assert ids["chrome"] - CHROME_ONLY_IDS == ids["firefox"]
    assert {
        "slots-section", "slots-keep", "slots-rotating", "slots-next", "slots-watched",
        "slots-status", "stream-window-status", "stream-window-use", "stream-window-stop",
        "stream-window-private", "stream-window-message", "bell-check-on-open",
        "at-risk-message",
    } <= ids["firefox"]


def test_f24_every_element_a_popup_script_looks_up_exists_in_its_page():
    for name in POPUP_JS:
        ids = set(re.findall(r'\bid="([^"]+)"', _html(name)))
        used = set(re.findall(r'getElementById\("([^"]+)"\)', _js(name)))
        assert used <= ids, f"{name}: {sorted(used - ids)}"


def test_am18_no_en_or_em_dash_in_the_popup_files():
    for path in list(POPUP_JS.values()) + list(POPUP_HTML.values()):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            assert "\u2013" not in line and "\u2014" not in line, (
                f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
            )


def test_c15_popup_browser_values():
    assert _constants(_js("chrome"))["POPUP_BROWSER"] == '"chrome";'
    assert _constants(_js("firefox"))["POPUP_BROWSER"] == '"firefox";'


def test_c15_popup_constants_match_in_both_popups():
    chrome, firefox = _constants(_js("chrome")), _constants(_js("firefox"))
    assert set(chrome) == set(firefox), (
        f"chrome-only={sorted(set(chrome) - set(firefox))}, "
        f"firefox-only={sorted(set(firefox) - set(chrome))}"
    )
    for name in chrome:
        if name == "POPUP_BROWSER":
            continue
        assert chrome[name] == firefox[name], name
    for consts in (chrome, firefox):
        assert _number(consts["POPUP_SLOT_PLAN_STALE_MS"]) == 300000
        assert _number(consts["ACK_EXPIRY_BUFFER_MS"]) == 14400000


def test_o16_as05_at_risk_rows_never_open_a_tab_from_the_popup():
    # The background opens the save-streak page (or queues the turn); the
    # popup's other tabs.create calls (streamer list, debug page, about
    # page, Chrome's sound settings) stay.
    for name in POPUP_JS:
        js = _js(name)
        start = js.index("// --- At-risk streaks ---")
        end = js.index("// --- Slots (the desktop app's Slot mode) ---")
        section = js[start:end]
        assert "tabs.create" not in section, name
        assert 'type: "save_streak_now"' in section, name
