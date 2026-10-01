"""Guard against the Chrome and Firefox content scripts drifting apart.

The bell, streak and save-streak code is mirrored line for line; only the
runtime idioms differ (chrome.* against browser.*). The player code is not
compared: the two play strategies differ on purpose, and Firefox has extra
ERROR_ constants. So this compares every BELL_, STREAK_, ALREADY_SAVED,
SAVE_STREAK_ and NOT_ELIGIBLE_ constant by name and value, the plan's list
of content constants (3.15), and the export lists.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_SCRIPTS = {
    "chrome": ROOT / "chrome_extension" / "content.js",
    "firefox": ROOT / "firefox_extension" / "content.js",
}
PREFIXES = ("BELL_", "STREAK_", "ALREADY_SAVED", "SAVE_STREAK_", "NOT_ELIGIBLE_")

# Plan 3.15, the content list, with the values it fixes.
EXPECTED_NUMBERS = {
    "BELL_OPEN_CHECK_RETRY_MS": "5000",
    "BELL_OPEN_CHECK_RETRIES": "4",
    "BELL_USER_BUSY_POLL_MS": "10000",
    "BELL_USER_BUSY_MAX_MS": "60000",
    "BELL_USER_ACTIVE_WINDOW_MS": "5000",
    "BELL_RENDER_POLL_MS": "250",
    "BELL_RENDER_STABLE_MS": "750",
    "BELL_RENDER_MIN_MS": "1000",
    "BELL_RENDER_MAX_MS": "8000",
    "BELL_RETRY_AFTER_EMPTY_MS": "60000",
    "BELL_BACKSTOP_MIN_PAGE_AGE_MS": "30000",
    "STREAK_DEDUP_TTL_MS": "172800000",
    "STREAK_CARD_ROOT_MAX_CHARS": "1500",
    "STREAK_UNPARSED_MAX_CHARS": "120",
    "NOT_ELIGIBLE_MIN_GAP_MS": "5000",
    "STREAK_DEADLINE_SLACK_MS": "3600000",
    # Plan A46: the slow hidden-tab check of a bell without badge markup.
    "BELL_HIDDEN_NO_BADGE_INTERVAL_MS": "600000",
}
EXPECTED_REGEXES = {
    "STREAK_LOGIN_RE": r"/^[a-z0-9_]{1,25}$/",
    "STREAK_NEAR_MISS_RE": r"/\b(?:broke|end(?:s|ed)?|expire[sd]?|maintained|sav(?:e|ed|ing))\b/i",
    "NOT_ELIGIBLE_HEADING_RE": r"/no\s+content\s+eligible/i",
}
EXPECTED_STRINGS = {
    # The legacy selectors, then today's dropdown as recorded on 2026-10-01
    # (plan A46): the center-window balloon, and an unlabeled dialog holding
    # persistent-notification cards.
    "STREAK_POPOVER_SELECTOR":
        '[data-a-target="onsite-notifications-popover"], [role="dialog"][aria-label*="otification" i], '
        '[data-test-selector="center-window__balloon"], '
        '[role="dialog"]:has([data-test-selector="persistent-notification"])',
    "STREAK_CARD_SELECTOR": '[data-test-selector="persistent-notification"]',
    "STREAK_CHAT_SELECTOR":
        '[data-a-target="chat-scroller"], [data-test-selector="chat-scrollable-area__message-container"], '
        '.chat-scrollable-area__message-container, .stream-chat, [role="log"], [class*="chat-line" i], '
        '[class*="chat-scrollable" i], [class*="video-chat" i], [data-a-target*="chat" i], '
        '[data-test-selector*="chat" i]',
    "STREAK_SIDEBAR_SELECTOR":
        '[role="group"][aria-label="Watch Streaks at risk" i], #side-nav, [data-a-target="side-nav-bar"], '
        'nav[aria-label*="side" i]',
    "ALREADY_SAVED_MODAL_SELECTOR": '[role="dialog"][aria-modal="true"], .tw-modal',
    "BELL_BALLOON_SELECTOR": '[data-test-selector="center-window__balloon"]',
}
EXPECTED_LISTS = {
    "STREAK_RESERVED_PATHS": ["directory", "videos", "settings", "subscriptions", "inventory", "drops",
                              "wallet", "save-streak", "popout", "embed", "moderator", "team", "search"],
    "STREAK_PAGE_SCAN_PATHS": ["directory", "drops", "inventory", "notifications", "search", "settings",
                               "subscriptions", "wallet"],
    # Plan A46: the legacy data-a-targets first, then today's bell, all
    # page-wide; the generic label only inside the top nav, so a channel's
    # own notification toggle is never the bell.
    "BELL_BUTTON_SELECTORS": ['[data-a-target="onsite-notifications-toggle__button"]',
                              '[data-a-target="onsite-notifications-toggle"]',
                              'button[aria-label="Open Notifications" i]',
                              '[data-a-target="top-nav-container"] button[aria-label*="otification" i]',
                              'nav button[aria-label*="otification" i]'],
}
KEPT = ("BELL_AUTO_CLICK_MIN_INTERVAL_MS",)
REMOVED = ("BELL_OPEN_RENDER_DELAY_MS",)
REQUIRED_EXPORTS = {
    "parseStreakText", "parseBellBadgeCount", "findBonusClaimButton", "parseAlreadySavedText",
    "parseTimeAgo", "streakLoginFor", "isNotEligibleText", "streakCandidateSource", "streakCardRelation",
    "STREAK_RESERVED_PATHS", "findBellButton", "isBellDropdownOpen", "findKeptStreakDialog",
}


def _constants(text: str) -> dict:
    """{NAME: value text with whitespace collapsed} for every ALL-CAPS const,
    a definition running to the first line that ends with a semicolon."""
    out = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*const ([A-Z][A-Z0-9_]*) =(.*)$", lines[i])
        if not m:
            i += 1
            continue
        value = m.group(2)
        while not value.rstrip().endswith(";") and i + 1 < len(lines):
            i += 1
            value += "\n" + lines[i]
        out[m.group(1)] = " ".join(value.strip().rstrip(";").split())
        i += 1
    return out


def _load():
    return {name: _constants(path.read_text(encoding="utf-8")) for name, path in CONTENT_SCRIPTS.items()}


def _string_value(value: str) -> str:
    """The value of a string literal or a +-concatenation of them."""
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", value)
    rest = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "", value)
    assert set(rest.replace("+", "").split()) == set(), f"not a plain string: {value}"
    return "".join(a or b for a, b in parts)


def _list_value(value: str) -> list:
    """The strings of a frozen list literal, single or double quoted."""
    m = re.fullmatch(r"Object\.freeze\(\[(.*)\]\)", value)
    assert m, f"not a frozen list: {value}"
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", m.group(1))
    return [a or b for a, b in parts]


def _exports(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"module\.exports = \{(.*?)\};", text, re.S)
    assert m, f"no module.exports in {path}"
    return {name.strip() for name in m.group(1).split(",") if name.strip()}


def test_ap12_mirrored_constants_match_by_name_and_value():
    consts = _load()
    picked = {b: {k: v for k, v in c.items() if k.startswith(PREFIXES)} for b, c in consts.items()}
    assert set(picked["chrome"]) == set(picked["firefox"]), (
        f"chrome-only={sorted(set(picked['chrome']) - set(picked['firefox']))}, "
        f"firefox-only={sorted(set(picked['firefox']) - set(picked['chrome']))}")
    for name in picked["chrome"]:
        assert picked["chrome"][name] == picked["firefox"][name], name


def test_ap12_the_plan_content_constants_have_the_plan_values():
    for browser, consts in _load().items():
        for name, value in EXPECTED_NUMBERS.items():
            assert consts.get(name) == value, f"{browser} {name} = {consts.get(name)!r}"
        for name, value in EXPECTED_REGEXES.items():
            assert consts.get(name) == value, f"{browser} {name} = {consts.get(name)!r}"
        for name, value in EXPECTED_STRINGS.items():
            assert name in consts, f"{browser} lacks {name}"
            assert _string_value(consts[name]) == value, f"{browser} {name}"
        for name, value in EXPECTED_LISTS.items():
            assert name in consts, f"{browser} lacks {name}"
            assert _list_value(consts[name]) == value, f"{browser} {name}"
        for name in KEPT:
            assert name in consts, f"{browser} lost {name}"
        for name in REMOVED:
            assert name not in consts, f"{browser} still defines {name}"


def test_ap12_the_export_lists_match():
    chrome = _exports(CONTENT_SCRIPTS["chrome"])
    firefox = _exports(CONTENT_SCRIPTS["firefox"])
    assert chrome == firefox
    assert REQUIRED_EXPORTS <= chrome, sorted(REQUIRED_EXPORTS - chrome)


def _section_functions(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    start = text.index("// Streak monitor.")
    end = text.index("// Channel points bonus")
    return set(re.findall(r"^\s*(?:async\s+)?function (\w+)\(", text[start:end], re.M))


def test_ap12_the_streak_and_bell_code_defines_the_same_functions():
    chrome = _section_functions(CONTENT_SCRIPTS["chrome"])
    firefox = _section_functions(CONTENT_SCRIPTS["firefox"])
    assert chrome == firefox, (sorted(chrome - firefox), sorted(firefox - chrome))
    assert {"runOpenBellCheck", "streakCandidateSource", "cardRootFor", "collectSaveStreakSlugs",
            "parseTimeAgo", "streakCardRelation"} <= chrome


def _section_function_sources(path: Path) -> dict:
    """{name: source} for every top-level function of the streak and bell
    section, from its `function` line to the first line that is `  }`."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "// Streak monitor." in line)
    end = next(i for i, line in enumerate(lines) if "// Channel points bonus" in line)
    out = {}
    i = start
    while i < end:
        m = re.match(r"^  (?:async )?function (\w+)\(", lines[i])
        if not m:
            i += 1
            continue
        j = i
        while lines[j] != "  }":
            j += 1
        out[m.group(1)] = "\n".join(lines[i:j + 1])
        i = j + 1
    return out


