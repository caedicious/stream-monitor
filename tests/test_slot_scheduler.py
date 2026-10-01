"""Slot mode scheduler (slot_scheduler.py, 1.12.0).

Pure tests, no threads: a fake clock, an inputs builder, and a fake executor
that applies each plan the way the extension does (the close list, then the
opens while the plan is assigning) and reports its tabs on the next tick. An
autouse wrapper runs the invariant checker after every tick: rule 4 (one tab
per slot, one slot per streamer, at most K + C named) and rule 47 (no slot
tab listed for closing before M confirmed minutes unless its reason is one
the rule allows), plus the plan shape of 3.3.
"""
import json

import pytest

import slot_scheduler as ss
import streak_saves as sv

BASE = 1790000000.0
MIN = 60
M = 30 * MIN
HOUR = 3600
KEY = "firefox-1a2b3c4d"
KEY_B = "firefox-0f0e0d0c"
CHROME_KEY = "chrome-0a0b0c0d"
STREAMERS = ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8")

PLAN_KEYS = {"v", "seq", "generated_at", "live_as_of", "state", "active", "assigning", "pause",
             "executor", "turn_minutes", "slots", "close", "queue", "served", "dismissed",
             "draining", "next_change_at"}
SLOT_KEYS = {"id", "kind", "streamer", "entry", "url", "mode", "lent", "assigned_at",
             "confirmed_at", "turn_ends_at", "hold_until", "waiting", "item_kind",
             "deadline_at", "verify"}
EARLY_OK = {"offline", "unlisted", "save_done"}


def iso(epoch):
    return sv.epoch_to_iso(epoch)


def layout(k, c):
    return [f"keep-{i}" for i in range(1, k + 1)] + [f"cycle-{i}" for i in range(1, c + 1)]


def _alive_names(inp):
    names = set()
    for rep in (inp.get("reports") or {}).values():
        if inp["mono"] - rep["mono"] <= ss.SLOT_EXECUTOR_ALIVE_SECONDS:
            names |= set(rep["streamers"])
    return names


def _check_invariants(sched, inp, res, before, before_layout, draining_before, last_seq):
    assert res.state == sched.state
    if res.state == "off":
        assert res.plan is None and res.tooltip is None
        return
    plan = res.plan
    assert set(plan) == PLAN_KEYS
    assert plan["state"] == res.state
    assert plan["seq"] >= last_seq
    k, c, m, now = inp["K"], inp["C"], inp["M"], inp["now"]
    assert [s["id"] for s in plan["slots"]] == layout(k, c)
    named = [s["streamer"] for s in plan["slots"] if s["streamer"]]
    # Rule 4: one tab per slot, one slot per streamer, at most K + C named.
    assert len(named) == len(set(named)) <= k + c
    for s in plan["slots"]:
        assert set(s) == SLOT_KEYS
        if s["streamer"] is None:
            assert s["entry"] is None and s["mode"] is None and s["lent"] is False
        elif s["kind"] == "keep" and not s["lent"]:
            assert s["entry"] == "live" and s["mode"] is None
        elif s["lent"]:
            assert s["kind"] == "keep" and s["entry"] == "live" and s["mode"] == "turn"
        else:
            assert s["mode"] in ("turn", "idle")
            if s["entry"] == "save":
                assert s["mode"] == "turn"
    assert not set(named) & {d["streamer"] for d in plan["draining"]}
    closed = [x["streamer"] for x in plan["close"]]
    assert len(closed) == len(set(closed))
    assert not set(closed) & set(named)
    drained = before_layout != layout(k, c)
    # Rule 47: the minimum dwell.
    for entry in plan["close"]:
        reason = entry["reason"]
        assert reason in ss.CLOSE_REASONS
        rec = before.get(entry["streamer"])
        if rec is None:
            continue
        assert reason != "unplanned", "a slot tab is never listed as an unplanned extra"
        if reason in EARLY_OK:
            continue
        if reason in ("preempted", "idle_swap"):
            assert rec["mode"] == "idle", entry
            continue
        if reason == "displaced":
            first = rec["first_confirmed_at"]
            assert first is not None and now >= first + m, entry
            continue
        assert reason == "turn_over"
        if drained or entry["streamer"] in draining_before or rec["mode"] == "idle":
            continue
        assert rec["turn_ends_at"] is not None and now >= rec["turn_ends_at"], entry
    # Rule 32: nothing opens while paused (a relabel or an adopted tab is not an open).
    if inp.get("paused"):
        new = set(named) - set(before)
        assert new <= _alive_names(inp), new


@pytest.fixture(autouse=True)
def invariant_checker(monkeypatch):
    real_tick = ss.SlotScheduler.tick

    def checked_tick(self, inp):
        before = {s["streamer"]: dict(s) for s in self.slots if s["streamer"]}
        for login, drain in self.draining.items():
            before[login] = dict(drain["record"])
        before_layout = [s["id"] for s in self.slots]
        draining_before = set(self.draining)
        last_seq = self.seq
        res = real_tick(self, inp)
        _check_invariants(self, inp, res, before, before_layout, draining_before, last_seq)
        return res

    monkeypatch.setattr(ss.SlotScheduler, "tick", checked_tick)


class Rig:
    """A scheduler, a fake clock, the desktop's inputs and a fake executor."""

    def __init__(self, streamers=STREAMERS, pinned=(), k=2, c=1, m=M, auto_save=True,
                 check_interval=60, default_browser="firefox"):
        self.sched = ss.SlotScheduler()
        self.now = BASE
        self.mono = 1000.0
        self.streamers = list(streamers)
        self.pinned = set(pinned)
        self.k, self.c, self.m = k, c, m
        self.auto_save = auto_save
        self.check_interval = check_interval
        self.default_browser = default_browser
        self.slot_mode = True
        self.live = {}
        self.tabs = set()
        self.key = KEY
        self.browser = "firefox"
        self.extension = True
        self.capable = True
        self.busy = None
        self.reports = {}
        self.gone = []
        self.saves = {}
        self.watch_start = {}
        self.paused = False
        self.auto_paused = False
        self.helix_ok = True
        self.live_as_of = None
        self.results = []
        self.plan = None
        self.absorb = {}
        self.seed_capable = False

    # the world
    def go_live(self, *logins, started=None):
        for login in logins:
            self.live[login] = started or iso(self.now)

    def go_offline(self, *logins):
        for login in logins:
            self.live.pop(login, None)

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds

    def close_tab(self, login, reason="user_closed"):
        self.tabs.discard(login)
        self.gone.append({"key": self.key, "streamer": login, "reason": reason,
                          "at": int(self.now)})

    # the desktop
    def inputs(self, authoritative=True, **over):
        poll = None
        if authoritative and self.helix_ok:
            self.live_as_of = self.now
            poll = {x: s for x, s in self.live.items() if x in self.streamers}
        inp = {
            "now": self.now, "mono": self.mono,
            "authoritative": bool(authoritative and self.helix_ok),
            "poll": poll, "live_as_of": self.live_as_of,
            "check_interval": self.check_interval,
            "rank": {x: i for i, x in enumerate(self.streamers)},
            "listed": frozenset(self.streamers),
            "pinned": frozenset(self.pinned) & frozenset(self.streamers),
            "slot_mode": self.slot_mode, "auto_save": self.auto_save,
            "K": self.k, "C": self.c, "M": self.m,
            "paused": self.paused or self.auto_paused, "auto_paused": self.auto_paused,
            "reports": dict(self.reports), "gone": list(self.gone),
            "default_browser": self.default_browser,
            "saves": dict(self.saves), "watch_start": dict(self.watch_start),
            "wake_gap": 0, "seed_capable": self.seed_capable, "absorb": self.absorb,
        }
        inp.update(over)
        return inp

    def refresh_report(self):
        if self.extension:
            self.reports[self.key] = {
                "browser": self.browser, "streamers": frozenset(self.tabs),
                "epoch": self.now, "mono": self.mono,
                "plan_seq": self.sched.seq if self.capable else None, "busy": self.busy,
            }

    def tick(self, authoritative=True, apply=True, report=True, **over):
        if report:
            self.refresh_report()
        res = self.sched.tick(self.inputs(authoritative, **over))
        self.gone = []
        self.seed_capable = False
        self.results.append(res)
        self.plan = res.plan
        if apply:
            self.apply(res)
        return res

    def apply(self, res):
        plan = res.plan
        if not (self.extension and plan and plan["active"] and plan["executor"] == self.key
                and not self.busy):
            return
        for entry in plan["close"]:
            self.tabs.discard(entry["streamer"])
        if plan["assigning"]:
            for s in plan["slots"]:
                if s["streamer"]:
                    self.tabs.add(s["streamer"])

    def step(self, seconds=MIN, **kw):
        self.advance(seconds)
        return self.tick(**kw)

    def run(self, seconds, every=MIN, **kw):
        out = []
        end = self.now + seconds
        while self.now + every <= end + 1e-6:
            out.append(self.step(every, **kw))
        return out

    # reading the plan
    def slot(self, slot_id):
        return next(s for s in self.plan["slots"] if s["id"] == slot_id)

    def occ(self):
        return {s["id"]: s["streamer"] for s in self.plan["slots"]}

    def holder(self, login):
        return next((s for s in self.plan["slots"] if s["streamer"] == login), None)

    def events(self, name, results=None):
        results = self.results if results is None else results
        return [fields for r in results for (n, fields) in r.events if n == name]

    def closes(self):
        return {c["streamer"]: c["reason"] for c in self.plan["close"]}

    def queue(self):
        return [(e["streamer"], e["entry"]) for e in self.plan["queue"]]

    def item_inp(self):
        return self.inputs(authoritative=False)


def card_item(login, now, age_s=None, status="broke", count=5, hours=None, unit=None):
    extras = {"card_age_s": age_s, "card_age_unit_s": unit, "source": "bell", "deadline_at": None}
    if age_s is not None and unit is None:
        extras["card_age_unit_s"] = sv.infer_age_unit(age_s)
    return sv.make_card_item(login, status, count, now, hours, extras, now)


# ---------------------------------------------------------------------------
# Mode, capable extension, counts (rules 2, 3, 4)
# ---------------------------------------------------------------------------

def test_r02_r43_no_capable_report_keeps_none_ever_with_an_inactive_plan():
    rig = Rig()
    rig.capable = False
    rig.go_live("s1", "s2")
    rig.tabs = {"s1", "s2"}
    res = rig.tick()
    assert res.state == "none_ever"
    assert res.plan["active"] is False and res.plan["assigning"] is False
    assert res.plan["executor"] is None
    assert all(s["streamer"] is None for s in res.plan["slots"])
    assert res.notices == ["none_ever"]
    assert res.tooltip == ss.TOOLTIP_NONE_EVER


def test_r02_a_bare_browser_key_never_executes():
    rig = Rig()
    rig.extension = False
    rig.reports = {"firefox": {"browser": "firefox", "streamers": frozenset(), "epoch": rig.now,
                               "mono": rig.mono, "plan_seq": 3, "busy": None}}
    assert rig.tick().state == "none_ever"


def test_r02_a_capable_report_activates_slot_mode():
    rig = Rig()
    rig.go_live("s1")
    res = rig.tick()
    assert res.state == "alive"
    assert res.plan["active"] is True and res.plan["executor"] == KEY
    assert rig.events("slot_mode_active") == [{"executor": KEY, "keep": 2, "cycle": 1, "minutes": 30}]
    assert res.notices == ["activated"] and res.absorbed is True
    assert rig.sched.executor_seen is True


@pytest.mark.parametrize("k,c", [(2, 1), (0, 3), (1, 2)])
def test_r03_count_variants_lay_out_their_slots_and_fill_them(k, c):
    rig = Rig(k=k, c=c, pinned={"s1", "s2"})
    rig.go_live("s1", "s2", "s3", "s4", "s5")
    rig.tick()
    assert [s["id"] for s in rig.plan["slots"]] == layout(k, c)
    keep_holders = [s["streamer"] for s in rig.plan["slots"] if s["kind"] == "keep" and not s["lent"]]
    assert keep_holders == ["s1", "s2"][:k]
    assert len([s for s in rig.plan["slots"] if s["streamer"]]) == 3
    assert all(s["kind"] == "cycle" or s["streamer"] in ("s1", "s2")
               for s in rig.plan["slots"] if s["streamer"] and not s["lent"])


def test_r04_the_plan_never_names_more_than_k_plus_c_streamers():
    rig = Rig(pinned={"s1", "s3", "s5", "s7"})
    rig.go_live(*STREAMERS)
    rig.tick()
    for _ in range(200):
        rig.step()
        named = [s["streamer"] for s in rig.plan["slots"] if s["streamer"]]
        assert len(named) <= 3
        assert len(rig.tabs) <= 3


# ---------------------------------------------------------------------------
# Rank, live status, freezing (rules 5 to 8)
# ---------------------------------------------------------------------------

def test_r05_rank_orders_live_entries_and_unlisted_saves_rank_after_listed_ones():
    rig = Rig(k=0, c=1)
    rig.go_live("s5", "s2", "s7", "s4")
    rig.tick()
    assert rig.occ()["cycle-1"] == "s2"
    assert rig.queue() == [("s4", "live"), ("s5", "live"), ("s7", "live")]
    far = rig.now + 20 * HOUR
    for login in ("zed", "s8"):
        item = sv.make_manual_item(login, "broke", 3, rig.now, far, rig.now)
        item["origin"] = "card"
        rig.sched.add_item(item, rig.item_inp())
    rig.step()
    assert rig.queue()[-2:] == [("s8", "save"), ("zed", "save")]


def test_r06_a_new_started_at_is_a_new_broadcast_and_starts_unserved():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(M + MIN)
    assert "s1" in rig.plan["served"]
    rig.run(M + MIN)
    assert "s2" in rig.plan["served"]
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert "s1" not in rig.plan["served"]
    assert rig.sched.tracked_session("s1") == iso(rig.now - MIN)
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["mode"] == "turn"


