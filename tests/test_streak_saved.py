"""Saved-streak memory (v1.11.2).

Twitch's "Save your streak" page can say "You've already maintained your
N-stream streak with X". The extension reports that as an already_saved
streak event; while the save counts, the desktop treats the streak as safe:
stale broke cards are logged but not notified, and no save-streak link is
queued, flushed, or offered for rescue.

A save stops counting when X goes live after it (Helix started_at, also
when it changes while X stays live), when the broadcast running while it
was seen ends and Stream Monitor did not have it open (skipped for a
pause), when a card with a higher count arrives, or when it runs out: 24
hours for a login the monitor does not poll, 7 days for any.
"""
import json
from unittest.mock import patch

import pytest

import stream_monitor_tray as sm

SAVED_AT = "2026-09-28T04:00:00.000Z"
SAVED_EPOCH = sm._iso_to_epoch(SAVED_AT)
# Every test runs on this pinned clock, so none of them depends on the day
# the suite runs (a real clock turned the 30-day prune into a time bomb).
NOW = "2026-09-28T08:00:00.000Z"
NOW_EPOCH = sm._iso_to_epoch(NOW)
HOUR = 3600
DAY = 24 * HOUR
SAVE_URL = "https://www.twitch.tv/save-streak/{}?sm=1"


@pytest.fixture(autouse=True)
def clean_streak_state(tmp_config_dir, monkeypatch):
    """Every test starts with no remembered streaks, an empty /config, alice
    and bob polled (the monitor fixture's list) and the clock at NOW.
    monkeypatch puts all of it back afterwards, so nothing leaks into other
    test files whatever order they run in."""
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset({"alice", "bob"}))
    monkeypatch.setattr(sm, "_streak_clock", lambda: NOW_EPOCH)
    monkeypatch.setattr(sm, "_tray_notifier", None)
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", {})


@pytest.fixture
def toasts(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "_tray_notifier", lambda title, msg: calls.append((title, msg)))
    return calls


def _event(status, streamer="alice", count=4, detected_at=SAVED_AT):
    return {"status": status, "streamer": streamer, "count": count, "detected_at": detected_at}


def _set_clock(monkeypatch, epoch):
    monkeypatch.setattr(sm, "_streak_clock", lambda: epoch)


def _helix(started_at, login="alice"):
    """A /helix/streams answer with one live stream."""
    return {"data": [{"user_login": login, "user_id": "1", "started_at": started_at}]}


def _poll(monitor, helix):
    """One monitor poll against a canned Helix answer."""
    with patch.object(monitor, "_api_get", return_value=helix), \
         patch.object(monitor, "open_stream"):
        monitor.process_state_changes(monitor.check_streams())


def _relaunch(monitor):
    """What a new process starts from: the state file, an empty session
    dedup, and every streamer as never seen."""
    sm.load_streak_state(now_epoch=sm._streak_clock())
    sm._streak_event_seen.clear()
    monitor.streamers = {name: sm.StreamerState(name=name) for name in monitor.streamers}


def test_already_saved_is_recorded_logged_notified_and_published(toasts):
    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event("already_saved"))

    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert mlog.call_args.args[0] == "streak_already_saved"
    assert len(toasts) == 1
    assert "already safe" in toasts[0][0]
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {"alice": SAVED_AT}


def test_stale_broke_card_after_a_save_is_logged_but_not_notified(toasts):
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()

    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event("broke", detected_at="2026-09-28T05:00:00.000Z"))
        sm.handle_streak_event(_event("in_danger", detected_at="2026-09-28T05:10:00.000Z"))

    assert toasts == []
    assert [c.args[0] for c in mlog.call_args_list] == ["streak_event_ignored"] * 2
    assert all(c.kwargs["reason"] == "already_saved" for c in mlog.call_args_list)


def test_cards_may_carry_a_display_name(toasts):
    # Only already_saved needs a login; a card's name can come from its text,
    # which may be a Japanese display name.
    display_name = "".join(chr(c) for c in (0x30C9, 0x30E9, 0x30A4, 0x30D6))
    sm.handle_streak_event(_event("broke", streamer=display_name))
    assert len(toasts) == 1


def test_a_new_broadcast_ends_the_save(toasts):
    sm.handle_streak_event(_event("already_saved"))
    sm.record_stream_live("alice", "2026-09-28T06:00:00.000Z")
    assert sm.streak_saved_since_last_live("alice") is None
    toasts.clear()

    sm.handle_streak_event(_event("broke", detected_at="2026-09-28T07:00:00.000Z"))

    # Same card text as before, judged afresh: this time it matters.
    assert len(toasts) == 1
    assert "broke" in toasts[0][0]


def test_a_save_seen_during_the_broadcast_still_counts():
    sm.record_stream_live("alice", "2026-09-28T03:00:00.000Z")
    sm.handle_streak_event(_event("already_saved", detected_at=SAVED_AT))
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT


# ---------------------------------------------------------------------------
# The session dedup: a save forgets the cards it covers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streamer", ["alice", "driveyabatty"])
def test_a_card_announced_before_the_save_alerts_again_once_the_save_ends(
        monkeypatch, toasts, streamer):
    """The owner's flow: the stale bell card is scraped, and toasted, before
    its save link says "already maintained". Once the save ends (alice goes
    live; nothing polls driveyabatty, so it runs out) a real break reads
    exactly like that card, and it alerts."""
    for status in ("broke", "in_danger"):
        sm.handle_streak_event(_event(status, streamer=streamer,
                                      detected_at="2026-09-28T02:00:00.000Z"))
    sm.handle_streak_event(_event("already_saved", streamer=streamer,
                                  detected_at="2026-09-28T03:00:00.000Z"))
    if streamer == "alice":
        sm.record_stream_live("alice", "2026-09-28T05:00:00.000Z")
    else:
        _set_clock(monkeypatch, NOW_EPOCH + 2 * DAY)
    assert sm.streak_saved_since_last_live(streamer) is None
    toasts.clear()

    later = sm._epoch_to_iso(sm._streak_clock())
    for status in ("broke", "in_danger"):
        sm.handle_streak_event(_event(status, streamer=streamer, detected_at=later))

    assert [title for title, _ in toasts] == [
        f"{streamer}: 4-stream streak broke",
        f"{streamer}: 4-stream streak in danger",
    ]


def test_a_late_lower_count_save_keeps_a_newer_break_announced(toasts):
    """A save forgets only the cards it covers (its count or lower). The
    5-stream break is already announced when a late report about the
    4-stream streak arrives; the next copy of that card ends the save (the
    streak grew) but is not toasted a second time."""
    sm.handle_streak_event(_event("broke", count=5, detected_at="2026-09-28T05:00:00.000Z"))
    sm.handle_streak_event(_event("already_saved", count=4, detected_at="2026-09-28T04:00:00.000Z"))
    toasts.clear()

    sm.handle_streak_event(_event("broke", count=5, detected_at="2026-09-28T06:00:00.000Z"))

    assert toasts == []
    assert sm.streak_saved_since_last_live("alice") is None


