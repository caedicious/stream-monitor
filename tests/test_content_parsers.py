"""The Twitch text parsers in both content scripts.

Streak counts can carry a thousands separator ("1,024-stream"), and the
save-streak check has to read the owner's real "No Content Eligible" card.
Each content.js exports parseStreakText and parseAlreadySavedText when a
CommonJS `module` exists. These tests load the file in a Node vm with the
few browser globals it touches at load time stubbed out; its timers are
no-ops, so the process exits as soon as it has printed the results.
Skipped when Node is not installed.
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

# argv: <content.js> <cases.json>. Prints [api[fn](text) for fn, text in cases].
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
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: file });
const api = sandbox.module.exports;
process.stdout.write(JSON.stringify(cases.map(([fn, text]) => api[fn](text))));
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
