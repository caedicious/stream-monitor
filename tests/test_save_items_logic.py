"""Save items, deadlines, the card verdict and card identity (streak_saves.py, 1.12.0).

Pure unit tests of the desktop half of automatic streak saves: DESIGN 11.2 to
11.4 and 12.5 as the build plan fixes them (3.5.2 verdict rows, 3.5.3 card
identity, 3.8 items, 3.13 coverage), plus SlotScheduler.add_item's gate and
merge_only path. No clock, no files: every time is passed in.
"""
import ast
from pathlib import Path

import pytest

import slot_scheduler as ss
import streak_saves as sv

ROOT = Path(__file__).resolve().parent.parent
NOW = 1790000000.0
MIN = 60
HOUR = 3600
DAY = 24 * HOUR


def extras(age=None, unit=None, source="bell", deadline_at=None, verified=True):
    return {"card_age_s": age, "card_age_unit_s": unit, "source": source,
            "deadline_at": deadline_at, "login_verified": verified}


def inputs(auto_save=True, saves=None, watch_start=None, now=NOW):
    return {"now": now, "mono": 1000.0, "auto_save": auto_save, "saves": saves or {},
            "watch_start": watch_start or {}, "rank": {}, "listed": frozenset()}


# ---------------------------------------------------------------------------
# The two modules stand alone (3.13)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("epoch", [0.0, 1.5, 1790000000.0, 1790000000.123456, 1790000000.9999,
                                   1759147200.25, 2000000000.0])
def test_c13_epoch_to_iso_matches_the_tray(epoch):
    import stream_monitor_tray
    assert sv.epoch_to_iso(epoch) == stream_monitor_tray._epoch_to_iso(epoch)


def test_c13_iso_to_epoch_matches_the_tray():
    import stream_monitor_tray
    for value in ("2026-09-29T12:00:00.000Z", "2026-09-29T12:00:00+02:00", "2026-09-29T12:00:00",
                  "", "nonsense", None, 5):
        assert sv.iso_to_epoch(value) == stream_monitor_tray._iso_to_epoch(value)


@pytest.mark.parametrize("name", ["slot_scheduler.py", "streak_saves.py"])
def test_c13_the_pure_modules_parse_as_python_3_11_and_import_no_tray_io_or_clock(name):
    src = (ROOT / name).read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 11))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"json", "math", "re", "dataclasses", "typing", "datetime", "streak_saves"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "print", "__import__")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "time", "monotonic", "utcnow") or \
                not (isinstance(node.value, ast.Name) and node.value.id in ("time", "datetime"))


def test_c13_the_constants_of_the_contract():
    assert sv.SAVE_WINDOW_HOURS == 24 and sv.SAVE_URGENT_HOURS == 6
    assert sv.CARD_AGE_MAX_SECONDS == 691200
    assert sv.CARD_AGE_UNITS == (1, 60, 3600, 86400)
    assert sv.EXPLICIT_DEADLINE_PAST_SECONDS == 3600
    assert sv.EXPLICIT_DEADLINE_FUTURE_SECONDS == 8 * 86400
    # "link" left the published sources after the 2026-10-01 live check
    # (A41 item 2); the desktop still parses it (3.5).
    assert sv.STREAK_SOURCES == ("bell", "page", "manual")
    assert sv.EVENT_SOURCES == ("bell", "page", "link", "manual")
    assert sv.STREAK_DEADLINE_SLACK_SECONDS == 3600
    assert sv.LOGIN_RE.pattern == r"^[a-z0-9_]{1,25}$"
    assert (ss.SLOT_PLAN_VERSION, ss.SLOT_MAX_TOTAL, ss.SLOT_STATE_MAX_AGE_SECONDS,
            ss.SLOT_OPEN_GIVE_UP_SECONDS, ss.SLOT_OFFLINE_STRIKES, ss.SLOT_REMOVAL_MEMORY_SECONDS,
            ss.SLOT_OUTAGE_NOTIFY_SECONDS, ss.SLOT_LIVE_STALE_SECONDS,
            ss.SLOT_EXECUTOR_ALIVE_SECONDS, ss.SLOT_STARTUP_GRACE_SECONDS,
            ss.SLOT_QUEUE_PUBLISH_MAX, ss.SLOT_TOOLTIP_MAX) == (
        1, 3, 600, 600, 2, 900, 300, 300, 150, 90, 10, 110)


# ---------------------------------------------------------------------------
# Logins and event fields (P5, 3.5)
# ---------------------------------------------------------------------------

def test_ap05_reserved_logins_are_exactly_the_13_names_of_a19():
    assert sv.RESERVED_LOGINS == frozenset({
        "directory", "videos", "settings", "subscriptions", "inventory", "drops", "wallet",
        "save-streak", "popout", "embed", "moderator", "team", "search"})