def test_a_card_and_a_save_are_judged_under_one_lock(monkeypatch):
    """A card looks for a save and adds its key under the lock a save is
    recorded (and forgets the keys it covers) under, so a card arriving
    together with the save cannot leave a covered key behind."""
    real_counted, real_record = sm._counted_save, sm.record_streak_saved

    def counted(*args, **kwargs):
        assert sm._streak_event_lock.locked(), "card judged outside the lock"
        return real_counted(*args, **kwargs)

    def record(*args, **kwargs):
        assert sm._streak_event_lock.locked(), "save recorded outside the lock"
        return real_record(*args, **kwargs)

    monkeypatch.setattr(sm, "_counted_save", counted)
    monkeypatch.setattr(sm, "record_streak_saved", record)
    sm.handle_streak_event(_event("broke"))
    sm.handle_streak_event(_event("already_saved"))
    sm.handle_streak_event(_event("broke"))

    assert sm.saved_streaks_for_config() == {"alice": SAVED_AT}


# ---------------------------------------------------------------------------
# What the desktop cannot observe: unpolled logins, missed broadcasts, and
# a streak that grew since the save.
# ---------------------------------------------------------------------------


def test_a_save_for_a_login_the_monitor_does_not_poll_lasts_24_hours(monkeypatch, toasts):
    """Nothing polls driveyabatty, so no go-live can end that save; it runs
    out after 24 hours instead. A polled streamer's save of the same age
    still counts."""
    sm.handle_streak_event(_event("already_saved", streamer="driveyabatty"))
    sm.handle_streak_event(_event("already_saved", streamer="alice"))
    _set_clock(monkeypatch, SAVED_EPOCH + DAY - 60)
    assert sm.streak_saved_since_last_live("driveyabatty") == SAVED_AT

    _set_clock(monkeypatch, SAVED_EPOCH + DAY + 60)
    assert sm.streak_saved_since_last_live("driveyabatty") is None
    assert sm.saved_streaks_for_config() == {"alice": SAVED_AT}
    toasts.clear()
    sm.handle_streak_event(_event("broke", streamer="driveyabatty"))
    assert len(toasts) == 1 and "broke" in toasts[0][0]


def test_every_save_stops_counting_after_seven_days(monkeypatch, toasts):
    """alice is polled, but none of her broadcasts was seen (the desktop may
    have been off for them): a week is the most any save lasts."""
    sm.handle_streak_event(_event("already_saved"))
    _set_clock(monkeypatch, SAVED_EPOCH + 7 * DAY - 60)
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT

    _set_clock(monkeypatch, SAVED_EPOCH + 7 * DAY + 60)
    assert sm.streak_saved_since_last_live("alice") is None
    sm.publish_saved_streaks()
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}
    toasts.clear()
    sm.handle_streak_event(_event("broke"))
    assert len(toasts) == 1


@pytest.mark.parametrize("status", ["broke", "in_danger"])
def test_a_card_with_a_higher_count_ends_the_save(status, toasts):
    """Twitch said the 4-stream streak was kept; a card about a 5-stream
    streak means it grew after that, so it is a newer break: the save ends
    and the card alerts."""
    sm.handle_streak_event(_event("already_saved", count=4))
    toasts.clear()

    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event(status, count=4, detected_at="2026-09-28T05:00:00.000Z"))
        assert toasts == []  # same count: the stale card
        sm.handle_streak_event(_event(status, count=5, detected_at="2026-09-28T06:00:00.000Z"))

    assert sm.streak_saved_since_last_live("alice") is None
    assert sm._streak_state["saved"] == {}
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}
    assert len(toasts) == 1 and "5-stream" in toasts[0][0]
    ended = [c.kwargs for c in mlog.call_args_list if c.args[0] == "streak_save_ended"]
    assert len(ended) == 1
    assert ended[0]["reason"] == "count_grew"
    assert (ended[0]["saved_count"], ended[0]["card_count"]) == (4, 5)


# ---------------------------------------------------------------------------
# Broadcast boundaries: Helix started_at, and the end of the broadcast a
# save was seen during.
# ---------------------------------------------------------------------------


def test_the_go_live_is_the_broadcast_start_from_helix(monitor):
    with patch("stream_monitor_tray.log_activity") as mlog:
        _poll(monitor, _helix("2026-09-28T03:00:00Z"))

    assert monitor.live_stream_meta["alice"]["started_at"] == "2026-09-28T03:00:00Z"
    assert sm._streak_state["last_live"]["alice"] == "2026-09-28T03:00:00Z"
    live = next(c for c in mlog.call_args_list if c.args[0] == "stream_live")
    assert live.kwargs["started_at"] == "2026-09-28T03:00:00Z"


def test_going_live_records_the_broadcast_and_republishes(monitor):
    sm.record_streak_saved("alice", 4, SAVED_AT)
    assert sm.saved_streaks_for_config() == {"alice": SAVED_AT}

    with patch.object(monitor, "open_stream"):
        monitor.process_state_changes({"alice": True, "bob": False})

    # No started_at from Helix: the poll time stands in for it.
    assert sm._streak_state["last_live"]["alice"] == NOW
    assert sm.streak_saved_since_last_live("alice") is None
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}


def test_a_relaunch_mid_broadcast_keeps_a_save_seen_during_it(monitor):
    """alice has been live since 03:00 and Twitch said the streak was kept
    at 04:00. A relaunch sees her live on its first poll, which used to look
    like a new broadcast and erase the save. Helix gives the same start, so
    it is the same broadcast and the save stays."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    _relaunch(monitor)
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))

    assert sm._streak_state["last_live"]["alice"] == "2026-09-28T03:00:00Z"
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {"alice": SAVED_AT}


def test_a_broadcast_that_started_after_the_save_ends_it(monitor):
    sm.handle_streak_event(_event("already_saved"))
    _poll(monitor, _helix("2026-09-28T05:00:00Z"))
    assert sm.streak_saved_since_last_live("alice") is None
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}


def test_a_save_seen_during_a_broadcast_ends_when_that_broadcast_ends(monitor, toasts):
    """Twitch's answer at 03:30 could only speak for alice's broadcasts
    before the one running then. When that broadcast (skipped for a pause)
    ends, the save stops counting: the save-streak link opens as it always
    did, and the real break card for it alerts."""
    monitor.config.vod_fallback = True
    monitor.streamers["alice"].was_live = True
    monitor.missed_while_paused["alice"] = "03:00:00"
    sm.record_stream_live("alice", "2026-09-28T03:00:00.000Z")
    sm.handle_streak_event(_event("already_saved", detected_at="2026-09-28T03:30:00.000Z"))
    assert sm.streak_saved_since_last_live("alice") == "2026-09-28T03:30:00.000Z"
    toasts.clear()

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
        assert monitor.wait_for_pending_opens(timeout=5)

    mopen.assert_called_once_with(SAVE_URL.format("alice"))
    assert "alice" not in monitor.missed_while_paused
    assert sm.streak_saved_since_last_live("alice") is None
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}
    sm.handle_streak_event(_event("broke", detected_at="2026-09-28T09:00:00.000Z"))
    assert len(toasts) == 1 and "broke" in toasts[0][0]


def test_a_broadcast_stream_monitor_had_open_keeps_the_save_past_its_end(
        monitor, monkeypatch, toasts):
    """alice's 03:00 broadcast opened in a tab and Twitch said the streak
    was kept at 04:00. That broadcast ending does not end the save: the
    owner's rule is "until the next go-live", so the stale card stays quiet
    and /config keeps her. A relaunch afterwards does not take that end for
    one it missed. Her next broadcast ends the save."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    _poll(monitor, {"data": []})

    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {"alice": SAVED_AT}
    assert sm._streak_state["last_offline"]["alice"] == NOW
    assert "alice" not in sm._streak_state["missed_end"]

    _relaunch(monitor)
    monitor.process_state_changes({"alice": False, "bob": False})
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    toasts.clear()
    sm.handle_streak_event(_event("broke", detected_at=NOW))
    assert toasts == []

    _set_clock(monkeypatch, NOW_EPOCH + 2 * HOUR)
    _poll(monitor, _helix("2026-09-28T09:30:00Z"))
    assert sm.streak_saved_since_last_live("alice") is None


