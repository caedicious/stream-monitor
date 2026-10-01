"""The Twitch text parsers in both content scripts.

Streak counts can carry a thousands separator ("1,024-stream"), and the
save-streak check has to read the owner's real "No Content Eligible" card.
Each content.js exports its parsers when a CommonJS `module` exists. These
tests load the file in a Node vm with the few browser globals it touches at
load time stubbed out; its timers are no-ops, so the process exits as soon
as it has printed the results. Skipped when Node is not installed.

1.12 adds the detection hardening of the streak audit: age labels (P2), the
age gate (P3), login checks and the reserved names (P5), card identity
(P6, plan 3.5.3), badge parsing (B6), the nothing-to-watch heading (S10)
and the positive scope that keeps chat text out (F31, plan A39). Those run
as small JavaScript bodies against the exported functions, with
element-like stubs where a function reads the page.

Plan A46 adds today's Twitch as recorded on 2026-10-01: the badge-less
bell, the unlabeled dropdown dialog with persistent-notification cards,
the slow hidden-tab check and the already-kept modal after the in-place
move, loaded from the recorded markup. A generic notifications label counts
as the bell only inside the top nav, so a channel's own notification toggle
is never clicked.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTENT_SCRIPTS = {
    "chrome": ROOT / "chrome_extension" / "content.js",
    "firefox": ROOT / "firefox_extension" / "content.js",
}
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

# Loads content.js in a vm sandbox. The shared part of both harnesses.
SANDBOX = r"""
const vm = require("vm");
const fs = require("fs");
const noop = () => {};
const location = { pathname: "/directory", search: "", href: "https://www.twitch.tv/directory" };
const win = { location };
win.top = win;
const ext = {
  runtime: { onMessage: { addListener: noop }, sendMessage: () => Promise.resolve() },
  storage: {
    local: { get: (key, cb) => { if (cb) cb({}); return Promise.resolve({}); } },
    onChanged: { addListener: noop },
  },
};
const sandbox = {
  window: win,
  location,
  document: {
    body: { querySelectorAll: () => [] },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: noop,
    visibilityState: "visible",
  },
  MutationObserver: class { observe() {} disconnect() {} },
  MouseEvent: class {},
  console: { log: noop, warn: noop, error: noop },
  setTimeout: () => 0,
  setInterval: () => 0,
  clearTimeout: noop,
  clearInterval: noop,
  module: { exports: {} },
  chrome: ext,
  browser: ext,
};
function loadApi(file) {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: file });
  return sandbox.module.exports;
}
"""

# argv: <content.js> <cases.json>. Prints [api[fn](text) for fn, text in cases].
HARNESS = SANDBOX + r"""
const [file, casesFile] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const api = loadApi(file);
process.stdout.write(JSON.stringify(cases.map(([fn, text]) => api[fn](text))));
"""

# argv: <content.js> <bodies.json>. Each body is a function body run with
# (api, location, stub, NOW) that returns a JSON value. Prints {id: {ok, value}}.
BODY_HARNESS = SANDBOX + r"""
const [file, casesFile] = process.argv.slice(2);
const bodies = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const api = loadApi(file);
const NOW = Date.parse("2026-09-29T12:00:00.000Z");
const stub = {
  // An element inside some of "popover", "chat" (the chat guard) and
  // "sidebar"; closest() answers from the selector it is asked for.
  scoped(inside) {
    return {
      closest(sel) {
        if (sel.includes("onsite-notifications-popover") && inside.includes("popover")) return {};
        if (sel.includes("chat-scroller") && inside.includes("chat")) return {};
        if (sel.includes("side-nav") && inside.includes("sidebar")) return {};
        return null;
      },
    };
  },
  // A bell button: its aria-label, an optional badge element holding
  // `badge` text, an optional plain child holding `child` text.
  bell({ label = "", badge, child } = {}) {
    return {
      getAttribute: (n) => (n === "aria-label" ? label : null),
      querySelector: (sel) => (badge !== undefined && sel.includes("badge") ? { textContent: badge } : null),
      querySelectorAll: () => (child !== undefined ? [{ textContent: child, children: [] }] : []),
    };
  },
  // A card root: its text, an optional time[datetime], elements with a
  // title or aria-label, text leaves and link hrefs.
  root({ text = "", datetime, attrs = [], leaves = [], hrefs = [] } = {}) {
    const leafEls = leaves.map((t) => ({ children: [], textContent: t }));
    const attrEls = attrs.map((a) => ({ getAttribute: (n) => (a[n] === undefined ? null : a[n]) }));
    const links = hrefs.map((h) => ({ getAttribute: (n) => (n === "href" ? h : null) }));
    const timeEls = datetime ? [{ getAttribute: () => datetime }] : [];
    return {
      nodeType: 1,
      textContent: text,
      children: [],
      matches: () => false,
      querySelector: (sel) => (sel === "time[datetime]" ? timeEls[0] || null : null),
      querySelectorAll: (sel) => (sel === "a[href]" ? links : sel === "[title], [aria-label]" ? attrEls
        : sel === "*" ? leafEls : sel === "time[datetime]" ? timeEls : []),
    };
  },
};
const out = {};
for (const [id, body] of Object.entries(bodies)) {
  try {
    const value = new Function("api", "location", "stub", "NOW", body)(api, location, stub, NOW);
    out[id] = { ok: true, value: value === undefined ? null : value };
  } catch (e) {
    out[id] = { ok: false, error: String((e && e.stack) || e) };
  }
}
process.stdout.write(JSON.stringify(out));
"""

# The card the owner saw on a save-streak page (v1.11.2 request).
OWNER_TEXT = (
    "No Content Eligible You've already maintained your 4-stream streak with "
    "DriveYaBatty. Keep'em going by watching more live streams!"
)

CASES = [
    ("parseAlreadySavedText", OWNER_TEXT, {"count": 4, "streamer": "driveyabatty"}),
    (
        "parseAlreadySavedText",
        "You've already maintained your 1,024-stream streak with Fox. Keep'em going!",
        {"count": 1024, "streamer": "fox"},
    ),
    ("parseAlreadySavedText", "You've already maintained your 1234,567-stream streak with Fox.", None),
    ("parseAlreadySavedText", "Your 4-stream streak on DriveYaBatty broke", None),
    (
        "parseStreakText",
        "Your 4-stream streak on DriveYaBatty broke. Save your streak",
        {"status": "broke", "streamer": "driveyabatty", "count": 4, "deadline_hours": 24},
    ),
    (
        "parseStreakText",
        "Your 1,024-stream streak on Fox broke",
        {"status": "broke", "streamer": "fox", "count": 1024, "deadline_hours": 24},
    ),
    (
        "parseStreakText",
        "Your 2,500-stream streak on Fox ends in 3 hours",
        {"status": "in_danger", "streamer": "fox", "count": 2500, "deadline_hours": 3},
    ),
    ("parseStreakText", "Your 1234,567-stream streak on Fox broke", None),
    ("parseStreakText", OWNER_TEXT, None),
]


def _run_parsers(content_js: Path, tmp_path: Path) -> list:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([[fn, text] for fn, text, _ in CASES]), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness), str(content_js), str(cases)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("browser", sorted(CONTENT_SCRIPTS))
def test_streak_parsers(browser, tmp_path):
    results = _run_parsers(CONTENT_SCRIPTS[browser], tmp_path)
    assert len(results) == len(CASES)
    for (fn, text, expected), got in zip(CASES, results):
        assert got == expected, f"{browser} {fn}({text!r}) returned {got!r}"


# ---------------------------------------------------------------------------
# 1.12: function bodies run against the exports, one node process per file.
# ---------------------------------------------------------------------------

BODIES = {}


def body(case_id: str, js: str) -> str:
    BODIES[case_id] = js
    return case_id


# P2: age labels, {seconds, unit}.
AGE_LABELS = {
    "just now": {"seconds": 0, "unit": 60},
    "20 hours ago": {"seconds": 72000, "unit": 3600},
    "1 day ago": {"seconds": 86400, "unit": 86400},
    "2 months ago": {"seconds": 5184000, "unit": 2592000},
    "an hour ago": {"seconds": 3600, "unit": 3600},
    "a day ago": {"seconds": 86400, "unit": 86400},
    "yesterday": {"seconds": 86400, "unit": 86400},
    "Yesterday at 9:15 PM": {"seconds": 86400, "unit": 86400},
    "1 yr ago": {"seconds": 31536000, "unit": 31536000},
    "3 years ago": {"seconds": 94608000, "unit": 31536000},
    "5 minutes ago": {"seconds": 300, "unit": 60},
    "1 min ago": {"seconds": 60, "unit": 60},
    "45s ago": {"seconds": 45, "unit": 1},
    "3h ago": {"seconds": 10800, "unit": 3600},
    "2 weeks ago": {"seconds": 1209600, "unit": 604800},
    "3 mo ago": {"seconds": 7776000, "unit": 2592000},
    "a month ago": {"seconds": 2592000, "unit": 2592000},
    "Watch now2 hours ago": {"seconds": 7200, "unit": 3600},
    "yesterdayjam": None,
    "justnowgaming": None,
    "ends in 3 hours": None,
    "no age here": None,
    "": None,
}
body("age_labels", "return %s.map((t) => api.parseTimeAgo(t));" % json.dumps(list(AGE_LABELS)))
body("age_seconds_wrapper",
     'return [api.parseTimeAgoSeconds("20 hours ago"), api.parseTimeAgoSeconds("nothing"),'
     ' api.parseTimeAgoSeconds("just now")];')

# P2 and P1: a card's age read from its root; card_age_s is a whole number.
body("event_datetime", r"""
  const root = stub.root({ text: "Your 8-stream streak on fresh broke",
    datetime: new Date(NOW - 5400500).toISOString() });
  const ev = api.streakCardEvent(api.parseStreakText(root.textContent), root, "bell", NOW);
  return { age: ev.card_age_s, isInt: Number.isInteger(ev.card_age_s), unit: ev.card_age_unit_s, ev };
""")
body("event_leaf_label", r"""
  const root = stub.root({ text: "Your 12-stream streak on Bob brokeWatch now3 hours ago",
    leaves: ["Your 12-stream streak on Bob broke", "Watch now", "3 hours ago"], hrefs: ["/save-streak/bob"] });
  const ev = api.streakCardEvent(api.parseStreakText(root.textContent), root, "page", NOW);
  return [ev.card_age_s, ev.card_age_unit_s, ev.streamer, ev.login_verified, ev.source];
""")
body("event_title_date", r"""
  const at = "Sep 29, 2026, 10:30:15 AM";
  const root = stub.root({ text: "Your 3-stream streak on carol broke", attrs: [{ title: at }] });
  const ev = api.streakCardEvent(api.parseStreakText(root.textContent), root, "bell", Date.parse(at) + 90000);
  const root2 = stub.root({ text: "Your 3-stream streak on carol broke", attrs: [{ "aria-label": "Sep 29, 2026 10:30 AM" }] });
  const ev2 = api.streakCardEvent(api.parseStreakText(root2.textContent), root2, "bell", Date.parse("Sep 29, 2026 10:30 AM") + 120000);
  const root3 = stub.root({ text: "Your 3-stream streak on carol broke", attrs: [{ title: "Notifications" }] });
  const ev3 = api.streakCardEvent(api.parseStreakText(root3.textContent), root3, "bell", NOW);
  return [ev.card_age_s, ev.card_age_unit_s, ev2.card_age_s, ev2.card_age_unit_s, ev3.card_age_s, ev3.card_age_unit_s];
""")
body("event_unknown_age", r"""
  const root = stub.root({ text: "Your 9-stream streak on nage broke" });
  const ev = api.streakCardEvent(api.parseStreakText(root.textContent), root, "bell", NOW);
  return ev;
""")
body("event_week_unit", r"""
  const root = stub.root({ text: "Your 3-stream streak on wk ends in 10 days", leaves: ["1 week ago"] });
  const ev = api.streakCardEvent(api.parseStreakText(root.textContent), root, "bell", NOW);
  return [ev.card_age_s, ev.card_age_unit_s, api.passesAgeGate(ev)];
""")

# P3: the age gate.
body("age_gate", r"""
  const ev = (status, age, hours) => ({ status, card_age_s: age, deadline_hours: hours });
  return [
    api.passesAgeGate(ev("broke", 86399, 24)),
    api.passesAgeGate(ev("broke", 86400, 24)),
    api.passesAgeGate(ev("broke", null, 24)),
    api.passesAgeGate(ev("in_danger", 17999, 5)),
    api.passesAgeGate(ev("in_danger", 18000, 5)),
    api.passesAgeGate(ev("in_danger", null, 5)),
  ];
""")
body("age_gate_labels", r"""
  const read = (text, label) => {
    const root = stub.root({ text, leaves: [label] });
    return api.passesAgeGate(api.streakCardEvent(api.parseStreakText(text), root, "bell", NOW));
  };
  return [
    read("Your 4-stream streak on old broke", "1 day ago"),
    read("Your 4-stream streak on old broke", "yesterday"),
    read("Your 4-stream streak on old broke", "23 hours ago"),
    read("Your 4-stream streak on old broke", "2 months ago"),
    read("Your 2-stream streak on edna ends in 1 hour", "3 hours ago"),
    read("Your 3-stream streak on dora ends in 5 hours", "2 hours ago"),
  ];