@pytest.mark.parametrize("name,ok", [
    ("alice", True), ("a_1", True), ("x" * 25, True), ("x" * 26, False), ("Alice", False),
    ("popout", False), ("team", False), ("search", False), ("", False), ("al ice", False),
    (None, False), (7, False)])
def test_ap05_login_ok(name, ok):
    assert sv.login_ok(name) is ok


def test_c05_parse_card_extras_keeps_valid_fields():
    deadline = sv.epoch_to_iso(NOW + 5 * HOUR)
    got = sv.parse_card_extras({"card_age_s": 7200, "card_age_unit_s": 3600, "login_verified": True,
                                "source": "page", "deadline_at": deadline}, NOW)
    assert got == {"card_age_s": 7200, "card_age_unit_s": 3600, "login_verified": True,
                   "source": "page", "deadline_at": NOW + 5 * HOUR}


def test_c05_parse_card_extras_drops_invalid_fields():
    got = sv.parse_card_extras({"card_age_s": -5, "card_age_unit_s": 7, "login_verified": "yes",
                                "source": "chat", "deadline_at": "tomorrow"}, NOW)
    assert got == {"card_age_s": None, "card_age_unit_s": None, "login_verified": None,
                   "source": "bell", "deadline_at": None}
    assert sv.parse_card_extras({"card_age_s": True}, NOW)["card_age_s"] is None
    assert sv.parse_card_extras({"card_age_s": 1.5}, NOW)["card_age_s"] is None
    assert sv.parse_card_extras({}, NOW)["source"] == "bell"
    assert sv.parse_card_extras({"card_age_unit_s": 60}, NOW)["card_age_unit_s"] is None


@pytest.mark.parametrize("source", ["bell", "page", "link", "manual"])
def test_c05_am41_every_event_source_is_kept_including_the_unpublished_link(source):
    # /config stopped publishing "link" (A41 item 2), but a link event is
    # still a link, never a bell card with no count.
    assert sv.parse_card_extras({"source": source}, NOW)["source"] == source


def test_c05_an_age_above_eight_days_is_kept_for_the_stale_verdict():
    assert sv.parse_card_extras({"card_age_s": 700000}, NOW)["card_age_s"] == 700000


@pytest.mark.parametrize("age,unit", [(0, 60), (45, 1), (120, 60), (3600, 3600), (7200, 3600),
                                      (86400, 86400), (172800, 86400), (5400, 60)])
def test_c05_an_absent_unit_is_the_largest_that_divides_the_age(age, unit):
    assert sv.infer_age_unit(age) == unit
    assert sv.parse_card_extras({"card_age_s": age}, NOW)["card_age_unit_s"] == unit


@pytest.mark.parametrize("offset,kept", [(-HOUR, True), (-HOUR - 1, False), (8 * DAY, True),
                                         (8 * DAY + 1, False), (0, True)])
def test_as02_c05_an_explicit_deadline_counts_only_inside_its_window(offset, kept):
    got = sv.parse_card_extras({"deadline_at": sv.epoch_to_iso(NOW + offset)}, NOW)
    assert (got["deadline_at"] is not None) is kept


# ---------------------------------------------------------------------------
# Items and deadlines (DESIGN 11.2, 11.3, plan 3.8)
# ---------------------------------------------------------------------------

ITEM_KEYS = {"login", "kind", "count", "url", "break_at", "deadline_at", "age_unit_s", "session",
             "origin", "verify", "created_at", "open_failures"}


def test_as02_a_broke_card_of_known_age_counts_24_hours_from_its_earliest_posting_time():
    item = sv.make_card_item("alice", "broke", 12, NOW, None, extras(age=3 * HOUR, unit=3600), NOW,
                             session="2026-09-21T10:00:00.000Z")
    assert set(item) == ITEM_KEYS
    assert item["break_at"] == NOW - 3 * HOUR - HOUR
    assert item["deadline_at"] == item["break_at"] + 24 * HOUR
    assert item["age_unit_s"] == 3600 and item["kind"] == "broke" and item["count"] == 12
    assert item["url"] == "https://www.twitch.tv/save-streak/alice?sm=1"
    assert item["origin"] == "card" and item["verify"] is False and item["open_failures"] == 0
    assert item["created_at"] == NOW and item["session"] == "2026-09-21T10:00:00.000Z"


def test_as02_an_in_danger_card_uses_deadline_hours_and_defaults_to_24():
    item = sv.make_card_item("bob", "in_danger", 3, NOW, 5, extras(age=20 * MIN, unit=60), NOW)
    assert item["break_at"] == NOW - 21 * MIN
    assert item["deadline_at"] == item["break_at"] + 5 * HOUR
    assert item["kind"] == "in_danger"
    item = sv.make_card_item("bob", "in_danger", 3, NOW, None, extras(), NOW)
    assert item["deadline_at"] == NOW + 24 * HOUR