def test_the_end_of_a_skipped_broadcast_counts_even_without_a_recorded_go_live(monitor):
    """No start is on record for alice, but the monitor saw a broadcast it
    skipped for a pause end: that end still closes it."""
    sm.handle_streak_event(_event("already_saved"))
    monitor.streamers["alice"].was_live = True
    monitor.missed_while_paused["alice"] = "03:00:00"

    monitor.process_state_changes({"alice": False, "bob": False})

    assert sm.streak_saved_since_last_live("alice") is None
    assert sm._streak_state["missed_end"]["alice"] == NOW


def test_a_broadcast_that_restarts_between_two_polls_is_a_new_go_live(monitor, toasts):
    """alice has been live since 03:00, and Twitch said the streak was kept
    at 04:00. She ends and starts again between two polls, so no poll sees
    her offline, but Helix's start changes: that is a new broadcast, and
    the save no longer covers her cards."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()

    _poll(monitor, _helix("2026-09-28T07:59:30Z"))

    assert sm._streak_state["last_live"]["alice"] == "2026-09-28T07:59:30Z"
    assert sm.streak_saved_since_last_live("alice") is None
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {}
    sm.handle_streak_event(_event("broke", detected_at=NOW))
    assert len(toasts) == 1 and "broke" in toasts[0][0]


@pytest.mark.parametrize("started_at", ["2026-09-28T03:00:00Z", None, "2099-01-01T00:00:00Z"])
def test_a_poll_that_finds_the_same_broadcast_changes_nothing(monitor, started_at):
    """Still live with the same Helix start, or with none the desktop can
    use (missing, or in the future): nothing is written, and the save seen
    during the broadcast stays."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    stream = {"user_login": "alice", "user_id": "1"}
    if started_at is not None:
        stream["started_at"] = started_at

    with patch.object(sm, "_write_streak_state_locked") as mwrite:
        _poll(monitor, {"data": [stream]})

    mwrite.assert_not_called()
    assert sm._streak_state["last_live"]["alice"] == "2026-09-28T03:00:00Z"
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT


@pytest.mark.parametrize("started_at, recorded", [
    ("2026-09-28T05:00:00Z", "2026-09-28T05:00:00Z"),
    (None, NOW),
])
def test_a_go_live_the_tab_logic_skips_is_still_recorded(monitor, started_at, recorded):
    """A late rescue ack can leave browser_opened set while alice is
    offline, so her next go-live skips the transition branch. It is still a
    new broadcast: its start (the poll time when Helix gives none) is
    recorded and ends the save."""
    sm.handle_streak_event(_event("already_saved"))
    monitor.streamers["alice"].browser_opened = True
    stream = {"user_login": "alice", "user_id": "1"}
    if started_at is not None:
        stream["started_at"] = started_at

    _poll(monitor, {"data": [stream]})

    assert sm._streak_state["last_live"]["alice"] == recorded
    assert sm.streak_saved_since_last_live("alice") is None


def test_while_still_paused_that_link_is_queued_and_offered(monitor):
    monitor.config.vod_fallback = True
    monitor.auto_paused = True
    monitor.streamers["alice"].was_live = True
    monitor.missed_while_paused["alice"] = "03:00:00"
    sm.record_stream_live("alice", "2026-09-28T03:00:00.000Z")
    sm.handle_streak_event(_event("already_saved", detected_at="2026-09-28T03:30:00.000Z"))

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        monitor.process_state_changes({"alice": False, "bob": False})
    mopen.assert_not_called()
    assert set(monitor.queued_vods) == {"alice"}

    monitor._offer_rescue_or_flush(set())
    assert [c["streamer"] for c in monitor.rescue_pending["candidates"]] == ["alice"]


@pytest.mark.parametrize("paused", [False, True])
def test_a_one_poll_gap_mid_broadcast_does_not_lose_the_save(monitor, paused):
    """alice drops out of one Helix answer mid-broadcast (a failed re-auth
    reads everyone as offline the same way), which looks like an end. With
    her tab open that end changes nothing. Skipped for a pause, it looks
    like the end of a missed broadcast and the save stops counting, until
    the next poll shows the same broadcast start: the end was not real, and
    the save counts again."""
    monitor.paused = paused
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    _poll(monitor, {"data": []})
    assert (sm.streak_saved_since_last_live("alice") is None) is paused

    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert "alice" not in sm._streak_state["last_offline"]
    assert "alice" not in sm._streak_state["missed_end"]