""")

# P5: the login.
LOGIN_CASES = [
    (["bob", ["/save-streak/bob"]], {"streamer": "bob", "login_verified": True}),
    (["bob", ["/popout/bob/chat"]], {"streamer": "bob", "login_verified": True}),
    (["bob", ["https://www.twitch.tv/save-streak/carol"]], {"streamer": "carol", "login_verified": True}),
    (["bob", ["/save-streak/popout"]], {"streamer": "bob", "login_verified": True}),
    (["bob", ["https://clips.twitch.tv/save-streak/zed"]], {"streamer": "bob", "login_verified": True}),
    (["bob", ["https://example.com/save-streak/zed"]], {"streamer": "bob", "login_verified": True}),
    (["popout", ["/popout/alice/chat"]], {"streamer": "popout", "login_verified": False}),
    (["\u65e5\u672c\u8a9e", ["/save-streak/realname"]], {"streamer": "realname", "login_verified": True}),
    (["\u65e5\u672c\u8a9e", []], {"streamer": "\u65e5\u672c\u8a9e", "login_verified": False}),
    (["a_name_that_is_26_chars_xx", []], {"streamer": "a_name_that_is_26_chars_xx", "login_verified": False}),
    (["bad-name", []], {"streamer": "bad-name", "login_verified": False}),
]
body("logins", "return %s.map(([n, h]) => api.streakLoginFor(n, h));" % json.dumps([c for c, _ in LOGIN_CASES]))
RESERVED = ["directory", "videos", "settings", "subscriptions", "inventory", "drops", "wallet",
            "save-streak", "popout", "embed", "moderator", "team", "search"]
body("reserved", "return { list: Array.from(api.STREAK_RESERVED_PATHS), verified: %s.map("
                 "(n) => api.streakLoginFor(n, ['/' + n]).login_verified) };" % json.dumps(RESERVED))

# B6: the unread count.
BADGE_CASES = [
    ({"label": "Notifications, 3 unread"}, 3),
    ({"label": "Notifications, 9+ unread", "badge": "9+"}, 9),
    ({"label": "Notifications", "badge": "9+"}, 9),
    ({"label": "Notifications", "badge": "99+"}, 99),
    ({"label": "Notifications", "badge": ""}, 1),
    ({"label": "Notifications, 0 unread"}, 0),
    ({"label": "Notifications", "badge": "0"}, 0),
    ({"label": "Notifications", "child": "4"}, 4),
    ({"label": "Notifications", "child": "12+"}, 12),
    ({"label": "You have unread notifications"}, 1),
    ({"label": "Notifications"}, None),
    ({"label": ""}, None),
]
body("badges", "return [%s, api.parseBellBadgeCount(null)];" % ", ".join(
    "api.parseBellBadgeCount(stub.bell(%s))" % json.dumps(c) for c, _ in BADGE_CASES))

# S10: the nothing-to-watch heading.
body("not_eligible", "return %s.map((t) => api.isNotEligibleText(t));" % json.dumps([
    "No Content Eligible", "no  content\neligible", OWNER_TEXT, "Watch alice", "", "Content is eligible",
]))

# F31 (A39): where a candidate counts.
body("scope", r"""
  const at = (path, inside) => { location.pathname = path; return api.streakCandidateSource(stub.scoped(inside)); };
  const out = [
    at("/alice", ["popover"]),
    at("/alice", []),
    at("/alice", ["chat"]),
    at("/alice", ["sidebar"]),
    at("/directory", []),
    at("/directory/following", []),
    at("/directory", ["chat"]),
    at("/notifications", []),
    at("/inventory", ["popover"]),
    at("/videos/123", []),
    at("/popout/alice/chat", []),
    at("/save-streak/alice", []),
    at("/", []),
    at("/wallet", []),
    at("/drops/inventory", []),
    api.streakCandidateSource(null),
  ];
  location.pathname = "/directory";
  return out;
""")

# P6 (3.5.3): card identity with the numbers the desktop tests use.
body("relation", r"""
  const MIN = 60, HOUR = 3600, T = 1790000000;
  const rec = (detected, age, unit) => age === null
    ? { break_at: detected, unit: 0 }
    : { break_at: detected - age - unit, unit };
  const first = rec(T + 70 * MIN, 3600, 3600);
  const second = rec(T + 110 * MIN, 3600, 3600);
  const twoHours = rec(T + 125 * MIN, 7200, 3600);
  const inDanger = rec(T, 20 * MIN, 60);
  const reissued = rec(T + 5 * HOUR, 10 * MIN, 60);
  const stored = rec(T, 2 * MIN, 60);
  const older = rec(T, 3 * HOUR, 3600);
  const known = rec(T, 3600, 3600);
  const unknown = rec(T + 9 * HOUR, null, null);
  const r = api.streakCardRelation;
  return [
    r(first, second), r(second, first), r(first, twoHours),
    r(inDanger, reissued), r(reissued, inDanger),
    r(stored, older),
    r(known, unknown), r(unknown, known), r(unknown, rec(T, null, null)),
  ];
""")
body("escalation", r"""
  const MIN = 60000, HOUR = 3600000, DAY = 86400000, T = 1790000000000, SLACK = 3600000;
  const rec = (detected, age, unit, deadline) => ({
    break_at: detected - age - unit, unit, deadline_at: deadline === undefined ? null : deadline });
  const stored = rec(T, 20 * MIN, 60000, T + 10 * HOUR);
  const atLimit = rec(T + MIN, 21 * MIN, 60000, T + 10 * HOUR - SLACK - 60000);
  const over = rec(T + MIN, 21 * MIN, 60000, T + 10 * HOUR - SLACK - 61000);
  const drift = rec(T + MIN, 21 * MIN, 60000, T + 10 * HOUR - SLACK);
  const hourUnit = rec(T, HOUR, 3600000, T + 10 * HOUR);
  // Mixed units, the larger one on either side: the threshold is the slack
  // plus the larger unit, never the smaller one or one side's alone.
  const hr23 = rec(T, 23 * HOUR, 3600000, T + 24 * HOUR);
  const dayIn = rec(T + MIN, DAY, 86400000, T + MIN);
  const dayInOver = rec(T + MIN, DAY, 86400000, T + 24 * HOUR - SLACK - DAY - 1000);
  const dayRec = rec(T, DAY, 86400000, T + 48 * HOUR);
  const fineIn = rec(T + MIN, 30 * HOUR, 1000, T + 48 * HOUR - SLACK - 2 * HOUR);
  const fineOver = rec(T + MIN, 30 * HOUR, 1000, T + 48 * HOUR - SLACK - DAY - 1000);
  const e = api.streakIsEscalation;
  const r = api.streakCardRelation;
  return [
    api.streakCardRelation(stored, over),
    e(stored, over), e(stored, atLimit), e(stored, drift),
    e(hourUnit, rec(T, HOUR, 3600000, T + 10 * HOUR - 2 * HOUR)),
    e(hourUnit, rec(T, HOUR, 3600000, T + 10 * HOUR - 2 * HOUR - 1000)),
    e(stored, rec(T + 5 * HOUR, MIN, 60000, T)),
    e(stored, rec(T, 20 * MIN, 60000, null)),
    r(hr23, dayIn), r(dayRec, fineIn),
    e(hr23, dayIn), e(hr23, dayInOver), e(dayRec, fineIn), e(dayRec, fineOver),
  ];