def test_as02_a_card_of_unknown_age_counts_from_detected_at():
    detected = NOW - 10 * MIN
    item = sv.make_card_item("carol", "broke", 2, detected, None, extras(), NOW)
    assert item["break_at"] == detected and item["age_unit_s"] == 0
    assert item["deadline_at"] == detected + 24 * HOUR
    later = sv.make_card_item("carol", "broke", 2, NOW + 60, None, extras(), NOW)
    assert later["break_at"] == NOW


def test_as02_an_explicit_deadline_overrides_the_card_rows():
    item = sv.make_card_item("dave", "broke", 2, NOW, None, extras(age=HOUR, deadline_at=NOW + 2 * HOUR), NOW)
    assert item["deadline_at"] == NOW + 2 * HOUR
    assert item["break_at"] == NOW - 2 * HOUR


def test_as02_ap07_a_link_item_has_no_count_and_no_age():
    item = sv.make_link_item("erin", NOW - 30, NOW)
    assert set(item) == ITEM_KEYS
    assert (item["kind"], item["count"], item["age_unit_s"], item["origin"]) == ("broke", None, 0, "link")
    assert item["break_at"] == NOW - 30 and item["deadline_at"] == NOW - 30 + 24 * HOUR
    via_card = sv.make_card_item("erin", "broke", None, NOW, None, extras(source="link"), NOW)
    assert via_card["origin"] == "link" and via_card["count"] is None


def test_as02_am14_a_link_event_through_the_card_builder_gets_the_link_item_shape():
    # A link event may carry an age (3.11); A14 still gives no age, no count
    # and break_at = detected_at, whichever builder WP3 routes it through.
    via_card = sv.make_card_item("erin", "broke", 7, NOW, 5,
                                 extras(age=2 * HOUR, unit=3600, source="link",
                                        deadline_at=NOW + HOUR), NOW, session="b1")
    assert via_card["age_unit_s"] == 0 and via_card["count"] is None
    assert via_card["break_at"] == NOW and via_card["deadline_at"] == NOW + 24 * HOUR
    assert via_card["origin"] == "link" and via_card["kind"] == "broke"
    assert via_card["session"] == "b1"
    assert dict(via_card, session=None) == sv.make_link_item("erin", NOW, NOW)
    card = sv.make_card_item("erin", "broke", 7, NOW, None, extras(age=2 * HOUR, unit=3600, source="page"), NOW)
    assert card["origin"] == "card" and card["age_unit_s"] == 3600


def test_as02_o16_a_manual_item_with_a_count_and_with_a_null_count():
    row_deadline = NOW + 7 * HOUR
    item = sv.make_manual_item("fay", "in_danger", 9, NOW - 60, row_deadline, NOW)
    assert (item["kind"], item["count"], item["origin"]) == ("in_danger", 9, "manual")
    assert item["break_at"] == NOW - 60 and item["deadline_at"] == row_deadline
    link_row = sv.make_manual_item("fay", "broke", None, NOW - 60, None, NOW)
    assert link_row["count"] is None
    assert link_row["deadline_at"] == NOW - 60 + 24 * HOUR and link_row["age_unit_s"] == 0


def test_as02_a_missed_item_breaks_at_the_last_sighting_with_the_poll_as_its_unit():
    item = sv.make_missed_item("gina", NOW - 5 * MIN, 60, "2026-09-21T10:00:00.000Z", NOW)
    assert (item["kind"], item["count"], item["origin"]) == ("missed", None, "offline_edge")
    assert item["break_at"] == NOW - 5 * MIN and item["deadline_at"] == NOW - 5 * MIN + 24 * HOUR
    assert item["age_unit_s"] == 60 and item["session"] == "2026-09-21T10:00:00.000Z"
    absorbed = sv.make_missed_item("gina", NOW, 0, None, NOW, origin="absorbed")
    assert absorbed["origin"] == "absorbed"


def test_ap03_as02_the_24_hour_save_window_is_one_constant(monkeypatch):
    monkeypatch.setattr(sv, "SAVE_WINDOW_HOURS", 48)
    assert sv.make_link_item("hank", NOW, NOW)["deadline_at"] == NOW + 48 * HOUR
    assert sv.make_missed_item("hank", NOW, 60, None, NOW)["deadline_at"] == NOW + 48 * HOUR
    assert sv.make_card_item("hank", "broke", 1, NOW, None, extras(), NOW)["deadline_at"] == NOW + 48 * HOUR


def test_as02_expiry_keeps_an_item_at_exactly_deadline_plus_unit_and_drops_it_a_second_later():
    item = sv.make_card_item("ivan", "broke", 4, NOW, None, extras(age=HOUR, unit=3600), NOW)
    edge = item["deadline_at"] + item["age_unit_s"]
    assert sv.item_expired(item, edge) is False
    assert sv.item_expired(item, edge + 1) is True
    unknown = sv.make_card_item("ivan", "broke", 4, NOW, None, extras(), NOW)
    assert sv.item_expired(unknown, unknown["deadline_at"]) is False
    assert sv.item_expired(unknown, unknown["deadline_at"] + 1) is True