def test_an_end_missed_while_the_desktop_was_off_is_caught_on_the_first_poll(monitor, monkeypatch):
    """The old process saw alice go live at 03:00 and the save at 04:00,
    then stopped before her broadcast ended. The new process's first poll
    finds her offline: that broadcast is over, so the save stops counting.
    A save seen after that end stands, poll after poll."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    sm.handle_streak_event(_event("already_saved"))
    _relaunch(monitor)
    monitor.process_state_changes({"alice": False, "bob": False})

    assert sm.streak_saved_since_last_live("alice") is None
    assert sm._streak_state["last_offline"]["alice"] == NOW

    _set_clock(monkeypatch, NOW_EPOCH + HOUR)
    later = sm._epoch_to_iso(NOW_EPOCH + 30 * 60)
    sm.handle_streak_event(_event("already_saved", detected_at=later))
    monitor.process_state_changes({"alice": False, "bob": False})
    assert sm.streak_saved_since_last_live("alice") == later
    assert sm._streak_state["last_offline"]["alice"] == NOW


# ---------------------------------------------------------------------------
# already_saved reports: always evaluated, clamped, validated.
# ---------------------------------------------------------------------------


def test_the_same_page_after_a_go_live_is_recorded_again(toasts):
    """A go-live ended the first save. The same "already maintained your
    4-stream streak" seen again is recorded again; only the toast and the
    activity entry are once per streamer and count for the session."""
    sm.handle_streak_event(_event("already_saved", detected_at="2026-09-28T03:00:00.000Z"))
    sm.record_stream_live("alice", "2026-09-28T04:00:00.000Z")
    assert sm.streak_saved_since_last_live("alice") is None

    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event("already_saved", detected_at="2026-09-28T05:00:00.000Z"))

    assert sm.streak_saved_since_last_live("alice") == "2026-09-28T05:00:00.000Z"
    assert sm.ConfigRequestHandler.config_data["saved_streaks"] == {
        "alice": "2026-09-28T05:00:00.000Z",
    }
    assert len(toasts) == 1
    mlog.assert_not_called()


def test_a_resent_older_detection_does_not_move_the_save_back(toasts):
    sm.handle_streak_event(_event("already_saved", detected_at="2026-09-28T05:00:00.000Z"))
    sm.handle_streak_event(_event("already_saved", detected_at=SAVED_AT))
    assert sm.streak_saved_since_last_live("alice") == "2026-09-28T05:00:00.000Z"
    assert len(toasts) == 1


def test_a_late_report_from_before_the_last_go_live_is_ignored(toasts):
    """The extension re-sends a detection it could not deliver, with its
    original time. alice went live at 05:00, after it: nothing to record."""
    sm.record_stream_live("alice", "2026-09-28T05:00:00.000Z")

    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event("already_saved"))

    assert sm._streak_state["saved"] == {}
    assert sm.saved_streaks_for_config() == {}
    assert toasts == []
    (call,) = mlog.call_args_list
    assert call.args[0] == "streak_event_ignored"
    assert call.kwargs["reason"] == "superseded_by_live"
    assert call.kwargs["superseded_at"] == "2026-09-28T05:00:00.000Z"


def test_a_late_report_from_a_missed_broadcast_that_has_since_ended_is_ignored(monitor, toasts):
    """Seen at 04:00 while alice's 03:00 broadcast ran (skipped for a
    pause), delivered only after it ended: it cannot vouch for that
    broadcast."""
    monitor.paused = True
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    _poll(monitor, {"data": []})

    with patch("stream_monitor_tray.log_activity") as mlog:
        sm.handle_streak_event(_event("already_saved"))

    assert sm._streak_state["saved"] == {}
    assert toasts == []
    assert mlog.call_args.kwargs["reason"] == "superseded_by_live"
    assert mlog.call_args.kwargs["superseded_at"] == NOW


def test_a_late_report_from_a_broadcast_stream_monitor_had_open_counts(monitor, toasts):
    """The same late report for a broadcast that had a tab open: only a
    go-live after the detection supersedes it, so it is recorded."""
    _poll(monitor, _helix("2026-09-28T03:00:00Z"))
    _poll(monitor, {"data": []})

    sm.handle_streak_event(_event("already_saved"))

    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert len(toasts) == 1 and "already safe" in toasts[0][0]


def test_a_detected_at_in_the_future_is_clamped_to_now(monkeypatch):
    sm.handle_streak_event(_event("already_saved", detected_at="2099-01-01T00:00:00.000Z"))
    assert sm.streak_saved_since_last_live("alice") == NOW

    # So the next broadcast ends it, like any other save.
    _set_clock(monkeypatch, NOW_EPOCH + HOUR)
    sm.record_stream_live("alice", sm._epoch_to_iso(NOW_EPOCH + 30 * 60))
    assert sm.streak_saved_since_last_live("alice") is None


@pytest.mark.parametrize("started_at", ["2099-01-01T00:00:00Z", "0001-01-01T00:00:00Z", "", None])
def test_a_missing_or_implausible_broadcast_start_becomes_now(started_at):
    sm.record_stream_live("alice", started_at)
    assert sm._streak_state["last_live"]["alice"] == NOW


def test_a_save_ahead_of_a_stepped_back_clock_is_distrusted(monkeypatch):
    """The clock was stepped back an hour after the save was recorded, so
    its real time is unknown: it stops counting, and a restart drops it. A
    step of a minute or two changes nothing."""
    sm.handle_streak_event(_event("already_saved", detected_at=NOW))
    _set_clock(monkeypatch, NOW_EPOCH - 120)
    assert sm.streak_saved_since_last_live("alice") == NOW

    _set_clock(monkeypatch, NOW_EPOCH - HOUR)
    assert sm.streak_saved_since_last_live("alice") is None
    sm.load_streak_state(now_epoch=NOW_EPOCH - HOUR)
    assert sm._streak_state["saved"] == {}


def test_a_distrusted_save_does_not_block_a_new_report(monkeypatch):
    # Its later date must not win the keep-the-later comparison.
    sm.handle_streak_event(_event("already_saved", detected_at=NOW))
    _set_clock(monkeypatch, NOW_EPOCH - HOUR)
    fresh = sm._epoch_to_iso(NOW_EPOCH - HOUR - 60)

    sm.handle_streak_event(_event("already_saved", detected_at=fresh))

    assert sm.streak_saved_since_last_live("alice") == fresh


def test_already_saved_needs_a_twitch_login():
    for bad in ("not a login!", "x" * 65, "../alice", "alice?sm=1"):
        with pytest.raises(ValueError):
            sm.handle_streak_event(_event("already_saved", streamer=bad))
    assert sm._streak_state["saved"] == {}
    assert sm.record_streak_saved("Not Alice", 4, SAVED_AT).outcome == "bad_login"

    # A login in another case is still that login.
    sm.handle_streak_event(_event("already_saved", streamer="Alice"))
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT


def test_bad_already_saved_payloads_are_rejected():
    with pytest.raises(ValueError):
        sm.handle_streak_event(_event("already_saved", count="four"))
    with pytest.raises(ValueError):
        sm.handle_streak_event({"status": "kept", "streamer": "alice", "count": 1})


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def test_state_survives_a_restart(tmp_config_dir):
    sm.record_streak_saved("alice", 4, SAVED_AT)
    sm.record_stream_live("bob", "2026-09-28T02:00:00.000Z")
    sm.record_stream_offline("bob")
    assert (tmp_config_dir / "streak_state.json").exists()

    sm._streak_state = sm._empty_streak_state()  # forget everything
    sm.load_streak_state(now_epoch=NOW_EPOCH)

    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert sm._streak_state["last_live"]["bob"] == "2026-09-28T02:00:00.000Z"
    assert sm._streak_state["last_offline"]["bob"] == NOW
    assert sm._streak_state["missed_end"]["bob"] == NOW


def test_corrupt_or_ancient_state_is_dropped(tmp_config_dir):
    path = tmp_config_dir / "streak_state.json"
    path.write_text("{not json", encoding="utf-8")
    sm.load_streak_state(now_epoch=NOW_EPOCH)
    assert sm.saved_streaks_for_config() == {}

    path.write_text(
        '{"saved": {"old": {"at": "2020-01-01T00:00:00.000Z", "count": 3}, '
        '"BAD NAME": {"at": "%s", "count": 1}}, "last_live": 5}' % SAVED_AT,
        encoding="utf-8",
    )
    sm.load_streak_state(now_epoch=NOW_EPOCH)
    assert sm.saved_streaks_for_config() == {}


def test_broadcast_times_older_than_the_cutoff_are_pruned(tmp_config_dir):
    old = sm._epoch_to_iso(NOW_EPOCH - sm.STREAK_STATE_MAX_AGE_SECONDS - HOUR)
    recent = "2026-09-27T20:00:00.000Z"
    (tmp_config_dir / "streak_state.json").write_text(json.dumps({
        "saved": {},
        "last_live": {"gone": old, "bob": recent, "early": "1901-01-01T00:00:00"},
        "last_offline": {"gone": old, "bob": recent},
        "missed_end": {"gone": old, "bob": recent, "BAD NAME": recent},
    }), encoding="utf-8")

    sm.load_streak_state(now_epoch=NOW_EPOCH)

    assert sm._streak_state["last_live"] == {"bob": recent}
    assert sm._streak_state["last_offline"] == {"bob": recent}
    assert sm._streak_state["missed_end"] == {"bob": recent}


# ---------------------------------------------------------------------------
# The VOD queue and /config.
# ---------------------------------------------------------------------------


def test_the_rescue_offer_drops_saved_links_from_the_queue(monitor):
    monitor.queued_vods = {
        "alice": {"url": SAVE_URL.format("alice"), "ended_at": "2026-09-28T01:00:00.000Z"},
        "bob": {"url": SAVE_URL.format("bob"), "ended_at": "2026-09-28T02:00:00.000Z"},
    }
    sm.record_streak_saved("alice", 4, SAVED_AT)

    with patch("stream_monitor_tray.log_activity") as mlog:
        monitor._offer_rescue_or_flush(set())

    assert [c["streamer"] for c in monitor.rescue_pending["candidates"]] == ["bob"]
    assert set(monitor.queued_vods) == {"bob"}
    assert set(sm.ConfigRequestHandler.config_data["queued_vods"]) == {"bob"}
    skipped = [c.kwargs for c in mlog.call_args_list if c.args[0] == "vod_skipped"]
    assert skipped == [{"streamer": "alice", "reason": "streak_already_saved", "saved_at": SAVED_AT}]


def test_a_queue_of_only_saved_links_is_emptied_without_an_offer(monitor):
    monitor.queued_vods = {"alice": {"url": SAVE_URL.format("alice"), "ended_at": "x"}}
    sm.record_streak_saved("alice", 4, SAVED_AT)

    monitor._offer_rescue_or_flush(set())

    assert monitor.rescue_pending is None
    assert monitor.queued_vods == {}
    assert sm.ConfigRequestHandler.config_data["queued_vods"] == {}


def test_flush_opens_only_the_unsaved_link(monitor):
    monitor.queued_vods = {
        "alice": {"url": SAVE_URL.format("alice"), "ended_at": "2026-09-28T01:00:00.000Z"},
        "bob": {"url": SAVE_URL.format("bob"), "ended_at": "2026-09-28T02:00:00.000Z"},
    }
    sm.record_streak_saved("alice", 4, SAVED_AT)

    with patch("stream_monitor_tray.webbrowser.open") as mopen, \
         patch("stream_monitor_tray.log_activity") as mlog:
        assert monitor._flush_queued_vods(reason="test") == 1
        assert monitor.wait_for_pending_opens(timeout=5)

    mopen.assert_called_once_with(SAVE_URL.format("bob"))
    assert monitor.queued_vods == {}
    skipped = [c for c in mlog.call_args_list if c.args[0] == "vod_skipped"]
    assert len(skipped) == 1 and skipped[0].kwargs["streamer"] == "alice"


def test_flush_with_only_saved_streaks_opens_nothing(monitor):
    monitor.queued_vods = {"alice": {"url": SAVE_URL.format("alice"), "ended_at": "x"}}
    sm.record_streak_saved("alice", 4, SAVED_AT)

    with patch("stream_monitor_tray.webbrowser.open") as mopen:
        assert monitor._flush_queued_vods(reason="test") == 0

    mopen.assert_not_called()
    assert monitor.queued_vods == {}
    assert monitor._notify_calls == []


def test_saved_streaks_are_only_published_under_the_state_lock(monitor, monkeypatch):
    """A snapshot computed and assigned outside the lock could land after a
    newer one and hide a save from /config for a whole poll."""
    class LockCheckingConfig(dict):
        def __setitem__(self, key, value):
            if key == "saved_streaks":
                assert sm._streak_state_lock.locked(), "saved_streaks published outside the lock"
            super().__setitem__(key, value)

    config = LockCheckingConfig()
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", config)

    sm.handle_streak_event(_event("already_saved"))
    sm.set_polled_streamers(["alice", "bob"])
    sm.load_streak_state(now_epoch=NOW_EPOCH)
    _poll(monitor, _helix("2026-09-28T05:00:00Z"))
    _poll(monitor, {"data": []})
    sm.publish_saved_streaks()

    assert config["saved_streaks"] == {}


def test_start_polls_the_listed_streamers(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "_get_oauth_token", lambda: True)
    monkeypatch.setattr(monitor, "_monitor_loop", lambda: None)
    monitor.thread = None
    monitor.config.streamers = ["Alice", "carol"]

    assert monitor.start() is True
    assert sm._polled_streamers == frozenset({"alice", "carol"})


# ---------------------------------------------------------------------------
# 1.12.0: the card verdict (plan 3.5.2, DESIGN 12.5 C2). Every row through
# handle_streak_event, with the monitor's runs of watching read through the
# watch-start provider. The save is alice's 4-stream streak, kept at 04:00.
# ---------------------------------------------------------------------------


class _Items:
    """A stand-in for the monitor inbox: records each submitted item and
    reports it as created."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, payload):
        self.calls.append((kind, payload))
        item = sm._InboxItem(kind, payload)
        item.result = True
        item.done.set()
        return item