""")

_RESULTS = {}


def _results(browser: str, tmp_path_factory) -> dict:
    if browser not in _RESULTS:
        tmp = tmp_path_factory.mktemp(f"content_{browser}")
        harness = tmp / "harness.js"
        harness.write_text(BODY_HARNESS, encoding="utf-8")
        cases = tmp / "bodies.json"
        cases.write_text(json.dumps(BODIES), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), str(CONTENT_SCRIPTS[browser]), str(cases)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert result.returncode == 0, result.stderr
        _RESULTS[browser] = json.loads(result.stdout)
    return _RESULTS[browser]


@pytest.fixture(params=sorted(CONTENT_SCRIPTS))
def run(request, tmp_path_factory):
    browser = request.param
    results = _results(browser, tmp_path_factory)

    def get(case_id):
        got = results[case_id]
        assert got["ok"], f"{browser} {case_id}: {got.get('error')}"
        return got["value"]
    return get


def test_ap02_age_labels_read_as_seconds_and_their_unit(run):
    for (label, expected), got in zip(AGE_LABELS.items(), run("age_labels")):
        assert got == expected, f"parseTimeAgo({label!r}) returned {got!r}"
    assert run("age_seconds_wrapper") == [72000, None, 0]


def test_ap02_a_time_datetime_card_sends_a_whole_number_of_seconds(run):
    got = run("event_datetime")
    assert got["isInt"] is True
    assert got["age"] == 5400
    assert got["unit"] == 1
    ev = got["ev"]
    assert ev["status"] == "broke" and ev["streamer"] == "fresh" and ev["count"] == 8
    assert ev["deadline_hours"] == 24
    assert ev["login_verified"] is True and ev["source"] == "bell"
    assert ev["save_url"] == "https://www.twitch.tv/save-streak/fresh"
    assert ev["detected_at"] == "2026-09-29T12:00:00.000Z"


def test_ap02_the_age_comes_from_a_leaf_label_title_or_aria_label(run):
    assert run("event_leaf_label") == [10800, 3600, "bob", True, "page"]
    assert run("event_title_date") == [90, 1, 120, 60, None, None]


def test_ap01_an_unknown_age_is_sent_as_null(run):
    ev = run("event_unknown_age")
    assert ev["card_age_s"] is None and ev["card_age_unit_s"] is None
    assert set(ev) == {"status", "streamer", "count", "deadline_hours", "card_age_s", "card_age_unit_s",
                       "login_verified", "source", "save_url", "detected_at", "page_url"}


def test_ap02_ap03_a_week_label_that_passes_the_gate_goes_out_in_days(run):
    assert run("event_week_unit") == [604800, 86400, True]


def test_ap03_the_age_gate_drops_a_day_old_broke_card_and_an_in_danger_card_past_its_deadline(run):
    assert run("age_gate") == [True, False, True, True, False, True]
    assert run("age_gate_labels") == [False, False, True, False, False, True]


def test_ap05_the_login_comes_from_a_save_streak_link_or_the_sentence(run):
    for (args, expected), got in zip(LOGIN_CASES, run("logins")):
        assert got == expected, f"streakLoginFor{tuple(args)!r} returned {got!r}"


def test_ap05_the_reserved_names_are_never_verified(run):
    got = run("reserved")
    assert got["list"] == RESERVED
    assert got["verified"] == [False] * len(RESERVED)


def test_ab6_badge_parsing(run):
    got = run("badges")
    assert got == [expected for _, expected in BADGE_CASES] + [None]


def test_as10_not_eligible_text_is_the_heading_without_the_maintained_sentence(run):
    assert run("not_eligible") == [True, True, False, False, False, False]


def test_f31_candidates_count_only_in_the_dropdown_or_on_a_listed_page_outside_chat(run):
    assert run("scope") == [
        "bell",  # the dropdown on a channel page
        None,    # a neutral chat container on a channel page
        None,    # the chat guard on a channel page
        None,    # the sidebar is for links only
        "page",  # a listed page
        "page",
        None,    # the chat guard on a listed page
        "page",
        "bell",  # the dropdown wins on a listed page
        None,    # VOD chat replay
        None,    # popout chat
        None,    # a save-streak page
        None,    # the front page
        "page",
        "page",
        None,
    ]


def test_ap06_c05_card_identity_matches_the_desktop(run):
    assert run("relation") == [
        "same", "same", "same",
        "newer", "older",
        "older",
        "same", "same", "same",
    ]


def test_ap06_c05_escalation_needs_more_than_the_slack_plus_the_larger_unit(run):
    assert run("escalation") == [
        "same", True, False, False, False, True, False, False,
        "same", "same", False, True, False, True,
    ]


# ---------------------------------------------------------------------------
# 1.12: the card walk and the render wait against a small DOM.
#
# Each body gets a fresh copy of content.js loaded as a frame (so only the
# bonus claimer starts), a fake clock (Date and setTimeout), a document
# built from a few element and text classes with compound selectors (tag,
# #id, .class, [attr], [attr="v"], [attr*="v" i], :has(...)) joined by
# descendant combinators (a space; any other combinator throws), and the
# messages the page sends. Bodies are async: (env, h, lib, NOW). Intervals
# fire only in an env made with intervals: true.
#
# lib.topEnv(opts) loads another copy as the page itself, so every feature
# starts: the background's messages go through env.ask, document listeners
# get trusted events through env.fire and env.userClick, and a small
# MutationObserver delivers childList and attribute records in a microtask,
# as a browser does. lib.bellPage builds a top-bar bell whose click opens
# the dropdown and marks everything read. lib.todayPage builds today's top
# bar from the recorded markup (plan A46, read with a small HTML reader,
# lib.parse), env.move makes an in-place move and opts.before builds the
# page the script finds at load.
# ---------------------------------------------------------------------------

DOM_HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [file, casesFile] = process.argv.slice(2);
const source = fs.readFileSync(file, "utf8");
const bodies = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const NOW = Date.parse("2026-09-29T12:00:00.000Z");
const noop = () => {};
const flush = () => new Promise((resolve) => setImmediate(resolve));

function splitList(sel) {
  const out = [];
  let depth = 0;
  let quote = null;
  let cur = "";
  for (const ch of sel) {
    if (quote) { if (ch === quote) quote = null; cur += ch; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    if (ch === "]" || ch === ")") depth--;
    if (ch === "," && depth === 0) { out.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}
// The index of the ")" that closes the "(" at open, quotes respected.
function closingParen(s, open) {
  let depth = 0;
  let quote = null;
  for (let j = open; j < s.length; j++) {
    const ch = s[j];
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === "(") depth++;
    if (ch === ")" && --depth === 0) return j;
  }
  return -1;
}
// One complex selector split at its descendant combinators (white space
// outside brackets, parentheses and quotes). Any other combinator throws.
function splitCompounds(part, sel) {
  const out = [];
  let depth = 0;
  let quote = null;
  let cur = "";
  for (const ch of part) {
    if (quote) { if (ch === quote) quote = null; cur += ch; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    if (ch === "]" || ch === ")") depth--;
    if (depth === 0 && (ch === ">" || ch === "+" || ch === "~")) throw new Error("unsupported selector: " + sel);
    if (depth === 0 && /\s/.test(ch)) { if (cur) out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}
const TOKEN = /(#[\w-]+)|(\.[\w-]+)|\[\s*([\w-]+)\s*(?:([*^$~]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*(i)?\s*)?\]/y;
function parseCompound(part, sel) {
  const m = /^(\*|[a-zA-Z][a-zA-Z0-9-]*)?/.exec(part);
  const compound = { tag: m[1] && m[1] !== "*" ? m[1].toLowerCase() : null, tests: [] };
  let i = m[0].length;
  while (i < part.length) {
    // :has(<selector list>): a descendant matches it.
    if (part.startsWith(":has(", i)) {
      const close = closingParen(part, i + 4);
      if (close < 0) throw new Error("unsupported selector: " + sel);
      compound.tests.push({ has: part.slice(i + 5, close) });
      i = close + 1;
      continue;
    }
    TOKEN.lastIndex = i;
    const t = TOKEN.exec(part);
    if (!t) throw new Error("unsupported selector: " + sel);
    i = TOKEN.lastIndex;
    if (t[1]) compound.tests.push({ attr: "id", op: "=", value: t[1].slice(1) });
    else if (t[2]) compound.tests.push({ attr: "class", op: "~=", value: t[2].slice(1) });
    else compound.tests.push({ attr: t[3], op: t[4] || null, value: t[5] ?? t[6] ?? t[7] ?? null, ci: !!t[8] });
  }
  return compound;
}
// A selector list as one chain of compounds per selector, outermost first.
const parsedSelectors = new Map();
function parseSelector(sel) {
  if (parsedSelectors.has(sel)) return parsedSelectors.get(sel);
  const list = splitList(sel).map((part) => splitCompounds(part, sel).map((c) => parseCompound(c, sel)));
  parsedSelectors.set(sel, list);
  return list;
}
function compoundMatches(el, c) {
  return (!c.tag || c.tag === el.tagName.toLowerCase()) &&
    c.tests.every((t) => (t.has !== undefined ? el.querySelector(t.has) !== null : attrMatches(el, t)));
}
function attrMatches(el, t) {
  let v = el.getAttribute(t.attr);
  if (v === null) return false;
  if (!t.op) return true;
  let want = t.value;
  if (t.ci) { v = v.toLowerCase(); want = want.toLowerCase(); }
  if (t.op === "=") return v === want;
  if (t.op === "*=") return want !== "" && v.includes(want);
  if (t.op === "^=") return want !== "" && v.startsWith(want);
  if (t.op === "$=") return want !== "" && v.endsWith(want);
  if (t.op === "~=") return v.split(/\s+/).includes(want);
  return false;
}
// The env of the body running now (a body may make its own).
let currentEnv = null;

// Queues a mutation record for every observer that watches target, and
// delivers each observer's queue in a microtask, as a browser does.
function mutated(target, record) {
  const env = currentEnv;
  if (!env) return;
  for (const obs of env.observers) {
    const hit = obs.targets.find(({ node, opts }) => node === target || (opts.subtree && node.contains(target)));
    if (!hit) continue;
    const o = hit.opts;
    if (record.type === "childList" && !o.childList) continue;
    if (record.type === "attributes" &&
      (!o.attributes || (o.attributeFilter && !o.attributeFilter.includes(record.attributeName)))) continue;
    obs.queue.push({ addedNodes: [], removedNodes: [], attributeName: null, ...record, target });
    if (!obs.scheduled) {
      obs.scheduled = true;
      queueMicrotask(() => obs.deliver());
    }
  }
}
class FakeMutationObserver {
  constructor(cb) {
    this.cb = cb;
    this.targets = [];
    this.queue = [];
    this.scheduled = false;
    this.env = currentEnv;
    if (currentEnv) currentEnv.observers.push(this);
  }
  observe(node, opts) { this.targets.push({ node, opts: opts || {} }); }
  disconnect() { this.targets = []; this.queue = []; }
  deliver() {
    this.scheduled = false;
    const records = this.queue;
    this.queue = [];
    if (!records.length || !this.targets.length) return;
    try {
      this.cb(records, this);
    } catch (e) {
      if (this.env) this.env.errors.push(String((e && e.stack) || e));
    }
  }
}

class TextNode {
  constructor(text) { this.nodeType = 3; this.data = String(text); this.parentElement = null; }
  get textContent() { return this.data; }
}
class El {
  constructor(tag, attrs) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = { ...(attrs || {}) };
    this.parentElement = null;
    this.childNodes = [];
    this.listeners = {};
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  get isConnected() { for (let n = this; n; n = n.parentElement) if (n.tagName === "HTML") return true; return false; }
  get isContentEditable() { return false; }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? String(this.attrs[name]) : null; }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    mutated(this, { type: "attributes", attributeName: name });
  }
  adopt(kid) {
    const node = typeof kid === "string" ? new TextNode(kid) : kid;
    const old = node.parentElement;
    if (old) {
      old.childNodes = old.childNodes.filter((n) => n !== node);
      mutated(old, { type: "childList", removedNodes: [node] });
    }
    node.parentElement = this;
    return node;
  }
  append(...kids) {
    const nodes = kids.flat().map((k) => this.adopt(k));
    this.childNodes.push(...nodes);
    if (nodes.length) mutated(this, { type: "childList", addedNodes: nodes });
    return this;
  }
  prepend(...kids) {
    const nodes = kids.flat().map((k) => this.adopt(k));
    this.childNodes.unshift(...nodes);
    if (nodes.length) mutated(this, { type: "childList", addedNodes: nodes });
    return this;
  }
  remove() {
    const old = this.parentElement;
    if (!old) return;
    old.childNodes = old.childNodes.filter((n) => n !== this);
    this.parentElement = null;
    mutated(old, { type: "childList", removedNodes: [this] });
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  // Runs the listeners of this element and its ancestors (the document's
  // own listeners are not reached).
  dispatch(ev) {
    for (let n = this; n; n = n.parentElement) for (const fn of (n.listeners[ev.type] || []).slice()) fn(ev);
  }
  click() { this.dispatch({ type: "click", isTrusted: false, target: this }); }
  focus() { if (currentEnv) currentEnv.document.activeElement = this; }
  blur() {
    if (currentEnv && currentEnv.document.activeElement === this) currentEnv.document.activeElement = currentEnv.body;
  }
  contains(other) { for (let n = other; n; n = n.parentElement) if (n === this) return true; return false; }
  // The last compound of a chain matches this element and each one before it
  // an ancestor further up, as a browser matches against the whole document.
  // The nearest matching ancestor is always a safe pick for descendant
  // combinators.
  matches(sel) {
    return parseSelector(sel).some((chain) => {
      if (!compoundMatches(this, chain[chain.length - 1])) return false;
      let n = this.parentElement;
      for (let k = chain.length - 2; k >= 0; k--) {
        while (n && !compoundMatches(n, chain[k])) n = n.parentElement;
        if (!n) return false;
        n = n.parentElement;
      }
      return true;
    });
  }
  closest(sel) { for (let n = this; n; n = n.parentElement) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.matches(sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function h(tag, attrs, ...kids) { return new El(tag, attrs).append(...kids); }

// A small HTML reader, enough for the recorded markup: elements, quoted and
// bare attributes, text and the usual entities; void elements and "<x/>"
// close at once. Nothing is reparented, so a <p> inside a <p> stays there,
// as in the DOM Twitch's scripts build. Returns the top-level nodes.
const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
  "track", "wbr"]);
function decodeEntities(s) {
  const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: String.fromCharCode(160) };
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (all, e) => {
    const k = e.toLowerCase();
    if (k[0] !== "#") return named[k];
    return String.fromCodePoint(k[1] === "x" ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10));
  });
}
function parseHtml(html) {
  const holder = new El("template", {});
  const stack = [holder];
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=\/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[1]) {
      const tag = m[1].toUpperCase();
      for (let k = stack.length - 1; k > 0; k--) if (stack[k].tagName === tag) { stack.length = k; break; }
    } else if (m[2]) {
      const attrs = {};
      const ar = /([^\s=\/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = ar.exec(m[3] || ""))) attrs[a[1].toLowerCase()] = decodeEntities(a[2] ?? a[3] ?? a[4] ?? "");
      const tag = m[2].toLowerCase();
      const el = new El(tag, attrs);
      top.append(el);
      if (!m[4] && !VOID_TAGS.has(tag)) stack.push(el);
    } else if (m[5] !== undefined) {
      top.append(decodeEntities(m[5]));
    }
  }
  const nodes = holder.childNodes.slice();
  holder.childNodes = [];
  for (const n of nodes) n.parentElement = null;
  return nodes;
}

// The markup recorded in the owner's logged-in Firefox on 2026-10-01 (plan
// A46), from the Python side: the bell and the already-kept modal verbatim,
// the dropdown and cards in their recorded structure.
const RECORDED = __RECORDED__;

const lib = {
  // A notification card as the harness's synthetic Twitch renders one:
  // body > text, an optional save-streak link, an optional age label or
  // time element.
  card(text, { ago, link, datetime } = {}) {
    const inner = h("div", { class: "notification-card__body" }, h("p", { class: "notification-card__text" }, text));
    if (link) inner.append(h("a", { href: "/save-streak/" + link }, "Watch now"));
    if (datetime) inner.append(h("time", { datetime }, ago || ""));
    else if (ago) inner.append(h("span", { class: "notification-card__age" }, ago));
    return h("div", { class: "notification-card" }, inner);
  },
  popover(...cards) {
    return h("div", { "data-a-target": "onsite-notifications-popover", role: "dialog" },
      h("h4", { class: "notifications-header" }, "Notifications"),
      h("div", { class: "notification-list" }, ...cards));
  },
  live(login, ago) { return lib.card(login + " is live: Just Chatting with the community", { ago }); },
  sent(env, type) { return env.sent.filter((m) => m.type === type); },
  // Another copy of content.js loaded as a frame, or as the page itself.
  frameEnv(opts = {}) { return makeEnv({ ...opts, top: false }); },
  topEnv(opts = {}) { return makeEnv({ ...opts, top: true }); },
  // The card events a scan of root sent in env e.
  scanEvents(e, root) {
    const from = e.sent.length;
    e.api.scanAndReport({ root });
    return e.sent.slice(from).filter((m) => m.type === "streak_event").map((m) => m.event);
  },
  // Every card event env e sent, as "status:streamer:count:age:source".
  brief(e) {
    return lib.sent(e, "streak_event").map((m) => m.event)
      .map((v) => `${v.status}:${v.streamer}:${v.count}:${v.card_age_s}:${v.source}`);
  },
  // A top-bar bell with a search input before it. A click toggles the
  // dropdown (cards: [text, lib.card options] pairs) and marks everything
  // read. lost: that many first clicks do nothing; mountDelayMs: the
  // dropdown mounts that long after the click; focusInput: the click also
  // moves focus into the input, as a page may. clicks: {at, trusted}, at in
  // ms after the env loaded.
  bellPage(e, { badge = 0, cards = [], lost = 0, mountDelayMs = 0, focusInput = false } = {}) {
    const bell = h("button", { "data-a-target": "onsite-notifications-toggle__button", "aria-label": "Notifications" });
    const input = h("input", { id: "sm-input", type: "text" });
    const page = { bell, input, clicks: [], lost };
    const popNow = () => e.body.querySelector('[data-a-target="onsite-notifications-popover"]');
    page.setBadge = (n) => {
      const old = bell.querySelector(".notification-badge");
      if (old) old.remove();
      bell.setAttribute("aria-label", n ? `Notifications, ${n} unread` : "Notifications");
      if (n) bell.append(h("span", { class: "notification-badge" }, String(n)));
    };
    page.isOpen = () => !!popNow();
    page.mount = () => {
      e.body.append(lib.popover(...cards.map(([text, o]) => lib.card(text, o))));
      page.setBadge(0);
    };
    bell.addEventListener("click", (ev) => {
      page.clicks.push({ at: e.now - NOW, trusted: ev.isTrusted });
      if (focusInput) input.focus();
      const open = popNow();
      if (open) { open.remove(); return; }
      if (page.lost > 0) { page.lost -= 1; return; }
      if (mountDelayMs) e.later(mountDelayMs, page.mount);
      else page.mount();
    });
    e.body.append(h("nav", { class: "top-nav" }, input, bell));
    page.setBadge(badge);
    return page;
  },
  // The recorded markup (RECORDED), for the bodies.
  recorded: RECORDED,
  // Nodes from HTML text (parseHtml), and the first element of it.
  parse(html) { return parseHtml(html); },
  el(html) { return parseHtml(html).find((n) => n.nodeType === 1) || null; },
  // Today's top bar (plan A46): the toast container (toastHtml inside it)
  // and the recorded bell, verbatim. A click toggles the recorded dropdown,
  // a dialog appended to the body: its balloon and header at once, the
  // cards (HTML strings) renderDelayMs later. Opening marks nothing read.
  // chat: lines (HTML strings) in a chat scroller, or none.
  todayPage(e, { cards = [], renderDelayMs = 0, toastHtml = "", chat = null } = {}) {
    const wrap = lib.el(RECORDED.bell);
    const bell = wrap.querySelector("button");
    const toast = h("div", { "data-test-selector": "onsite-notifications-toast-manager" }, ...lib.parse(toastHtml));
    const page = { bell, wrap, toast, clicks: [], dialog: null };
    page.isOpen = () => !!page.dialog && page.dialog.isConnected;
    page.open = () => {
      const dialog = lib.el(RECORDED.dropdown);
      e.body.append(dialog);
      page.dialog = dialog;
      const list = dialog.querySelector('[data-test-selector="center-window__content"]').children[0];
      const fill = () => { if (page.dialog === dialog) for (const c of cards) list.append(...lib.parse(c)); };
      if (renderDelayMs) e.later(renderDelayMs, fill);
      else fill();
    };
    page.close = () => {
      if (page.dialog) page.dialog.remove();
      page.dialog = null;
    };
    bell.addEventListener("click", (ev) => {
      page.clicks.push({ at: e.now - NOW, trusted: ev.isTrusted });
      if (page.isOpen()) page.close();
      else page.open();
    });
    e.body.append(h("nav", { class: "top-nav" }, h("input", { id: "sm-input", type: "text" }), toast, wrap));
    if (chat) {
      page.chat = h("div", { "data-a-target": "chat-scroller" }, ...chat.map((c) => h("div", { class: "chat-line__message" },
        ...lib.parse(c))));
      e.body.append(h("section", { class: "stream-chat" }, page.chat));
    }
    return page;
  },
  // The recorded already-kept dialog (body > div > div > dialog > div >
  // div.tw-modal), the count and name swapped in when given.
  keptModal(count, name) {
    let html = RECORDED.modal;
    if (count !== undefined) html = html.replace("31-stream streak with TheAmatorium", `${count}-stream streak with ${name}`);
    return lib.el(html);
  },
  // Resolves {value, at}: what p resolved to and when, in ms of the fake
  // clock after the call, stepping the clock stepMs at a time.
  async settle(env, p, stepMs = 50, maxMs = 20000) {
    const start = env.now;
    let done = false;
    let value;
    let at = null;
    p.then((v) => { done = true; value = v; at = env.now - start; });
    await flush();
    while (!done && env.now - start < maxMs) await env.advance(stepMs);
    return { value, at };
  },
};

function makeEnv(opts = {}) {
  const env = {
    now: NOW, timers: [], seq: 0, sent: [], sentAt: [], lookups: 0, observers: [], listeners: [], docListeners: {},
    errors: [],
  };
  currentEnv = env;
  class FakeDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(env.now); }
    static now() { return env.now; }
  }
  const html = h("html", {});
  const body = h("body", {});
  html.append(body);
  const path = opts.path || "/alice";
  const location = { pathname: path, search: "", href: "https://www.twitch.tv" + path };
  // A frame (window.top differs) by default: only the bonus claimer starts
  // at load. opts.top loads the script as the page itself.
  const win = { location };
  win.top = opts.top ? win : {};
  const ext = {
    runtime: {
      onMessage: { addListener: (fn) => env.listeners.push(fn) },
      sendMessage: (m) => {
        env.sent.push(JSON.parse(JSON.stringify(m)));
        env.sentAt.push(env.now - NOW); // when, in ms after the env loaded
        return Promise.resolve();
      },
    },
    storage: {
      local: { get: (key, cb) => { if (cb) cb({}); return Promise.resolve({}); } },
      onChanged: { addListener: noop },
    },
  };
  const document = {
    body,
    documentElement: html,
    querySelector: (sel) => { env.lookups++; return html.querySelector(sel); },
    querySelectorAll: (sel) => html.querySelectorAll(sel),
    addEventListener: (type, fn) => { (env.docListeners[type] = env.docListeners[type] || []).push(fn); },
    visibilityState: opts.visibility || "visible",
    activeElement: body,
    hasFocus: () => true,
  };
  const sandbox = {
    window: win, location, document, Date: FakeDate,
    MutationObserver: FakeMutationObserver,
    MouseEvent: class {},
    console: { log: noop, warn: noop, error: noop },
    setTimeout: (fn, ms) => { const id = ++env.seq; env.timers.push({ id, at: env.now + Math.max(0, ms || 0), fn }); return id; },
    clearTimeout: (id) => { env.timers = env.timers.filter((t) => t.id !== id); },
    // Intervals run only with opts.intervals (the route watcher, the minute
    // ticks); otherwise they never fire, as before.
    setInterval: (fn, ms) => {
      if (!opts.intervals) return 0;
      const id = ++env.seq;
      const every = Math.max(1, ms || 0);
      env.timers.push({ id, at: env.now + every, fn, every });
      return id;
    },
    clearInterval: (id) => { env.timers = env.timers.filter((t) => t.id !== id); },
    module: { exports: {} },
    chrome: ext,
    browser: ext,
  };
  vm.createContext(sandbox);
  // opts.before(body) builds the page the script finds when it loads.
  if (opts.before) opts.before(body);
  vm.runInContext(source, sandbox, { filename: file });
  Object.assign(env, { api: sandbox.module.exports, document, body, location });
  // Runs every timer due within ms, in order, letting promises settle after each.
  env.advance = async (ms) => {
    const until = env.now + ms;
    for (;;) {
      await flush();
      const due = env.timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      env.timers = env.timers.filter((t) => t !== due);
      env.now = due.at;
      if (due.every) env.timers.push({ ...due, at: due.at + due.every });
      due.fn();
    }
    env.now = until;
    await flush();
  };
  env.later = (ms, fn) => sandbox.setTimeout(fn, ms);
  // A message from the background (top only): resolves with the answer.
  env.ask = (message) => new Promise((resolve, reject) => {
    if (!env.listeners.length) { reject(new Error("no message listener: a frame")); return; }
    env.listeners[0](message, {}, resolve);
  });
  // A trusted document event, for the script's own document listeners.
  env.fire = (type, extra = {}) => {
    for (const fn of (env.docListeners[type] || []).slice()) fn({ type, isTrusted: true, ...extra });
  };
  // Your click: a trusted pointerdown, then the click.
  env.userClick = (el) => {
    env.fire("pointerdown", { target: el });
    el.dispatch({ type: "click", isTrusted: true, target: el });
  };
  env.setVisibility = (state) => {
    document.visibilityState = state;
    env.fire("visibilitychange");
  };
  // An in-place move (history.pushState): the same document, a new path.
  env.move = (path) => {
    const [pathname, query] = path.split("?");
    location.pathname = pathname;
    location.search = query ? "?" + query : "";
    location.href = "https://www.twitch.tv" + path;
  };
  return env;
}

(async () => {
  const AsyncFunction = (async () => {}).constructor;
  const out = {};
  for (const [id, body] of Object.entries(bodies)) {
    try {
      const env = makeEnv();
      const value = await new AsyncFunction("env", "h", "lib", "NOW", body)(env, h, lib, NOW);
      out[id] = { ok: true, value: value === undefined ? null : value };
    } catch (e) {
      out[id] = { ok: false, error: String((e && e.stack) || e) };
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

DOM_BODIES = {}


def dom_body(case_id: str, js: str) -> str:
    DOM_BODIES[case_id] = PICK + js
    return case_id


# Shared by every DOM body: one popover scan, its reply, and the messages it
# sent, card events reduced to the fields the P4 cases compare.
PICK = r"""
  const pick = (ev) => ({ streamer: ev.streamer, count: ev.count, age: ev.card_age_s, unit: ev.card_age_unit_s,
    verified: ev.login_verified, source: ev.source });
  const scan = (pop) => {
    const from = env.sent.length;
    const reply = env.api.scanAndReport({ root: pop });
    const sent = env.sent.slice(from);
    return {
      reply,
      events: sent.filter((m) => m.type === "streak_event").map((m) => pick(m.event)),
      unparsed: sent.filter((m) => m.type === "streak_unparsed").map((m) => m.text),
    };
  };
