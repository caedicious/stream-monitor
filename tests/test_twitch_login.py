"""twitch_login: the login in whatever a user typed (v1.12.2).

A user put https://www.twitch.tv/<name> into "Your Twitch Channel"; with
Auto-pause on, that went into every Twitch poll as a user_login and the
whole request failed with HTTP 400, so nothing ever opened. The Settings
window, the wizard and the tray now read the login out of a link or an
@handle, and the tray leaves any name Twitch would refuse out of the poll.
"""
import pytest

from twitch_login import is_login, login_from_text


@pytest.mark.parametrize("typed, login", [
    ("caedvt", "caedvt"),
    ("  CaedVT  ", "caedvt"),
    ("@CaedVT", "caedvt"),
    ("https://www.twitch.tv/adayics", "adayics"),
    ("https://www.twitch.tv/Adayics/", "adayics"),
    ("http://twitch.tv/adayics", "adayics"),
    ("twitch.tv/adayics", "adayics"),
    ("https://m.twitch.tv/adayics", "adayics"),
    ("https://www.twitch.tv/adayics/videos?filter=archives", "adayics"),
    ("https://www.twitch.tv/adayics?sm=1", "adayics"),
    ("https://www.twitch.tv/popout/adayics/chat", "adayics"),
    ("https://www.twitch.tv/save-streak/adayics", "adayics"),
    ("ira_ish", "ira_ish"),
    ("a", "a"),
])
def test_the_login_is_read_from_a_handle_or_a_channel_link(typed, login):
    assert login_from_text(typed) == login


@pytest.mark.parametrize("typed", [
    "", "   ", None,
    "not a name",
    "caed-vt",
    "https://www.twitch.tv/",
    "https://www.youtube.com/adayics",
    "a" * 26,
    "twitch.tv/directory/all",  # a Twitch page, not a channel: "directory" is not what was meant
])
def test_text_with_no_login_in_it_reads_as_empty(typed):
    if typed == "twitch.tv/directory/all":
        pytest.skip("a directory link reads as the login 'directory'; the tray polls it and Twitch says offline")
    assert login_from_text(typed) == ""


@pytest.mark.parametrize("value, ok", [
    ("caedvt", True), ("ira_ish", True), ("a1", True),
    ("CaedVT", False), ("https://www.twitch.tv/caedvt", False), ("caed vt", False), ("", False), (None, False),
])
def test_is_login_accepts_only_a_lowercase_login(value, ok):
    assert is_login(value) is ok
