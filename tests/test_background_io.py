"""The desktop I/O of both backgrounds, run in Node (build plan WP4b task 12).

Each background.js is loaded in a Node vm with a stub chrome or browser
namespace (storage.local and storage.session in memory, every other member a
no-op that resolves) and a fake fetch (Chrome) or fake XMLHttpRequest
(Firefox), so the module-scope listeners and the init IIFE run harmlessly;
then the top-level functions are called. Playwright cannot load a Firefox
extension, so this is the only automated check of the Firefox-only I/O
(postJson over XHR).
"""
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKGROUNDS = {
    "chrome": ROOT / "chrome_extension" / "background.js",
    "firefox": ROOT / "firefox_extension" / "background.js",
}

# The harness. BG_PATH, BG_KIND, SEED (storage.local before the load), WORLD
# (windows and tabs before the load) and CASE are prepended as constants;
# the case body runs after the init IIFE has finished and prints one JSON
# line.
HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const nodeCrypto = require("crypto");

const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

// delays: {key: ms} a set of that key waits before it lands (a slow
// storage write, for the race cases).
function memArea(store, delays = {}) {
  return {
    async get(keys) {
      if (keys === null || keys === undefined) return clone(store);
      const list = typeof keys === "string" ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
      const out = {};
      for (const k of list) if (Object.prototype.hasOwnProperty.call(store, k)) out[k] = clone(store[k]);
      return out;
    },
    async set(items) {
      const wait = Math.max(0, ...Object.keys(items).map((k) => delays[k] || 0));
      if (wait) await new Promise((r) => setTimeout(r, wait));
      for (const [k, v] of Object.entries(items)) store[k] = clone(v);
    },
    async remove(keys) {
      for (const k of [].concat(keys)) delete store[k];
    },
  };
}

// Any member not given below: a callable no-op resolving to undefined, with
// addListener and friends, never a thenable.
function anyStub() {
  const fn = function () { return Promise.resolve(undefined); };
  return new Proxy(fn, {
    get(t, prop) {
      if (prop === "then") return undefined;
      if (prop === "addListener" || prop === "removeListener") return () => {};
      if (prop === "hasListener") return () => false;
      return anyStub();
    },
    apply() { return Promise.resolve(undefined); },
  });
}

function stub(target) {
  return new Proxy(target, {
    get(t, prop) {
      if (prop === "then") return undefined;
      if (Object.prototype.hasOwnProperty.call(t, prop)) {
        const v = t[prop];
        return v && typeof v === "object" && !Array.isArray(v) ? stub(v) : v;
      }
      return anyStub();
    },
  });
}

const local = {};
const session = {};
const delays = {};
const requests = [];
const logs = [];
const badgeTexts = [];
let responder = () => ({ status: 503, text: "" });
let nextTabId = 100;

// The windows and tabs tabs.query, tabs.get, tabs.move and the windows
// calls see (the stream window cases). Empty unless WORLD or a case fills
// it: then tabs.query finds nothing and tabs.get and windows.get throw.
// Tabs made by tabs.create are not added. calls records moves, updates,
// removes and window creations; onMove(id, windowId) runs right after a
// move lands. lastFocused, when a case sets it, is what
// windows.getLastFocused answers (by default window 1, focused). alarms
// holds the alarms alarms.get finds (WORLD.alarms and every one created);
// alarmsCreated lists the alarms.create calls.
const world = { windows: {}, tabs: {}, calls: [], onMove: null, nextWindowId: 50, lastFocused: null,
  alarms: {}, alarmsCreated: [] };
Object.assign(world.windows, clone(WORLD.windows || {}));
Object.assign(world.tabs, clone(WORLD.tabs || {}));
Object.assign(world.alarms, clone(WORLD.alarms || {}));

function windowView(w, populate) {
  const out = clone(w);
  if (populate) out.tabs = Object.values(world.tabs).filter((t) => t.windowId === w.id).map(clone);
  return out;
}

const api = stub({
  storage: { local: memArea(local, delays), session: memArea(session) },
  permissions: { contains: async () => true },
  tabs: {
    query: async (q = {}) => Object.values(world.tabs).filter((t) =>
      (q.windowId === undefined || t.windowId === q.windowId) &&
      (q.active === undefined || !!t.active === q.active)).map(clone),
    get: async (id) => {
      if (!world.tabs[id]) throw new Error(`No tab with id: ${id}.`);
      return clone(world.tabs[id]);
    },
    move: async (id, props) => {
      world.calls.push(["tabs.move", id, props.windowId]);
      const tab = world.tabs[id];
      if (!tab) throw new Error(`No tab with id: ${id}.`);
      tab.windowId = props.windowId;
      if (world.onMove) world.onMove(id, props.windowId);
      return clone(tab);
    },
    update: async (id, props) => {
      world.calls.push(["tabs.update", id, clone(props)]);
      return world.tabs[id] ? clone(world.tabs[id]) : undefined;
    },
    remove: async (id) => {
      world.calls.push(["tabs.remove", id]);
      delete world.tabs[id];
    },
    create: async (opts) => ({ id: nextTabId++, windowId: opts.windowId || 1, url: opts.url, active: !!opts.active }),
    sendMessage: async () => { throw new Error("Could not establish connection. Receiving end does not exist."); },
  },
  windows: {
    getLastFocused: async () => (world.lastFocused ? clone(world.lastFocused)
      : { id: 1, type: "normal", incognito: false, focused: true }),
    getAll: async (o = {}) => Object.values(world.windows).map((w) => windowView(w, !!o.populate)),
    get: async (id, o = {}) => {
      if (!world.windows[id]) throw new Error(`No window with id: ${id}.`);
      return windowView(world.windows[id], !!o.populate);
    },
    create: async (o = {}) => {
      world.calls.push(["windows.create", clone(o)]);
      const id = world.nextWindowId++;
      world.windows[id] = { id, type: "normal", incognito: false, focused: false, state: "normal",
        left: o.left ?? 0, top: o.top ?? 0, width: o.width ?? 1000, height: o.height ?? 800 };
      if (typeof o.tabId === "number" && world.tabs[o.tabId]) world.tabs[o.tabId].windowId = id;
      return windowView(world.windows[id], true);
    },
    update: async (id, props) => {
      world.calls.push(["windows.update", id, clone(props)]);
      return world.windows[id] ? windowView(world.windows[id], false) : undefined;
    },
  },
  alarms: {
    get: async (name) => clone(world.alarms[name]),
    create: async (name, info) => {
      world.alarmsCreated.push([name, clone(info)]);
      world.alarms[name] = { name, scheduledTime: info && info.when };
    },
    clear: async (name) => { delete world.alarms[name]; return true; },
  },
  runtime: { getURL: (p) => `ext://test/${p}` },
  action: {
    setBadgeText: async (d) => { badgeTexts.push(d.text); },
    setBadgeBackgroundColor: async () => {},
  },
});

function record(url, method, headers, body) {
  let parsed = null;
  if (typeof body === "string" && body) {
    try { parsed = JSON.parse(body); } catch (e) { parsed = body; }
  }
  requests.push({ url: String(url), path: new URL(String(url)).pathname, method, headers, body: parsed });
}

async function fakeFetch(url, init = {}) {
  record(url, init.method || "GET", init.headers || {}, init.body);
  const r = responder(new URL(String(url)).pathname, init.method || "GET");
  if (r.fail) {
    const e = new Error(r.fail === "timeout" ? "The operation was aborted due to timeout" : "Failed to fetch");
    e.name = r.fail === "timeout" ? "TimeoutError" : "TypeError";
    throw e;
  }
  const text = r.text || "";
  return {
    status: r.status,
    ok: r.status >= 200 && r.status < 300,
    text: async () => text,
    json: async () => JSON.parse(text),
  };
}

class FakeXHR {
  constructor() { this.headers = {}; this.status = 0; this.responseText = ""; this.response = null; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) {
    record(this.url, this.method, this.headers, body);
    const r = responder(new URL(this.url).pathname, this.method);
    setTimeout(() => {
      if (r.fail === "timeout") { if (this.ontimeout) this.ontimeout(); return; }
      if (r.fail) { if (this.onerror) this.onerror(); return; }
      this.status = r.status;
      this.responseText = r.text || "";
      if (this.responseType === "json") {
        try { this.response = JSON.parse(this.responseText); } catch (e) { this.response = null; }
      } else {
        this.response = this.responseText;
      }
      if (this.onload) this.onload();
    }, 0);
  }
}

const quiet = {
  log: (...a) => logs.push(a.map(String).join(" ")),
  warn: (...a) => logs.push(a.map(String).join(" ")),
  error: (...a) => logs.push(a.map(String).join(" ")),
  info: () => {}, debug: () => {},
};

const context = {
  console: quiet, setTimeout, clearTimeout, URL, AbortSignal, AbortController,
  crypto: nodeCrypto.webcrypto, TextEncoder, TextDecoder,
};
if (BG_KIND === "chrome") {
  context.chrome = api;
  context.fetch = fakeFetch;
} else {
  context.browser = api;
  context.XMLHttpRequest = FakeXHR;
}
process.on("unhandledRejection", () => {});

local.instanceId = "a1b2c3d4";
Object.assign(local, clone(SEED));
vm.createContext(context);
vm.runInContext(fs.readFileSync(BG_PATH, "utf8"), context, { filename: BG_PATH });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function ready() {
  for (let i = 0; i < 200; i++) {
    if (logs.some((l) => /(Service worker|Event page) ready/.test(l))) return;
    await sleep(10);
  }
  throw new Error("the init IIFE did not finish: " + logs.slice(-5).join(" | "));
}