# ---------------------------------------------------------------------------
# Merging (DESIGN 11.4, plan 3.8)
# ---------------------------------------------------------------------------

def test_as02_merge_the_later_break_supplies_kind_count_and_the_earlier_open_deadline_wins():
    old = sv.make_card_item("jay", "broke", 4, NOW - 5 * HOUR, None, extras(), NOW - 5 * HOUR)
    new = sv.make_missed_item("jay", NOW - HOUR, 60, "s2", NOW)
    merged, changed = sv.merge_items(old, new, NOW)
    assert changed is True
    assert (merged["kind"], merged["count"], merged["break_at"], merged["age_unit_s"], merged["session"]) == \
        ("missed", None, NOW - HOUR, 60, "s2")
    assert merged["deadline_at"] == old["deadline_at"]
    assert merged["origin"] == "offline_edge"
    assert merged["created_at"] == old["created_at"] and merged["open_failures"] == 0
    back, _ = sv.merge_items(new, old, NOW)
    assert back["kind"] == "missed" and back["deadline_at"] == old["deadline_at"]


def test_as02_merge_a_tie_takes_the_incoming_item():
    a = sv.make_card_item("kim", "broke", 4, NOW, None, extras(), NOW)
    b = sv.make_card_item("kim", "broke", 6, NOW, None, extras(), NOW)
    merged, changed = sv.merge_items(a, b, NOW)
    assert merged["count"] == 6 and changed is True


def test_as02_merge_broke_over_in_danger_takes_the_broke_deadline():
    danger = sv.make_card_item("lee", "in_danger", 5, NOW - HOUR, 3, extras(), NOW - HOUR)
    broke = sv.make_card_item("lee", "broke", 5, NOW, None, extras(), NOW)
    merged, _ = sv.merge_items(danger, broke, NOW)
    assert merged["kind"] == "broke" and merged["deadline_at"] == broke["deadline_at"]
    assert broke["deadline_at"] > danger["deadline_at"]
    other_way, _ = sv.merge_items(broke, danger, NOW)
    assert other_way["deadline_at"] == danger["deadline_at"]


def test_as02_merge_manual_origin_wins_and_verify_needs_both():
    manual = sv.make_manual_item("max", "broke", 3, NOW - HOUR, None, NOW - HOUR)
    card = sv.make_card_item("max", "broke", 3, NOW, None, extras(), NOW)
    card["verify"] = True
    merged, _ = sv.merge_items(manual, card, NOW)
    assert merged["origin"] == "manual" and merged["verify"] is False
    checked = dict(card)
    merged, _ = sv.merge_items(checked, dict(card, created_at=NOW + 5), NOW)
    assert merged["verify"] is True


def test_as02_merge_an_expired_deadline_gives_way_and_both_expired_keeps_the_later():
    old = sv.make_card_item("ned", "broke", 3, NOW - 30 * HOUR, None, extras(), NOW - 30 * HOUR)
    new = sv.make_card_item("ned", "broke", 3, NOW - 2 * HOUR, None, extras(), NOW)
    merged, _ = sv.merge_items(old, new, NOW)
    assert merged["deadline_at"] == new["deadline_at"]
    later = NOW + 40 * HOUR
    merged, _ = sv.merge_items(old, new, later)
    assert merged["deadline_at"] == max(old["deadline_at"], new["deadline_at"])


def test_as02_an_identical_merge_reports_no_change():
    item = sv.make_card_item("ola", "broke", 3, NOW, None, extras(), NOW)
    merged, changed = sv.merge_items(item, dict(item, created_at=NOW + 99), NOW)
    assert changed is False and merged == item


# ---------------------------------------------------------------------------
# add_item: the gate, coverage and merge_only (DESIGN 11.4, 12.2, plan 3.13)
# ---------------------------------------------------------------------------

def test_as02_add_item_creates_then_merges_and_logs_each_change():
    sched = ss.SlotScheduler()
    first = sv.make_card_item("pat", "broke", 4, NOW, None, extras(), NOW)
    changed, events = sched.add_item(first, inputs())
    assert changed is True
    assert events == [("streak_item_added", {
        "streamer": "pat", "kind": "broke", "deadline_at": sv.epoch_to_iso(NOW + DAY),
        "origin": "card", "verify": False, "merged": False, "mode": "slot"})]
    assert sched.add_item(dict(first), inputs()) == (False, [])
    shorter = sv.make_card_item("pat", "broke", 4, NOW, None, extras(deadline_at=NOW + 3 * HOUR), NOW)
    changed, events = sched.add_item(shorter, inputs())
    assert changed is True and events[0][1]["merged"] is True
    assert sched.items_snapshot()["pat"]["deadline_at"] == NOW + 3 * HOUR


