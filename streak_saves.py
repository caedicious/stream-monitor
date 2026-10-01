"""Save items, deadlines and the card verdict for automatic streak saves (1.12.0).

A broke or in-danger card from Twitch's bell, a save-streak link event (not
sent while STREAK_SOURCES leaves out "link"), a click on a Streaks at Risk
row in the popup, or a broadcast that ended before Stream Monitor watched it
long enough becomes one save item per streamer: a turn on Twitch's
save-streak page before the item's deadline.

Everything here is pure. Nothing reads the clock, a file or the network,
starts a thread, logs, or imports stream_monitor_tray: the tray passes `now`
in and does the I/O. Epochs are float seconds unless stated. Every ISO string
this module emits comes from epoch_to_iso, the tray's own format.

The 24-hour save window (SAVE_WINDOW_HOURS) is the existing code's belief
about Twitch and is UNVERIFIED; it lives in five places that change together
(the content scripts' age gate, the background's at-risk expiry default, the
popup's labels, the tray's toast, and here).
"""
import math
import re
from datetime import datetime, timezone

SAVE_WINDOW_HOURS = 24
SAVE_URGENT_HOURS = 6
CARD_AGE_MAX_SECONDS = 691200
CARD_AGE_UNITS = (1, 60, 3600, 86400)
EXPLICIT_DEADLINE_PAST_SECONDS = 3600
EXPLICIT_DEADLINE_FUTURE_SECONDS = 8 * 86400
# First path segments of twitch.tv pages that are not channels. The content
# scripts' STREAK_RESERVED_PATHS holds the same 13 names.
RESERVED_LOGINS = frozenset({
    "directory", "videos", "settings", "subscriptions", "inventory", "drops",
    "wallet", "save-streak", "popout", "embed", "moderator", "team", "search",
})
LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")
# /config publishes list(STREAK_SOURCES), the sources the extensions may send.
# "link" is not among them since the live check of 2026-10-01 (plan A41,
# A46): Twitch's sidebar "Save your Streak" pills link to VODs, never to a
# save-streak page, so the background forwards no link event. EVENT_SOURCES
# is every source a /streak_event payload may name (plan 3.5): a link event
# still becomes a link item, so adding "link" back here re-enables P7.
STREAK_SOURCES = ("bell", "page", "manual")
EVENT_SOURCES = ("bell", "page", "link", "manual")
# In-danger deadlines are whole hours, so a deadline that moves by less than
# this (plus the card's age unit) is label drift, not a new deadline.
STREAK_DEADLINE_SLACK_SECONDS = 3600

ITEM_KINDS = ("broke", "in_danger", "missed")
ITEM_ORIGINS = ("card", "link", "manual", "offline_edge", "absorbed")
SAVE_URL_TEMPLATE = "https://www.twitch.tv/save-streak/{}?sm=1"

