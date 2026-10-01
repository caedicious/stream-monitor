"""Guard against the Chrome and Firefox backgrounds drifting apart.

The two files are maintained in lockstep by scripted mirroring. v1.7.2
shipped with the rescue functions mirrored into the Firefox background
but not the five RESCUE_* constants they use, which is invisible to a
syntax check (undefined globals only throw at call time) and broke the
rescue ack in production. These tests fail on exactly that class of bug.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKGROUNDS = {
    "chrome": ROOT / "chrome_extension" / "background.js",
    "firefox": ROOT / "firefox_extension" / "background.js",
}


def _defined(text: str) -> set:
    return set(re.findall(r"^const ([A-Z][A-Z0-9_]{2,}) =", text, re.M))


def _identifiers(text: str) -> set:
    # Strip string literals so URL fragments and log text don't count.
    stripped = re.sub(r"(['\"`]).*?\1", "", text)
    return set(re.findall(r"(?<![.\w'\"])([A-Z][A-Z0-9_]{2,})(?![\w])", stripped))


def _load():
    texts = {name: path.read_text(encoding="utf-8") for name, path in BACKGROUNDS.items()}
    defined = {name: _defined(text) for name, text in texts.items()}
    return texts, defined


def test_every_shared_constant_is_defined_where_used():
    texts, defined = _load()
    # Only judge identifiers that are defined as constants in at least
    # one of the two files; that ignores ALL-CAPS words in comments while
    # catching a constant that was mirrored in usage but not definition.
    known_constants = defined["chrome"] | defined["firefox"]
    for name, text in texts.items():
        used = _identifiers(text) & known_constants
        missing = used - defined[name]
        assert not missing, (
            f"{name} background.js uses constants it never defines: {sorted(missing)}"
        )


def test_constant_sets_match_between_browsers():
    _, defined = _load()
    only_chrome = defined["chrome"] - defined["firefox"]
    only_firefox = defined["firefox"] - defined["chrome"]
    assert not only_chrome and not only_firefox, (
        f"constant drift between backgrounds: "
        f"chrome-only={sorted(only_chrome)}, firefox-only={sorted(only_firefox)}"
    )


# The top-level functions Slot mode, the stream window and the streak side
# define (1.12.0 build plan 3.14). A function mirrored in usage but missing
# in one file only throws when it is called, like the v1.7.2 constants.
REQUIRED_FUNCTIONS = (
    "applySlotPlan", "openPlannedTab", "closeTrackedTab", "appendGone",
    "slotPendingFor", "planActiveHere", "myExecutorKey", "refreshPlanSoon",
    "withTrackedTabs", "withSlotState", "withStreamWindow", "withTabPlacement",
    "ensureStreamWindow", "placeInStreamWindow", "targetWindowForOpen",
    "requestBellCheck", "mergeAtRiskEntry", "openManualSaveTab",
    "handleNotEligible", "releaseLowQuality",
)


def _functions(text: str) -> set:
    return set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", text, re.M))


def test_c14_required_functions_exist_in_both_backgrounds():
    texts, _ = _load()
    for name, text in texts.items():
        missing = [f for f in REQUIRED_FUNCTIONS if f not in _functions(text)]
        assert not missing, f"{name} background.js has no top-level function {missing}"


# Executor helpers beyond the 3.14 list that both backgrounds need: the
# tab listeners' wait for the init scan after a browser start, and the
# apply pass's hold on streamers whose gone entry a plan predates.
EXECUTOR_HELPERS = ("afterInitScan", "goneHoldsOpen", "logGoneHold", "reportOpenTabs")


def test_c14_executor_helpers_exist_in_both_backgrounds():
    texts, _ = _load()
    for name, text in texts.items():
        missing = [f for f in EXECUTOR_HELPERS if f not in _functions(text)]
        assert not missing, f"{name} background.js has no top-level function {missing}"


def _function_source(text: str, fn: str) -> str:
    """The source of a top-level function, from its declaration to the
    closing brace at column 0."""
    text = text.replace("\r\n", "\n")
    match = re.search(r"^(?:async\s+)?function\s+" + re.escape(fn) + r"\s*\(", text, re.M)
    assert match, f"no top-level function {fn}"
    end = text.find("\n}\n", match.start())
    assert end > 0, f"{fn} does not end with a closing brace at column 0"
    return text[match.start():end + 2]


def _run_node(script: str) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# (reason, gone at, plan generated_at) -> the plan predates the gone entry.
GONE_HOLD_CASES = [
    ("user_closed", 1000, 999, True),
    ("user_closed", 1000, 1000, True),
    ("user_closed", 1000, 1001, False),
    ("navigated", 1000, 1000, True),
    ("raid", 1000, 1000, True),
    ("already_saved", 1000, 1000, True),
    ("not_eligible", 1000, 1000, True),
    ("window_closed", 1000, 999, True),
    ("window_closed", 1000, 1000, False),
    ("window_closed", 1000, 1001, False),
]


def test_r25_r26_r27_a_plan_older_than_a_gone_entry_never_reopens_its_streamer_in_both_backgrounds():
    """A user close, a move or a raid dismisses or excludes the streamer
    (rules 25, 26), so a plan from the gone entry's second or earlier is
    older than the desktop's answer to it. A first window close is reissued
    (rule 27) by a plan published in that same second, so only an earlier
    one is held."""
    texts, _ = _load()
    cases = [
        {"reason": r, "at": at, "gen": gen, "want": want} for r, at, gen, want in GONE_HOLD_CASES
    ]
    for name, text in texts.items():
        script = "\n".join([
            _function_source(text, "isPlainObject"),
            _function_source(text, "goneHoldsOpen"),
            "const cases = " + json.dumps(cases) + ";",
            "const out = cases.map((c) => goneHoldsOpen(",
            "  {recentGone: {alice: {reason: c.reason, at: c.at}}}, 'alice', {generated_at: c.gen}));",
            "out.push(goneHoldsOpen({recentGone: {}}, 'alice', {generated_at: 1}));",
            "out.push(goneHoldsOpen({}, 'alice', {generated_at: 1}));",
            "out.push(goneHoldsOpen({recentGone: {alice: {reason: 'user_closed', at: 5}}}, 'alice', {}));",
            "out.push(goneHoldsOpen({recentGone: {alice: {reason: 'user_closed', at: 5}}}, 'bob', {generated_at: 1}));",
            "console.log(JSON.stringify(out));",
        ])
        got = _run_node(script)
        want = [c["want"] for c in cases] + [False, False, False, False]
        assert got == want, f"{name}: {got} != {want}"


def test_c04_a_queued_report_takes_the_most_specific_reason_in_both_backgrounds():
    """Plan 3.4's reason strings: the report queued behind the one in
    flight reads its state when it starts, so a later, more specific reason
    relabels a queued tabs-changed report, and a different specific reason
    gets a report of its own after it."""
    texts, _ = _load()
    sequences = {
        "relabel": ["tabs-changed", "tabs-changed", "plan-applied", "tabs-changed"],
        "paused": ["tabs-changed", "tabs-changed", "paused"],
        "collide": ["tabs-changed", "refresh", "tabs-changed", "plan-applied", "paused", "tabs-changed"],
        "same": ["init", "plan-applied", "plan-applied"],
    }
    expected = {
        "relabel": ["tabs-changed", "plan-applied"],
        "paused": ["tabs-changed", "paused"],
        "collide": ["tabs-changed", "refresh", "plan-applied", "paused"],
        "same": ["init", "plan-applied"],
    }
    for name, text in texts.items():
        script = "\n".join([
            "let reportInFlight = null;",
            "let reportQueued = null;",
            "let reportQueuedReason = null;",
            "let sent = [];",
            "function sendOpenTabsReport(reason) {",
            "  sent.push(reason);",
            "  return new Promise((r) => setTimeout(r, 20));",
            "}",
            _function_source(text, "reportOpenTabs"),
            _function_source(text, "startOpenTabsReport"),
            "const sequences = " + json.dumps(sequences) + ";",
            "(async () => {",
            "  const out = {};",
            "  for (const [key, reasons] of Object.entries(sequences)) {",
            "    sent = [];",
            "    await Promise.all(reasons.map((r) => reportOpenTabs(r)));",
            "    await new Promise((r) => setTimeout(r, 100));",
            "    out[key] = sent;",
            "  }",
            "  console.log(JSON.stringify(out));",
            "})();",
        ])
        got = _run_node(script)
        assert got == expected, f"{name}: {got}"


def _message_routes(text: str) -> set:
    """The (field, value) pairs the runtime.onMessage listener dispatches
    on: message.type === "x" and message.action === "x"."""
    start = re.search(r"^(?:chrome|browser)\.runtime\.onMessage\.addListener\(", text, re.M)
    assert start, "no top-level runtime.onMessage listener"
    end = text.find("\n});", start.end())
    assert end > 0, "the runtime.onMessage listener does not end with a top-level });"
    body = text[start.end():end]
    found = re.findall(r"\bmessage\.(type|action)\s*===\s*([\"'])([^\"']+)\2", body)
    return {(field, value) for field, _quote, value in found}


def test_c11_message_routes_match_in_both_backgrounds():
    texts, _ = _load()
    routes = {name: _message_routes(text) for name, text in texts.items()}
    assert routes["chrome"], "found no message routes in the chrome listener"
    assert routes["chrome"] == routes["firefox"], (
        f"message routes differ: chrome-only={sorted(routes['chrome'] - routes['firefox'])}, "
        f"firefox-only={sorted(routes['firefox'] - routes['chrome'])}"
    )
    assert ("type", "slot_status") in routes["chrome"]


# Plan 3.15: the backgrounds' Slot mode, stream window and streak constants
# and their values. NOT_ELIGIBLE_CLOSES_TURN is the release gate switch
# (A41): true or false, the same in both files.
CONTRACT_CONSTANTS = {
    "SLOT_OPEN_STAGGER_MS": "10000",
    "SLOT_PLAN_VERSION": "1",
    "SLOT_PLAN_STALE_MS": "300000",
    "SLOT_GONE_MAX": "100",
    "SLOT_PENDING_OPEN_MAX_AGE_MS": "120000",
    "WINDOW_CLOSED_TOMBSTONE_MS": "600000",
    "STREAM_WINDOW_MIN_WIDTH": "200",
    "STREAM_WINDOW_MIN_HEIGHT": "150",
    "STREAM_WINDOW_MAX_COORD": "20000",
    "STREAM_WINDOW_MATCH_PX": "8",
    "STREAM_WINDOW_PLACE_MAX_FAILURES": "3",
    "BELL_CHECK_AFTER_COMPLETE_MS": "12000",
    "BELL_CHECK_COVER_MS": "60000",
    "MANUAL_SAVE_VISIT_MS": "1800000",
    "RESCUE_ACK_HARD_TIMEOUT_MS": "600000",
    "STREAK_DEADLINE_SLACK_MS": "3600000",
}


def _constant_values(text: str) -> dict:
    return dict(re.findall(r"^const ([A-Z][A-Z0-9_]{2,}) = (.+?);", text, re.M))


def test_c15_background_constants_have_the_contract_values():
    texts, _ = _load()
    values = {name: _constant_values(text) for name, text in texts.items()}
    for name, got in values.items():
        wrong = {k: got.get(k) for k, v in CONTRACT_CONSTANTS.items() if got.get(k) != v}
        assert not wrong, f"{name} background.js constants differ from the contract: {wrong}"
        assert got.get("NOT_ELIGIBLE_CLOSES_TURN") in ("true", "false"), name
    assert values["chrome"]["NOT_ELIGIBLE_CLOSES_TURN"] == values["firefox"]["NOT_ELIGIBLE_CLOSES_TURN"]


def test_am41_the_not_eligible_switch_is_off_in_both_backgrounds():
    """A41 item 3, recorded 2026-10-01 (X09): the page Twitch shows for a
    streak that can no longer be saved was not observed, so the release
    sets NOT_ELIGIBLE_CLOSES_TURN false in both files."""
    texts, _ = _load()
    for name, text in texts.items():
        assert _constant_values(text).get("NOT_ELIGIBLE_CLOSES_TURN") == "false", name


# A46: where Twitch moves a save-streak page in place, and whether the tab
# there is still the save visit of a tracked entry of "dave". Each case:
# (url, saveStreak, landing, isSaveLanding, isKnownSaveLanding,
# tabShowsEntry). isSaveLanding judges a move onTabUpdated sees happen;
# isKnownSaveLanding and tabShowsEntry judge a tab found later, where a VOD
# counts only as the entry's remembered landing.
VOD = "https://www.twitch.tv/videos/2888044378"
SAVE_LANDING_CASES = [
    (VOD, True, None, True, False, False),
    (VOD, True, "/videos/2888044378", True, True, True),
    (VOD, True, "/videos/1", True, False, False),
    (VOD + "?t=1h2m", True, "/videos/2888044378", True, True, True),
    ("https://twitch.tv/videos/2888044378/", True, "/videos/2888044378", True, True, True),
    ("https://www.twitch.tv/dave/clip/AntediluvianPoisedRhinocerosHumbleLife-az1h_w4_FqDUU7VC?range=7d",
     True, None, True, True, True),
    ("https://www.twitch.tv/Dave", True, None, True, True, True),
    ("https://www.twitch.tv/dave/", True, None, True, True, True),
    ("https://www.twitch.tv/dave?sm=1", True, None, True, True, True),
    ("https://www.twitch.tv/dave/videos", True, None, True, True, True),
    ("https://www.twitch.tv/dave/videos?filter=archives", True, None, True, True, True),
    # The save-streak page itself shows dave, but it is not a landing.
    ("https://www.twitch.tv/save-streak/dave", True, None, False, False, True),
    # Other pages of the channel show dave as before; they are not landings.
    ("https://www.twitch.tv/dave/about", True, None, False, False, True),
    ("https://www.twitch.tv/dave/clip", True, None, False, False, True),
    # Not dave, not a VOD.
    ("https://www.twitch.tv/zed/clip/Slug", True, None, False, False, False),
    ("https://www.twitch.tv/zed", True, None, False, False, False),
    ("https://www.twitch.tv/videos/abc", True, None, False, False, False),
    ("https://www.twitch.tv/videos", True, None, False, False, False),
    ("https://www.twitch.tv/directory", True, None, False, False, False),
    ("https://example.com/videos/123", True, None, False, False, False),
    ("", True, None, False, False, False),
    # Not a save visit: a VOD is a page away from dave, the channel is his.
    (VOD, False, None, False, False, False),
    ("https://www.twitch.tv/dave/clip/Slug", False, None, False, False, True),
    ("https://www.twitch.tv/dave", False, None, False, False, True),
    # A save turn the plan flipped to live (saveStreak false) whose tab has
    # not left its VOD yet: the remembered landing still counts, so a close
    # finds the tab; another VOD does not.
    (VOD, False, "/videos/2888044378", False, True, True),
    (VOD, False, "/videos/1", False, False, False),
]


def test_am46_save_landings_match_in_both_backgrounds():
    """A46: a tracked save-streak tab that Twitch moves to /videos/<digits>,
    /dave/clip/..., /dave/videos or /dave is still the save visit; nothing
    else is, and only an entry with saveStreak makes one. A tab found later
    (DESIGN 8.3 step 0, the scan after a browser start, a release) counts on
    a VOD only when that VOD is the entry's remembered landing, since a VOD
    URL names no streamer; every page that names the streamer counts, as
    before. The landing counts whatever saveStreak says, so a save turn the
    plan flipped to live is still found on its VOD until its tab is sent to
    the channel."""
    texts, _ = _load()
    cases = [{"url": u, "save": s, "landing": l} for u, s, l, *_rest in SAVE_LANDING_CASES]
    want = [list(rest) for _u, _s, _l, *rest in SAVE_LANDING_CASES]
    for name, text in texts.items():
        consts = [line for line in text.replace("\r\n", "\n").split("\n")
                  if re.match(r"^const (TWITCH_URL_PATTERN|VOD_URL_PATTERN) =", line)]
        assert len(consts) == 2, f"{name}: {consts}"
        ignored = re.search(r"^const IGNORED_PATHS = new Set\(\[.*?\]\);", text, re.M | re.S)
        save_re = re.search(r"^const SAVE_STREAK_URL_PATTERN =\s*\n?\s*/.*?/;", text, re.M)
        assert ignored and save_re, name
        script = "\n".join(consts + [
            ignored.group(0),
            save_re.group(0),
            _function_source(text, "getStreamerFromUrl"),
            _function_source(text, "vodPathOf"),
            _function_source(text, "isSaveLanding"),
            _function_source(text, "isKnownSaveLanding"),
            _function_source(text, "tabShowsEntry"),
            "const cases = " + json.dumps(cases) + ";",
            "console.log(JSON.stringify(cases.map((c) => {",
            "  const entry = { originalStreamer: 'dave', saveStreak: c.save };",
            "  if (c.landing) entry.landing = c.landing;",
            "  return [isSaveLanding(entry, c.url), isKnownSaveLanding(entry, c.url), tabShowsEntry(entry, c.url)];",
            "})));",
        ])
        got = _run_node(script)
        wrong = [(c, g, w) for c, g, w in zip(cases, got, want) if g != w]
        assert not wrong, f"{name}: (case, got, want) {wrong}"


SAVE_VISIT_HELPERS_REQUIRED = (
    "vodPathOf", "isSaveLanding", "isKnownSaveLanding", "tabShowsEntry", "noteSaveLanding",
    "sendSaveTurnToChannel", "queueTabUpdated",
)


def test_am46_save_visit_helpers_exist_in_both_backgrounds():
    texts, _ = _load()
    for name, text in texts.items():
        missing = [f for f in SAVE_VISIT_HELPERS_REQUIRED if f not in _functions(text)]
        assert not missing, f"{name} background.js has no top-level function {missing}"


def test_am46_tab_updates_run_one_at_a_time_per_tab_in_both_backgrounds():
    """A46 (X09 fix round 2): the tabs.onUpdated listener registered at
    module scope is queueTabUpdated, which runs onTabUpdated for one tab's
    events one after another (a Map of per-tab promise chains at module
    scope), never onTabUpdated directly. Twitch's two in-place moves after a
    save-streak link otherwise race, and the second was taken for a raid."""
    texts, _ = _load()
    for name, text in texts.items():
        ns = "chrome" if name == "chrome" else "browser"
        lines = text.replace("\r\n", "\n").split("\n")
        registrations = [line for line in lines if re.match(rf"^\s*{ns}\.tabs\.onUpdated\.addListener\(", line)]
        assert len(registrations) == 1, f"{name}: {registrations}"
        assert registrations[0].startswith(f"{ns}.tabs.onUpdated.addListener(queueTabUpdated);"), \
            f"{name}: {registrations[0]}"
        assert re.search(r"^const tabUpdateChains = new Map\(\);", text, re.M), name
        source = _function_source(text, "queueTabUpdated")
        assert "onTabUpdated(tabId, changeInfo, tab)" in source and ".catch(" in source, name
        assert "tabUpdateChains.delete(tabId)" in source, name


def test_c15_shared_constants_have_the_same_values_in_both_backgrounds():
    """Every one-line constant has one value in both files; only the
    browser name the reports carry differs."""
    texts, _ = _load()
    values = {name: _constant_values(text) for name, text in texts.items()}
    differ = {k: (v, values["firefox"].get(k)) for k, v in values["chrome"].items() if v != values["firefox"].get(k)}
    assert differ == {"OPEN_TABS_BROWSER": ('"chrome"', '"firefox"')}, differ


# The stream window, bell check, streak-side and rescue-claim helpers of
# 1.12.0 (build plan WP4b) that both backgrounds must define, beside the
# 3.14 list.
STREAK_SIDE_HELPERS = (
    "forwardStreakEvent", "streakAnswer", "handleStreakCard", "persistAtRiskStreak", "atRiskEntryFromEvent",
    "handleStreakAlreadySaved", "endSaveVisit", "holdsSaveTurn", "releaseSaveStreakTab",
    "sweepForSaveStreakTargets", "rotateRescue", "maybeStartRescueFromConfig", "postRescueAck",
    "dropRescueClaim", "saveStreakNow", "dropLapsedSaveRequests", "settleManualSaveVisit",
    "scheduleBellCheck", "setStreamWindow", "clearStreamWindow", "streamWindowStatus",
    "refreshStreamWindowBounds", "reconcileStreamWindowPlacement", "focusPlacedTab", "finishCreatedTab",
    "beginPlacementMove", "endPlacementMove", "samePlacement",
)


def test_c14_streak_side_and_stream_window_helpers_exist_in_both_backgrounds():
    texts, _ = _load()
    for name, text in texts.items():
        missing = [f for f in STREAK_SIDE_HELPERS if f not in _functions(text)]
        assert not missing, f"{name} background.js has no top-level function {missing}"


# Plan 3.11: the routes the popup and the content scripts send that the
# background answers since 1.12.0.
WP4B_ROUTES = {
    ("type", "stream_window_set"), ("type", "stream_window_clear"), ("type", "stream_window_status"),
    ("type", "save_streak_now"), ("type", "streak_unparsed"), ("type", "bell_missing"),
    ("type", "not_eligible"), ("type", "streak_event"),
}


def test_c11_the_popup_and_content_routes_are_handled_in_both_backgrounds():
    texts, _ = _load()
    for name, text in texts.items():
        missing = WP4B_ROUTES - _message_routes(text)
        assert not missing, f"{name} background.js does not handle {sorted(missing)}"


def test_c14_no_stage_one_stub_is_left():
    """WP4a left stubs for the stream window and the streak side (task 11);
    WP4b replaces every one, so none of the four one-line bodies remains."""
    texts, _ = _load()
    stubs = [
        r"async function ensureStreamWindow\(seed\) \{\s*return null;\s*\}",
        r"async function requestBellCheck\(tabId\) \{\}",
        r"async function openManualSaveTab\(login\) \{\}",
        r"async function handleNotEligible\(tabId, streamer\) \{\}",
        r"function mergeAtRiskEntry\(existing, incoming\) \{\s*return existing;\s*\}",
    ]
    for name, text in texts.items():
        left = [s for s in stubs if re.search(s, text)]
        assert not left, f"{name} background.js still has stubs: {left}"