def test_as02_add_item_is_gated_by_auto_save_except_for_manual_items():
    sched = ss.SlotScheduler()
    card = sv.make_card_item("quin", "broke", 4, NOW, None, extras(), NOW)
    assert sched.add_item(card, inputs(auto_save=False)) == (False, [])
    assert sched.items_snapshot() == {}
    manual = sv.make_manual_item("quin", "broke", None, NOW, None, NOW)
    changed, _ = sched.add_item(manual, inputs(auto_save=False))
    assert changed is True and sched.items_snapshot()["quin"]["origin"] == "manual"


def test_as02_add_item_skips_a_covered_or_expired_item():
    sched = ss.SlotScheduler()
    card = sv.make_card_item("rae", "broke", 4, NOW, None, extras(age=HOUR, unit=3600), NOW)
    covered, events = sched.add_item(card, inputs(saves={"rae": {"at": NOW - 60, "count": 4}}))
    assert covered is False
    assert events == [("vod_skipped", {"streamer": "rae", "reason": "streak_already_saved",
                                       "saved_at": sv.epoch_to_iso(NOW - 60)})]
    old = sv.make_card_item("rae", "broke", 4, NOW - 3 * DAY, None, extras(), NOW - 3 * DAY)
    expired, events = sched.add_item(old, inputs())
    assert expired is False and events[0][0] == "streak_item_expired"
    assert sched.items_snapshot() == {}


def test_as02_add_item_merge_only_merges_a_shorter_deadline_and_never_creates():
    sched = ss.SlotScheduler()
    danger = sv.make_card_item("sam", "in_danger", 5, NOW, 10, extras(), NOW)
    escalated = sv.make_card_item("sam", "in_danger", 5, NOW, 3, extras(), NOW)
    assert sched.add_item(escalated, inputs(), merge_only=True) == (False, [])
    assert sched.items_snapshot() == {}
    sched.add_item(danger, inputs())
    changed, events = sched.add_item(escalated, inputs(), merge_only=True)
    assert changed is True and events[0][1]["merged"] is True
    assert sched.items_snapshot()["sam"]["deadline_at"] == NOW + 3 * HOUR
    assert sched.add_item(escalated, inputs(), merge_only=True) == (False, [])
    assert sched.add_item(danger, inputs(), merge_only=True) == (False, [])
    assert sched.items_snapshot()["sam"]["deadline_at"] == NOW + 3 * HOUR


# ---------------------------------------------------------------------------
# queued_vods entries (plan 3.8, A8)
# ---------------------------------------------------------------------------

def test_as02_am08_a_queued_vod_carries_the_item_and_expires_only_with_a_deadline():
    item = sv.make_card_item("tia", "broke", 4, NOW, None, extras(age=HOUR, unit=3600), NOW)
    entry = sv.item_to_queued_vod(item)
    assert entry == {"url": item["url"], "ended_at": sv.epoch_to_iso(item["break_at"]),
                     "deadline_at": sv.epoch_to_iso(item["deadline_at"]), "age_unit_s": 3600,
                     "origin": "card", "verify": False, "item": item}
    edge = item["deadline_at"] + 3600
    assert sv.queued_vod_expired(entry, edge) is False
    assert sv.queued_vod_expired(entry, edge + 1) is True
    legacy = {"url": item["url"], "ended_at": sv.epoch_to_iso(NOW - 40 * DAY)}
    assert sv.queued_vod_expired(legacy, NOW) is False
    assert sv.queued_vod_expired(dict(legacy, deadline_at="garbage"), NOW) is False


def test_as03_queued_vod_coverage_uses_the_embedded_item_else_the_ended_at_rule():
    item = sv.make_card_item("uma", "broke", 4, NOW, None, extras(age=HOUR, unit=3600), NOW)
    entry = sv.item_to_queued_vod(item)
    assert sv.queued_vod_covered(entry, None, None) is False
    assert sv.queued_vod_covered(entry, {"at": item["break_at"] - 1, "count": 4}, None) is False
    assert sv.queued_vod_covered(entry, {"at": item["break_at"], "count": 4}, None) is True
    ended = NOW - HOUR
    plain = {"url": item["url"], "ended_at": sv.epoch_to_iso(ended)}
    assert sv.queued_vod_covered(plain, {"at": ended - 60, "count": 1}, None) is False
    assert sv.queued_vod_covered(plain, {"at": ended + 60, "count": 1}, None) is True
    broken = {"url": item["url"], "ended_at": "not a time"}
    assert sv.queued_vod_covered(broken, {"at": NOW - 60 * DAY, "count": 1}, None) is True
    assert sv.queued_vod_covered(broken, None, None) is False


