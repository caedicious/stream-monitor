"""The streak sentences, read the way Twitch writes them (streak audit P9, P12).

Both content scripts must carry the same three sentence patterns, and the
patterns use only syntax that JavaScript and Python's re share, so this
module reads them out of the files and runs a fixture list through Python.
The same fixtures then go through each file's own parseStreakText (Node),
so the Python reading cannot drift from what the extension does.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTENT_SCRIPTS = {
    "chrome": ROOT / "chrome_extension" / "content.js",
    "firefox": ROOT / "firefox_extension" / "content.js",
}
PATTERNS = ("STREAK_BROKE_RE", "STREAK_IN_DANGER_RE", "ALREADY_SAVED_RE")
NODE = shutil.which("node")


def _literal(text: str, name: str) -> tuple:
    m = re.search(r"^\s*const %s =\s*/(.+)/([a-z]*);\s*$" % name, text, re.M)
    assert m, f"{name} not found"
    return m.group(1), m.group(2)


def _patterns(browser: str) -> dict:
    text = CONTENT_SCRIPTS[browser].read_text(encoding="utf-8")
    return {name: _literal(text, name) for name in PATTERNS}


def _compiled() -> dict:
    out = {}
    for name, (source, flags) in _patterns("chrome").items():
        assert set(flags) <= {"i"}, f"{name} uses flags {flags!r}"
        out[name] = re.compile(source, re.I if "i" in flags else 0)
    return out


def _count(text: str) -> int:
    return int(text.replace(",", ""))


def parse_card(text: str):
    """The Python reading of parseStreakText."""
    rx = _compiled()
    m = rx["STREAK_BROKE_RE"].search(text)
    if m:
        return {"status": "broke", "streamer": m.group(2).lower(), "count": _count(m.group(1)),
                "deadline_hours": 24}
    m = rx["STREAK_IN_DANGER_RE"].search(text)
    if m:
        n = int(m.group(3))
        unit = m.group(4).lower()
        hours = n
        if unit.startswith("d"):
            hours = n * 24
        elif unit.startswith("m"):
            # JavaScript's Math.round: halves go up.
            hours = max(1, int(n / 60 + 0.5))
        return {"status": "in_danger", "streamer": m.group(2).lower(), "count": _count(m.group(1)),
                "deadline_hours": hours}
    return None


def parse_saved(text: str):
    m = _compiled()["ALREADY_SAVED_RE"].search(text)
    return {"count": _count(m.group(1)), "streamer": m.group(2).lower()} if m else None


OWNER_TEXT = (
    "No Content Eligible You've already maintained your 4-stream streak with "
    "DriveYaBatty. Keep'em going by watching more live streams!"
)
# Recorded in the owner's logged-in Firefox on 2026-10-01 (plan A46): a
# broke card, a card that is no streak event, and the already-kept modal's
# text as textContent runs it together (140 characters).
RECORDED_BROKE = "Your 31-stream streak on TheAmatorium broke! Watch a clip, VOD or stream in the next 24h to save it."
RECORDED_ABOUT_TO_REACH = ("You're about to reach a 7-stream watch streak! Join BansheeBooVT's stream now to earn "
                           "your streak rewards!")
RECORDED_MODAL_TEXT = ("No Content EligibleYou've already maintained your 31-stream streak with TheAmatorium. "
                       "Keep'em going by watching more live streams!Learn More")


def broke(login, count):
    return {"status": "broke", "streamer": login, "count": count, "deadline_hours": 24}


def danger(login, count, hours):
    return {"status": "in_danger", "streamer": login, "count": count, "deadline_hours": hours}


CARD_FIXTURES = [
    ("Your 4-stream streak on DriveYaBatty broke", broke("driveyabatty", 4)),
    ("Your 1,024-stream streak on Fox broke.", broke("fox", 1024)),
    ("Your 12-live streak on alice broke!", broke("alice", 12)),
    ("Your 7 stream streak on bob broke", broke("bob", 7)),
    ("Your 5-stream streak with alice broke", broke("alice", 5)),
    ("Your 5-stream streak on alice ends in 3 hours", danger("alice", 5, 3)),
    ("Your 5-stream streak on alice ends in 3h.", danger("alice", 5, 3)),
    ("Your 5-stream streak on alice expires in 2 days", danger("alice", 5, 48)),
    ("Your 5-stream streak on alice ends in 45 minutes", danger("alice", 5, 1)),
    ("Your 5-stream streak on alice ends in 90 mins", danger("alice", 5, 2)),
    ("Your 5-stream streak on alice will end in 4 hrs", danger("alice", 5, 4)),
    ("Your 5-stream streak on alice ends in the next 6 hours", danger("alice", 5, 6)),
    ("Your 5-stream streak on alice expires within the next 2 days", danger("alice", 5, 48)),
    ("Your 5-stream streak with alice ends within the next 3h", danger("alice", 5, 3)),
    ("Your 5-stream streak on alice in the next 10 hours", danger("alice", 5, 10)),
    ("Your 5-stream streak on alice within the next 1 day", danger("alice", 5, 24)),
    ("Your 2,500-stream streak on Fox ends in 3 hours", danger("fox", 2500, 3)),
    # Recorded on live Twitch, 2026-10-01 (plan A46), and the same wording.
    (RECORDED_BROKE, broke("theamatorium", 31)),
    ("Your 77-stream streak on InnocentOfSin broke! Watch a clip, VOD or stream in the next 24h to save it.",
     broke("innocentofsin", 77)),
    (RECORDED_ABOUT_TO_REACH, None),
    # Unmatched.
    ("Your 5-stream streak on alice ends in 3 months", None),
    ("Your 5-stream streak on alice ends in 3 mo", None),
    ("Your 5-stream streak on alice ends in 3 minutesago", None),
    ("Your watch streak with alice has ended", None),
    ("Your 1234,567-stream streak on Fox broke", None),
    ("Your streak on alice broke", None),
    ("Your 5-stream streak on alice ends soon", None),
    ("Your 5-stream streak on alice is safe", None),
    (OWNER_TEXT, None),
]

SAVED_FIXTURES = [
    (OWNER_TEXT, {"count": 4, "streamer": "driveyabatty"}),
    ("You've already maintained your 1,024-stream streak with Fox.", {"count": 1024, "streamer": "fox"}),
    ("You've already maintained your 5-stream streak on alice", None),
    ("Your 5-stream streak on alice broke", None),
    (RECORDED_MODAL_TEXT, {"count": 31, "streamer": "theamatorium"}),
    (RECORDED_BROKE, None),
]

AGE_FIXTURES = [
    ("just now", {"seconds": 0, "unit": 60}),
    ("20 hours ago", {"seconds": 72000, "unit": 3600}),
    ("1 day ago", {"seconds": 86400, "unit": 86400}),
    ("2 months ago", {"seconds": 5184000, "unit": 2592000}),
    ("an hour ago", {"seconds": 3600, "unit": 3600}),
    ("yesterday", {"seconds": 86400, "unit": 86400}),
]


@pytest.mark.parametrize("name", PATTERNS)
def test_ap12_the_sentence_patterns_are_identical_in_both_files(name):
    assert _patterns("chrome")[name] == _patterns("firefox")[name]


@pytest.mark.parametrize("text,expected", CARD_FIXTURES)
def test_ap09_ap12_card_sentences_through_python_re(text, expected):
    assert parse_card(text) == expected


@pytest.mark.parametrize("text,expected", SAVED_FIXTURES)
def test_ap12_the_maintained_sentence_through_python_re(text, expected):
    assert parse_saved(text) == expected


def test_ap09_a_month_unit_is_never_read_as_minutes():
    m = _compiled()["STREAK_IN_DANGER_RE"].search("Your 5-stream streak on alice ends in 3 months")
    assert m is None


def test_a46_the_recorded_texts_have_the_recorded_lengths_and_no_near_miss():
    assert len(RECORDED_MODAL_TEXT) == 140
    # The "about to reach" card is neither a card nor a near miss (P8), so
    # it sends nothing at all.
    for browser in CONTENT_SCRIPTS:
        source, flags = _literal(CONTENT_SCRIPTS[browser].read_text(encoding="utf-8"), "STREAK_NEAR_MISS_RE")
        near = re.compile(source, re.I if "i" in flags else 0)
        assert near.search(RECORDED_ABOUT_TO_REACH) is None, browser
        assert near.search(RECORDED_BROKE) is not None, browser  # parsed first, so never a near miss


HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [file, casesFile] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const noop = () => {};
const location = { pathname: "/directory", search: "", href: "https://www.twitch.tv/directory" };
const win = { location };
win.top = win;
const ext = {
  runtime: { onMessage: { addListener: noop }, sendMessage: () => Promise.resolve() },
  storage: { local: { get: (k, cb) => { if (cb) cb({}); return Promise.resolve({}); } }, onChanged: { addListener: noop } },
};
const sandbox = {
  window: win, location,
  document: { body: { querySelectorAll: () => [] }, querySelector: () => null, querySelectorAll: () => [],
    addEventListener: noop, visibilityState: "visible" },
  MutationObserver: class { observe() {} disconnect() {} }, MouseEvent: class {},
  console: { log: noop, warn: noop, error: noop },
  setTimeout: () => 0, setInterval: () => 0, clearTimeout: noop, clearInterval: noop,
  module: { exports: {} }, chrome: ext, browser: ext,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: file });
const api = sandbox.module.exports;
process.stdout.write(JSON.stringify(cases.map(([fn, text]) => api[fn](text))));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("browser", sorted(CONTENT_SCRIPTS))
def test_ap12_each_file_reads_the_fixtures_as_python_does(browser, tmp_path):
    cases = ([["parseStreakText", t, e] for t, e in CARD_FIXTURES]
             + [["parseAlreadySavedText", t, e] for t, e in SAVED_FIXTURES]
             + [["parseTimeAgo", t, e] for t, e in AGE_FIXTURES])
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps([[fn, text] for fn, text, _ in cases]), encoding="utf-8")
    result = subprocess.run([NODE, str(harness), str(CONTENT_SCRIPTS[browser]), str(data)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    for (fn, text, expected), value in zip(cases, got):
        assert value == expected, f"{browser} {fn}({text!r}) returned {value!r}"
