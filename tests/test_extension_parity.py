"""Guard against the Chrome and Firefox backgrounds drifting apart.

The two files are maintained in lockstep by scripted mirroring. v1.7.2
shipped with the rescue functions mirrored into the Firefox background
but not the five RESCUE_* constants they use, which is invisible to a
syntax check (undefined globals only throw at call time) and broke the
rescue ack in production. These tests fail on exactly that class of bug.
"""
import re
from pathlib import Path

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
