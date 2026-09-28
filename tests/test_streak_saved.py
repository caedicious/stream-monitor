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