def test_as02_clean_item_validates_a_stored_item():
    item = sv.make_card_item("val", "broke", 4, NOW, None, extras(), NOW)
    assert sv.clean_item(dict(item)) == item
    assert sv.clean_item(dict(item), "val") == item
    assert sv.clean_item(dict(item), "other") is None
    assert sv.clean_item(dict(item, kind="lost")) is None
    assert sv.clean_item(dict(item, deadline_at="soon")) is None
    assert sv.clean_item(dict(item, login="Bad Name")) is None
    assert sv.clean_item("text") is None
    assert sv.clean_item(dict(item, verify="yes"))["verify"] is False


# ---------------------------------------------------------------------------
# Card identity and escalation (3.5.3, A40)
# ---------------------------------------------------------------------------

def rec(detected, age, unit, deadline=None):
    card = {"detected_at": detected, "card_age_s": age, "card_age_unit_s": unit, "count": 5}
    return sv.card_record(card, deadline, detected)


def test_ap06_c05_a_card_read_across_a_label_rollover_is_the_same_card():
    posted = NOW
    first = rec(posted + 70 * MIN, 3600, 3600)
    second = rec(posted + 110 * MIN, 3600, 3600)
    assert first["break_at"] == posted - 50 * MIN
    assert second["break_at"] == posted - 10 * MIN
    assert first["break_at"] // HOUR != second["break_at"] // HOUR
    assert sv.card_relation(first, second) == "same"
    assert sv.card_relation(second, first) == "same"
    two_hours = rec(posted + 125 * MIN, 7200, 3600)
    assert sv.card_relation(first, two_hours) == "same"


def test_ap06_c05_an_in_danger_card_with_the_same_count_reissued_five_hours_later_is_newer():
    first = rec(NOW, 20 * MIN, 60)
    reissued = rec(NOW + 5 * HOUR, 10 * MIN, 60)
    assert sv.card_relation(first, reissued) == "newer"
    assert sv.card_relation(reissued, first) == "older"


def test_ap06_c05_an_older_card_is_older():
    stored = rec(NOW, 2 * MIN, 60)
    older = rec(NOW, 3 * HOUR, 3600)
    assert sv.card_relation(stored, older) == "older"


def test_ap06_c05_unknown_ages_are_the_same_card():
    known = rec(NOW, 3600, 3600)
    unknown = rec(NOW + 9 * HOUR, None, None)
    assert unknown["unit"] == 0 and unknown["break_at"] == NOW + 9 * HOUR
    assert sv.card_relation(known, unknown) == "same"
    assert sv.card_relation(unknown, known) == "same"
    assert sv.card_relation(unknown, rec(NOW, None, None)) == "same"


def test_ap06_c05_a_card_record_holds_the_last_sighting_break_unit_and_deadline():
    card = {"detected_at": NOW, "card_age_s": 120, "card_age_unit_s": None, "count": 3}
    assert sv.card_record(card, NOW + DAY, NOW + 5) == {
        "seen_at": NOW + 5, "break_at": NOW - 180, "unit": 60, "deadline_at": NOW + DAY}


def test_ap06_c05_escalation_needs_more_than_the_slack_plus_the_larger_unit():
    slack = sv.STREAK_DEADLINE_SLACK_SECONDS
    stored = rec(NOW, 20 * MIN, 60, deadline=NOW + 10 * HOUR)
    at_limit = rec(NOW + MIN, 21 * MIN, 60, deadline=NOW + 10 * HOUR - slack - 60)
    over = rec(NOW + MIN, 21 * MIN, 60, deadline=NOW + 10 * HOUR - slack - 61)
    drift = rec(NOW + MIN, 21 * MIN, 60, deadline=NOW + 10 * HOUR - slack)
    assert sv.card_relation(stored, over) == "same"
    assert sv.is_escalation(stored, over) is True
    assert sv.is_escalation(stored, at_limit) is False
    assert sv.is_escalation(stored, drift) is False
    hour_unit = rec(NOW, HOUR, 3600, deadline=NOW + 10 * HOUR)
    assert sv.is_escalation(hour_unit, rec(NOW, HOUR, 3600, deadline=NOW + 10 * HOUR - 2 * HOUR)) is False
    assert sv.is_escalation(hour_unit, rec(NOW, HOUR, 3600, deadline=NOW + 10 * HOUR - 2 * HOUR - 1)) is True
    newer = rec(NOW + 5 * HOUR, MIN, 60, deadline=NOW)
    assert sv.is_escalation(stored, newer) is False
    assert sv.is_escalation(stored, rec(NOW, 20 * MIN, 60, deadline=None)) is False


def test_ap06_c05_the_dedup_key_has_no_posting_hour():
    assert sv.dedup_key("broke", "alice", 12) == ("broke", "alice", 12)
    assert sv.dedup_key("broke", "alice", None) == ("broke", "alice", None)