"""

# P4: a streak card among other notifications (the go-live kind) keeps its
# own age, whichever neighbor is older or younger and however many there are.
dom_body("p4_neighbors", r"""
  const read = (login, neighborAges, streakAgo) => {
    const pop = lib.popover(...neighborAges.map((a, i) => lib.live("host" + i, a)),
      lib.card(`Your 5-stream streak on ${login} broke`, { ago: streakAgo }));
    env.body.append(pop);
    const got = { length: pop.textContent.length, ...scan(pop) };
    pop.remove();
    return got;
  };
  const minutes = (n) => Array.from({ length: n }, (_, i) => `${i + 1} minute${i ? "s" : ""} ago`);
  return {
    older: read("alice", ["2 hours ago"], "2 days ago"),
    younger: read("amy", ["3 minutes ago"], "20 minutes ago"),
    fiveOlder: read("ann", ["2 hours ago", "5 hours ago", "6 hours ago", "7 hours ago", "9 hours ago"], "2 days ago"),
    fiveYounger: read("ava", minutes(5), "20 minutes ago"),
    tenOlder: read("abe", ["1 hour ago", "2 hours ago", "3 hours ago", "4 hours ago", "5 hours ago", "6 hours ago",
      "7 hours ago", "8 hours ago", "9 hours ago", "10 hours ago"], "2 days ago"),
    tenYounger: read("ada", minutes(10), "22 minutes ago"),
  };
""")

# P4 and P6: a card between two dated items, read again after a new
# notification arrives on top.
dom_body("p4_new_top_item", r"""
  const read = (login, streakAgo) => {
    const pop = lib.popover(lib.live("bob", "5 minutes ago"),
      lib.card(`Your 5-stream streak on ${login} broke`, { ago: streakAgo }), lib.live("dave", "3 hours ago"));
    env.body.append(pop);
    const first = scan(pop);
    env.now += 60000;
    pop.querySelector(".notification-list").prepend(lib.live("erin", "just now"));
    const second = scan(pop);
    const length = pop.textContent.length;
    pop.remove();
    return { first, second, length };
  };
  return { old: read("carol", "2 days ago"), fresh: read("cora", "3 hours ago") };
""")

# P4: an undated card among dated ones stays undated.
dom_body("p4_undated", r"""
  const pop = lib.popover(lib.live("bob", "5 minutes ago"), lib.card("Your 9-stream streak on carol broke"),
    lib.live("dave", "1 hour ago"));
  env.body.append(pop);
  return scan(pop);
""")

# P4, P5, P7, P8: a reworded card with its own save-streak link next to a
# matched card, with and without the matched card's own link. The matched
# card keeps its name; the link goes out as a link event and the rewording
# as a near miss.
for _id, _link in (("p4_near_miss_link_own", '"alice"'), ("p4_near_miss_link_none", "undefined")):
    dom_body(_id, r"""
  const pop = lib.popover(
    lib.card("Your watch streak with bob has ended", { ago: "1 hour ago", link: "bob" }),
    lib.card("Your 5-stream streak on alice broke", { ago: "20 minutes ago", link: %s }));
  env.body.append(pop);
  return scan(pop);
""" % _link)

# P4, P2: the sentence wrapped in the card's own save-streak link, its age
# label a sibling of the link, among dated neighbors.
dom_body("p4_link_wraps_sentence", r"""
  const dana = h("div", { class: "notification-card" },
    h("a", { href: "/save-streak/dana" }, h("p", {}, "Your 4-stream streak on Dana broke")),
    h("span", {}, "4 hours ago"));
  const pop = lib.popover(lib.live("bob", "1 minute ago"), dana, lib.live("erin", "2 minutes ago"));
  env.body.append(pop);
  return scan(pop);
""")

# P4: two sentences in one container (CB10's pair) each keep their own age;
# a /popout/ link never names a card.
dom_body("p4_pair", r"""
  const pair = h("div", { class: "pair" },
    h("div", { class: "c1" }, h("p", {}, "Your 12-stream streak on Bob broke"),
      h("a", { href: "/popout/bob/chat" }, "Chat"), h("span", {}, "2 hours ago")),
    h("div", { class: "c2" }, h("p", {}, "Your 3-stream streak on Carol broke"), h("span", {}, "5 hours ago")));
  const holder = h("div", { class: "notification-card" }, pair, h("a", { href: "/popout/bob/chat" }, "Open chat"));
  const pop = lib.popover(holder);
  env.body.append(pop);
  return scan(pop);
""")

# B5: the render wait. The dropdown can mount after the click returns.
dom_body("b5_first_look", r"""
  env.lookups = 0;
  const p = env.api.waitForBellRender();
  const syncLookups = env.lookups;
  env.body.append(lib.popover(lib.card("Your 3-stream streak on carol broke", { ago: "4 hours ago" })));
  const res = await lib.settle(env, p);
  return { syncLookups, res };
""")
dom_body("b5_late_mounts", r"""
  const cards = () => lib.popover(lib.card("Your 3-stream streak on carol broke", { ago: "4 hours ago" }));
  const popNow = () => env.body.querySelector('[data-a-target="onsite-notifications-popover"]');
  const microtask = env.api.waitForBellRender();
  Promise.resolve().then(() => env.body.append(cards()));
  const a = await lib.settle(env, microtask);
  popNow().remove();

  const later = env.api.waitForBellRender();
  env.later(300, () => env.body.append(cards()));
  const b = await lib.settle(env, later);
  popNow().remove();

  // Mounted empty at once, its list only after 3 s.
  const shell = h("div", { "data-a-target": "onsite-notifications-popover", role: "dialog" });
  const slow = env.api.waitForBellRender();
  env.body.append(shell);
  env.later(3000, () => shell.append(h("p", {}, "Your 3-stream streak on carol broke")));
  const c = await lib.settle(env, slow);
  shell.remove();
  return [a, b, c];
""")
dom_body("b5_gives_up", r"""
  const never = await lib.settle(env, env.api.waitForBellRender());
  const shell = h("div", { "data-a-target": "onsite-notifications-popover", role: "dialog" });
  const emptyWait = env.api.waitForBellRender();
  env.body.append(shell);
  const empty = await lib.settle(env, emptyWait);
  shell.remove();
  const pop = lib.popover(lib.card("Your 3-stream streak on carol broke", { ago: "4 hours ago" }));
  const closedWait = env.api.waitForBellRender();
  env.body.append(pop);
  env.later(600, () => pop.remove());
  const closed = await lib.settle(env, closedWait);
  return { never, empty, closed };
""")

# B5: at the cap the wait goes on with what the dropdown shows. A list that
# renders at 7.4 s (a slow first fetch) and text that keeps changing past
# 8 s are both read; only an empty dropdown is "nothing rendered".
dom_body("b5_text_at_the_cap", r"""
  const pop = () => h("div", { "data-a-target": "onsite-notifications-popover", role: "dialog" });
  const shell = pop();
  const lateWait = env.api.waitForBellRender();
  env.body.append(shell);
  env.later(7400, () => shell.append(h("p", {}, "Your 3-stream streak on carol broke")));
  const late = await lib.settle(env, lateWait);
  shell.remove();

  const ticking = pop();
  const busyWait = env.api.waitForBellRender();
  env.body.append(ticking);
  let n = 0;
  const tick = () => { ticking.append(h("span", {}, String(n++))); if (n < 30) env.later(500, tick); };
  tick();
  const busy = await lib.settle(env, busyWait);
  ticking.remove();
  return { late, busy };