_HOUR = 3600
_ITEM_LOGIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def epoch_to_iso(epoch: float) -> str:
    """ISO 8601 UTC with milliseconds and a trailing Z, byte for byte the
    format of stream_monitor_tray._epoch_to_iso."""
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_to_epoch(value):
    """Epoch seconds for an ISO 8601 timestamp (Z or offset), else None. One
    without an offset is read as UTC (the tray's _iso_to_epoch rule)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def save_url(login: str) -> str:
    """The save-streak URL, always built from the login (the c2ce458 rule)."""
    return SAVE_URL_TEMPLATE.format(login)


def login_ok(name) -> bool:
    """A Twitch login the desktop may turn into a save item: the login
    pattern, and not one of the reserved twitch.tv page names."""
    return isinstance(name, str) and bool(LOGIN_RE.match(name)) and name not in RESERVED_LOGINS


def infer_age_unit(card_age_s: int) -> int:
    """The unit of an "N units ago" label when the event did not say: the
    largest unit that divides the age. "just now" (0) reads as minutes."""
    if not _is_int(card_age_s) or card_age_s <= 0:
        return 60
    for unit in reversed(CARD_AGE_UNITS):
        if card_age_s % unit == 0:
            return unit
    return 1


def parse_card_extras(payload: dict, now: float) -> dict:
    """The optional 1.12 fields of a /streak_event payload. Invalid fields
    become None; source falls back to "bell"; deadline_at becomes an epoch
    and counts only within [now - 1 h, now + 8 days]."""
    payload = payload if isinstance(payload, dict) else {}
    age = payload.get("card_age_s")
    if not (_is_int(age) and age >= 0):
        age = None
    unit = payload.get("card_age_unit_s")
    if not (_is_int(unit) and unit in CARD_AGE_UNITS):
        unit = None
    if age is None:
        unit = None
    elif unit is None:
        unit = infer_age_unit(age)
    verified = payload.get("login_verified")
    if not isinstance(verified, bool):
        verified = None
    source = payload.get("source")
    if source not in EVENT_SOURCES:
        source = "bell"
    deadline = iso_to_epoch(payload.get("deadline_at"))
    if deadline is not None and not (
        now - EXPLICIT_DEADLINE_PAST_SECONDS <= deadline <= now + EXPLICIT_DEADLINE_FUTURE_SECONDS
    ):
        deadline = None
    return {
        "card_age_s": age,
        "card_age_unit_s": unit,
        "login_verified": verified,
        "source": source,
        "deadline_at": deadline,
    }


def _card_break(card: dict):
    """(break_at, unit): the earliest possible posting time of a card and its
    age unit, or (detected_at, 0) when the age is unknown."""
    detected = card.get("detected_at")
    age = card.get("card_age_s")
    if _is_int(age) and age >= 0:
        unit = card.get("card_age_unit_s")
        if not (_is_int(unit) and unit in CARD_AGE_UNITS):
            unit = infer_age_unit(age)
        return detected - age - unit, unit
    return detected, 0


def card_record(card: dict, deadline_at: float, now: float) -> dict:
    """The dedup record of one card: when it was last seen, its earliest
    possible posting time, its age unit (0 when unknown) and its deadline."""
    break_at, unit = _card_break(card)
    return {"seen_at": now, "break_at": break_at, "unit": unit, "deadline_at": deadline_at}


def card_relation(record: dict, incoming: dict) -> str:
    """Whether an incoming card record is the same card as a stored one, an
    older one, or a newer one. A floored "N units ago" label only places a
    card within [break_at, break_at + unit), so two readings whose intervals
    overlap can be one card. An unknown age on either side reads as the
    same card."""
    r_unit = record.get("unit") or 0
    i_unit = incoming.get("unit") or 0
    if r_unit == 0 or i_unit == 0:
        return "same"
    if incoming["break_at"] >= record["break_at"] + r_unit:
        return "newer"
    if incoming["break_at"] + i_unit <= record["break_at"]:
        return "older"
    return "same"


def is_escalation(record: dict, incoming: dict) -> bool:
    """A same or older card whose deadline is earlier than the stored one by
    more than the slack plus the larger of the two age units: a deadline
    update, not label drift."""
    if card_relation(record, incoming) == "newer":
        return False
    old = record.get("deadline_at")
    new = incoming.get("deadline_at")
    if not (_is_num(old) and _is_num(new)):
        return False
    unit = max(record.get("unit") or 0, incoming.get("unit") or 0)
    return old - new > STREAK_DEADLINE_SLACK_SECONDS + unit


def dedup_key(status, login, count) -> tuple:
    """The per-run dedup key of a card; link events use ("broke", login, None)."""
    return (status, login, count)


def _detected(detected_at, now: float) -> float:
    if not _is_num(detected_at) or detected_at > now:
        return float(now)
    return float(detected_at)


def _item(login, kind, count, break_at, deadline_at, age_unit_s, session, origin, now) -> dict:
    return {
        "login": login,
        "kind": kind,
        "count": count if _is_int(count) else None,
        "url": save_url(login),
        "break_at": float(break_at),
        "deadline_at": float(deadline_at),
        "age_unit_s": int(age_unit_s),
        "session": session if isinstance(session, str) else None,
        "origin": origin,
        "verify": False,
        "created_at": float(now),
        "open_failures": 0,
    }


def make_card_item(login, status, count, detected_at, deadline_hours, extras, now, session=None) -> dict:
    """A save item from a broke or in-danger card (DESIGN 11.3). A link
    event (extras source "link") gives the make_link_item item whatever age
    or deadline it carries (A14: no count, no age, break_at = detected_at)."""
    extras = extras if isinstance(extras, dict) else {}
    if extras.get("source") == "link":
        item = make_link_item(login, detected_at, now)
        item["session"] = session if isinstance(session, str) else None
        return item
    detected = _detected(detected_at, now)
    break_at, unit = _card_break({
        "detected_at": detected,
        "card_age_s": extras.get("card_age_s"),
        "card_age_unit_s": extras.get("card_age_unit_s"),
    })
    kind = status if status in ("broke", "in_danger") else "broke"
    hours = SAVE_WINDOW_HOURS
    if kind == "in_danger" and _is_int(deadline_hours) and 0 <= deadline_hours <= 8760:
        hours = deadline_hours
    deadline = break_at + hours * _HOUR
    explicit = extras.get("deadline_at")
    if _is_num(explicit):
        deadline = float(explicit)
    return _item(login, kind, count, break_at, deadline, unit, session, "card", now)


def make_link_item(login, detected_at, now) -> dict:
    """A save item from a save-streak link event: no count, no age."""
    detected = _detected(detected_at, now)
    return _item(login, "broke", None, detected, detected + SAVE_WINDOW_HOURS * _HOUR, 0, None, "link", now)


def make_manual_item(login, status, count, detected_at, deadline_at, now) -> dict:
    """A save item from a click on a Streaks at Risk row. The count may be
    None (a row made from a link event); the row's deadline wins when it has
    one."""
    detected = _detected(detected_at, now)
    kind = status if status in ("broke", "in_danger") else "broke"
    deadline = float(deadline_at) if _is_num(deadline_at) else detected + SAVE_WINDOW_HOURS * _HOUR
    return _item(login, kind, count, detected, deadline, 0, None, "manual", now)


def make_missed_item(login, last_seen_live, check_interval, session, now, origin="offline_edge") -> dict:
    """A save item for a broadcast that ended before it was watched long
    enough: the break is the last live sighting, the unit one poll."""
    break_at = float(last_seen_live) if _is_num(last_seen_live) else float(now)
    unit = int(check_interval) if _is_int(check_interval) and check_interval >= 0 else 0
    return _item(login, "missed", None, break_at, break_at + SAVE_WINDOW_HOURS * _HOUR, unit,
                 session, origin, now)


def item_expired(item: dict, now: float) -> bool:
    """Past the latest time the item's save window could close. An item
    exactly at deadline_at + age_unit_s is kept."""
    return now > item["deadline_at"] + (item.get("age_unit_s") or 0)


def merge_items(existing: dict, incoming: dict, now: float) -> tuple:
    """(merged, changed): one item per streamer (DESIGN 11.4, plan 3.8)."""
    src = incoming if incoming["break_at"] >= existing["break_at"] else existing
    merged = dict(existing)
    for key in ("kind", "count", "break_at", "age_unit_s", "session"):
        merged[key] = src.get(key)
    if incoming.get("kind") == "broke" and existing.get("kind") == "in_danger":
        deadline = incoming["deadline_at"]
    else:
        old_gone = item_expired(existing, now)
        new_gone = item_expired(incoming, now)
        if old_gone and new_gone:
            deadline = max(existing["deadline_at"], incoming["deadline_at"])
        elif old_gone:
            deadline = incoming["deadline_at"]
        elif new_gone:
            deadline = existing["deadline_at"]
        else:
            deadline = min(existing["deadline_at"], incoming["deadline_at"])
    merged["deadline_at"] = deadline
    merged["verify"] = bool(existing.get("verify")) and bool(incoming.get("verify"))
    if existing.get("origin") == "manual" or incoming.get("origin") == "manual":
        merged["origin"] = "manual"
    else:
        merged["origin"] = src.get("origin")
    merged["created_at"] = existing.get("created_at")
    merged["open_failures"] = existing.get("open_failures", 0)
    merged["url"] = save_url(existing["login"])
    return merged, merged != existing


def clean_item(raw, login=None):
    """A validated copy of a stored item (the plan 3.8 shape), or None."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("login", login)
    if login is not None and name != login:
        return None
    if not (isinstance(name, str) and _ITEM_LOGIN_RE.match(name)):
        return None
    if raw.get("kind") not in ITEM_KINDS or raw.get("origin") not in ITEM_ORIGINS:
        return None
    for key in ("break_at", "deadline_at", "created_at"):
        if not _is_num(raw.get(key)):
            return None
    unit = raw.get("age_unit_s")
    unit = unit if _is_int(unit) and unit >= 0 else 0
    failures = raw.get("open_failures")
    failures = failures if _is_int(failures) and failures >= 0 else 0
    item = _item(name, raw["kind"], raw.get("count"), raw["break_at"], raw["deadline_at"], unit,
                 raw.get("session"), raw["origin"], raw["created_at"])
    item["verify"] = raw.get("verify") is True
    item["open_failures"] = failures
    return item