def test_r07_offline_needs_two_misses_and_replans_add_no_strike():
    rig = Rig(pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.run(5 * MIN)
    before = rig.occ()
    sessions = dict(rig.live)
    rig.go_offline("s1", "s3")
    rig.step()
    for _ in range(10):
        rig.step(30, authoritative=False)
    assert rig.occ() == before
    assert rig.plan["close"] == []
    rig.live = sessions
    rig.step()
    rig.go_offline("s3")
    rig.step()
    rig.step()
    assert rig.holder("s3") is None
    assert rig.closes().get("s3") == "offline"
    assert rig.holder("s1") is not None


def test_r07_a_one_poll_omission_changes_no_slot_hold_close_served_or_dismissal():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s2", "s3", "s4")
    rig.tick()
    rig.step()
    rig.close_tab("s4")
    rig.step()
    rig.go_live("s1")
    rig.step()
    snapshot = (rig.occ(), rig.slot("keep-1")["waiting"], rig.plan["dismissed"])
    assert snapshot[1] == "s1" and snapshot[2] == ["s4"]
    saved = dict(rig.live)
    rig.live = {}
    rig.step()
    assert (rig.occ(), rig.slot("keep-1")["waiting"], rig.plan["dismissed"]) == snapshot
    assert rig.plan["close"] == []
    rig.live = saved
    rig.step()
    assert (rig.occ(), rig.slot("keep-1")["waiting"], rig.plan["dismissed"]) == snapshot


def test_r07_a_failed_poll_is_not_a_poll():
    rig = Rig(k=0, c=1)
    rig.go_live("s1")
    rig.tick()
    rig.step()
    rig.go_offline("s1")
    rig.helix_ok = False
    for _ in range(4):
        rig.step()
    assert rig.holder("s1") is not None


def test_r08_stale_live_data_freezes_the_plan():
    rig = Rig(k=0, c=1)
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    rig.helix_ok = False
    rig.run(M + 10 * MIN)
    assert rig.occ()["cycle-1"] == "s1"
    assert rig.events("slot_turn_over") == []
    assert len(rig.events("slot_frozen")) == 1
    assert rig.events("slot_frozen")[0] == {"reason": "stale_live"}
    assert rig.plan["generated_at"] == int(rig.now)
    rig.helix_ok = True
    rig.step()
    assert rig.events("slot_turn_over")[0]["streamer"] == "s1"
    assert rig.occ()["cycle-1"] == "s2"


def test_r08_r45_with_check_interval_600_the_plan_stays_fresh_and_turns_end_on_time():
    rig = Rig(k=0, c=1, check_interval=600)
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step(30, authoritative=False)
    confirmed = rig.slot("cycle-1")["confirmed_at"]
    assert confirmed is not None
    ends = rig.slot("cycle-1")["turn_ends_at"]
    ticks = 0
    while rig.now < ends:
        ticks += 1
        authoritative = ticks % 20 == 0
        rig.step(30, authoritative=authoritative)
    assert rig.events("slot_frozen") == []
    assert rig.events("slot_turn_over")[0]["streamer"] == "s1"
    assert rig.now - ends < 30


# ---------------------------------------------------------------------------
# Keep Open slots (rules 9 to 15, O4, O5)
# ---------------------------------------------------------------------------

def test_r09_keep_open_targets_are_the_first_k_live_pins_by_rank():
    rig = Rig(pinned={"s1", "s3", "s5"})
    rig.go_live("s5", "s3", "s1", "s2")
    rig.tick()
    assert rig.occ()["keep-1"] == "s1" and rig.occ()["keep-2"] == "s3"
    assert rig.occ()["cycle-1"] == "s2"
    assert ("s5", "live") in rig.queue()


def test_o05_r09_keep_open_streamers_beyond_k_take_rotating_turns():
    rig = Rig(pinned={"s1", "s2", "s3"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s2", "cycle-1": "s3"}
    assert rig.slot("cycle-1")["mode"] == "turn"
    assert rig.queue() == [("s4", "live")]


def test_r10_a_target_in_the_rotating_slot_relabels_into_a_free_keep_slot():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    cycle = dict(rig.slot("cycle-1"))
    rig.run(4 * MIN)
    rig.go_offline("s1")
    rig.step()
    res = rig.step()
    assert rig.occ()["keep-1"] == "s2"
    keep = rig.slot("keep-1")
    assert keep["confirmed_at"] == cycle["confirmed_at"] and keep["assigned_at"] == cycle["assigned_at"]
    assert keep["mode"] is None and keep["turn_ends_at"] is None
    assert "s2" not in rig.closes()
    assert [e for e in rig.events("slot_assigned", [res]) if e["streamer"] == "s2"] == []


def test_r11_a_pin_that_is_not_a_target_never_displaces():
    rig = Rig(k=1, c=1, pinned={"s2", "s3"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s2")
    rig.tick()
    rig.run(40 * MIN)
    rig.go_live("s3")
    rig.run(5 * MIN)
    assert rig.occ()["keep-1"] == "s2"
    assert rig.slot("keep-1")["waiting"] is None
    assert rig.events("slot_hold_scheduled") == [] and rig.events("slot_displaced") == []


def test_r11_an_unpinned_occupant_ranked_above_the_target_is_displaceable():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1")
    rig.tick()
    rig.run(40 * MIN)
    rig.pinned = {"s3"}
    rig.go_live("s3")
    rig.step()
    assert rig.events("slot_displaced") == [
        {"slot": "keep-1", "victim": "s1", "target": "s3", "served": True}]
    assert rig.occ()["keep-1"] == "s3"


def test_r12_o04_a_young_victim_gets_a_hold_that_resolves_at_first_confirmed_plus_m():
    rig = Rig(pinned={"s2", "s3", "s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s2", "s3", "s4")
    rig.tick()
    rig.step()
    first = rig.slot("keep-2")["confirmed_at"]
    rig.run(11 * MIN)
    rig.go_live("s1")
    rig.step()
    keep2 = rig.slot("keep-2")
    assert keep2["streamer"] == "s3" and keep2["waiting"] == "s1"
    assert keep2["hold_until"] == first + M
    assert rig.events("slot_hold_scheduled") == [
        {"slot": "keep-2", "victim": "s3", "target": "s1", "hold_until": iso(first + M)}]
    while rig.now + MIN < first + M:
        rig.step()
        assert rig.occ()["keep-2"] == "s3"
        assert len(rig.tabs) <= 3
    rig.step(first + M - rig.now)
    assert rig.events("slot_displaced")[0]["victim"] == "s3"
    assert "s1" in (rig.occ()["keep-1"], rig.occ()["keep-2"])
    assert len(rig.events("slot_hold_scheduled")) == 1


def test_r12_displacement_is_immediate_when_the_victim_already_had_m():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.run(M + 2 * MIN)
    rig.go_live("s1")
    rig.step()
    assert rig.occ()["keep-1"] == "s1"
    assert rig.events("slot_hold_scheduled") == []
    assert rig.events("slot_displaced")[0]["served"] is True


def test_r12_o04_an_unconfirmed_victim_gets_a_hold_not_a_close():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick(apply=False)
    rig.go_live("s1")
    rig.step(apply=False)
    keep = rig.slot("keep-1")
    assert keep["streamer"] == "s2" and keep["confirmed_at"] is None
    assert keep["waiting"] == "s1" and keep["hold_until"] is None
    assert rig.plan["close"] == []
    assert rig.events("slot_hold_scheduled")[0]["hold_until"] is None
    rig.tabs.add("s2")
    rig.step()
    keep = rig.slot("keep-1")
    assert keep["hold_until"] == keep["confirmed_at"] + M
    rig.run(3 * MIN)
    assert rig.slot("keep-1")["waiting"] == "s1"
    assert len(rig.events("slot_hold_scheduled")) == 1


@pytest.mark.parametrize("how", ["window_closed", "silent_loss"])
def test_r12_a_reissued_victim_keeps_its_first_confirmed_at_and_its_hold(how):
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.step()
    first = rig.slot("keep-1")["confirmed_at"]
    rig.run(5 * MIN)
    rig.go_live("s1")
    rig.step()
    assert rig.slot("keep-1")["hold_until"] == first + M
    if how == "window_closed":
        rig.close_tab("s2", "window_closed")
    else:
        rig.tabs.discard("s2")
    rig.step(apply=False)
    assert rig.events("slot_open_reissued")[-1]["why"] == how
    assert rig.slot("keep-1")["confirmed_at"] is None
    assert rig.slot("keep-1")["hold_until"] == first + M
    rig.tabs.add("s2")
    rig.step()
    assert rig.slot("keep-1")["confirmed_at"] > first
    assert rig.slot("keep-1")["hold_until"] == first + M
    rig.step(first + M - rig.now)
    assert rig.occ()["keep-1"] == "s1"


def test_r12_the_hold_is_cancelled_when_the_target_goes_offline():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.go_live("s1")
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    rig.go_offline("s1")
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    rig.step()
    assert rig.slot("keep-1")["waiting"] is None and rig.slot("keep-1")["hold_until"] is None
    rig.run(40 * MIN)
    assert rig.occ()["keep-1"] == "s2"


def test_r12_a_victim_that_goes_offline_during_a_hold_is_replaced_at_once():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.go_live("s1")
    rig.step()
    rig.go_offline("s2")
    rig.step()
    rig.step()
    assert rig.occ()["keep-1"] in ("s1",)
    assert rig.closes().get("s2") == "offline"


def test_r12_two_targets_hold_different_victims():
    rig = Rig(k=2, c=1, pinned={"s3", "s4"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s3", "s4")
    rig.tick()
    rig.step()
    assert rig.occ()["keep-1"] == "s3" and rig.occ()["keep-2"] == "s4"
    assert not rig.slot("keep-1")["lent"] and not rig.slot("keep-2")["lent"]
    rig.pinned = {"s1", "s2"}
    rig.go_live("s1", "s2")
    rig.step()
    holds = rig.events("slot_hold_scheduled")
    assert sorted((h["target"], h["victim"]) for h in holds) == [("s1", "s4"), ("s2", "s3")]
    assert {rig.slot("keep-1")["waiting"], rig.slot("keep-2")["waiting"]} == {"s1", "s2"}
    assert len(rig.tabs) <= 3


def _holds(rig):
    return [(h["victim"], h["target"]) for h in rig.events("slot_hold_scheduled")]


def test_r12_a_new_hold_of_the_same_pair_is_logged_again_after_the_victim_went_offline():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"), auto_save=False)
    rig.go_live("s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.go_live("s1")
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    rig.go_offline("s2")
    rig.run(3 * MIN)
    assert rig.occ()["keep-1"] == "s1"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    rig.go_live("s2", started=iso(rig.now))
    rig.run(5 * MIN)
    assert rig.occ()["keep-1"] == "s2"
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    assert _holds(rig) == [("s2", "s1"), ("s2", "s1")]


def test_r12_o06_a_new_hold_of_the_same_pair_is_logged_again_after_a_lent_turn_end():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"), auto_save=False)
    rig.go_live("s2")
    rig.tick()
    rig.run(2 * MIN)
    rig.go_live("s3")
    rig.step()
    rig.go_live("s1")
    rig.step()
    assert rig.occ() == {"keep-1": "s2", "cycle-1": "s3"}
    assert rig.slot("keep-1")["lent"] and rig.slot("keep-1")["waiting"] == "s1"
    rig.run(M)
    assert rig.occ()["keep-1"] == "s1" and not rig.slot("keep-1")["lent"]
    rig.run(5 * MIN)
    assert rig.occ()["cycle-1"] == "s2" and rig.slot("cycle-1")["mode"] == "idle"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    rig.pinned = {"s1", "s2"}
    rig.step()
    assert rig.occ()["keep-1"] == "s2"
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    assert _holds(rig) == [("s2", "s1"), ("s2", "s1")]


def test_r12_am35_a_new_hold_of_the_same_pair_is_logged_again_after_a_count_change():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"), auto_save=False)
    rig.go_live("s2")
    rig.tick()
    rig.run(2 * MIN)
    rig.go_live("s1")
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    rig.k = 2
    rig.step()
    assert rig.occ()["keep-1"] == "s2" and rig.occ()["keep-2"] == "s1"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    rig.k = 1
    rig.step()
    assert rig.occ()["keep-1"] == "s2"
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    assert _holds(rig) == [("s2", "s1"), ("s2", "s1")]


def test_r13_a_displaced_victim_with_m_is_served_and_moves_to_an_idle_slot_with_its_clock():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.run(M + 5 * MIN)
    assert rig.slot("cycle-1")["streamer"] == "s3" and rig.slot("cycle-1")["mode"] == "idle"
    keep = dict(rig.slot("keep-1"))
    rig.go_live("s1")
    res = rig.step()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    cycle = rig.slot("cycle-1")
    assert cycle["mode"] == "idle" and cycle["confirmed_at"] == keep["confirmed_at"]
    assert rig.closes() == {"s3": "idle_swap"}
    assert [e for e in rig.events("slot_assigned", [res]) if e["streamer"] == "s2"] == []
    assert rig.events("slot_displaced", [res])[0]["served"] is True
    assert "s2" in rig.plan["served"]


def test_r13_a_displaced_victim_that_is_not_the_idle_pick_closes():
    rig = Rig(k=1, c=1, pinned={"s1", "s3"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s3", "s2")
    rig.tick()
    rig.run(M + 5 * MIN)
    assert rig.slot("cycle-1")["streamer"] == "s2"
    rig.go_live("s1")
    rig.step()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    assert rig.closes() == {"s3": "displaced"}


@pytest.mark.parametrize("since_restart", [M + 2 * MIN, 10 * MIN])
def test_r06_r13_a_displaced_keep_occupant_that_restarted_stays_idle_and_yields_to_a_newcomer(since_restart):
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.run(M + 5 * MIN)
    rig.go_live("s2", started=iso(rig.now))
    rig.run(since_restart)
    rig.go_live("s1")
    rig.step()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    assert rig.slot("cycle-1")["mode"] == "idle"
    mark = len(rig.results) - 1
    rig.run(5 * MIN)
    assert rig.slot("cycle-1")["mode"] == "idle" and rig.slot("cycle-1")["turn_ends_at"] is None
    assert rig.events("slot_turn_over", rig.results[mark:]) == []
    rig.go_live("s3")
    res = rig.step()
    assert rig.events("slot_preempted", [res]) == [{"slot": "cycle-1", "idle": "s2", "newcomer": "s3"}]
    assert rig.occ()["cycle-1"] == "s3"


def test_r14_an_unpinned_occupant_stays_without_demand_and_an_unlisted_one_closes():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.pinned = set()
    rig.run(40 * MIN)
    assert rig.occ()["keep-1"] == "s1"
    rig.streamers = ["s2", "s3"]
    rig.step()
    assert rig.holder("s1") is None
    assert rig.closes()["s1"] == "unlisted"


def test_r14_a_rotating_occupant_no_longer_listed_closes_as_unlisted_and_gets_no_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.streamers = ["s2"]
    rig.step()
    assert rig.closes() == {"s1": "unlisted"} and rig.occ()["cycle-1"] == "s2"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert rig.sched.items_snapshot() == {}


def test_r15_a_keep_open_occupant_is_served_after_m_minutes():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.step()
    confirmed = rig.slot("keep-1")["confirmed_at"]
    rig.run(M - 2 * MIN)
    assert "s1" not in rig.plan["served"]
    rig.step(confirmed + M - rig.now)
    assert "s1" in rig.plan["served"]
    assert rig.events("slot_served") == [{"streamer": "s1", "how": "keep", "session": iso(BASE)}]
    assert rig.occ()["keep-1"] == "s1"


# ---------------------------------------------------------------------------
# Lent Keep Open slots (rule 16 as amended by A1, O6)
# ---------------------------------------------------------------------------

def test_r16_o06_with_nobody_pinned_every_slot_rotates_and_only_cycle_slots_idle():
    rig = Rig(streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s2", "cycle-1": "s3"}
    assert rig.slot("keep-1")["lent"] and rig.slot("keep-2")["lent"]
    assert not rig.slot("cycle-1")["lent"]
    rig.run(M + 3 * MIN)
    assert rig.slot("keep-1")["streamer"] is None and rig.slot("keep-2")["streamer"] is None
    assert rig.slot("cycle-1")["mode"] == "idle" and rig.slot("cycle-1")["streamer"] == "s1"


def test_o06_r16_seven_live_streams_with_nobody_pinned_rotate_three_at_a_time():
    rig = Rig(streamers=STREAMERS[:7])
    rig.go_live(*STREAMERS[:7])
    rig.tick()
    for _ in range(3 * 32):
        rig.step()
        assert len([s for s in rig.plan["slots"] if s["streamer"]]) <= 3
    assert set(rig.plan["served"]) == set(STREAMERS[:7])
    assert len(rig.events("slot_turn_over")) == 7


def test_r16_o06_targets_take_their_keep_slots_before_any_is_lent():
    rig = Rig(k=2, c=1, pinned={"s3"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    assert rig.occ() == {"keep-1": "s3", "keep-2": "s1", "cycle-1": "s2"}
    assert rig.slot("keep-1")["lent"] is False and rig.slot("keep-2")["lent"] is True


def test_r16_o06_a_lent_slot_takes_live_entries_only():
    # The lent turn ends while the cycle turn still runs and a save item
    # waits: the free Keep Open slot stays empty until cycle-1 frees.
    rig = Rig(k=1, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    assert rig.slot("keep-1")["streamer"] == "s1" and rig.slot("keep-1")["lent"]
    rig.run(10 * MIN)
    rig.go_live("s2")
    rig.step()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    rig.sched.add_item(card_item("zed", rig.now, age_s=2 * HOUR), rig.item_inp())
    rig.step()
    cycle_ends = rig.slot("cycle-1")["turn_ends_at"]
    lent_ends = rig.slot("keep-1")["turn_ends_at"]
    assert lent_ends < cycle_ends - 5 * MIN
    assert rig.queue() == [("zed", "save")] and rig.plan["queue"][0]["urgent"] is False
    rig.run(lent_ends - rig.now)
    assert rig.occ() == {"keep-1": None, "cycle-1": "s2"}
    while rig.now + MIN < cycle_ends:
        rig.step()
        assert rig.occ() == {"keep-1": None, "cycle-1": "s2"}
    res = rig.step(cycle_ends - rig.now)
    assert rig.events("slot_turn_over", [res]) == [{"slot": "cycle-1", "streamer": "s2", "entry": "live"}]
    assert rig.occ() == {"keep-1": None, "cycle-1": "zed"}
    for r in rig.results:
        for s in r.plan["slots"]:
            if s["kind"] == "keep":
                assert s["entry"] in (None, "live")
    zed = [e for e in rig.events("slot_assigned") if e["streamer"] == "zed"]
    assert zed == [{"slot": "cycle-1", "streamer": "zed", "entry": "save", "mode": "turn", "lent": False}]


def test_r16_o06_a_lent_slot_never_idles():
    rig = Rig(k=1, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2"}
    rig.run(M + 3 * MIN)
    assert rig.slot("keep-1")["streamer"] is None
    assert rig.slot("cycle-1")["mode"] == "idle"
    assert rig.closes().get("s1") in (None, "turn_over")


def test_r16_o06_a_target_holding_a_lent_slot_reclaims_it_in_place():
    rig = Rig(k=1, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.run(5 * MIN)
    lent = dict(rig.slot("keep-1"))
    assert lent["streamer"] == "s2" and lent["lent"]
    rig.pinned = {"s2"}
    res = rig.step()
    keep = rig.slot("keep-1")
    assert keep["streamer"] == "s2" and not keep["lent"] and keep["mode"] is None
    assert keep["confirmed_at"] == lent["confirmed_at"] and keep["turn_ends_at"] is None
    assert rig.events("slot_reclaimed", [res]) == [
        {"slot": "keep-1", "target": "s2", "occupant": "s2", "how": "in_place"}]
    assert rig.plan["close"] == []


def test_r16_o06_a_waiting_target_takes_the_lent_slot_when_its_turn_ends_in_the_same_tick():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.step()
    ends = rig.slot("keep-1")["turn_ends_at"]
    rig.run(3 * MIN)
    rig.go_live("s1")
    rig.step()
    keep = rig.slot("keep-1")
    assert keep["streamer"] == "s2" and keep["waiting"] == "s1" and keep["hold_until"] == ends
    assert rig.events("slot_hold_scheduled")[-1] == {
        "slot": "keep-1", "victim": "s2", "target": "s1", "hold_until": iso(ends)}
    while rig.now + MIN < ends:
        rig.step()
    res = rig.step(ends - rig.now)
    assert rig.occ()["keep-1"] == "s1" and not rig.slot("keep-1")["lent"]
    assert rig.events("slot_reclaimed", [res]) == [
        {"slot": "keep-1", "target": "s1", "occupant": "s2", "how": "turn_end"}]
    assert rig.events("slot_served", [res]) == [
        {"streamer": "s2", "how": "turn", "session": iso(BASE)},
        {"streamer": "s3", "how": "turn", "session": iso(BASE)}]
    # s1 was queued, so s3's turn closed; the empty rotating slot then idles
    # on the highest-ranked stream left, s2.
    assert rig.closes() == {"s3": "turn_over"}
    assert rig.occ()["cycle-1"] == "s2" and rig.slot("cycle-1")["mode"] == "idle"


def test_r16_o06_a_waiting_target_takes_a_rotating_turn_meanwhile_then_relabels():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2")
    rig.tick()
    rig.step()
    lent_ends = rig.slot("keep-1")["turn_ends_at"]
    rig.run(9 * MIN)
    rig.go_live("s3")
    rig.run(2 * MIN)
    assert rig.occ() == {"keep-1": "s2", "cycle-1": "s3"}
    rig.go_live("s1")
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"
    rig.go_offline("s3")
    rig.run(3 * MIN)
    assert rig.occ()["cycle-1"] == "s1"
    cycle = dict(rig.slot("cycle-1"))
    while rig.now + MIN < lent_ends:
        rig.step()
    res = rig.step(lent_ends - rig.now)
    assert rig.occ()["keep-1"] == "s1"
    keep = rig.slot("keep-1")
    assert keep["confirmed_at"] == cycle["confirmed_at"] and keep["mode"] is None
    assert rig.events("slot_reclaimed", [res]) == [
        {"slot": "keep-1", "target": "s1", "occupant": "s2", "how": "turn_end"}]
    assert "s1" not in rig.closes()


def test_r16_o06_no_hold_is_scheduled_while_paused():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.run(3 * MIN)
    rig.paused = True
    rig.go_live("s1")
    rig.run(3 * MIN)
    assert rig.slot("keep-1")["waiting"] is None
    assert rig.events("slot_hold_scheduled") == []
    rig.paused = False
    rig.step()
    assert rig.slot("keep-1")["waiting"] == "s1"


# ---------------------------------------------------------------------------
# Queue, turns, idle (rules 17 to 23, O7, O8, A2)
# ---------------------------------------------------------------------------

def test_r17_o08_queue_order_manual_then_urgent_by_deadline_then_live_by_rank_then_other_saves():
    rig = Rig(streamers=STREAMERS)
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.step()
    now = rig.now
    inp = rig.item_inp()
    rig.sched.add_item(sv.make_manual_item("zed", "broke", None, now, now + 20 * HOUR, now), inp)
    rig.sched.add_item(card_item("yan", now, age_s=21 * HOUR), inp)
    rig.sched.add_item(card_item("s6", now, age_s=22 * HOUR), inp)
    rig.sched.add_item(card_item("xia", now, age_s=4 * HOUR), inp)
    rig.sched.add_item(card_item("wes", now, age_s=4 * HOUR), inp)
    rig.go_live("s5", "s4", "s6")
    rig.step()
    assert rig.queue() == [("zed", "save"), ("s6", "live"), ("yan", "save"), ("s4", "live"),
                           ("s5", "live"), ("wes", "save"), ("xia", "save")]
    heads = rig.plan["queue"]
    assert [e["urgent"] for e in heads] == [True, True, True, False, False, False, False]
    assert heads[0]["manual"] is True and heads[1]["deadline_at"] is not None


def _urgent_item_on_the_turn():
    """0 + 1: s1 holds the rotating turn with an urgent item attached, and
    s2 (live, unserved, not urgent) waits behind it."""
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.sched.add_item(card_item("s1", rig.now, age_s=20 * HOUR), rig.item_inp())
    rig.go_live("s2")
    rig.step()
    assert rig.occ()["cycle-1"] == "s1" and rig.queue() == [("s2", "live")]
    return rig


def _assigned(rig):
    return [(e["streamer"], e["entry"]) for e in rig.events("slot_assigned")]


def test_r17_r18_an_urgent_check_keeps_the_rotating_slot_ahead_of_a_waiting_live_stream():
    rig = _urgent_item_on_the_turn()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    _run_until_just_before(rig, ends)
    rig.step(ends - rig.now)
    assert rig.closes() == {"s1": "turn_over"}
    assert rig.sched.items_snapshot()["s1"]["verify"] is True
    # s1's check is the urgent head; its channel tab is still open, so the
    # slot waits one tick for it instead of going to s2.
    assert rig.queue() == [("s1", "save"), ("s2", "live")]
    assert rig.occ()["cycle-1"] is None
    rig.step()
    slot = rig.slot("cycle-1")
    assert slot["streamer"] == "s1" and slot["entry"] == "save" and slot["verify"] is True
    assert slot["url"] == "https://www.twitch.tv/save-streak/s1?sm=1"
    assert _assigned(rig) == [("s1", "live"), ("s1", "save")]


def test_r17_r24_an_occupant_that_goes_offline_mid_turn_keeps_the_slot_for_its_urgent_save():
    rig = _urgent_item_on_the_turn()
    rig.run(5 * MIN)
    rig.go_offline("s1")
    rig.step()
    rig.step()
    assert rig.closes() == {"s1": "offline"}
    assert rig.queue() == [("s1", "save"), ("s2", "live")]
    assert rig.plan["queue"][0]["urgent"] is True
    assert rig.occ()["cycle-1"] is None
    rig.step()
    assert rig.holder("s1")["entry"] == "save" and rig.holder("s1")["id"] == "cycle-1"
    assert _assigned(rig) == [("s1", "live"), ("s1", "save")]


def test_r18_a_turn_lasts_m_from_confirmation_and_serves_the_stream():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    slot = rig.slot("cycle-1")
    assert slot["turn_ends_at"] == slot["confirmed_at"] + M
    ends = slot["turn_ends_at"]
    while rig.now + MIN < ends:
        rig.step()
    assert rig.occ()["cycle-1"] == "s1"
    res = rig.step(ends - rig.now)
    assert rig.events("slot_turn_over", [res]) == [{"slot": "cycle-1", "streamer": "s1", "entry": "live"}]
    assert rig.closes() == {"s1": "turn_over"} and rig.occ()["cycle-1"] == "s2"
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "turn", "session": iso(BASE)}]


def test_r18_o01_a_served_live_turn_leaves_a_verify_item_that_runs_once_no_slot_holds_the_streamer():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    item = card_item("s2", rig.now, age_s=20 * HOUR)
    changed, _ = rig.sched.add_item(item, rig.item_inp())
    assert changed
    rig.step()
    assert rig.queue()[0] == ("s2", "live") and rig.plan["queue"][0]["urgent"] is True
    rig.run(M + 2 * MIN)
    assert rig.occ()["cycle-1"] == "s2" and rig.slot("cycle-1")["entry"] == "live"
    assert rig.sched.items_snapshot()["s2"]["verify"] is False
    ends = rig.slot("cycle-1")["turn_ends_at"]
    for res in rig.results:
        assert [s for s in res.plan["slots"] if s["entry"] == "save"] == []
    while rig.now + MIN < ends:
        rig.step()
    turn_end = rig.step(ends - rig.now)
    # The live turn served the broadcast; the item stays as a check.
    assert "s2" in rig.plan["served"]
    assert rig.sched.items_snapshot()["s2"]["verify"] is True
    assert rig.events("streak_item_done", [turn_end]) == []
    assert rig.closes() == {"s2": "turn_over"} and rig.holder("s2") is None
    # The check waits until the channel tab is gone, then runs on the
    # save-streak page; nothing idles in front of it.
    assert rig.queue() == [("s2", "save")] and rig.occ()["cycle-1"] is None
    rig.step()
    slot = rig.holder("s2")
    assert slot["entry"] == "save" and slot["verify"] is True and slot["item_kind"] == "broke"
    assert slot["url"] == "https://www.twitch.tv/save-streak/s2?sm=1"
    rig.step()
    rig.close_tab("s2", "already_saved")
    rig.step()
    assert rig.holder("s2") is None
    assert rig.sched.items_snapshot() == {}
    assert rig.events("streak_item_done") == [{"streamer": "s2", "reason": "already_saved"}]
    rig.run(2 * M)
    assert all(s["entry"] != "save" for r in rig.results[-60:] for s in r.plan["slots"])


def test_r18_an_item_for_a_keep_open_occupant_never_opens_a_second_tab():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.sched.add_item(card_item("s1", rig.now, age_s=20 * HOUR), rig.item_inp())
    rig.run(2 * M)
    assert rig.occ() == {"keep-1": "s1", "cycle-1": None}
    assert rig.queue() == []
    assert rig.slot("keep-1")["deadline_at"] == int(rig.sched.items_snapshot()["s1"]["deadline_at"])
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.holder("s1") is None
    rig.step()
    assert rig.holder("s1")["entry"] == "save" and rig.holder("s1")["id"] == "cycle-1"


def test_r18_a_save_turn_flips_to_live_when_its_streamer_goes_live():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.tick()
    rig.sched.add_item(card_item("s1", rig.now, age_s=2 * HOUR), rig.item_inp())
    rig.step()
    rig.run(5 * MIN)
    save = dict(rig.slot("cycle-1"))
    assert save["streamer"] == "s1" and save["entry"] == "save"
    rig.go_live("s1")
    rig.step()
    slot = rig.slot("cycle-1")
    assert slot["entry"] == "live" and slot["url"] == "https://www.twitch.tv/s1?sm=1"
    assert slot["confirmed_at"] == save["confirmed_at"] and slot["turn_ends_at"] == save["turn_ends_at"]
    assert rig.plan["close"] == []
    rig.run(save["turn_ends_at"] - rig.now)
    assert "s1" in rig.plan["served"]


def _saved_status_run(saves):
    rig = Rig(pinned={"s2"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.saves = saves
    rig.watch_start = {x: BASE - 10 * HOUR for x in STREAMERS}
    rig.go_live("s2", "s3", "s4")
    rig.tick()
    trace = []
    for i in range(150):
        if i == 20:
            rig.go_live("s1")
        if i == 70:
            rig.go_live("s5")
        rig.step()
        trace.append([(s["id"], s["streamer"], s["entry"], s["mode"]) for s in rig.plan["slots"]])
    return trace


def test_r18_saved_status_never_affects_live_turns():
    saves = {x: {"at": BASE - 10 * MIN, "count": 7} for x in ("s1", "s2", "s3", "s4", "s5")}
    assert _saved_status_run(saves) == _saved_status_run({})


def test_r19_a_higher_ranked_stream_going_live_never_cuts_a_running_turn():
    rig = Rig(k=0, c=1)
    rig.go_live("s5", "s6")
    rig.tick()
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    rig.run(5 * MIN)
    rig.go_live("s1")
    while rig.now + MIN < ends:
        rig.step()
        assert rig.occ()["cycle-1"] == "s5"
    assert rig.queue()[0] == ("s1", "live")
    rig.step(ends - rig.now)
    assert rig.occ()["cycle-1"] == "s1"


def test_r20_a_cut_turn_starts_over_in_full_and_served_is_per_broadcast():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    rig.run(20 * MIN)
    rig.close_tab("s1", "window_closed")
    rig.step(apply=False)
    assert rig.slot("cycle-1")["turn_ends_at"] is None
    rig.tabs.add("s1")
    rig.step()
    slot = rig.slot("cycle-1")
    assert slot["turn_ends_at"] == rig.now + M
    rig.run(M + MIN)
    assert "s1" in rig.plan["served"]
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert "s1" not in rig.plan["served"]


def test_r21_an_empty_queue_idles_on_the_highest_ranked_live_stream_in_place():
    rig = Rig(k=1, c=1, pinned={"s3"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s3")
    rig.tick()
    assert rig.occ() == {"keep-1": "s3", "cycle-1": "s1"}
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    rig.run(ends - rig.now)
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["mode"] == "idle"
    assert rig.plan["close"] == []
    assert rig.events("slot_idle")[-1] == {"slot": "cycle-1", "streamer": "s1"}


def test_r21_the_rotating_slot_stays_empty_when_only_keep_open_streams_are_live():
    rig = Rig(k=2, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(M + 5 * MIN)
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s2", "cycle-1": None}


def test_r21_an_idle_occupant_is_served_after_m_and_a_restart_turns_the_slot_to_turn_mode():
    rig = Rig(k=1, c=1, pinned={"s3"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s3")
    rig.tick()
    rig.run(M + 2 * MIN)
    rig.go_live("s1")
    rig.run(M + 3 * MIN)
    assert rig.slot("cycle-1")["mode"] == "idle" and rig.occ()["cycle-1"] == "s1"
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    slot = rig.slot("cycle-1")
    assert slot["streamer"] == "s1" and slot["mode"] == "turn"
    assert "s1" not in rig.plan["served"]
    assert rig.plan["close"] == []
    assert slot["turn_ends_at"] == rig.now - MIN + M


def test_r22_o07_a_newcomer_preempts_the_idle_slot_then_the_higher_ranked_stream_returns():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s2")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert rig.slot("cycle-1")["mode"] == "idle" and rig.occ()["cycle-1"] == "s2"
    rig.go_live("s5")
    res = rig.step()
    assert rig.events("slot_preempted", [res]) == [{"slot": "cycle-1", "idle": "s2", "newcomer": "s5"}]
    assert rig.closes()["s2"] == "preempted" and rig.occ()["cycle-1"] == "s5"
    rig.run(M + 3 * MIN)
    assert rig.occ()["cycle-1"] == "s2" and rig.slot("cycle-1")["mode"] == "idle"
    rig.go_live("s1")
    rig.run(M + 3 * MIN)
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["mode"] == "idle"


def test_r22_o07_a_new_save_item_preempts_the_idle_slot():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert rig.slot("cycle-1")["mode"] == "idle"
    rig.sched.add_item(card_item("zed", rig.now, age_s=2 * HOUR), rig.item_inp())
    rig.step()
    assert rig.occ()["cycle-1"] == "zed" and rig.slot("cycle-1")["entry"] == "save"
    assert rig.events("slot_preempted")[-1]["newcomer"] == "zed"


def test_r22_am02_with_two_rotating_slots_a_newcomer_preempts_the_lowest_ranked_idle():
    rig = Rig(k=0, c=2, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert [rig.slot(x)["mode"] for x in ("cycle-1", "cycle-2")] == ["idle", "idle"]
    rig.go_live("s4")
    res = rig.step()
    assert rig.events("slot_preempted", [res]) == [{"slot": "cycle-2", "idle": "s2", "newcomer": "s4"}]
    assert rig.occ() == {"cycle-1": "s1", "cycle-2": "s4"}


def test_r22_am02_a_newcomer_takes_a_free_lendable_keep_slot_without_preempting():
    rig = Rig(k=1, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert rig.occ() == {"keep-1": None, "cycle-1": "s1"}
    rig.go_live("s3")
    rig.step()
    assert rig.occ() == {"keep-1": "s3", "cycle-1": "s1"}
    assert rig.slot("keep-1")["lent"] is True
    assert rig.events("slot_preempted") == []


def test_r23_an_idle_tab_gives_way_to_a_higher_ranked_stream_only_after_m():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s3")
    rig.tick()
    rig.run(2 * M + 5 * MIN)
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["mode"] == "idle"
    idle_since = rig.slot("cycle-1")["confirmed_at"]
    rig.streamers = ["s3", "s1", "s2"]
    rig.step()
    assert rig.now < idle_since + M
    assert rig.occ()["cycle-1"] == "s1"
    while rig.now + MIN < idle_since + M:
        rig.step()
        assert rig.occ()["cycle-1"] == "s1"
    rig.step(idle_since + M - rig.now)
    assert rig.closes() == {"s1": "idle_swap"} and rig.occ()["cycle-1"] == "s3"
    assert rig.slot("cycle-1")["mode"] == "idle"


def test_r23_am02_with_two_rotating_slots_the_idle_set_follows_rank_after_m():
    rig = Rig(k=0, c=2, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.run(2 * M + 5 * MIN)
    assert {rig.occ()["cycle-1"], rig.occ()["cycle-2"]} == {"s1", "s2"}
    rig.streamers = ["s3", "s1", "s2"]
    rig.step()
    # s2 has idled for less than M: it stays for now.
    assert {rig.occ()["cycle-1"], rig.occ()["cycle-2"]} == {"s1", "s2"}
    rig.run(M)
    swaps = [c for r in rig.results for c in r.plan["close"] if c["reason"] == "idle_swap"]
    assert swaps and {c["streamer"] for c in swaps} == {"s2"}
    assert {rig.occ()["cycle-1"], rig.occ()["cycle-2"]} == {"s1", "s3"}


# ---------------------------------------------------------------------------
# Offline, raids, closed tabs, lost tabs (rules 24 to 29, O9, O11 to O13)
# ---------------------------------------------------------------------------

def test_r24_o09_a_broadcast_that_ends_unserved_becomes_a_missed_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(10 * MIN)
    last_seen = rig.now
    rig.go_offline("s1", "s2")
    rig.step()
    res = rig.step()
    assert rig.closes() == {"s1": "offline"}
    items = rig.sched.items_snapshot()
    assert set(items) == {"s1", "s2"}
    for login in ("s1", "s2"):
        it = items[login]
        assert it["kind"] == "missed" and it["origin"] == "offline_edge" and it["count"] is None
        assert it["break_at"] == last_seen and it["deadline_at"] == last_seen + 24 * HOUR
        assert it["age_unit_s"] == 60 and it["session"] == iso(BASE)
        assert it["url"] == f"https://www.twitch.tv/save-streak/{login}?sm=1"
    added = rig.events("streak_item_added", [res])
    assert {e["streamer"] for e in added} == {"s1", "s2"}
    assert added[0]["deadline_at"] == iso(last_seen + 24 * HOUR)
    assert added[0]["mode"] == "slot" and added[0]["merged"] is False
    # s1's save (equal deadline, better rank) waits for its channel tab to
    # close and keeps the rotating slot; s2 does not jump it.
    assert rig.queue() == [("s1", "save"), ("s2", "save")]
    assert rig.occ()["cycle-1"] is None
    rig.step()
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["entry"] == "save"


def test_r24_o09_with_saves_off_an_unserved_broadcast_is_dropped_with_a_log_line():
    rig = Rig(k=0, c=1, auto_save=False, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(5 * MIN)
    rig.go_offline("s1")
    rig.step()
    rig.step()
    assert rig.sched.items_snapshot() == {}
    assert rig.events("slot_unserved_dropped") == [{"streamer": "s1", "session": iso(BASE)}]


def test_r24_o11_an_occupant_that_goes_offline_closes_and_its_keep_slot_refills():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(M + 5 * MIN)
    rig.go_offline("s1")
    rig.step()
    rig.step()
    assert rig.closes()["s1"] == "offline"
    assert rig.occ()["keep-1"] == "s2"
    assert rig.sched.items_snapshot() == {}


def test_r24_o12_a_dismissed_broadcast_that_ends_unserved_gets_an_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(5 * MIN)
    rig.close_tab("s1")
    rig.run(5 * MIN)
    assert rig.plan["dismissed"] == ["s1"]
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.sched.items_snapshot()["s1"]["kind"] == "missed"
    assert rig.plan["dismissed"] == []


def test_r24_a_served_or_covered_or_unlisted_broadcast_gets_no_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert "s1" in rig.plan["served"]
    rig.saves = {"s2": {"at": rig.now + 30, "count": 4}}
    rig.streamers = ["s1", "s2"]
    rig.go_offline("s1", "s2")
    rig.advance(60)
    rig.step()
    rig.step()
    assert rig.sched.items_snapshot() == {}
    skipped = rig.events("vod_skipped")
    assert skipped == [{"streamer": "s2", "reason": "streak_already_saved",
                        "saved_at": iso(rig.saves["s2"]["at"])}]


def test_r25_o13_a_raid_before_m_excludes_and_the_end_makes_an_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(10 * MIN)
    rig.close_tab("s1", "raid")
    res = rig.step()
    assert rig.events("slot_gone", [res]) == [
        {"slot": "cycle-1", "streamer": "s1", "entry": "live", "reason": "raid"}]
    assert rig.occ()["cycle-1"] == "s2"
    assert ("s1", "live") not in rig.queue()
    assert "s1" not in rig.plan["served"]
    rig.run(M + 3 * MIN)
    assert rig.holder("s1") is None
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.sched.items_snapshot()["s1"]["kind"] == "missed"


def test_r25_o13_a_raid_after_m_counts_as_served_and_makes_no_item():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(M + 5 * MIN)
    rig.close_tab("s1", "raid")
    rig.step()
    assert "s1" in rig.plan["served"]
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.sched.items_snapshot() == {}


def test_r25_o01_a_raid_out_never_settles_the_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.sched.add_item(card_item("s1", rig.now, age_s=3 * HOUR), rig.item_inp())
    rig.run(10 * MIN)
    rig.close_tab("s1", "raid")
    rig.step()
    item = rig.sched.items_snapshot()["s1"]
    assert item["verify"] is False and item["kind"] == "broke"
    assert rig.events("streak_item_done") == []
    rig.go_offline("s1")
    rig.run(2 * MIN)
    item = rig.sched.items_snapshot()["s1"]
    assert item["verify"] is False
    assert rig.holder("s1")["entry"] == "save"


def _run_until_just_before(rig, when):
    while rig.now + MIN < when:
        rig.step()
    assert rig.now < when


def test_r25_a_turn_raided_after_its_end_but_before_a_tick_counts_as_served():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    _run_until_just_before(rig, ends)
    assert "s1" not in rig.plan["served"]
    rig.advance(ends + 5 - rig.now)
    rig.close_tab("s1", "raid")
    res = rig.tick()
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "turn", "session": iso(BASE)}]
    assert "s1" not in rig.sched.excluded and rig.events("slot_turn_over", [res]) == []
    assert rig.occ()["cycle-1"] == "s2"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert "s1" not in rig.sched.items_snapshot()


def test_r25_a_keep_open_occupant_raided_after_its_m_minutes_but_before_a_tick_is_served():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.step()
    due = rig.slot("keep-1")["confirmed_at"] + M
    _run_until_just_before(rig, due)
    assert "s1" not in rig.plan["served"]
    rig.advance(due + 5 - rig.now)
    rig.close_tab("s1", "raid")
    res = rig.tick()
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "keep", "session": iso(BASE)}]
    assert "s1" not in rig.sched.excluded


def _raid_just_after(rig, due):
    """A raid 0.2 s after a fractional due time: the extension stamps the
    raid with the floored epoch, which is below the due time itself."""
    _run_until_just_before(rig, due)
    rig.advance(due + 0.2 - rig.now)
    rig.close_tab("s1", "raid")
    assert rig.gone[-1]["at"] < due
    return rig.tick()


def test_r25_a_turn_confirmed_at_a_fractional_epoch_and_raided_just_after_its_end_is_served():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.advance(MIN + 0.7)
    rig.tick()
    # The plan floors its epochs; the record keeps the fractional clock.
    ends = rig.sched._slot_of("s1")["turn_ends_at"]
    assert ends != int(ends)
    res = _raid_just_after(rig, ends)
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "turn", "session": iso(BASE)}]
    assert "s1" not in rig.sched.excluded
    assert rig.occ()["cycle-1"] == "s2"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert "s1" not in rig.sched.items_snapshot()


def test_r25_a_keep_open_occupant_confirmed_at_a_fractional_epoch_and_raided_just_after_m_is_served():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.advance(MIN + 0.7)
    rig.tick()
    due = rig.sched._slot_of("s1")["confirmed_at"] + M
    assert due != int(due)
    res = _raid_just_after(rig, due)
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "keep", "session": iso(BASE)}]
    assert "s1" not in rig.sched.excluded
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert "s1" not in rig.sched.items_snapshot()


def test_r21_r25_an_idle_occupant_raided_after_its_m_minutes_but_before_a_tick_is_served():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(M + 3 * MIN)
    assert rig.slot("cycle-1")["mode"] == "idle" and rig.occ()["cycle-1"] == "s1"
    # An idle occupant not yet served for its broadcast (a restored record,
    # for instance) whose M minutes run out 30 s from now.
    record = rig.sched._slot_of("s1")
    record["confirmed_at"] = rig.now - M + 30
    del rig.sched.served["s1"]
    rig.advance(35)
    rig.close_tab("s1", "raid")
    res = rig.tick()
    assert rig.events("slot_served", [res]) == [{"streamer": "s1", "how": "turn", "session": iso(BASE)}]
    assert "s1" not in rig.sched.excluded


def test_r25_a_raid_before_the_turn_end_that_is_processed_after_it_still_excludes():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    _run_until_just_before(rig, ends)
    rig.advance(ends + 10 - rig.now)
    rig.tabs.discard("s1")
    rig.gone.append({"key": KEY, "streamer": "s1", "reason": "raid", "at": int(ends - 5)})
    res = rig.tick()
    assert rig.events("slot_served", [res]) == [] and rig.events("slot_turn_over", [res]) == []
    assert rig.sched.excluded["s1"]["reason"] == "raid"
    assert rig.occ()["cycle-1"] == "s2"
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert rig.sched.items_snapshot()["s1"]["kind"] == "missed"


def test_r25_r39_a_draining_occupant_raided_before_its_drain_ends_is_excluded_not_served():
    rig = Rig(k=2, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.run(10 * MIN)
    rig.k = 1
    rig.step()
    until = {d["streamer"]: d["until"] for d in rig.plan["draining"]}
    assert set(until) == {"s2"}
    rig.run(5 * MIN)
    rig.close_tab("s2", "raid")
    rig.step()
    assert rig.plan["draining"] == []
    assert rig.sched.excluded["s2"]["reason"] == "raid"
    rig.run(until["s2"] - rig.now + 5 * MIN)
    assert "s2" not in rig.plan["served"]
    assert "s2" not in [e["streamer"] for e in rig.events("slot_served")]
    assert "s2" not in [c["streamer"] for r in rig.results for c in r.plan["close"]]
    rig.go_offline("s2")
    rig.run(3 * MIN)
    assert rig.sched.items_snapshot()["s2"]["kind"] == "missed"


@pytest.mark.parametrize("reason", ["user_closed", "navigated"])
def test_r26_o12_a_closed_slot_tab_dismisses_the_streamer_for_the_broadcast(reason):
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.close_tab("s1", reason)
    res = rig.step()
    assert rig.events("slot_gone", [res]) == [
        {"slot": "keep-1", "streamer": "s1", "entry": "live", "reason": reason}]
    assert rig.plan["dismissed"] == ["s1"]
    rig.run(2 * M)
    assert rig.holder("s1") is None
    assert "s1" not in [e["streamer"] for e in rig.plan["queue"]]
    assert rig.occ()["keep-1"] in (None, "s2")
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert rig.plan["dismissed"] == [] and rig.occ()["keep-1"] == "s1"


def test_r26_o12_closing_or_navigating_a_save_turn_ends_its_item_and_dismisses_nothing():
    for reason in ("user_closed", "navigated"):
        rig = Rig(k=0, c=1, streamers=("s1",))
        rig.tick()
        rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
        rig.step()
        rig.step()
        assert rig.occ()["cycle-1"] == "zed"
        rig.close_tab("zed", reason)
        rig.step()
        assert rig.sched.items_snapshot() == {}
        assert rig.events("streak_item_done") == [{"streamer": "zed", "reason": reason}]
        assert rig.plan["dismissed"] == []
        rig.run(5 * MIN)
        assert rig.holder("zed") is None


def test_r26_a_dismissed_streamers_item_waits_until_the_broadcast_ends():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(3 * MIN)
    rig.close_tab("s1")
    rig.step()
    rig.sched.add_item(card_item("s1", rig.now, age_s=HOUR), rig.item_inp())
    rig.run(10 * MIN)
    assert rig.holder("s1") is None and rig.queue() == []
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.holder("s1")["entry"] == "save"


def _dismiss_s1_with_an_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(3 * MIN)
    rig.close_tab("s1")
    rig.step()
    rig.sched.add_item(card_item("s1", rig.now, age_s=HOUR), rig.item_inp())
    rig.step()
    assert rig.plan["dismissed"] == ["s1"] and rig.occ()["cycle-1"] is None
    return rig


def _restart(rig, after):
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    rig.sched = ss.SlotScheduler()
    rig.advance(after)
    rig.sched.load_state(state, rig.now)
    rig.sched.reset_startup(rig.mono)


def test_r26_r38_a_dismissal_whose_broadcast_ended_during_a_restart_expires_after_two_polls():
    rig = _dismiss_s1_with_an_item()
    rig.go_offline("s1")
    _restart(rig, 5 * MIN)
    mark = len(rig.results)
    res = rig.tick(report=False)
    assert res.state == "waiting" and rig.plan["dismissed"] == ["s1"]
    rig.step()
    assert rig.plan["dismissed"] == []
    rig.step()
    assert rig.holder("s1")["entry"] == "save"
    # The end of that broadcast was not seen: no missed item is made.
    assert [e for e in rig.events("streak_item_added", rig.results[mark:])
            if e["origin"] == "offline_edge"] == []


def test_r26_r46_a_dismissal_whose_broadcast_ended_while_slot_mode_was_off_expires():
    rig = _dismiss_s1_with_an_item()
    rig.slot_mode = False
    assert rig.step().state == "off"
    rig.go_offline("s1")
    rig.run(5 * MIN)
    rig.slot_mode = True
    rig.step()
    rig.sched.add_item(card_item("s1", rig.now, age_s=HOUR), rig.item_inp())
    rig.step()
    assert rig.plan["dismissed"] == []
    rig.step()
    assert rig.holder("s1")["entry"] == "save"


def test_r07_r26_a_one_poll_omission_after_a_restart_keeps_the_dismissal():
    rig = _dismiss_s1_with_an_item()
    started = rig.live["s1"]
    _restart(rig, 5 * MIN)
    mark = len(rig.results)
    rig.go_offline("s1")
    rig.tick(report=False)
    rig.go_live("s1", started=started)
    rig.run(2 * M)
    assert rig.plan["dismissed"] == ["s1"]
    assert [e for e in rig.events("slot_assigned", rig.results[mark:]) if e["streamer"] == "s1"] == []
    assert rig.holder("s1") is None


def test_r14_r26_records_of_a_broadcast_that_ended_while_unlisted_expire_once_relisted():
    rig = _dismiss_s1_with_an_item()
    rig.streamers = ["s2"]
    rig.run(5 * MIN)
    rig.go_offline("s1")
    rig.run(5 * MIN)
    assert rig.plan["dismissed"] == ["s1"]
    rig.streamers = ["s1", "s2"]
    rig.step()
    assert rig.plan["dismissed"] == ["s1"]
    rig.step()
    assert rig.plan["dismissed"] == []


def test_r27_window_closed_reissues_once_then_dismisses():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.close_tab("s1", "window_closed")
    res = rig.step()
    assert rig.events("slot_open_reissued", [res]) == [
        {"slot": "cycle-1", "streamer": "s1", "why": "window_closed"}]
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["confirmed_at"] is None
    assert "s1" in rig.tabs
    rig.step()
    assert rig.slot("cycle-1")["confirmed_at"] is not None
    rig.close_tab("s1", "window_closed")
    rig.step()
    assert rig.plan["dismissed"] == ["s1"] and rig.occ()["cycle-1"] == "s2"


def test_r28_a_silent_loss_reissues_once_then_gives_up():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.tabs.discard("s1")
    res = rig.step()
    assert rig.events("slot_open_reissued", [res]) == [
        {"slot": "cycle-1", "streamer": "s1", "why": "silent_loss"}]
    rig.step()
    rig.tabs.discard("s1")
    res = rig.step()
    assert rig.events("slot_open_gave_up", [res]) == [
        {"slot": "cycle-1", "streamer": "s1", "entry": "live", "why": "silent_loss"}]
    assert rig.occ()["cycle-1"] == "s2"
    assert ("s1", "live") not in rig.queue()
    rig.go_offline("s1")
    rig.run(2 * MIN)
    assert rig.sched.items_snapshot()["s1"]["kind"] == "missed"


def test_r28_a_save_entry_lost_twice_goes_back_once_then_is_dropped():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    for _ in range(2):
        rig.step()
        rig.step()
        rig.tabs.discard("zed")
        rig.step()
        rig.step()
        rig.tabs.discard("zed")
        rig.step()
    assert rig.events("slot_open_gave_up") and len(rig.events("slot_open_gave_up")) == 2
    assert rig.events("streak_item_done") == [{"streamer": "zed", "reason": "open_failed"}]
    assert rig.sched.items_snapshot() == {}


def test_r29_an_open_unconfirmed_for_600_alive_seconds_is_given_up():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick(apply=False)
    rig.run(9 * MIN, apply=False)
    assert rig.occ()["cycle-1"] == "s1"
    rig.step(apply=False)
    assert rig.events("slot_open_gave_up") == [
        {"slot": "cycle-1", "streamer": "s1", "entry": "live", "why": "never_confirmed"}]
    assert rig.occ()["cycle-1"] == "s2"


def test_r29_busy_paused_and_asleep_time_never_counts_toward_the_give_up():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick(apply=False)
    rig.run(5 * MIN, apply=False)
    rig.busy = "paused"
    rig.run(20 * MIN, apply=False)
    rig.busy = None
    rig.paused = True
    rig.run(20 * MIN, apply=False)
    rig.paused = False
    rig.advance(HOUR)
    rig.tick(apply=False, wake_gap=HOUR)
    rig.run(3 * MIN, apply=False)
    assert rig.events("slot_open_gave_up") == []
    rig.run(3 * MIN, apply=False)
    assert len(rig.events("slot_open_gave_up")) == 1


def test_r29_a_save_item_given_up_is_retried_once_then_dropped():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.run(11 * MIN, apply=False)
    assert rig.sched.items_snapshot()["zed"]["open_failures"] == 1
    rig.run(11 * MIN, apply=False)
    assert rig.sched.items_snapshot() == {}
    assert rig.events("streak_item_done") == [{"streamer": "zed", "reason": "open_failed"}]


def test_as10_a_not_eligible_gone_entry_ends_the_item_and_records_no_save():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.step()
    rig.step()
    assert rig.occ()["cycle-1"] == "zed"
    rig.close_tab("zed", "not_eligible")
    res = rig.step()
    assert rig.events("streak_item_done", [res]) == [{"streamer": "zed", "reason": "not_eligible"}]
    assert rig.events("slot_gone", [res]) == [
        {"slot": "cycle-1", "streamer": "zed", "entry": "save", "reason": "not_eligible"}]
    assert rig.occ()["cycle-1"] is None
    assert rig.sched.items_snapshot() == {}
    names = {n for r in rig.results for (n, _) in r.events}
    assert not names & {"streak_already_saved", "streak_saved", "vod_skipped"}
    assert all(e["reason"] != "already_saved" for e in rig.events("streak_item_done"))


def test_r26_a_gone_entry_from_an_earlier_broadcast_or_assignment_is_ignored():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.gone.append({"key": KEY, "streamer": "s1", "reason": "user_closed", "at": int(BASE) - 10})
    rig.step()
    assert rig.plan["dismissed"] == [] and rig.occ()["cycle-1"] == "s1"
    rig.run(M)
    assert rig.occ()["cycle-1"] == "s2"
    assigned = rig.slot("cycle-1")["assigned_at"]
    rig.gone.append({"key": KEY, "streamer": "s2", "reason": "user_closed", "at": assigned - 1})
    rig.step()
    assert rig.plan["dismissed"] == [] and rig.occ()["cycle-1"] == "s2"
    rig.tabs.discard("s2")
    rig.gone.append({"key": KEY, "streamer": "s2", "reason": "user_closed", "at": assigned})
    rig.step()
    assert rig.plan["dismissed"] == ["s2"]


# ---------------------------------------------------------------------------
# Extras (rule 31, A13)
# ---------------------------------------------------------------------------

def test_r31_a_keep_target_extra_is_adopted_and_other_extras_are_unplanned_and_not_served():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2", "s3")
    rig.tabs = {"s1", "s3"}
    res = rig.tick(apply=False)
    assert rig.occ()["keep-1"] == "s1"
    assert {"slot": "keep-1", "streamer": "s1", "mode": None} in rig.events("slot_extra_adopted", [res])
    assert rig.occ()["cycle-1"] == "s2"
    assert rig.closes() == {"s3": "unplanned"}
    assert rig.events("slot_extra_closed", [res]) == [{"streamer": "s3"}]
    assert "s3" not in rig.plan["served"]
    assert ("s3", "live") in rig.queue()
    rig.step(apply=False)
    assert rig.events("slot_extra_closed") == [{"streamer": "s3"}]


def test_r31_an_extra_takes_the_rotating_slot_only_as_the_queue_head():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s3")
    rig.tabs = {"s3"}
    rig.tick(apply=False)
    assert rig.occ()["cycle-1"] == "s1" and rig.closes() == {"s3": "unplanned"}
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s3")
    rig.tabs = {"s1"}
    rig.tick(apply=False)
    assert rig.occ()["cycle-1"] == "s1"
    slot = rig.slot("cycle-1")
    assert slot["confirmed_at"] == BASE and slot["turn_ends_at"] == BASE + M


def test_r31_an_extra_never_jumps_an_urgent_save_item():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=20 * HOUR), rig.item_inp())
    rig.go_live("s1")
    rig.tabs = {"s1"}
    rig.step(apply=False)
    assert rig.occ()["cycle-1"] == "zed"
    assert rig.closes() == {"s1": "unplanned"}


def test_r31_am13_an_extra_that_is_the_first_live_entry_is_adopted_into_a_lendable_keep_slot():
    rig = Rig(k=1, c=1, streamers=("s1", "s2"))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=20 * HOUR), rig.item_inp())
    rig.go_live("s1")
    rig.tabs = {"s1"}
    res = rig.step(apply=False)
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "zed"}
    assert rig.slot("keep-1")["lent"] is True
    assert rig.events("slot_extra_adopted", [res]) == [{"slot": "keep-1", "streamer": "s1", "mode": "turn"}]
    assert rig.plan["close"] == []


def test_r31_am13_an_extra_never_borrows_the_keep_slot_a_new_target_needs():
    rig = Rig(k=1, c=1, pinned={"s1", "s5"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s1")
    rig.tick()
    rig.run(25 * MIN)
    rig.go_live("s3")
    rig.run(10 * MIN)
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s3"} and "s1" in rig.plan["served"]
    rig.go_offline("s1")
    rig.run(3 * MIN)
    assert rig.occ() == {"keep-1": None, "cycle-1": "s3"}
    # s5 (a Keep Open target) and s2 (an extra tab, ranked above s5) go live
    # on the same tick: the empty Keep Open slot is s5's, not a lent turn.
    rig.go_live("s2", "s5")
    rig.tabs.add("s2")
    res = rig.step()
    assert rig.occ() == {"keep-1": "s5", "cycle-1": "s3"}
    assert rig.slot("keep-1")["lent"] is False
    assert rig.events("slot_extra_adopted", [res]) == []
    assert rig.events("slot_hold_scheduled") == []
    assert rig.closes() == {"s2": "unplanned"}
    assert ("s2", "live") in rig.queue()


def test_r31_turning_slot_mode_on_adopts_the_open_tabs_instead_of_closing_them_before_a_poll():
    rig = Rig(streamers=("s1", "s2", "s3", "s4"))
    rig.slot_mode = False
    rig.go_live("s1", "s2", "s3")
    rig.tabs = {"s1", "s2", "s3"}
    rig.tick()
    rig.run(HOUR)
    for _ in range(2):
        # The Settings save restarts the monitor, whose first tick replans
        # without a poll (A10) while live_as_of is 20 s old.
        rig.slot_mode = True
        rig.advance(20)
        res = rig.tick(authoritative=False)
        assert res.state == "alive" and res.plan["close"] == []
        assert rig.tabs == {"s1", "s2", "s3"}
        res = rig.step()
        assert sorted(e["streamer"] for e in rig.events("slot_extra_adopted", [res])) == ["s1", "s2", "s3"]
        assert rig.events("slot_assigned", [res]) == [] and res.plan["close"] == []
        assert rig.tabs == {"s1", "s2", "s3"}
        rig.slot_mode = False
        rig.run(10 * MIN)


def test_r31_an_extra_is_adopted_as_the_idle_pick_when_the_queue_is_empty():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.sched.served["s1"] = {"session": iso(BASE), "at": BASE, "how": "turn"}
    rig.go_live("s1")
    rig.tabs = {"s1"}
    res = rig.tick(apply=False)
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["mode"] == "idle"
    assert rig.events("slot_extra_adopted", [res]) == [
        {"slot": "cycle-1", "streamer": "s1", "mode": "idle"}]
    assert rig.events("slot_assigned", [res]) == [] and rig.plan["close"] == []


# ---------------------------------------------------------------------------
# Pause, busy, executor (rules 32, 34, 37)
# ---------------------------------------------------------------------------

def test_r32_paused_no_opens_no_displacement_but_turn_ends_still_close():
    rig = Rig(k=1, c=1, pinned={"s1", "s2"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.run(M + 5 * MIN)
    rig.auto_paused = True
    rig.go_live("s1", "s4")
    res = rig.step()
    assert res.plan["assigning"] is False and res.plan["pause"] == "live"
    rig.run(2 * M)
    assert rig.occ()["keep-1"] == "s2"
    assert rig.events("slot_displaced") == []
    assert rig.holder("s4") is None
    assert "s1" not in rig.tabs and "s4" not in rig.tabs
    rig.auto_paused = False
    rig.paused = True
    assert rig.step().plan["pause"] == "manual"
    rig.paused = False
    rig.step()
    assert rig.occ()["keep-1"] == "s1"


def test_r32_turn_ends_close_while_paused_and_the_queue_resumes_after():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    rig.auto_paused = True
    rig.run(ends - rig.now)
    assert rig.closes() == {"s1": "turn_over"} and rig.occ()["cycle-1"] is None
    rig.run(10 * MIN)
    assert rig.occ()["cycle-1"] is None
    rig.auto_paused = False
    rig.step()
    assert rig.occ()["cycle-1"] == "s2"


def test_r32_a_window_closed_reissue_during_auto_pause_opens_nothing_until_the_lift():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(5 * MIN)
    rig.auto_paused = True
    rig.close_tab("s1", "window_closed")
    rig.run(20 * MIN)
    assert "s1" not in rig.tabs
    assert rig.occ()["cycle-1"] == "s1" and rig.slot("cycle-1")["confirmed_at"] is None
    assert rig.events("slot_open_gave_up") == []
    rig.auto_paused = False
    rig.step()
    assert "s1" in rig.tabs
    rig.step()
    assert rig.slot("cycle-1")["confirmed_at"] is not None


def test_r34_a_busy_report_holds_the_plan_and_still_records_closes():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(3 * MIN)
    rig.busy = "paused"
    res = rig.step()
    assert rig.events("slot_plan_blocked", [res]) == [{"busy": "paused"}]
    assert res.tooltip == ss.TOOLTIP_BUSY and res.plan["generated_at"] == int(rig.now)
    rig.close_tab("s1")
    rig.run(M + 5 * MIN)
    assert len(rig.events("slot_plan_blocked")) == 1
    assert rig.plan["dismissed"] == ["s1"]
    assert rig.events("slot_turn_over") == []
    rig.busy = None
    rig.step()
    assert rig.holder("s1") is None and rig.occ()["cycle-1"] == "s2"
    rig.busy = "paused"
    rig.step()
    assert len(rig.events("slot_plan_blocked")) == 2


def test_c03_r34_a_busy_first_tick_after_a_fresh_start_lists_k_plus_c_slots():
    # A tray Stop then Start keeps the executor's report in memory, so the
    # first tick of the fresh scheduler is alive and, here, busy.
    rig = Rig(pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.run(5 * MIN)
    rig.busy = "paused"
    rig.step()
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    rig.sched = ss.SlotScheduler()
    rig.advance(ss.SLOT_STATE_MAX_AGE_SECONDS + MIN)
    rig.sched.load_state(state, rig.now)
    assert rig.sched.slots == []
    rig.sched.reset_startup(rig.mono)
    res = rig.tick()
    assert res.state == "alive" and res.tooltip == ss.TOOLTIP_BUSY
    assert [s["id"] for s in res.plan["slots"]] == layout(2, 1)
    assert all(s["streamer"] is None for s in res.plan["slots"])
    assert rig.events("slot_assigned", [res]) == [] and res.plan["close"] == []


def test_r34_r39_am35_a_count_change_while_busy_relabels_and_drains_without_closing():
    rig = Rig(pinned={"s1", "s2"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(2 * MIN)
    rig.go_live("s3")
    rig.run(8 * MIN)
    before = {s["streamer"]: dict(s) for s in rig.plan["slots"] if s["streamer"]}
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s2", "cycle-1": "s3"}
    rig.busy = "paused"
    rig.step()
    close_before = rig.plan["close"]
    rig.k = 1
    res = rig.step()
    assert [s["id"] for s in res.plan["slots"]] == layout(1, 1)
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s3"}
    assert rig.slot("keep-1")["confirmed_at"] == before["s1"]["confirmed_at"]
    until = before["s2"]["confirmed_at"] + M
    assert res.plan["draining"] == [{"streamer": "s2", "until": until}]
    assert res.plan["close"] == close_before and res.tooltip == ss.TOOLTIP_BUSY
    rig.run(5 * MIN)
    assert "s2" in rig.tabs and rig.plan["draining"] == [{"streamer": "s2", "until": until}]
    rig.busy = None
    _run_until_just_before(rig, until)
    assert "s2" in rig.tabs and "s2" not in rig.closes()
    rig.step(until - rig.now)
    assert rig.closes()["s2"] == "turn_over" and "s2" not in rig.tabs
    assert rig.plan["draining"] == []
    # Back to 2 + 1 while busy: the new Keep Open slot stays empty until the
    # pause lifts.
    rig.busy = "paused"
    rig.step()
    rig.k = 2
    res = rig.step()
    assert [s["id"] for s in res.plan["slots"]] == layout(2, 1)
    assert rig.occ()["keep-2"] is None and rig.events("slot_assigned", [res]) == []
    rig.run(3 * MIN)
    assert rig.occ()["keep-2"] is None
    rig.busy = None
    res = rig.step()
    assert rig.occ()["keep-2"] == "s2"


def test_r37_the_default_browser_takes_the_executor_role_from_another_browser():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.extension = False
    rig.reports = {CHROME_KEY: {"browser": "chrome", "streamers": frozenset(), "epoch": rig.now,
                                "mono": rig.mono, "plan_seq": 0, "busy": None}}
    assert rig.tick().plan["executor"] == CHROME_KEY
    rig.advance(30)
    rig.reports[KEY] = {"browser": "firefox", "streamers": frozenset(), "epoch": rig.now,
                        "mono": rig.mono, "plan_seq": 0, "busy": None}
    res = rig.tick()
    assert res.plan["executor"] == KEY
    assert rig.events("slot_executor_changed", [res]) == [{"from": CHROME_KEY, "to": KEY}]


def test_r37_the_executor_is_sticky_among_profiles_of_one_browser_and_moves_after_150_seconds():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.extension = False

    def rep(key):
        return {"browser": "firefox", "streamers": frozenset(), "epoch": rig.now,
                "mono": rig.mono, "plan_seq": 0, "busy": None}

    rig.reports = {KEY: rep(KEY)}
    rig.tick()
    rig.advance(20)
    rig.reports[KEY_B] = rep(KEY_B)
    assert rig.tick().plan["executor"] == KEY
    for _ in range(3):
        rig.advance(50)
        rig.reports[KEY_B] = rep(KEY_B)
        rig.tick()
    assert rig.plan["executor"] == KEY_B
    assert rig.events("slot_executor_changed")[-1] == {"from": KEY, "to": KEY_B}


def test_r37_a_new_executor_opens_what_it_lacks_without_a_reissue_or_give_up():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.key, rig.browser = CHROME_KEY, "chrome"
    rig.go_live("s1")
    rig.tick()
    rig.run(3 * MIN)
    assert rig.plan["executor"] == CHROME_KEY and rig.slot("cycle-1")["confirmed_at"] is not None
    rig.reports[CHROME_KEY]["mono"] = rig.mono
    rig.key, rig.browser = KEY, "firefox"
    rig.tabs = set()
    res = rig.step()
    assert res.plan["executor"] == KEY
    assert rig.slot("cycle-1")["streamer"] == "s1" and rig.slot("cycle-1")["confirmed_at"] is None
    assert "s1" in rig.tabs
    rig.step()
    assert rig.slot("cycle-1")["confirmed_at"] is not None
    assert rig.events("slot_open_reissued") == [] and rig.events("slot_open_gave_up") == []


def test_r37_another_browser_executes_only_while_no_default_profile_reports():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.extension = False
    rig.reports = {CHROME_KEY: {"browser": "chrome", "streamers": frozenset(), "epoch": rig.now,
                                "mono": rig.mono, "plan_seq": 0, "busy": None},
                   KEY: {"browser": "firefox", "streamers": frozenset(), "epoch": rig.now - 400,
                         "mono": rig.mono - 400, "plan_seq": 0, "busy": None}}
    assert rig.tick().plan["executor"] == CHROME_KEY


# ---------------------------------------------------------------------------
# Absorb, restarts, settings, outages (rules 33, 38, 39, 42, 43, 44, 45, 46)
# ---------------------------------------------------------------------------

def _absorb_snapshots(now):
    ended = iso(now - 2 * HOUR)
    card = sv.make_card_item("cara", "broke", 4, now - HOUR, None,
                             {"card_age_s": 3600, "card_age_unit_s": 3600}, now)
    return {
        "queued_vods": {
            "vic": {"url": sv.save_url("vic"), "ended_at": ended},
            "cara": sv.item_to_queued_vod(card),
            "old": {"url": sv.save_url("old"), "ended_at": iso(now - 30 * HOUR),
                    "deadline_at": iso(now - 5 * HOUR)},
        },
        "held": {"hal": sv.make_card_item("hal", "in_danger", 2, now, 5, {}, now)},
        "pending_offer": {"id": "rescue-1", "created_at": iso(now), "batch_size": 3,
                          "rotate_minutes": 30,
                          "candidates": [{"streamer": "pen", "url": sv.save_url("pen"),
                                          "kind": "ended", "ended_at": ended},
                                         {"streamer": "s1", "url": "https://twitch.tv/s1?sm=1",
                                          "kind": "live", "ended_at": None}]},
        "last_acked_offer": {"offer": {"id": "rescue-0", "created_at": iso(now), "batch_size": 3,
                                       "rotate_minutes": 30,
                                       "candidates": [{"streamer": "ack", "url": sv.save_url("ack"),
                                                       "kind": "ended", "ended_at": ended}]},
                             "claimant": KEY, "acked_at": now - 60},
    }


def test_r33_activation_absorbs_leftovers_once_and_skips_expired_or_covered_entries():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.absorb = _absorb_snapshots(rig.now)
    rig.saves = {"vic": {"at": rig.now - 60, "count": 3}}
    res = rig.tick()
    assert res.absorbed is True
    items = rig.sched.items_snapshot()
    assert set(items) == {"cara", "hal", "pen", "ack"}
    assert items["pen"]["kind"] == "missed" and items["pen"]["origin"] == "absorbed"
    assert items["pen"]["break_at"] == rig.now - 2 * HOUR
    assert items["pen"]["deadline_at"] == rig.now + 22 * HOUR
    # DESIGN S2a: every leftover becomes a missed item with origin absorbed;
    # break_at is the entry's ended_at and the entry's own deadline wins.
    for login in items:
        assert items[login]["kind"] == "missed" and items[login]["origin"] == "absorbed"
        assert items[login]["count"] is None
    assert items["cara"]["break_at"] == rig.now - 3 * HOUR
    assert items["cara"]["deadline_at"] == rig.now + 21 * HOUR and items["cara"]["age_unit_s"] == 3600
    assert items["hal"]["break_at"] == rig.now and items["hal"]["deadline_at"] == rig.now + 5 * HOUR
    names = [n for (n, _) in res.events]
    assert names.index("slot_mode_active") > max(i for i, n in enumerate(names) if n == "streak_item_added")
    assert {e["streamer"] for e in rig.events("streak_item_added", [res])} == set(items)
    assert rig.events("vod_skipped", [res])[0]["streamer"] == "vic"
    assert rig.events("streak_item_expired", [res])[0]["streamer"] == "old"
    for _ in range(3):
        again = rig.step()
        assert again.absorbed is False
    assert len(rig.events("slot_mode_active")) == 1


def test_r33_as03_absorbed_unknown_age_cards_are_judged_as_missed_items():
    rig = Rig(k=0, c=1, streamers=("s1",))
    now = rig.now
    unknown = sv.make_card_item("una", "broke", 4, now - HOUR, None, {}, now)
    held = sv.make_card_item("hed", "in_danger", 4, now - HOUR, 5, {}, now)
    kept = sv.make_card_item("kip", "broke", 4, now - HOUR, None, {}, now)
    rig.absorb = {"queued_vods": {"una": sv.item_to_queued_vod(unknown),
                                  "kip": sv.item_to_queued_vod(kept)},
                  "held": {"hed": held}}
    # Equal counts and no watch start: the card rule would keep both items;
    # the missed rule covers a save seen after the break.
    rig.saves = {"una": {"at": now - 30 * MIN, "count": 4},
                 "hed": {"at": now - 30 * MIN, "count": 4},
                 "kip": {"at": now - 2 * HOUR, "count": 4}}
    res = rig.tick()
    assert sorted(e["streamer"] for e in rig.events("vod_skipped", [res])) == ["hed", "una"]
    assert set(rig.sched.items_snapshot()) == {"kip"}
    slot = rig.slot("cycle-1")
    assert slot["streamer"] == "kip" and slot["entry"] == "save" and slot["item_kind"] == "missed"


def test_r33_am12_an_absorbed_manual_item_stays_manual_and_takes_the_next_turn():
    rig = Rig(k=0, c=1, streamers=("s1",))
    now = rig.now
    manual = sv.make_manual_item("man", "broke", None, now - HOUR, now + 20 * HOUR, now - HOUR)
    rig.absorb = {"queued_vods": {
        "man": sv.item_to_queued_vod(manual),
        "abe": {"url": sv.save_url("abe"), "ended_at": iso(now - 20 * HOUR)}}}
    rig.tick(apply=False)
    items = rig.sched.items_snapshot()
    assert items["man"]["origin"] == "manual" and items["man"]["kind"] == "broke"
    assert items["man"]["created_at"] == now - HOUR
    assert items["abe"]["origin"] == "absorbed" and items["abe"]["kind"] == "missed"
    assert rig.occ()["cycle-1"] == "man"
    assert rig.queue() == [("abe", "save")]


def test_r33_absent_then_alive_twice_absorbs_once():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.sched.executor_seen = True
    rig.sched.reset_startup(rig.mono)
    rig.extension = False
    rig.tick()
    rig.run(2 * MIN)
    assert rig.plan["state"] == "absent"
    rig.extension = True
    assert rig.step().absorbed is True
    rig.extension = False
    rig.reports = {}
    rig.run(4 * MIN)
    assert rig.plan["state"] == "absent"
    rig.extension = True
    assert rig.step().absorbed is False


def test_r38_a_fresh_start_within_10_minutes_restores_slots_and_the_first_report_confirms():
    rig = Rig(pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.run(10 * MIN)
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    occupied = rig.occ()
    rig.sched = ss.SlotScheduler()
    rig.advance(5 * MIN)
    rig.sched.load_state(state, rig.now)
    rig.sched.reset_startup(rig.mono)
    res = rig.tick(report=False)
    assert res.state == "waiting" and res.plan["assigning"] is False
    assert rig.occ() == occupied
    res = rig.step()
    assert res.state == "alive"
    assert rig.occ() == occupied
    assert rig.plan["close"] == [] and rig.events("slot_open_reissued", [res]) == []
    assert rig.events("slot_assigned", [res]) == []


def test_r38_a_fresh_start_with_a_stale_file_adopts_three_reported_tabs_and_opens_nothing():
    rig = Rig(pinned={"s1", "s2"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s1", "s2", "s3", "s4", "s5")
    rig.tick()
    rig.run(10 * MIN)
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    tabs = set(rig.tabs)
    assert tabs == {"s1", "s2", "s3"}
    rig.sched = ss.SlotScheduler()
    rig.advance(20 * MIN)
    rig.sched.load_state(state, rig.now)
    assert all(s["streamer"] is None for s in rig.sched.slots)
    rig.sched.reset_startup(rig.mono)
    res = rig.tick(report=False)
    assert res.state == "waiting" and rig.events("slot_assigned", [res]) == []
    res = rig.step(apply=False)
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s2", "cycle-1": "s3"}
    assert rig.events("slot_assigned", [res]) == []
    assert len(rig.events("slot_extra_adopted", [res])) == 3
    assert rig.plan["close"] == []


def test_r38_waiting_assigns_nothing_for_90_seconds_then_goes_absent():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.sched.executor_seen = True
    rig.sched.reset_startup(rig.mono)
    rig.extension = False
    rig.go_live("s1")
    res = rig.tick()
    assert res.state == "waiting" and res.plan["active"] is True and res.plan["assigning"] is False
    rig.step(60)
    assert rig.plan["state"] == "waiting" and rig.occ()["cycle-1"] is None
    res = rig.step(31)
    assert res.state == "absent"
    assert rig.events("slot_executor_absent") == [{"executor": None}]


def test_r38_am24_the_capability_seed_sets_executor_seen():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.extension = False
    rig.sched.reset_startup(rig.mono)
    rig.seed_capable = True
    res = rig.tick()
    assert rig.sched.executor_seen is True and res.state == "waiting"
    rig2 = Rig(k=0, c=1, streamers=("s1",))
    rig2.extension = False
    rig2.sched.reset_startup(rig2.mono)
    assert rig2.tick().state == "none_ever"


def test_r39_a_settings_save_applies_new_ranks_and_pins_on_the_next_tick():
    rig = Rig(k=1, c=1, streamers=("s1", "s2", "s3"))
    rig.go_live("s2", "s3")
    rig.tick()
    rig.run(3 * MIN)
    before = dict(rig.slot("cycle-1"))
    rig.pinned = {"s3"}
    rig.streamers = ["s3", "s2", "s1"]
    rig.step()
    assert rig.occ()["cycle-1"] == before["streamer"]
    assert rig.slot("cycle-1")["confirmed_at"] == before["confirmed_at"]


@pytest.mark.parametrize("start,end", [((2, 1), (1, 1)), ((1, 2), (1, 1))])
def test_r39_am35_a_reduced_count_relabels_by_rank_and_drains_the_rest(start, end):
    rig = Rig(k=start[0], c=start[1], pinned={"s1", "s2"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.run(10 * MIN)
    before = {s["streamer"]: dict(s) for s in rig.plan["slots"] if s["streamer"]}
    rig.k, rig.c = end
    res = rig.step()
    assert [s["id"] for s in res.plan["slots"]] == layout(*end)
    assert rig.occ()["keep-1"] == "s1"
    assert rig.slot("keep-1")["confirmed_at"] == before["s1"]["confirmed_at"]
    kept = rig.occ()["cycle-1"]
    assert rig.slot("cycle-1")["confirmed_at"] == before[kept]["confirmed_at"]
    draining = {d["streamer"]: d["until"] for d in res.plan["draining"]}
    assert set(draining) == set(before) - {"s1", kept}
    for login, until in draining.items():
        rec = before[login]
        expected = rec["turn_ends_at"] if rec["mode"] == "turn" else rec["confirmed_at"] + M
        assert until == expected
    assert res.plan["close"] == []
    for login in draining:
        assert login in rig.tabs
    rig.run(25 * MIN)
    assert all(login not in rig.tabs for login in draining)
    assert all(c["reason"] == "turn_over" for r in rig.results for c in r.plan["close"]
               if c["streamer"] in draining)


def test_r42_o14_absent_opens_the_untabbed_plan_streams_once_capped_at_k_plus_c():
    rig = Rig(pinned={"s1"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.go_live("s1", "s2", "s3", "s4", "s5")
    rig.tick()
    rig.run(5 * MIN)
    rig.extension = False
    rig.reports = {}
    rig.run(3 * MIN)
    assert rig.plan["state"] == "absent" and rig.plan["active"] is False
    opens = [o for r in rig.results for o in r.opens]
    assert sorted(o["streamer"] for o in opens) == ["s1", "s2", "s3"]
    assert {o["kind"] for o in opens} == {"stream"}
    assert [e["streamer"] for e in rig.events("slot_fallback_open")] == [o["streamer"] for o in opens]
    rig.go_offline("s3")
    rig.run(30 * MIN)
    opens = [o for r in rig.results for o in r.opens]
    assert len(opens) == 3
    assert rig.events("slot_turn_over") == [] and rig.events("slot_preempted") == []


def test_r42_am09_a_boot_with_the_browser_closed_assigns_k_plus_c_and_opens_each_once():
    rig = Rig(pinned={"s2"}, streamers=("s1", "s2", "s3", "s4", "s5"))
    rig.sched.executor_seen = True
    rig.sched.reset_startup(rig.mono)
    rig.extension = False
    rig.go_live("s1", "s2", "s3", "s4", "s5")
    rig.tick()
    rig.run(2 * MIN)
    assert rig.plan["state"] == "absent"
    rig.run(40 * MIN)
    opens = [o for r in rig.results for o in r.opens]
    assert sorted(o["slot"] for o in opens) == ["cycle-1", "keep-1", "keep-2"]
    assert sorted(o["streamer"] for o in opens) == ["s1", "s2", "s3"]
    assert rig.events("slot_turn_over") == [] and rig.events("slot_served") == []
    assert rig.occ()["keep-1"] == "s2"


def _outage(rig):
    rig.extension = False
    rig.reports = {}
    rig.step()
    assert rig.plan["state"] == "absent"


def _outage_opens(rig, k_plus_c):
    opens = [o["streamer"] for r in rig.results for o in r.opens]
    assert len(opens) == len(set(opens)) and len(opens) <= k_plus_c
    return sorted(opens)


def test_r42_am09_a_pin_during_an_outage_relabels_without_a_second_open():
    rig = Rig(k=2, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3"))
    rig.go_live("s1", "s3")
    rig.tick()
    rig.run(M + 5 * MIN)
    assert rig.occ() == {"keep-1": "s1", "keep-2": None, "cycle-1": "s3"}
    assert rig.slot("cycle-1")["mode"] == "idle"
    _outage(rig)
    rig.run(2 * MIN)
    assert _outage_opens(rig, 3) == ["s1", "s3"]
    rig.pinned = {"s1", "s3"}
    rig.run(3 * MIN)
    assert rig.occ()["keep-2"] == "s3"
    assert _outage_opens(rig, 3) == ["s1", "s3"]


def test_r42_am09_a_save_turn_that_goes_live_during_an_outage_prefills_without_a_second_open():
    rig = Rig(k=1, c=1, pinned={"s2"}, streamers=("s1", "s2"))
    rig.tick()
    rig.sched.add_item(card_item("s2", rig.now, age_s=HOUR), rig.item_inp())
    rig.run(3 * MIN)
    assert rig.occ() == {"keep-1": None, "cycle-1": "s2"} and rig.slot("cycle-1")["entry"] == "save"
    _outage(rig)
    rig.run(2 * MIN)
    assert [(o["streamer"], o["kind"]) for r in rig.results for o in r.opens] == [("s2", "vod")]
    rig.go_live("s2")
    rig.run(3 * MIN)
    assert rig.occ() == {"keep-1": "s2", "cycle-1": None}
    assert rig.slot("keep-1")["entry"] == "live"
    assert [(o["streamer"], o["kind"]) for r in rig.results for o in r.opens] == [("s2", "vod")]


def test_r39_am09_am35_a_count_change_during_an_outage_carries_the_desktop_opens():
    rig = Rig(k=1, c=2, pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3")
    rig.tick()
    rig.run(5 * MIN)
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s2", "cycle-2": "s3"}
    _outage(rig)
    rig.run(2 * MIN)
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]
    rig.k, rig.c = 2, 1
    rig.run(3 * MIN)
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s3", "cycle-1": "s2"}
    assert rig.slot("keep-2")["lent"] is True
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]


FIVE = ("s1", "s2", "s3", "s4", "s5")


def _outage_rig(k, c, pinned, opened):
    rig = Rig(k=k, c=c, pinned=set(pinned), streamers=FIVE)
    rig.go_live(*FIVE)
    rig.tick()
    rig.run(5 * MIN)
    _outage(rig)
    rig.run(2 * MIN)
    assert _outage_opens(rig, k + c) == opened
    return rig


def _draining(rig):
    return sorted(d["streamer"] for d in rig.plan["draining"])


@pytest.mark.parametrize("k, c, draining", [(1, 2, ["s2"]), (0, 3, ["s1", "s2"])])
def test_r39_am09_am35_a_count_change_that_drains_a_desktop_open_during_an_outage_keeps_k_plus_c_opens(
        k, c, draining):
    rig = _outage_rig(2, 1, ("s1", "s2"), ["s1", "s2", "s3"])
    rig.k, rig.c = k, c
    rig.run(30 * MIN)
    assert rig.plan["state"] == "absent"
    assert _draining(rig) == draining
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]
    assert [s for s in rig.occ().values() if s] == [s for s in ("s1", "s3") if s not in draining]


def test_r39_am09_am35_an_occupant_gone_offline_then_a_count_change_during_an_outage_keeps_k_plus_c_opens():
    rig = _outage_rig(2, 1, ("s1", "s2"), ["s1", "s2", "s3"])
    rig.go_offline("s2")
    rig.run(5 * MIN)
    assert rig.occ() == {"keep-1": "s1", "keep-2": None, "cycle-1": "s3"}
    rig.k, rig.c = 1, 2
    rig.run(30 * MIN)
    assert _draining(rig) == []
    assert rig.occ() == {"keep-1": "s1", "cycle-1": "s3", "cycle-2": None}
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]


def test_r39_am09_am35_growing_the_count_during_an_outage_opens_only_the_new_room():
    rig = _outage_rig(1, 1, ("s1",), ["s1", "s2"])
    rig.k, rig.c = 0, 3
    rig.run(30 * MIN)
    assert _draining(rig) == ["s1"]
    # cycle-2 carries the spent budget, so one new stream opens, not two.
    assert rig.occ() == {"cycle-1": "s2", "cycle-2": None, "cycle-3": "s3"}
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]


def test_r16_r39_am09_am35_one_more_keep_slot_during_an_outage_opens_exactly_one_more_stream():
    rig = _outage_rig(1, 1, ("s1",), ["s1", "s2"])
    rig.k, rig.c = 2, 1
    rig.run(30 * MIN)
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]
    assert rig.occ() == {"keep-1": "s1", "keep-2": "s3", "cycle-1": "s2"}
    assert rig.slot("keep-2")["lent"] is True


def test_r39_am09_am35_several_count_changes_in_one_outage_keep_k_plus_c_opens():
    rig = _outage_rig(1, 2, ("s1",), ["s1", "s2", "s3"])
    rig.k, rig.c = 0, 3
    rig.run(5 * MIN)
    assert _draining(rig) == ["s1"]
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]
    rig.k, rig.c = 2, 1
    rig.run(30 * MIN)
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]


def test_r42_absent_opens_wait_for_the_pause_lift():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.go_live("s1")
    rig.tick()
    rig.run(2 * MIN)
    rig.extension = False
    rig.reports = {}
    rig.auto_paused = True
    rig.run(5 * MIN)
    assert [o for r in rig.results for o in r.opens] == []
    rig.auto_paused = False
    rig.step()
    assert [o["streamer"] for r in rig.results for o in r.opens] == ["s1"]


def test_r42_the_outage_notice_comes_once_after_300_seconds_and_back_shifts_the_clocks():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    ends = rig.slot("cycle-1")["turn_ends_at"]
    rig.run(3 * MIN)
    rig.extension = False
    rig.reports[KEY]["mono"] = rig.mono
    rig.reports[KEY]["epoch"] = rig.now
    absent_results = rig.run(25 * MIN)
    notices = [n for r in absent_results for n in r.notices]
    assert notices == ["outage"]
    absent_at = next(i for i, r in enumerate(absent_results) if r.state == "absent")
    outage_at = next(i for i, r in enumerate(absent_results) if "outage" in r.notices)
    assert (outage_at - absent_at) * MIN >= ss.SLOT_OUTAGE_NOTIFY_SECONDS
    assert absent_results[-1].tooltip == ss.TOOLTIP_ABSENT
    absent_s = (len(absent_results) - absent_at - 1) * MIN + MIN
    rig.extension = True
    res = rig.step()
    back = rig.events("slot_executor_back", [res])
    assert back == [{"executor": KEY, "absent_s": absent_s}]
    assert {"gap_s": absent_s, "reason": "absent"} in rig.events("slot_clock_shifted", [res])
    assert rig.slot("cycle-1")["turn_ends_at"] == ends + absent_s


def test_r43_none_ever_notice_on_each_entry():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.capable = False
    first = rig.tick()
    assert first.notices == ["none_ever"]
    assert rig.step().notices == []
    rig.slot_mode = False
    off = rig.step()
    assert off.state == "off" and off.notices == []
    rig.slot_mode = True
    assert rig.step().notices == ["none_ever"]
    assert rig.step().notices == []


def test_r44_a_wake_gap_shifts_every_clock_once_by_the_sleep_alone():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step()
    keep = dict(rig.slot("keep-1"))
    cycle = dict(rig.slot("cycle-1"))
    gap = 2 * HOUR
    rig.advance(gap + MIN)
    res = rig.tick(wake_gap=gap)
    assert rig.events("slot_clock_shifted", [res]) == [{"gap_s": gap, "reason": "sleep"}]
    assert rig.slot("keep-1")["confirmed_at"] == keep["confirmed_at"] + gap
    assert rig.slot("cycle-1")["turn_ends_at"] == cycle["turn_ends_at"] + gap
    rig.step()
    assert rig.slot("cycle-1")["turn_ends_at"] == cycle["turn_ends_at"] + gap
    assert rig.occ()["cycle-1"] == "s2"


def test_r45_turns_end_on_replans_between_polls():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"), check_interval=300)
    rig.go_live("s1", "s2")
    rig.tick()
    rig.step(30, authoritative=False)
    ends = rig.slot("cycle-1")["turn_ends_at"]
    while rig.now < ends:
        rig.step(30, authoritative=(int(rig.now - BASE) % 300 == 0))
    assert rig.events("slot_turn_over")[0]["streamer"] == "s1"
    assert rig.now - ends < 30


def test_r46_turning_off_publishes_null_and_hands_over_untabbed_live_streams_and_items():
    rig = Rig(k=0, c=1, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.run(3 * MIN)
    rig.close_tab("s2")
    rig.step()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.slot_mode = False
    res = rig.step()
    assert res.plan is None and res.state == "off" and res.tooltip is None
    assert rig.events("slot_mode_inactive", [res]) == [{"reason": "setting_off"}]
    # s1 has a tab, s2 was dismissed for this broadcast.
    assert res.off_transition["open_live"] == ["s3", "s4"]
    assert [i["login"] for i in res.off_transition["items"]] == ["zed"]
    assert res.persist is True
    assert rig.sched.items_snapshot() == {}
    again = rig.step()
    assert again.off_transition is None and again.events == []


def test_r46_r42_turning_off_during_an_outage_does_not_reopen_the_desktops_own_opens():
    rig = _outage_rig(2, 1, (), ["s1", "s2", "s3"])
    rig.slot_mode = False
    res = rig.step()
    assert res.off_transition["open_live"] == ["s4", "s5"]


def test_r46_r42_turning_off_after_a_count_change_in_an_outage_leaves_out_the_draining_desktop_open():
    rig = _outage_rig(2, 1, (), ["s1", "s2", "s3"])
    rig.k, rig.c = 1, 1
    rig.run(3 * MIN)
    assert len(_draining(rig)) == 1
    assert _outage_opens(rig, 3) == ["s1", "s2", "s3"]
    rig.slot_mode = False
    res = rig.step()
    assert res.off_transition["open_live"] == ["s4", "s5"]


def test_r46_r42_turning_off_in_a_paused_outage_hands_over_every_untabbed_live_stream():
    rig = Rig(k=2, c=1, streamers=FIVE)
    rig.go_live(*FIVE)
    rig.tick()
    rig.run(5 * MIN)
    rig.paused = True
    _outage(rig)
    rig.run(3 * MIN)
    assert [o for r in rig.results for o in r.opens] == []
    rig.slot_mode = False
    res = rig.step()
    assert res.off_transition["open_live"] == list(FIVE)


def test_r46_items_restored_while_slot_mode_is_off_go_to_the_normal_path_once():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    state = rig.sched.to_state(rig.now)
    rig.sched = ss.SlotScheduler()
    rig.sched.load_state(state, rig.now + HOUR)
    rig.sched.reset_startup(rig.mono)
    rig.slot_mode = False
    res = rig.step()
    assert res.state == "off" and res.plan is None
    assert res.off_transition == {"open_live": [], "items": [state["save_queue"]["zed"]]}
    assert rig.events("slot_mode_inactive", [res]) == []
    assert rig.step().off_transition is None


def test_r04_r47_the_invariant_checker_fails_on_a_violation():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    res = rig.step()
    inp = rig.inputs()
    record = next(s for s in rig.sched.slots if s["streamer"] == "s1")
    before = {"s1": dict(record)}
    early = ss.TickResult(**dict(res.__dict__, plan=dict(
        res.plan, slots=[dict(res.plan["slots"][0], streamer="s2")],
        close=[{"streamer": "s1", "reason": "turn_over"}])))
    with pytest.raises(AssertionError):
        _check_invariants(rig.sched, inp, early, before, layout(0, 1), set(), 0)
    doubled = ss.TickResult(**dict(res.__dict__, plan=dict(
        res.plan, close=[{"streamer": "s1", "reason": "offline"}])))
    with pytest.raises(AssertionError):
        _check_invariants(rig.sched, inp, doubled, before, layout(0, 1), set(), 0)


def test_r47_no_slot_tab_is_listed_for_closing_before_m_confirmed_minutes():
    rig = Rig(pinned={"s1", "s4", "s6"}, streamers=STREAMERS, m=20 * MIN)
    rig.go_live("s2", "s3", "s4", "s5", "s6", "s7")
    rig.tick()
    script = {
        1: lambda: rig.go_live("s1"),
        4: lambda: rig.go_offline("s5"),
        9: lambda: rig.go_live("s2", started=iso(rig.now)),
        14: lambda: rig.close_tab(rig.occ()["cycle-1"] or "s9"),
        30: lambda: rig.sched.add_item(card_item("zed", rig.now, age_s=2 * HOUR), rig.item_inp()),
        45: lambda: setattr(rig, "auto_paused", True),
        50: lambda: setattr(rig, "auto_paused", False),
        70: lambda: rig.go_live("s8"),
        90: lambda: rig.go_offline("s3"),
        100: lambda: setattr(rig, "streamers", ["s8", "s1", "s2", "s3", "s4", "s5", "s6", "s7"]),
        220: lambda: rig.go_live("s7", started=iso(rig.now)),
    }
    for i in range(260):
        if i in script:
            script[i]()
        rig.step()
    names = {n for r in rig.results for (n, _) in r.events}
    assert {"slot_turn_over", "slot_hold_scheduled", "slot_displaced", "slot_idle",
            "slot_preempted", "streak_item_added"} <= names


# ---------------------------------------------------------------------------
# The interface the tray uses (3.13)
# ---------------------------------------------------------------------------

def test_c13_complete_item_frees_a_save_turn_and_the_next_plan_closes_it_as_save_done():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.step()
    rig.step()
    assert rig.occ()["cycle-1"] == "zed" and "zed" in rig.tabs
    done, events = rig.sched.complete_item("zed", "already_saved", rig.now)
    assert done is True
    assert events == [("streak_item_done", {"streamer": "zed", "reason": "already_saved"})]
    assert rig.sched.complete_item("zed", "already_saved", rig.now) == (False, [])
    rig.step()
    assert rig.closes() == {"zed": "save_done"} and rig.occ()["cycle-1"] is None


def test_c13_served_and_session_queries():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    assert rig.sched.tracked_session("s1") == iso(BASE)
    assert rig.sched.tracked_session("s2") is None
    assert rig.sched.is_broadcast_served("s1") is False
    rig.run(M + 2 * MIN)
    assert rig.sched.is_broadcast_served("s1") is True
    rig.go_live("s1", started=iso(rig.now))
    rig.step()
    assert rig.sched.is_broadcast_served("s1") is False


# ---------------------------------------------------------------------------
# Plan shape, seq, persistence, tray text (3.3, 3.7, A36)
# ---------------------------------------------------------------------------

def _assert_plan_types(plan):
    assert plan["v"] == 1 and isinstance(plan["seq"], int) and plan["seq"] >= 0
    assert isinstance(plan["generated_at"], int)
    assert plan["live_as_of"] is None or isinstance(plan["live_as_of"], int)
    assert plan["state"] in ("none_ever", "waiting", "alive", "absent")
    assert plan["active"] is (plan["state"] in ("waiting", "alive"))
    assert isinstance(plan["assigning"], bool)
    assert plan["assigning"] is False or plan["state"] == "alive"
    assert plan["pause"] in (None, "manual", "live")
    assert plan["executor"] is None or isinstance(plan["executor"], str)
    assert isinstance(plan["turn_minutes"], int)
    for key in ("slots", "close", "queue", "served", "dismissed", "draining"):
        assert isinstance(plan[key], list)
    for s in plan["slots"]:
        for key in ("assigned_at", "confirmed_at", "turn_ends_at", "hold_until", "deadline_at"):
            assert s[key] is None or isinstance(s[key], int)
        assert isinstance(s["lent"], bool) and isinstance(s["verify"], bool)
        assert s["item_kind"] in (None, "broke", "in_danger", "missed")
        if s["streamer"]:
            assert s["url"] in (f"https://www.twitch.tv/{s['streamer']}?sm=1",
                                f"https://www.twitch.tv/save-streak/{s['streamer']}?sm=1")
    for c in plan["close"]:
        assert set(c) == {"streamer", "reason"}
    assert len(plan["queue"]) <= ss.SLOT_QUEUE_PUBLISH_MAX
    for q in plan["queue"]:
        assert set(q) == {"streamer", "entry", "deadline_at", "urgent", "manual"}
        assert q["deadline_at"] is None or isinstance(q["deadline_at"], int)
    for d in plan["draining"]:
        assert set(d) == {"streamer", "until"} and isinstance(d["until"], int)
    assert plan["next_change_at"] is None or isinstance(plan["next_change_at"], int)
    assert plan["served"] == sorted(plan["served"]) and plan["dismissed"] == sorted(plan["dismissed"])
    json.dumps(plan)


def test_c03_every_plan_key_is_present_with_its_type_in_every_state():
    seen = set()
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.capable = False
    _assert_plan_types(rig.tick().plan)
    seen.add(rig.plan["state"])
    rig = Rig(pinned={"s1"}, streamers=STREAMERS)
    rig.sched.executor_seen = True
    rig.sched.reset_startup(rig.mono)
    rig.extension = False
    rig.go_live(*STREAMERS[:6])
    for _ in range(3):
        rig.step()
        _assert_plan_types(rig.plan)
        seen.add(rig.plan["state"])
    rig.extension = True
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    for i in range(80):
        rig.busy = "paused" if 30 <= i < 33 else None
        rig.step()
        _assert_plan_types(rig.plan)
        seen.add(rig.plan["state"])
    assert seen == {"none_ever", "waiting", "absent", "alive"}


def test_c03_seq_moves_only_when_a_field_the_extension_acts_on_changes():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    first = rig.tick()
    seq = first.plan["seq"]
    confirmed = rig.step()
    assert confirmed.plan["seq"] == seq
    assert confirmed.plan["slots"][0]["confirmed_at"] is not None
    assert rig.events("slot_plan_changed", [confirmed]) == []
    rig.run(5 * MIN)
    assert rig.plan["seq"] == seq
    rig.close_tab("s1")
    changed = rig.step()
    assert changed.plan["seq"] == seq + 1
    assert rig.events("slot_plan_changed", [changed])[0]["seq"] == seq + 1


def test_c03_c07_seq_is_restored_from_a_state_file_older_than_600_seconds():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(5 * MIN)
    rig.close_tab("s1")
    rig.step()
    seq = rig.plan["seq"]
    state = rig.sched.to_state(rig.now)
    fresh = ss.SlotScheduler()
    fresh.load_state(state, rig.now + 3 * HOUR)
    assert fresh.seq == seq
    assert all(s["streamer"] is None for s in fresh.slots) or fresh.slots == []
    rig.sched = fresh
    rig.advance(3 * HOUR)
    fresh.reset_startup(rig.mono)
    assert rig.tick().plan["seq"] > seq


def test_c07_state_round_trips_through_json():
    rig = Rig(pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.run(M + 5 * MIN)
    rig.close_tab(rig.occ()["cycle-1"])
    rig.step()
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    assert state["v"] == 1 and "held" not in state
    copy = ss.SlotScheduler()
    copy.load_state(state, rig.now + 60)
    again = json.loads(json.dumps(copy.to_state(rig.now)))
    assert again == state


def test_c07_fresh_start_restore_rules():
    rig = Rig(k=0, c=1, streamers=("s1", "s2"))
    rig.go_live("s1", "s2")
    rig.tick()
    rig.run(3 * MIN)
    rig.close_tab("s1")
    rig.step()
    rig.sched.add_item(card_item("zed", rig.now, age_s=HOUR), rig.item_inp())
    rig.sched.add_item(card_item("old", rig.now, age_s=23 * HOUR), rig.item_inp())
    state = json.loads(json.dumps(rig.sched.to_state(rig.now)))
    state["served"]["BAD NAME"] = {"session": iso(BASE), "at": BASE, "how": "turn"}
    state["save_queue"]["x" * 70] = dict(state["save_queue"]["zed"], login="x" * 70)
    young = ss.SlotScheduler()
    young.load_state(state, rig.now + ss.SLOT_STATE_MAX_AGE_SECONDS)
    assert [s["streamer"] for s in young.slots] == ["s2"]
    assert young.executor == KEY and young.executor_seen is True
    assert set(young.dismissed) == {"s1"} and "BAD NAME" not in young.served
    assert set(young.items_snapshot()) == {"zed", "old"}
    old = ss.SlotScheduler()
    old.load_state(state, rig.now + 2 * HOUR)
    assert all(s["streamer"] is None for s in old.slots) and old.executor is None
    assert set(old.dismissed) == {"s1"} and old.executor_seen is True
    assert set(old.items_snapshot()) == {"zed"}
    for bad in (None, [], "text", {"v": 2, "seq": 9}, {"v": 1, "seq": "x", "slots": "no"}):
        empty = ss.SlotScheduler()
        empty.load_state(bad, rig.now)
        assert empty.seq == 0 and empty.items_snapshot() == {} and empty.executor_seen is False


def test_c07_r06_a_restored_keep_occupant_keeps_the_records_of_its_current_broadcast():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2"))
    rig.go_live("s1")
    rig.tick()
    rig.run(M + 5 * MIN)
    rig.go_live("s1", started=iso(rig.now))
    session = rig.live["s1"]
    rig.run(M + 5 * MIN)
    rig.close_tab("s1", "window_closed")
    rig.run(3 * MIN)
    assert rig.sched.served["s1"]["session"] == session
    assert rig.sched.reissued["s1"]["session"] == session
    _restart(rig, 2 * MIN)
    mark = len(rig.results)
    res = rig.tick(authoritative=False)
    assert res.state == "alive" and rig.occ()["keep-1"] == "s1"
    rig.run(3 * MIN)
    assert rig.events("slot_served", rig.results[mark:]) == []
    assert rig.sched.served["s1"]["session"] == session
    assert rig.sched.reissued["s1"]["window_closed"] == 1


def test_am36_fixed_tooltip_strings():
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.capable = False
    assert rig.tick().tooltip == "Slot mode: needs extension 1.12, opening normally"
    rig = Rig(k=0, c=1, streamers=("s1",))
    rig.go_live("s1")
    rig.tick()
    rig.busy = "paused"
    assert rig.step().tooltip == "Slot mode: paused in the browser"
    rig.busy = None
    rig.extension = False
    rig.run(4 * MIN)
    assert rig.plan["state"] == "absent"
    assert rig.results[-1].tooltip == "Slot mode: browser extension not reporting"
    rig.slot_mode = False
    assert rig.step().tooltip is None


def test_am36_the_running_tooltip_summarizes_the_plan_within_the_cap():
    rig = Rig(k=1, c=1, pinned={"s1"}, streamers=("s1", "s2", "s3", "s4"))
    rig.go_live("s1", "s2", "s3", "s4")
    rig.tick()
    rig.step()
    rig.run(18 * MIN)
    assert rig.results[-1].tooltip == "Slots: s1 / rotating: s2 12m / queue 2"
    long_names = tuple(f"{c}" * 25 for c in "abcd")
    rig = Rig(k=2, c=1, pinned=set(long_names[:2]), streamers=long_names)
    rig.go_live(*long_names)
    res = rig.tick()
    assert len(res.tooltip) <= ss.SLOT_TOOLTIP_MAX < 128
    assert res.tooltip.startswith("Slots: ")