@pytest.fixture
def items(monkeypatch):
    recorder = _Items()
    monkeypatch.setattr(sm, "_monitor_submitter", recorder)
    return recorder


def _watching_since(monkeypatch, starts):
    monkeypatch.setattr(sm, "_watch_start_provider", lambda name: starts.get(name))


def _card(status="broke", streamer="alice", count=4, detected_at="2026-09-28T07:00:00.000Z", **extra):
    payload = {"status": status, "streamer": streamer, "count": count, "detected_at": detected_at,
               "source": "bell"}
    payload.update(extra)
    return payload


def _auto_save(on=True):
    sm.ConfigRequestHandler.config_data["auto_save_streaks"] = on


def test_as03_row_1_a_card_posted_after_the_save_is_fresh_and_ends_it(toasts):
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()
    with patch("stream_monitor_tray.log_activity") as mlog:
        # "1 hour ago" at 07:00: posted at 05:00 at the earliest, after the save.
        answer = sm.handle_streak_event(_card(card_age_s=HOUR, card_age_unit_s=HOUR))

    assert answer == {"verdict": "fresh", "item": False}
    assert sm.streak_saved_since_last_live("alice") is None
    ended = [c.kwargs for c in mlog.call_args_list if c.args[0] == "streak_save_ended"]
    assert ended == [{"streamer": "alice", "reason": "newer_card", "saved_at": SAVED_AT,
                      "saved_count": 4, "card_status": "broke", "card_count": 4}]
    assert len(toasts) == 1