""")

# P2: a streamer's name never reads as an age. Each card is read in a fresh
# page among dated neighbors; the name sits in the sentence or in its own
# element. The age comes from the card's own label, so the age gate passes
# or drops the card by that label.
dom_body("p2_names_that_look_like_ages", r"""
  const strong = (name, rest, ago) => h("div", { class: "notification-card" },
    h("div", { class: "notification-card__body" },
      h("p", { class: "notification-card__text" }, "Your 12-stream streak on ", h("strong", {}, name), rest),
      h("span", { class: "notification-card__age" }, ago)));
  const plain = (name, rest, ago) => lib.card(`Your 12-stream streak on ${name}${rest}`, { ago });
  const read = (card) => {
    const e = lib.frameEnv();
    const pop = lib.popover(lib.live("bob", "5 minutes ago"), card, lib.live("dave", "3 hours ago"));
    e.body.append(pop);
    return lib.scanEvents(e, pop).map((v) => [v.streamer, v.card_age_s, v.card_age_unit_s]);
  };
  const names = ["yesterdayjam", "justnowgaming", "5hago"];
  const out = { broke: {}, danger: {}, strongBroke: {}, strongDanger: {} };
  for (const name of names) {
    out.broke[name] = [read(plain(name, " broke", "2 hours ago")), read(plain(name, " broke", "3 days ago"))];
    out.danger[name] = [read(plain(name, " ends in 5 hours", "2 hours ago")),
      read(plain(name, " ends in 5 hours", "6 hours ago"))];
  }
  for (const name of ["yesterday", "justnow", "5hago"]) {
    out.strongBroke[name] = [read(strong(name, " broke", "2 hours ago")), read(strong(name, " broke", "3 days ago"))];
    out.strongDanger[name] = [read(strong(name, " ends in 5 hours", "2 hours ago")),
      read(strong(name, " ends in 5 hours", "6 hours ago"))];
  }
  // The rescue sweep's links carry their card's age the same way.
  const e = lib.frameEnv();
  e.body.append(lib.popover(
    lib.card("Your 12-stream streak on yesterdayjam broke", { ago: "2 hours ago", link: "yesterdayjam" }),
    lib.card("Your 4-stream streak on justnowgaming broke", { ago: "3 days ago", link: "justnowgaming" })));
  out.links = e.api.saveStreakScanReply();
  return out;
""")

# P6 (plan 3.5.3): a scan that sees only part of a list. A card older than
# one the page already holds for the same streamer under another key is not
# sent; a newer one is, and so is a card of unknown age.
dom_body("p6_partial_views", r"""
  const cards = {
    broke: lib.card("Your 12-stream streak on bob broke", { ago: "10 minutes ago" }),
    danger: lib.card("Your 12-stream streak on bob ends in 5 hours", { ago: "3 hours ago" }),
    lower: lib.card("Your 11-stream streak on bob broke", { ago: "3 hours ago" }),
    undated: lib.card("Your 12-stream streak on bob ends in 4 hours"),
    newer: lib.card("Your 13-stream streak on bob ends in 20 hours", { ago: "1 minute ago" }),
    carol: lib.card("Your 7-stream streak on carol ends in 5 hours", { ago: "3 hours ago" }),
  };
  env.body.append(lib.popover(...Object.values(cards)));
  const one = (name) => scan(cards[name]).events.map((e) => `${e.streamer}:${e.count}:${e.age}`);
  return ["broke", "danger", "lower", "carol", "undated", "newer", "danger", "broke"].map(one);
""")

# P6 and B7: cards that Twitch inserts one by one into a list already on
# screen (the open dropdown, or a listed page) give one event per streamer,
# the newest, in either order; a card added in a later batch and cards
# removed one by one send nothing out of date.
dom_body("p6_cards_one_by_one", r"""
  const BROKE = ["Your 12-stream streak on bob broke", { ago: "10 minutes ago" }];
  const DANGER = ["Your 12-stream streak on bob ends in 5 hours", { ago: "3 hours ago" }];
  const CAROL = ["Your 7-stream streak on carol broke", { ago: "1 hour ago" }];
  const BOB12 = ["Your 12-stream streak on bob broke", { ago: "2 hours ago" }];
  const BOB13 = ["Your 13-stream streak on bob broke", { ago: "10 minutes ago" }];
  const add = (list, cards) => { for (const [text, o] of cards) list.append(lib.card(text, o)); };
  const run = async (path, steps) => {
    const t = lib.topEnv({ path });
    await t.advance(300);
    const list = h("div", { class: "notification-list" });
    if (path === "/notifications") {
      t.body.append(h("main", {}, list));
    } else {
      t.body.append(h("div", { "data-a-target": "onsite-notifications-popover", role: "dialog" },
        h("h4", {}, "Notifications"), list));
    }
    await t.advance(300);
    for (const step of steps) {
      step(list);
      await t.advance(300);
    }
    await t.advance(3000);
    return { events: lib.brief(t), errors: t.errors };
  };
  const removeAll = (list) => { for (const c of list.children.slice()) c.remove(); };
  const removeOne = (list) => { if (list.children.length) list.children[0].remove(); };
  return {
    brokeFirst: await run("/alice", [(l) => add(l, [BROKE, DANGER, CAROL])]),
    dangerFirst: await run("/alice", [(l) => add(l, [DANGER, BROKE, CAROL])]),
    pair: await run("/alice", [(l) => add(l, [BOB12, BOB13])]),
    pairReversed: await run("/alice", [(l) => add(l, [BOB13, BOB12])]),
    laterBatch: await run("/alice", [(l) => add(l, [BROKE, CAROL]), (l) => add(l, [DANGER])]),
    removedAtOnce: await run("/alice", [(l) => add(l, [BROKE, DANGER, CAROL]), removeAll]),
    removedApart: await run("/alice", [(l) => add(l, [BROKE, DANGER, CAROL]), removeOne, removeOne, removeOne]),
    removedDangerFirst: await run("/alice", [(l) => add(l, [DANGER, BROKE, CAROL]), removeOne, removeOne, removeOne]),
    listedPage: await run("/notifications", [(l) => add(l, [DANGER, BROKE, CAROL])]),
  };
""")

# B2 step 5: the open check's click is lost (nothing mounts within the
# render wait), then you open the dropdown yourself and read it without a
# key or another click. The check's retry 60 s later reads it and leaves it
# open. Without your input, a dropdown that mounts only after the wait is
# the check's own and the retry closes it.
dom_body("b2_late_mount_ownership", r"""
  const GINA = [["Your 7-stream streak on gina broke", { ago: "10 minutes ago" }]];
  const run = async (pageOpts, youOpenAt) => {
    const t = lib.topEnv();
    const page = lib.bellPage(t, { badge: 2, cards: GINA, ...pageOpts });
    await t.advance(500);
    const t0 = t.now;
    const first = await lib.settle(t, t.ask({ action: "checkBell", mode: "open" }));
    let openAfterYou = null;
    if (youOpenAt !== null) {
      await t.advance(t0 + youOpenAt - t.now);
      t.userClick(page.bell);
      openAfterYou = page.isOpen();
    }
    await t.advance(t0 + 80000 - t.now);
    const status = await t.ask({ action: "getStatus" });
    return {
      first: first.value,
      openAfterYou,
      openAtEnd: page.isOpen(),
      clicks: page.clicks.map((c) => [Math.round((c.at - (t0 - NOW)) / 1000), c.trusted]),
      result: status.lastBellCheckResult,
      events: lib.brief(t),
      errors: t.errors,
    };
  };
  return {
    youOpened: await run({ lost: 1 }, 28000),
    lateMount: await run({ mountDelayMs: 9500 }, null),
  };
""")

# B4 with B2 step 5: the hidden-tab check's click is lost; you come back to
# the tab, open the dropdown and leave again. The check's retry after the
# empty render reads your dropdown and leaves it open.
dom_body("b4_hidden_retry_leaves_your_dropdown", r"""
  const t = lib.topEnv({ visibility: "hidden" });
  const page = lib.bellPage(t, { badge: 2, lost: 1, cards: [["Your 7-stream streak on gina broke", { ago: "10 minutes ago" }]] });
  await t.advance(25000);
  t.setVisibility("visible");
  await t.advance(5000);
  t.userClick(page.bell);
  const openAfterYou = page.isOpen();
  await t.advance(10000);
  t.setVisibility("hidden");
  await t.advance(60000);
  return {
    openAfterYou,
    openAtEnd: page.isOpen(),
    clicks: page.clicks.map((c) => [Math.round(c.at / 1000), c.trusted]),
    events: lib.brief(t),
    errors: t.errors,
  };
""")

# B2 step 6: focus goes back only when the click moved it. You click into
# the chat box during the render wait: focus stays there. The page itself
# moves focus on the bell click and you do nothing: focus goes back to the
# button that had it.
dom_body("b2_focus_after_the_check", r"""
  const GINA = [["Your 7-stream streak on gina broke", { ago: "10 minutes ago" }]];
  const t = lib.topEnv();
  const page = lib.bellPage(t, { badge: 1, cards: GINA });
  await t.advance(500);
  const pending = t.ask({ action: "checkBell", mode: "open" });
  await t.advance(450);
  t.userClick(page.input);
  page.input.focus();
  const yours = await lib.settle(t, pending);

  const t2 = lib.topEnv();
  const page2 = lib.bellPage(t2, { badge: 1, cards: GINA, focusInput: true });
  const button = h("button", { id: "sm-other", type: "button" }, "other");
  t2.body.append(button);
  await t2.advance(500);
  button.focus();
  const pages = await lib.settle(t2, t2.ask({ action: "checkBell", mode: "open" }));
  const id = (e) => e.document.activeElement.getAttribute("id") || e.document.activeElement.tagName;
  return {
    yours: { reply: yours.value, active: id(t), open: page.isOpen() },
    pages: { reply: pages.value, active: id(t2), open: page2.isOpen() },
    errors: t.errors.concat(t2.errors),
  };
""")

# B6: on a hidden tab, a read clears the badge (no badge element at all).
# The next notification is still a rise and is read at once, not at the
# next minute tick.
dom_body("b6_badge_rise_after_a_read", r"""
  const t = lib.topEnv({ visibility: "hidden" });
  const page = lib.bellPage(t, { badge: 0, cards: [["Your 7-stream streak on gina broke", { ago: "10 minutes ago" }]] });
  await t.advance(11000);
  page.setBadge(1);
  await t.advance(4000);
  const firstClicks = page.clicks.length;
  await t.advance(75000 - (t.now - NOW));
  page.setBadge(1);
  await t.advance(5000);
  return { firstClicks, clicks: page.clicks.map((c) => Math.round(c.at / 1000)), errors: t.errors };