def card_verdict(card: dict, save, *, lapsed_by_age: bool, watch_start) -> tuple:
    """(verdict, row) of a broke or in-danger card against the save in force
    (plan 3.5.2). The first matching row decides; row is None for a card
    with no save to judge against."""
    age = card.get("card_age_s")
    age_known = _is_int(age) and age >= 0
    if age_known and age > CARD_AGE_MAX_SECONDS:
        return "stale", 0
    if save is None:
        return "fresh", None
    saved_at = save["at"]
    if age_known:
        break_at, _ = _card_break(card)
        if break_at > saved_at:
            return "fresh", 1
        return "stale", 2
    count = card.get("count")
    saved_count = save.get("count")
    if _is_int(count) and _is_int(saved_count):
        if count > saved_count:
            return "fresh", 3
        if count < saved_count:
            return "stale", 4
    if lapsed_by_age:
        return "fresh", 7
    if watch_start is not None and watch_start <= saved_at:
        return "stale", 5
    return "verify", 6


def save_covers(item: dict, save, watch_start) -> bool:
    """Whether a save that counts now makes the item's save turn pointless
    (plan 3.13)."""
    if save is None:
        return False
    saved_at = save["at"]
    if item.get("origin") == "manual":
        return saved_at >= item["created_at"]
    if item.get("kind") == "missed":
        return saved_at >= item["break_at"]
    if (item.get("age_unit_s") or 0) > 0:
        return item["break_at"] <= saved_at
    count = item.get("count")
    saved_count = save.get("count")
    if _is_int(count) and _is_int(saved_count):
        if count > saved_count:
            return False
        if count < saved_count:
            return True
    return watch_start is not None and watch_start <= saved_at