def test_as03_am25_a_card_that_ends_a_save_forgets_the_other_keys_but_its_own(toasts):
    """Plan 3.5.1 step 5.4 (A25): the streamer's other keys go, so a real
    break seen before the save is announced again; the card that ended the
    save keeps its key, so reading it again is a duplicate."""
    danger = ("in_danger", "alice", 5)
    sm.handle_streak_event(_card(status="in_danger", count=5, deadline_hours=10))
    sm.handle_streak_event(_event("already_saved"))  # a 4-stream save does not cover it
    assert danger in sm._streak_event_seen
    toasts.clear()

    answer = sm.handle_streak_event(_card(card_age_s=HOUR, card_age_unit_s=HOUR))

    assert answer["verdict"] == "fresh"
    assert danger not in sm._streak_event_seen and danger not in sm._streak_event_seen_at
    assert ("broke", "alice", 4) in sm._streak_event_seen
    again = sm.handle_streak_event(_card(card_age_s=HOUR, card_age_unit_s=HOUR))
    assert again["verdict"] == "duplicate"
    assert len(toasts) == 1


def test_as03_row_2_a_known_age_card_from_before_the_save_is_stale_even_with_a_higher_count(toasts):
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()
    with patch("stream_monitor_tray.log_activity") as mlog:
        # "5 hours ago" at 07:00: posted by 03:00, before the save. The age
        # decides before the count does.
        answer = sm.handle_streak_event(_card(count=9, card_age_s=5 * HOUR, card_age_unit_s=HOUR))

    assert answer == {"verdict": "stale", "item": False}
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    (call,) = mlog.call_args_list
    assert call.args[0] == "streak_event_ignored"
    assert (call.kwargs["reason"], call.kwargs["verdict"], call.kwargs["saved_at"]) == (
        "already_saved", "stale", SAVED_AT)
    assert toasts == []


def test_as03_row_3_a_higher_count_of_unknown_age_is_fresh_and_ends_the_save(monkeypatch, toasts):
    _watching_since(monkeypatch, {"alice": SAVED_EPOCH - HOUR})
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()
    with patch("stream_monitor_tray.log_activity") as mlog:
        answer = sm.handle_streak_event(_card(count=5))

    assert answer["verdict"] == "fresh"
    ended = [c.kwargs for c in mlog.call_args_list if c.args[0] == "streak_save_ended"]
    assert [e["reason"] for e in ended] == ["count_grew"]
    assert toasts and "5-stream" in toasts[0][0]


def test_as03_o02_row_4_the_owners_3_stream_card_against_a_kept_4_stream_streak_is_stale(monkeypatch, toasts):
    _watching_since(monkeypatch, {})
    sm.handle_streak_event(_event("already_saved", count=4))
    toasts.clear()
    answer = sm.handle_streak_event(_card(count=3))
    assert answer == {"verdict": "stale", "item": False}
    assert toasts == []
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT


def test_as03_row_5_an_equal_count_with_unbroken_watching_is_stale(monkeypatch, items, toasts):
    _auto_save()
    # The desktop has polled alice without a gap since before the save: a
    # broadcast after it would have been seen and would have ended it.
    _watching_since(monkeypatch, {"alice": SAVED_EPOCH - HOUR})
    sm.handle_streak_event(_event("already_saved"))
    toasts.clear()

    answer = sm.handle_streak_event(_card())
    # The same for a link event, which has no count.
    link = sm.handle_streak_event(_card(count=None, source="link"))

    assert answer == link == {"verdict": "stale", "item": False}
    assert items.calls == [("item_done", {"login": "alice", "reason": "already_saved", "saved_at": SAVED_AT})]
    assert toasts == []


def test_o01_row_6_an_equal_count_after_a_gap_in_watching_is_a_quiet_check(monkeypatch, items, toasts):
    _auto_save()
    # Watching restarted after the save (a sleep, failed polls): a whole
    # broadcast may have been missed, so only the page can tell (O1 (c)).
    _watching_since(monkeypatch, {"alice": SAVED_EPOCH + HOUR})
    sm.handle_streak_event(_event("already_saved"))
    items.calls.clear()
    toasts.clear()

    with patch("stream_monitor_tray.log_activity") as mlog:
        answer = sm.handle_streak_event(_card())
        again = sm.handle_streak_event(_card(detected_at="2026-09-28T07:30:00.000Z"))

    assert answer == {"verdict": "verify", "item": True}
    assert again == {"verdict": "verify", "item": False}  # one check per card
    ((kind, payload),) = items.calls
    assert kind == "streak_item" and payload["item"]["verify"] is True
    assert payload["item"]["origin"] == "card" and payload["merge_only"] is False
    assert [c.args[0] for c in mlog.call_args_list] == ["streak_event_ignored"] * 2
    assert mlog.call_args_list[0].kwargs["verdict"] == "verify"
    assert toasts == []
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT  # a check ends nothing


def test_am04_row_7_an_age_lapsed_save_does_not_quiet_an_equal_count_card(monkeypatch, toasts):
    """Nobody polls driveyabatty, so the save lapses after 24 hours. It still
    feeds the verdict (a lower count stays stale), but an equal count of
    unknown age is fresh (row 7): the save no longer vouches for it."""
    _watching_since(monkeypatch, {"driveyabatty": SAVED_EPOCH - HOUR})
    sm.handle_streak_event(_event("already_saved", streamer="driveyabatty"))
    toasts.clear()
    _set_clock(monkeypatch, SAVED_EPOCH + DAY + HOUR)
    later = sm._epoch_to_iso(sm._streak_clock())

    lower = sm.handle_streak_event(_card(streamer="driveyabatty", count=3, detected_at=later))
    equal = sm.handle_streak_event(_card(streamer="driveyabatty", detected_at=later))

    assert lower["verdict"] == "stale"
    assert equal["verdict"] == "fresh"
    assert [title for title, _ in toasts] == ["driveyabatty: 4-stream streak broke"]
    # Row 7 does not end the lapsed save; rows 1 and 3 would.
    assert "driveyabatty" in sm._streak_state["saved"]