""")


# ---------------------------------------------------------------------------
# A46: today's Twitch, recorded in the owner's logged-in Firefox on
# 2026-10-01 (plan A46, X09). The bell and the already-kept modal are the
# recorded markup, verbatim. The dropdown and the cards follow the recorded
# structure: a dialog portal under the body > center-window balloon > ... >
# center-window content > div > one persistent-notification per card, whose
# link wraps body > p > p (the sentence) with the age label beside the body.
# TheAmatorium's card and the "about to reach" card are the recorded text;
# the other three broke cards use the same wording.
# ---------------------------------------------------------------------------

RECORDED_BELL_HTML = (
    '<div style="display: inherit;" data-test-selector="toggle-balloon-wrapper__mouse-enter-detector">'
    '<div class="InjectLayout-sc-1i43xsx-0 bohlnR">'
    '<button class="ScCoreButton-sc-ocjdkq-0 eBQIRH ScButtonIcon-sc-9yap0r-0 jcYIUn" aria-label="Open Notifications">'
    '<div class="ButtonIconFigure-sc-1emm8lf-0 jgYFuA"><div class="ScSvgWrapper-sc-wkgzod-0 cnGLHG tw-svg">'
    '<svg width="24" height="24" viewBox="0 0 24 24" focusable="false" aria-hidden="true" role="presentation">'
    '<path fill-rule="evenodd" d="M5 3h14l3 6v12H2V9l3-6Z"></path></svg></div></div></button></div></div>'
)
KEPT_SENTENCE = ("You've already maintained your 31-stream streak with TheAmatorium. "
                 "Keep'em going by watching more live streams!")
# The record's boxHtml: the tw-modal, 140 characters of text.
RECORDED_MODAL_BOX_HTML = (
    '<div class="Layout-sc-1xcs6mc-0 ScModalWrapper-sc-1k48se-0 RBThw dYkRIX tw-modal">'
    '<div class="ScModalHeader-sc-1j4ii0l-0 iWcnfN tw-modal-header">'
    '<div class="ScModalHeaderButton-sc-1j4ii0l-1 LIwjN tw-modal-header__button"></div>'
    '<div class="ScModalHeaderTitle-sc-1j4ii0l-2 bpyYFk tw-modal-header__title">'
    '<h2 id="WiclcsuZIU1MUylqbx9Zo4WbU2tw97Do-header" class="CoreText-sc-1txzju1-0 gUQtFU">No Content Eligible</h2>'
    '</div>'
    '<div class="ScModalHeaderButton-sc-1j4ii0l-1 eiKKbG tw-modal-header__button">'
    '<button class="ScCoreButton-sc-ocjdkq-0 foaenL ScButtonIcon-sc-9yap0r-0 cLnwrX" aria-label="Close Modal">'
    '<div class="ButtonIconFigure-sc-1emm8lf-0 dEsSMI"><div class="ScSvgWrapper-sc-wkgzod-0 cnGLHG tw-svg"><svg/>'
    '</div></div></button></div></div>'
    '<div class="Layout-sc-1xcs6mc-0 dPxRWu">' + KEPT_SENTENCE + '</div>'
    '<div class="Layout-sc-1xcs6mc-0 iFvAGF"><div class="Layout-sc-1xcs6mc-0">'
    '<a class="ScCoreButton-sc-ocjdkq-0 fvjDUd" rel="noopener noreferrer" target="_blank" '
    'href="https://help.twitch.tv/s/article/recover-watch-streaks">'
    '<div class="ScCoreButtonLabel-sc-s7h2b7-0 bfhate">'
    '<div data-a-target="tw-core-button-label-text" class="Layout-sc-1xcs6mc-0 zdujK">Learn More</div>'
    '</div></a></div></div></div>'
)
# Where it sat: body > div > div > div[role=dialog][aria-modal] > div > box.
RECORDED_MODAL_HTML = ('<div><div><div role="dialog" aria-modal="true"><div>' + RECORDED_MODAL_BOX_HTML +
                       '</div></div></div></div>')
RECORDED_DROPDOWN_HTML = (
    '<div role="dialog"><div data-test-selector="center-window__balloon"><div><div>'
    '<div><h4>Notifications</h4><button aria-label="Settings"></button><button aria-label="Close"></button></div>'
    '<div><button>My Twitch (1186)</button><button>My Channel (63)</button></div>'
    '<div><p>Last 24 Hours</p><button aria-label="Mark 1186 as Read"></button></div>'
    '<div data-test-selector="center-window__content"><div></div></div>'
    '</div></div></div></div>'
)


def recorded_card(sentence: str, ago: str, href: str) -> str:
    """One persistent-notification card in the recorded structure."""
    return ('<div data-test-selector="persistent-notification"><div>'
            '<a data-test-selector="persistent-notification__click" href="' + href + '"><div><div>'
            '<div data-test-selector="persistent-notification__body"><p><p>' + sentence + '</p></p></div>'
            '<div><span>' + ago + '</span></div>'
            '</div></div></a></div></div>')


SAVE_IT = " Watch a clip, VOD or stream in the next 24h to save it."
BROKE_SENTENCES = {
    "theamatorium": "Your 31-stream streak on TheAmatorium broke!" + SAVE_IT,
    "icozy": "Your 2-stream streak on iCozy broke!" + SAVE_IT,
    "bansheeboovt": "Your 5-stream streak on BansheeBooVT broke!" + SAVE_IT,
    "innocentofsin": "Your 77-stream streak on InnocentOfSin broke!" + SAVE_IT,
}
ABOUT_TO_REACH = ("You're about to reach a 7-stream watch streak! Join BansheeBooVT's stream now to earn your "
                  "streak rewards!")
RECORDED_CARDS = [
    recorded_card(ABOUT_TO_REACH, "3 minutes ago", "https://www.twitch.tv/bansheeboovt"),
    recorded_card(BROKE_SENTENCES["theamatorium"], "1 hour ago", "https://www.twitch.tv/save-streak/theamatorium"),
    recorded_card(BROKE_SENTENCES["icozy"], "3 hours ago", "https://www.twitch.tv/save-streak/icozy"),
    recorded_card(BROKE_SENTENCES["bansheeboovt"], "5 hours ago", "https://www.twitch.tv/save-streak/bansheeboovt"),
    recorded_card(BROKE_SENTENCES["innocentofsin"], "20 hours ago",
                  "https://www.twitch.tv/save-streak/innocentofsin"),
]
RECORDED = {
    "bell": RECORDED_BELL_HTML,
    "modal": RECORDED_MODAL_HTML,
    "dropdown": RECORDED_DROPDOWN_HTML,
    "cards": RECORDED_CARDS,
    "broke": BROKE_SENTENCES,
    "aboutToReach": ABOUT_TO_REACH,
    "kept": KEPT_SENTENCE,
}

# Shared by the A46 bodies: a chat line (HTML) holding text, and the card
# events of an env as "status:streamer:count:age:unit:verified:source".
TODAY = r"""
  const RECORDED = lib.recorded;
  const chatLine = (text) => '<span class="chat-author__display-name">troll</span><span>: </span>' +
    '<span class="text-fragment">' + text + "</span>";
  const full = (e) => lib.sent(e, "streak_event").map((m) => m.event).filter((v) => v.status !== "already_saved")
    .map((v) => `${v.status}:${v.streamer}:${v.count}:${v.card_age_s}:${v.card_age_unit_s}:${v.login_verified}:${v.source}`);
  const sentenceIn = (root) => root.querySelectorAll("p").filter((p) => p.children.length === 0)[0];
"""


def today_body(case_id: str, js: str) -> str:
    return dom_body(case_id, TODAY + js)


# A46: the open check on a visible channel page with today's markup. The
# recorded bell is found (no badge: unread unknown), the dropdown's balloon
# and header show at once and the cards 2.5 s later: the check waits for
# the cards, reads the four broke cards (each with its own age and login),
# gives the "about to reach" card no event and no near miss, and closes
# the dialog. Chat lines with the same sentences and a card in the toast
# container are never read on a channel page.
today_body("a46_open_check_today", r"""
  const chat = [RECORDED.broke.theamatorium, RECORDED.aboutToReach, "No Content Eligible " + RECORDED.kept]
    .map(chatLine);
  const toastHtml = '<div data-test-selector="persistent-notification"><div><p><p>' +
    "Your 9-stream streak on Toastie broke! Watch a clip, VOD or stream in the next 24h to save it.</p></p></div></div>";
  const t = lib.topEnv({ path: "/theamatorium" });
  const page = lib.todayPage(t, { cards: RECORDED.cards, renderDelayMs: 2500, toastHtml, chat });
  await t.advance(500);
  const bell = t.api.findBellButton();
  const before = {
    bell: bell === page.bell,
    label: bell && bell.getAttribute("aria-label"),
    badge: t.api.parseBellBadgeCount(bell),
    open: t.api.isBellDropdownOpen(),
    bellFound: (await t.ask({ action: "getStatus" })).bellFound,
  };
  const reply = await lib.settle(t, t.ask({ action: "checkBell", mode: "open" }));
  await t.advance(6000); // the 5 s first scan and the full scan after the burst
  const first = lib.sent(t, "streak_event")[0];
  return {
    before,
    reply: reply.value,
    at: reply.at,
    clicks: page.clicks.map((c) => c.trusted),
    open: page.isOpen(),
    dialogs: t.body.querySelectorAll('[role="dialog"]').length,
    events: full(t),
    first: first && first.event,
    unparsed: lib.sent(t, "streak_unparsed").map((m) => m.text),
    saved: lib.sent(t, "streak_event").filter((m) => m.event.status === "already_saved").length,
    result: (await t.ask({ action: "getStatus" })).lastBellCheckResult,
    errors: t.errors,
  };
""")

# A46: what counts as the dropdown, and which button is the bell.
today_body("a46_dropdown_and_bell", r"""
  const t = lib.frameEnv({ path: "/theamatorium" });
  const a = t.api;
  const open = () => a.isBellDropdownOpen();
  const out = { nothing: open() };
  const toast = h("div", { "data-test-selector": "onsite-notifications-toast-manager" }, lib.el(RECORDED.cards[1]));
  t.body.append(toast);
  out.toast = open();
  out.toastScope = a.streakCandidateSource(sentenceIn(toast));
  toast.remove();
  const modal = lib.keptModal();
  t.body.append(modal);
  out.keptModal = open();
  modal.remove();
  const dialog = lib.el(RECORDED.dropdown);
  t.body.append(dialog);
  out.balloon = open();
  const list = dialog.querySelector('[data-test-selector="center-window__content"]').children[0];
  list.append(lib.el(RECORDED.cards[1]));
  out.withCards = open();
  out.scope = a.streakCandidateSource(sentenceIn(list));
  dialog.remove();
  const bare = h("div", { role: "dialog" }, lib.el(RECORDED.cards[1]));
  t.body.append(bare);
  out.bare = open();
  out.bareScope = a.streakCandidateSource(sentenceIn(bare));
  bare.remove();
  const other = h("div", { role: "dialog" }, h("p", {}, "Share this clip"));
  t.body.append(other);
  out.other = open();
  other.remove();
  const legacy = lib.popover(lib.card("Your 3-stream streak on carol broke"));
  t.body.append(legacy);
  out.legacy = open();
  legacy.remove();
  // A channel's own notification toggle first in the page, then the bars.
  const toggle = h("button", { "aria-label": "Turn on notifications for TheAmatorium" });
  t.body.append(h("main", {}, toggle));
  out.toggleOnly = a.findBellButton() === null;
  const wrap = lib.el(RECORDED.bell);
  t.body.append(h("nav", {}, wrap));
  out.todayWins = a.findBellButton() === wrap.querySelector("button");
  out.badge = a.parseBellBadgeCount(wrap.querySelector("button"));
  const old = h("button", { "data-a-target": "onsite-notifications-toggle__button", "aria-label": "Notifications" });
  t.body.append(old);
  out.legacyFirst = a.findBellButton() === old;
  out.modalChars = lib.keptModal().querySelector(".tw-modal").textContent.length;
  return out;
""")

# A46: the already-kept modal, on any page, right after the in-place move.
today_body("a46_kept_modal", r"""
  const legacyCard = '<div class="save-streak-card"><h2>No Content Eligible</h2><p>' +
    "You've already maintained your 5-stream streak with Kept. Keep'em going by watching more live streams!</p></div>";
  const chatSection = (texts, neutral) => neutral
    ? h("aside", { class: "x-side" }, h("div", { class: "x-msgs" }, ...texts.map((x) => h("div", { class: "x-msg" },
      ...lib.parse(chatLine(x))))))
    : h("section", { class: "stream-chat" }, h("div", { "data-a-target": "chat-scroller" },
      ...texts.map((x) => h("div", { class: "chat-line__message" }, ...lib.parse(chatLine(x))))));
  const both = "No Content Eligible " + RECORDED.kept;
  const run = async ({ path, before, steps = [], until = 70000 }) => {
    const t = lib.topEnv({ path, intervals: true, before });
    for (const [ms, fn] of steps) t.later(ms, () => fn(t));
    await t.advance(until);
    const saved = [];
    lib.sent(t, "streak_event").forEach((m) => {
      if (m.event.status === "already_saved") saved.push(`${m.event.streamer}:${m.event.count}:${m.event.page_url}`);
    });
    const at = t.sent.map((m, i) => (m.type === "streak_event" && m.event.status === "already_saved" ? t.sentAt[i] : null))
      .filter((x) => x !== null);
    return { saved, at, notEligible: lib.sent(t, "not_eligible").length, errors: t.errors };
  };
  const moveTo = (path, ...nodes) => (t) => {
    t.move(path);
    for (const n of t.body.children.slice()) n.remove();
    t.body.append(h("main", {}, h("h1", {}, path)), ...nodes);
  };
  return {
    moved: await run({ path: "/save-streak/theamatorium",
      before: (body) => body.append(h("main", {}, h("div", { class: "loading" }))),
      steps: [[700, moveTo("/theamatorium", chatSection(["hello"]), lib.keptModal())]] }),
    loadedAfterMove: await run({ path: "/theamatorium", before: (body) => body.append(lib.keptModal()) }),
    stays: await run({ path: "/save-streak/theamatorium", steps: [[500, (t) => t.body.append(lib.keptModal())]] }),
    staysThenMoves: await run({ path: "/save-streak/theamatorium", steps: [
      [500, (t) => t.body.append(lib.keptModal())],
      [5000, (t) => t.move("/theamatorium")],
    ] }),
    // A channel page open past the rescan window; the owner follows the
    // broke card there and Twitch goes to /save-streak/theamatorium and back
    // inside one route poll, then opens the dialog.
    lateRoundTrip: await run({ path: "/theamatorium", until: 700000, steps: [
      [660000, moveTo("/save-streak/theamatorium")],
      [660400, moveTo("/theamatorium", lib.keptModal())],
    ] }),
    redrawn: await run({ path: "/theamatorium", before: (body) => body.append(lib.keptModal()), steps: [
      [2000, (t) => { t.body.querySelector('[role="dialog"]').parentElement.parentElement.remove();
        t.body.append(lib.keptModal()); }],
      [4000, (t) => t.move("/theamatorium/videos")],
    ] }),
    otherChannel: await run({ path: "/icozy", before: (body) => body.append(lib.keptModal()) }),
    vod: await run({ path: "/videos/2888044378", before: (body) => body.append(lib.keptModal()) }),
    clip: await run({ path: "/save-streak/icozy", steps: [[500, moveTo(
      "/icozy/clip/AntediluvianPoisedRhinocerosHumbleLife-az1h_w4_FqDUU7VC?range=7d")]] }),
    headingOnly: await run({ path: "/theamatorium",
      before: (body) => { const m = lib.keptModal(); m.querySelector(".dPxRWu").remove(); body.append(m); } }),
    chatOnChannel: await run({ path: "/theamatorium",
      before: (body) => body.append(chatSection([both]), chatSection([both], true)),
      steps: [[3000, (t) => t.body.append(chatSection([both]), chatSection([both], true))]] }),
    chatOnSavePage: await run({ path: "/save-streak/theamatorium", before: (body) => body.append(chatSection([both])),
      steps: [[4000, (t) => t.body.append(chatSection([both]))]] }),
    legacyPage: await run({ path: "/save-streak/kept", before: (body) => body.append(...lib.parse(legacyCard)) }),
  };
""")

# A46: on a hidden tab today's badge-less bell gets the slow check, at most
# once per 10 minutes counted from the page load or the last click on the
# bell (the open check's included). An explicit zero never clicks.
today_body("a46_hidden_no_badge", r"""
  const secs = (page) => page.clicks.map((c) => Math.round(c.at / 1000));
  const t = lib.topEnv({ path: "/theamatorium", visibility: "hidden", intervals: true });
  const page = lib.todayPage(t, { cards: [RECORDED.cards[1]] });
  await t.advance(570000);
  const beforeTen = secs(page);
  await t.advance(660000 - (t.now - NOW));
  t.setVisibility("visible");
  await t.advance(1000);
  t.setVisibility("hidden");
  await t.advance(1260000 - (t.now - NOW));
  const slow = { beforeTen, clicks: secs(page), events: full(t), errors: t.errors };

  const t2 = lib.topEnv({ path: "/theamatorium", visibility: "hidden", intervals: true });
  const page2 = lib.todayPage(t2, { cards: [RECORDED.cards[1]] });
  await t2.advance(12000);
  const reply = await lib.settle(t2, t2.ask({ action: "checkBell", mode: "open" }));
  await t2.advance(700000 - (t2.now - NOW));
  const afterOpenCheck = { reply: reply.value, clicks: secs(page2), errors: t2.errors };

  const zero = async (attrs, kids) => {
    const z = lib.topEnv({ path: "/theamatorium", visibility: "hidden", intervals: true });
    const bell = h("button", { "data-a-target": "onsite-notifications-toggle__button", ...attrs }, ...kids);
    let clicks = 0;
    bell.addEventListener("click", () => { clicks += 1; });
    z.body.append(h("nav", {}, bell));
    await z.advance(1300000);
    return { clicks, errors: z.errors };
  };
  return {
    slow,
    afterOpenCheck,
    zeroLabel: await zero({ "aria-label": "Notifications, 0 unread" }, []),
    zeroBadge: await zero({ "aria-label": "Notifications" }, [h("span", { class: "notification-badge" }, "0")]),
  };