# ---------------------------------------------------------------------------
# The card verdict (3.5.2, DESIGN 12.5 C2, O1 (c), A4)
# ---------------------------------------------------------------------------

SAVE_AT = NOW - 10 * HOUR


def card(age=None, unit=None, count=4, detected=NOW):
    return {"detected_at": detected, "card_age_s": age, "card_age_unit_s": unit, "count": count}


def verdict(c, save=None, lapsed=False, watch=None):
    return sv.card_verdict(c, save, lapsed_by_age=lapsed, watch_start=watch)


def test_as03_row_0_a_card_older_than_eight_days_is_stale_even_without_a_save():
    assert verdict(card(age=691201, unit=1)) == ("stale", 0)
    assert verdict(card(age=691200, unit=1)) == ("fresh", None)
    assert verdict(card(age=691201, unit=1), {"at": SAVE_AT, "count": 4}) == ("stale", 0)


def test_as03_no_save_is_fresh():
    assert verdict(card()) == ("fresh", None)
    assert verdict(card(age=3600, unit=3600)) == ("fresh", None)


def test_as03_row_1_a_known_age_card_posted_after_the_save_is_fresh():
    save = {"at": SAVE_AT, "count": 4}
    assert verdict(card(age=5 * HOUR, unit=3600), save) == ("fresh", 1)


def test_as03_row_2_a_known_age_card_with_a_higher_count_posted_before_the_save_is_stale():
    save = {"at": SAVE_AT, "count": 4}
    assert verdict(card(age=11 * HOUR, unit=3600, count=9), save) == ("stale", 2)
    edge = card(age=9 * HOUR, unit=3600, count=4)
    assert verdict(edge, save) == ("stale", 2)
    just_after = card(age=9 * HOUR - 1, unit=3600, count=4)
    assert verdict(just_after, save) == ("fresh", 1)


def test_as03_row_3_unknown_age_higher_count_is_fresh():
    assert verdict(card(count=5), {"at": SAVE_AT, "count": 4}) == ("fresh", 3)


def test_as03_row_4_the_owners_3_stream_card_against_a_kept_4_stream_streak_is_stale():
    assert verdict(card(count=3), {"at": SAVE_AT, "count": 4}) == ("stale", 4)


def test_as03_row_5_equal_count_watched_without_a_gap_since_the_save_is_stale():
    save = {"at": SAVE_AT, "count": 4}
    assert verdict(card(count=4), save, watch=SAVE_AT - HOUR) == ("stale", 5)
    assert verdict(card(count=4), save, watch=SAVE_AT) == ("stale", 5)


def test_o01_row_6_equal_count_after_a_gap_in_watching_is_verify():
    save = {"at": SAVE_AT, "count": 4}
    assert verdict(card(count=4), save, watch=SAVE_AT + 60) == ("verify", 6)
    assert verdict(card(count=4), save, watch=None) == ("verify", 6)


def test_as03_o01_a_null_count_link_event_uses_rows_5_to_7():
    save = {"at": SAVE_AT, "count": 4}
    assert verdict(card(count=None), save, watch=SAVE_AT - 1) == ("stale", 5)
    assert verdict(card(count=None), save, watch=None) == ("verify", 6)
    assert verdict(card(count=None), save, lapsed=True) == ("fresh", 7)


def test_am04_o02_row_7_a_save_lapsed_by_age_makes_an_equal_count_card_fresh():
    lapsed = {"at": NOW - 8 * DAY, "count": 4}
    assert verdict(card(count=4), lapsed, lapsed=True, watch=lapsed["at"] - 1) == ("fresh", 7)


