"""House style for the 1.12.0 release (change control section 7; build plan
WP6 task 4).

Nothing written for 1.12.0 may hold an en-dash, an em-dash, a curly quote or
an emoji: the owner reads them as machine-written text. Older lines keep
theirs; that is history, not license. So:
- files new in 1.12.0 are scanned in full;
- files changed in 1.12.0 are scanned on the lines added since v1.11.2, the
  latest shipped release, read from `git diff`. That part is skipped when
  git or the tag is not available (CI checks out one commit and no tags);
- the four popup files hold no en-dash or em-dash at all (plan A18).

The lists are fixed to the 1.12.0 release. A test that has to mention one of
these characters writes it as an escape, as this file does.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE_TAG = "v1.11.2"

# Change control 7.3: U+2013, U+2014, the four curly quotes, and emoji.
FORBIDDEN = re.compile(r"[\u2013\u2014\u2018\u2019\u201c\u201d\U0001F300-\U0001FAFF\u2600-\u27BF]")

NEW_FILES = (
    "slot_scheduler.py",
    "streak_saves.py",
    "tests/test_background_io.py",
    "tests/test_content_parity.py",
    "tests/test_cross_contracts.py",
    "tests/test_house_style.py",
    "tests/test_popup_parity.py",
    "tests/test_save_items_logic.py",
    "tests/test_settings_slot_mode.py",
    "tests/test_slot_mode_http.py",
    "tests/test_slot_mode_monitor.py",
    "tests/test_slot_scheduler.py",
    "tests/test_streak_items.py",
    "tests/test_streak_regex_fixtures.py",
)

CHANGED_FILES = (
    "PRIVACY.md",
    "README.md",
    "SHA256SUMS.txt",
    "about.html",
    "chrome_extension/background.js",
    "chrome_extension/content.js",
    "chrome_extension/manifest.json",
    "chrome_extension/popup.html",
    "chrome_extension/popup.js",
    "firefox_extension/background.js",
    "firefox_extension/content.js",
    "firefox_extension/manifest.json",
    "firefox_extension/popup.html",
    "firefox_extension/popup.js",
    "installer.iss",
    "settings_editor.py",
    "setup_wizard.py",
    "stream_monitor_tray.py",
    "tests/conftest.py",
    "tests/test_config.py",
    "tests/test_content_parsers.py",
    "tests/test_extension_parity.py",
    "tests/test_host_header.py",
    "tests/test_post_origin.py",
    "tests/test_setup_wizard.py",
    "tests/test_state_changes.py",
    "tests/test_streak_saved.py",
)

POPUP_FILES = (
    "chrome_extension/popup.html",
    "chrome_extension/popup.js",
    "firefox_extension/popup.html",
    "firefox_extension/popup.js",
)


def _hits(lines) -> list:
    """[(line number, character code, line)] for each line with a forbidden
    character."""
    out = []
    for number, line in lines:
        m = FORBIDDEN.search(line)
        if m:
            out.append((number, f"U+{ord(m.group()):04X}", line.strip()[:120]))
    return out


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=60)


def _base_available() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return _git("rev-parse", "--verify", "--quiet", BASE_TAG + "^{commit}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _added_lines(path: str) -> list:
    """[(line number in the file today, text)] for every line that is new or
    changed since BASE_TAG, working tree included."""
    result = _git("diff", "--no-color", "--no-ext-diff", "--text", "--unified=0", BASE_TAG, "--", path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out, number, in_hunk = [], 0, False
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if header:
            number, in_hunk = int(header.group(1)), True
            continue
        if not in_hunk:
            continue  # the file header (diff, index, ---, +++)
        if line.startswith("+"):
            out.append((number, line[1:]))
            number += 1
    return out


def test_f28_the_style_pattern_catches_every_forbidden_character():
    for ch in ("\u2013", "\u2014", "\u2018", "\u2019", "\u201C", "\u201D",
               "\U0001F300", "\U0001F600", "\U0001FAFF", "\u2600", "\u2705", "\u2713", "\u27BF"):
        assert FORBIDDEN.search("a " + ch + " b"), f"U+{ord(ch):04X} is not caught"
    for text in ("plain ASCII - a hyphen, 'straight' and \"double\" quotes", "caf\u00E9", "\u2192", "\u00D7"):
        assert not FORBIDDEN.search(text), text


def test_f28_the_file_lists_name_real_files():
    for path in NEW_FILES + CHANGED_FILES + POPUP_FILES:
        assert (ROOT / path).is_file(), f"{path} does not exist"
    assert not set(NEW_FILES) & set(CHANGED_FILES)
    if not _base_available():
        pytest.skip(f"git or the {BASE_TAG} tag is not available")
    listed = _git("ls-tree", "-r", "--name-only", BASE_TAG)
    assert listed.returncode == 0
    at_base = set(listed.stdout.decode("utf-8", "replace").splitlines())
    assert not [p for p in NEW_FILES if p in at_base], "a file listed as new exists in " + BASE_TAG
    assert not [p for p in CHANGED_FILES if p not in at_base], "a file listed as changed is new since " + BASE_TAG


def test_f28_files_new_in_1_12_0_hold_no_dash_curly_quote_or_emoji():
    problems = []
    for path in NEW_FILES:
        text = (ROOT / path).read_text(encoding="utf-8")
        for number, code, line in _hits(enumerate(text.splitlines(), 1)):
            problems.append(f"{path}:{number}: {code}: {line}")
    assert not problems, "\n".join(problems)


def test_f28_lines_added_since_v1_11_2_hold_no_dash_curly_quote_or_emoji():
    if not _base_available():
        pytest.skip(f"git or the {BASE_TAG} tag is not available")
    problems, added = [], 0
    for path in CHANGED_FILES:
        lines = _added_lines(path)
        added += len(lines)
        for number, code, line in _hits(lines):
            problems.append(f"{path}:{number}: {code}: {line}")
    assert added > 0, "no line differs from " + BASE_TAG
    assert not problems, "\n".join(problems)


def test_f28_am18_the_popup_files_hold_no_en_or_em_dash():
    problems = []
    for path in POPUP_FILES:
        text = (ROOT / path).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if "\u2013" in line or "\u2014" in line:
                problems.append(f"{path}:{number}: {line.strip()[:120]}")
    assert not problems, "\n".join(problems)