""")

# Shared by the channel toggle bodies: a channel page's header with the
# Follow button and the channel's own notification toggle (the bell beside
# Follow), put first in the page; clicks on the toggle in ms after load.
TOGGLE = r"""
  const channelHeader = (e, label = "Turn on Notifications") => {
    const toggle = h("button", { id: "toggle", "aria-label": label });
    const clicks = [];
    toggle.addEventListener("click", () => clicks.push(e.now - NOW));
    e.body.prepend(h("main", {}, h("div", { class: "channel-info-content" },
      h("button", { "data-a-target": "follow-button", "aria-label": "Follow TheAmatorium" }), toggle)));
    return { toggle, clicks };
  };
"""

# A46 hardening: the generic label ("otification") counts only inside the
# top nav, so a channel's own notification toggle is never the bell; today's
# exact label and the legacy data-a-targets count anywhere. Each case is a
# fresh page with the channel header first; the answer is the id of the
# button found, or null.
today_body("a46_bell_only_in_the_top_nav", TOGGLE + r"""
  const t = lib.frameEnv({ path: "/theamatorium" });
  const which = () => { const b = t.api.findBellButton(); return b ? b.getAttribute("id") || "unnamed" : null; };
  const recordedBell = () => {
    const wrap = lib.el(RECORDED.bell);
    wrap.querySelector("button").setAttribute("id", "recorded");
    return wrap;
  };
  const generic = (label = "Notifications") => h("button", { id: "bell", "aria-label": label });
  const topNav = (...kids) => h("nav", { "data-a-target": "top-nav-container" },
    h("input", { id: "sm-input", type: "text" }), h("div", { "data-test-selector": "onsite-notifications-toast-manager" }),
    ...kids);
  const page = (build, label) => {
    for (const n of t.body.children.slice()) n.remove();
    build();
    channelHeader(t, label);
    return which();
  };
  return {
    toggleAlone: ["Turn on Notifications", "Turn Off Notifications", "Notifications",
      "Turn on notifications for TheAmatorium"].map((label) => page(() => {}, label)),
    topBarRendering: page(() => t.body.append(topNav())),
    topNavGeneric: page(() => t.body.append(topNav(generic()))),
    topNavDiv: page(() => t.body.append(h("div", { "data-a-target": "top-nav-container" }, generic()))),
    plainNav: page(() => t.body.append(h("nav", {}, generic("Open notifications menu")))),
    topNavFirst: page(() => t.body.append(h("nav", { "aria-label": "Sidebar" }, h("button", { id: "side",
      "aria-label": "Notifications" })), topNav(generic()))),
    outsideNav: page(() => t.body.append(h("div", { class: "top-bar" }, generic()))),
    recordedOutsideNav: page(() => t.body.append(h("div", {}, recordedBell()))),
    recordedTopNav: page(() => t.body.append(topNav(recordedBell()))),
    legacyAnywhere: page(() => t.body.append(h("div", {}, h("button", { id: "legacy",
      "data-a-target": "onsite-notifications-toggle__button", "aria-label": "Notifications" })))),
  };
""")

# A46 hardening: a page whose only notification button is the channel's own
# toggle has no bell, so the open check answers no-bell and the hidden-tab
# slow check never clicks; with the top bar still rendering, the open check
# waits for today's bell and clicks that, never the toggle; on a hidden tab
# with both, the slow check clicks the bell.
today_body("a46_channel_toggle_never_clicked", TOGGLE + r"""
  const secs = (page) => page.clicks.map((c) => Math.round(c.at / 1000));

  const t = lib.topEnv({ path: "/theamatorium" });
  const only = channelHeader(t);
  await t.advance(500);
  const bellFound = (await t.ask({ action: "getStatus" })).bellFound;
  const reply = await lib.settle(t, t.ask({ action: "checkBell", mode: "open" }), 50, 30000);
  const openOnly = {
    bellFound, reply: reply.value, at: reply.at, toggleClicks: only.clicks,
    missing: lib.sent(t, "bell_missing").length, errors: t.errors,
  };

  const t2 = lib.topEnv({ path: "/theamatorium" });
  const page2 = lib.todayPage(t2, { cards: [RECORDED.cards[1]] });
  const nav = page2.wrap.parentElement;
  page2.wrap.remove();
  const late = channelHeader(t2);
  t2.later(7000, () => nav.append(page2.wrap));
  const reply2 = await lib.settle(t2, t2.ask({ action: "checkBell", mode: "open" }), 50, 30000);
  await t2.advance(6000);
  const topBarLate = {
    reply: reply2.value, at: reply2.at, bellClicks: secs(page2), toggleClicks: late.clicks,
    open: page2.isOpen(), events: full(t2), errors: t2.errors,
  };

  const t3 = lib.topEnv({ path: "/theamatorium", visibility: "hidden", intervals: true });
  const hiddenOnly = channelHeader(t3);
  await t3.advance(1300000);
  const hidden = { toggleClicks: hiddenOnly.clicks, events: full(t3), errors: t3.errors };

  const t4 = lib.topEnv({ path: "/theamatorium", visibility: "hidden", intervals: true });
  const page4 = lib.todayPage(t4, { cards: [RECORDED.cards[1]] });
  const both = channelHeader(t4);
  await t4.advance(1300000);
  const hiddenWithBell = { bellClicks: secs(page4), toggleClicks: both.clicks, events: full(t4), errors: t4.errors };

  return { openOnly, topBarLate, hidden, hiddenWithBell };
""")

# A46 with B7: today's dialog opened and closed in one task is still read,
# from the removed subtree.
today_body("a46_removed_dialog", r"""
  const t = lib.topEnv({ path: "/theamatorium" });
  const page = lib.todayPage(t, { cards: RECORDED.cards });
  await t.advance(6000);
  const before = full(t).length;
  page.open();
  page.close();
  await t.advance(3000);
  return { before, events: full(t), errors: t.errors };
""")

# A46 with P4: a persistent-notification block holding two sentences is not
# one card; each sentence keeps its own age.
today_body("a46_two_sentences_one_block", r"""
  const dialog = lib.el(RECORDED.dropdown);
  const list = dialog.querySelector('[data-test-selector="center-window__content"]').children[0];
  list.append(lib.el('<div data-test-selector="persistent-notification">' +
    '<div class="c1"><p>Your 12-stream streak on Bob broke</p><span>2 hours ago</span></div>' +
    '<div class="c2"><p>Your 3-stream streak on Carol broke</p><span>5 hours ago</span></div></div>'));
  list.append(lib.el(RECORDED.cards[1]));
  env.body.append(dialog);
  return scan(dialog);
""")

# A46 with P4, P5: the persistent-notification block is the card root. An
# undated card without a link next to another notification that links
# /save-streak/bob keeps its own name; bob's link goes out on its own.
today_body("a46_block_is_the_card", r"""
  const dialog = lib.el(RECORDED.dropdown);
  const list = dialog.querySelector('[data-test-selector="center-window__content"]').children[0];
  list.append(lib.el('<div data-test-selector="persistent-notification"><div><a data-test-selector=' +
    '"persistent-notification__click"><div><div><div data-test-selector="persistent-notification__body"><p><p>' +
    "Your 9-stream streak on Alice broke! Watch a clip, VOD or stream in the next 24h to save it.</p></p></div>" +
    "</div></div></a></div></div>"));
  list.append(lib.el('<div data-test-selector="persistent-notification"><div><a data-test-selector=' +
    '"persistent-notification__click" href="https://www.twitch.tv/save-streak/bob"><div><div>' +
    '<div data-test-selector="persistent-notification__body"><p><p>Bob is live: Just Chatting</p></p></div>' +
    "</div></div></a></div></div>"));
  env.body.append(dialog);
  return scan(dialog);
