"""Slot mode scheduler (1.12.0).

Slot mode keeps at most K + C stream tabs open: K Keep Open slots for the
highest-ranked live Keep Open streamers, and C rotating slots that give every
other live stream, and every save item, one turn of M minutes, then rest on
the highest-ranked live stream. The desktop decides and publishes the plan
in /config; the browser extension that executes it opens and closes tabs and
reports back.

SlotScheduler.tick(inputs) holds every rule of DESIGN section 7.3 (steps S0
to S13) with the build plan's amendments A1 to A3, A9, A13, A24 and A35. It
is pure: no I/O, no threads, no clock of its own, no logging, no import of
stream_monitor_tray. The tray builds the inputs (plan 3.13), logs the events
the tick returns in order, writes slot_state.json when `persist` is set, and
publishes the plan.

Epochs are float seconds internally; the published plan carries them floored
to ints, and event fields carry them as ISO strings from
streak_saves.epoch_to_iso.
"""
import json
import math
import re
from dataclasses import dataclass
from typing import Optional

import streak_saves

SLOT_PLAN_VERSION = 1
SLOT_MAX_TOTAL = 3
SLOT_STATE_MAX_AGE_SECONDS = 600
SLOT_OPEN_GIVE_UP_SECONDS = 600
SLOT_OFFLINE_STRIKES = 2
SLOT_REMOVAL_MEMORY_SECONDS = 900
SLOT_OUTAGE_NOTIFY_SECONDS = 300
SLOT_LIVE_STALE_SECONDS = 300
SLOT_EXECUTOR_ALIVE_SECONDS = 150
SLOT_STARTUP_GRACE_SECONDS = 90
SLOT_QUEUE_PUBLISH_MAX = 10
SLOT_TOOLTIP_MAX = 110

TOOLTIP_ABSENT = "Slot mode: browser extension not reporting"
TOOLTIP_NONE_EVER = "Slot mode: needs extension 1.12, opening normally"
TOOLTIP_BUSY = "Slot mode: paused in the browser"

CLOSE_REASONS = ("turn_over", "offline", "displaced", "preempted", "idle_swap",
                 "unlisted", "save_done", "unplanned")
GONE_REASONS = ("user_closed", "navigated", "raid", "window_closed",
                "already_saved", "not_eligible")
STATES = ("off", "none_ever", "waiting", "alive", "absent")

_ACTIVE_STATES = ("waiting", "alive", "absent")
_HOUR = 3600
# A saved slot_state.json dated this far ahead of now still counts as fresh
# (a small clock correction between two runs).
_STATE_FUTURE_TOLERANCE_SECONDS = 60
_GONE_MEMORY_SECONDS = 24 * _HOUR
_STATE_LOGIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_INSTANCE_RE = re.compile(r"^[0-9a-f]{8}$")
_SLOT_ID_RE = re.compile(r"^(keep|cycle)-([1-9])$")
_SLOT_FIELDS = ("id", "kind", "streamer", "entry", "url", "session", "assigned_at",
                "confirmed_at", "first_confirmed_at", "turn_ends_at", "mode",
                "hold_until", "waiting", "lent")
_CLOCK_FIELDS = ("confirmed_at", "first_confirmed_at", "turn_ends_at", "hold_until")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _login_ok(value) -> bool:
    return isinstance(value, str) and bool(_STATE_LOGIN_RE.match(value))


def _floor(value):
    return None if value is None else int(math.floor(value))


def _iso(value):
    return None if value is None else streak_saves.epoch_to_iso(value)


def _live_url(login: str) -> str:
    return f"https://www.twitch.tv/{login}?sm=1"


def _slot_url(login: str, entry: str) -> str:
    return _live_url(login) if entry == "live" else streak_saves.save_url(login)


def _layout(k: int, c: int) -> list:
    return [f"keep-{i}" for i in range(1, k + 1)] + [f"cycle-{i}" for i in range(1, c + 1)]


def _empty_slot(slot_id: str) -> dict:
    return {
        "id": slot_id,
        "kind": "keep" if slot_id.startswith("keep-") else "cycle",
        "streamer": None, "entry": None, "url": None, "session": None,
        "assigned_at": None, "confirmed_at": None, "first_confirmed_at": None,
        "turn_ends_at": None, "mode": None, "hold_until": None, "waiting": None,
        "lent": False,
    }


def _key_browser(key: str) -> str:
    return key.rsplit("-", 1)[0]


def _executor_key_ok(key, browser) -> bool:
    """Only "<browser>-<instance>" keys execute; a bare browser key never
    does (A23)."""
    if not isinstance(key, str) or not isinstance(browser, str) or not browser:
        return False
    prefix = browser + "-"
    return key.startswith(prefix) and bool(_INSTANCE_RE.match(key[len(prefix):]))


def _streamers_of(rep) -> frozenset:
    if not isinstance(rep, dict):
        return frozenset()
    names = rep.get("streamers")
    if not isinstance(names, (set, frozenset, list, tuple)):
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str))


@dataclass
class TickResult:
    plan: Optional[dict]
    events: list
    opens: list
    state: str
    notices: list
    persist: bool
    absorbed: bool
    off_transition: Optional[dict]
    tooltip: Optional[str]


class _Tick:
    """One tick's inputs (plan 3.13) and its scratch data."""

    def __init__(self, inp: dict):
        inp = inp if isinstance(inp, dict) else {}
        self.now = float(inp.get("now") or 0.0)
        self.mono = float(inp.get("mono") or 0.0)
        self.authoritative = bool(inp.get("authoritative"))
        poll = inp.get("poll")
        self.poll = poll if isinstance(poll, dict) else None
        live_as_of = inp.get("live_as_of")
        self.live_as_of = float(live_as_of) if _is_num(live_as_of) else None
        interval = inp.get("check_interval")
        self.check_interval = int(interval) if _is_num(interval) and interval > 0 else 60
        rank = inp.get("rank")
        self.rank_map = dict(rank) if isinstance(rank, dict) else {}
        listed = inp.get("listed")
        self.listed = frozenset(listed) if listed is not None else frozenset(self.rank_map)
        self.pinned = frozenset(inp.get("pinned") or ()) & self.listed
        self.slot_mode = bool(inp.get("slot_mode"))
        self.auto_save = bool(inp.get("auto_save"))
        self.K = int(inp.get("K", 2))
        self.C = int(inp.get("C", 1))
        self.M = float(inp.get("M", 1800))
        self.paused = bool(inp.get("paused"))
        self.auto_paused = bool(inp.get("auto_paused"))
        reports = inp.get("reports")
        self.reports = reports if isinstance(reports, dict) else {}
        self.gone = list(inp.get("gone") or ())
        self.default_browser = inp.get("default_browser")
        saves = inp.get("saves")
        self.saves = saves if isinstance(saves, dict) else {}
        watch = inp.get("watch_start")
        self.watch_start = watch if isinstance(watch, dict) else {}
        gap = inp.get("wake_gap")
        self.wake_gap = float(gap) if _is_num(gap) and gap > 0 else 0.0
        self.seed_capable = bool(inp.get("seed_capable"))
        absorb = inp.get("absorb")
        self.absorb = absorb if isinstance(absorb, dict) else {}
        self.events = []
        self.notices = []
        self.live = set()
        self.offline_confirmed = set()
        self.frozen = False
        self.busy = False
        self.rep = None
        self.absorbed = False
        self.freed_lent = {}
        self.gone_logins = set()
        self.queue = []

    def rank(self, login) -> int:
        return self.rank_map.get(login, len(self.listed))

    def order_key(self, login):
        return (self.rank(login), login)

    def ev(self, name: str, **fields) -> None:
        self.events.append((name, fields))

    def save_for(self, login):
        save = self.saves.get(login)
        if isinstance(save, dict) and _is_num(save.get("at")):
            return save
        return None

    def rep_alive(self, rep) -> bool:
        return isinstance(rep, dict) and _is_num(rep.get("mono")) and \
            self.mono - rep["mono"] <= SLOT_EXECUTOR_ALIVE_SECONDS