def test_am04_o02_a_save_lapsed_by_age_still_feeds_rows_0_to_4():
    lapsed = {"at": NOW - 8 * DAY, "count": 4}
    assert verdict(card(count=3), lapsed, lapsed=True) == ("stale", 4)
    assert verdict(card(count=5), lapsed, lapsed=True) == ("fresh", 3)
    assert verdict(card(age=9 * DAY // 2, unit=86400, count=4), lapsed, lapsed=True) == ("fresh", 1)
    assert verdict(card(age=691201, unit=1, count=9), lapsed, lapsed=True) == ("stale", 0)
    old_card = card(age=691200 - 5, unit=1, count=9)
    assert verdict(old_card, {"at": NOW - 3 * DAY, "count": 4}, lapsed=True) == ("stale", 2)


# ---------------------------------------------------------------------------
# save_covers (3.13, DESIGN 12.5 C3)
# ---------------------------------------------------------------------------

def test_as03_no_save_covers_nothing():
    item = sv.make_card_item("wyn", "broke", 4, NOW, None, extras(), NOW)
    assert sv.save_covers(item, None, NOW) is False


def test_as03_a_manual_item_is_covered_by_a_save_seen_since_it_was_made():
    item = sv.make_manual_item("xan", "broke", 4, NOW - HOUR, None, NOW)
    assert sv.save_covers(item, {"at": NOW - 1, "count": 9}, NOW - DAY) is False
    assert sv.save_covers(item, {"at": NOW, "count": 1}, None) is True


def test_as03_a_save_seen_during_a_missed_broadcast_does_not_cover_it_one_seen_after_does():
    ended = NOW - HOUR
    for origin in ("offline_edge", "absorbed"):
        item = sv.make_missed_item("yul", ended, 60, "b1", NOW, origin=origin)
        assert sv.save_covers(item, {"at": ended - 20 * MIN, "count": 4}, NOW - DAY) is False
        assert sv.save_covers(item, {"at": ended + 5 * MIN, "count": 4}, None) is True


def test_as03_known_age_card_and_link_items_are_covered_by_a_save_at_or_after_their_break():
    item = sv.make_card_item("zoe", "broke", 4, NOW, None, extras(age=HOUR, unit=3600), NOW)
    assert sv.save_covers(item, {"at": item["break_at"], "count": 1}, None) is True
    assert sv.save_covers(item, {"at": item["break_at"] - 1, "count": 99}, NOW - DAY) is False


def test_as03_unknown_age_items_are_covered_by_count_then_by_watching():
    item = sv.make_card_item("abe", "broke", 4, NOW, None, extras(), NOW)
    at = NOW - HOUR
    assert sv.save_covers(item, {"at": at, "count": 3}, at - 1) is False
    assert sv.save_covers(item, {"at": at, "count": 5}, None) is True
    assert sv.save_covers(item, {"at": at, "count": 4}, at - 1) is True
    assert sv.save_covers(item, {"at": at, "count": 4}, at + 1) is False
    assert sv.save_covers(item, {"at": at, "count": 4}, None) is False
    link = sv.make_link_item("abe", NOW, NOW)
    assert sv.save_covers(link, {"at": at, "count": 4}, at) is True
    assert sv.save_covers(link, {"at": at, "count": 4}, None) is False


def test_as03_in_danger_items_follow_the_card_rules_for_known_and_unknown_ages():
    known = sv.make_card_item("ada", "in_danger", 6, NOW, 5, extras(age=20 * MIN, unit=60), NOW)
    assert known["kind"] == "in_danger" and known["age_unit_s"] == 60
    assert sv.save_covers(known, {"at": known["break_at"], "count": 1}, None) is True
    assert sv.save_covers(known, {"at": known["break_at"] - 1, "count": 99}, NOW - DAY) is False
    unknown = sv.make_card_item("ada", "in_danger", 6, NOW, 5, extras(), NOW)
    assert unknown["kind"] == "in_danger" and unknown["age_unit_s"] == 0
    at = NOW - HOUR
    assert sv.save_covers(unknown, {"at": at, "count": 5}, at - 1) is False
    assert sv.save_covers(unknown, {"at": at, "count": 7}, None) is True
    assert sv.save_covers(unknown, {"at": at, "count": 6}, at - 1) is True
    assert sv.save_covers(unknown, {"at": at, "count": 6}, None) is False
    manual = sv.make_manual_item("ada", "in_danger", 6, NOW - HOUR, NOW + 4 * HOUR, NOW)
    assert sv.save_covers(manual, {"at": NOW - 1, "count": 99}, NOW - DAY) is False
    assert sv.save_covers(manual, {"at": NOW, "count": 1}, None) is True


@pytest.mark.parametrize("kind", ["broke", "in_danger"])
def test_as03_an_absorbed_card_kind_item_is_judged_by_its_kind_and_age(kind):
    # S2a now makes every absorbed item a missed item; an absorbed broke or
    # in_danger item can still come back from an older slot_state.json.
    known = dict(sv.make_card_item("bex", kind, 4, NOW, None, extras(age=HOUR, unit=3600), NOW),
                 origin="absorbed")
    known = sv.clean_item(known)
    assert known["origin"] == "absorbed" and known["kind"] == kind
    assert sv.save_covers(known, {"at": known["break_at"], "count": 1}, None) is True
    assert sv.save_covers(known, {"at": known["break_at"] - 1, "count": 9}, NOW - DAY) is False
    unknown = sv.clean_item(dict(sv.make_card_item("bex", kind, 4, NOW, None, extras(), NOW),
                                 origin="absorbed"))
    at = NOW - HOUR
    assert sv.save_covers(unknown, {"at": at, "count": 3}, at - 1) is False
    assert sv.save_covers(unknown, {"at": at, "count": 5}, None) is True
    assert sv.save_covers(unknown, {"at": at, "count": 4}, at - 1) is True
    assert sv.save_covers(unknown, {"at": at, "count": 4}, None) is False