def test_a46_ap12_every_bell_lookup_goes_through_bell_button_selectors():
    """BELL_BUTTON_SELECTORS is the one list of bell selectors: no other line
    of code names one, and only findBellButton reads the list (the open
    check, the hidden-tab checks and the badge observer all call it)."""
    needles = ("onsite-notifications-toggle", "Open Notifications", 'button[aria-label*="otification"',
               "top-nav-container")
    for browser, path in CONTENT_SCRIPTS.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if "const BELL_BUTTON_SELECTORS" in line)
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == "]);")
        code = [line for i, line in enumerate(lines)
                if not start <= i <= end and not line.lstrip().startswith("//")]
        for needle in needles:
            hits = [line.strip() for line in code if needle in line]
            assert hits == [], f"{browser}: a bell selector outside BELL_BUTTON_SELECTORS: {hits}"
        assert sum("BELL_BUTTON_SELECTORS" in line for line in code) == 1, browser
        readers = {name for name, src in _section_function_sources(path).items() if "BELL_BUTTON_SELECTORS" in src}
        assert readers == {"findBellButton"}, f"{browser}: {sorted(readers)}"


def test_ap12_the_streak_and_bell_functions_match_line_for_line():
    chrome = _section_function_sources(CONTENT_SCRIPTS["chrome"])
    firefox = _section_function_sources(CONTENT_SCRIPTS["firefox"])
    assert set(chrome) == set(firefox)
    # Only the runtime idiom may differ (chrome.* against browser.*); since
    # A46 the already_saved report goes through notifyBackground too.
    idiom = {name for name, src in chrome.items() if "chrome." in src or "browser." in firefox[name]}
    assert idiom <= {"notifyBackground", "startBellCheckSetting"}, sorted(idiom)
    for name in sorted(set(chrome) - idiom):
        assert chrome[name] == firefox[name], f"{name} differs between the two content scripts"
    assert {"waitForBellRender", "cardRootFor", "crossesCardEdge", "cardAgeFor", "findStreakCards",
            "scheduleRetryAfterEmpty", "scheduleOpenCheckRetry", "maybeAutoOpenBell", "scanForAlreadySaved",
            "findBellButton", "findOpenCheckBell", "dropdownText", "noBadgeCheckDue", "pageChannelLogin",
            "readKeptStreakDialog", "findKeptStreakDialog", "reportAlreadySaved", "scanForKeptStreakDialog",
            "armSaveStreakCheck"} <= set(chrome) - idiom