""")


_DOM_RESULTS = {}


def _dom_results(browser: str, tmp_path_factory) -> dict:
    if browser not in _DOM_RESULTS:
        tmp = tmp_path_factory.mktemp(f"content_dom_{browser}")
        harness = tmp / "dom_harness.js"
        harness.write_text(DOM_HARNESS.replace("__RECORDED__", json.dumps(RECORDED)), encoding="utf-8")
        cases = tmp / "dom_bodies.json"
        cases.write_text(json.dumps(DOM_BODIES), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), str(CONTENT_SCRIPTS[browser]), str(cases)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        assert result.returncode == 0, result.stderr
        _DOM_RESULTS[browser] = json.loads(result.stdout)
    return _DOM_RESULTS[browser]


@pytest.fixture(params=sorted(CONTENT_SCRIPTS))
def dom(request, tmp_path_factory):
    browser = request.param
    results = _dom_results(browser, tmp_path_factory)

    def get(case_id):
        got = results[case_id]
        assert got["ok"], f"{browser} {case_id}: {got.get('error')}"
        return got["value"]
    return get


def _ev(streamer, count, age, unit, verified=True, source="bell"):
    return {"streamer": streamer, "count": count, "age": age, "unit": unit, "verified": verified, "source": source}


NO_EVENT = {"reply": {"cards": 1, "events": 0}, "events": [], "unparsed": []}


def test_ap04_ap03_a_card_among_other_notifications_keeps_its_own_age(dom):
    got = dom("p4_neighbors")
    for case in got.values():
        assert case.pop("length") < 1500
    # An older neighbor never lends its age: the 2-day card is dropped.
    assert got["older"] == NO_EVENT
    assert got["fiveOlder"] == NO_EVENT
    assert got["tenOlder"] == NO_EVENT
    # A younger neighbor never makes the card look newer.
    assert got["younger"]["events"] == [_ev("amy", 5, 1200, 60)]
    assert got["fiveYounger"]["events"] == [_ev("ava", 5, 1200, 60)]
    assert got["tenYounger"]["events"] == [_ev("ada", 5, 1320, 60)]


def test_ap04_ap06_a_new_notification_on_top_neither_revives_nor_resends_a_card(dom):
    got = dom("p4_new_top_item")
    old, fresh = got["old"], got["fresh"]
    assert old["length"] < 1500 and fresh["length"] < 1500
    assert old["first"] == NO_EVENT and old["second"] == NO_EVENT
    assert fresh["first"]["events"] == [_ev("cora", 5, 10800, 3600)]
    assert fresh["second"] == NO_EVENT


def test_ap04_ap02_an_undated_card_among_dated_notifications_stays_undated(dom):
    got = dom("p4_undated")
    assert got["reply"] == {"cards": 1, "events": 1}
    assert got["events"] == [_ev("carol", 9, None, None)]


def test_ap04_ap05_f31_a_neighbor_never_lends_its_save_streak_link(dom):
    for case_id in ("p4_near_miss_link_own", "p4_near_miss_link_none"):
        got = dom(case_id)
        assert got["reply"] == {"cards": 1, "events": 2}, case_id
        assert got["events"] == [_ev("alice", 5, 1200, 60), _ev("bob", None, None, None, source="link")], case_id
        assert got["unparsed"] == ["Your watch streak with bob has ended"], case_id


def test_ap04_ap02_a_link_around_the_sentence_keeps_the_card_whole(dom):
    got = dom("p4_link_wraps_sentence")
    assert got["reply"] == {"cards": 1, "events": 1}
    assert got["events"] == [_ev("dana", 4, 14400, 3600)]


def test_ap04_ap05_two_cards_in_one_container_keep_their_own_ages(dom):
    got = dom("p4_pair")
    assert got["reply"] == {"cards": 2, "events": 2}
    assert sorted(got["events"], key=lambda e: e["streamer"]) == [
        _ev("bob", 12, 7200, 3600), _ev("carol", 3, 18000, 3600)]


def test_ab5_the_render_wait_takes_its_first_look_one_poll_after_the_click(dom):
    got = dom("b5_first_look")
    assert got["syncLookups"] == 0
    assert got["res"] == {"value": True, "at": 1000}


def test_ab5_a_dropdown_that_mounts_after_the_click_returns_is_still_read(dom):
    microtask, delayed, slow_list = dom("b5_late_mounts")
    assert microtask == {"value": True, "at": 1000}
    assert delayed == {"value": True, "at": 1250}
    assert slow_list == {"value": True, "at": 3750}


def test_ab5_nothing_rendered_gives_up_at_the_cap_and_a_closed_dropdown_at_once(dom):
    got = dom("b5_gives_up")
    assert got["never"] == {"value": False, "at": 8000}
    assert got["empty"] == {"value": False, "at": 8000}
    assert got["closed"] == {"value": False, "at": 750}


def test_ab5_at_the_cap_a_dropdown_holding_text_counts_as_rendered(dom):
    got = dom("b5_text_at_the_cap")
    # The list rendered at 7.4 s, only 600 ms before the cap.
    assert got["late"] == {"value": True, "at": 8000}
    # Text that never holds still for 750 ms.
    assert got["busy"] == {"value": True, "at": 8000}


def test_ap02_ap03_a_name_that_looks_like_an_age_never_gives_the_card_its_age(dom):
    got = dom("p2_names_that_look_like_ages")
    for kind in ("broke", "danger", "strongBroke", "strongDanger"):
        for name, (fresh, stale) in got[kind].items():
            # The card's own label gives the age; the gate drops it by that label.
            assert fresh == [[name, 7200, 3600]], (kind, name, fresh)
            assert stale == [], (kind, name, stale)
    assert set(got["strongBroke"]) == {"yesterday", "justnow", "5hago"}
    assert got["links"] == {
        "slugs": ["yesterdayjam", "justnowgaming"],
        "links": [{"login": "yesterdayjam", "card_age_s": 7200}, {"login": "justnowgaming", "card_age_s": 259200}],
    }


def test_ap06_c05_a_partial_view_never_sends_a_card_older_than_one_already_held(dom):
    assert dom("p6_partial_views") == [
        ["bob:12:600"],    # the broke card
        [],                # the in-danger card behind it
        [],                # a lower count, older
        ["carol:7:10800"],  # another streamer
        ["bob:12:null"],   # an unknown age is never held back
        ["bob:13:60"],     # a newer card
        [],
        [],
    ]


def test_ap06_ab7_cards_inserted_one_by_one_give_one_event_per_streamer(dom):
    got = dom("p6_cards_one_by_one")
    bell = ["broke:bob:12:600:bell", "broke:carol:7:3600:bell"]
    for case in ("brokeFirst", "dangerFirst", "laterBatch", "removedAtOnce", "removedApart", "removedDangerFirst"):
        assert got[case] == {"events": bell, "errors": []}, case
    assert got["pair"] == {"events": ["broke:bob:13:600:bell"], "errors": []}
    assert got["pairReversed"] == {"events": ["broke:bob:13:600:bell"], "errors": []}
    assert got["listedPage"] == {"events": ["broke:bob:12:600:page", "broke:carol:7:3600:page"], "errors": []}


NOT_RENDERED = {"ok": False, "opened": True, "unreadBefore": 2, "rendered": False, "cards": 0, "events": 0,
                "reason": "not-rendered"}


def test_ab2_a_dropdown_you_open_after_a_lost_click_stays_open(dom):
    got = dom("b2_late_mount_ownership")
    yours = got["youOpened"]
    assert yours["first"] == NOT_RENDERED
    assert yours["openAfterYou"] is True
    # The retry at +68 s reads it and neither clicks nor closes it.
    assert yours["clicks"] == [[0, False], [28, True]]
    assert yours["openAtEnd"] is True
    assert yours["result"] == "read-open"
    assert yours["events"] == ["broke:gina:7:600:bell"]
    assert yours["errors"] == []


def test_ab2_ab5_a_late_mount_without_your_input_is_closed_by_the_retry(dom):
    late = dom("b2_late_mount_ownership")["lateMount"]
    assert late["first"] == NOT_RENDERED
    assert late["clicks"] == [[0, False], [68, False]]
    assert late["openAtEnd"] is False
    assert late["result"] == "read-open"
    assert late["events"] == ["broke:gina:7:600:bell"]
    assert late["errors"] == []


def test_ab4_ab2_the_hidden_retry_leaves_a_dropdown_you_opened(dom):
    got = dom("b4_hidden_retry_leaves_your_dropdown")
    assert got["openAfterYou"] is True
    assert got["clicks"] == [[10, False], [30, True]]
    assert got["openAtEnd"] is True
    assert got["events"] == ["broke:gina:7:600:bell"]
    assert got["errors"] == []


def test_ab2_focus_goes_back_only_when_the_click_moved_it(dom):
    got = dom("b2_focus_after_the_check")
    read = {"ok": True, "opened": True, "unreadBefore": 1, "rendered": True, "cards": 1, "events": 1}
    # You clicked into the chat box during the render wait: it keeps focus.
    assert got["yours"] == {"reply": read, "active": "sm-input", "open": False}
    # The page moved focus on the bell click: it goes back to the button.
    assert got["pages"] == {"reply": read, "active": "sm-other", "open": False}
    assert got["errors"] == []


def test_ab6_ab4_a_badge_that_rises_again_after_a_read_is_read_at_once(dom):
    got = dom("b6_badge_rise_after_a_read")
    assert got["firstClicks"] == 2
    assert got["clicks"] == [11, 12, 75, 76]
    assert got["errors"] == []


# ---------------------------------------------------------------------------
# A46: today's Twitch markup (the X09 record of 2026-10-01).
# ---------------------------------------------------------------------------

TODAY_EVENTS = [
    "broke:theamatorium:31:3600:3600:true:bell",
    "broke:icozy:2:10800:3600:true:bell",
    "broke:bansheeboovt:5:18000:3600:true:bell",
    "broke:innocentofsin:77:72000:3600:true:bell",
]


def test_a46_the_recorded_markup_is_loaded_verbatim():
    # The bell and the modal box as recorded; the box text is the recorded
    # 140 characters (checked in the DOM by test_a46_dropdown_*).
    assert RECORDED_BELL_HTML.count("<button") == 1 and 'aria-label="Open Notifications"' in RECORDED_BELL_HTML
    assert "badge" not in RECORDED_BELL_HTML and "data-a-target" not in RECORDED_BELL_HTML
    assert RECORDED_MODAL_BOX_HTML.startswith('<div class="Layout-sc-1xcs6mc-0 ScModalWrapper-sc-1k48se-0 RBThw')
    assert len("No Content Eligible" + KEPT_SENTENCE + "Learn More") == 140
    for text in [RECORDED_BELL_HTML, RECORDED_MODAL_HTML, RECORDED_DROPDOWN_HTML, *RECORDED_CARDS]:
        assert text.isascii()


def test_a46_b2_the_open_check_reads_todays_dropdown_and_closes_it(dom):
    got = dom("a46_open_check_today")
    assert got["before"] == {"bell": True, "label": "Open Notifications", "badge": None, "open": False,
                             "bellFound": True}
    # Today's bell shows no count, so unread is unknown.
    assert got["reply"] == {"ok": True, "opened": True, "unreadBefore": None, "rendered": True, "cards": 4,
                            "events": 4}
    # The header showed at once; the check waited for the cards (2.5 s),
    # then 750 ms of unchanged text.
    assert 3250 <= got["at"] < 4000, got["at"]
    assert got["clicks"] == [False, False]
    assert got["open"] is False and got["dialogs"] == 0
    assert got["events"] == TODAY_EVENTS
    first = got["first"]
    assert first["save_url"] == "https://www.twitch.tv/save-streak/theamatorium"
    assert first["page_url"] == "https://www.twitch.tv/theamatorium"
    assert first["deadline_hours"] == 24
    # The "about to reach" card is neither an event nor a near miss; chat
    # and the toast container are never read.
    assert got["unparsed"] == []
    assert got["saved"] == 0
    assert got["result"] == "opened"
    assert got["errors"] == []


def test_a46_f31_the_dropdown_is_todays_dialog_and_the_bell_its_button(dom):
    assert dom("a46_dropdown_and_bell") == {
        "nothing": False,
        "toast": False,        # the toast container is not the dropdown
        "toastScope": None,    # and a card in it is not read on a channel page
        "keptModal": False,    # nor is the already-kept modal
        "balloon": True,       # open from the moment the balloon shows
        "withCards": True,
        "scope": "bell",
        "bare": True,          # cards in an unlabeled dialog, balloon or not
        "bareScope": "bell",
        "other": False,        # another unlabeled dialog
        "legacy": True,
        "toggleOnly": True,    # a channel's own toggle alone is no bell
        "todayWins": True,     # the exact label wins over a channel toggle
        "badge": None,         # today's bell: no badge, no count
        "legacyFirst": True,   # a legacy bell comes first when both exist
        "modalChars": 140,     # the recorded modal, verbatim
    }


def test_a46_already_saved_from_the_kept_modal_on_any_page_right_after_the_move(dom):
    got = dom("a46_kept_modal")
    for case, value in got.items():
        assert value["errors"] == [], case
        assert value["notEligible"] == 0, case
    kept = "theamatorium:31:https://www.twitch.tv/"
    # The in-place move from /save-streak/theamatorium to the channel at
    # 0.7 s: reported on the channel page by the next route check.
    assert got["moved"]["saved"] == [kept + "theamatorium"]
    assert 700 < got["moved"]["at"][0] <= 1700
    # Loaded after the move, with the modal up: at once.
    assert got["loadedAfterMove"]["saved"] == [kept + "theamatorium"]
    assert got["loadedAfterMove"]["at"] == [0]
    # Over a save-streak page that stays.
    assert got["stays"]["saved"] == [kept + "save-streak/theamatorium"]
    # One report per page for one kept streak: through a move, a redraw and
    # another route of the same channel.
    assert got["staysThenMoves"]["saved"] == [kept + "save-streak/theamatorium"]
    assert got["redrawn"]["saved"] == [kept + "theamatorium"]
    # A round trip through /save-streak/theamatorium inside one route poll,
    # 11 minutes after the channel page loaded: the observer, which outlives
    # the 10-minute rescan window, still reports it.
    assert got["lateRoundTrip"]["saved"] == [kept + "theamatorium"]
    assert 660400 < got["lateRoundTrip"]["at"][0] <= 661400
    # The name must fit the page's channel; a page without one takes the
    # name when it is a login.
    assert got["otherChannel"]["saved"] == []
    assert got["vod"]["saved"] == [kept + "videos/2888044378"]
    # A move to a clip, a heading without the sentence, and chat lines with
    # both texts (chat scroller or neutral) give nothing.
    assert got["clip"]["saved"] == []
    assert got["headingOnly"]["saved"] == []
    assert got["chatOnChannel"]["saved"] == []
    assert got["chatOnSavePage"]["saved"] == []
    # The old on-page card still reports, at the first scan.
    assert got["legacyPage"]["saved"] == ["kept:5:https://www.twitch.tv/save-streak/kept"]
    assert got["legacyPage"]["at"] == [3000]


def test_a46_ab4_a_hidden_tab_checks_a_badgeless_bell_every_ten_minutes(dom):
    got = dom("a46_hidden_no_badge")
    slow = got["slow"]
    assert slow["beforeTen"] == []
    # Open and close at 10 min, nothing when the tab is hidden again at
    # 11 min, open and close at 20 min.
    assert slow["clicks"] == [600, 601, 1200, 1201]
    assert slow["events"] == ["broke:theamatorium:31:3600:3600:true:bell"]
    assert slow["errors"] == []
    # The open check's click at 12 s starts the 10 minutes over.
    after = got["afterOpenCheck"]
    assert after["reply"]["opened"] is True and after["reply"]["unreadBefore"] is None
    assert after["clicks"] == [12, 13, 660, 661]
    assert after["errors"] == []
    # An explicit zero never clicks, in the label or in a badge.
    assert got["zeroLabel"] == {"clicks": 0, "errors": []}
    assert got["zeroBadge"] == {"clicks": 0, "errors": []}


def test_a46_b2_the_generic_bell_label_counts_only_inside_the_top_nav(dom):
    assert dom("a46_bell_only_in_the_top_nav") == {
        # The channel's own toggle, in each of its labels, is never the bell.
        "toggleAlone": [None, None, None, None],
        # Nor while the top bar is still rendering without its bell.
        "topBarRendering": None,
        # A generic label in the top nav: its data-a-target (on a nav or
        # not), then any nav, the top nav first.
        "topNavGeneric": "bell",
        "topNavDiv": "bell",
        "plainNav": "bell",
        "topNavFirst": "bell",
        # A generic label outside any nav is not the bell.
        "outsideNav": None,
        # The recorded bell and the legacy data-a-target count anywhere.
        "recordedOutsideNav": "recorded",
        "recordedTopNav": "recorded",
        "legacyAnywhere": "legacy",
    }


def test_a46_b2_ab4_a_channels_own_notification_toggle_is_never_clicked(dom):
    got = dom("a46_channel_toggle_never_clicked")
    # The toggle alone: no bell, so the open check looks four more times,
    # 5 s apart, answers no-bell and reports the bell missing once.
    assert got["openOnly"] == {
        "bellFound": False,
        "reply": {"ok": False, "opened": False, "unreadBefore": None, "rendered": False, "cards": 0, "events": 0,
                  "reason": "no-bell"},
        "at": 20000, "toggleClicks": [], "missing": 1, "errors": [],
    }
    # The top bar renders today's bell 7 s in: the look at 10 s finds it and
    # the check opens and closes it; the toggle is never clicked.
    assert got["topBarLate"] == {
        "reply": {"ok": True, "opened": True, "unreadBefore": None, "rendered": True, "cards": 1, "events": 1},
        "at": 11000, "bellClicks": [10, 11], "toggleClicks": [], "open": False,
        "events": ["broke:theamatorium:31:3600:3600:true:bell"], "errors": [],
    }
    # A hidden tab for 21 minutes: the slow no-badge check never clicks the
    # toggle; with today's bell in the top bar it clicks the bell instead.
    assert got["hidden"] == {"toggleClicks": [], "events": [], "errors": []}
    assert got["hiddenWithBell"] == {
        "bellClicks": [600, 601, 1200, 1201], "toggleClicks": [],
        "events": ["broke:theamatorium:31:3600:3600:true:bell"], "errors": [],
    }


def test_a46_ab7_todays_dialog_closed_in_the_same_task_is_still_read(dom):
    got = dom("a46_removed_dialog")
    assert got["before"] == 0
    assert got["events"] == TODAY_EVENTS
    assert got["errors"] == []


def test_a46_ap04_a_block_with_two_sentences_is_read_card_by_card(dom):
    got = dom("a46_two_sentences_one_block")
    assert got["reply"] == {"cards": 3, "events": 3}
    assert sorted(got["events"], key=lambda e: e["streamer"]) == [
        _ev("bob", 12, 7200, 3600), _ev("carol", 3, 18000, 3600), _ev("theamatorium", 31, 3600, 3600)]
    assert got["unparsed"] == []


def test_a46_ap04_ap05_the_card_block_keeps_a_neighbors_link_out(dom):
    got = dom("a46_block_is_the_card")
    assert got["reply"] == {"cards": 1, "events": 2}
    assert got["events"] == [_ev("alice", 9, None, None), _ev("bob", None, None, None, source="link")]
    assert got["unparsed"] == []