class SlotScheduler:
    """Every Slot mode rule, as one pure tick (DESIGN 7.3)."""

    def __init__(self) -> None:
        self.state = "off"
        self.startup_mono = None
        self.activated = False
        self._clear_persistent()
        self._reset_runtime()

    # -- state -------------------------------------------------------------

    def _clear_persistent(self) -> None:
        self.seq = 0
        self.executor = None
        self.executor_seen = False
        self.slots = []
        # {login: {session, at, how}}; how is "turn" or "keep"
        self.served = {}
        # {login: {session, at, reason}}; reason is "raid" or "gave_up"
        self.excluded = {}
        # {login: {session, at, reason}}; reason is "user_closed", "navigated"
        # or "window_closed"
        self.dismissed = {}
        # {login: {session, count, window_closed, silent_loss}}
        self.reissued = {}
        # {login: item}, the plan 3.8 item shape (persisted as save_queue)
        self.items = {}
        # {login: {until, record}}: occupants a reduced count released (S6a)
        self.draining = {}

    def _reset_runtime(self) -> None:
        self.tracked_live = {}
        self.offline_strikes = {}
        self.last_seen_live = {}
        self.executor_last_mono = None
        self.absent_since_mono = None
        self.unconfirmed_alive_s = {}
        self.recent_removals = {}
        self.extras_first_seen = {}
        self.extras_closed_logged = set()
        self._reset_outage_opens()
        self.processed_gone = set()
        self.hold_logged = set()
        self.last_published = None
        self.last_plan_sans = None
        self.last_state_snapshot = None
        self.last_close = []
        self.last_tick_mono = None
        self.frozen_logged = False
        self.blocked_logged = False
        self.outage_notified = False
        # False until a successful poll is consumed since a start or since
        # Slot mode was last off: until then tracked_live is not live data.
        self.polled = False
        # {login: misses}: authoritative polls that did not list a login with
        # per-broadcast records whose broadcast end was not observed.
        self.record_strikes = {}

    def _reset_outage_opens(self) -> None:
        """A9, per outage and not persisted: desktop_opened marks the slot
        ids an open was made for (or that carry the budget of one),
        outage_opens counts the opens, and desktop_opened_logins holds the
        logins they opened."""
        self.desktop_opened = {}
        self.outage_opens = 0
        self.desktop_opened_logins = set()

    # -- public interface (plan 3.13) --------------------------------------

    def tick(self, inp: dict) -> TickResult:
        t = _Tick(inp)
        prev = self.state
        dt = 0.0
        if self.last_tick_mono is not None:
            dt = max(0.0, t.mono - self.last_tick_mono - t.wake_gap)
        self.last_tick_mono = t.mono
        if not t.slot_mode:
            return self._tick_off(t, prev)
        if self.startup_mono is None:
            self.startup_mono = t.mono
        if t.seed_capable:
            self.executor_seen = True
        # S1 clock shift
        if t.wake_gap > 0:
            self._shift_clocks(t.wake_gap, None, t)
            t.ev("slot_clock_shifted", gap_s=int(round(t.wake_gap)), reason="sleep")
        # S2 executor state
        state = self._executor_state(t)
        self._transition(t, prev, state)
        self.state = state
        if state == "none_ever":
            self._broadcasts(t, silent=True)
            self._count_change(t)
            return self._finish(t, self._publish(t, [], []), [])
        if state == "alive" and not self.activated:
            self._absorb(t)
        if state == "alive":
            t.rep = t.reports.get(self.executor)
            # S3 busy: record what the owner did, change nothing else. The
            # S6a relabel still runs (it opens and closes nothing), so the
            # plan always lists exactly K + C slots (3.3).
            if t.rep.get("busy") == "paused":
                t.busy = True
                self._broadcasts(t)
                self._process_gone(t)
                self._count_change(t)
                if not self.blocked_logged:
                    t.ev("slot_plan_blocked", busy="paused")
                    self.blocked_logged = True
                t.queue = self._queue(t)
                return self._finish(t, self._publish(t, list(self.last_close), t.queue), [])
        self.blocked_logged = False
        # S4 broadcasts
        self._broadcasts(t)
        # S5 report processing
        if state == "alive":
            self._confirm(t)
        self._process_gone(t)
        if state == "alive":
            self._silent_loss(t)
            self._never_confirmed(t, dt)
            self._extras_seen(t)
        # S6 save items, S6a count change
        self._items_step(t)
        self._count_change(t)
        self._drain_step(t, state)
        # S7 to S10, skipped while waiting or frozen
        if state in ("alive", "absent") and not t.frozen:
            if state == "alive":
                self._adopt_extras(t)
            self._keep_step(t, state)
            self._rotating_step(t, state)
            self._fill_step(t, state)
        # S11 close list, S12 publish, S13 absent-mode opens
        close = self._close_list(t, state)
        t.queue = self._queue(t)
        plan = self._publish(t, close, t.queue)
        opens = self._absent_opens(t) if state == "absent" else []
        return self._finish(t, plan, opens)

    def add_item(self, item: dict, inp: dict, merge_only: bool = False) -> tuple:
        """(changed, events). The gate (auto saves on, or a manual item),
        coverage at creation, and the one-item-per-streamer merge. With
        merge_only the item only merges into this login's existing item."""
        t = _Tick(inp)
        clean = streak_saves.clean_item(item)
        if clean is None:
            return False, []
        if clean["origin"] != "manual" and not t.auto_save:
            return False, []
        if merge_only and clean["login"] not in self.items:
            return False, []
        changed = self._put_item(t, clean)
        return changed, t.events

    def complete_item(self, login: str, reason: str, now: float) -> tuple:
        """(completed, events): the login's item is done; a slot holding the
        login on a save entry is freed (close reason save_done)."""
        events = []
        item = self.items.pop(login, None)
        if item is None:
            return False, events
        events.append(("streak_item_done", {"streamer": login, "reason": reason}))
        slot = self._slot_of(login)
        if slot is not None and slot["entry"] == "save":
            self.recent_removals[login] = ("save_done", now)
            self._clear_slot(slot)
        return True, events

    def is_broadcast_served(self, login: str) -> bool:
        return self._served_now(login)

    def tracked_session(self, login: str):
        return self.tracked_live.get(login)

    def items_snapshot(self) -> dict:
        return {login: dict(item) for login, item in self.items.items()}

    def to_state(self, now: float) -> dict:
        """slot_state.json content, without the tray's "held" map."""
        return {
            "v": 1,
            "saved_at": float(now),
            "seq": self.seq,
            "executor": self.executor,
            "executor_seen": self.executor_seen,
            "slots": [dict(s) for s in self.slots],
            "served": {k: dict(v) for k, v in self.served.items()},
            "excluded": {k: dict(v) for k, v in self.excluded.items()},
            "dismissed": {k: dict(v) for k, v in self.dismissed.items()},
            "reissued": {k: dict(v) for k, v in self.reissued.items()},
            "save_queue": {k: dict(v) for k, v in self.items.items()},
            "draining": [{"streamer": k, "until": d["until"], "record": dict(d["record"])}
                         for k, d in sorted(self.draining.items())],
        }

    def load_state(self, data: dict, now: float) -> None:
        """Restore from slot_state.json (DESIGN 6.1, plan 3.7): the per-
        broadcast maps, items and executor_seen always; seq always; slots,
        executor and draining only from a file at most 600 s old."""
        self._clear_persistent()
        self._reset_runtime()
        self.state = "off"
        self.activated = False
        if not isinstance(data, dict) or data.get("v") != 1:
            return
        seq = data.get("seq")
        if _is_int(seq) and seq >= 0:
            self.seq = seq
        self.executor_seen = data.get("executor_seen") is True
        self.served = self._load_map(data.get("served"), "how", ("turn", "keep"))
        self.excluded = self._load_map(data.get("excluded"), "reason", ("raid", "gave_up"))
        self.dismissed = self._load_map(data.get("dismissed"), "reason",
                                        ("user_closed", "navigated", "window_closed"))
        reissued = data.get("reissued")
        if isinstance(reissued, dict):
            for login, rec in reissued.items():
                if not (_login_ok(login) and isinstance(rec, dict)):
                    continue
                session = rec.get("session")
                if session is not None and not isinstance(session, str):
                    continue
                counts = {k: rec.get(k) if _is_int(rec.get(k)) and rec.get(k) >= 0 else 0
                          for k in ("count", "window_closed", "silent_loss")}
                self.reissued[login] = dict(session=session, **counts)
        queue = data.get("save_queue")
        if isinstance(queue, dict):
            for login, raw in queue.items():
                item = streak_saves.clean_item(raw, login) if _login_ok(login) else None
                if item is not None and not streak_saves.item_expired(item, now):
                    self.items[login] = item
        saved_at = data.get("saved_at")
        if not (_is_num(saved_at) and
                -_STATE_FUTURE_TOLERANCE_SECONDS <= now - saved_at <= SLOT_STATE_MAX_AGE_SECONDS):
            return
        executor = data.get("executor")
        if isinstance(executor, str) and _executor_key_ok(executor, _key_browser(executor)):
            self.executor = executor
        seen_ids = set()
        seen_logins = set()
        slots = data.get("slots")
        for raw in slots if isinstance(slots, list) else ():
            rec = self._load_slot(raw, saved_at)
            if rec is None or rec["id"] in seen_ids:
                continue
            seen_ids.add(rec["id"])
            if rec["streamer"] in seen_logins:
                rec = _empty_slot(rec["id"])
            if rec["streamer"]:
                seen_logins.add(rec["streamer"])
            self.slots.append(rec)
        self.slots.sort(key=lambda s: (0 if s["kind"] == "keep" else 1, s["id"]))
        draining = data.get("draining")
        for raw in draining if isinstance(draining, list) else ():
            if not isinstance(raw, dict):
                continue
            login = raw.get("streamer")
            rec = self._load_slot(raw.get("record"), saved_at)
            if (not _login_ok(login) or login in seen_logins or rec is None
                    or rec["streamer"] != login or not _is_num(raw.get("until"))):
                continue
            seen_logins.add(login)
            self.draining[login] = {"until": float(raw["until"]), "record": rec}
        # A restored live occupant counts as live until the polls say
        # otherwise, so the two-miss rule can close its slot.
        records = list(self.slots) + [d["record"] for d in self.draining.values()]
        for rec in records:
            if rec["streamer"] and rec["entry"] == "live" and isinstance(rec["session"], str):
                self.tracked_live[rec["streamer"]] = rec["session"]
                self.offline_strikes[rec["streamer"]] = 0
                self.last_seen_live[rec["streamer"]] = float(saved_at)

    def reset_startup(self, mono: float) -> None:
        """A fresh start (a launch, or Stop then Start from the tray): the
        90 s grace counts from here, and the next active tick is a new
        activation."""
        self.startup_mono = mono
        self.activated = False
        self.state = "off"
        self.executor_last_mono = None
        self.absent_since_mono = None
        self.outage_notified = False
        self._reset_outage_opens()
        self.last_tick_mono = None
        self.polled = False
        self.record_strikes = {}

    # -- loading helpers ---------------------------------------------------

    @staticmethod
    def _load_map(raw, field, allowed) -> dict:
        out = {}
        if not isinstance(raw, dict):
            return out
        for login, rec in raw.items():
            if not (_login_ok(login) and isinstance(rec, dict)):
                continue
            session = rec.get("session")
            if not isinstance(session, str) or rec.get(field) not in allowed:
                continue
            at = rec.get("at")
            out[login] = {"session": session, "at": float(at) if _is_num(at) else 0.0,
                          field: rec[field]}
        return out

    def _load_slot(self, raw, saved_at):
        if not isinstance(raw, dict):
            return None
        slot_id = raw.get("id")
        if not isinstance(slot_id, str) or not _SLOT_ID_RE.match(slot_id):
            return None
        rec = _empty_slot(slot_id)
        login = raw.get("streamer")
        entry = raw.get("entry")
        if not _login_ok(login) or entry not in ("live", "save"):
            return rec
        if entry == "save" and login not in self.items:
            return rec
        lent = raw.get("lent") is True and rec["kind"] == "keep"
        if rec["kind"] == "keep" and not lent:
            mode = None
        elif lent:
            mode = "turn"
        else:
            mode = raw.get("mode") if raw.get("mode") in ("turn", "idle") else "turn"
        if entry == "save":
            mode = "turn" if rec["kind"] == "cycle" else None
            if rec["kind"] == "keep":
                return rec
        session = raw.get("session")
        waiting = raw.get("waiting")
        assigned = raw.get("assigned_at")
        rec.update({
            "streamer": login,
            "entry": entry,
            "url": _slot_url(login, entry),
            "session": session if isinstance(session, str) else None,
            "assigned_at": float(assigned) if _is_num(assigned) else float(saved_at),
            "mode": mode,
            "lent": lent,
            "waiting": waiting if _login_ok(waiting) else None,
        })
        for key in _CLOCK_FIELDS:
            value = raw.get(key)
            rec[key] = float(value) if _is_num(value) else None
        if mode != "turn":
            rec["turn_ends_at"] = None
        return rec

    # -- small helpers -----------------------------------------------------

    def _slot_of(self, login):
        if not login:
            return None
        for s in self.slots:
            if s["streamer"] == login:
                return s
        return None

    def _keep_slot_of(self, login):
        s = self._slot_of(login)
        if s is not None and s["kind"] == "keep" and not s["lent"]:
            return s
        return None

    def _keep_slots(self) -> list:
        return [s for s in self.slots if s["kind"] == "keep"]

    def _cycle_slots(self) -> list:
        return [s for s in self.slots if s["kind"] == "cycle"]

    def _rotating_occupied(self) -> list:
        return [s for s in self.slots if s["streamer"] and (s["kind"] == "cycle" or s["lent"])]

    def _named(self) -> set:
        return {s["streamer"] for s in self.slots if s["streamer"]}

    def _started_epoch(self, login):
        return streak_saves.iso_to_epoch(self.tracked_live.get(login))

    def _served_for(self, login, session) -> bool:
        rec = self.served.get(login)
        return rec is not None and (session is None or rec.get("session") == session)

    def _served_now(self, login) -> bool:
        return self._served_for(login, self.tracked_live.get(login))

    def _recently_removed(self, t, login) -> bool:
        rec = self.recent_removals.get(login)
        return rec is not None and t.now - rec[1] <= SLOT_REMOVAL_MEMORY_SECONDS

    def _absent_ok(self, t, slot, state) -> bool:
        """A9: while absent, an empty slot is assigned only when its id is
        unmarked (no desktop open was made for it in this outage and it
        carries no spent budget from a count change), and never while
        paused."""
        return state != "absent" or (slot["id"] not in self.desktop_opened and not t.paused)

    def _clear_slot(self, slot) -> None:
        slot_id = slot["id"]
        slot.clear()
        slot.update(_empty_slot(slot_id))
        self.unconfirmed_alive_s.pop(slot_id, None)

    def _free(self, t, slot, reason) -> None:
        login = slot["streamer"]
        if login and reason:
            self.recent_removals[login] = (reason, t.now)
        self._clear_slot(slot)

    def _assign(self, t, slot, login, entry, mode, lent, event=True) -> None:
        slot.update({
            "streamer": login, "entry": entry, "url": _slot_url(login, entry),
            "session": self.tracked_live.get(login), "assigned_at": t.now,
            "confirmed_at": None, "first_confirmed_at": None, "turn_ends_at": None,
            "mode": mode, "hold_until": None, "waiting": None, "lent": lent,
        })
        self.unconfirmed_alive_s[slot["id"]] = 0.0
        self.recent_removals.pop(login, None)
        self.extras_first_seen.pop(login, None)
        if entry == "save":
            self.reissued.pop(login, None)
        if event:
            t.ev("slot_assigned", slot=slot["id"], streamer=login, entry=entry, mode=mode, lent=lent)

    def _move(self, src, dst, mode, lent) -> None:
        """Relabel: the record moves with its clocks; nothing reopens."""
        for key in _SLOT_FIELDS:
            if key not in ("id", "kind"):
                dst[key] = src[key]
        dst["mode"] = mode
        dst["lent"] = lent
        dst["waiting"] = None
        dst["hold_until"] = None
        if mode != "turn":
            dst["turn_ends_at"] = None
        if src["id"] in self.unconfirmed_alive_s:
            self.unconfirmed_alive_s[dst["id"]] = self.unconfirmed_alive_s[src["id"]]
        # The desktop open of this outage moves with the record (the tab is
        # the same); the source id stays marked, which keeps the A9 cap.
        if src["id"] in self.desktop_opened:
            self.desktop_opened[dst["id"]] = src["streamer"]
        self._clear_slot(src)

    def _mark_served(self, t, login, how) -> None:
        session = self.tracked_live.get(login)
        if session is None:
            slot = self._slot_of(login)
            session = slot["session"] if slot else None
        rec = self.served.get(login)
        if rec is not None and rec.get("session") == session:
            return
        self.served[login] = {"session": session, "at": t.now, "how": how}
        t.ev("slot_served", streamer=login, how=how, session=session)
        # D7 (a): a live turn never settles a break; the item becomes a
        # quick check of the save-streak page once no slot holds the login.
        item = self.items.get(login)
        if item is not None and item["break_at"] < t.now and not item["verify"]:
            item["verify"] = True

    def _complete(self, t, login, reason) -> bool:
        if self.items.pop(login, None) is None:
            return False
        t.ev("streak_item_done", streamer=login, reason=reason)
        return True

    def _put_item(self, t, item) -> bool:
        """Create or merge one item, with the expiry and coverage checks of
        DESIGN 12.2. Returns True when an item was created or changed."""
        login = item["login"]
        if streak_saves.item_expired(item, t.now):
            t.ev("streak_item_expired", streamer=login, deadline_at=_iso(item["deadline_at"]))
            return False
        save = t.save_for(login)
        if streak_saves.save_covers(item, save, t.watch_start.get(login)):
            t.ev("vod_skipped", streamer=login, reason="streak_already_saved", saved_at=_iso(save["at"]))
            return False
        existing = self.items.get(login)
        merged_flag = existing is not None
        if existing is None:
            stored = item
        else:
            stored, changed = streak_saves.merge_items(existing, item, t.now)
            if not changed:
                return False
        self.items[login] = stored
        t.ev("streak_item_added", streamer=login, kind=stored["kind"],
             deadline_at=_iso(stored["deadline_at"]), origin=stored["origin"],
             verify=stored["verify"], merged=merged_flag, mode="slot")
        return True

    def _reissue_rec(self, login, slot) -> dict:
        session = self.tracked_live.get(login) if slot["entry"] == "live" else slot["session"]
        rec = self.reissued.get(login)
        if rec is None or rec.get("session") != session:
            rec = {"session": session, "count": 0, "window_closed": 0, "silent_loss": 0}
            self.reissued[login] = rec
        return rec

    def _reopen(self, slot) -> None:
        """A reissue: the extension reopens the tab and the clock restarts on
        reconfirmation; first_confirmed_at stays (rule 12)."""
        slot["confirmed_at"] = None
        if slot["mode"] == "turn":
            slot["turn_ends_at"] = None
            if slot["lent"]:
                slot["hold_until"] = None
        self.unconfirmed_alive_s[slot["id"]] = 0.0

    def _shift_clocks(self, gap, only, t) -> None:
        for s in self.slots:
            if only is not None and s["streamer"] not in only:
                continue
            for key in _CLOCK_FIELDS:
                if s[key] is not None:
                    s[key] += gap
        if only is None:
            for login in self.extras_first_seen:
                self.extras_first_seen[login] += gap
            for d in self.draining.values():
                d["until"] += gap

    def _targets(self, t) -> list:
        """The first K live Keep Open streamers by rank that are not
        dismissed or excluded (rule 9)."""
        eligible = [x for x in t.live if x in t.pinned and x not in self.dismissed
                    and x not in self.excluded and x not in self.draining]
        return sorted(eligible, key=t.order_key)[:max(0, t.K)]

    def _lendable_keep(self, t) -> list:
        """Empty Keep Open slots a rotating turn may borrow: those left once
        every Keep Open target without one has taken one (A1)."""
        missing = 0
        for target in self._targets(t):
            slot = self._slot_of(target)
            if slot is None or slot["kind"] != "keep":
                missing += 1
        empties = [s for s in self._keep_slots() if not s["streamer"]]
        return empties[missing:]

    def _queue(self, t) -> list:
        """The S10a queue: urgent, then live by rank, then the other saves.

        A save entry whose streamer's channel tab the plan just freed is
        still open is withheld: the extension keeps an open tab for a planned
        streamer, so assigning it now would leave the channel page in place
        of the save-streak page. It stays in the queue and keeps a free
        rotating slot reserved (no lower entry and no idle pick takes it), and
        it is placed once the executor reports the tab gone."""
        held = self._named() | set(self.draining)
        open_now = _streamers_of(t.rep) if t.rep is not None else frozenset()
        horizon = t.now + streak_saves.SAVE_URGENT_HOURS * _HOUR
        urgent, live, rest = [], [], []
        for login in t.live:
            if (login in held or login in self.dismissed or login in self.excluded
                    or self._served_now(login)):
                continue
            item = self.items.get(login)
            deadline = item["deadline_at"] if item is not None else None
            manual = item is not None and item["origin"] == "manual"
            entry = {"streamer": login, "entry": "live", "deadline_at": deadline,
                     "urgent": False, "manual": manual}
            if manual or (deadline is not None and deadline <= horizon):
                entry["urgent"] = True
                urgent.append(entry)
            else:
                live.append(entry)
        for login, item in self.items.items():
            if login in held or login in self.dismissed:
                continue
            if login in t.live and not self._served_now(login):
                continue  # attached: it rides the live entry
            if streak_saves.item_expired(item, t.now):
                continue
            manual = item["origin"] == "manual"
            entry = {"streamer": login, "entry": "save", "deadline_at": item["deadline_at"],
                     "urgent": False, "manual": manual,
                     "withheld": login in open_now and self._recently_removed(t, login)}
            if manual or item["deadline_at"] <= horizon:
                entry["urgent"] = True
                urgent.append(entry)
            else:
                rest.append(entry)
        urgent.sort(key=lambda e: (0 if e["manual"] else 1, e["deadline_at"],
                                   t.rank(e["streamer"]), e["streamer"]))
        live.sort(key=lambda e: t.order_key(e["streamer"]))
        rest.sort(key=lambda e: (e["deadline_at"], t.rank(e["streamer"]), e["streamer"]))
        return urgent + live + rest

    def _idle_pick_order(self, t) -> list:
        """S10c: live logins in no slot, not dismissed or excluded, by rank.
        Served ones count."""
        held = self._named() | set(self.draining)
        cands = [x for x in t.live if x not in held and x not in self.dismissed
                 and x not in self.excluded]
        return sorted(cands, key=t.order_key)

    def _swap_order(self, t, include=()) -> list:
        """A2: the idle-pick order for the idle set: live, not in a Keep Open
        occupant's slot or a turn-mode slot, not dismissed or excluded."""
        blocked = set()
        for s in self.slots:
            if s["streamer"] and ((s["kind"] == "keep" and not s["lent"]) or s["mode"] == "turn"):
                blocked.add(s["streamer"])
        blocked -= set(include)
        cands = [x for x in t.live if x not in blocked and x not in self.dismissed
                 and x not in self.excluded and x not in self.draining]
        return sorted(cands, key=t.order_key)

    def _idle_capacity(self, extra_slot=None) -> int:
        return len([s for s in self._cycle_slots()
                    if s is extra_slot or not s["streamer"] or s["mode"] == "idle"])

    # -- S0 ----------------------------------------------------------------

    def _tick_off(self, t, prev) -> TickResult:
        had_plan = prev != "off"
        off = None
        if had_plan or self.items:
            open_live = []
            if prev in _ACTIVE_STATES:
                reported = set()
                for rep in t.reports.values():
                    if t.rep_alive(rep):
                        reported |= _streamers_of(rep)
                # Rule 46: a stream the desktop opened in this outage has the
                # desktop's own tab, so it is not opened a second time.
                if prev == "absent":
                    reported |= self.desktop_opened_logins
                for login in sorted(self.tracked_live, key=t.order_key):
                    if login in reported:
                        continue
                    dismissed = self.dismissed.get(login)
                    if dismissed is not None and dismissed.get("session") == self.tracked_live[login]:
                        continue
                    open_live.append(login)
            off = {"open_live": open_live,
                   "items": [dict(self.items[k]) for k in sorted(self.items)]}
            if had_plan:
                t.ev("slot_mode_inactive", reason="setting_off")
            self.items = {}
            self.slots = [_empty_slot(s["id"]) for s in self.slots]
            self.draining = {}
            self.tracked_live = {}
            self.offline_strikes = {}
            self.last_seen_live = {}
            self.unconfirmed_alive_s = {}
            self.recent_removals = {}
            self.extras_first_seen = {}
            self._reset_outage_opens()
            self.last_published = None
            self.last_close = []
        self.state = "off"
        self.activated = False
        # tracked_live is not kept while Slot mode is off.
        self.polled = False
        return TickResult(plan=None, events=t.events, opens=[], state="off", notices=[],
                          persist=off is not None, absorbed=False, off_transition=off,
                          tooltip=None)

    # -- S2 ----------------------------------------------------------------

    def _executor_state(self, t) -> str:
        capable = {}
        for key, rep in t.reports.items():
            if not isinstance(rep, dict) or rep.get("plan_seq") is None:
                continue
            if not _executor_key_ok(key, rep.get("browser")):
                continue
            capable[key] = rep
        alive = {k: r for k, r in capable.items() if t.rep_alive(r)}
        current = self.executor
        chosen = None
        default = t.default_browser

        def most_recent(keys):
            return max(keys, key=lambda k: (alive[k]["mono"], k))

        if default:
            default_alive = [k for k in alive if alive[k].get("browser") == default]
            if default_alive and (current is None or _key_browser(current) != default):
                chosen = most_recent(default_alive)
        if chosen is None:
            if current in alive:
                chosen = current
            elif alive:
                preferred = [k for k in alive if default and alive[k].get("browser") == default]
                chosen = most_recent(preferred or list(alive))
        if chosen is not None:
            if chosen != current:
                t.ev("slot_executor_changed", **{"from": current, "to": chosen})
                if current is not None:
                    # The new executor opens what it lacks; its tabs are
                    # confirmed afresh instead of read as lost.
                    for s in self.slots:
                        if s["streamer"]:
                            self._reopen(s)
                self.executor = chosen
            self.executor_last_mono = alive[chosen]["mono"]
            return "alive"
        if self.executor_seen:
            if t.mono - self.startup_mono < SLOT_STARTUP_GRACE_SECONDS:
                return "waiting"
            return "absent"
        return "none_ever"

    def _transition(self, t, prev, state) -> None:
        if prev == "off":
            self.record_strikes = {}
        if prev in ("off", "none_ever") and state in _ACTIVE_STATES:
            self.activated = False
        if state == "none_ever" and prev != "none_ever":
            t.notices.append("none_ever")
        if state == "absent":
            if prev != "absent":
                self.absent_since_mono = t.mono
                self.outage_notified = False
                self._reset_outage_opens()
                t.ev("slot_executor_absent", executor=self.executor)
            if (not self.outage_notified
                    and t.mono - self.absent_since_mono >= SLOT_OUTAGE_NOTIFY_SECONDS):
                self.outage_notified = True
                t.notices.append("outage")
        elif prev == "absent" and state == "alive":
            since = self.absent_since_mono if self.absent_since_mono is not None else t.mono
            absent_s = max(0.0, t.mono - since)
            t.ev("slot_executor_back", executor=self.executor, absent_s=int(round(absent_s)))
            if absent_s > 0:
                listed = _streamers_of(t.reports.get(self.executor))
                self._shift_clocks(absent_s, listed, t)
                t.ev("slot_clock_shifted", gap_s=int(round(absent_s)), reason="absent")
            self.absent_since_mono = None
            self._reset_outage_opens()

    # -- S2a ---------------------------------------------------------------

    def _absorb(self, t) -> None:
        absorb = t.absorb
        vods = absorb.get("queued_vods")
        for login in sorted(vods) if isinstance(vods, dict) else ():
            entry = vods[login]
            if not (_login_ok(login) and isinstance(entry, dict)):
                continue
            self._put_item(t, self._item_from_vod(t, login, entry))
        held = absorb.get("held")
        for login in sorted(held) if isinstance(held, dict) else ():
            item = streak_saves.clean_item(held[login], login) if _login_ok(login) else None
            if item is None:
                continue
            self._put_item(t, self._absorbed(t, login, item["break_at"], item["deadline_at"],
                                             item["age_unit_s"], item["verify"], item))
        offers = [absorb.get("pending_offer")]
        acked = absorb.get("last_acked_offer")
        if isinstance(acked, dict):
            offers.append(acked.get("offer"))
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            candidates = offer.get("candidates")
            for cand in candidates if isinstance(candidates, list) else ():
                if not isinstance(cand, dict) or cand.get("kind") != "ended":
                    continue
                login = cand.get("streamer")
                if not _login_ok(login):
                    continue
                ended = streak_saves.iso_to_epoch(cand.get("ended_at"))
                item = streak_saves.make_missed_item(login, ended if ended is not None else t.now,
                                                     0, None, t.now, origin="absorbed")
                self._put_item(t, item)
        self.executor_seen = True
        self.activated = True
        t.absorbed = True
        t.ev("slot_mode_active", executor=self.executor, keep=t.K, cycle=t.C,
             minutes=int(t.M // 60))
        t.notices.append("activated")

    @staticmethod
    def _absorbed(t, login, break_at, deadline, unit, verify, source) -> dict:
        """DESIGN S2a: a leftover becomes a missed item with origin absorbed
        and the given break, deadline and unit. A manual item (a Streaks at
        Risk click, A12) keeps its own shape, so it stays the next rotating
        turn (O16) and is covered only by a save seen since the click."""
        if source is not None and source["origin"] == "manual":
            item = dict(source)
        else:
            session = source["session"] if source is not None else None
            item = streak_saves.make_missed_item(login, break_at, unit, session, t.now,
                                                 origin="absorbed")
        if deadline is not None:
            item["deadline_at"] = float(deadline)
        item["verify"] = bool(verify)
        return item

    @staticmethod
    def _item_from_vod(t, login, entry) -> dict:
        """A queued_vods entry (plan 3.8) as an S2a item: break_at from
        ended_at (unparseable: now); the entry's own deadline_at wins, then
        its embedded item's, else break_at + 24 h; the unit from the entry,
        else its embedded item."""
        embedded = streak_saves.clean_item(entry.get("item"), login)
        ended = streak_saves.iso_to_epoch(entry.get("ended_at"))
        unit = entry.get("age_unit_s")
        if not (_is_int(unit) and unit >= 0):
            unit = embedded["age_unit_s"] if embedded is not None else 0
        deadline = streak_saves.iso_to_epoch(entry.get("deadline_at"))
        if deadline is None and embedded is not None:
            deadline = embedded["deadline_at"]
        verify = entry.get("verify") is True or (embedded is not None and embedded["verify"])
        return SlotScheduler._absorbed(t, login, ended if ended is not None else t.now, deadline,
                                       unit, verify, embedded)

    # -- S4 ----------------------------------------------------------------

    def _forget_live(self, login) -> None:
        self.tracked_live.pop(login, None)
        self.offline_strikes.pop(login, None)
        self.last_seen_live.pop(login, None)

    def _expire_records(self, t) -> None:
        """Records of a broadcast whose end was never observed (it ended
        while the desktop was stopped, while Slot mode was off, or while the
        login was unlisted) expire after SLOT_OFFLINE_STRIKES polls that do
        not list the login, the two-miss rule of S4. No item is made. A login
        the polls list again enters tracked_live, where S4's session rule
        keeps or drops its records."""
        maps = (self.served, self.excluded, self.dismissed, self.reissued)
        keyed = set().union(*maps)
        for login in list(self.record_strikes):
            if login not in keyed:
                del self.record_strikes[login]
        named = self._named() | set(self.draining)
        for login in sorted(keyed):
            # Unlisted: the polls cannot say whether it ended. In a slot: the
            # records belong to the running assignment (a save turn's
            # reissue count).
            if login in self.tracked_live or login not in t.listed or login in named:
                self.record_strikes.pop(login, None)
                continue
            strikes = self.record_strikes.get(login, 0) + 1
            if strikes < SLOT_OFFLINE_STRIKES:
                self.record_strikes[login] = strikes
                continue
            for mapping in maps:
                mapping.pop(login, None)
            self.record_strikes.pop(login, None)

    def _broadcasts(self, t, silent=False) -> None:
        for login in list(self.tracked_live):
            if login not in t.listed:
                self._forget_live(login)  # rule 14: no item
        if t.authoritative and t.poll is not None:
            self.polled = True
            seen = t.live_as_of if t.live_as_of is not None else t.now
            for login, started in t.poll.items():
                if login not in t.listed or not isinstance(started, str):
                    continue
                self.tracked_live[login] = started
                self.offline_strikes[login] = 0
                self.last_seen_live[login] = seen
            for login in list(self.tracked_live):
                if login in t.poll:
                    continue
                strikes = self.offline_strikes.get(login, 0) + 1
                self.offline_strikes[login] = strikes
                if strikes >= SLOT_OFFLINE_STRIKES:
                    t.offline_confirmed.add(login)
            self._expire_records(t)
        t.live = set(self.tracked_live) - t.offline_confirmed
        for login in sorted(t.offline_confirmed):
            session = self.tracked_live.get(login)
            if silent or self._served_for(login, session):
                continue
            # Rule 24 (O9, O12, A3): an unserved broadcast gets a save turn,
            # whether it held a slot, waited, was excluded or was dismissed.
            if t.auto_save:
                item = streak_saves.make_missed_item(login, self.last_seen_live.get(login),
                                                     t.check_interval, session, t.now)
                self._put_item(t, item)
            else:
                t.ev("slot_unserved_dropped", streamer=login, session=session)
        for mapping in (self.served, self.excluded, self.dismissed, self.reissued):
            for login in list(mapping):
                current = self.tracked_live.get(login)
                if login in t.offline_confirmed or (
                        current is not None and mapping[login].get("session") != current):
                    del mapping[login]
        for login in t.offline_confirmed:
            self._forget_live(login)
        # A Keep Open occupant, and a draining one, follow a broadcast
        # restart, so a relabel or slot_state.json carries the current
        # session. Cycle slots keep theirs: S9 reads a changed session as a
        # restart of an idle occupant.
        records = [s for s in self.slots if s["kind"] == "keep" and not s["lent"]]
        records += [d["record"] for d in self.draining.values()]
        for rec in records:
            login = rec["streamer"]
            if login and rec["entry"] == "live" and login in self.tracked_live:
                rec["session"] = self.tracked_live[login]
        if t.live_as_of is None or not self.polled:
            # No live data yet (none since a start or since Slot mode was
            # off): nothing to change, and no reported tab is an extra.
            t.frozen = True
        elif t.now - t.live_as_of > max(SLOT_LIVE_STALE_SECONDS, 3 * t.check_interval):
            t.frozen = True
            if not self.frozen_logged and not silent:
                t.ev("slot_frozen", reason="stale_live")
                self.frozen_logged = True
        else:
            self.frozen_logged = False

    # -- S5 ----------------------------------------------------------------

    def _confirm(self, t) -> None:
        rep = t.rep
        names = _streamers_of(rep)
        epoch = rep.get("epoch")
        if not _is_num(epoch):
            return
        for s in self.slots:
            if not s["streamer"] or s["confirmed_at"] is not None or s["streamer"] not in names:
                continue
            if epoch < s["assigned_at"]:
                continue
            s["confirmed_at"] = float(epoch)
            if s["first_confirmed_at"] is None:
                s["first_confirmed_at"] = float(epoch)
            if s["mode"] == "turn":
                s["turn_ends_at"] = s["confirmed_at"] + t.M
            if s["waiting"] and s["hold_until"] is None:
                if s["lent"]:
                    s["hold_until"] = s["turn_ends_at"]
                elif s["kind"] == "keep":
                    s["hold_until"] = s["first_confirmed_at"] + t.M
            self.unconfirmed_alive_s.pop(s["id"], None)
            t.ev("slot_confirmed", slot=s["id"], streamer=s["streamer"],
                 latency_s=int(round(s["confirmed_at"] - s["assigned_at"])))

    def _process_gone(self, t) -> None:
        cutoff = int(t.now) - _GONE_MEMORY_SECONDS
        self.processed_gone = {k for k in self.processed_gone if k[3] >= cutoff}
        for g in t.gone:
            if not isinstance(g, dict):
                continue
            login, reason, at = g.get("streamer"), g.get("reason"), g.get("at")
            if not _login_ok(login) or reason not in GONE_REASONS or not _is_int(at):
                continue
            key = (g.get("key"), login, reason, at)
            if key in self.processed_gone:
                continue
            self.processed_gone.add(key)
            started = self._started_epoch(login)
            if started is not None and at < int(started):
                continue  # an earlier broadcast
            slot = self._slot_of(login)
            drain = self.draining.get(login) if slot is None else None
            rec = slot if slot is not None else (drain["record"] if drain is not None else None)
            if rec is not None and at < int(rec["assigned_at"]):
                continue  # an earlier assignment
            t.gone_logins.add(login)
            self._apply_gone(t, login, reason, slot, at)
            if drain is not None:
                # The tab of a draining occupant is gone: its drain ends
                # here, without the served marking a drain end gives.
                self.draining.pop(login, None)

    def _gone_event(self, t, slot, login, reason) -> None:
        t.ev("slot_gone", slot=slot["id"], streamer=login, entry=slot["entry"], reason=reason)

    def _serve_before_raid(self, t, login, rec, at) -> None:
        """Rule 25: a raid counts as served when the occupant already had its
        M minutes at the raid's own time, even when no tick marked it yet
        (the S8 and S9 tests, evaluated at the raid). Only while alive: an
        absent plan's clocks stand still."""
        if (self.state != "alive" or rec["entry"] != "live" or rec["confirmed_at"] is None
                or self._served_now(login)):
            return
        # The raid's at is a whole-second epoch (the extension floors it), so
        # the desktop's fractional clocks are floored too (plan WP1 task 6).
        raid_at = min(at, int(t.now))
        if rec["mode"] == "turn":
            if rec["turn_ends_at"] is not None and raid_at >= int(rec["turn_ends_at"]):
                self._mark_served(t, login, "turn")
            return
        started = self._started_epoch(login)
        start = max(rec["confirmed_at"], started) if started is not None else rec["confirmed_at"]
        if raid_at >= int(start + t.M):
            self._mark_served(t, login, "keep" if rec["mode"] is None else "turn")

    def _apply_gone(self, t, login, reason, slot, at) -> None:
        drain = self.draining.get(login) if slot is None else None
        rec = slot if slot is not None else (drain["record"] if drain is not None else None)
        entry = rec["entry"] if rec is not None else None
        session = self.tracked_live.get(login)
        if reason in ("user_closed", "navigated"):
            if entry == "save":
                # Rule 26: a closed save turn ends its item, dismisses nothing.
                self._complete(t, login, reason)
            elif login in self.tracked_live:
                self.dismissed[login] = {"session": session, "at": t.now, "reason": reason}
            if slot is not None:
                self._gone_event(t, slot, login, reason)
                self._free(t, slot, None)
        elif reason == "raid":
            if entry == "save":
                self._complete(t, login, "navigated")
            else:
                if rec is not None:
                    self._serve_before_raid(t, login, rec, at)
                if login in self.tracked_live and not self._served_now(login):
                    # Rule 25 (O13): excluded for the broadcast; its end
                    # brings a save turn. A raid never settles an item.
                    self.excluded[login] = {"session": session, "at": t.now, "reason": "raid"}
            if slot is not None:
                self._gone_event(t, slot, login, reason)
                self._free(t, slot, None)
        elif reason == "window_closed":
            if slot is None:
                return
            marks = self._reissue_rec(login, slot)
            if marks["window_closed"] >= 1:
                # Rule 27: the second close in a broadcast dismisses.
                if entry == "save":
                    self._complete(t, login, "user_closed")
                else:
                    self.dismissed[login] = {"session": session, "at": t.now,
                                             "reason": "window_closed"}
                self._gone_event(t, slot, login, reason)
                self._free(t, slot, None)
            else:
                marks["window_closed"] += 1
                marks["count"] += 1
                self._reopen(slot)
                t.ev("slot_open_reissued", slot=slot["id"], streamer=login, why="window_closed")
        else:
            # already_saved, not_eligible (12.3): the turn is done now. No
            # save is recorded here; only /streak_event records one.
            self._complete(t, login, reason)
            if slot is not None and entry == "save":
                self._gone_event(t, slot, login, reason)
                self._free(t, slot, "save_done")

    def _give_up(self, t, slot, why) -> None:
        login, entry = slot["streamer"], slot["entry"]
        t.ev("slot_open_gave_up", slot=slot["id"], streamer=login, entry=entry, why=why)
        if entry == "live":
            self.excluded[login] = {"session": self.tracked_live.get(login, slot["session"]),
                                    "at": t.now, "reason": "gave_up"}
        else:
            item = self.items.get(login)
            if item is not None:
                item["open_failures"] = item.get("open_failures", 0) + 1
                if item["open_failures"] >= 2:
                    self._complete(t, login, "open_failed")
        self._free(t, slot, None)

    def _silent_loss(self, t) -> None:
        rep = t.rep
        names = _streamers_of(rep)
        epoch = rep.get("epoch")
        if not _is_num(epoch):
            return
        for s in self.slots:
            login = s["streamer"]
            if (not login or s["confirmed_at"] is None or login in names
                    or login in t.gone_logins or not epoch > s["confirmed_at"]):
                continue
            rec = self._reissue_rec(login, s)
            if rec["silent_loss"] >= 1:
                self._give_up(t, s, "silent_loss")
            else:
                rec["silent_loss"] += 1
                rec["count"] += 1
                self._reopen(s)
                t.ev("slot_open_reissued", slot=s["id"], streamer=login, why="silent_loss")

    def _never_confirmed(self, t, dt) -> None:
        # Only alive, non-busy, assigning time counts toward the give-up.
        if t.paused or dt <= 0:
            return
        for s in self.slots:
            if not s["streamer"] or s["confirmed_at"] is not None:
                continue
            waited = self.unconfirmed_alive_s.get(s["id"], 0.0) + dt
            if waited >= SLOT_OPEN_GIVE_UP_SECONDS:
                self._give_up(t, s, "never_confirmed")
            else:
                self.unconfirmed_alive_s[s["id"]] = waited

    def _extras_seen(self, t) -> None:
        names = _streamers_of(t.rep)
        named = self._named()
        for login in names:
            if login not in named:
                self.extras_first_seen.setdefault(login, t.now)
        for login in list(self.extras_first_seen):
            if login not in names or login in named:
                del self.extras_first_seen[login]

    # -- S6 and S6a ----------------------------------------------------------

    def _items_step(self, t) -> None:
        for login in sorted(self.items):
            item = self.items[login]
            if streak_saves.item_expired(item, t.now):
                del self.items[login]
                t.ev("streak_item_expired", streamer=login, deadline_at=_iso(item["deadline_at"]))
                continue
            save = t.save_for(login)
            if streak_saves.save_covers(item, save, t.watch_start.get(login)):
                del self.items[login]
                t.ev("vod_skipped", streamer=login, reason="streak_already_saved",
                     saved_at=_iso(save["at"]))
                continue
            if not t.auto_save and item["origin"] != "manual":
                self._complete(t, login, "setting_off")

    def _count_change(self, t) -> None:
        """S6a with A35: map occupants onto the new slot ids by rank; the
        rest drain."""
        expected = _layout(max(0, t.K), max(0, t.C))
        if [s["id"] for s in self.slots] == expected:
            return
        fresh = {sid: _empty_slot(sid) for sid in expected}
        occupied = [s for s in self.slots if s["streamer"]]
        old_unconfirmed = dict(self.unconfirmed_alive_s)
        self.unconfirmed_alive_s = {}
        # The new layout reuses old ids, so marks are read from a snapshot and
        # rebuilt for the new ids only (see the end of this method).
        old_opened = dict(self.desktop_opened)
        self.desktop_opened = {}
        keep_free = [sid for sid in expected if sid.startswith("keep-")]
        cycle_free = [sid for sid in expected if sid.startswith("cycle-")]
        by_rank = lambda s: t.order_key(s["streamer"])  # noqa: E731
        keep_occ = sorted([s for s in occupied if s["kind"] == "keep" and not s["lent"]], key=by_rank)
        rot_occ = sorted([s for s in occupied if s["kind"] == "cycle" or s["lent"]], key=by_rank)

        def place(src, slot_id, mode, lent):
            dst = fresh[slot_id]
            for key in _SLOT_FIELDS:
                if key not in ("id", "kind"):
                    dst[key] = src[key]
            dst["mode"] = mode
            dst["lent"] = lent
            dst["waiting"] = None
            dst["hold_until"] = None
            if mode != "turn":
                dst["turn_ends_at"] = None
            if src["id"] in old_unconfirmed:
                self.unconfirmed_alive_s[slot_id] = old_unconfirmed[src["id"]]
            if src["id"] in old_opened:
                self.desktop_opened[slot_id] = src["streamer"]

        for s in keep_occ:
            if keep_free:
                place(s, keep_free.pop(0), None, False)
            else:
                self._start_drain(t, s)
        leftover = []
        for s in rot_occ:
            if cycle_free:
                place(s, cycle_free.pop(0), s["mode"] or "turn", False)
            else:
                leftover.append(s)
        for s in leftover:
            if s["mode"] == "turn" and s["entry"] == "live" and keep_free:
                place(s, keep_free.pop(0), "turn", True)
            else:
                self._start_drain(t, s)
        self.slots = [fresh[sid] for sid in expected]
        self._carry_outage_budget(expected, fresh)

    def _carry_outage_budget(self, expected, fresh) -> None:
        """A9 across a count change: the opens this outage already made stay
        spent. The placed occupants brought their marks; the rest of the
        spent budget marks further new ids (empty ones first, in layout
        order, then occupied unmarked ones). One outage then makes at most
        as many desktop opens as its largest K + C (exactly K + C when the
        total does not change), even when a desktop-opened occupant drains
        or a marked id goes away."""
        budget = min(len(expected), self.outage_opens) - len(self.desktop_opened)
        order = ([sid for sid in expected if not fresh[sid]["streamer"]]
                 + [sid for sid in expected if fresh[sid]["streamer"]])
        for sid in order:
            if budget <= 0:
                break
            if sid not in self.desktop_opened:
                self.desktop_opened[sid] = fresh[sid]["streamer"]
                budget -= 1

    def _start_drain(self, t, slot) -> None:
        if slot["kind"] == "keep" and not slot["lent"]:
            first = slot["first_confirmed_at"]
            until = first + t.M if first is not None else t.now
        elif slot["mode"] == "turn":
            until = slot["turn_ends_at"] if slot["turn_ends_at"] is not None else t.now
        else:
            until = t.now
        self.draining[slot["streamer"]] = {"until": max(until, t.now), "record": dict(slot)}

    def _drain_step(self, t, state) -> None:
        if state != "alive" or t.frozen:
            return
        for login in sorted(self.draining):
            drain = self.draining[login]
            rec = drain["record"]
            gone = rec["entry"] == "live" and (login not in t.listed or login not in self.tracked_live)
            if not gone and t.now < drain["until"]:
                continue
            del self.draining[login]
            if gone:
                reason = "unlisted" if login not in t.listed else "offline"
            else:
                reason = "turn_over"
                confirmed = rec["confirmed_at"]
                if rec["entry"] == "live" and confirmed is not None:
                    started = self._started_epoch(login)
                    start = max(confirmed, started) if started is not None else confirmed
                    if rec["mode"] == "turn":
                        if rec["turn_ends_at"] is not None and t.now >= rec["turn_ends_at"]:
                            self._mark_served(t, login, "turn")
                    elif t.now >= start + t.M:
                        self._mark_served(t, login, "keep" if rec["mode"] is None else "turn")
                elif (rec["entry"] == "save" and rec["turn_ends_at"] is not None
                      and t.now >= rec["turn_ends_at"]):
                    self._complete(t, login, "turn_done")
            self.recent_removals[login] = (reason, t.now)

    # -- S7 ----------------------------------------------------------------

    def _adopt(self, t, slot, login, entry, mode, lent) -> None:
        first = self.extras_first_seen.get(login, t.now)
        slot.update({
            "streamer": login, "entry": entry, "url": _slot_url(login, entry),
            "session": self.tracked_live.get(login), "assigned_at": first,
            "confirmed_at": first, "first_confirmed_at": first,
            "turn_ends_at": first + t.M if mode == "turn" else None,
            "mode": mode, "hold_until": None, "waiting": None, "lent": lent,
        })
        self.extras_first_seen.pop(login, None)
        self.recent_removals.pop(login, None)
        self.unconfirmed_alive_s.pop(slot["id"], None)
        if entry == "save":
            self.reissued.pop(login, None)
        t.ev("slot_extra_adopted", slot=slot["id"], streamer=login, mode=mode)

    def _adopt_extras(self, t) -> None:
        names = _streamers_of(t.rep)

        def extras():
            named = self._named()
            return {x for x in names if x not in named and x not in self.draining
                    and x in self.extras_first_seen and not self._recently_removed(t, x)}

        targets = self._targets(t)
        for login in sorted(extras(), key=t.order_key):
            if login in targets and self._keep_slot_of(login) is None:
                empty = next((s for s in self._keep_slots() if not s["streamer"]), None)
                if empty is None:
                    break
                self._adopt(t, empty, login, "live", None, False)
        while True:
            pool = extras()
            if not pool:
                return
            queue = self._queue(t)
            free_cycle = [s for s in self._cycle_slots() if not s["streamer"]]
            if queue:
                head = queue[0]
                if free_cycle and head["streamer"] in pool:
                    self._adopt(t, free_cycle[0], head["streamer"], head["entry"], "turn", False)
                    continue
                lendable = self._lendable_keep(t)
                first_live = next((e for e in queue if e["entry"] == "live"), None)
                if lendable and first_live is not None and first_live["streamer"] in pool:
                    self._adopt(t, lendable[0], first_live["streamer"], "live", "turn", True)
                    continue
                return
            order = self._idle_pick_order(t)
            if free_cycle and order and order[0] in pool:
                self._adopt(t, free_cycle[0], order[0], "live", "idle", False)
                continue
            return

    # -- S8 ----------------------------------------------------------------

    def _cleanup(self, t) -> None:
        for s in self.slots:
            login = s["streamer"]
            if not login or s["entry"] != "live":
                continue
            if login not in t.listed:
                self._free(t, s, "unlisted")
            elif login not in self.tracked_live:
                self._free(t, s, "offline")

    def _log_hold(self, t, slot, target) -> None:
        key = (slot["streamer"], target)
        if key in self.hold_logged:
            return
        self.hold_logged.add(key)
        t.ev("slot_hold_scheduled", slot=slot["id"], victim=slot["streamer"], target=target,
             hold_until=_iso(slot["hold_until"]))

    def _keep_step(self, t, state) -> None:
        absent = state == "absent"
        self._cleanup(t)
        if not absent:
            for s in self._keep_slots():
                login = s["streamer"]
                if not login or s["lent"] or s["confirmed_at"] is None:
                    continue
                started = self._started_epoch(login)
                start = max(s["confirmed_at"], started) if started is not None else s["confirmed_at"]
                if t.now >= start + t.M and not self._served_now(login):
                    self._mark_served(t, login, "keep")
        targets = self._targets(t)
        reserved = set()
        for target in targets:
            if self._keep_slot_of(target) is not None:
                continue
            current = self._slot_of(target)
            if current is not None and current["entry"] == "save":
                continue  # its save turn runs out first; save entries never take a Keep Open slot
            if current is not None and current["kind"] == "keep" and current["lent"]:
                # A1 (r1): the lent slot becomes the target's slot in place.
                current.update({"lent": False, "mode": None, "turn_ends_at": None,
                                "hold_until": None, "waiting": None})
                t.ev("slot_reclaimed", slot=current["id"], target=target, occupant=target,
                     how="in_place")
                continue
            empties = [s for s in self._keep_slots() if not s["streamer"]]
            if empties:
                if current is not None and current["kind"] == "cycle":
                    self._move(current, empties[0], None, False)  # S8 (a) relabel
                elif not t.paused:
                    usable = [s for s in empties if self._absent_ok(t, s, state)]
                    if usable:
                        self._assign(t, usable[0], target, "live", None, False)  # S8 (b)
                continue
            lent = [s for s in self._keep_slots() if s["lent"] and s["streamer"]]
            if lent:
                # A1 (r2): wait for the lent turn that ends first.
                if t.paused or absent or any(s["waiting"] == target for s in lent):
                    continue
                free = [s for s in lent if s["waiting"] is None]
                if not free:
                    continue
                pick = min(free, key=lambda s: (0, s["turn_ends_at"], s["id"])
                           if s["turn_ends_at"] is not None else (1, s["assigned_at"], s["id"]))
                pick["waiting"] = target
                pick["hold_until"] = pick["turn_ends_at"]
                self._log_hold(t, pick, target)
                continue
            # S8 (c), only with no lent slot (A1 r3)
            if t.paused or absent:
                continue
            occupants = [s for s in self._keep_slots() if s["streamer"] and not s["lent"]]
            victim = next((s for s in occupants if s["waiting"] == target), None)
            if victim is None:
                cands = [s for s in occupants if s["streamer"] not in targets
                         and s["id"] not in reserved and s["waiting"] is None]
                if not cands:
                    continue
                victim = max(cands, key=lambda s: (
                    t.rank(s["streamer"]),
                    -s["first_confirmed_at"] if s["first_confirmed_at"] is not None else -math.inf,
                    s["streamer"]))
            reserved.add(victim["id"])
            first = victim["first_confirmed_at"]
            if first is not None and t.now >= first + t.M:
                self._displace(t, victim, target)
            else:
                victim["waiting"] = target
                victim["hold_until"] = first + t.M if first is not None else None
                self._log_hold(t, victim, target)
        target_set = set(targets)
        for s in self._keep_slots():
            waiting = s["waiting"]
            if waiting and (waiting not in target_set or self._keep_slot_of(waiting) is not None):
                s["waiting"] = None
                s["hold_until"] = None
        # A hold is logged once while it lasts. Every path that ends one
        # (a freed or relabelled slot, a turn end, a displacement, a count
        # change) leaves no slot with that pair, so a later hold of the same
        # pair is logged again.
        self.hold_logged &= {(s["streamer"], s["waiting"]) for s in self.slots if s["waiting"]}

    def _displace(self, t, victim_slot, target) -> None:
        victim = victim_slot["streamer"]
        confirmed = victim_slot["confirmed_at"]
        if confirmed is not None and t.now >= confirmed + t.M and not self._served_now(victim):
            self._mark_served(t, victim, "keep")
        served = self._served_now(victim)
        record = dict(victim_slot)
        slot_id = victim_slot["id"]
        self._clear_slot(victim_slot)
        current = self._slot_of(target)
        if current is not None and current["kind"] == "cycle":
            self._move(current, victim_slot, None, False)
        else:
            self._assign(t, victim_slot, target, "live", None, False)
        t.ev("slot_displaced", slot=slot_id, victim=victim, target=target, served=served)
        self.hold_logged.discard((victim, target))
        # Rule 13: a victim that would be the idle pick moves into an idle or
        # empty rotating slot instead of closing, clock kept.
        dest = None
        if victim in t.live and victim not in self.dismissed and not self._queue(t):
            capacity = self._idle_capacity()
            desired = self._swap_order(t, include=(victim,))[:capacity]
            if victim in desired:
                empties = [s for s in self._cycle_slots() if not s["streamer"]]
                if empties:
                    dest = empties[0]
                else:
                    idle = [s for s in self._cycle_slots() if s["mode"] == "idle"
                            and s["streamer"] and s["streamer"] not in desired]
                    idle.sort(key=lambda s: t.order_key(s["streamer"]), reverse=True)
                    dest = idle[0] if idle else None
        if dest is None:
            self.recent_removals[victim] = ("displaced", t.now)
            return
        if dest["streamer"]:
            self._free(t, dest, "idle_swap")
        for key in _SLOT_FIELDS:
            if key not in ("id", "kind"):
                dest[key] = record[key]
        dest.update({"mode": "idle", "lent": False, "turn_ends_at": None,
                     "hold_until": None, "waiting": None})
        t.ev("slot_idle", slot=dest["id"], streamer=victim)

    # -- S9 ----------------------------------------------------------------

    def _stays_idle(self, t, slot) -> bool:
        capacity = self._idle_capacity(extra_slot=slot)
        return slot["streamer"] in self._swap_order(t, include=(slot["streamer"],))[:capacity]

    def _unplaced(self, t, queue) -> list:
        """A2 dry run of S10b: free cycle slots take any entry, free lendable
        Keep Open slots take live entries."""
        cycle = len([s for s in self._cycle_slots() if not s["streamer"]])
        lend = len(self._lendable_keep(t))
        left = []
        for entry in queue:
            if entry["entry"] == "live" and lend > 0:
                lend -= 1
            elif cycle > 0:
                cycle -= 1
            else:
                left.append(entry)
        return left

    def _rotating_step(self, t, state) -> None:
        absent = state == "absent"
        self._cleanup(t)
        for s in self._rotating_occupied():
            login = s["streamer"]
            if s["entry"] == "save":
                if login not in self.items:
                    self._free(t, s, "save_done")
                    continue
                current = self.tracked_live.get(login)
                if login in t.live and current is not None and current != s["session"]:
                    # Went live during its own save turn: same slot and clock.
                    s.update({"entry": "live", "url": _live_url(login), "session": current})
            elif s["mode"] == "turn":
                current = self.tracked_live.get(login)
                if current is not None:
                    s["session"] = current
        if not absent:
            for s in self._rotating_occupied():
                if (s["mode"] != "turn" or s["confirmed_at"] is None or s["turn_ends_at"] is None
                        or t.now < s["turn_ends_at"]):
                    continue
                login, entry = s["streamer"], s["entry"]
                if entry == "live":
                    self._mark_served(t, login, "turn")
                else:
                    self._complete(t, login, "turn_done")
                t.ev("slot_turn_over", slot=s["id"], streamer=login, entry=entry)
                if (entry == "live" and not s["lent"] and not self._queue(t)
                        and self._stays_idle(t, s)):
                    s["mode"] = "idle"
                    s["turn_ends_at"] = None
                    t.ev("slot_idle", slot=s["id"], streamer=login)
                else:
                    if s["lent"]:
                        t.freed_lent[s["id"]] = login
                    self._free(t, s, "turn_over")
        idle_slots = [s for s in self._cycle_slots() if s["streamer"] and s["mode"] == "idle"]
        for s in idle_slots:
            login = s["streamer"]
            current = self.tracked_live.get(login)
            started = self._started_epoch(login)
            if current is not None and current != s["session"]:
                # A new broadcast is unserved again: turn mode, in place.
                s["session"] = current
                s["mode"] = "turn"
                if s["confirmed_at"] is not None:
                    start = max(s["confirmed_at"], started) if started is not None else s["confirmed_at"]
                    s["turn_ends_at"] = start + t.M
                continue
            if not absent and s["confirmed_at"] is not None and not self._served_now(login):
                start = max(s["confirmed_at"], started) if started is not None else s["confirmed_at"]
                if t.now >= start + t.M:
                    self._mark_served(t, login, "turn")
        if t.paused or absent:
            return
        idle_slots = [s for s in self._cycle_slots() if s["streamer"] and s["mode"] == "idle"]
        queue = self._queue(t)
        if queue:
            # Rule 22 (O7, A2): a queue entry that no free slot can take
            # preempts an idle occupant, lowest-ranked first.
            victims = sorted(idle_slots, key=lambda s: t.order_key(s["streamer"]), reverse=True)
            for entry in self._unplaced(t, queue):
                if not victims:
                    break
                s = victims.pop(0)
                t.ev("slot_preempted", slot=s["id"], idle=s["streamer"], newcomer=entry["streamer"])
                self._free(t, s, "preempted")
            return
        # Rule 23 (A2): an idle tab outside the desired idle set gives way
        # once it has had M minutes.
        desired = set(self._swap_order(t)[:self._idle_capacity()])
        for s in idle_slots:
            login = s["streamer"]
            if login in desired or s["confirmed_at"] is None:
                continue
            started = self._started_epoch(login)
            start = max(s["confirmed_at"], started) if started is not None else s["confirmed_at"]
            if start + t.M <= t.now:
                self._free(t, s, "idle_swap")

    # -- S10 ---------------------------------------------------------------

    def _fill_step(self, t, state) -> None:
        if t.paused or state == "waiting":
            return
        # A1 pre-fill: every Keep Open target takes an empty Keep Open slot
        # before any is lent.
        for target in self._targets(t):
            if self._keep_slot_of(target) is not None:
                continue
            current = self._slot_of(target)
            if current is not None and (current["kind"] == "keep" or current["entry"] == "save"):
                continue
            empties = [s for s in self._keep_slots() if not s["streamer"]]
            if not empties:
                break
            if current is not None:
                dest = empties[0]
                self._move(current, dest, None, False)
                relabel = True
            else:
                usable = [s for s in empties if self._absent_ok(t, s, state)]
                if not usable:
                    continue
                dest = usable[0]
                self._assign(t, dest, target, "live", None, False, event=False)
                relabel = False
            if dest["id"] in t.freed_lent:
                t.ev("slot_reclaimed", slot=dest["id"], target=target,
                     occupant=t.freed_lent[dest["id"]], how="turn_end")
            elif not relabel:
                t.ev("slot_assigned", slot=dest["id"], streamer=target, entry="live",
                     mode=None, lent=False)
        # S10b
        queue = self._queue(t)
        free_cycle = [s for s in self._cycle_slots()
                      if not s["streamer"] and self._absent_ok(t, s, state)]
        lendable = [s for s in self._lendable_keep(t) if self._absent_ok(t, s, state)]
        waiting_head = False
        for entry in queue:
            if entry.get("withheld"):
                # Its rotating slot is held for it until the executor reports
                # the channel tab gone; no lower entry or idle pick takes it.
                # A lent Keep Open slot never takes a save entry, so live
                # entries may still borrow one.
                if free_cycle:
                    free_cycle.pop(0)
                waiting_head = True
            elif entry["entry"] == "live" and lendable:
                self._assign(t, lendable.pop(0), entry["streamer"], "live", "turn", True)
            elif free_cycle:
                self._assign(t, free_cycle.pop(0), entry["streamer"], entry["entry"], "turn", False)
            else:
                waiting_head = True
        if waiting_head:
            return
        # S10c: empty cycle slots take successive idle picks.
        for s in free_cycle:
            order = self._idle_pick_order(t)
            if not order:
                break
            self._assign(t, s, order[0], "live", "idle", False)
            t.ev("slot_idle", slot=s["id"], streamer=order[0])

    # -- S11 to S13 ----------------------------------------------------------

    def _close_list(self, t, state) -> list:
        for login in list(self.recent_removals):
            if not self._recently_removed(t, login):
                del self.recent_removals[login]
        if state != "alive" or t.rep is None:
            return []
        names = _streamers_of(t.rep)
        named = self._named()
        close = []
        for login in sorted(names):
            if login in named or login in self.draining:
                continue
            removal = self.recent_removals.get(login)
            if removal is not None:
                close.append({"streamer": login, "reason": removal[0]})
                continue
            if t.frozen:
                continue  # no live data to judge an extra by
            close.append({"streamer": login, "reason": "unplanned"})
            if login not in self.extras_closed_logged:
                self.extras_closed_logged.add(login)
                t.ev("slot_extra_closed", streamer=login)
        self.extras_closed_logged &= set(names)
        return close

    def _slot_public(self, s) -> dict:
        login = s["streamer"]
        item = self.items.get(login) if login else None
        save = bool(login) and s["entry"] == "save"
        return {
            "id": s["id"],
            "kind": s["kind"],
            "streamer": login,
            "entry": s["entry"] if login else None,
            "url": s["url"] if login else None,
            "mode": s["mode"] if login and (s["kind"] == "cycle" or s["lent"]) else None,
            "lent": bool(s["lent"]) and bool(login),
            "assigned_at": _floor(s["assigned_at"]) if login else None,
            "confirmed_at": _floor(s["confirmed_at"]) if login else None,
            "turn_ends_at": _floor(s["turn_ends_at"]) if login else None,
            "hold_until": _floor(s["hold_until"]) if login else None,
            "waiting": s["waiting"] if login else None,
            "item_kind": item["kind"] if save and item is not None else None,
            "deadline_at": _floor(item["deadline_at"]) if item is not None else None,
            "verify": bool(save and item is not None and item["verify"]),
        }

    def _publish(self, t, close, queue) -> dict:
        """S12: the plan of 3.3. seq moves only when a field the extension
        acts on changes."""
        state = self.state
        active = state in ("waiting", "alive")
        assigning = state == "alive" and not t.paused
        executor = None if state == "none_ever" else self.executor
        slots = [self._slot_public(s) for s in self.slots]
        acted = json.dumps([active, executor, assigning,
                            [[s["id"], s["streamer"], s["entry"]] for s in slots],
                            [[c["streamer"], c["reason"]] for c in close]])
        if acted != self.last_published:
            self.seq += 1
            self.last_published = acted
            t.ev("slot_plan_changed", seq=self.seq,
                 slots=[{"id": s["id"], "streamer": s["streamer"], "entry": s["entry"],
                         "mode": s["mode"], "lent": s["lent"]} for s in slots],
                 close=[dict(c) for c in close])
        self.last_close = [dict(c) for c in close]
        upcoming = []
        for s in self.slots:
            if s["streamer"]:
                upcoming += [s["turn_ends_at"], s["hold_until"]]
        upcoming += [d["until"] for d in self.draining.values()]
        upcoming = [v for v in upcoming if v is not None and v > t.now]
        return {
            "v": SLOT_PLAN_VERSION,
            "seq": self.seq,
            "generated_at": int(math.floor(t.now)),
            "live_as_of": _floor(t.live_as_of),
            "state": state,
            "active": active,
            "assigning": assigning,
            "pause": ("live" if t.auto_paused else "manual") if t.paused else None,
            "executor": executor,
            "turn_minutes": int(t.M // 60),
            "slots": slots,
            "close": [dict(c) for c in close],
            "queue": [{"streamer": e["streamer"], "entry": e["entry"],
                       "deadline_at": _floor(e["deadline_at"]), "urgent": e["urgent"],
                       "manual": e["manual"]} for e in queue[:SLOT_QUEUE_PUBLISH_MAX]],
            "served": sorted(self.served),
            "dismissed": sorted(self.dismissed),
            "draining": [{"streamer": k, "until": _floor(d["until"])}
                         for k, d in sorted(self.draining.items())],
            "next_change_at": _floor(min(upcoming)) if upcoming else None,
        }

    def _absent_opens(self, t) -> list:
        """S13 (rule 42, A9): the desktop opens the plan's untabbed streams,
        once per slot id per outage and at most K + C in all, never while
        paused."""
        if t.paused:
            return []
        reported = set()
        for rep in t.reports.values():
            if t.rep_alive(rep):
                reported |= _streamers_of(rep)
        opens = []
        for s in self.slots:
            login = s["streamer"]
            if not login or s["id"] in self.desktop_opened or login in reported:
                continue
            if self.outage_opens >= len(self.slots):
                break  # the A9 backstop; the carried marks keep it unused
            self.desktop_opened[s["id"]] = login
            self.desktop_opened_logins.add(login)
            self.outage_opens += 1
            opens.append({"slot": s["id"], "streamer": login, "url": s["url"],
                          "kind": "stream" if s["entry"] == "live" else "vod"})
            t.ev("slot_fallback_open", slot=s["id"], streamer=login, url=s["url"])
        return opens

    def _tooltip(self, t) -> str:
        if self.state == "absent":
            return TOOLTIP_ABSENT
        if self.state == "none_ever":
            return TOOLTIP_NONE_EVER
        if t.busy:
            return TOOLTIP_BUSY
        keep = [s["streamer"] for s in self._keep_slots() if s["streamer"] and not s["lent"]]
        rotating = []
        for s in self.slots:
            if not s["streamer"] or not (s["kind"] == "cycle" or s["lent"]):
                continue
            label = ("save " if s["entry"] == "save" else "") + s["streamer"]
            if s["mode"] == "idle":
                label += " idle"
            elif s["turn_ends_at"] is not None:
                label += " %dm" % max(0, math.ceil((s["turn_ends_at"] - t.now) / 60))
            else:
                label += " opening"
            rotating.append(label)
        text = "Slots: %s / rotating: %s / queue %d" % (
            ", ".join(keep) if keep else "none",
            ", ".join(rotating) if rotating else "empty",
            len(t.queue))
        if len(text) > SLOT_TOOLTIP_MAX:
            text = text[:SLOT_TOOLTIP_MAX - 3] + "..."
        return text

    def _persisted(self) -> str:
        data = self.to_state(0.0)
        del data["saved_at"]
        return json.dumps(data, sort_keys=True)

    def _finish(self, t, plan, opens) -> TickResult:
        sans = dict(plan)
        sans.pop("generated_at", None)
        sans.pop("live_as_of", None)
        sans_text = json.dumps(sans, sort_keys=True)
        snapshot = self._persisted()
        persist = sans_text != self.last_plan_sans or snapshot != self.last_state_snapshot
        self.last_plan_sans = sans_text
        self.last_state_snapshot = snapshot
        return TickResult(plan=plan, events=t.events, opens=opens, state=self.state,
                          notices=t.notices, persist=persist, absorbed=t.absorbed,
                          off_transition=None, tooltip=self._tooltip(t))