(async () => {
  await ready();
  requests.length = 0;
  const out = await CASE(context);
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
"""


def _run(kind: str, case: str, tmp_path: Path, seed=None, world=None, bg_path=None):
    """bg_path: another copy of the background to load (a switch flipped)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = "\n".join([
        f"const BG_PATH = {json.dumps(str(bg_path or BACKGROUNDS[kind]))};",
        f"const BG_KIND = {json.dumps(kind)};",
        f"const SEED = {json.dumps(seed or {})};",
        f"const WORLD = {json.dumps(world or {})};",
        f"const CASE = {case};",
        HARNESS,
    ])
    path = tmp_path / f"io_{kind}.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


FORWARD_CASE = r"""async (ctx) => {
  const event = { status: "broke", streamer: "bob", count: 3, detected_at: "2026-09-29T10:00:00.000Z",
    card_age_s: 3600, card_age_unit_s: 3600, login_verified: true, source: "bell" };
  const answers = {
    fresh: { status: 200, text: JSON.stringify({ verdict: "fresh", item: true }) },
    stale: { status: 200, text: JSON.stringify({ verdict: "stale", item: false }) },
    unknown: { status: 200, text: JSON.stringify({ verdict: "later-kind", item: true }) },
    no_content: { status: 204, text: "" },
    not_json: { status: 200, text: "ok" },
    bad: { status: 400, text: "bad" },
    timeout: { fail: "timeout" },
    down: { fail: "network" },
  };
  const out = {};
  for (const [name, answer] of Object.entries(answers)) {
    responder = (path) => (path === "/streak_event" ? answer : { status: 503 });
    out[name] = await ctx.forwardStreakEvent(event);
  }
  const posts = requests.filter((r) => r.path === "/streak_event");
  out.posts = posts.length;
  out.body = posts[0].body;
  out.method = posts[0].method;
  out.ctype = posts[0].headers["Content-Type"];
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_c05_forward_streak_event_reads_the_desktop_verdict(kind, tmp_path):
    """Plan 3.5 and DESIGN 12.6: a 200 JSON answer gives its verdict and
    item; a 204 (a 1.11 desktop), a 200 that is not JSON and an unknown
    verdict give no verdict but still count as delivered; a 400 and a
    timeout count as not delivered."""
    got = _run(kind, FORWARD_CASE, tmp_path)
    assert got["fresh"] == {"ok": True, "verdict": "fresh", "item": True, "reached": True}
    assert got["stale"] == {"ok": True, "verdict": "stale", "item": False, "reached": True}
    for name in ("unknown", "no_content", "not_json"):
        assert got[name]["ok"] is True and got[name]["verdict"] is None and got[name]["item"] is False, name
    assert got["bad"]["ok"] is False and got["bad"]["verdict"] is None and got["bad"]["reached"] is True
    for name in ("timeout", "down"):
        assert got[name]["ok"] is False and got[name]["verdict"] is None and got[name]["reached"] is False, name
    assert got["posts"] == 8
    assert got["method"] == "POST" and got["ctype"] == "application/json"
    assert got["body"]["source"] == "bell" and got["body"]["card_age_s"] == 3600


FRESH_OVER_SAVE_CASE = r"""async (ctx) => {
  const now = Date.now();
  const card = { status: "broke", streamer: "bob", count: 5, detected_at: new Date(now).toISOString(),
    login_verified: true, source: "bell" };
  const saves = {
    local: { at: new Date(now - 5 * 60 * 1000).toISOString(), source: "local", count: 5, page_url: "", pending: true },
    desktop: { at: new Date(now - 5 * 60 * 1000).toISOString(), source: "desktop", seenAt: now, count: 5 },
  };
  const out = {};
  for (const [name, save] of Object.entries(saves)) {
    delete local.atRiskStreaks;
    local.savedStreaks = { bob: save };
    // Control: no verdict (the desktop down), so the local rule drops the
    // card because the save counts.
    responder = (path) => (path === "/streak_event" ? { fail: "network" } : { status: 503 });
    await ctx.handleStreakCard(card);
    const control = !!(local.atRiskStreaks && local.atRiskStreaks.bob);
    // The desktop judges the same card fresh: listed, whatever the save.
    responder = (path) => (path === "/streak_event"
      ? { status: 200, text: JSON.stringify({ verdict: "fresh", item: true }) } : { status: 503 });
    badgeTexts.length = 0;
    await ctx.handleStreakCard(card);
    const row = local.atRiskStreaks ? local.atRiskStreaks.bob : null;
    out[name] = { control, count: row ? row.count : null, key: row ? row.card_key : null,
      badge: badgeTexts.length ? badgeTexts[badgeTexts.length - 1] : null };
  }
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_c05_f15_a_fresh_verdict_lists_a_card_a_counting_local_save_would_drop(kind, tmp_path):
    """F15 and DESIGN 12.6: the desktop's verdict decides, and the extension
    does not repeat the v1.11.2 saved check on top of it. With a save that
    counts here (this browser's own pending report, or one the desktop
    lists) the local rule drops a card of the same count; a "fresh" verdict
    for that card lists it and the badge counts it."""
    got = _run(kind, FRESH_OVER_SAVE_CASE, tmp_path)
    for name in ("local", "desktop"):
        assert got[name]["control"] is False, f"{name}: the local rule listed the card"
        assert got[name]["count"] == 5 and got[name]["key"] == "broke:bob:5", f"{name}: {got[name]}"
        assert got[name]["badge"] == "1", f"{name}: {got[name]}"


RESCUE_CASE = r"""async (ctx) => {
  const offer = (id) => ({ id, created_at: "2026-09-29T10:00:00.000Z", batch_size: 3, rotate_minutes: 30,
    candidates: [{ streamer: "bob", url: "https://www.twitch.tv/save-streak/bob", kind: "ended", ended_at: null }] });
  const out = {};
  const acks = () => requests.filter((r) => r.path === "/rescue_ack");
  // The stored claim's id at the moment each ack POST goes out.
  const atAck = [];
  const seen = (answer) => (path) => {
    if (path !== "/rescue_ack") return { status: 503 };
    atAck.push(local.rescueClaim ? local.rescueClaim.id : null);
    return answer;
  };
  out.atAck = atAck;

  // A 409: the claim goes and nothing opens.
  responder = seen({ status: 409 });
  await ctx.maybeStartRescueFromConfig(offer("off-1"));
  out.refusedBody = acks()[0].body;
  out.claimAfter409 = local.rescueClaim === undefined;
  out.sessionAfter409 = local.rescueSession === undefined;

  // The answer is lost: the claim stays for the next tick.
  requests.length = 0;
  responder = seen({ fail: "network" });
  await ctx.maybeStartRescueFromConfig(offer("off-2"));
  out.lostBody = acks()[0].body;
  out.claimKept = local.rescueClaim ? local.rescueClaim.id : null;

  // The next tick, whose /config no longer lists the offer, acks it again;
  // the 204 starts the rotation and settles the claim.
  requests.length = 0;
  responder = (path) => (path === "/rescue_ack" ? { status: 204 } : { status: 503 });
  await ctx.maybeStartRescueFromConfig(null);
  out.retryBody = acks()[0].body;
  out.claimAfter204 = local.rescueClaim === undefined;
  out.sourceIds = local.rescueSession ? local.rescueSession.sourceIds : null;
  out.opened = Object.values(local.trackedTabs || {}).map((t) => t.originalStreamer);

  // An expired claim is dropped without another ack.
  requests.length = 0;
  local.rescueClaim = { id: "off-3", offer: offer("off-3"), at: Date.now() - 11 * 60 * 1000 };
  await ctx.maybeStartRescueFromConfig(null);
  out.expiredAcks = acks().length;
  out.expiredGone = local.rescueClaim === undefined;

  // Last, since it leaves a claim behind: while off-4's ack is out, the
  // stored claim becomes another (well formed) one, off-5. The 409 for
  // off-4 removes only a claim for off-4.
  requests.length = 0;
  responder = (path) => {
    if (path !== "/rescue_ack") return { status: 503 };
    local.rescueClaim = { id: "off-5", offer: offer("off-5"), at: Date.now() };
    return { status: 409 };
  };
  await ctx.maybeStartRescueFromConfig(offer("off-4"));
  out.otherAckBody = acks()[0].body;
  out.otherClaim = local.rescueClaim ? local.rescueClaim.id : null;
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_c06_as07_the_rescue_ack_names_the_claimant_and_survives_a_lost_answer(kind, tmp_path):
    """AUDIT S7 and plan 3.6: the ack body carries id, browser and instance;
    the claim is written before the POST, removed on a 409, kept when the
    answer is lost, acked again on the next tick even with no offer in
    /config, completed by a 204 (the rotation starts), and dropped once
    RESCUE_ACK_HARD_TIMEOUT_MS has passed. A 409 removes only the claim it
    acted on."""
    got = _run(kind, RESCUE_CASE, tmp_path)
    claimant = {"browser": kind, "instance": "a1b2c3d4"}
    # The claim was in storage when each first POST went out, the 409 pass
    # included (it keeps no claim afterwards).
    assert got["atAck"] == ["off-1", "off-2"]
    assert got["otherAckBody"] == {"id": "off-4", **claimant}
    assert got["otherClaim"] == "off-5"
    assert got["refusedBody"] == {"id": "off-1", **claimant}
    assert got["claimAfter409"] is True and got["sessionAfter409"] is True
    assert got["lostBody"] == {"id": "off-2", **claimant}
    assert got["claimKept"] == "off-2"
    assert got["retryBody"] == {"id": "off-2", **claimant}
    assert got["claimAfter204"] is True
    assert got["sourceIds"] == ["off-2"]
    assert got["opened"] == ["bob"]
    assert got["expiredAcks"] == 0 and got["expiredGone"] is True


REPORT_CASE = r"""async (ctx) => {
  const now = Math.floor(Date.now() / 1000);
  local.trackedTabs = {
    "11": { originalStreamer: "alice", raidHopCount: 0, openedAt: Date.now(), slot: "keep-1" },
    "12": { originalStreamer: "carol", raidHopCount: 0, openedAt: Date.now() },
  };
  local.slotState = { v: 1, appliedSeq: 7, pendingOpens: {}, gone: [{ streamer: "bob", reason: "user_closed", at: now - 5 }],
    deferredLogged: {}, windowClosed: [], recentGone: {} };
  local.extensionPaused = true;
  local.slotPlan = { v: 1, seq: 7, generated_at: now, active: true, assigning: true,
    executor: `${BG_KIND}-a1b2c3d4`, slots: [], close: [], queue: [] };
  responder = (path) => (path === "/open_tabs" ? { status: 204 } : { status: 503 });
  await ctx.reportOpenTabs("refresh");
  await sleep(50);
  const posts = requests.filter((r) => r.path === "/open_tabs");
  const paused = posts[0].body;
  const goneAfter = local.slotState.gone.length;

  requests.length = 0;
  local.extensionPaused = false;
  await ctx.reportOpenTabs("tabs-changed");
  const running = requests.filter((r) => r.path === "/open_tabs")[0].body;
  return { paused, goneAfter, running, ctype: posts[0].headers["Content-Type"] };
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_c04_the_open_tabs_report_carries_instance_plan_seq_gone_and_busy(kind, tmp_path):
    """Plan 3.4: browser, instance, streamers, reason, plan_seq, gone and busy
    ("paused" while Pause extension holds a plan that runs here); the sent
    gone entries go after a 2xx."""
    got = _run(kind, REPORT_CASE, tmp_path)
    paused = got["paused"]
    assert paused["browser"] == kind and paused["instance"] == "a1b2c3d4"
    assert sorted(paused["streamers"]) == ["alice", "carol"]
    assert paused["reason"] == "refresh" and paused["plan_seq"] == 7
    assert [g["streamer"] for g in paused["gone"]] == ["bob"] and paused["gone"][0]["reason"] == "user_closed"
    assert paused["busy"] == "paused"
    assert got["goneAfter"] == 0
    assert got["running"]["busy"] is None and got["running"]["gone"] == []
    assert got["ctype"] == "application/json"


MERGE_CASE = r"""async (ctx) => {
  const H = 3600 * 1000;
  const at = (ms) => new Date(ms).toISOString();
  const t0 = Date.parse("2026-09-29T11:40:00.000Z");
  const card = (over) => ctx.atRiskEntryFromEvent({ status: "broke", streamer: "alice", count: 5,
    detected_at: at(t0), card_age_s: 3600, card_age_unit_s: 3600, ...over }, t0 + 10 * H);
  const out = {};
  // Posted 10:30: "1 hour ago" at 11:40 and at 12:20 is one card.
  const first = { ...ctx.mergeAtRiskEntry(null, card({})), acknowledged_at: at(t0), requested_at: 1, requested_mode: "slot" };
  const later = ctx.mergeAtRiskEntry(first, card({ detected_at: at(t0 + 40 * 60 * 1000) }));
  out.sameKeepsRow = later.acknowledged_at === at(t0) && later.deadline_at === first.deadline_at &&
    later.requested_mode === "slot";
  // Across the unit rollover ("2 hours ago" at 12:31): still the same card.
  const rolled = ctx.mergeAtRiskEntry(first, card({ detected_at: at(t0 + 51 * 60 * 1000), card_age_s: 7200 }));
  out.rolloverKeepsRow = rolled.acknowledged_at === at(t0) && rolled.break_at === first.break_at;
  // Unknown ages: the same card whatever the times.
  const unknown = ctx.mergeAtRiskEntry({ ...ctx.mergeAtRiskEntry(null, card({ card_age_s: null, card_age_unit_s: null })),
    acknowledged_at: at(t0) }, card({ card_age_s: null, card_age_unit_s: null, detected_at: at(t0 + 2 * H) }));
  out.unknownKeepsRow = unknown.acknowledged_at === at(t0) && unknown.detected_at === at(t0);
  // An in-danger card of the same count re-issued five hours later is new.
  const danger = (over) => ctx.atRiskEntryFromEvent({ status: "in_danger", streamer: "alice", count: 5,
    detected_at: at(t0), card_age_s: 60, card_age_unit_s: 60, deadline_hours: 10, ...over }, t0 + 10 * H);
  const d1 = { ...ctx.mergeAtRiskEntry(null, danger({})), acknowledged_at: at(t0) };
  const d2 = ctx.mergeAtRiskEntry(d1, danger({ detected_at: at(t0 + 5 * H), deadline_hours: 5 }));
  out.reissuedIsNew = d2.acknowledged_at === null && d2.detected_at === at(t0 + 5 * H);
  // The same card read 30 minutes later ("31 minutes ago"): an escalated
  // deadline re-arms it; an hour less is label drift and changes nothing.
  const again = (hours) => danger({ detected_at: at(t0 + 30 * 60 * 1000), card_age_s: 1860, deadline_hours: hours });
  const d3 = ctx.mergeAtRiskEntry(d1, again(7));
  out.escalationRearms = d3.acknowledged_at === null && Date.parse(d3.deadline_at) < Date.parse(d1.deadline_at) &&
    d3.detected_at === d1.detected_at;
  const d4 = ctx.mergeAtRiskEntry(d1, again(9));
  out.driftKeeps = d4.acknowledged_at === at(t0) && d4.deadline_at === d1.deadline_at;
  // An older in-danger card never overwrites a newer broke row.
  const broke = ctx.mergeAtRiskEntry(null, card({ detected_at: at(t0 + 2 * H), card_age_s: 60, card_age_unit_s: 60 }));
  const older = ctx.mergeAtRiskEntry(broke, danger({ detected_at: at(t0 + 2 * H), card_age_s: 7200, card_age_unit_s: 3600 }));
  out.olderLoses = older === broke || (older.status === "broke" && older.card_key === broke.card_key);
  // A link row has no count and a synthesized save_url.
  const link = ctx.atRiskEntryFromEvent({ status: "broke", streamer: "Zed", count: null, source: "link",
    detected_at: at(t0), save_url: "https://example.com/x" }, t0);
  out.link = { count: link.count, key: link.card_key, url: link.save_url, unit: link.age_unit_s,
    deadlineH: (Date.parse(link.deadline_at) - Date.parse(link.break_at)) / H };
  // break_at is the earliest posting time: detected - age - unit.
  out.breakAt = (t0 - Date.parse(first.break_at)) / H;
  // An event's own deadline_at counts only inside [now - 1 h, now + 8 days].
  const own = ctx.atRiskEntryFromEvent({ status: "broke", streamer: "alice", count: 5, detected_at: at(t0),
    deadline_at: at(t0 + 3 * H) }, t0);
  const far = ctx.atRiskEntryFromEvent({ status: "broke", streamer: "alice", count: 5, detected_at: at(t0),
    deadline_at: at(t0 + 9 * 24 * H) }, t0);
  out.ownDeadline = own.deadline_at === at(t0 + 3 * H);
  out.farDeadlineIgnored = far.deadline_at === at(t0 + 24 * H);
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_ap06_c10_the_at_risk_merge_follows_card_identity(kind, tmp_path):
    """Plan 3.10 and 3.5.3 in mergeAtRiskEntry: a card re-read across a label
    rollover, or with an unknown age, keeps its acknowledged row and its
    deadline; a reissued in-danger card and an escalated deadline re-arm it;
    label drift does not; an older in-danger card never replaces a newer
    broke row. Rows store break_at, deadline_at, age_unit_s and card_key; a
    link row keeps a null count and a save_url built from the login."""
    got = _run(kind, MERGE_CASE, tmp_path)
    for key in ("sameKeepsRow", "rolloverKeepsRow", "unknownKeepsRow", "reissuedIsNew",
                "escalationRearms", "driftKeeps", "olderLoses", "ownDeadline", "farDeadlineIgnored"):
        assert got[key] is True, key
    assert got["link"] == {
        "count": None, "key": "broke:zed:null", "url": "https://www.twitch.tv/save-streak/zed",
        "unit": 0, "deadlineH": 24,
    }
    assert got["breakAt"] == 2


WAKE_CASE = r"""async (ctx) => {
  await sleep(50);
  return { rows: Object.keys(local.atRiskStreaks || {}).sort(), badges: badgeTexts };
}"""


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(login: str, deadline_ms: int, acknowledged_ms=None) -> dict:
    hour = 3600 * 1000
    return {
        "streamer": login, "status": "broke", "count": 3, "detected_at": _iso(deadline_ms - 24 * hour),
        "deadline_hours": 24, "break_at": _iso(deadline_ms - 24 * hour), "deadline_at": _iso(deadline_ms),
        "age_unit_s": 0, "card_key": f"broke:{login}:3", "save_url": f"https://www.twitch.tv/save-streak/{login}",
        "acknowledged_at": _iso(acknowledged_ms) if acknowledged_ms else None,
    }


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_as04_the_module_wake_call_prunes_then_refreshes_the_badge(kind, tmp_path):
    """AUDIT S4.3 and mv3-extension-discipline pattern 3: the call at module
    top level (a worker woken for a message fires neither onStartup nor
    onInstalled, and the stub namespace fires no event at all) prunes the
    expired rows, then sets the badge from the rows left: unacknowledged
    and not expired. A row 3 hours past its deadline is inside the 4-hour
    buffer; one 5 hours past it is not."""
    hour = 3600 * 1000
    now_ms = int(time.time() * 1000)
    seed = {"atRiskStreaks": {
        "one": _row("one", now_ms + 5 * hour),
        "two": _row("two", now_ms - 3 * hour),
        "gone": _row("gone", now_ms - 5 * hour),
        "seen": _row("seen", now_ms + 5 * hour, acknowledged_ms=now_ms),
    }}
    got = _run(kind, WAKE_CASE, tmp_path, seed=seed)
    assert got["rows"] == ["one", "seen", "two"]
    assert got["badges"] and got["badges"][-1] == "2"


# Shared by the stream window cases: a normal window, a Twitch tab and a
# trackedTabs entry.
STREAM_WINDOW_HELPERS = r"""
  const win = (id, left) => ({ id, type: "normal", incognito: false, focused: false, state: "normal",
    left, top: 0, width: 1200, height: 900 });
  const tab = (id, windowId, login) => ({ id, windowId, url: `https://www.twitch.tv/${login}`, active: false,
    incognito: false });
  const entry = (login) => ({ originalStreamer: login, raidHopCount: 0, openedAt: Date.now() });
  const moves = () => world.calls.filter((c) => c[0] === "tabs.move").map((c) => [c[1], c[2]]);
  const lines = (re) => logs.filter((l) => re.test(l)).length;
"""

CLEAR_THEN_SET_CASE = r"""async (ctx) => {""" + STREAM_WINDOW_HELPERS + r"""
  world.windows[5] = win(5, 0);       // S, the first stream window
  world.windows[7] = win(7, 1300);    // W, designated later
  world.windows[8] = win(8, 2600);    // X
  world.tabs[10] = tab(10, 7, "alice");
  world.tabs[11] = tab(11, 7, "bob");
  local.trackedTabs = { "10": entry("alice"), "11": entry("bob") };
  const out = {};
  out.setS = await ctx.setStreamWindow(5);
  out.clear = await ctx.clearStreamWindow();
  // No stream window now: the owner drags bob on to X, and dan opens in X.
  world.tabs[11].windowId = 8;
  world.tabs[14] = tab(14, 8, "dan");
  local.trackedTabs["14"] = entry("dan");
  world.calls.length = 0;
  out.setW = await ctx.setStreamWindow(7);
  out.movesSetW = moves();
  await ctx.reconcileStreamWindowPlacement();
  out.movesAfter = moves().length;
  out.where = { alice: world.tabs[10].windowId, bob: world.tabs[11].windowId, dan: world.tabs[14].windowId };
  out.placement = clone(local.tabPlacement);
  out.dragLines = lines(/tab 11 \(bob\) was moved out by hand/);
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_f07_designating_after_stop_using_gathers_the_tabs_left_in_the_old_window(kind, tmp_path):
    """DESIGN 10.4 step 4 and 10.12: after "Stop using a stream window", a
    new designation gathers the stream tabs still in the window they were
    placed in, and the tabs opened while none was set. A tab the owner
    dragged out meanwhile stays where it is, and the next reconciliation
    marks it ownerPlaced."""
    got = _run(kind, CLEAR_THEN_SET_CASE, tmp_path)
    assert got["setS"] == {"ok": True, "moved": 2}
    assert got["clear"] == {"ok": True}
    assert got["setW"] == {"ok": True, "moved": 2}
    assert got["movesSetW"] == [[10, 7], [14, 7]]
    assert got["movesAfter"] == 2
    assert got["where"] == {"alice": 7, "bob": 8, "dan": 7}
    assert got["placement"]["10"]["placedIn"] == 7 and got["placement"]["14"]["placedIn"] == 7
    assert got["placement"]["11"] == {"placedIn": 5, "seenIn": 5, "ownerPlaced": True, "failures": 0}
    assert got["dragLines"] == 1


SET_RACE_CASE = r"""async (ctx) => {""" + STREAM_WINDOW_HELPERS + r"""
  world.windows[5] = win(5, 0);
  world.windows[7] = win(7, 1300);
  world.windows[9] = win(9, 2600);
  world.tabs[10] = tab(10, 5, "alice");
  local.trackedTabs = { "10": entry("alice") };
  local.streamWindow = { id: 5, state: "normal", left: 0, top: 0, width: 1200, height: 900,
    normal: { left: 0, top: 0, width: 1200, height: 900 }, setAt: Date.now(), checkedAt: Date.now() };
  local.tabPlacement = { "10": { placedIn: 5, seenIn: 5, ownerPlaced: false, failures: 0 } };
  // Slow placement writes; the reconciliation starts right after the
  // designation's own move of tab 10 lands, before its record is written.
  delays.tabPlacement = 30;
  let race = null;
  world.onMove = (id) => {
    if (id === 10 && !race) race = ctx.reconcileStreamWindowPlacement();
  };
  const out = {};
  out.set = await ctx.setStreamWindow(7);
  out.raced = !!race;
  await race;
  world.onMove = null;
  out.afterRace = clone(local.tabPlacement["10"]);
  out.raceLines = lines(/was moved out by hand/);
  // A real drag afterwards is still the owner's.
  world.tabs[10].windowId = 9;
  await ctx.reconcileStreamWindowPlacement();
  out.afterDrag = clone(local.tabPlacement["10"]);
  out.dragLines = lines(/tab 10 \(alice\) was moved out by hand/);
  out.where = world.tabs[10].windowId;
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_f07_the_reconciliation_never_takes_the_extensions_own_move_for_the_owners(kind, tmp_path):
    """DESIGN 10.11: a reconciliation that runs while "Use this window for
    streams" moves a tab (its placement record not written yet) does not
    mark that tab ownerPlaced; a drag after it still is."""
    got = _run(kind, SET_RACE_CASE, tmp_path)
    assert got["set"] == {"ok": True, "moved": 1}
    assert got["raced"] is True
    assert got["afterRace"] == {"placedIn": 7, "seenIn": 7, "ownerPlaced": False, "failures": 0}
    assert got["raceLines"] == 0
    assert got["afterDrag"]["ownerPlaced"] is True and got["dragLines"] == 1
    assert got["where"] == 9


def _update_world(extra_tab: bool) -> dict:
    """The stream window 5 on the second monitor holds alice's stream (tab
    10), and with extra_tab a tab of the owner's; the owner dragged bob's
    stream (tab 11) into the working window 7 and watches it there."""
    def twitch(tab_id, window_id, login, active=False):
        return {"id": tab_id, "windowId": window_id, "url": f"https://www.twitch.tv/{login}", "active": active,
                "incognito": False}

    def window(window_id, left):
        return {"id": window_id, "type": "normal", "incognito": False, "focused": False, "state": "normal",
                "left": left, "top": 0, "width": 1280, "height": 1000}

    tabs = {
        "10": twitch(10, 5, "alice"),
        "11": twitch(11, 7, "bob", active=True),
        "12": {"id": 12, "windowId": 7, "url": "https://example.com/work", "active": False, "incognito": False},
    }
    if extra_tab:
        tabs["13"] = {"id": 13, "windowId": 5, "url": "https://example.com/notes", "active": True, "incognito": False}
    return {"windows": {"5": window(5, 1920), "7": window(7, 0)}, "tabs": tabs}


def _update_seed() -> dict:
    now_ms = int(time.time() * 1000)
    bounds = {"left": 1920, "top": 0, "width": 1280, "height": 1000}
    return {
        "streamWindow": {"id": 5, "state": "normal", **bounds, "normal": dict(bounds), "setAt": now_ms,
                         "checkedAt": now_ms},
        "trackedTabs": {
            "10": {"originalStreamer": "alice", "raidHopCount": 0, "openedAt": now_ms - 60000},
            "11": {"originalStreamer": "bob", "raidHopCount": 0, "openedAt": now_ms - 60000},
        },
        "tabPlacement": {
            "10": {"placedIn": 5, "seenIn": 5, "ownerPlaced": False, "failures": 0},
            "11": {"placedIn": 5, "seenIn": 7, "ownerPlaced": True, "failures": 0},
        },
    }


UPDATE_CASE = r"""async (ctx) => {
  await sleep(50);
  return {
    calls: world.calls,
    windows: Object.keys(world.windows).map(Number).sort((a, b) => a - b),
    where: { alice: world.tabs[10].windowId, bob: world.tabs[11].windowId },
    streamWindowId: local.streamWindow ? local.streamWindow.id : null,
    placement: local.tabPlacement || {},
    cleared: logs.some((l) => /the browser started again, so its old window id is not trusted/.test(l)),
    adopted: logs.some((l) => /Stream window: adopted the restored window/.test(l)),
    recreated: logs.some((l) => /Stream window: recreated it/.test(l)),
  };
}"""


@pytest.mark.parametrize("extra_tab", [True, False], ids=["with_owner_tab", "stream_tab_only"])
@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_f07_r27_an_update_adopts_the_stream_window_in_place_and_keeps_owner_drags(kind, extra_tab, tmp_path):
    """DESIGN 10.7 step 3 and 10.12 "Browser restart": a start without the
    session marker (a browser restart, or an extension update with the same
    tab and window ids) clears the stored window id; the init scan re-places
    alice's kept tab and adopts the window it sits in, since that window is
    at the remembered bounds and holds it, instead of recreating a second
    window there. bob's tab, dragged out by the owner, keeps its ownerPlaced
    record and is not moved back."""
    got = _run(kind, UPDATE_CASE, tmp_path, seed=_update_seed(), world=_update_world(extra_tab))
    assert got["cleared"] is True
    assert got["adopted"] is True and got["recreated"] is False
    assert not [c for c in got["calls"] if c[0] in ("windows.create", "tabs.move")], got["calls"]
    assert got["windows"] == [5, 7]
    assert got["where"] == {"alice": 5, "bob": 7}
    assert got["streamWindowId"] == 5
    assert got["placement"]["10"]["placedIn"] == 5 and got["placement"]["10"]["ownerPlaced"] is False
    assert got["placement"]["11"]["ownerPlaced"] is True


DRAG_THEN_SET_CASE = r"""async (ctx) => {""" + STREAM_WINDOW_HELPERS + r"""
  world.windows[5] = win(5, 0);       // the first stream window
  world.windows[7] = win(7, 1300);    // designated next
  world.windows[8] = win(8, 2600);    // the owner's working window
  world.tabs[10] = tab(10, 5, "alice");
  world.tabs[11] = tab(11, 5, "bob");
  local.trackedTabs = { "10": entry("alice"), "11": entry("bob") };
  const out = {};
  out.setS = await ctx.setStreamWindow(5);

  // 1. The owner drags bob into window 8 and, before the next
  // reconciliation, presses "Use this window for streams" in window 7.
  world.tabs[11].windowId = 8;
  world.calls.length = 0;
  out.setW = await ctx.setStreamWindow(7);
  out.movesSetW = moves();
  await ctx.reconcileStreamWindowPlacement();
  out.movesAfterSetW = moves();
  out.bob = { where: world.tabs[11].windowId, rec: clone(local.tabPlacement["11"]) };
  out.bobLines = lines(/tab 11 \(bob\) was moved out by hand/);

  // 2. The owner drags alice into window 8 too, then presses the button
  // again in the stream window itself.
  world.tabs[10].windowId = 8;
  world.calls.length = 0;
  out.again = await ctx.setStreamWindow(7);
  out.movesAgain = moves();
  await ctx.reconcileStreamWindowPlacement();
  out.movesAfterAgain = moves();
  out.alice = { where: world.tabs[10].windowId, rec: clone(local.tabPlacement["10"]) };
  out.aliceLines = lines(/tab 10 \(alice\) was moved out by hand/);

  // 3. Two tabs whose move failed while they sat in window 5: the owner
  // has dragged carol into window 8 since; erin is still where she was.
  world.tabs[12] = tab(12, 8, "carol");
  world.tabs[13] = tab(13, 5, "erin");
  local.trackedTabs["12"] = entry("carol");
  local.trackedTabs["13"] = entry("erin");
  local.tabPlacement["12"] = { placedIn: null, seenIn: 5, ownerPlaced: false, failures: 1 };
  local.tabPlacement["13"] = { placedIn: null, seenIn: 5, ownerPlaced: false, failures: 1 };
  world.calls.length = 0;
  out.failed = await ctx.setStreamWindow(7);
  out.movesFailed = moves();
  await ctx.reconcileStreamWindowPlacement();
  out.movesAfterFailed = moves();
  out.carol = { where: world.tabs[12].windowId, rec: clone(local.tabPlacement["12"]) };
  out.erin = { where: world.tabs[13].windowId, rec: clone(local.tabPlacement["13"]) };
  out.carolLines = lines(/tab 12 \(carol\) was moved out by hand/);
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_f07_a_designation_leaves_a_tab_the_owner_just_dragged_out(kind, tmp_path):
    """DESIGN 10.4 and 10.12: "Use this window for streams" gathers a placed
    tab only while it still sits in the window it was placed in. A tab the
    owner dragged out a moment before (no reconciliation has marked it
    ownerPlaced yet) stays where the owner put it, whether the button is
    pressed in another window or again in the stream window, and a tab
    whose move failed and which the owner then dragged on stays too; the
    next reconciliation marks each one ownerPlaced. A tab whose move failed
    and that is still where it was is gathered."""
    got = _run(kind, DRAG_THEN_SET_CASE, tmp_path)
    assert got["setS"] == {"ok": True, "moved": 0}

    assert got["setW"] == {"ok": True, "moved": 1}
    assert got["movesSetW"] == [[10, 7]] and got["movesAfterSetW"] == [[10, 7]]
    assert got["bob"] == {"where": 8, "rec": {"placedIn": 5, "seenIn": 5, "ownerPlaced": True, "failures": 0}}
    assert got["bobLines"] == 1

    assert got["again"] == {"ok": True, "moved": 0}
    assert got["movesAgain"] == [] and got["movesAfterAgain"] == []
    assert got["alice"] == {"where": 8, "rec": {"placedIn": 7, "seenIn": 7, "ownerPlaced": True, "failures": 0}}
    assert got["aliceLines"] == 1

    assert got["failed"] == {"ok": True, "moved": 1}
    assert got["movesFailed"] == [[13, 7]] and got["movesAfterFailed"] == [[13, 7]]
    assert got["carol"] == {"where": 8, "rec": {"placedIn": None, "seenIn": 5, "ownerPlaced": True, "failures": 1}}
    assert got["erin"] == {"where": 7, "rec": {"placedIn": 7, "seenIn": 7, "ownerPlaced": False, "failures": 0}}
    assert got["carolLines"] == 1


# FOCUSED (true or false) is substituted: whether the stream window, the
# last focused window, has focus.
FOCUS_CASE = r"""async (ctx) => {""" + STREAM_WINDOW_HELPERS + r"""
  const now = Date.now();
  // The stream window is the owner's only window: a docs tab in front, dan's
  // stream behind it.
  world.windows[5] = win(5, 0);
  world.tabs[20] = { id: 20, windowId: 5, url: "https://example.com/docs", active: true, incognito: false,
    lastAccessed: now - 1000 };
  world.tabs[21] = { ...tab(21, 5, "dan"), lastAccessed: now - 5000 };
  local.trackedTabs = { "21": entry("dan") };
  local.streamWindow = { id: 5, state: "normal", left: 0, top: 0, width: 1200, height: 900,
    normal: { left: 0, top: 0, width: 1200, height: 900 }, setAt: now, checkedAt: now };
  world.lastFocused = { id: 5, type: "normal", incognito: false, focused: FOCUSED };
  const out = {};
  out.target = await ctx.targetWindowForOpen("https://www.twitch.tv/dan?sm=1");
  // The desktop opened dan's stream, and the browser made it the active tab.
  world.tabs[20].active = false;
  world.tabs[21].active = true;
  world.tabs[21].lastAccessed = now;
  world.calls.length = 0;
  out.focusedTab = await ctx.focusPlacedTab({ id: 21, windowId: 5 }, 5);
  out.updates = world.calls.filter((c) => c[0] === "tabs.update").map((c) => [c[1], c[2]]);
  out.windowUpdates = world.calls.filter((c) => c[0] === "windows.update").length;
  out.inUse = lines(/Stream window: in use/);
  return out;
}"""


@pytest.mark.parametrize("focused", [False, True], ids=["unfocused", "focused"])
@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_r36_f07_the_stream_window_is_in_use_only_while_it_has_focus(kind, focused, tmp_path):
    """DESIGN 10.9 and rule 36: the owner works in the stream window only
    while it is the focused window. Last focused but without focus (the
    owner switched to a game, and no browser window has focus), a new tab
    there is opened active and a stream tab the browser made active stays
    in front, with no in-use line. With focus, the new tab opens in the
    background and the owner's tab is put back in front. The window's own
    focus is never touched."""
    case = FOCUS_CASE.replace("FOCUSED", "true" if focused else "false")
    got = _run(kind, case, tmp_path)
    assert got["target"] == {"windowId": 5, "active": not focused, "createdTab": None, "inStreamWindow": True}
    assert got["windowUpdates"] == 0
    if focused:
        assert got["focusedTab"] is False
        assert got["updates"] == [[20, {"active": True}]]
        assert got["inUse"] == 2
    else:
        assert got["focusedTab"] is True
        assert got["updates"] == [[21, {"active": True}]]
        assert got["inUse"] == 0


MANUAL_CLOSE_CASE = r"""async (ctx) => {
  const MIN = 60 * 1000;
  const now = Date.now();
  const at = (ms) => new Date(ms).toISOString();
  const row = () => ({ streamer: "dan", status: "broke", count: 3, detected_at: at(now - MIN), deadline_hours: 24,
    break_at: at(now - MIN), deadline_at: at(now + 20 * 60 * MIN), age_unit_s: 0, card_key: "broke:dan:3",
    save_url: "https://www.twitch.tv/save-streak/dan", acknowledged_at: null,
    requested_at: now - MIN, requested_mode: "tab" });
  const out = {};
  let seq = 1;
  // A manual save tab (a Streaks at Risk click in normal mode) that a plan
  // then closes as unplanned: 15 minutes old (past the grace period, short
  // of MANUAL_SAVE_VISIT_MS) and 40 minutes old.
  for (const [name, age] of [["early", 15 * MIN], ["visited", 40 * MIN]]) {
    world.tabs[30] = { id: 30, windowId: 1, url: "https://www.twitch.tv/save-streak/dan", active: false,
      incognito: false };
    local.trackedTabs = { "30": { originalStreamer: "dan", raidHopCount: 0, openedAt: now - age,
      saveStreak: true, manualSave: true } };
    local.atRiskStreaks = { dan: row() };
    local.slotPlan = { v: 1, seq: seq++, generated_at: Math.floor(Date.now() / 1000), active: true,
      assigning: false, executor: `${BG_KIND}-a1b2c3d4`, slots: [],
      close: [{ streamer: "dan", reason: "unplanned" }], queue: [] };
    world.calls.length = 0;
    await ctx.applySlotPlan(local.slotPlan);
    // The browser's onRemoved for the closed tab.
    await ctx.onTabRemoved(30, { windowId: 1, isWindowClosing: false });
    const r = local.atRiskStreaks.dan;
    out[name] = {
      removed: world.calls.some((c) => c[0] === "tabs.remove" && c[1] === 30),
      tracked: !!(local.trackedTabs && local.trackedTabs["30"]),
      requestedMode: r.requested_mode === undefined ? null : r.requested_mode,
      requestedAt: r.requested_at === undefined ? null : r.requested_at,
      acknowledged: !!r.acknowledged_at,
    };
  }
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_c10_am15_as05_a_plan_close_of_a_manual_save_tab_settles_its_request(kind, tmp_path):
    """Plan 3.10, A15 and AUDIT S5: a manual save tab the plan closes
    (closeTrackedTab untracks it before the remove, so onTabRemoved finds no
    entry) still settles the Streaks at Risk request it served: closed before
    MANUAL_SAVE_VISIT_MS the request is cleared and the row is not
    acknowledged; closed after it the row is acknowledged."""
    got = _run(kind, MANUAL_CLOSE_CASE, tmp_path)
    early = got["early"]
    assert early["removed"] is True and early["tracked"] is False
    assert early["requestedMode"] is None and early["requestedAt"] is None
    assert early["acknowledged"] is False
    visited = got["visited"]
    assert visited["removed"] is True and visited["tracked"] is False
    assert visited["acknowledged"] is True


ALREADY_SAVED_CASE = r"""async (ctx) => {
  const H = 3600 * 1000;
  const now = Date.now();
  const at = (ms) => new Date(ms).toISOString();
  const row = (login, count, detectedMs, acknowledgedAt = null) => ({ streamer: login, status: "broke", count,
    detected_at: at(detectedMs), deadline_hours: 24, break_at: at(detectedMs), deadline_at: at(detectedMs + 24 * H),
    age_unit_s: 0, card_key: `broke:${login}:${count}`, save_url: `https://www.twitch.tv/save-streak/${login}`,
    acknowledged_at: acknowledgedAt });
  const earlier = at(now - 30 * 60 * 1000);
  local.atRiskStreaks = {
    bob: row("bob", 12, now - H),
    carl: row("carl", 13, now - H),
    dan: row("dan", 4, now - 2 * H),
    eve: row("eve", 6, now - H, earlier),
  };
  responder = (path) => (path === "/streak_event"
    ? { status: 200, text: JSON.stringify({ verdict: "saved", item: true }) }
    : path === "/config" ? { status: 200, text: JSON.stringify({ streamers: [] }) } : { status: 204 });
  const saveAt = at(now);
  for (const [login, count] of [["bob", 12], ["carl", 12], ["eve", 6]]) {
    await ctx.handleStreakAlreadySaved({ status: "already_saved", streamer: login, count, detected_at: saveAt,
      page_url: "" }, null);
  }
  // The plan fetch each report starts, done before the rows are read.
  await ctx.refreshPlanSoon();
  const view = (r) => (r ? { count: r.count, ack: r.acknowledged_at } : null);
  const out = { afterReport: {}, afterMerge: {} };
  for (const login of ["bob", "carl", "dan", "eve"]) out.afterReport[login] = view(local.atRiskStreaks[login]);
  out.posts = requests.filter((r) => r.path === "/streak_event").map((r) => r.body.streamer);
  out.configFetched = requests.some((r) => r.path === "/config");
  // The desktop's saved-streak copy: bob's save (this report) and a save
  // for dan made after dan's card was seen.
  await ctx.mergeSavedStreaksFromDesktop({ bob: saveAt, dan: at(now - H) }, Date.now());
  for (const login of ["bob", "carl", "dan", "eve"]) out.afterMerge[login] = view(local.atRiskStreaks[login]);
  out.earlier = earlier;
  out.cleared = logs.filter((l) => /Cleared at-risk row\(s\) for /.test(l));
  out.badge = badgeTexts.length ? badgeTexts[badgeTexts.length - 1] : null;
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_as04_c10_already_saved_acknowledges_the_row_and_the_config_copy_keeps_it(kind, tmp_path):
    """Plan 3.10 (request lifetime), WP4b task 4 and AUDIT S4.5: a save-streak
    page that says the streak is already kept acknowledges the streamer's
    Streaks at Risk row instead of removing it, unless the row's card shows a
    higher count than the page (a newer break), and an acknowledged row keeps
    its timestamp. The desktop's saved_streaks copy then leaves the
    acknowledged row alone and still removes an unacknowledged row the save
    settles (the case of a profile that does not run the plan)."""
    got = _run(kind, ALREADY_SAVED_CASE, tmp_path)
    assert got["posts"] == ["bob", "carl", "eve"]
    assert got["configFetched"] is True
    after = got["afterReport"]
    assert after["bob"] is not None, "already_saved removed bob's row"
    assert after["bob"]["count"] == 12 and isinstance(after["bob"]["ack"], str), after["bob"]
    assert after["carl"] == {"count": 13, "ack": None}, "carl's card outgrows the save, so it stays at risk"
    assert after["dan"] == {"count": 4, "ack": None}
    assert after["eve"] == {"count": 6, "ack": got["earlier"]}, "an acknowledged row lost its timestamp"
    merged = got["afterMerge"]
    assert merged["bob"] == after["bob"], "the saved-streak copy removed or changed bob's acknowledged row"
    assert merged["carl"] == {"count": 13, "ack": None}
    assert merged["dan"] is None, "an unacknowledged row the save settles was kept"
    assert merged["eve"] == after["eve"]
    assert len(got["cleared"]) == 1 and got["cleared"][0].endswith("Cleared at-risk row(s) for dan: streak already saved")
    assert got["badge"] == "1"


# A46 (the live check of 2026-10-01): Twitch moves a save-streak page in place
# to a VOD, the streamer's clip or videos, or the channel. The recorded clip
# slug and VOD id.
CLIP_SLUG = "AntediluvianPoisedRhinocerosHumbleLife-az1h_w4_FqDUU7VC"
VOD_ID = "2888044378"

# Shared by the save-visit cases: a fresh plan this profile executes, a
# trackedTabs entry, a tab in world.tabs, and a Streaks at Risk row.
SAVE_VISIT_HELPERS = r"""
  const plan = (seq, slots, close = []) => ({ v: 1, seq, generated_at: Math.floor(Date.now() / 1000),
    active: true, assigning: false, executor: `${BG_KIND}-a1b2c3d4`, slots, close, queue: [] });
  const entry = (login, extra = {}) => ({ originalStreamer: login, raidHopCount: 0, openedAt: Date.now(), ...extra });
  const putTab = (id, url) => {
    world.tabs[id] = { id, windowId: 1, url, active: false, incognito: false };
    return clone(world.tabs[id]);
  };
  const row = (login) => ({ streamer: login, status: "broke", count: 3, detected_at: new Date().toISOString(),
    deadline_hours: 24, break_at: new Date().toISOString(), deadline_at: new Date(Date.now() + 20 * 3600 * 1000).toISOString(),
    age_unit_s: 0, card_key: `broke:${login}:3`, save_url: `https://www.twitch.tv/save-streak/${login}`, acknowledged_at: null });
  const goneList = () => ((local.slotState && local.slotState.gone) || []).map((g) => `${g.streamer}:${g.reason}`);
  const removedIds = () => world.calls.filter((c) => c[0] === "tabs.remove").map((c) => c[1]);
  const view = () => Object.fromEntries(Object.entries(local.trackedTabs || {}).map(([k, e]) => [k, {
    streamer: e.originalStreamer, slot: e.slot || null, save: e.saveStreak === true, landing: e.landing || null }]));
  const lines = (re) => logs.filter((l) => re.test(l));
"""

LANDING_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  const api = ctx.chrome || ctx.browser;
  // releaseLowQuality is the only sender of setLowQuality false.
  const released = [];
  api.tabs.sendMessage = async (id, msg) => {
    if (msg && msg.action === "setLowQuality" && msg.enabled === false) released.push(id);
    throw new Error("Could not establish connection. Receiving end does not exist.");
  };
  local.slotPlan = plan(4, [{ id: "keep-1", streamer: "alice", entry: "live" }, { id: "cycle-1", streamer: "dave", entry: "save" }]);
  local.trackedTabs = {
    "40": entry("dave", { saveStreak: true, slot: "cycle-1" }),
    "41": entry("erin", { saveStreak: true }),
    "42": entry("finn", { saveStreak: true }),
    "43": entry("gus", { saveStreak: true }),
    "44": entry("alice", { saveStreak: false, slot: "keep-1" }),
    "45": entry("hal", { saveStreak: true }),
    "46": entry("ivy", { saveStreak: true }),
  };
  const moves = [
    [40, "https://www.twitch.tv/videos/VOD_ID"],
    [41, "https://www.twitch.tv/erin/clip/CLIP_SLUG?range=7d"],
    [42, "https://www.twitch.tv/finn"],
    [43, "https://www.twitch.tv/gus/videos"],
    [44, "https://www.twitch.tv/videos/VOD_ID"],
    [45, "https://www.twitch.tv/directory/category/just-chatting"],
    [46, "https://www.twitch.tv/zed/clip/CLIP_SLUG"],
  ];
  for (const [id, url] of moves) await ctx.onTabUpdated(id, { url }, putTab(id, url));
  const out = { afterMoves: view(), gone: goneList(), released: released.slice(), removed: removedIds() };
  // dave's visit goes on from the VOD to his channel: the VOD path is dropped.
  const url = "https://www.twitch.tv/dave";
  await ctx.onTabUpdated(40, { url }, putTab(40, url));
  out.daveAfter = view()["40"];
  out.lines = lines(/Save visit for /);
  out.paths = lines(/Save visit for /).map((l) => l.replace(/^.*moved to (\S+); still tracked$/, "$1"));
  return out;
}""".replace("VOD_ID", VOD_ID).replace("CLIP_SLUG", CLIP_SLUG)


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_a_save_visit_stays_tracked_where_twitch_moves_it(kind, tmp_path):
    """A46: a tracked save-streak tab (saveStreak) that Twitch moves in place
    to a VOD, the streamer's clip, the streamer's channel or the streamer's
    videos stays tracked as the same visit, with its slot marker: no gone
    entry, no releaseLowQuality, no raid. A VOD path is kept in the entry
    (landing) and dropped when the visit moves on. Every other move keeps
    the 1.12 behavior: a Keep Open tab moving to a VOD and a save tab moving
    to the directory are untracked with a navigated gone entry and their
    quality released, and a save tab on another streamer's clip is a raid."""
    got = _run(kind, LANDING_CASE, tmp_path)
    after = got["afterMoves"]
    assert sorted(after) == ["40", "41", "42", "43"], after
    assert after["40"] == {"streamer": "dave", "slot": "cycle-1", "save": True, "landing": f"/videos/{VOD_ID}"}
    for key, login in (("41", "erin"), ("42", "finn"), ("43", "gus")):
        assert after[key] == {"streamer": login, "slot": None, "save": True, "landing": None}, after[key]
    assert got["gone"] == ["alice:navigated", "hal:navigated", "ivy:raid"]
    assert got["released"] == [44, 45], "a save visit's quality was released, or a navigate-away kept it"
    assert got["removed"] == [46]
    assert got["daveAfter"] == {"streamer": "dave", "slot": "cycle-1", "save": True, "landing": None}
    assert got["paths"] == [f"/videos/{VOD_ID}", f"/erin/clip/{CLIP_SLUG}", "/finn", "/gus/videos", "/dave"]


CLOSE_LANDED_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  local.atRiskStreaks = { dave: row("dave"), erin: row("erin"), lou: row("lou") };
  putTab(50, "https://www.twitch.tv/videos/VOD_ID");
  putTab(51, "https://www.twitch.tv/erin/clip/CLIP_SLUG?range=7d");
  putTab(52, "https://www.twitch.tv/videos/123");
  putTab(53, "https://www.twitch.tv/zed");
  putTab(54, "https://www.twitch.tv/videos/321");
  local.trackedTabs = {
    "50": entry("dave", { saveStreak: true, slot: "cycle-1", landing: "/videos/VOD_ID" }),
    "51": entry("erin", { saveStreak: true, slot: "cycle-2" }),
    "52": entry("kim", { slot: "keep-1" }),
    "53": entry("lou", { saveStreak: true, slot: "keep-2" }),
    "54": entry("mo", { saveStreak: true, slot: "keep-1", landing: "/videos/999" }),
  };
  local.slotPlan = plan(9, [{ id: "keep-1" }, { id: "cycle-1" }, { id: "cycle-2" }],
    ["dave", "erin", "kim", "lou", "mo"].map((streamer) => ({ streamer, reason: "turn_over" })));
  await ctx.applySlotPlan(local.slotPlan);
  const ack = (login) => !!(local.atRiskStreaks[login] && local.atRiskStreaks[login].acknowledged_at);
  return {
    removed: removedIds(), tracked: view(), gone: goneList(),
    ack: { dave: ack("dave"), erin: ack("erin"), lou: ack("lou") },
    closed: lines(/Slot plan: closed \w+ \(turn_over\)$/).map((l) => l.replace(/^.*closed (\w+) .*$/, "$1")),
    lost: lines(/no longer shows \w+; untracked it instead of closing it \(turn_over\)$/)
      .map((l) => l.replace(/^.*no longer shows (\w+);.*$/, "$1")),
  };
}""".replace("VOD_ID", VOD_ID).replace("CLIP_SLUG", CLIP_SLUG)


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_r08_a_turn_over_close_finds_a_save_visit_on_its_vod_or_clip(kind, tmp_path):
    """A46 and DESIGN 8.3 step 0: a save visit on the VOD (its remembered
    landing) or clip Twitch moved it to still shows its streamer, so the
    plan's turn_over close closes the tab and acknowledges the Streaks at
    Risk row (step 4). An entry that is not a save visit on a VOD, a save
    entry whose tab now shows another channel, and a save entry whose tab
    shows a VOD other than its landing (a stale id naming the owner's own
    VOD tab) are lost as before: untracked, never closed, no row settled."""
    got = _run(kind, CLOSE_LANDED_CASE, tmp_path)
    assert got["removed"] == [50, 51]
    assert got["tracked"] == {}
    assert got["gone"] == []
    assert got["ack"] == {"dave": True, "erin": True, "lou": False}
    assert got["closed"] == ["dave", "erin"]
    assert got["lost"] == ["kim", "lou", "mo"]


FLIP_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  const api = ctx.chrome || ctx.browser;
  const released = [];
  api.tabs.sendMessage = async (id, msg) => {
    if (msg && msg.action === "setLowQuality" && msg.enabled === false) released.push(id);
    throw new Error("Could not establish connection. Receiving end does not exist.");
  };
  // A navigation moves the tab, except in a tab being dragged.
  const busy = new Set([96]);
  const navs = [];
  api.tabs.update = async (id, props) => {
    world.calls.push(["tabs.update", id, clone(props)]);
    if (props && props.url) {
      navs.push([id, props.url]);
      if (busy.has(id)) throw new Error("Tabs cannot be edited right now (user may be dragging a tab).");
      if (world.tabs[id]) world.tabs[id].url = props.url;
    }
    return world.tabs[id] ? clone(world.tabs[id]) : undefined;
  };
  putTab(90, "https://www.twitch.tv/videos/VOD_ID");
  putTab(91, "https://www.twitch.tv/erin/clip/CLIP_SLUG?range=7d");
  putTab(92, "https://www.twitch.tv/save-streak/finn");
  putTab(93, "https://www.twitch.tv/save-streak/gus");
  putTab(94, "https://www.twitch.tv/hal");
  putTab(95, "https://www.twitch.tv/videos/555");
  putTab(96, "https://www.twitch.tv/videos/666");
  local.trackedTabs = {
    "90": entry("dave", { saveStreak: true, slot: "cycle-1", landing: "/videos/VOD_ID" }),
    "91": entry("erin", { saveStreak: true, slot: "cycle-2" }),
    "92": entry("finn", { saveStreak: true, slot: "keep-1" }),
    "93": entry("gus", { saveStreak: true, slot: "cycle-3" }),
    "94": entry("hal", { saveStreak: false, slot: "keep-2" }),
    // Flipped by an earlier pass whose navigation did not happen.
    "95": entry("ivy", { saveStreak: false, slot: "cycle-4", landing: "/videos/555" }),
    "96": entry("jo", { saveStreak: true, slot: "cycle-5", landing: "/videos/666" }),
  };
  const slots = [
    { id: "keep-1", streamer: "finn", entry: "live" }, { id: "keep-2", streamer: "hal", entry: "live" },
    { id: "cycle-1", streamer: "dave", entry: "live" }, { id: "cycle-2", streamer: "erin", entry: "live" },
    { id: "cycle-3", streamer: "gus", entry: "save" }, { id: "cycle-4", streamer: "ivy", entry: "live" },
    { id: "cycle-5", streamer: "jo", entry: "live" },
  ];
  local.slotPlan = plan(4, slots);
  await ctx.applySlotPlan(local.slotPlan);
  const out = {
    navs: navs.slice(), afterFlip: view(), gone: goneList(), released: released.slice(), removed: removedIds(),
    sent: lines(/save turn is now a live turn/).map((l) => l.replace(/^\[Stream Monitor\] /, "")),
    failed: lines(/could not send tab/).map((l) => l.replace(/^\[Stream Monitor\] /, "")),
  };
  // The browser's onUpdated for each navigation, and Twitch adding a time to
  // jo's VOD URL in place: nothing changes.
  for (const id of [90, 91, 92, 95]) await ctx.queueTabUpdated(id, { url: world.tabs[id].url }, clone(world.tabs[id]));
  const joUrl = "https://www.twitch.tv/videos/666?t=1m2s";
  await ctx.queueTabUpdated(96, { url: joUrl }, putTab(96, joUrl));
  out.afterEvents = view();
  out.goneAfterEvents = goneList();
  out.releasedAfterEvents = released.slice();
  // The same plan again: nothing is sent twice (jo's tab is still busy).
  navs.length = 0;
  local.slotPlan = plan(5, slots);
  await ctx.applySlotPlan(local.slotPlan);
  out.secondPass = navs.slice();
  // The turns end. jo's tab never left its VOD, and is closed there.
  local.slotPlan = plan(6, slots.map((s) => ({ id: s.id })),
    ["dave", "erin", "finn", "gus", "hal", "ivy", "jo"].map((streamer) => ({ streamer, reason: "turn_over" })));
  await ctx.applySlotPlan(local.slotPlan);
  out.closed = removedIds();
  out.tracked = view();
  out.lost = lines(/no longer shows/).length;
  out.goneAtEnd = goneList();
  return out;
}""".replace("VOD_ID", VOD_ID).replace("CLIP_SLUG", CLIP_SLUG)


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_r08_a_save_turn_flipped_to_live_goes_to_the_channel(kind, tmp_path):
    """A46 (X09 fix round 2): a plan that flips a save turn to live sends its
    tab to the streamer's channel (with sm=1) from the VOD, clip or
    save-streak page the save visit was on, keeps it tracked with its slot
    marker (saveStreak false) and drops the VOD path once the navigation is
    issued. The navigation's own onUpdated event changes nothing: no gone
    entry, no releaseLowQuality, no untrack. An entry an earlier pass flipped
    whose navigation did not happen (landing kept) is sent now; a tab that
    cannot be navigated keeps its landing, so a later turn_over close still
    finds and closes it on the VOD instead of leaving it open and untracked.
    A save turn the plan keeps as save, and a live tab that was never a
    save visit, are left where they are."""
    got = _run(kind, FLIP_CASE, tmp_path)
    channel = lambda login: f"https://www.twitch.tv/{login}?sm=1"  # noqa: E731
    assert got["navs"] == [[90, channel("dave")], [91, channel("erin")], [92, channel("finn")],
                           [95, channel("ivy")], [96, channel("jo")]]
    want = {
        "90": {"streamer": "dave", "slot": "cycle-1", "save": False, "landing": None},
        "91": {"streamer": "erin", "slot": "cycle-2", "save": False, "landing": None},
        "92": {"streamer": "finn", "slot": "keep-1", "save": False, "landing": None},
        "93": {"streamer": "gus", "slot": "cycle-3", "save": True, "landing": None},
        "94": {"streamer": "hal", "slot": "keep-2", "save": False, "landing": None},
        "95": {"streamer": "ivy", "slot": "cycle-4", "save": False, "landing": None},
        "96": {"streamer": "jo", "slot": "cycle-5", "save": False, "landing": "/videos/666"},
    }
    assert got["afterFlip"] == want
    assert got["gone"] == [] and got["released"] == [] and got["removed"] == []
    assert got["sent"] == [
        f"Slot plan: dave's save turn is now a live turn; tab 90 sent from /videos/{VOD_ID} to /dave",
        f"Slot plan: erin's save turn is now a live turn; tab 91 sent from /erin/clip/{CLIP_SLUG} to /erin",
        "Slot plan: finn's save turn is now a live turn; tab 92 sent from /save-streak/finn to /finn",
        "Slot plan: ivy's save turn is now a live turn; tab 95 sent from /videos/555 to /ivy",
    ]
    assert len(got["failed"]) == 1 and got["failed"][0].startswith(
        "Slot plan: could not send tab 96 to jo's channel: Tabs cannot be edited right now"), got["failed"]
    assert got["afterEvents"] == want
    assert got["goneAfterEvents"] == [] and got["releasedAfterEvents"] == []
    assert got["secondPass"] == [[96, channel("jo")]]
    assert got["closed"] == [90, 91, 92, 93, 94, 95, 96]
    assert got["tracked"] == {} and got["lost"] == 0 and got["goneAtEnd"] == []


CHAIN_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  delete local.slotPlan;
  local.raidFollowThrough = false;
  const out = {};
  // alice's channel tab (no plan) follows a link to bob's save-streak page,
  // and Twitch moves on in place at once: to bob's clip, or (the streak
  // already kept) to bob's channel. The browser hands over both events
  // before either is handled.
  const hops = {
    clip: [30, ["https://www.twitch.tv/save-streak/bob", "https://www.twitch.tv/bob/clip/CLIP_SLUG?range=7d"]],
    kept: [31, ["https://www.twitch.tv/save-streak/bob", "https://www.twitch.tv/bob"]],
  };
  for (const [name, [id, urls]] of Object.entries(hops)) {
    putTab(id, "https://www.twitch.tv/alice");
    local.trackedTabs = { [String(id)]: entry("alice") };
    const before = logs.length;
    await Promise.all(urls.map((url) => ctx.queueTabUpdated(id, { url, status: "loading" }, putTab(id, url))));
    const mine = logs.slice(before);
    out[name] = {
      tracked: Object.keys(local.trackedTabs),
      closed: removedIds().includes(id),
      raid: mine.filter((l) => /Raid detected|Raid follow-through/.test(l)).length,
      moved: mine.filter((l) => new RegExp(`Tab ${id} moved to bob's save-streak page \\(not a raid\\), untracking`).test(l)).length,
    };
  }
  // A handler that throws never stops the events queued behind it.
  putTab(32, "https://www.twitch.tv/alice");
  local.trackedTabs = { "32": entry("alice") };
  const broken = {};
  Object.defineProperty(broken, "url", { get() { throw new Error("boom"); } });
  await Promise.all([
    ctx.queueTabUpdated(32, broken, clone(world.tabs[32])),
    ctx.queueTabUpdated(32, { url: "https://example.com/" }, putTab(32, "https://example.com/")),
  ]);
  await sleep(0);
  out.error = {
    tracked: Object.keys(local.trackedTabs),
    failed: logs.filter((l) => /Tab 32 update handling failed: boom/.test(l)).length,
    away: logs.filter((l) => /Tab 32 navigated away from Twitch, untracking/.test(l)).length,
  };
  // Settled chains leave nothing behind.
  out.chains = vm.runInContext("tabUpdateChains.size", ctx);
  return out;
}""".replace("CLIP_SLUG", CLIP_SLUG)


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_one_tabs_updates_run_in_order_and_a_two_hop_move_is_no_raid(kind, tmp_path):
    """A46 (X09 fix round 2): the tabs.onUpdated listener (queueTabUpdated)
    handles one tab's events one after another. A tracked channel tab whose
    page follows a save-streak link that Twitch moves on in place at once
    (to the clip, or to the channel with the kept modal) is untracked at the
    first event as the owner's move; the second event finds it untracked and
    is no raid: the tab stays open, with no raid line. A handler that throws
    is logged and the next event of that tab still runs; a settled chain
    leaves no entry in the map."""
    got = _run(kind, CHAIN_CASE, tmp_path)
    for name in ("clip", "kept"):
        assert got[name] == {"tracked": [], "closed": False, "raid": 0, "moved": 1}, (name, got[name])
    assert got["error"] == {"tracked": [], "failed": 1, "away": 1}, got["error"]
    assert got["chains"] == 0


SCAN_CASE = r"""async (ctx) => {
  await sleep(50);
  const view = Object.fromEntries(Object.entries(local.trackedTabs || {}).map(([k, e]) => [k, {
    streamer: e.originalStreamer, slot: e.slot || null, save: e.saveStreak === true, landing: e.landing || null }]));
  responder = (path) => (path === "/open_tabs" ? { status: 204 } : { status: 503 });
  await ctx.reportOpenTabs("refresh");
  const report = requests.filter((r) => r.path === "/open_tabs").pop();
  return {
    tracked: view,
    readopted: logs.filter((l) => /Re-adopted restored tab for /.test(l)).map((l) => l.replace(/^.*tab for (\w+).*$/, "$1")),
    streamers: report ? report.body.streamers.slice().sort() : null,
  };
}"""


def _scan_seed_and_world(kind: str):
    """The previous session's entries and the tabs a browser start finds.
    The init runs with no browser-session marker, as after a restart."""
    old = int(time.time()) - 400
    opened = int(time.time() * 1000) - 3600 * 1000

    def entry(login, **extra):
        return {"originalStreamer": login, "raidHopCount": 0, "openedAt": opened, **extra}

    def tab(tab_id, url):
        return {"id": tab_id, "windowId": 1, "url": url, "active": False, "incognito": False}

    seed = {
        # Stale: no plan pass touches the markers, so what they read comes
        # from the scan (a null plan would strip them).
        "slotPlan": {"v": 1, "seq": 7, "generated_at": old, "active": True, "assigning": True,
                     "executor": f"{kind}-a1b2c3d4", "slots": [], "close": [], "queue": []},
        "trackedTabs": {
            # Same id (an extension update keeps tab ids): still on its VOD.
            "60": entry("dave", saveStreak=True, slot="cycle-1", landing=f"/videos/{VOD_ID}"),
            # Dead ids (a browser restart): the restored copies are untracked.
            "987654": entry("erin", saveStreak=True, slot="keep-1", landing="/videos/777"),
            "987655": entry("finn", saveStreak=True, slot="keep-2"),
            # Not save visits: a plain entry whose tab is on a VOD now, and a
            # lost plain entry (its restored VOD tab names no streamer).
            "63": entry("gus", slot="keep-1"),
            "987656": entry("hal"),
            # A save entry whose id now names a tab on another VOD (the
            # owner's own): lost, not kept.
            "65": entry("ivy", saveStreak=True, slot="keep-1", landing="/videos/111"),
            # Save turns the plan flipped to live (saveStreak false) before
            # their tabs left the VOD: the same id still on it is kept, a
            # restored copy on it is re-adopted, both with the landing.
            "67": entry("kai", saveStreak=False, slot="cycle-3", landing="/videos/444"),
            "987657": entry("jo", saveStreak=False, slot="cycle-2", landing="/videos/888"),
        },
    }
    world = {"tabs": {
        "60": tab(60, f"https://www.twitch.tv/videos/{VOD_ID}"),
        "61": tab(61, "https://www.twitch.tv/videos/777"),
        "62": tab(62, f"https://www.twitch.tv/finn/clip/{CLIP_SLUG}?range=7d"),
        "63": tab(63, "https://www.twitch.tv/videos/999"),
        "64": tab(64, "https://www.twitch.tv/videos/555"),
        "65": tab(65, "https://www.twitch.tv/videos/222"),
        "66": tab(66, "https://www.twitch.tv/videos/888"),
        "67": tab(67, "https://www.twitch.tv/videos/444"),
    }}
    return seed, world


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_r41_a_restored_save_visit_on_a_vod_is_readopted_after_a_restart(kind, tmp_path):
    """A46 with DESIGN 8.8 (the E14 paths): after a browser start the init
    scan keeps a save visit whose tab id still shows its VOD, re-adopts a
    restored copy on the remembered VOD path (a dead id) and one on the
    streamer's clip, each with its slot marker and as a save visit, and the
    next report lists all three. A plain entry on a VOD is lost as before,
    so is a save entry whose tab now shows a VOD other than its landing, and
    a VOD tab no entry remembers is never adopted. A save turn the plan
    flipped to live whose tab never left its VOD (saveStreak false, the
    landing kept) is found there the same way: kept by its id, or
    re-adopted on the restored copy, with its landing, so the next pass
    still sends it to the channel."""
    seed, world = _scan_seed_and_world(kind)
    got = _run(kind, SCAN_CASE, tmp_path, seed=seed, world=world)
    assert got["tracked"] == {
        "60": {"streamer": "dave", "slot": "cycle-1", "save": True, "landing": f"/videos/{VOD_ID}"},
        "61": {"streamer": "erin", "slot": "keep-1", "save": True, "landing": "/videos/777"},
        "62": {"streamer": "finn", "slot": "keep-2", "save": True, "landing": None},
        "66": {"streamer": "jo", "slot": "cycle-2", "save": False, "landing": "/videos/888"},
        "67": {"streamer": "kai", "slot": "cycle-3", "save": False, "landing": "/videos/444"},
    }, got["tracked"]
    assert sorted(got["readopted"]) == ["erin", "finn", "jo"]
    assert got["streamers"] == ["dave", "erin", "finn", "jo", "kai"]


RELEASE_LANDED_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  delete local.slotPlan;
  responder = (path) => (path === "/streak_event" ? { status: 204 } : { status: 503 });
  putTab(80, "https://www.twitch.tv/dave");
  putTab(81, "https://www.twitch.tv/erin/about");
  putTab(82, "https://www.twitch.tv/videos/VOD_ID");
  putTab(83, "https://www.twitch.tv/gus");
  putTab(84, "https://www.twitch.tv/videos/321");
  local.trackedTabs = {
    "80": entry("dave", { saveStreak: true, rescue: true }),
    "81": entry("erin", { saveStreak: true }),
    "82": entry("finn", { saveStreak: true, manualSave: true, landing: "/videos/VOD_ID" }),
    "83": entry("gus", { saveStreak: false }),
    "84": entry("jo", { saveStreak: true }),
  };
  for (const [id, login] of [[80, "dave"], [81, "erin"], [82, "finn"], [83, "gus"], [84, "jo"]]) {
    await ctx.handleStreakAlreadySaved({ status: "already_saved", streamer: login, count: 31,
      detected_at: new Date().toISOString(), page_url: world.tabs[id] ? world.tabs[id].url : "" }, id);
  }
  return {
    removed: removedIds(), tracked: Object.keys(local.trackedTabs).sort(),
    left: lines(/Leaving tab \d+ open for \w+/).map((l) => l.replace(/^.*Leaving tab (\d+) open for (\w+): (.*)$/, "$2: $3")),
  };
}""".replace("VOD_ID", VOD_ID)


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am46_already_saved_closes_a_save_visit_where_twitch_moved_it(kind, tmp_path):
    """A46 with DESIGN 12.3: outside a plan, the already-kept modal on the
    channel page a save-streak page moved to closes that save-streak tab (a
    rescue or manual save visit), as the page itself did before; so does a
    save visit on the VOD it moved to. A save tab the owner moved on to
    another page of the channel, one on a VOD it never moved to, and a
    channel tab that was never a save-streak page, stay."""
    got = _run(kind, RELEASE_LANDED_CASE, tmp_path)
    assert got["removed"] == [80, 82]
    assert got["tracked"] == ["81", "83", "84"]
    assert got["left"] == [
        "erin: it is no longer on the save-streak page",
        "gus: Stream Monitor did not open it as that save-streak page",
        "jo: it is no longer on the save-streak page",
    ]


NOT_ELIGIBLE_CASE = r"""async (ctx) => {""" + SAVE_VISIT_HELPERS + r"""
  putTab(70, "https://www.twitch.tv/save-streak/dave");
  local.trackedTabs = { "70": entry("dave", { saveStreak: true, slot: "cycle-1" }) };
  local.atRiskStreaks = { dave: row("dave") };
  local.slotPlan = plan(5, [{ id: "cycle-1", streamer: "dave", entry: "save" }]);
  await ctx.handleNotEligible(70, "dave");
  await sleep(50);
  const out = {
    tracked: !!local.trackedTabs["70"], removed: removedIds(), gone: goneList(),
    ack: !!local.atRiskStreaks.dave.acknowledged_at,
    lines: lines(/othing to watch/).map((l) => l.replace(/^\[Stream Monitor\] /, "")),
    posts: requests.filter((r) => r.path === "/streak_event").length,
  };
  // The turn then runs its length: the plan's turn_over close ends it.
  local.slotPlan = plan(6, [{ id: "cycle-1" }], [{ streamer: "dave", reason: "turn_over" }]);
  await ctx.applySlotPlan(local.slotPlan);
  out.afterTurn = { tracked: !!local.trackedTabs["70"], removed: removedIds(), ack: !!local.atRiskStreaks.dave.acknowledged_at };
  return out;
}"""


@pytest.mark.parametrize("closes", [False, True], ids=["shipped_false", "flipped_true"])
@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_am41_am16_not_eligible_follows_the_switch(kind, closes, tmp_path):
    """A41 item 3 and A16: the shipped backgrounds hold
    NOT_ELIGIBLE_CLOSES_TURN false (the unsavable page was never seen live),
    so a not_eligible report on a save turn writes one debug line and does
    nothing else: the tab stays tracked with no gone entry and the row is
    not acknowledged, until the plan's turn_over close ends the turn and
    acknowledges it. A copy with the switch true ends the turn at once with
    a not_eligible gone entry and acknowledges the row; neither posts."""
    text = BACKGROUNDS[kind].read_text(encoding="utf-8")
    line = "const NOT_ELIGIBLE_CLOSES_TURN = false;"
    assert text.count(line) == 1, "the shipped background does not hold NOT_ELIGIBLE_CLOSES_TURN false"
    bg_path = None
    if closes:
        bg_path = tmp_path / f"background_{kind}_closes.js"
        bg_path.write_text(text.replace(line, "const NOT_ELIGIBLE_CLOSES_TURN = true;"), encoding="utf-8")
    got = _run(kind, NOT_ELIGIBLE_CASE, tmp_path, bg_path=bg_path)
    assert got["posts"] == 0
    if closes:
        assert got["tracked"] is False and got["removed"] == [70]
        assert got["gone"] == ["dave:not_eligible"] and got["ack"] is True
        assert got["lines"] == ["Nothing to watch on dave's save-streak page (tab 70)",
                                "Slot plan: nothing to watch for dave; turn ended early"]
        assert got["afterTurn"] == {"tracked": False, "removed": [70], "ack": True}
    else:
        assert got["tracked"] is True and got["removed"] == [] and got["gone"] == [] and got["ack"] is False
        assert got["lines"] == ["Nothing to watch on dave's save-streak page (tab 70); left as it is"]
        assert got["afterTurn"] == {"tracked": False, "removed": [70], "ack": True}


# ---------------------------------------------------------------------------
# Max open streams by list order (v1.12.1)
# ---------------------------------------------------------------------------

MAX_TABS_ORDER_CASE = r"""async (ctx) => {
  const MIN = 60 * 1000;
  const now = Date.now();
  const list = ["khaosvt", "caedvt", "faenilia", "mervynova", "sw33t_exe", "krytouz", "maachuh", "siigynn",
    "itsthefluffs"];
  const tab = (name, ageMin, extra = {}) => ({ originalStreamer: name, raidHopCount: 0,
    openedAt: now - ageMin * MIN, saveStreak: false, ...extra });
  const allLive = () => Object.fromEntries(list.map((n) => [n, now]));
  // A reading now, at the end of two hours of unbroken readings.
  const reading = (seen = allLive(), o = {}) => ({ at: now, since: now - 120 * MIN, seen, ...o });
  // leaving: keys of tabs already scheduled to close (the record names the
  // tab's streamer, or leavingAs).
  const C = (tabs, o = {}) => ({ pinned: new Set(o.pinned || []), order: new Map(list.map((n, i) => [n, i])),
    liveSeen: "liveSeen" in o ? o.liveSeen : reading(),
    leaving: new Map((o.leaving || []).map((k) => [k, o.leavingAs || tabs[k].originalStreamer])), now });
  const brief = (d) => (d ? [d.kind, d.why || (d.target ? d.target.streamer : null), d.target ? d.target.tabKey : null]
    : null);
  const decide = (tabs, key, o) => brief(ctx.maxTabsDecision(tabs, key, 3, C(tabs, o)));
  const trio = (a, b, c) => ({ "1": tab("khaosvt", a), "2": tab("caedvt", b), "3": tab("maachuh", c) });
  const everyone = ["khaosvt", "caedvt", "maachuh", "itsthefluffs"];
  const out = {};

  // The reported case: Khaos, Caed and maachuh open, itsthefluffs goes live.
  out.reported = decide({ ...trio(9, 8, 7), "4": tab("itsthefluffs", 0) }, "4");
  // A newcomer above maachuh: maachuh gives way, at its grace end or at once.
  out.higherInGrace = decide({ ...trio(9, 8, 7), "4": tab("faenilia", 0) }, "4");
  out.higherPastGrace = decide({ ...trio(90, 80, 70), "4": tab("faenilia", 0) }, "4");
  // Keep Open: maachuh is protected, so faenilia (below Khaos and Caed) goes.
  out.keepOpenOther = decide({ ...trio(90, 80, 70), "4": tab("faenilia", 0) }, "4", { pinned: ["maachuh"] });
  out.pinnedNewcomer = decide({ ...trio(90, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { pinned: ["itsthefluffs"] });
  out.allProtected = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80),
    "3": tab("maachuh", 70, { rescue: true }), "4": tab("itsthefluffs", 0) }, "4", { pinned: ["khaosvt", "caedvt"] });
  // A Keep Open newcomer is never closed for the limit, also when nobody else can give way.
  out.keptNewcomerAllProtected = decide({ ...trio(90, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { pinned: everyone });
  // Manual save and slot tabs are never chosen, however low their streamer.
  out.manualAndSlot = decide({ "1": tab("khaosvt", 90), "2": tab("siigynn", 80, { manualSave: true }),
    "3": tab("itsthefluffs", 70, { slot: "cycle-1" }), "4": tab("maachuh", 0) }, "4");

  // A stream that ended gives way before any live one, even the top of the list.
  const ended = { ...allLive(), khaosvt: now - 30 * MIN };
  out.endedFirst = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4", { liveSeen: reading(ended) });
  // Ended while its tab is still in grace: closed at once, there is nothing left to watch.
  out.endedInGrace = decide({ ...trio(15, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { liveSeen: reading({ ...allLive(), khaosvt: now - 12 * MIN }) });
  // One missed poll is not an end.
  out.missedPoll = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { liveSeen: reading({ ...allLive(), khaosvt: now - 2 * MIN }) });
  // An old reading (the desktop app is not answering) or none says nothing.
  out.staleData = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { liveSeen: reading(ended, { at: now - 10 * MIN }) });
  out.noLiveData = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4", { liveSeen: undefined });
  // Readings that only resumed 5 minutes ago (the desktop app was closed,
  // the computer asleep) cannot say that a stream ended.
  out.runTooShort = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { liveSeen: reading(ended, { since: now - 5 * MIN }) });
  // Keep Open protects even after the stream ended (the owner's choice).
  out.endedKeepOpen = decide({ ...trio(120, 80, 70), "4": tab("itsthefluffs", 0) }, "4",
    { pinned: ["khaosvt"], liveSeen: reading(ended) });
  // A save visit plays a recording: its streamer being offline is no end.
  const offline = { ...allLive(), maachuh: now - 30 * MIN };
  out.saveVisit = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("maachuh", 20, { saveStreak: true }),
    "4": tab("faenilia", 0) }, "4", { liveSeen: reading(offline) });
  out.landedVisit = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80),
    "3": tab("maachuh", 20, { landing: "/videos/1" }), "4": tab("faenilia", 0) }, "4", { liveSeen: reading(offline) });

  // A streamer not on the list (a raid that was followed) counts as lowest
  // but is never an ended stream; two of them: the oldest tab first.
  out.unlisted = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("raidtarget", 12),
    "4": tab("itsthefluffs", 0) }, "4");
  out.unlistedTie = decide({ "1": tab("khaosvt", 90), "2": tab("raid_a", 6), "3": tab("raid_b", 4),
    "4": tab("caedvt", 0) }, "4");

  // An older tab of a streamer with a newer one goes before any live stream, at once.
  out.duplicate = decide({ "1": tab("khaosvt", 5), "2": tab("caedvt", 80), "3": tab("maachuh", 70),
    "4": tab("khaosvt", 0) }, "4");
  out.endedBeforeDuplicate = decide({ "1": tab("khaosvt", 5), "2": tab("caedvt", 80), "3": tab("maachuh", 70),
    "4": tab("khaosvt", 0) }, "4", { liveSeen: reading({ ...allLive(), caedvt: now - 30 * MIN }) });

  // Tabs already scheduled to close do not count, and are not chosen again.
  const five = { ...trio(90, 80, 70), "4": tab("itsthefluffs", 5), "5": tab("faenilia", 0) };
  out.leavingNotChosenAgain = decide(five, "5", { leaving: ["4"] });
  out.leavingMakesRoom = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "4": tab("itsthefluffs", 5),
    "5": tab("maachuh", 0) }, "5", { leaving: ["4"] });
  // A record for another streamer (the tab id was used again) says nothing about this tab.
  out.leavingOtherStreamer = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "4": tab("itsthefluffs", 5),
    "5": tab("maachuh", 0) }, "5", { leaving: ["4"], leavingAs: "someoneelse" });
  out.selfLeaving = decide(five, "4", { leaving: ["4"] });
  out.withinLimit = decide({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "4": tab("maachuh", 0) }, "4");
  out.goneMeanwhile = decide(trio(90, 80, 70), "9");

  // A scheduled close that has come due is decided again.
  const due = (tabs, key, max, o) => brief(ctx.maxTabsDueDecision(tabs, key, max, C(tabs, o)));
  const four = { ...trio(90, 80, 70), "4": tab("itsthefluffs", 31) };
  const pair = (a, b) => ({ "2": tab("caedvt", 80), "3": tab("maachuh", 70), "4": tab("itsthefluffs", a),
    "5": tab("siigynn", b) });
  out.due = {
    stillOver: due(four, "4", 3),
    limitOff: due(four, "4", 0),
    roomAgain: due({ "2": tab("caedvt", 80), "3": tab("maachuh", 70), "4": tab("itsthefluffs", 31) }, "4", 3),
    gone: due(trio(90, 80, 70), "9", 2),
    // A stream ended in the meantime: its tab closes, the due one stays.
    endedStandIn: due(four, "4", 3, { liveSeen: reading(ended) }),
    // Keep Open since it was scheduled: the lowest unprotected gives way, at once or after its grace.
    keptNow: due(four, "4", 3, { pinned: ["itsthefluffs"] }),
    keptNowInGrace: due({ ...trio(9, 8, 7), "4": tab("itsthefluffs", 31) }, "4", 3, { pinned: ["itsthefluffs"] }),
    allKept: due(four, "4", 3, { pinned: everyone }),
    // A lower stream that arrived while there was room.
    lowerInGrace: due({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("maachuh", 31),
      "4": tab("itsthefluffs", 10) }, "3", 3),
    lowerPastGrace: due({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("maachuh", 31),
      "4": tab("itsthefluffs", 40) }, "3", 3),
    duplicateStandIn: due({ "1": tab("khaosvt", 50), "2": tab("caedvt", 80), "3": tab("khaosvt", 20),
      "4": tab("itsthefluffs", 31) }, "4", 3),
    // Two tabs waiting and room for one: the first to come due stays; at
    // the second the lower of the two gives way.
    firstOfTwo: due(pair(31, 20), "4", 3, { leaving: ["5"] }),
    secondOfTwo: due(pair(41, 31), "5", 3),
    bothGo: due({ "1": tab("khaosvt", 90), ...pair(31, 20) }, "5", 3, { leaving: ["4"] }),
  };

  // The live record the desktop's list feeds.
  await ctx.noteLiveSeen(["KhaosVT", "caedvt"], now - 20 * MIN);
  await ctx.noteLiveSeen(["caedvt"], now - 18 * MIN);
  out.run = { sinceAgoMin: (now - local.liveSeen.since) / MIN, atAgoMin: (now - local.liveSeen.at) / MIN };
  await ctx.noteLiveSeen(["caedvt"], now);
  local.liveSeen.seen.longgone = now - 25 * 60 * MIN;
  out.live = { at: local.liveSeen.at === now, newRun: local.liveSeen.since === now,
    khaosAgoMin: (now - local.liveSeen.seen.khaosvt) / MIN, caedAgoMin: (now - local.liveSeen.seen.caedvt) / MIN };
  await ctx.noteLiveSeen([], now + 1000);
  out.live.pruned = !("longgone" in local.liveSeen.seen) && "caedvt" in local.liveSeen.seen;
  out.live.sameRun = local.liveSeen.since === now;
  const E = (info, liveSeen) => ctx.streamEnded(info, liveSeen, now);
  const run = (seen, sinceMin = 60) => ({ at: now, since: now - sinceMin * MIN, seen });
  out.ended = {
    young: E(tab("khaosvt", 5), run({})),
    neverSeen: E(tab("khaosvt", 40), run({})),
    neverSeenShortRun: E(tab("khaosvt", 40), run({}, 5)),
    noRunStart: E(tab("khaosvt", 40), { at: now, seen: {} }),
    seenLately: E(tab("khaosvt", 40), run({ khaosvt: now - 9 * MIN })),
    seenLongAgo: E(tab("khaosvt", 40), run({ khaosvt: now - 10 * MIN })),
    seenBeforeTheGap: E(tab("khaosvt", 40), run({ khaosvt: now - 30 * MIN }, 5)),
    staleReading: E(tab("khaosvt", 40), { at: now - 4 * MIN, since: now - 60 * MIN, seen: {} }),
    saveVisit: E(tab("khaosvt", 40, { saveStreak: true }), run({})),
    landedVisit: E(tab("khaosvt", 40, { landing: "/videos/1" }), run({})),
  };
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_max_tabs_list_order_decides_who_gives_way(kind, tmp_path):
    """v1.12.1 (the owner's request of 2026-10-03): at the limit the open
    stream lowest in the desktop's list order gives way, the new one
    included; a stream that ended and an older duplicate tab go before any
    live one; Keep Open, rescue, manual save and slot tabs are never
    chosen; a tab already scheduled to close neither counts nor is chosen
    again; and a scheduled close is decided again when it comes due."""
    got = _run(kind, MAX_TABS_ORDER_CASE, tmp_path)
    lowest = ["expire", "lowest", None]
    assert got["reported"] == lowest
    assert got["higherInGrace"] == ["swap", "maachuh", "3"]
    assert got["higherPastGrace"] == ["close", "maachuh", "3"]
    assert got["keepOpenOther"] == lowest
    assert got["pinnedNewcomer"] == ["close", "maachuh", "3"]
    assert got["allProtected"] == ["expire", "protected", None]
    assert got["keptNewcomerAllProtected"] is None
    assert got["manualAndSlot"] == lowest
    assert got["endedFirst"] == ["close", "khaosvt", "1"]
    assert got["endedInGrace"] == ["close", "khaosvt", "1"]
    assert got["missedPoll"] == lowest
    assert got["staleData"] == lowest
    assert got["noLiveData"] == lowest
    assert got["runTooShort"] == lowest
    assert got["endedKeepOpen"] == lowest
    assert got["saveVisit"] == ["swap", "maachuh", "3"]
    assert got["landedVisit"] == ["swap", "maachuh", "3"]
    assert got["unlisted"] == ["swap", "raidtarget", "3"]
    assert got["unlistedTie"] == ["swap", "raid_a", "2"]
    assert got["duplicate"] == ["close", "khaosvt", "1"]
    assert got["endedBeforeDuplicate"] == ["close", "caedvt", "2"]
    assert got["leavingNotChosenAgain"] == ["close", "maachuh", "3"]
    assert got["leavingMakesRoom"] is None
    assert got["leavingOtherStreamer"] == ["swap", "itsthefluffs", "4"]
    assert got["selfLeaving"] is None
    assert got["withinLimit"] is None
    assert got["goneMeanwhile"] is None
    assert got["due"] == {
        "stillOver": ["close", "itsthefluffs", "4"],
        "limitOff": ["room", None, None],
        "roomAgain": ["room", None, None],
        "gone": ["room", None, None],
        "endedStandIn": ["instead", "khaosvt", "1"],
        "keptNow": ["instead", "maachuh", "3"],
        "keptNowInGrace": ["defer", "maachuh", "3"],
        "allKept": ["kept", None, None],
        "lowerInGrace": ["defer", "itsthefluffs", "4"],
        "lowerPastGrace": ["instead", "itsthefluffs", "4"],
        "duplicateStandIn": ["instead", "khaosvt", "1"],
        "firstOfTwo": ["room", None, None],
        "secondOfTwo": ["instead", "itsthefluffs", "4"],
        "bothGo": ["close", "siigynn", "5"],
    }
    assert got["run"] == {"sinceAgoMin": 20, "atAgoMin": 18}
    assert got["live"] == {"at": True, "newRun": True, "khaosAgoMin": 20, "caedAgoMin": 0, "pruned": True,
                           "sameRun": True}
    assert got["ended"] == {
        "young": False, "neverSeen": True, "neverSeenShortRun": False, "noRunStart": False, "seenLately": False,
        "seenLongAgo": True, "seenBeforeTheGap": False, "staleReading": False, "saveVisit": False,
        "landedVisit": False,
    }


MAX_TABS_FLOW_CASE = r"""async (ctx) => {
  const MIN = 60 * 1000;
  const now = Date.now();
  const list = ["khaosvt", "caedvt", "faenilia", "maachuh", "siigynn", "itsthefluffs"];
  const tab = (name, ageMin) => ({ originalStreamer: name, raidHopCount: 0, openedAt: now - ageMin * MIN,
    saveStreak: false });
  const lines = (re) => logs.filter((l) => re.test(l)).map((l) => l.replace(/^\[Stream Monitor\] /, ""));
  const removed = () => world.calls.filter((c) => c[0] === "tabs.remove").map((c) => c[1]);
  const tracked = () => Object.keys(local.trackedTabs || {}).sort();
  const expirations = () => local.pendingExpirations.map((e) => [e.tabKey, e.streamer,
    Math.round((e.scheduledAt - now) / MIN)]);
  const swaps = () => local.pendingSwaps.map((s) => [s.targetTabKey, s.targetStreamer, s.newTabKey, s.newStreamer,
    Math.round((s.scheduledAt - now) / MIN)]);
  const asked = () => requests.filter((r) => r.path === "/config").length;
  const down = () => ({ status: 503, text: "" });
  const reset = (tabs) => {
    local.trackedTabs = tabs;
    local.pendingSwaps = [];
    local.pendingExpirations = [];
    local.maxTabs = 3;
    local.monitoredStreamers = list;
    local.pinnedStreamers = [];
    local.liveSeen = { at: now, since: now - 120 * MIN, seen: Object.fromEntries(list.map((n) => [n, now])) };
    responder = down;
    world.calls.length = 0;
    logs.length = 0;
    requests.length = 0;
  };
  const young = (newcomer) => ({ "1": tab("khaosvt", 9), "2": tab("caedvt", 8), "3": tab("maachuh", 7),
    "4": tab(newcomer, 0) });
  const everyone = ["khaosvt", "caedvt", "maachuh", "itsthefluffs"];
  const out = {};

  // The reported case: the newcomer is the lowest, so it is the one to go,
  // after its 30 minutes. Nothing closes now.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  out.lowest = { expirations: expirations(), swaps: local.pendingSwaps.length, removed: removed(),
    tracked: tracked(), lines: lines(/Max tabs \(3\) reached/), asked: asked() };
  await ctx.executePendingExpiration("4");
  out.lowestDue = { removed: removed(), tracked: tracked() };

  // Khaos's tab closed before the timer: there is room, the newcomer stays.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  delete local.trackedTabs["1"];
  await ctx.executePendingExpiration("4");
  out.roomAgain = { removed: removed(), tracked: tracked(), lines: lines(/has room again/),
    expirations: local.pendingExpirations.length };

  // The limit was turned off before the timer.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  local.maxTabs = 0;
  await ctx.executePendingExpiration("4");
  out.limitOff = { removed: removed(), tracked: tracked() };

  // A newcomer above maachuh: maachuh is the lowest and still in grace, so
  // both stay open until its 30 minutes are up.
  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  out.swap = { swaps: swaps(), expirations: local.pendingExpirations.length, removed: removed(),
    lines: lines(/Max tabs \(3\) reached/) };
  await ctx.executePendingSwap("4");
  out.swapDue = { removed: removed(), tracked: tracked(), lines: lines(/Executing pending swap/) };

  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  delete local.trackedTabs["2"];
  await ctx.executePendingSwap("4");
  out.swapRoom = { removed: removed(), tracked: tracked(), lines: lines(/has room again/) };

  // A plan made the swap's new tab a slot tab since: the swap is left to the plan.
  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  local.trackedTabs["4"].slot = "cycle-1";
  await ctx.executePendingSwap("4");
  out.swapSlotPartner = { removed: removed(), tracked: tracked(), lines: lines(/Slot plan: skipped/) };

  // The swap's new tab goes away: the target's close stays scheduled, as a
  // pending expiration, and is dropped when it comes due with room again.
  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  delete local.trackedTabs["4"];
  await ctx.cancelPendingSwapsForTab("4");
  out.newTabGone = { swaps: swaps(), expirations: expirations(), lines: lines(/^\[Stream Monitor\] Pending swap \(/) };
  await ctx.executePendingExpiration("3");
  out.newTabGoneDue = { removed: removed(), tracked: tracked(), lines: lines(/has room again/) };
  // The swap's target goes away: the swap is dropped.
  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  delete local.trackedTabs["3"];
  await ctx.cancelPendingSwapsForTab("3");
  out.targetGone = { swaps: swaps(), expirations: expirations(), lines: lines(/Cancelled pending swap/) };

  // Past its grace, the lowest closes at once.
  reset({ "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("maachuh", 70), "4": tab("faenilia", 0) });
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  out.closeNow = { removed: removed(), tracked: tracked(), lines: lines(/Max tabs \(3\) reached/) };

  // An ended stream goes first, with its reason in the log. The desktop is
  // asked again before the verdict is acted on; here it does not answer.
  const old = () => ({ "1": tab("khaosvt", 120), "2": tab("caedvt", 80), "3": tab("maachuh", 70),
    "4": tab("itsthefluffs", 0) });
  reset(old());
  local.liveSeen.seen.khaosvt = now - 30 * MIN;
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  out.endedCloses = { removed: removed(), tracked: tracked(), lines: lines(/Max tabs \(3\) reached/), asked: asked() };
  // The same, but the desktop answers that Khaos is live (the stored
  // reading was behind): nothing ended, the newcomer is the lowest.
  reset(old());
  local.liveSeen.seen.khaosvt = now - 30 * MIN;
  responder = (path) => (path === "/config"
    ? { status: 200, text: JSON.stringify({ streamers: list, live_streamers: ["KhaosVT", "caedvt", "maachuh"] }) }
    : down());
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  out.confirmedLive = { removed: removed(), expirations: expirations(), asked: asked() };

  // A stream ends while a lower one waits to close: when the timer comes
  // due the ended tab closes and the live newcomer stays.
  reset({ "1": tab("khaosvt", 20), "2": tab("caedvt", 8), "3": tab("maachuh", 7), "4": tab("itsthefluffs", 0) });
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  local.liveSeen.seen.khaosvt = now - 12 * MIN;
  await ctx.executePendingExpiration("4");
  out.dueEnded = { removed: removed(), tracked: tracked(), expirations: expirations(),
    lines: lines(/Pending expiration fired/) };

  // The owner puts the waiting stream on Keep Open: it stays, and the
  // lowest unprotected stream closes when its own 30 minutes are up.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  local.pinnedStreamers = ["itsthefluffs"];
  await ctx.executePendingExpiration("4");
  out.dueKeepOpen = { removed: removed(), tracked: tracked(), expirations: expirations(),
    lines: lines(/Pending expiration fired/) };
  // Everything open is on Keep Open by then: nothing closes.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  local.pinnedStreamers = everyone;
  await ctx.executePendingExpiration("4");
  out.dueAllKept = { removed: removed(), expirations: expirations(), lines: lines(/Pending expiration fired/) };
  // The due tab is also the target of a swap still pending: its own timer
  // decides now, the other record does not make it count as leaving.
  reset(young("itsthefluffs"));
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  local.pendingSwaps = [{ newTabKey: "1", newStreamer: "khaosvt", targetTabKey: "4", targetStreamer: "itsthefluffs",
    scheduledAt: now + 30 * MIN }];
  await ctx.executePendingExpiration("4");
  out.dueTwoRecords = { removed: removed(), tracked: tracked() };
  // A swap target put on Keep Open: the newcomer, now the lowest unprotected, is the one that waits.
  reset(young("faenilia"));
  await ctx.enforceMaxTabs("4", "faenilia", 3, now);
  local.pinnedStreamers = ["maachuh"];
  await ctx.executePendingSwap("4");
  out.dueSwapKeepOpen = { removed: removed(), expirations: expirations(), lines: lines(/Pending swap fired/) };

  // A Keep Open newcomer with only protected tabs open is not scheduled to close.
  reset(young("itsthefluffs"));
  local.pinnedStreamers = everyone;
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  out.keptNewcomer = { expirations: expirations(), swaps: swaps(), removed: removed(), lines: lines(/Max tabs/) };
  // A newcomer that is not protected, with only protected tabs open, gets its 30 minutes.
  reset(young("itsthefluffs"));
  local.pinnedStreamers = ["khaosvt", "caedvt", "maachuh"];
  await ctx.enforceMaxTabs("4", "itsthefluffs", 3, now);
  out.allProtected = { expirations: expirations(), removed: removed(), lines: lines(/Max tabs/) };

  // A second tab of a stream that dropped and came back: the older tab closes at once.
  reset({ "1": tab("khaosvt", 5), "2": tab("caedvt", 80), "3": tab("maachuh", 70), "4": tab("khaosvt", 0) });
  await ctx.enforceMaxTabs("4", "khaosvt", 3, now);
  out.duplicate = { removed: removed(), tracked: tracked(), lines: lines(/Max tabs \(3\) reached/) };

  // Two streams tracked in the same moment: each sees the other's record.
  reset({ ...young("itsthefluffs"), "5": tab("siigynn", 0) });
  await Promise.all([ctx.enforceMaxTabs("4", "itsthefluffs", 3, now), ctx.enforceMaxTabs("5", "siigynn", 3, now)]);
  out.twoAtOnce = { expirations: expirations().sort(), swaps: swaps(), removed: removed() };
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_max_tabs_by_list_order_end_to_end_in_the_background(kind, tmp_path):
    """enforceMaxTabs and the two timers with real storage: the lowest
    newcomer gets 30 minutes and then closes; a scheduled close is dropped
    when there is room again or the limit is off, and goes to another tab
    when a stream ended or Keep Open changed meanwhile; a higher newcomer
    swaps out the lowest open stream at its grace end, or at once when
    past it; Keep Open newcomers and duplicates; two arrivals at once."""
    got = _run(kind, MAX_TABS_FLOW_CASE, tmp_path)
    assert got["lowest"] == {
        "expirations": [["4", "itsthefluffs", 30]], "swaps": 0, "removed": [], "tracked": ["1", "2", "3", "4"],
        "lines": ["Max tabs (3) reached and itsthefluffs is the lowest in list order among the open streams. "
                  "Keeping its tab open for 30m to preserve streak, then closing."],
        "asked": 0,
    }
    assert got["lowestDue"] == {"removed": [4], "tracked": ["1", "2", "3"]}
    assert got["roomAgain"] == {
        "removed": [], "tracked": ["2", "3", "4"], "expirations": 0,
        "lines": ["Pending expiration fired but Max tabs has room again; itsthefluffs (tab 4) stays open"],
    }
    assert got["limitOff"] == {"removed": [], "tracked": ["1", "2", "3", "4"]}
    assert got["swap"] == {
        "swaps": [["3", "maachuh", "4", "faenilia", 23]], "expirations": 0, "removed": [],
        "lines": ["Max tabs (3) reached but unpinned maachuh (tab 3), the lowest in list order, is in grace "
                  "(23m left). Keeping both tabs open; swap scheduled."],
    }
    assert got["swapDue"] == {
        "removed": [3], "tracked": ["1", "2", "4"],
        "lines": ["Executing pending swap: closing maachuh (tab 3) now that grace has expired; "
                  "faenilia keeps its slot"],
    }
    assert got["swapRoom"] == {
        "removed": [], "tracked": ["1", "3", "4"],
        "lines": ["Pending swap fired but Max tabs has room again; maachuh (tab 3) stays open"],
    }
    assert got["swapSlotPartner"] == {
        "removed": [], "tracked": ["1", "2", "3", "4"],
        "lines": ["Slot plan: skipped the pending swap for maachuh (tab 3); the plan manages slot tabs"],
    }
    assert got["newTabGone"] == {
        "swaps": [], "expirations": [["3", "maachuh", 23]],
        "lines": ["Pending swap (faenilia <- maachuh): tab 4 is gone; the close of maachuh (tab 3) stays scheduled"],
    }
    assert got["newTabGoneDue"] == {
        "removed": [], "tracked": ["1", "2", "3"],
        "lines": ["Pending expiration fired but Max tabs has room again; maachuh (tab 3) stays open"],
    }
    assert got["targetGone"] == {
        "swaps": [], "expirations": [],
        "lines": ["Cancelled pending swap (faenilia <- maachuh) because tab 3 is gone"],
    }
    assert got["closeNow"] == {
        "removed": [3], "tracked": ["1", "2", "4"],
        "lines": ["Max tabs (3) reached. Closing unpinned tab 3 (maachuh) to make room for faenilia "
                  "(lowest in list order)"],
    }
    assert got["endedCloses"] == {
        "removed": [1], "tracked": ["2", "3", "4"],
        "lines": ["Max tabs (3) reached. Closing unpinned tab 1 (khaosvt) to make room for itsthefluffs "
                  "(its stream ended)"],
        "asked": 1,
    }
    assert got["confirmedLive"] == {"removed": [], "expirations": [["4", "itsthefluffs", 30]], "asked": 1}
    assert got["dueEnded"] == {
        "removed": [1], "tracked": ["2", "3", "4"], "expirations": [],
        "lines": ["Pending expiration fired but khaosvt (tab 1) gives way before itsthefluffs (tab 4): "
                  "its stream ended. Closing it; itsthefluffs stays open"],
    }
    assert got["dueKeepOpen"] == {
        "removed": [], "tracked": ["1", "2", "3", "4"], "expirations": [["3", "maachuh", 23]],
        "lines": ["Pending expiration fired but maachuh (tab 3) gives way before itsthefluffs (tab 4) and is in "
                  "grace (23m left). itsthefluffs stays open; maachuh closes then."],
    }
    assert got["dueAllKept"] == {
        "removed": [], "expirations": [],
        "lines": ["Pending expiration fired but every open tab is protected now; itsthefluffs (tab 4) stays open"],
    }
    assert got["dueTwoRecords"] == {"removed": [4], "tracked": ["1", "2", "3"]}
    assert got["dueSwapKeepOpen"] == {
        "removed": [], "expirations": [["4", "faenilia", 30]],
        "lines": ["Pending swap fired but faenilia (tab 4) gives way before maachuh (tab 3) and is in grace "
                  "(30m left). maachuh stays open; faenilia closes then."],
    }
    assert got["keptNewcomer"] == {"expirations": [], "swaps": [], "removed": [], "lines": []}
    assert got["allProtected"] == {
        "expirations": [["4", "itsthefluffs", 30]], "removed": [],
        "lines": ["Max tabs (3) reached and all open tabs are pinned or rescue-protected. Keeping itsthefluffs's "
                  "tab open for 30m to preserve streak, then closing."],
    }
    assert got["duplicate"] == {
        "removed": [1], "tracked": ["2", "3", "4"],
        "lines": ["Max tabs (3) reached. Closing unpinned tab 1 (khaosvt) to make room for khaosvt "
                  "(a newer tab of this stream is open)"],
    }
    assert got["twoAtOnce"] == {
        "expirations": [["4", "itsthefluffs", 30], ["5", "siigynn", 30]], "swaps": [], "removed": [],
    }


MAX_TABS_LISTENER_CASE = r"""async (ctx) => {
  const MIN = 60 * 1000;
  const now = Date.now();
  const list = ["KhaosVT", "caedvt", "faenilia", "maachuh", "siigynn", "itsthefluffs"];
  const tab = (name, ageMin) => ({ originalStreamer: name, raidHopCount: 0, openedAt: now - ageMin * MIN,
    saveStreak: false });
  const lines = (re) => logs.filter((l) => re.test(l)).map((l) => l.replace(/^\[Stream Monitor\] /, ""));
  // Open: Khaos, Caed, maachuh (Keep Open in the desktop's settings),
  // itsthefluffs (waiting to close) and siigynn (the target of a swap).
  local.trackedTabs = { "1": tab("khaosvt", 90), "2": tab("caedvt", 80), "3": tab("maachuh", 70),
    "5": tab("itsthefluffs", 5), "6": tab("siigynn", 6) };
  local.pendingExpirations = [{ tabKey: "5", streamer: "itsthefluffs", scheduledAt: now + 25 * MIN }];
  local.pendingSwaps = [{ newTabKey: "2", newStreamer: "caedvt", targetTabKey: "6", targetStreamer: "siigynn",
    scheduledAt: now + 24 * MIN }];
  local.maxTabs = 3;
  delete local.liveSeen;
  responder = (path) => (path === "/config"
    ? { status: 200, text: JSON.stringify({ streamers: list, pinned_streamers: ["Maachuh"],
      live_streamers: list }) }
    : { status: 503, text: "" });
  await ctx.fetchConfig({ applyPlan: false });
  const out = { seenKhaos: typeof ((local.liveSeen || {}).seen || {}).khaosvt === "number",
    runStarted: typeof (local.liveSeen || {}).since === "number" };
  world.calls.length = 0;
  logs.length = 0;

  // faenilia goes live: the desktop opens her stream.
  const url = "https://www.twitch.tv/faenilia?sm=1";
  await ctx.onTabUpdated(4, { url }, { id: 4, windowId: 1, url });
  out.tracked = Object.keys(local.trackedTabs).sort();
  out.removed = world.calls.filter((c) => c[0] === "tabs.remove").map((c) => c[1]);
  out.expirations = local.pendingExpirations.map((e) => [e.tabKey, e.streamer,
    Math.round((e.scheduledAt - local.trackedTabs["4"].openedAt) / MIN)]);
  out.swaps = local.pendingSwaps.map((s) => s.targetTabKey);
  out.lines = lines(/Max tabs \(3\) reached/);
  return out;
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_max_tabs_from_the_config_refresh_to_the_tab_listener(kind, tmp_path):
    """The whole path: fetchConfig stores the list order, Keep Open and the
    live record; the tab listener then applies the limit with them and with
    the pending records in storage. faenilia is the lowest of the streams
    that stay (maachuh is on Keep Open, itsthefluffs and siigynn are
    already waiting to close), so she gets her 30 minutes and nothing
    closes."""
    got = _run(kind, MAX_TABS_LISTENER_CASE, tmp_path)
    assert got["seenKhaos"] is True and got["runStarted"] is True
    assert got["tracked"] == ["1", "2", "3", "4", "5", "6"] and got["removed"] == []
    assert got["expirations"] == [["5", "itsthefluffs", 25], ["4", "faenilia", 30]]
    assert got["swaps"] == ["6"]
    assert got["lines"] == ["Max tabs (3) reached and faenilia is the lowest in list order among the open streams. "
                            "Keeping its tab open for 30m to preserve streak, then closing."]


REARM_CASE = r"""async (ctx) => {
  const lines = (re) => logs.filter((l) => re.test(l)).map((l) => l.replace(/^\[Stream Monitor\] /, ""));
  return {
    swaps: (local.pendingSwaps || []).map((s) => `${s.newTabKey}->${s.targetTabKey}`),
    expirations: (local.pendingExpirations || []).map((e) => [e.tabKey, e.streamer, e.scheduledAt]),
    created: world.alarmsCreated.filter(([name]) => /^pending-/.test(name)).map(([name, info]) => [name, info.when]),
    lines: lines(/Re-armed|stale pending/),
    startedAt: STARTED_AT,
  };
}"""


@pytest.mark.parametrize("kind", ["chrome", "firefox"])
def test_max_tabs_the_init_arms_pending_closes_that_lost_their_alarm(kind, tmp_path):
    """An extension update clears every alarm while the records and the
    tabs stay. The init arms each surviving record that has no alarm (a
    tab waiting to close does not count against the limit, so without its
    alarm it would stay for good), keeps the close of a swap whose new tab
    is gone, and still drops the records of tabs that are gone."""
    now = int(time.time() * 1000)
    minute = 60 * 1000

    def entry(login):
        return {"originalStreamer": login, "raidHopCount": 0, "openedAt": now - 50 * minute, "saveStreak": False}

    def tab(tab_id, login):
        return {"id": tab_id, "windowId": 1, "url": f"https://www.twitch.tv/{login}?sm=1", "active": False,
                "incognito": False}

    logins = {"3": "maachuh", "4": "itsthefluffs", "5": "faenilia", "6": "siigynn", "7": "krytouz"}
    seed = {
        "monitoredStreamers": list(logins.values()),
        "maxTabs": 3,
        "trackedTabs": {key: entry(login) for key, login in logins.items()},
        "pendingExpirations": [
            {"tabKey": "4", "streamer": "itsthefluffs", "scheduledAt": now + 20 * minute},
            {"tabKey": "8", "streamer": "gone", "scheduledAt": now + 5 * minute},
            {"tabKey": "7", "streamer": "krytouz", "scheduledAt": now + 9 * minute},
        ],
        "pendingSwaps": [
            # Overdue by an hour: armed for 30 seconds from now.
            {"newTabKey": "5", "newStreamer": "faenilia", "targetTabKey": "3", "targetStreamer": "maachuh",
             "scheduledAt": now - 60 * minute},
            # Its new tab is gone, its target is still open.
            {"newTabKey": "9", "newStreamer": "gone", "targetTabKey": "6", "targetStreamer": "siigynn",
             "scheduledAt": now + 10 * minute},
        ],
    }
    world = {
        "tabs": {key: tab(int(key), login) for key, login in logins.items()},
        # Tab 7's alarm survived.
        "alarms": {"pending-expire-7": {"name": "pending-expire-7", "scheduledTime": now + 9 * minute}},
    }
    case = REARM_CASE.replace("STARTED_AT", str(now))
    got = _run(kind, case, tmp_path, seed=seed, world=world)
    assert got["swaps"] == ["5->3"]
    assert got["expirations"] == [["4", "itsthefluffs", now + 20 * minute], ["7", "krytouz", now + 9 * minute],
                                  ["6", "siigynn", now + 10 * minute]]
    created = dict(got["created"])
    assert sorted(created) == ["pending-expire-4", "pending-expire-6", "pending-swap-5"]
    assert created["pending-expire-4"] == now + 20 * minute
    assert created["pending-expire-6"] == now + 10 * minute
    assert 25 * 1000 <= created["pending-swap-5"] - now <= 3 * minute
    assert got["lines"] == ["Dropped 1 stale pending swap(s) on startup",
                            "Dropped 1 stale pending expiration(s) on startup",
                            "Re-armed 2 pending close(s) that had no alarm"]


def test_max_tabs_rules_are_the_same_in_both_backgrounds():
    """The list-order rules are one block of code, identical in both
    backgrounds apart from the chrome and browser namespaces, and so are
    the two timers. Max open streams gives 30 minutes; Slot mode's
    deferral of an unplanned close keeps its own 10."""
    def block(text, start, end):
        a = text.index(start)
        return text[a:text.index(end, a)]

    texts = {kind: path.read_text(encoding="utf-8") for kind, path in BACKGROUNDS.items()}
    for start, end in (("// Max open streams: who gives way", "// removeInfo.isWindowClosing tells"),
                       ("async function cancelPendingSwapsForTab(", "// Pending expirations:"),
                       ("function executePendingExpiration(", "async function shouldAutoMute(")):
        chrome = block(texts["chrome"], start, end)
        firefox = block(texts["firefox"], start, end)
        assert firefox.replace("browser.", "chrome.") == chrome
    for text in texts.values():
        assert "function maxTabsDecision(" in text and "function maxTabsDueDecision(" in text
        assert "const GRACE_MINUTES = 30;" in text
        assert "const UNPLANNED_CLOSE_DEFER_MINUTES = 10;" in text
