"""One Twitch login from whatever a user typed.

The Settings window, the setup wizard and the tray app all take channel
names from people, and a channel link or an @handle is what gets pasted
(a user put https://www.twitch.tv/<name> into "Your Twitch Channel", which
made every Twitch poll fail with HTTP 400). A login is letters, digits and
underscores; Twitch answers 400 to a user_login that is anything else.
"""
import re

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")
# A channel link: an optional scheme and www. or m., twitch.tv, then an
# optional popout, embed or save-streak segment before the login.
_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/(?:(?:popout|embed|save-streak)/)?([^/?#\s]+)",
                      re.IGNORECASE)


def login_from_text(text) -> str:
    """The lowercase login in text ("caedvt", "@CaedVT",
    "https://www.twitch.tv/caedvt/videos", "twitch.tv/caedvt?sm=1"), or ""
    when no login can be read from it."""
    s = str(text or "").strip()
    m = _LINK_RE.match(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip().lower()
    return s if LOGIN_RE.match(s) else ""


def is_login(text) -> bool:
    """Whether text is already a lowercase login Twitch accepts."""
    return bool(LOGIN_RE.match(str(text or "")))