def test_as03_am04_a_card_read_while_the_save_counts_is_judged_afresh_once_it_lapses(monkeypatch, toasts):
    """Plan 3.5.1 step 5.3: a stale card adds no dedup key. Read while the
    save counts (row 5), the same card read after the save lapsed (row 7)
    is fresh and announced once, not a duplicate of the stale read."""
    _watching_since(monkeypatch, {"driveyabatty": SAVED_EPOCH - HOUR})
    sm.handle_streak_event(_event("already_saved", streamer="driveyabatty"))
    toasts.clear()

    stale = sm.handle_streak_event(_card(streamer="driveyabatty"))
    assert stale["verdict"] == "stale"
    assert ("broke", "driveyabatty", 4) not in sm._streak_event_seen

    # Less than 48 hours after the stale read, so no prune hides a key.
    _set_clock(monkeypatch, SAVED_EPOCH + DAY + HOUR)
    later = sm._epoch_to_iso(sm._streak_clock())
    again = sm.handle_streak_event(_card(streamer="driveyabatty", detected_at=later))

    assert again["verdict"] == "fresh"
    assert [title for title, _ in toasts] == ["driveyabatty: 4-stream streak broke"]


def test_as03_row_0_a_card_older_than_eight_days_is_stale_with_or_without_a_save(toasts):
    answer = sm.handle_streak_event(_card(streamer="bob", card_age_s=691201))
    assert answer == {"verdict": "stale", "item": False}
    assert toasts == []


def test_as03_missed_item_coverage_during_and_after(monitor):
    """A missed broadcast's save-streak link (ended at 06:00) is covered only
    by a save seen after the broadcast ended (D7 (b)): one seen during a
    broadcast Stream Monitor had open does not cover that broadcast's end."""
    ended = "2026-09-28T06:00:00.000Z"
    link = {"url": SAVE_URL.format("alice"), "ended_at": ended,
            "deadline_at": sm._epoch_to_iso(sm._iso_to_epoch(ended) + DAY), "origin": "offline_edge"}

    monitor.queued_vods = {"alice": dict(link)}
    sm.record_streak_saved("alice", 4, "2026-09-28T05:30:00.000Z")
    monitor._offer_rescue_or_flush(set())
    assert [c["streamer"] for c in monitor.rescue_pending["candidates"]] == ["alice"]

    monitor.rescue_pending = None
    monitor.queued_vods = {"alice": dict(link)}
    sm.record_streak_saved("alice", 4, "2026-09-28T07:00:00.000Z")
    with patch("stream_monitor_tray.log_activity") as mlog:
        monitor._offer_rescue_or_flush(set())
    assert monitor.rescue_pending is None and monitor.queued_vods == {}
    skipped = [c.kwargs for c in mlog.call_args_list if c.args[0] == "vod_skipped"]
    assert skipped == [{"streamer": "alice", "reason": "streak_already_saved",
                        "saved_at": "2026-09-28T07:00:00.000Z"}]


# ---------------------------------------------------------------------------
# 1.12.0: card identity and the session dedup (plan 3.5.3, AUDIT P6)
# ---------------------------------------------------------------------------


def _at(hhmm):
    return sm._iso_to_epoch(f"2026-09-28T{hhmm}:00.000Z")


def _read_at(monkeypatch, hhmm, **card):
    """The extension reads a card at hh:mm (the desktop's clock too)."""
    _set_clock(monkeypatch, _at(hhmm))
    return sm.handle_streak_event(_card(detected_at=sm._epoch_to_iso(_at(hhmm)), **card))


def test_ap06_the_same_card_across_a_label_rollover_is_one_toast_and_one_item(monkeypatch, items, toasts):
    """A card posted at 10:30 reads "1 hour ago" at 11:40 (earliest posting
    09:40) and again at 12:20 (earliest 10:20). A posting-hour key would call
    them two cards; their possible posting times overlap, so they are one."""
    _auto_save()
    first = _read_at(monkeypatch, "11:40", streamer="bob", card_age_s=HOUR, card_age_unit_s=HOUR)
    second = _read_at(monkeypatch, "12:20", streamer="bob", card_age_s=HOUR, card_age_unit_s=HOUR)

    assert first == {"verdict": "fresh", "item": True}
    assert second == {"verdict": "duplicate", "item": False}
    assert len(toasts) == 1
    assert len(items.calls) == 1


def test_ap06_a_reissued_in_danger_card_is_a_new_card(monkeypatch, items, toasts):
    _auto_save()
    first = _read_at(monkeypatch, "10:00", streamer="bob", status="in_danger", count=6,
                     deadline_hours=3, card_age_s=0)
    again = _read_at(monkeypatch, "15:00", streamer="bob", status="in_danger", count=6,
                     deadline_hours=3, card_age_s=0)

    assert first["verdict"] == again["verdict"] == "fresh"
    assert len(toasts) == 2
    assert len(items.calls) == 2


def test_ap06_the_48_hour_prune_counts_from_the_last_sighting(monkeypatch, toasts):
    t0 = _at("08:00")
    for hours, expected in ((0, "fresh"), (47, "duplicate"), (94, "duplicate"), (143, "fresh")):
        _set_clock(monkeypatch, t0 + hours * HOUR)
        answer = sm.handle_streak_event(_card(streamer="bob", detected_at=sm._epoch_to_iso(t0 + hours * HOUR)))
        assert answer["verdict"] == expected, hours
    assert len(toasts) == 2
    assert ("broke", "bob", 4) in sm._streak_event_seen_at


@pytest.mark.parametrize("card", [{}, {"count": None, "source": "link"}], ids=["card", "link"])
def test_ap06_an_item_record_lasts_while_its_card_is_still_being_read(monkeypatch, items, toasts, card):
    """An item record has no clock of its own: it goes with its card's seen
    record, whose 48 hours count from the last sighting. A card of unknown
    age (or a link event) re-read every few hours never makes a second
    item, past 48 hours after the first one too."""
    _auto_save()
    t0 = _at("08:00")
    answers = []
    for hours in (0, 6, 12, 18, 24, 30, 36, 42, 47, 49, 54):
        _set_clock(monkeypatch, t0 + hours * HOUR)
        answers.append(sm.handle_streak_event(
            _card(streamer="bob", detected_at=sm._epoch_to_iso(t0 + hours * HOUR), **card)))

    assert answers[0] == {"verdict": "fresh", "item": True}
    assert all(a == {"verdict": "duplicate", "item": False} for a in answers[1:]), answers
    assert len(items.calls) == 1
    assert len(toasts) == 1