def item_to_queued_vod(item: dict) -> dict:
    """The normal-mode queued_vods entry for an item (plan 3.8)."""
    return {
        "url": item["url"],
        "ended_at": epoch_to_iso(item["break_at"]),
        "deadline_at": epoch_to_iso(item["deadline_at"]),
        "age_unit_s": item.get("age_unit_s") or 0,
        "origin": item.get("origin"),
        "verify": bool(item.get("verify")),
        "item": dict(item),
    }


def queued_vod_expired(entry: dict, now: float) -> bool:
    """Only an entry with a parseable deadline_at ever expires."""
    deadline = iso_to_epoch(entry.get("deadline_at")) if isinstance(entry, dict) else None
    if deadline is None:
        return False
    unit = entry.get("age_unit_s")
    unit = unit if _is_int(unit) and unit >= 0 else 0
    return now > deadline + unit


def queued_vod_covered(entry: dict, save, watch_start) -> bool:
    """Coverage of a queued_vods entry: its embedded item when present, else
    the missed rule on ended_at. An unparseable ended_at counts as covered
    when a save counts, as in v1.11.2."""
    if save is None:
        return False
    item = clean_item(entry.get("item")) if isinstance(entry, dict) else None
    if item is not None:
        return save_covers(item, save, watch_start)
    ended = iso_to_epoch(entry.get("ended_at")) if isinstance(entry, dict) else None
    if ended is None:
        return True
    return save["at"] >= ended