def test_ap06_am20_an_already_saved_after_a_link_event_keeps_the_link_key(items, toasts):
    """A20: a link key has no count, so a save never covers it; an int key
    at or below the saved count is forgotten. The v1.11.2 filter raised
    TypeError on the link key and the POST answered 500."""
    _auto_save()
    link = sm.handle_streak_event(_card(count=None, source="link", login_verified=True))
    assert link["verdict"] == "fresh"
    sm.handle_streak_event(_card(count=3, detected_at="2026-09-28T06:00:00.000Z"))
    link_key, low_key = ("broke", "alice", None), ("broke", "alice", 3)
    assert link_key in sm._streak_event_seen and low_key in sm._streak_event_seen
    assert link_key in sm._streak_item_keys
    items.calls.clear()

    answer = sm.handle_streak_event(_event("already_saved"))

    assert answer["verdict"] == "saved"
    assert sm.streak_saved_since_last_live("alice") == SAVED_AT
    assert link_key in sm._streak_event_seen and link_key in sm._streak_event_seen_at
    assert link_key in sm._streak_item_keys
    assert low_key not in sm._streak_event_seen and low_key not in sm._streak_event_seen_at
    assert items.calls == [("item_done", {"login": "alice", "reason": "already_saved", "saved_at": SAVED_AT})]


def test_ap06_keys_are_dropped_at_a_go_live(toasts):
    sm.handle_streak_event(_card(streamer="bob"))
    assert sm.handle_streak_event(_card(streamer="bob"))["verdict"] == "duplicate"

    sm.record_stream_live("bob", "2026-09-28T07:30:00.000Z")

    assert ("broke", "bob", 4) not in sm._streak_event_seen
    assert sm.handle_streak_event(_card(streamer="bob"))["verdict"] == "fresh"
    assert len(toasts) == 2


def test_ap06_duplicate_makes_one_item_after_saves_are_turned_on(items, toasts):
    _auto_save(False)
    assert sm.handle_streak_event(_card(streamer="bob")) == {"verdict": "fresh", "item": False}
    _auto_save(True)
    assert sm.handle_streak_event(_card(streamer="bob")) == {"verdict": "duplicate", "item": True}
    assert sm.handle_streak_event(_card(streamer="bob")) == {"verdict": "duplicate", "item": False}

    assert len(items.calls) == 1
    assert len(toasts) == 1


def test_ap06_a_card_whose_item_is_done_makes_no_second(monkeypatch, items):
    """The item made at 11:40 has had its turn. Read again at 12:20, across
    its label's rollover, the card is the same card: no second item."""
    _auto_save()
    _read_at(monkeypatch, "11:40", streamer="bob", card_age_s=HOUR, card_age_unit_s=HOUR)
    items.calls.clear()  # the item is done; the desktop still knows the card
    answer = _read_at(monkeypatch, "12:20", streamer="bob", card_age_s=HOUR, card_age_unit_s=HOUR)
    assert answer == {"verdict": "duplicate", "item": False}
    assert items.calls == []


def test_ap06_a_shorter_deadline_for_the_same_key_updates_the_item(monitor, monkeypatch):
    """An in-danger card's deadline is escalated (the same card, "ends in 8
    hours" then "ends in 5 hours"): the pending item takes the earlier
    deadline. Once the item is done, the same escalation makes nothing."""
    _auto_save()
    monitor.config.auto_save_streaks = True
    monitor.paused = True  # no offer: nothing to open

    submits = []

    def submit(kind, payload):
        submits.append((kind, payload))
        item = monitor.submit(kind, payload)
        monitor._drain_inbox()
        return item

    monkeypatch.setattr(sm, "_monitor_submitter", submit)
    posted = _at("09:59")
    card = dict(streamer="zed", status="in_danger", count=7)
    key = ("in_danger", "zed", 7)

    first = _read_at(monkeypatch, "10:10", deadline_hours=8, card_age_s=600, **card)
    assert first == {"verdict": "fresh", "item": True}
    assert sm._iso_to_epoch(monitor.queued_vods["zed"]["deadline_at"]) == posted + 8 * HOUR

    second = _read_at(monkeypatch, "10:20", deadline_hours=5, card_age_s=1200, **card)
    assert second == {"verdict": "duplicate", "item": True}
    assert sm._iso_to_epoch(monitor.queued_vods["zed"]["deadline_at"]) == posted + 5 * HOUR
    # The item record takes the new deadline (step 5.5), so the same
    # escalated card read again is not re-submitted.
    assert sm._streak_item_keys[key]["deadline_at"] == posted + 5 * HOUR
    submits.clear()
    repeat = _read_at(monkeypatch, "10:22", deadline_hours=5, card_age_s=1320, **card)
    assert repeat == {"verdict": "duplicate", "item": False}
    assert submits == []

    # An hour earlier is within the slack (in-danger hours are whole hours),
    # so it is label drift: nothing is submitted and nothing changes.
    drift = _read_at(monkeypatch, "10:25", deadline_hours=4, card_age_s=1500, **card)
    assert drift == {"verdict": "duplicate", "item": False}
    assert submits == []
    assert sm._streak_item_keys[key]["deadline_at"] == posted + 5 * HOUR
    assert sm._iso_to_epoch(monitor.queued_vods["zed"]["deadline_at"]) == posted + 5 * HOUR

    monitor.queued_vods.clear()  # the item had its turn
    third = _read_at(monkeypatch, "10:30", deadline_hours=2, card_age_s=1800, **card)
    assert third == {"verdict": "duplicate", "item": False}
    assert monitor.queued_vods == {} and monitor.held_save_items == {}


# ---------------------------------------------------------------------------
# 1.12.0: housekeeping (DESIGN 12.5 C5)
# ---------------------------------------------------------------------------


def test_as03_dc5_prune_on_every_write(tmp_config_dir):
    old = sm._epoch_to_iso(NOW_EPOCH - sm.STREAK_STATE_MAX_AGE_SECONDS - HOUR)
    sm._streak_state["saved"]["ancient"] = {"at": old, "count": 2}
    sm._streak_state["last_live"]["gone"] = old
    sm._streak_state["last_offline"]["gone"] = old
    sm._streak_state["missed_end"]["gone"] = old
    sm._streak_state["last_live"]["bob"] = "2026-09-27T20:00:00.000Z"

    sm.record_streak_saved("alice", 4, SAVED_AT)  # any write

    for key in ("last_live", "last_offline", "missed_end"):
        assert "gone" not in sm._streak_state[key]
    assert "ancient" not in sm._streak_state["saved"]
    assert sm._streak_state["last_live"]["bob"] == "2026-09-27T20:00:00.000Z"
    on_disk = json.loads((tmp_config_dir / "streak_state.json").read_text(encoding="utf-8"))
    assert set(on_disk["saved"]) == {"alice"}
    assert on_disk["last_live"] == {"bob": "2026-09-27T20:00:00.000Z"}
