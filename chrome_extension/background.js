/**
 * Stream Monitor Companion (Chromium)
 * Companion browser extension for the Stream Monitor desktop app.
 * Closes Twitch tabs on raids, keeps background streams playing, and
 * preserves viewer counts via Twitch's own play/unmute controls.
 *
 * Built for Manifest V3 service workers — all state is persisted to
 * chrome.storage.local so it survives service worker termination.
 */

const CONFIG_URL = "http://127.0.0.1:52832/config";
const TWITCH_URL_PATTERN = /^https?:\/\/(?:www\.)?twitch\.tv\/([a-zA-Z0-9_]+)/;
const CONFIG_ALARM = "refresh-config";
const KEEPALIVE_ALARM = "keepalive";
const CONFIG_INTERVAL_MINUTES = 1;
const KEEPALIVE_INTERVAL_MINUTES = 2;
// 2000 entries at ~150 bytes each = ~300 KB stored in browser.storage.local,
// well within the per-extension quota. At ~10 entries/min idle activity that
// covers ~3.5 hours; with bursts of activity (config changes, tab events),
// realistic retention is closer to 30+ hours of meaningful events. Increased
// from 200 because diagnosing a missed-stream event from earlier in the same
// day requires entries from many hours ago to still be present.
const MAX_LOG_ENTRIES = 2000;

// Grace period: a tab that has been open for less than this duration is
// "in grace" and should not be closed by max-tabs displacement. This
// protects the viewer's Twitch view streak/drops eligibility on freshly
// opened streams. When a higher-priority streamer needs the slot and the
// only candidate to displace is still in grace, both tabs stay open
// temporarily and the displacement is scheduled for when grace expires.
const GRACE_MINUTES = 10;
const GRACE_MS = GRACE_MINUTES * 60 * 1000;
const PENDING_SWAP_ALARM_PREFIX = "pending-swap-";
const PENDING_EXPIRE_ALARM_PREFIX = "pending-expire-";

// Load-failure recovery: a tracked tab whose page never actually loaded
// (DNS failure, network drop, "Server Not Found") runs no content script,
// so none of the in-page recovery logic can ever fire. The background
// detects the dead page by pinging the content script and reloads the tab
// until it loads, backing off from 1 minute up to a 5 minute cap between
// attempts. The counter resets as soon as a ping succeeds.
const LOAD_RECOVERY_BASE_DELAY_MS = 60 * 1000;
const LOAD_RECOVERY_MAX_DELAY_MS = 5 * 60 * 1000;

// Streak-rescue rotation (v1.7.0). When the desktop's auto-pause lifts it
// publishes a rescue offer in /config; this extension acknowledges via
// POST /rescue_ack and then owns the whole rotation: open the first
// RESCUE_BATCH_SIZE targets, and every RESCUE_ROTATE_MINUTES close the
// oldest open rescue tab and open the next queued one. When the queue
// drains, a sweep looks for leftover "Save your Streak" UI (sidebar
// entry, bell cards, at-risk store) and feeds anything found back into
// the queue. The session ends when a sweep finds nothing and the last
// slots have finished their turns.
const RESCUE_ROTATE_ALARM = "rescue-rotate";
const RESCUE_BATCH_SIZE = 3;
const RESCUE_ROTATE_MINUTES = 30;
const RESCUE_OPEN_STAGGER_MS = 10000;
const RESCUE_ACK_URL = "http://127.0.0.1:52832/rescue_ack";
// Where the desktop learns which monitored streamers already have a
// Stream Monitor tab open in this browser (v1.10.0). See reportOpenTabs.
const OPEN_TABS_URL = "http://127.0.0.1:52832/open_tabs";
const OPEN_TABS_BROWSER = "chrome";
// After a tracked tab reports status "complete", wait this long before
// verifying the content script is alive. Error pages report "complete"
// too, so this catches a failed open within seconds instead of waiting
// for the next keepalive tick. The delay gives document_idle injection
// time to happen on genuinely loading pages.
const LOAD_VERIFY_AFTER_COMPLETE_MS = 8000;

// Slot mode (1.12.0). The desktop decides which streams hold the stream
// tabs and publishes that as slot_plan in /config; the one browser profile
// the plan names as its executor opens and closes the tabs (applySlotPlan).
// The same values are in the Firefox background.
const SLOT_OPEN_STAGGER_MS = 10000;
const SLOT_PLAN_VERSION = 1;
const SLOT_PLAN_STALE_MS = 300000;
const SLOT_GONE_MAX = 100;
const SLOT_PENDING_OPEN_MAX_AGE_MS = 120000;
const WINDOW_CLOSED_TOMBSTONE_MS = 600000;
const STREAM_WINDOW_MIN_WIDTH = 200;
const STREAM_WINDOW_MIN_HEIGHT = 150;
const STREAM_WINDOW_MAX_COORD = 20000;
const STREAM_WINDOW_MATCH_PX = 8;
const STREAM_WINDOW_PLACE_MAX_FAILURES = 3;
const BELL_CHECK_AFTER_COMPLETE_MS = 12000;
const BELL_CHECK_COVER_MS = 60000;
const MANUAL_SAVE_VISIT_MS = 1800000;
const RESCUE_ACK_HARD_TIMEOUT_MS = 600000;
const STREAK_DEADLINE_SLACK_MS = 3600000;
// A41 release gate: false, because the live check of 2026-10-01 (X09) never
// saw what a save-streak page shows for a streak that can no longer be
// saved. A not_eligible report then only logs; the turn runs its length.
const NOT_ELIGIBLE_CLOSES_TURN = false;
// A plan names Twitch logins; anything else is never opened.
const SLOT_LOGIN_RE = /^[a-z0-9_]{1,25}$/;
// This browser profile's id (8 lowercase hex digits, see getInstanceId).
const INSTANCE_ID_RE = /^[0-9a-f]{8}$/;

// Streak cards (1.12.0): the save window a card's deadline counts from (the
// unverified 24 h belief, AUDIT P3), the units an "N units ago" label can
// have, the verdicts a 1.12 desktop answers a card with (plan 3.5), and the
// window an event's own deadline_at must fall in to count.
const SAVE_WINDOW_HOURS = 24;
const CARD_AGE_UNITS = [1, 60, 3600, 86400];
const STREAK_VERDICTS = new Set(["fresh", "stale", "verify", "duplicate", "saved"]);
const EXPLICIT_DEADLINE_PAST_MS = 3600000;
const EXPLICIT_DEADLINE_FUTURE_MS = 691200000;
// A Streaks at Risk click queued as a rotating turn stays marked until a
// plan generated at least this long after the click lists the streamer in
// neither a slot nor the queue (the item expired or was dropped).
const MANUAL_REQUEST_PLAN_GRACE_MS = 60000;

const IGNORED_PATHS = new Set([
  "directory", "videos", "settings", "subscriptions",
  "inventory", "drops", "wallet", "save-streak",
]);

// Special-case path: Twitch's "save your streak" deep link lives at
// /save-streak/<streamer>. We want tabs opened to this URL to be tracked
// as the streamer's tab (auto-mute, low-quality, player keepalive), so
// extract the streamer from the SECOND path segment instead of the first.
const SAVE_STREAK_URL_PATTERN =
  /^https?:\/\/(?:www\.)?twitch\.tv\/save-streak\/([a-zA-Z0-9_]+)/;

// A VOD page. Twitch moves a save-streak page in place (same document) to a
// VOD, a clip or the channel (A46, the live check of 2026-10-01); a VOD URL
// names no streamer, so see isSaveLanding.
const VOD_URL_PATTERN = /^https?:\/\/(?:www\.)?twitch\.tv\/videos\/(\d+)(?=[/?#]|$)/;

// ---------------------------------------------------------------------------
// Debug logging — writes to console AND a circular buffer in storage
// ---------------------------------------------------------------------------
//
// Concurrent log() calls used to race on the read-modify-write of
// debugLog: two callers could both read the same N-entry baseline,
// each push their own entry, and write back N+1 entries — losing
// one entry permanently. We saw this drop the IIFE's "Event page
// starting (init)" entry frequently.
//
// Fix: serialize storage writes through a Promise queue. All log()
// calls within the same service-worker / event-page lifetime go
// through _logQueue in order, so the read-modify-write is atomic
// from each caller's perspective. Cross-suspension races are still
// possible if the page suspends mid-write, but those are rare and
// drop at most one entry per suspension instead of dropping
// continuously under load.

let _logQueue = Promise.resolve();
// Entries waiting to be persisted. Bursts of log() calls coalesce into a
// single storage read-modify-write: the first queued flush drains the
// whole buffer, and the flushes queued behind it become no-ops. Same
// entries persisted in the same order — just one ~300KB array write per
// burst instead of one per entry.
let _pendingLogEntries = [];

async function log(level, ...args) {
  const msg = args.map(a => (typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
  const prefix = `[Stream Monitor]`;
  const ts = new Date().toISOString();

  if (level === "error") console.error(prefix, ...args);
  else if (level === "warn") console.warn(prefix, ...args);
  else console.log(prefix, ...args);

  _pendingLogEntries.push({ ts, level, msg });
  _logQueue = _logQueue.then(async () => {
    if (_pendingLogEntries.length === 0) return; // drained by an earlier flush
    const batch = _pendingLogEntries;
    _pendingLogEntries = [];
    try {
      const result = await chrome.storage.local.get("debugLog");
      const debugLog = result.debugLog || [];
      debugLog.push(...batch);
      if (debugLog.length > MAX_LOG_ENTRIES) {
        debugLog.splice(0, debugLog.length - MAX_LOG_ENTRIES);
      }
      await chrome.storage.local.set({ debugLog });
    } catch (e) {
      console.error(prefix, "Failed to write debug log:", e);
    }
  });
  return _logQueue;
}

// ---------------------------------------------------------------------------
// State helpers — persist trackedTabs and monitoredStreamers to storage
// ---------------------------------------------------------------------------

async function loadState() {
  const result = await chrome.storage.local.get(["trackedTabs", "monitoredStreamers", "pinnedStreamers"]);
  const monitored = Array.isArray(result.monitoredStreamers) ? result.monitoredStreamers : [];
  const pinned = Array.isArray(result.pinnedStreamers) ? result.pinnedStreamers : [];
  return {
    trackedTabs: result.trackedTabs || {},
    monitoredStreamers: new Set(monitored), // fast membership check
    pinnedStreamers: new Set(pinned),       // streamers user marked "Keep Open"
  };
}

async function saveTrackedTabs(trackedTabs) {
  await chrome.storage.local.set({ trackedTabs });
  // The set of open Stream Monitor tabs just changed: tell the desktop,
  // without holding up the caller.
  reportOpenTabs("tabs-changed").catch(() => {});
}

// ---------------------------------------------------------------------------
// Serialized storage writers (1.12.0)
//
// storage.local has no transactions, so every read-modify-write of a key
// that several async paths can hit runs as one step of a promise chain for
// that key, like _logQueue. A step gets the current value, changes it in
// place, and the chain saves it when it changed. Browser calls (mute,
// focus, move, remove) stay outside the steps. Lock order: a trackedTabs
// step may await withSlotState or withTabPlacement; a slotState or
// tabPlacement step never awaits the trackedTabs chain, and a streamWindow
// step awaits no other chain, so no two chains can wait on each other. A
// step never waits on another step of its own chain. The chains are
// in-memory by design: losing them on a recycle costs nothing, the data
// lives in storage.local.
// ---------------------------------------------------------------------------

// A promise chain: run(fn) starts fn once every step queued before it has
// settled and resolves to fn's result. A failed step does not stop it.
function makeChain() {
  let tail = Promise.resolve();
  return (fn) => {
    const run = tail.then(fn);
    tail = run.catch(() => {});
    return run;
  };
}

const trackedTabsChain = makeChain();
const slotStateChain = makeChain();
const streamWindowChain = makeChain();
const tabPlacementChain = makeChain();

// Every write of trackedTabs. fn(trackedTabs) changes the map in place; a
// changed map is saved, which also sends the desktop an open-tabs report
// (not awaited, so no write waits on the POST).
function withTrackedTabs(fn) {
  return trackedTabsChain(async () => {
    const result = await chrome.storage.local.get("trackedTabs");
    const raw = result.trackedTabs;
    const trackedTabs = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const before = JSON.stringify(trackedTabs);
    const out = await fn(trackedTabs);
    if (JSON.stringify(trackedTabs) !== before) await saveTrackedTabs(trackedTabs);
    return out;
  });
}

// slotState (storage.local), the executor's own bookkeeping:
//   {v: 1, appliedSeq, pendingOpens: {login: {slotId, url, at}},
//    gone: [{streamer, reason, at}], deferredLogged: {login: seq},
//    windowClosed: [{streamer, slot, at}], recentGone: {login: {reason, at}}}
// pendingOpens[].at and windowClosed[].at are epoch ms; gone[].at and
// recentGone[].at are epoch seconds (gone is sent to the desktop as is;
// recentGone stays after delivery, see goneHoldsOpen).
function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function normalizeSlotState(raw) {
  const s = isPlainObject(raw) ? raw : {};
  return {
    ...s,
    v: 1,
    appliedSeq: Number.isInteger(s.appliedSeq) && s.appliedSeq >= 0 ? s.appliedSeq : 0,
    pendingOpens: isPlainObject(s.pendingOpens) ? s.pendingOpens : {},
    gone: Array.isArray(s.gone) ? s.gone.filter(isPlainObject) : [],
    deferredLogged: isPlainObject(s.deferredLogged) ? s.deferredLogged : {},
    windowClosed: Array.isArray(s.windowClosed) ? s.windowClosed.filter(isPlainObject) : [],
    recentGone: isPlainObject(s.recentGone) ? s.recentGone : {},
  };
}

async function loadSlotState() {
  const result = await chrome.storage.local.get("slotState");
  return normalizeSlotState(result.slotState);
}

// Every write of slotState. fn(state) changes it in place.
function withSlotState(fn) {
  return slotStateChain(async () => {
    const state = await loadSlotState();
    const before = JSON.stringify(state);
    const out = await fn(state);
    if (JSON.stringify(state) !== before) await chrome.storage.local.set({ slotState: state });
    return out;
  });
}

// Every write of streamWindow. fn(box) gets {value: the stored streamWindow
// or null}; a new box.value replaces it, null removes the key.
function withStreamWindow(fn) {
  return streamWindowChain(async () => {
    const result = await chrome.storage.local.get("streamWindow");
    const box = { value: isPlainObject(result.streamWindow) ? result.streamWindow : null };
    const before = JSON.stringify(box.value);
    const out = await fn(box);
    if (JSON.stringify(box.value === undefined ? null : box.value) !== before) {
      if (isPlainObject(box.value)) await chrome.storage.local.set({ streamWindow: box.value });
      else await chrome.storage.local.remove("streamWindow");
    }
    return out;
  });
}

// Every write of tabPlacement ({tabKey: {placedIn, seenIn, ownerPlaced,
// failures}}). fn(map) changes it in place.
function withTabPlacement(fn) {
  return tabPlacementChain(async () => {
    const result = await chrome.storage.local.get("tabPlacement");
    const map = isPlainObject(result.tabPlacement) ? result.tabPlacement : {};
    const before = JSON.stringify(map);
    const out = await fn(map);
    if (JSON.stringify(map) !== before) await chrome.storage.local.set({ tabPlacement: map });
    return out;
  });
}

async function saveMonitoredStreamers(list) {
  await chrome.storage.local.set({ monitoredStreamers: list });
}

async function savePinnedStreamers(list) {
  await chrome.storage.local.set({ pinnedStreamers: list });
}

async function notifyUser(title, message) {
  // notifications is an optional permission. If the user hasn't granted it
  // (which is the default for fresh installs and for users who auto-updated
  // from 1.4.x), silently skip — no error, no nag. The user can opt in via
  // the popup's "Desktop notifications" toggle.
  let granted = false;
  try {
    granted = await chrome.permissions.contains({ permissions: ["notifications"] });
  } catch (e) {
    await log("warn", "permissions.contains failed:", e?.message || String(e));
    return;
  }
  if (!granted) return;
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icon-96.png"),
      title,
      message,
    });
  } catch (e) {
    await log("warn", "Failed to create notification:", e?.message || String(e));
  }
}

// ---------------------------------------------------------------------------
// Pending swaps — deferred displacement when the target tab is in grace
// ---------------------------------------------------------------------------
//
// When the only displaceable tab is still within its grace period, we keep
// both tabs open (max_tabs + 1 temporarily) and store a "pending swap"
// describing what to close later. A chrome.alarms alarm fires at grace
// expiry and runs executePendingSwap, which verifies both tabs are still
// valid and then closes the target.
//
// pendingSwaps shape:
//   [{ newTabKey, newStreamer, targetTabKey, targetStreamer, scheduledAt }]

async function loadPendingSwaps() {
  const result = await chrome.storage.local.get("pendingSwaps");
  const raw = result.pendingSwaps;
  return Array.isArray(raw) ? raw : [];
}

async function savePendingSwaps(swaps) {
  await chrome.storage.local.set({ pendingSwaps: swaps });
}

function pendingSwapAlarmName(newTabKey) {
  return `${PENDING_SWAP_ALARM_PREFIX}${newTabKey}`;
}

async function schedulePendingSwap(swap) {
  const swaps = await loadPendingSwaps();
  // If a swap already exists for this newTabKey, replace it (shouldn't
  // happen in normal flow but guards against duplicates on edge replays).
  const filtered = swaps.filter(s => s.newTabKey !== swap.newTabKey);
  filtered.push(swap);
  await savePendingSwaps(filtered);

  // Enforce Chrome's ~30s minimum alarm delay.
  const fireAt = Math.max(swap.scheduledAt, Date.now() + 30000);
  await chrome.alarms.create(pendingSwapAlarmName(swap.newTabKey), { when: fireAt });
  await log("info",
    `Scheduled pending swap: close ${swap.targetStreamer} (tab ${swap.targetTabKey}) at ${new Date(fireAt).toISOString()} to finalize slot for ${swap.newStreamer}`
  );
}

async function cancelPendingSwapsForTab(tabKey) {
  const swaps = await loadPendingSwaps();
  const remaining = [];
  for (const s of swaps) {
    if (s.newTabKey === tabKey || s.targetTabKey === tabKey) {
      await chrome.alarms.clear(pendingSwapAlarmName(s.newTabKey));
      await log("info",
        `Cancelled pending swap (${s.newStreamer} <- ${s.targetStreamer}) because tab ${tabKey} is gone`
      );
    } else {
      remaining.push(s);
    }
  }
  if (remaining.length !== swaps.length) {
    await savePendingSwaps(remaining);
  }
}

async function executePendingSwap(newTabKey) {
  const swaps = await loadPendingSwaps();
  const swap = swaps.find(s => s.newTabKey === newTabKey);
  if (!swap) {
    await log("info", `Pending swap alarm fired for tab ${newTabKey} but no matching swap in storage`);
    return;
  }

  const remaining = swaps.filter(s => s.newTabKey !== newTabKey);
  await savePendingSwaps(remaining);

  // Untracked before the close, so onTabRemoved does not take it for the
  // owner's. A tab with a slot marker belongs to the desktop's plan; a swap
  // recorded before the plan arrived never closes it.
  const outcome = await withTrackedTabs((trackedTabs) => {
    const target = trackedTabs[swap.targetTabKey];
    if (!target || target.originalStreamer !== swap.targetStreamer) return "target-untracked";
    const kept = trackedTabs[swap.newTabKey];
    if (!kept) return "new-untracked";
    if (typeof target.slot === "string" || typeof kept.slot === "string") return "slot";
    delete trackedTabs[swap.targetTabKey];
    return "close";
  });

  if (outcome === "target-untracked") {
    await log("info",
      `Pending swap fired but target ${swap.targetStreamer} (tab ${swap.targetTabKey}) is no longer tracked; slot already free`
    );
    return;
  }

  if (outcome === "new-untracked") {
    await log("info",
      `Pending swap fired but new tab ${swap.newTabKey} (${swap.newStreamer}) is no longer tracked; nothing to preserve`
    );
    return;
  }

  if (outcome === "slot") {
    await log("info",
      `Slot plan: skipped the pending swap for ${swap.targetStreamer} (tab ${swap.targetTabKey}); the plan manages slot tabs`
    );
    return;
  }

  await log("info",
    `Executing pending swap: closing ${swap.targetStreamer} (tab ${swap.targetTabKey}) now that grace has expired; ${swap.newStreamer} keeps its slot`
  );
  notifyUser(
    "Stream Monitor",
    `${swap.targetStreamer}'s ${GRACE_MINUTES}-min streak is safe. Closed their tab so ${swap.newStreamer} keeps the slot.`
  );

  try {
    await chrome.tabs.remove(Number(swap.targetTabKey));
  } catch (e) {
    await log("warn", `Failed to close target tab ${swap.targetTabKey} during pending swap:`, e.message);
  }
}

// ---------------------------------------------------------------------------
// Pending expirations — lowest-priority streamer gets a 10-min viewing
// window before being closed to respect max_tabs
// ---------------------------------------------------------------------------
//
// When a monitored streamer goes live and their rank is the lowest among
// all candidates (including already-open tabs), v1.5.0's behavior was to
// close the new tab immediately. As of v1.5.1.1 we instead let the tab
// live for GRACE_MINUTES so the viewer can still accumulate some Twitch
// view-streak credit. After the window expires, the tab auto-closes.
//
// pendingExpirations shape:
//   [{ tabKey, streamer, scheduledAt }]

async function loadPendingExpirations() {
  const result = await chrome.storage.local.get("pendingExpirations");
  const raw = result.pendingExpirations;
  return Array.isArray(raw) ? raw : [];
}

async function savePendingExpirations(expirations) {
  await chrome.storage.local.set({ pendingExpirations: expirations });
}

function pendingExpireAlarmName(tabKey) {
  return `${PENDING_EXPIRE_ALARM_PREFIX}${tabKey}`;
}

async function schedulePendingExpiration(tabKey, streamer, scheduledAt) {
  const expirations = await loadPendingExpirations();
  const filtered = expirations.filter(e => e.tabKey !== tabKey);
  filtered.push({ tabKey, streamer, scheduledAt });
  await savePendingExpirations(filtered);

  const fireAt = Math.max(scheduledAt, Date.now() + 30000);
  await chrome.alarms.create(pendingExpireAlarmName(tabKey), { when: fireAt });
  await log("info",
    `Scheduled pending expiration: close ${streamer} (tab ${tabKey}) at ${new Date(fireAt).toISOString()}`
  );
}

async function cancelPendingExpirationForTab(tabKey) {
  const expirations = await loadPendingExpirations();
  const remaining = expirations.filter(e => e.tabKey !== tabKey);
  if (remaining.length !== expirations.length) {
    await savePendingExpirations(remaining);
    await chrome.alarms.clear(pendingExpireAlarmName(tabKey));
    await log("info", `Cancelled pending expiration for tab ${tabKey}`);
  }
}

async function executePendingExpiration(tabKey) {
  const expirations = await loadPendingExpirations();
  const exp = expirations.find(e => e.tabKey === tabKey);
  if (!exp) {
    await log("info", `Pending expire alarm fired for tab ${tabKey} but no matching record`);
    return;
  }

  const remaining = expirations.filter(e => e.tabKey !== tabKey);
  await savePendingExpirations(remaining);

  // Untracked before the close; a slot tab is left to the plan (see
  // executePendingSwap).
  const outcome = await withTrackedTabs((trackedTabs) => {
    const tracked = trackedTabs[tabKey];
    if (!tracked || tracked.originalStreamer !== exp.streamer) return "untracked";
    if (typeof tracked.slot === "string") return "slot";
    delete trackedTabs[tabKey];
    return "close";
  });
  if (outcome === "untracked") {
    await log("info",
      `Pending expiration fired but ${exp.streamer} (tab ${tabKey}) is no longer the tracked streamer; skipping`
    );
    return;
  }
  if (outcome === "slot") {
    await log("info",
      `Slot plan: skipped the pending expiration for ${exp.streamer} (tab ${tabKey}); the plan manages slot tabs`
    );
    return;
  }

  await log("info",
    `Pending expiration: closing ${exp.streamer} (tab ${tabKey}) after ${GRACE_MINUTES}m streak grace`
  );
  notifyUser(
    "Stream Monitor",
    `Closed ${exp.streamer} after ${GRACE_MINUTES} minutes. Streak should be preserved.`
  );

  try {
    await chrome.tabs.remove(Number(tabKey));
  } catch (e) {
    await log("warn", `Failed to close tab ${tabKey} during pending expiration:`, e.message);
  }
}

async function shouldAutoMute(streamer) {
  const result = await chrome.storage.local.get(["autoMute", "muteExemptStreamers"]);
  if (!result.autoMute) return false;
  if (streamer) {
    const exempt = new Set(
      (Array.isArray(result.muteExemptStreamers) ? result.muteExemptStreamers : []).map(s =>
        String(s).toLowerCase()
      )
    );
    if (exempt.has(streamer.toLowerCase())) return false;
  }
  return true;
}

async function muteTabIfEnabled(tabId, streamer) {
  if (await shouldAutoMute(streamer)) {
    await chrome.tabs.update(tabId, { muted: true });
    return true;
  }
  return false;
}

async function shouldAutoFocus() {
  // Default ON: if a user has Firefox/Chrome configured to open external
  // tabs in background, the new stream tab can fail to start playing
  // properly and Twitch may not count the viewer, costing the user a
  // streak. Auto-focusing via the extension API works around the
  // browser-level setting. Users who explicitly toggle OFF in the popup
  // get stored false and that's respected.
  const result = await chrome.storage.local.get("autoFocusTabs");
  return result.autoFocusTabs ?? true;
}

async function focusTabIfEnabled(tab) {
  if (!tab || tab.id === undefined) return false;
  if (!(await shouldAutoFocus())) return false;
  try {
    await chrome.tabs.update(tab.id, { active: true });
    if (tab.windowId !== undefined) {
      await chrome.windows.update(tab.windowId, { focused: true });
    }
    return true;
  } catch (e) {
    await log("warn", `Failed to focus tab ${tab.id}:`, e?.message || String(e));
    return false;
  }
}

// ---------------------------------------------------------------------------
// Content script messaging — send commands to Twitch tabs
// ---------------------------------------------------------------------------

async function sendToContentScript(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (e) {
    // Content script may not be loaded yet (e.g., tab still loading)
    await log("warn", `Failed to message tab ${tabId}:`, e.message);
    return null;
  }
}

async function activatePlayerControl(tabId) {
  await sendToContentScript(tabId, { action: "ensurePlaying" });

  const result = await chrome.storage.local.get("lowQuality");
  if (result.lowQuality) {
    await sendToContentScript(tabId, { action: "setLowQuality", enabled: true });
  }
}

// The delayed activation after a tab opens or loads. It re-reads the
// tracked tabs when it fires: an in-place move to a save-streak page or a
// non-channel page fires "complete" a few ms after the URL change, before
// the untrack is saved, and a tab untracked in the meantime is left alone.
function activatePlayerControlSoon(tabId) {
  setTimeout(async () => {
    const { trackedTabs } = await loadState();
    if (trackedTabs[String(tabId)]) activatePlayerControl(tabId);
  }, 3000);
}

// A tab the extension stops tracking but leaves open (a save-streak link
// the viewer followed, a move to a non-channel page) is the viewer's
// again: stop lowering its quality. Quiet when nothing listens (the tab
// left Twitch).
async function releaseLowQuality(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { action: "setLowQuality", enabled: false });
  } catch (e) {
    // No content script on the new page.
  }
}

// ---------------------------------------------------------------------------
// Tab error recovery — reload tabs when content script reports errors
// ---------------------------------------------------------------------------

const tabReloadCooldowns = {}; // { tabId: lastReloadTimestamp }
const RELOAD_COOLDOWN_MS = 60000;

async function handleTabError(tabId) {
  const now = Date.now();
  const lastReload = tabReloadCooldowns[tabId] || 0;
  if (now - lastReload < RELOAD_COOLDOWN_MS) {
    await log("info", `Tab ${tabId} error but reload on cooldown, skipping`);
    return;
  }

  const { trackedTabs } = await loadState();
  if (!trackedTabs[String(tabId)]) return; // Only recover tracked tabs

  tabReloadCooldowns[tabId] = now;
  await log("info", `Tab ${tabId} error detected, reloading`);
  try {
    await chrome.tabs.reload(tabId);
  } catch (e) {
    await log("warn", `Failed to reload tab ${tabId}:`, e.message);
  }
}

// Listen for messages from content scripts
// Channel points bonus claims reported by content scripts (v1.11.0): one
// log line each and a running total for the popup. Serialized through a
// promise chain so two tabs claiming in the same tick cannot both write the
// same total. The chain itself is in-memory by design: losing it on a
// recycle costs nothing, the total lives in storage.local.
let _bonusClaimQueue = Promise.resolve();

function recordBonusClaim(streamer, tabId) {
  _bonusClaimQueue = _bonusClaimQueue
    .then(async () => {
      const result = await chrome.storage.local.get("bonusClaimCount");
      const total = (Number(result.bonusClaimCount) || 0) + 1;
      await chrome.storage.local.set({ bonusClaimCount: total });
      await log("info", `Claimed channel points bonus on ${streamer || "unknown"} (tab ${tabId}); ${total} so far`);
    })
    .catch(() => {});
}

// The streak handlers below run without being awaited; a storage failure
// inside them lands in the debug log instead of an unhandled rejection.
function logStreakStateError(e) {
  log("warn", "Streak state update failed:", e?.message || String(e));
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "tabError" && sender.tab) {
    handleTabError(sender.tab.id);
  } else if (message.action === "reloadTab" && sender.tab) {
    reloadTrackedTab(sender.tab.id);
  } else if (message.type === "streak_event" && message.event) {
    if (message.event.status === "already_saved") {
      handleStreakAlreadySaved(message.event, sender.tab ? sender.tab.id : null).catch(logStreakStateError);
    } else {
      handleStreakCard(message.event).catch(logStreakStateError);
    }
  } else if (message.type === "ack_streak" && message.streamer) {
    // Answer on failure too, so the popup's awaited call always settles.
    acknowledgeAtRiskStreak(message.streamer)
      .then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
    return true; // async response
  } else if (message.type === "dismiss_streak" && message.streamer) {
    dismissAtRiskStreak(message.streamer)
      .then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
    return true;
  } else if (message.type === "clear_acknowledged_streaks") {
    clearAcknowledgedStreaks()
      .then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
    return true;
  } else if (message.type === "bonus_claimed") {
    recordBonusClaim(message.streamer, sender.tab ? sender.tab.id : null);
  } else if (message.type === "slot_status") {
    slotStatus().then(sendResponse,
      () => sendResponse({ planActiveHere: false, executorKey: null, stale: false }));
    return true;
  } else if (message.type === "streak_unparsed") {
    logUnparsedStreakText(sender.tab ? sender.tab.id : null, message.text);
  } else if (message.type === "bell_missing") {
    logBellMissing(sender.tab || null, message.page_url).catch(() => {});
  } else if (message.type === "not_eligible" && sender.tab) {
    handleNotEligible(sender.tab.id, message.streamer).catch(logStreakStateError);
  } else if (message.type === "save_streak_now") {
    saveStreakNow(message.streamer).then(sendResponse,
      () => sendResponse({ ok: false, reason: "open_failed" }));
    return true;
  } else if (message.type === "stream_window_set") {
    setStreamWindow(message.windowId).then(sendResponse,
      () => sendResponse({ ok: false, reason: "missing" }));
    return true;
  } else if (message.type === "stream_window_clear") {
    clearStreamWindow().then(sendResponse, () => sendResponse({ ok: true }));
    return true;
  } else if (message.type === "stream_window_status") {
    streamWindowStatus(message.windowId).then(sendResponse, () => sendResponse({
      set: false, id: null, state: null, bounds: null, existsNow: false, isThisWindow: false,
    }));
    return true;
  }
});

// P8: a text that talks about a streak but matches no card sentence, so a
// Twitch rewording shows up in the debug log instead of as silence.
function logUnparsedStreakText(tabId, text) {
  const short = String(text || "").slice(0, 120);
  if (!short) return;
  log("info", `Unparsed streak text on tab ${tabId}: ${short}`);
}

// B8: the open check found no bell on this page.
async function logBellMissing(tab, pageUrl) {
  const tabId = tab ? tab.id : null;
  let streamer = null;
  if (tabId !== null) {
    const { trackedTabs } = await loadState();
    const entry = trackedTabs[String(tabId)];
    if (entry && entry.originalStreamer) streamer = entry.originalStreamer;
  }
  if (!streamer) streamer = getStreamerFromUrl(pageUrl || (tab && tab.url) || "") || "unknown";
  await log("info", `No notifications bell on tab ${tabId} (${streamer}): logged out, or Twitch changed the bell`);
}

// ---------------------------------------------------------------------------
// Streak event relay: POST to the desktop's /streak_event so it can log,
// raise a tray notification and (1.12.0) queue a save. Best-effort for
// broke and in_danger cards: if the desktop app is not running the post
// fails quietly, the same as the /config fetch on startup.
//
// Resolves {ok, verdict, item, reached}. ok is a 2xx, which is what an
// already_saved report needs before it stops being re-sent. A 1.12 desktop
// answers 200 with {"verdict", "item"} (plan 3.5); verdict is that string
// only when it is one this extension knows, else null, and null also for a
// 1.11 desktop's 204 or a 200 whose body is not JSON: the caller then takes
// the v1.11.2 path. item is true only with a known verdict and "item": true.
// reached is false when no HTTP answer came at all (desktop down, timeout).
// ---------------------------------------------------------------------------

const STREAK_EVENT_URL = "http://127.0.0.1:52832/streak_event";

// The verdict and item of a /streak_event answer body.
function streakAnswer(status, text) {
  let body = null;
  if (status === 200 && typeof text === "string" && text) {
    try {
      body = JSON.parse(text);
    } catch (e) {
      body = null;
    }
  }
  const verdict = isPlainObject(body) && STREAK_VERDICTS.has(body.verdict) ? body.verdict : null;
  return { verdict, item: verdict !== null && body.item === true };
}

async function forwardStreakEvent(event) {
  try {
    const hasPerm = await chrome.permissions.contains({
      origins: ["http://127.0.0.1/*"],
    });
    if (!hasPerm) {
      await log("info", "Streak event detected but host permission not granted; skipping");
      return { ok: false, verdict: null, item: false, reached: false };
    }
    const resp = await fetch(STREAK_EVENT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) {
      await log("warn", `Streak event POST returned HTTP ${resp.status}`);
      return { ok: false, verdict: null, item: false, reached: true };
    }
    // text(), not json(): a 204 has no body, and a body that is not JSON
    // must not throw.
    let text = "";
    try {
      text = await resp.text();
    } catch (e) {
      text = "";
    }
    const answer = streakAnswer(resp.status, text);
    await log(
      "info",
      `Streak event reported: ${event.status} on ${event.streamer} (count=${event.count})` +
      (answer.verdict ? `, verdict ${answer.verdict}${answer.item ? ", save queued" : ""}` : "")
    );
    return { ok: true, verdict: answer.verdict, item: answer.item, reached: true };
  } catch (e) {
    // Desktop app not running, or network blocked. This is expected when
    // the extension is installed standalone without the companion app.
    await log("info", "Streak event POST failed (desktop app likely not running):", e.message);
    return { ok: false, verdict: null, item: false, reached: false };
  }
}

// ---------------------------------------------------------------------------
// Open-tabs report (v1.10.0)
//
// Tells the desktop app which monitored streamers already have a Stream
// Monitor tab open in this browser (every tracked tab, rescue tabs
// included). The desktop keeps the latest report per browser, so when it
// is relaunched (or Stop then Start from the tray) it can leave those
// streams alone instead of opening a second tab for each. Sent after every
// config refresh and whenever the tracked set changes. A desktop older
// than 1.10.0 answers 404; that is logged once and otherwise ignored.
//
// Since 1.12.0 a report also carries this profile's instance id, the plan
// seq it last applied (plan_seq, 0 before any plan: a desktop reads its
// presence as "this extension can run Slot mode"), the slot tabs the owner
// closed or moved since the last delivered report (gone), and busy
// ("paused" while Pause extension holds a plan that runs here).
// ---------------------------------------------------------------------------

let openTabsUnsupportedLogged = false;
// The report being sent now, and the one queued behind it, which every
// caller that asks meanwhile shares. So reports go out one at a time and
// in order. The queued report reads its state only when it starts, so it
// takes the most specific reason asked for while it waited: a
// "tabs-changed" call joins it, a specific reason (plan-applied, paused,
// init, refresh) replaces a queued "tabs-changed", and a second, different
// specific reason gets a report of its own after it. In-memory: a recycle
// drops them, and the next config tick reports again.
let reportInFlight = null;
let reportQueued = null;
let reportQueuedReason = null;

function reportOpenTabs(reason) {
  if (reportQueued) {
    if (reason === reportQueuedReason || reason === "tabs-changed") return reportQueued;
    if (reportQueuedReason === "tabs-changed") {
      reportQueuedReason = reason;
      return reportQueued;
    }
    return reportQueued.then(() => reportOpenTabs(reason));
  }
  if (reportInFlight) {
    reportQueuedReason = reason;
    reportQueued = reportInFlight.then(() => {
      const queuedReason = reportQueuedReason;
      reportQueued = null;
      reportQueuedReason = null;
      return startOpenTabsReport(queuedReason);
    });
    return reportQueued;
  }
  return startOpenTabsReport(reason);
}

function startOpenTabsReport(reason) {
  const run = sendOpenTabsReport(reason);
  reportInFlight = run;
  run.then(() => {
    if (reportInFlight === run) reportInFlight = null;
  });
  return run;
}

// One report. Never rejects.
async function sendOpenTabsReport(reason) {
  try {
    const hasPerm = await chrome.permissions.contains({ origins: ["http://127.0.0.1/*"] });
    if (!hasPerm) return;
    let instance;
    try {
      instance = await getInstanceId();
    } catch (e) {
      await log("warn", "Open-tabs report skipped: no instance id:", e?.message || String(e));
      return;
    }
    const { trackedTabs } = await loadState();
    const streamers = [...new Set(
      Object.values(trackedTabs)
        .map(t => String((t && t.originalStreamer) || "").toLowerCase())
        .filter(Boolean)
    )];
    const slotState = await loadSlotState();
    const gone = slotState.gone.map(g => ({ streamer: g.streamer, reason: g.reason, at: g.at }));
    const settings = await chrome.storage.local.get("extensionPaused");
    const busy = settings.extensionPaused && (await planActiveHere()) ? "paused" : null;
    const resp = await fetch(OPEN_TABS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser: OPEN_TABS_BROWSER,
        instance,
        streamers,
        reason,
        plan_seq: slotState.appliedSeq,
        gone,
        busy,
      }),
      signal: AbortSignal.timeout(5000),
    });
    if (resp.status === 404) {
      if (!openTabsUnsupportedLogged) {
        openTabsUnsupportedLogged = true;
        await log("info", "Desktop app does not accept open-tabs reports yet (older than 1.10.0)");
      }
    } else if (!resp.ok) {
      await log("warn", `Open-tabs report returned HTTP ${resp.status}`);
    } else {
      await log("info",
        `Open-tabs report sent (${reason}): ${streamers.length} streamer(s) with a tab open` +
        (gone.length ? `, ${gone.length} gone` : "") + (busy ? `, busy ${busy}` : "")
      );
      if (gone.length > 0) await afterGoneDelivered(gone);
    }
  } catch (e) {
    // Desktop app not running: expected when the extension runs standalone.
    await log("info", "Open-tabs report failed (desktop app likely not running):", e?.message || String(e));
  }
}

// The desktop took these gone entries (a 2xx): drop exactly them, so any
// appended while the POST was out go with the next report. The desktop
// replans before it answers such a report, so fetch the new plan now.
async function afterGoneDelivered(sent) {
  const same = (a, b) => a.streamer === b.streamer && a.reason === b.reason && a.at === b.at;
  await withSlotState((state) => {
    state.gone = state.gone.filter(g => !sent.some(s => same(s, g)));
  });
  refreshPlanSoon().catch(() => {});
}

// Fetches /config once the report in flight (if any) is done, so the plan
// the desktop made from that report applies within seconds. Single-flight:
// a call while one runs asks for one more fetch after it. Never called
// from inside applySlotPlan.
let planRefreshInFlight = null;
let planRefreshAgain = false;

function refreshPlanSoon() {
  if (planRefreshInFlight) {
    planRefreshAgain = true;
    return planRefreshInFlight;
  }
  planRefreshInFlight = (async () => {
    try {
      do {
        planRefreshAgain = false;
        const pending = reportQueued || reportInFlight;
        if (pending) await pending;
        await fetchConfig();
      } while (planRefreshAgain);
    } catch (e) {
      await log("warn", "Slot plan: refresh failed:", e?.message || String(e));
    } finally {
      planRefreshInFlight = null;
    }
  })();
  return planRefreshInFlight;
}

// ---------------------------------------------------------------------------
// At-risk streak state — persisted across extension restarts so the toolbar
// badge and the popup's "Streaks at Risk" section survive a service-worker
// suspend/resume cycle. Cleared when the user clicks the row (acknowledges)
// or when the entry expires (deadline + 4h buffer).
// ---------------------------------------------------------------------------

const ACK_EXPIRY_BUFFER_MS = 4 * 60 * 60 * 1000; // 4h after deadline -> drop
const STREAK_BADGE_COLOR = "#dc3545";

// Every read-modify-write of atRiskStreaks and savedStreaks runs through
// this chain: the content scripts, the popup and the config tick can all
// write in the same instant, and storage.local has no transactions. A
// check-then-write (a card is only at risk if the streak is not saved)
// must also sit inside one step. In-memory by design: losing the chain on
// a recycle costs nothing, the data lives in storage.local. A step must
// never wait on another withStreakState call, or the chain deadlocks.
let _streakStateQueue = Promise.resolve();

function withStreakState(fn) {
  const run = _streakStateQueue.then(fn);
  // Keep the chain going after a failed step; the caller still sees it.
  _streakStateQueue = run.catch(() => {});
  return run;
}

async function loadAtRiskStreaks() {
  const r = await chrome.storage.local.get("atRiskStreaks");
  return r.atRiskStreaks || {};
}

async function saveAtRiskStreaks(map) {
  await chrome.storage.local.set({ atRiskStreaks: map });
}

// A26: an entry with deadline_at expires ACK_EXPIRY_BUFFER_MS after it;
// one stored before 1.12.0 keeps the v1.11.2 rule.
function _entryExpired(entry, now) {
  if (!entry) return false;
  const deadline = typeof entry.deadline_at === "string" ? Date.parse(entry.deadline_at) : NaN;
  if (!isNaN(deadline)) return now > deadline + ACK_EXPIRY_BUFFER_MS;
  if (!entry.detected_at) return false;
  const detected = Date.parse(entry.detected_at);
  if (isNaN(detected)) return false;
  const deadlineHours = entry.deadline_hours || 24;
  return now - detected > deadlineHours * 3600 * 1000 + ACK_EXPIRY_BUFFER_MS;
}

// Chain step only; callers outside the chain use pruneExpiredAtRiskStreaks.
async function pruneExpiredAtRiskStreaksStep() {
  const map = await loadAtRiskStreaks();
  const now = Date.now();
  let changed = false;
  for (const key of Object.keys(map)) {
    if (_entryExpired(map[key], now)) {
      delete map[key];
      changed = true;
    }
  }
  if (changed) await saveAtRiskStreaks(map);
  return map;
}

function pruneExpiredAtRiskStreaks() {
  return withStreakState(pruneExpiredAtRiskStreaksStep);
}

// The desktop's count rule: a card whose count is above the saved count is
// about a break after the save (the streak grew since). Needs both counts.
function cardOutgrowsSave(card, save) {
  return Number.isInteger(card.count) && Number.isInteger(save.count) && card.count > save.count;
}

// The largest label unit that divides an age in seconds ("just now" is 0
// seconds, read as minutes), the desktop's rule for an age sent without
// its unit.
function inferAgeUnit(ageS) {
  if (ageS === 0) return 60;
  for (const unit of [86400, 3600, 60]) {
    if (ageS % unit === 0) return unit;
  }
  return 1;
}

// The Streaks at Risk row for a broke or in_danger event (plan 3.10). The
// earliest possible posting time is break_at: detected_at less the label's
// age and one unit, since "N units ago" is floored; detected_at when the
// age is unknown (age_unit_s 0). The deadline counts from it, unless the
// event carries its own deadline_at inside the window plan 3.5 allows. A
// link event has no count, and none is ever made up; save_url is always
// built from the login.
function atRiskEntryFromEvent(event, nowMs) {
  const login = String(event.streamer || "").toLowerCase();
  const status = event.status === "in_danger" ? "in_danger" : "broke";
  const count = Number.isInteger(event.count) && event.count >= 0 ? event.count : null;
  let detected = Date.parse(event.detected_at);
  if (isNaN(detected) || detected > nowMs) detected = nowMs;
  const age = Number.isInteger(event.card_age_s) && event.card_age_s >= 0 ? event.card_age_s : null;
  const unit = age === null ? 0
    : (CARD_AGE_UNITS.includes(event.card_age_unit_s) ? event.card_age_unit_s : inferAgeUnit(age));
  const breakAt = age === null ? detected : detected - (age + unit) * 1000;
  const hours = Number.isInteger(event.deadline_hours) && event.deadline_hours >= 0 ? event.deadline_hours : null;
  let deadline = breakAt + (status === "in_danger" && hours !== null ? hours : SAVE_WINDOW_HOURS) * 3600000;
  const explicit = typeof event.deadline_at === "string" ? Date.parse(event.deadline_at) : NaN;
  if (!isNaN(explicit) && explicit >= nowMs - EXPLICIT_DEADLINE_PAST_MS &&
      explicit <= nowMs + EXPLICIT_DEADLINE_FUTURE_MS) {
    deadline = explicit;
  }
  return {
    streamer: login,
    status,
    count,
    detected_at: new Date(detected).toISOString(),
    deadline_hours: hours !== null ? hours : SAVE_WINDOW_HOURS,
    break_at: new Date(breakAt).toISOString(),
    deadline_at: new Date(deadline).toISOString(),
    age_unit_s: unit,
    card_key: `${status}:${login}:${count === null ? "null" : count}`,
    save_url: `https://www.twitch.tv/save-streak/${login}`,
    acknowledged_at: null,
  };
}

// The times of a row, in ms: break_at (detected_at for a row stored before
// 1.12.0), its unit and its deadline.
function atRiskCardTimes(entry) {
  const breakAt = Date.parse(entry.break_at);
  const detected = Date.parse(entry.detected_at);
  const unit = Number.isInteger(entry.age_unit_s) && entry.age_unit_s > 0 ? entry.age_unit_s * 1000 : 0;
  let deadline = Date.parse(entry.deadline_at);
  const start = !isNaN(breakAt) ? breakAt : (!isNaN(detected) ? detected : 0);
  if (isNaN(deadline)) deadline = start + (Number(entry.deadline_hours) || SAVE_WINDOW_HOURS) * 3600000;
  return { breakAt: start, unit, deadline };
}

// Plan 3.5.3 card identity: two readings of one card under one key are the
// same card when their possible posting intervals overlap; either age
// unknown counts as the same (the v1.11.2 rule).
function atRiskCardRelation(record, incoming) {
  if (!record.unit || !incoming.unit) return "same";
  if (incoming.breakAt >= record.breakAt + record.unit) return "newer";
  if (incoming.breakAt + incoming.unit <= record.breakAt) return "older";
  return "same";
}

// A deadline update: the same (or an older) card whose deadline is earlier
// by more than the slack plus the larger unit. Less is label drift.
function atRiskEscalation(record, incoming) {
  return record.deadline - incoming.deadline > STREAK_DEADLINE_SLACK_MS + Math.max(record.unit, incoming.unit);
}

// The Streaks at Risk row after an incoming card (pure; plan 3.10, AUDIT
// S4.2). No row: the card. The same card_key and the same (or an older)
// card: the stored row stays as it is, acknowledged or requested, except
// that an escalated deadline replaces its deadline and re-arms it. Any other
// card: the one with the later break_at wins the whole row (the incoming one
// on a tie), and an incoming winner starts unacknowledged and unrequested,
// so an older in-danger card never overwrites a newer broke row.
function mergeAtRiskEntry(existing, incoming) {
  if (!isPlainObject(incoming)) return isPlainObject(existing) ? existing : null;
  if (!isPlainObject(existing)) return { ...incoming, acknowledged_at: null };
  const record = atRiskCardTimes(existing);
  const card = atRiskCardTimes(incoming);
  if (existing.card_key === incoming.card_key && atRiskCardRelation(record, card) !== "newer") {
    const merged = { ...existing };
    if (atRiskEscalation(record, card)) {
      merged.deadline_at = incoming.deadline_at;
      merged.acknowledged_at = null;
    }
    // That the desktop saw the card is a fact to add, not a value to keep.
    if (merged.desktop_seen_at === undefined && typeof incoming.desktop_seen_at === "number") {
      merged.desktop_seen_at = incoming.desktop_seen_at;
    }
    return merged;
  }
  if (card.breakAt >= record.breakAt) {
    const winner = { ...incoming, acknowledged_at: null };
    delete winner.requested_at;
    delete winner.requested_mode;
    return winner;
  }
  return existing;
}

// Stores a broke or in_danger card in the Streaks at Risk list.
//
// With a desktop verdict (plan 3.5) the desktop decides: "fresh" adds the
// card or merges it into the row; "duplicate" merges into an existing row
// (which re-arms it on an escalated deadline) and never makes one; "stale"
// and "verify" never add. A link event is stored only on "fresh".
//
// Without one (the desktop did not answer, or a 1.11 desktop's 204) the
// v1.11.2 rule stands: a card a save that counts here covers is not stored.
// Resolves to {stored}, and {stored: false, askDesktop} when that save is
// one the desktop knows, so the desktop's view of it is worth fetching
// (handleStreakCard); afterDesktop marks that second look.
//
// seenAt (ms) records that the desktop took the card with a 2xx (see
// saveSettlesRow).
function persistAtRiskStreak(event, { afterDesktop = false, verdict = null, seenAt = null } = {}) {
  if (!event || !event.streamer || !event.status) return Promise.resolve({ stored: false });
  const key = String(event.streamer).toLowerCase();
  return withStreakState(async () => {
    if (verdict === null) {
      if (event.source === "link") return { stored: false };
      const saves = await loadSavedStreaks();
      const save = saves[key];
      if (savedEntryCounts(save, Date.now())) {
        if (cardOutgrowsSave(event, save)) {
          // The streak grew after the save, so the card is about a newer
          // break and the save no longer holds (the desktop ends it too).
          delete saves[key];
          await saveSavedStreaks(saves);
          await log("info",
            `Save for ${key} ended: the ${event.status} card shows a ${event.count}-stream streak, the save a ${save.count}-stream one`
          );
        } else {
          // A stale card: Twitch already confirmed this streak as kept.
          await log("info", afterDesktop
            ? `Ignoring ${event.status} card for ${key}: the desktop still counts the streak as saved`
            : `Ignoring ${event.status} card for ${key}: streak already saved`);
          // A report still owed to the desktop cannot be judged there.
          return { stored: false, askDesktop: !(save.source === "local" && save.pending) };
        }
      }
    } else if (verdict !== "fresh" && verdict !== "duplicate") {
      await log("info", `Not listing ${event.status} card for ${key}: the desktop judged it ${verdict}`);
      return { stored: false };
    }
    const map = await pruneExpiredAtRiskStreaksStep();
    const existing = isPlainObject(map[key]) ? map[key] : null;
    if (verdict === "duplicate" && !existing) return { stored: false };
    if (event.source === "link" && verdict !== "fresh") return { stored: false };
    const incoming = atRiskEntryFromEvent(event, Date.now());
    if (typeof seenAt === "number") incoming.desktop_seen_at = seenAt;
    const merged = mergeAtRiskEntry(existing, incoming);
    if (JSON.stringify(merged) === JSON.stringify(existing)) return { stored: false };
    map[key] = merged;
    await saveAtRiskStreaks(map);
    await refreshStreakBadge(map);
    return { stored: true };
  });
}

// The /config streak_sources this extension last read (a 1.12 desktop
// publishes them): the event sources that desktop takes. None from an
// older desktop.
async function loadStreakSources() {
  const result = await chrome.storage.local.get("streakSources");
  return Array.isArray(result.streakSources) ? result.streakSources : [];
}

// A broke or in_danger card (or a save-streak link) from a content script:
// posted to the desktop, then listed as its verdict says (12.6). A link
// event goes only to a desktop that takes them (A14). With no verdict the
// v1.11.2 path applies: this extension's own saved check, and when a save
// the desktop knows dropped the card, a /config fetched after the desktop
// answered (it ends a save that a higher-count card outgrew or whose
// broadcast has ended) decides.
async function handleStreakCard(event) {
  if (event && event.source === "link" && !(await loadStreakSources()).includes("link")) {
    await log("info", `Save-streak link for ${event.streamer} not sent: the desktop app does not take link events`);
    return;
  }
  const answer = await forwardStreakEvent(event);
  const seenAt = answer.ok ? Date.now() : null;
  if (answer.verdict !== null) {
    await persistAtRiskStreak(event, { afterDesktop: true, verdict: answer.verdict, seenAt });
    return;
  }
  const outcome = await persistAtRiskStreak(event, { seenAt });
  if (!answer.ok) return;
  if (!outcome.stored && outcome.askDesktop && (await refreshSavedStreaksFromDesktop())) {
    await persistAtRiskStreak(event, { afterDesktop: true, seenAt });
  }
}

// Whether a save makes an at-risk row moot. Never when the row's card
// outgrows the save. Otherwise when the card was seen at or before the
// save, or when the desktop saw the card before this list was fetched and
// still lists the save: its verdict that the card is stale. A later card
// the desktop never saw stays, since it may be a break the desktop could
// not see (a broadcast it missed while it was off).
function saveSettlesRow(save, row, fetchStartedAt) {
  if (cardOutgrowsSave(row, save)) return false;
  const rowAt = Date.parse(row.detected_at);
  const saveAt = Date.parse(save.at);
  if (isNaN(rowAt) || isNaN(saveAt) || rowAt <= saveAt) return true;
  return save.source === "desktop" && typeof row.desktop_seen_at === "number" &&
    row.desktop_seen_at < fetchStartedAt;
}

// ---------------------------------------------------------------------------
// Already-saved streaks (v1.11.2)
//
// A save-streak page that reads "You've already maintained your N-stream
// streak with X" means there is nothing to watch: the streak is safe. The
// content script reports it as a streak_event with status "already_saved".
// The desktop owns the rule "saved until X next goes live" and publishes
// the saves that still count in /config as saved_streaks.
//
// savedStreaks (storage.local) maps a login to one of:
//   {at, source: "desktop", seenAt, count?}
//     Listed by the desktop. Counts while /config keeps listing it, and
//     stops SAVED_STREAK_LOCAL_TTL_MS after the last fetch that did, so a
//     desktop that went quiet cannot hide a later break for good. count is
//     kept from this browser's own report of that save; /config has none.
//   {at, source: "local", count, page_url, pending: true}
//     Seen here, not yet acknowledged with a 2xx. Counts for
//     SAVED_STREAK_LOCAL_TTL_MS from the detection (at) and is re-sent with
//     its original payload after every successful /config.
//   {at, source: "local", count, page_url, pending: false, deliveredAt}
//     Acknowledged. The first list fetched after the delivery decides it:
//     absent there means the desktop saw a go-live after the detection.
// ---------------------------------------------------------------------------

const SAVED_STREAK_LOCAL_TTL_MS = 24 * 60 * 60 * 1000;
// The desktop's own login rule; a report it would refuse is dropped here.
const SAVED_STREAK_LOGIN_RE = /^[a-z0-9_]{1,64}$/;

async function loadSavedStreaks() {
  const r = await chrome.storage.local.get("savedStreaks");
  return r.savedStreaks && typeof r.savedStreaks === "object" ? r.savedStreaks : {};
}

async function saveSavedStreaks(map) {
  await chrome.storage.local.set({ savedStreaks: map });
}

function savedEntryCounts(entry, now) {
  if (!entry || typeof entry !== "object") return false;
  if (entry.source === "desktop") {
    return typeof entry.seenAt === "number" && now - entry.seenAt < SAVED_STREAK_LOCAL_TTL_MS;
  }
  const at = Date.parse(entry.at);
  return !isNaN(at) && now - at < SAVED_STREAK_LOCAL_TTL_MS;
}

// Logins whose streak currently counts as saved.
async function savedStreakSet() {
  const map = await loadSavedStreaks();
  const now = Date.now();
  const set = new Set();
  for (const [key, entry] of Object.entries(map)) {
    if (savedEntryCounts(entry, now)) set.add(key);
  }
  return set;
}

async function isStreakSaved(streamer) {
  const key = String(streamer || "").toLowerCase();
  if (!key) return false;
  return (await savedStreakSet()).has(key);
}

// Folds the desktop's saved_streaks into ours (entry shapes above). A
// fetchStartedAt at or before a delivery means this list may predate that
// report, so it cannot judge it yet. A save that counts afterwards also
// clears the unacknowledged at-risk rows it settles (saveSettlesRow): one
// written before the save was known, or a card another browser scraped
// (plan 3.10: how a profile that does not run the plan learns of a save
// another browser saw). An acknowledged row stays acknowledged: an
// already_saved page acknowledges its row, and a later read of the same
// card must not relight it (AUDIT S4.5).
function mergeSavedStreaksFromDesktop(listed, fetchStartedAt) {
  return withStreakState(async () => {
    const now = Date.now();
    const desktop = {};
    for (const [name, at] of Object.entries(listed || {})) {
      const key = String(name).toLowerCase();
      if (SAVED_STREAK_LOGIN_RE.test(key) && typeof at === "string") desktop[key] = at;
    }
    const before = await loadSavedStreaks();
    const next = {};
    for (const [key, entry] of Object.entries(before)) {
      if (!entry || entry.source !== "local" || !savedEntryCounts(entry, now)) continue;
      if (entry.pending) {
        next[key] = entry; // still owed to the desktop
      } else if (desktop[key]) {
        continue; // the desktop's entry below replaces it
      } else if (typeof entry.deliveredAt !== "number" || fetchStartedAt <= entry.deliveredAt) {
        next[key] = entry; // not yet judged by a list fetched after delivery
      }
      // Otherwise delivered and not listed: superseded by a go-live.
    }
    for (const [key, at] of Object.entries(desktop)) {
      if (next[key]) continue;
      const entry = { at, source: "desktop", seenAt: now };
      // The same save this browser reported (same detection time) keeps
      // its count, so the count rule still works while the desktop is down.
      const prev = before[key];
      if (prev && prev.at === at && Number.isInteger(prev.count)) entry.count = prev.count;
      next[key] = entry;
    }
    await saveSavedStreaks(next);

    const had = new Set(Object.keys(before).filter(k => savedEntryCounts(before[k], now)));
    const has = Object.keys(next).filter(k => savedEntryCounts(next[k], now));
    const added = has.filter(k => !had.has(k));
    const removed = [...had].filter(k => !has.includes(k));
    if (added.length > 0 || removed.length > 0) {
      await log("info",
        `Saved streaks from desktop: now ${has.length}` +
        (added.length ? `, added ${added.join(", ")}` : "") +
        (removed.length ? `, no longer saved ${removed.join(", ")}` : "")
      );
    }

    const atRisk = await loadAtRiskStreaks();
    const cleared = has.filter(k => atRisk[k] && !atRisk[k].acknowledged_at &&
      saveSettlesRow(next[k], atRisk[k], fetchStartedAt));
    if (cleared.length > 0) {
      for (const k of cleared) delete atRisk[k];
      await saveAtRiskStreaks(atRisk);
      await refreshStreakBadge(atRisk);
      await log("info", `Cleared at-risk row(s) for ${cleared.join(", ")}: streak already saved`);
    }
  });
}

// The already_saved payload the desktop expects, or null when the report
// has no usable login or count. A detection time in the future (clock
// jumped back) becomes now.
function alreadySavedPayload(event) {
  const streamer = String((event && event.streamer) || "").toLowerCase();
  const count = event ? event.count : undefined;
  if (!SAVED_STREAK_LOGIN_RE.test(streamer) || !Number.isInteger(count) || count < 0) return null;
  const now = Date.now();
  const seen = Date.parse(event.detected_at);
  return {
    status: "already_saved",
    streamer,
    count,
    detected_at: new Date(isNaN(seen) || seen > now ? now : seen).toISOString(),
    page_url: typeof event.page_url === "string" ? event.page_url : "",
  };
}

// The already_saved reports being posted now ("streamer at"), so a re-send
// pass that a /config fetch starts meanwhile does not post them twice.
// In-memory: a recycle drops the POST too, and the pending entry stays.
const savedStreakPostsInFlight = new Set();

// POSTs one already_saved report. A 2xx marks that detection delivered;
// anything else leaves it pending for the next successful /config.
async function deliverSavedStreak(payload) {
  const flight = `${payload.streamer} ${payload.detected_at}`;
  if (savedStreakPostsInFlight.has(flight)) return false;
  savedStreakPostsInFlight.add(flight);
  let ok = false;
  try {
    ({ ok } = await forwardStreakEvent(payload));
  } finally {
    savedStreakPostsInFlight.delete(flight);
  }
  if (!ok) return false;
  await withStreakState(async () => {
    const map = await loadSavedStreaks();
    const entry = map[payload.streamer];
    // A newer detection may have replaced this one while the POST was out.
    if (!entry || entry.source !== "local" || !entry.pending || entry.at !== payload.detected_at) return;
    entry.pending = false;
    entry.deliveredAt = Date.now();
    await saveSavedStreaks(map);
  });
  return true;
}

// Only one re-send pass at a time; concurrent config ticks share it.
// In-memory is enough: a recycle ends the pass anyway, and the pending
// entries it works from live in storage.local.
let savedStreakResendInFlight = null;

function resendPendingSavedStreaks() {
  if (savedStreakResendInFlight) return savedStreakResendInFlight;
  savedStreakResendInFlight = (async () => {
    try {
      const map = await loadSavedStreaks();
      const now = Date.now();
      for (const [streamer, entry] of Object.entries(map)) {
        if (!entry || entry.source !== "local" || !entry.pending) continue;
        if (!savedEntryCounts(entry, now) || !Number.isInteger(entry.count)) continue;
        if (savedStreakPostsInFlight.has(`${streamer} ${entry.at}`)) continue;
        await log("info", `Re-sending already_saved for ${streamer}: the desktop has not acknowledged it yet`);
        await deliverSavedStreak({
          status: "already_saved",
          streamer,
          count: entry.count,
          detected_at: entry.at,
          page_url: typeof entry.page_url === "string" ? entry.page_url : "",
        });
      }
    } finally {
      savedStreakResendInFlight = null;
    }
  })();
  return savedStreakResendInFlight;
}

async function handleStreakAlreadySaved(event, tabId) {
  const payload = alreadySavedPayload(event);
  if (!payload) {
    await log("warn", "Ignoring an already_saved report without a valid login and count");
    return;
  }
  const streamer = payload.streamer;
  await withStreakState(async () => {
    const map = await loadSavedStreaks();
    const save = {
      at: payload.detected_at,
      source: "local",
      count: payload.count,
      page_url: payload.page_url,
      pending: true,
    };
    map[streamer] = save;
    await saveSavedStreaks(map);
    // Twitch says the streak is kept: the Streaks at Risk row is
    // acknowledged, not removed (plan 3.10, AUDIT S4.5), so a later read of
    // the same card keeps it quiet. A row whose card shows a higher count
    // than this page is a newer break and stays unacknowledged. An already
    // acknowledged row keeps its timestamp.
    const atRisk = await loadAtRiskStreaks();
    const row = atRisk[streamer];
    if (isPlainObject(row) && !cardOutgrowsSave(row, save) && !row.acknowledged_at) {
      row.acknowledged_at = new Date().toISOString();
      await saveAtRiskStreaks(atRisk);
      await refreshStreakBadge(atRisk);
    }
  });
  await log("info", `Streak already saved for ${streamer} (${payload.count}-stream); nothing to rescue`);
  // Free the tab first; the POST can take up to its 5 s timeout.
  if (tabId !== null && tabId !== undefined) await endSaveVisit(tabId, streamer, "already_saved");
  await deliverSavedStreak(payload);
  // The desktop replans before it answers (DESIGN 9.4): fetch the plan now,
  // so the next occupant of a freed slot opens within seconds.
  refreshPlanSoon().catch(() => {});
}

// Whether a tracked tab holds a rotating slot on this streamer's save turn
// in the plan: tracked with saveStreak, a slot marker, and that slot's plan
// entry names the streamer with entry "save" (DESIGN 12.3).
function holdsSaveTurn(entry, streamer, plan) {
  if (!entry || !entry.saveStreak || entry.originalStreamer !== streamer || typeof entry.slot !== "string") {
    return false;
  }
  const slot = planSlots(plan).find(s => s.id === entry.slot);
  return !!slot && slotLogin(slot) === streamer && slot.entry === "save";
}

// A save-streak page that ends its visit: already_saved (the streak is
// kept) or not_eligible (nothing to watch). A save turn of a plan that runs
// here ends at once: a gone entry for the desktop, then the close (DESIGN
// 12.3, A16). For already_saved, any other tracked tab stays open and
// tracked while a plan runs here (a Keep Open or live tab the owner moved
// to that page, or a save turn that flipped to live; DESIGN 12.3).
// Otherwise, and for every other not_eligible page, releaseSaveStreakTab
// decides (A16): a page with nothing to watch has nothing to keep open for.
async function endSaveVisit(tabId, streamer, why) {
  const tabKey = String(tabId);
  const plan = await loadSlotPlan();
  const planHere = await planActiveHere(plan);
  const { trackedTabs } = await loadState();
  const tracked = trackedTabs[tabKey];
  if (planHere && tracked && holdsSaveTurn(tracked, streamer, plan)) {
    await appendGone(streamer, why === "not_eligible" ? "not_eligible" : "already_saved");
    await closeTrackedTab(tabKey, streamer, why);
    await log("info", why === "already_saved"
      ? `Slot plan: ${streamer} was already saved; turn ended early`
      : `Slot plan: nothing to watch for ${streamer}; turn ended early`);
    return;
  }
  if (why === "already_saved" && planHere && tracked) {
    await log("info", `Slot plan: tab ${tabKey} stays open for ${tracked.originalStreamer}; it is not ${streamer}'s save turn`);
    return;
  }
  await releaseSaveStreakTab(tabId, streamer, why);
}

// The page has nothing to watch, so a tab Stream Monitor opened as this
// streamer's save-streak page is closed. For a rescue slot that ends its
// turn now instead of after RESCUE_ROTATE_MINUTES (closing the tab frees
// the slot through onTabRemoved). The saveStreak flag is set when tracking
// starts, while the URL still has sm=1 (Twitch strips it soon after). Left
// alone: a rescue slot opened for a live stream (the save only covers the
// broadcasts before the one it is there to watch), a tab the user opened,
// and a tracked channel tab the user navigated to the save-streak page.
// why: "already_saved" (the streak is kept) or "not_eligible" (no stream
// can save it any more).
async function releaseSaveStreakTab(tabId, streamer, why = "already_saved") {
  const tabKey = String(tabId);
  const { trackedTabs } = await loadState();
  const tracked = trackedTabs[tabKey];
  if (!tracked || !tracked.saveStreak || tracked.originalStreamer !== streamer) {
    await log("info", tracked && tracked.rescue && tracked.originalStreamer === streamer
      ? `Rescue: leaving tab ${tabKey} on ${streamer}; its turn is for the live stream`
      : `Leaving tab ${tabKey} open for ${streamer}: Stream Monitor did not open it as that save-streak page`);
    return;
  }
  let tab = null;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    await log("info", `Save-streak tab ${tabKey} for ${streamer} is already closed`);
    return;
  }
  // The path survives the query strip; a tab that has since moved to
  // another page is not closed. A page Twitch itself moved the save-streak
  // page to (the channel with the already-kept modal, a clip, a VOD) is
  // still the save visit (A46).
  const url = (tab && (tab.url || tab.pendingUrl)) || "";
  const m = url.match(SAVE_STREAK_URL_PATTERN);
  if (m ? m[1].toLowerCase() !== streamer : !isKnownSaveLanding(tracked, url)) {
    await log("info", `Leaving tab ${tabKey} open for ${streamer}: it is no longer on the save-streak page`);
    return;
  }
  const session = await loadRescueSession();
  const inRotation = !!(session && session.active &&
    session.slots.some(s => s.tabKey === tabKey && s.streamer === streamer));
  // Untracked first, so onTabRemoved does not take this close for the
  // owner's (it still frees a rescue slot, which it does for any tab).
  const untracked = await withTrackedTabs((trackedTabs) => {
    const entry = trackedTabs[tabKey];
    if (!entry || !entry.saveStreak || entry.originalStreamer !== streamer) return null;
    delete trackedTabs[tabKey];
    return entry;
  });
  if (!untracked) return;
  const saved = why !== "not_eligible";
  try {
    await chrome.tabs.remove(tabId);
    await log("info", inRotation
      ? `Rescue: ${streamer} ${saved ? "was already saved" : "has nothing to watch"}; ended its turn early (tab ${tabKey})`
      : `Closed save-streak tab ${tabKey} for ${streamer}: ${saved ? "already saved" : "nothing to watch"}`);
    // Untracked before the close, so onTabRemoved cannot settle the
    // Streaks at Risk request this tab served (plan 3.10, A15).
    if (untracked.manualSave) await settleManualSaveVisit(untracked);
  } catch (e) {
    // Still open (for example "Tabs cannot be edited right now" during a
    // drag): track it again, so it is not left open and unreported.
    if (!isNoSuchTabError(e)) {
      await withTrackedTabs((trackedTabs) => {
        if (!trackedTabs[tabKey]) trackedTabs[tabKey] = untracked;
      });
    }
    await log("warn", `Could not close save-streak tab ${tabKey}:`, e?.message || String(e));
  }
}

// A16: the content script saw a save-streak page with nothing to watch
// (the "No Content Eligible" heading, no maintained sentence, no video)
// twice, NOT_ELIGIBLE_MIN_GAP_MS apart. The visit ends as for an
// already_saved page, but no save is recorded and nothing is posted: the
// gone entry "not_eligible" tells the desktop in Slot mode. The streamer's
// Streaks at Risk row is acknowledged. With NOT_ELIGIBLE_CLOSES_TURN false
// (the release gate A41 did not confirm the page) it only logs.
async function handleNotEligible(tabId, streamer) {
  const login = String(streamer || "").toLowerCase();
  if (!SLOT_LOGIN_RE.test(login)) return;
  if (!NOT_ELIGIBLE_CLOSES_TURN) {
    await log("info", `Nothing to watch on ${login}'s save-streak page (tab ${tabId}); left as it is`);
    return;
  }
  await log("info", `Nothing to watch on ${login}'s save-streak page (tab ${tabId})`);
  await endSaveVisit(tabId, login, "not_eligible");
  await acknowledgeAtRiskStreak(login);
}

// The error tabs.get and tabs.remove raise for a tab id that no longer
// exists: Chrome "No tab with id: N.", Firefox "Invalid tab ID: N".
function isNoSuchTabError(e) {
  return /no tab with id|invalid tab id/i.test(String((e && e.message) || e));
}

function acknowledgeAtRiskStreak(streamer) {
  if (!streamer) return Promise.resolve();
  const key = streamer.toLowerCase();
  return withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    if (!map[key]) return;
    map[key].acknowledged_at = new Date().toISOString();
    await saveAtRiskStreaks(map);
    await refreshStreakBadge(map);
  });
}

function dismissAtRiskStreak(streamer) {
  if (!streamer) return Promise.resolve();
  const key = streamer.toLowerCase();
  return withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    if (!map[key]) return;
    delete map[key];
    await saveAtRiskStreaks(map);
    await refreshStreakBadge(map);
  });
}

function clearAcknowledgedStreaks() {
  return withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    let changed = false;
    for (const key of Object.keys(map)) {
      if (map[key].acknowledged_at) {
        delete map[key];
        changed = true;
      }
    }
    if (changed) {
      await saveAtRiskStreaks(map);
      await refreshStreakBadge(map);
    }
  });
}

// A Streaks at Risk click (save_streak_now) that opened a tab ("tab") or
// queued the next rotating turn ("slot"): the row shows it until the visit
// ends (plan 3.10, A15).
function markAtRiskRequested(streamer, mode) {
  const key = String(streamer || "").toLowerCase();
  return withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    if (!isPlainObject(map[key])) return;
    map[key].requested_at = Date.now();
    map[key].requested_mode = mode;
    await saveAtRiskStreaks(map);
  });
}

// The request ended without saving anything (the owner closed the tab
// early, a save turn was closed or moved away, the plan dropped the item):
// the row goes back to a plain row, not acknowledged. Only a request of
// that mode is cleared.
function clearAtRiskRequest(streamer, mode) {
  const key = String(streamer || "").toLowerCase();
  return withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    const row = map[key];
    if (!isPlainObject(row) || row.requested_mode !== mode) return;
    delete row.requested_at;
    delete row.requested_mode;
    await saveAtRiskStreaks(map);
    await log("info", `Streaks at Risk: ${key} is no longer marked as opened (${mode})`);
  });
}

// A save-turn slot tab the owner closed or moved away (a user_closed or
// navigated gone entry): a click that asked for that turn is no longer
// pending (plan 3.10).
async function afterSaveTurnLeft(entry) {
  if (!entry || typeof entry.slot !== "string" || !entry.saveStreak) return;
  await clearAtRiskRequest(entry.originalStreamer, "slot");
}

// A manual save tab closed (A15): after MANUAL_SAVE_VISIT_MS it counts as
// visited and its row is acknowledged; an earlier close only clears the
// request.
async function settleManualSaveVisit(entry) {
  const streamer = String(entry.originalStreamer || "").toLowerCase();
  if (!streamer) return;
  if (typeof entry.openedAt === "number" && Date.now() - entry.openedAt >= MANUAL_SAVE_VISIT_MS) {
    await acknowledgeAtRiskStreak(streamer);
    await log("info", `Streaks at Risk: ${streamer}'s save-streak visit is done; row acknowledged`);
  } else {
    await clearAtRiskRequest(streamer, "tab");
  }
}

// The badge counts unacknowledged rows that have not expired (prune on
// read, AUDIT S4.3).
async function refreshStreakBadge(mapOpt) {
  const map = mapOpt || await loadAtRiskStreaks();
  const now = Date.now();
  const unack = Object.values(map).filter((e) => e && !e.acknowledged_at && !_entryExpired(e, now)).length;
  try {
    await chrome.action.setBadgeBackgroundColor({ color: STREAK_BADGE_COLOR });
    await chrome.action.setBadgeText({
      text: unack === 0 ? "" : unack > 9 ? "9+" : String(unack),
    });
  } catch (e) {
    // chrome.action not available in older builds; non-fatal.
  }
}

// Refresh the badge whenever the SW wakes up so it reflects current state
// (the badge is browser UI, separate from storage, and Chrome resets it on
// SW recycle). Also prune expired entries opportunistically.
chrome.runtime.onStartup.addListener(async () => {
  await pruneExpiredAtRiskStreaks();
  await refreshStreakBadge();
  await clearExtensionManagedSoundSettings();
  await checkAndPersistSoundBlockedState();
});
chrome.runtime.onInstalled.addListener(async () => {
  await pruneExpiredAtRiskStreaks();
  await refreshStreakBadge();
  await clearExtensionManagedSoundSettings();
  await checkAndPersistSoundBlockedState();
});
// Also prune + refresh once now, in case the SW just started in response
// to a message and neither onStartup nor onInstalled fired.
pruneExpiredAtRiskStreaks().then(() => refreshStreakBadge()).catch(() => {});
clearExtensionManagedSoundSettings().catch(() => {});
checkAndPersistSoundBlockedState().catch(() => {});

// ---------------------------------------------------------------------------
// Sound permission migration — v1.6.6 set an extension-managed Sound: Allow
// rule for *.twitch.tv via chrome.contentSettings.sound.set(). That was
// intended to override Sound: Block, but it backfired: Chrome's autoplay
// enforcer treats extension-managed permissions as weaker than user-set
// ones, and the result was that even user-set Allow got shadowed by the
// extension-managed entry. Tabs ended up muted by the autoplay policy
// regardless of what the user had configured, and the puzzle-piece icon
// entry in chrome://settings/content/sound couldn't even be removed by
// the user (Chrome enforces extension-managed rules).
//
// v1.6.7 reverses course: we call clear({}) on every startup, which
// removes ANY extension-managed entries we (or any prior version) created.
// Chrome falls back to user-set permissions, which the autoplay enforcer
// fully trusts. If the user has Sound: Block manually, they need to flip
// it to Allow themselves — the auto-fix attempt caused more problems than
// the original Block ever did.
//
// Firefox does not expose browser.contentSettings — guarded so the rest
// of init doesn't blow up.
// ---------------------------------------------------------------------------

async function clearExtensionManagedSoundSettings() {
  if (!chrome.contentSettings || !chrome.contentSettings.sound) {
    return;
  }
  try {
    await chrome.contentSettings.sound.clear({});
    await log(
      "info",
      "Cleared extension-managed sound content settings (v1.6.6 -> v1.6.7 migration)"
    );
  } catch (e) {
    await log(
      "warn",
      "Failed to clear chrome.contentSettings.sound:",
      e && e.message
    );
  }
}

// ---------------------------------------------------------------------------
// Effective Sound permission probe — checks what Chrome actually applies to
// twitch.tv after the v1.6.7 cleanup. If the user has manually set Sound to
// Block (their explicit choice, not anything we did), the extension's
// tab-mute strategy degrades — Twitch audio is suppressed at the site
// level, breaking the player + viewer-count behavior. We don't try to
// override (lesson from v1.6.6) but we DO surface a popup warning so the
// user knows the cure: flip it to Allow in chrome://settings.
// ---------------------------------------------------------------------------

async function checkAndPersistSoundBlockedState() {
  if (!chrome.contentSettings || !chrome.contentSettings.sound) {
    // Firefox or older Chrome — graceful no-op, clear any stale flag.
    try {
      await chrome.storage.local.set({ soundBlocked: false });
    } catch (_) {}
    return;
  }
  try {
    const result = await chrome.contentSettings.sound.get({
      primaryUrl: "https://www.twitch.tv/",
    });
    const blocked = result && result.setting === "block";
    await chrome.storage.local.set({ soundBlocked: !!blocked });
    if (blocked) {
      await log(
        "warn",
        "twitch.tv Sound permission is Block — extension tab-mute will not behave normally. Popup will surface a warning."
      );
    } else {
      await log(
        "info",
        `twitch.tv Sound permission is ${result && result.setting} (not blocked)`
      );
    }
  } catch (e) {
    await log(
      "warn",
      "Failed to probe twitch.tv Sound permission:",
      e && e.message
    );
    // Don't change the persisted flag on probe failure; keep last-known
    // state.
  }
}

// ---------------------------------------------------------------------------
// Reload tracked tab — preserves sm=1 and re-activates player after load
// ---------------------------------------------------------------------------

async function reloadTrackedTab(tabId) {
  const { trackedTabs } = await loadState();
  const tabKey = String(tabId);
  if (!trackedTabs[tabKey]) return;

  const now = Date.now();
  const lastReload = tabReloadCooldowns[tabId] || 0;
  if (now - lastReload < RELOAD_COOLDOWN_MS) {
    await log("info", `Tab ${tabId} reload requested but on cooldown`);
    return;
  }
  tabReloadCooldowns[tabId] = now;

  const streamer = trackedTabs[tabKey].originalStreamer;
  const url = `https://www.twitch.tv/${streamer}?sm=1`;
  await log("info", `Reloading tracked tab ${tabId} with sm=1: ${url}`);

  try {
    // Navigate to URL with sm=1 preserved (Twitch SPA may strip it on reload)
    await chrome.tabs.update(tabId, { url });
  } catch (e) {
    await log("warn", `Failed to reload tab ${tabId}:`, e.message);
  }
}

// ---------------------------------------------------------------------------
// Load-failure recovery: reload tracked tabs whose page never loaded
// ---------------------------------------------------------------------------
//
// "Server Not Found" and similar browser error pages run no content
// scripts, so the in-page recovery (play/unmute clicks, error-overlay
// reload) never gets a chance. From the background the signature is
// simple: the tab exists, its load state is settled, and a message to
// the content script has no receiver. When that happens we reload the
// tab, backing off from 1 to 5 minutes between attempts, indefinitely:
// the first reload after the network comes back loads normally, the
// ping starts answering, and the failure counter resets. Discarded
// (memory-unloaded) tabs have no content script either, so this also
// revives stream tabs the browser quietly put to sleep.
//
// State lives in storage (loadRecovery: { [tabKey]: { failures,
// nextRetryAt, loadingStrikes } }) so service-worker recycles keep the
// backoff. Entries are pruned when the tab closes, is untracked, or
// starts answering pings. Only tracked (sm=1) tabs are ever touched.

async function loadLoadRecovery() {
  const result = await chrome.storage.local.get("loadRecovery");
  const raw = result.loadRecovery;
  return raw && typeof raw === "object" ? raw : {};
}

async function saveLoadRecovery(map) {
  await chrome.storage.local.set({ loadRecovery: map });
}

async function clearLoadRecoveryForTab(tabKey) {
  const map = await loadLoadRecovery();
  if (map[tabKey]) {
    delete map[tabKey];
    await saveLoadRecovery(map);
  }
}

async function pingContentScript(tabId) {
  // Quiet by design: during a long outage this runs on every keepalive
  // tick for every tracked tab, and routing through sendToContentScript
  // would write a warn line each time and flood the debug log.
  try {
    const resp = await chrome.tabs.sendMessage(tabId, { action: "getStatus" });
    return resp !== undefined && resp !== null;
  } catch (e) {
    return false;
  }
}

// Returns true when the tab's content script is alive, false otherwise
// (dead page, still loading, or tab gone). Reloads the tab when the dead
// state is confirmed and the backoff window allows it.
async function checkTrackedTabLoaded(tabKey, streamer) {
  const tabId = Number(tabKey);
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return false; // tab is gone; onTabRemoved handles cleanup
  }

  const alive = await pingContentScript(tabId);
  const map = await loadLoadRecovery();
  const rec = map[tabKey] || { failures: 0, nextRetryAt: 0, loadingStrikes: 0 };

  if (alive) {
    if (rec.failures > 0 || rec.loadingStrikes > 0) {
      await log("info",
        `Load recovery: tab ${tabKey} (${streamer}) is answering again after ${rec.failures} reload attempt(s)`
      );
      delete map[tabKey];
      await saveLoadRecovery(map);
    }
    return true;
  }

  // No content script answered. A page that is legitimately still loading
  // also has no receiver until document_idle, so give an in-flight load
  // one full keepalive cycle before treating it as wedged.
  if (tab.status === "loading" && !tab.discarded) {
    rec.loadingStrikes = (rec.loadingStrikes || 0) + 1;
    map[tabKey] = rec;
    await saveLoadRecovery(map);
    if (rec.loadingStrikes < 2) return false;
  }

  const now = Date.now();
  if (now < rec.nextRetryAt) {
    map[tabKey] = rec;
    await saveLoadRecovery(map);
    return false; // backing off; retry on a later tick
  }

  rec.failures += 1;
  const backoff = Math.min(
    LOAD_RECOVERY_BASE_DELAY_MS * Math.pow(2, rec.failures - 1),
    LOAD_RECOVERY_MAX_DELAY_MS
  );
  rec.nextRetryAt = now + backoff;
  rec.loadingStrikes = 0;
  map[tabKey] = rec;
  await saveLoadRecovery(map);

  await log("warn",
    `Load recovery: tab ${tabKey} (${streamer}) has no content script ` +
    `(status=${tab.status}, discarded=${!!tab.discarded}), page likely never loaded ` +
    `(server not found / network drop / discarded). Reloading (attempt ${rec.failures}, ` +
    `next retry in ${Math.round(backoff / 1000)}s if it fails again).`
  );
  try {
    // Plain reload keeps the tab's own URL (including a save-streak deep
    // link and its sm=1 param). On a never-loaded page the URL is exactly
    // what the desktop app opened, so there is no SPA-stripped-param
    // concern here.
    await chrome.tabs.reload(tabId);
  } catch (e) {
    await log("warn", `Load recovery: reload of tab ${tabId} failed:`, e.message);
  }
  return false;
}

// ---------------------------------------------------------------------------
// Streak-rescue rotation
// ---------------------------------------------------------------------------
//
// Session shape (storage key rescueSession):
//   {
//     active: true,
//     sourceIds: [offerId...],      // desktop offers absorbed so far
//     queue: [{streamer, url, kind}], // pending, in priority order
//     slots: [{tabKey, streamer, openedAt}], // open now, FIFO, max 3
//     rescued: [streamer...],       // finished their turn this session
//     pendingOpens: {url: streamer},// opens in flight (race guard)
//     sweepDone: false,             // last sweep found nothing new
//     startedAt: iso,
//   }
//
// The rotation alarm is a one-shot re-armed after every step so a slow
// step can't pile up ticks. All state lives in storage; service-worker
// recycles re-arm the alarm and reconcile slots on startup.
//
// Every read-modify-write of rescueSession runs as one step of this chain.
// The config tick, the rotation alarm, tab events and the startup
// reconcile can all change the session in the same instant, and a writer
// holding an old copy across an await brings back a slot another writer
// just freed. Slow work (the stagger between opens, the sweep's messages
// to every Twitch tab) stays outside the steps, and a step must never wait
// on another withRescueSession call, or the chain deadlocks. In-memory by
// design, like _streakStateQueue.
let _rescueSessionQueue = Promise.resolve();

function withRescueSession(fn) {
  const run = _rescueSessionQueue.then(fn);
  // Keep the chain going after a failed step; the caller still sees it.
  _rescueSessionQueue = run.catch(() => {});
  return run;
}

async function loadRescueSession() {
  const result = await chrome.storage.local.get("rescueSession");
  const raw = result.rescueSession;
  return raw && typeof raw === "object" ? raw : null;
}

async function saveRescueSession(session) {
  await chrome.storage.local.set({ rescueSession: session });
}

async function clearRescueSession() {
  await chrome.storage.local.remove("rescueSession");
  await chrome.alarms.clear(RESCUE_ROTATE_ALARM);
}

async function ensureRescueAlarm() {
  const existing = await chrome.alarms.get(RESCUE_ROTATE_ALARM);
  if (!existing) {
    await chrome.alarms.create(RESCUE_ROTATE_ALARM, { delayInMinutes: RESCUE_ROTATE_MINUTES });
  }
}

// Which rescue open (if any) is this URL? Consulted by the tab-tracking
// paths so a rescue tab is flagged rescue:true even when the tracking
// event fires before openRescueTab's own trackedTabs write lands.
async function rescuePendingStreamerFor(url) {
  if (!url) return null;
  const session = await loadRescueSession();
  if (!session || !session.active || !session.pendingOpens) return null;
  return session.pendingOpens[url] || null;
}

// The rescue handoff is durable (AUDIT S7): a claim {id, offer, at} is
// written to storage.local (rescueClaim) before POST /rescue_ack, and kept
// until the desktop answers. A 204 (any 2xx) completes it: the offer's
// rotation starts. A 409 means the offer is stale (the desktop already fell
// back, or another browser profile claimed it): the claim goes and nothing
// opens. No answer (the desktop down, or its 204 lost on the way) keeps the
// claim, and every config tick sends the ack again until
// RESCUE_ACK_HARD_TIMEOUT_MS after the claim, even when /config no longer
// lists the offer: a desktop that took the lost ack answers the same
// claimant's repeat with 204 for 10 minutes (plan 3.6). Every read and
// write of rescueClaim runs inside a withRescueSession step; the POST runs
// between steps. A plan running here drops the claim (dropRescueClaim).

function validRescueOffer(offer) {
  return isPlainObject(offer) && typeof offer.id === "string" && offer.id !== "" &&
    Array.isArray(offer.candidates);
}

async function loadRescueClaim() {
  const result = await chrome.storage.local.get("rescueClaim");
  const claim = result.rescueClaim;
  return isPlainObject(claim) && typeof claim.id === "string" && typeof claim.at === "number" ? claim : null;
}

// A withRescueSession step: removes the claim when it is still the one
// for id. Resolves whether it did.
async function removeRescueClaimStep(id) {
  const stored = await loadRescueClaim();
  if (!stored || stored.id !== id) return false;
  await chrome.storage.local.remove("rescueClaim");
  return true;
}

function dropRescueClaim() {
  return withRescueSession(async () => {
    const stored = await loadRescueClaim();
    if (!stored) return;
    await removeRescueClaimStep(stored.id);
    await log("info", `Rescue: dropped the claim on offer ${stored.id}; the Slot mode plan runs here`);
  });
}

// POST /rescue_ack for an offer id, as this profile (plan 3.6). Resolves
// "acked" (2xx), "refused" (409) or "failed" (no answer, or any other
// status).
async function postRescueAck(id) {
  const body = { id, browser: OPEN_TABS_BROWSER };
  try {
    body.instance = await getInstanceId();
  } catch (e) {
    // Without an instance the desktop takes the ack but never re-acks it.
  }
  try {
    const resp = await fetch(RESCUE_ACK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    });
    if (resp.ok) return "acked";
    if (resp.status === 409) return "refused";
    await log("warn", `Rescue ack for offer ${id} returned HTTP ${resp.status}`);
    return "failed";
  } catch (e) {
    await log("warn", "Rescue ack POST failed:", e?.message || String(e));
    return "failed";
  }
}

async function maybeStartRescueFromConfig(rescueOffer, retried = false) {
  const offer = validRescueOffer(rescueOffer) ? rescueOffer : null;
  // The claim to act on: a stored one inside its window first, else a new
  // claim on this offer (unless its rotation already absorbed it).
  const claim = await withRescueSession(async () => {
    const stored = await loadRescueClaim();
    if (stored) {
      if (Date.now() - stored.at < RESCUE_ACK_HARD_TIMEOUT_MS && validRescueOffer(stored.offer)) return stored;
      await removeRescueClaimStep(stored.id);
      await log("info", `Rescue: gave up on offer ${stored.id}; the desktop never answered its ack`);
    }
    if (!offer) return null;
    const session = await loadRescueSession();
    if (session && Array.isArray(session.sourceIds) && session.sourceIds.includes(offer.id)) return null;
    const fresh = { id: offer.id, offer, at: Date.now() };
    await chrome.storage.local.set({ rescueClaim: fresh });
    return fresh;
  });
  if (!claim) return;

  const answer = await postRescueAck(claim.id);
  if (answer === "failed") {
    await log("info", `Rescue: offer ${claim.id} is claimed but not acknowledged yet; the next config tick asks again`);
    return;
  }
  if (answer === "refused") {
    const removed = await withRescueSession(() => removeRescueClaimStep(claim.id));
    if (removed) await log("info", `Rescue offer ${claim.id}: the desktop no longer offers it (409); nothing opened`);
  } else {
    await startRescueFromClaim(claim);
  }
  // A stored claim for an older offer is settled; the offer /config lists
  // now gets its own claim in the same tick.
  if (!retried && offer && offer.id !== claim.id) await maybeStartRescueFromConfig(offer, true);
}

// The ack was taken: the claimed offer's candidates join the rotation, and
// the claim goes in the same step, after the session is saved (a worker
// that dies between the two re-acks the claim, and sourceIds makes that a
// no-op).
async function startRescueFromClaim(claim) {
  const rescueOffer = claim.offer;
  const raw = rescueOffer.candidates
    .map(c => ({
      streamer: String(c.streamer || "").toLowerCase(),
      url: c.url,
      kind: c.kind === "ended" ? "ended" : "live",
    }))
    .filter(e => e.streamer && typeof e.url === "string");
  // The same streamer can arrive twice (ended during the pause, then live
  // again by the time the pause lifted). Watching the live stream saves
  // the streak, so the live entry wins; exact repeats collapse too.
  const liveNow = new Set(raw.filter(e => e.kind === "live").map(e => e.streamer));
  // An ended stream whose streak Twitch already confirmed as kept needs no
  // turn (an older desktop does not filter them). Live entries stay:
  // watching a live stream is never wasted.
  const saved = await savedStreakSet();
  const skipped = [];
  const seen = new Set();
  const entries = [];
  for (const e of raw) {
    if (e.kind === "ended" && liveNow.has(e.streamer)) continue;
    if (e.kind === "ended" && saved.has(e.streamer)) {
      if (!skipped.includes(e.streamer)) skipped.push(e.streamer);
      continue;
    }
    if (seen.has(e.streamer)) continue;
    seen.add(e.streamer);
    entries.push(e);
  }
  if (skipped.length > 0) {
    await log("info", `Rescue offer ${rescueOffer.id}: skipped ${skipped.join(", ")}, streak already saved`);
  }

  const session = await withRescueSession(async () => {
    // Settled meanwhile: a concurrent config tick absorbed it, or a plan
    // that runs here dropped the claim.
    const stored = await loadRescueClaim();
    if (!stored || stored.id !== claim.id) return null;
    let s = await loadRescueSession();
    if (s && s.active) {
      // Absorbed by a concurrent config tick while the ack was out.
      if (Array.isArray(s.sourceIds) && s.sourceIds.includes(rescueOffer.id)) {
        await removeRescueClaimStep(claim.id);
        return null;
      }
      // A second offer arrived mid-session (the user went live again and
      // ended again). Merge new candidates, dedup against everything the
      // session already knows about.
      s.sourceIds.push(rescueOffer.id);
      const known = new Set([
        ...s.queue.map(e => e.streamer),
        ...s.slots.map(slot => slot.streamer),
        ...s.rescued,
      ]);
      for (const e of entries) {
        if (!known.has(e.streamer)) s.queue.push(e);
      }
      s.sweepDone = false; // new material, sweep again at next drain
    } else if (entries.length === 0) {
      await removeRescueClaimStep(claim.id);
      await log("info", `Rescue offer ${rescueOffer.id}: nothing left to rotate`);
      return null;
    } else {
      s = {
        active: true,
        sourceIds: [rescueOffer.id],
        queue: entries,
        slots: [],
        rescued: [],
        pendingOpens: {},
        sweepDone: false,
        startedAt: new Date().toISOString(),
      };
    }
    await saveRescueSession(s);
    await removeRescueClaimStep(claim.id);
    return s;
  });
  if (!session) return;
  const total = session.queue.length + session.slots.length;
  await log("info",
    `Rescue session: absorbed offer ${rescueOffer.id} (${entries.length} candidate(s)); queue=${session.queue.length}, slots=${session.slots.length}`
  );
  notifyUser(
    "Stream Monitor",
    `Streak rescue started: rotating ${total} stream(s), ${RESCUE_BATCH_SIZE} at a time, ${RESCUE_ROTATE_MINUTES} min per turn.`
  );
  await topUpRescueSlots();
  await ensureRescueAlarm();
}

// Runs inside a withRescueSession step (openNextRescueSlot), which saves
// the session it changes.
async function openRescueTab(session, entry) {
  let url = entry.url;
  try {
    const u = new URL(url);
    if (u.searchParams.get("sm") !== "1") {
      u.searchParams.set("sm", "1");
      url = u.toString();
    }
  } catch {
    // keep url as-is
  }
  session.pendingOpens = session.pendingOpens || {};
  session.pendingOpens[url] = entry.streamer;
  await saveRescueSession(session);

  let tab = null;
  let target = null;
  try {
    // Honor the auto-focus setting, same as every other stream open. A
    // focused tab is what reliably gets the player started and the view
    // counted, and that matters most here: the whole point of a rescue
    // tab is to earn streak credit. Auto-focus off keeps background opens.
    // targetWindowForOpen gives that flag, the stream window when one is
    // set, and keeps the tab out of private windows.
    target = await targetWindowForOpen(url);
    tab = target.createdTab || await chrome.tabs.create(tabCreateOptions(url, target));
  } catch (e) {
    await log("warn", `Rescue: failed to open tab for ${entry.streamer}:`, e?.message || String(e));
    delete session.pendingOpens[url];
    await saveRescueSession(session);
    return false;
  }
  await finishCreatedTab(tab, target);

  const tabKey = String(tab.id);
  // Track directly with the rescue flag. Rescue targets may not be on the
  // monitored list at all (bell/sidebar finds), so URL-based tracking
  // would skip them; and the flag exempts the tab from max-tabs
  // displacement (its lifecycle belongs to the rotation).
  await withTrackedTabs((trackedTabs) => {
    const existing = trackedTabs[tabKey] || {};
    trackedTabs[tabKey] = {
      originalStreamer: entry.streamer,
      raidHopCount: existing.raidHopCount || 0,
      openedAt: Date.now(),
      rescue: true,
      saveStreak: SAVE_STREAK_URL_PATTERN.test(url),
    };
  });
  delete session.pendingOpens[url];
  session.slots.push({ tabKey, streamer: entry.streamer, openedAt: Date.now() });
  await muteTabIfEnabled(tab.id, entry.streamer);
  activatePlayerControlSoon(tab.id);
  await log("info",
    `Rescue: opened ${entry.streamer} (${entry.kind}) in tab ${tabKey} (slot ${session.slots.length}/${RESCUE_BATCH_SIZE})`
  );
  return true;
}

// Only one top-up loop may run at a time. The config alarm and an
// event-page wake can fire in the same instant (both fetch config and
// both reach here); two concurrent loops would open every candidate at
// once instead of RESCUE_BATCH_SIZE on a timer. Concurrent callers
// coalesce onto the in-flight loop.
let rescueTopUpInFlight = null;

async function topUpRescueSlots() {
  if (rescueTopUpInFlight) return rescueTopUpInFlight;
  rescueTopUpInFlight = (async () => {
    try {
      await topUpRescueSlotsLoop();
    } finally {
      rescueTopUpInFlight = null;
    }
  })();
  return rescueTopUpInFlight;
}

async function topUpRescueSlotsLoop() {
  for (;;) {
    const step = await openNextRescueSlot();
    if (!step || !step.more) return;
    if (step.tried) {
      // Stagger consecutive opens so the players start cleanly. The wait
      // is outside the chain, so tab events and the rotation go on
      // meanwhile. If the service worker dies mid-stagger, the next
      // config tick's top-up resumes where this left off.
      await new Promise(r => setTimeout(r, RESCUE_OPEN_STAGGER_MS));
    }
  }
}

// One chain step: takes the next queued entry and opens it, or drops it
// when its streak is already saved. Resolves to null when there is nothing
// to do (no active session, no free slot, or an empty queue), otherwise to
// {tried, more}: whether a tab open was attempted, and whether another
// entry is due.
function openNextRescueSlot() {
  return withRescueSession(async () => {
    const session = await loadRescueSession();
    if (!session || !session.active) return null;
    if (session.slots.length >= RESCUE_BATCH_SIZE || session.queue.length === 0) return null;
    const entry = session.queue.shift();
    let tried = false;
    // Saved after it was queued (for example its bell card was opened by
    // hand): drop the ended entry instead of opening a page with nothing
    // to watch. Live entries still open.
    if (entry.kind === "ended" && (await isStreakSaved(entry.streamer))) {
      await log("info", `Rescue: skipped ${entry.streamer}, streak already saved`);
    } else {
      await openRescueTab(session, entry);
      tried = true;
    }
    await saveRescueSession(session);
    return { tried, more: session.slots.length < RESCUE_BATCH_SIZE && session.queue.length > 0 };
  });
}

// Sweep for leftover streak-rescue targets: the sidebar "Save your
// Streak" entry, /save-streak/ links anywhere in open Twitch tabs, and
// the extension's own bell-scraped at-risk store. Skipped: expired rows,
// links whose card is a day old or more (the P3 gate; a link of unknown age
// stays), and streamers that are live with a tracked tab (they are being
// watched; an offline tracked tab lingers and does not count). A desktop
// that saves broken streaks itself (autoSaveStreaks) already has every card
// and link as an event, so then the sweep takes none (AUDIT S1, A28); the
// rotation still serves its queue.
async function sweepForSaveStreakTargets(session) {
  const settings = await chrome.storage.local.get(["autoSaveStreaks", "liveStreamers"]);
  if (settings.autoSaveStreaks === true) {
    await log("info", "Rescue sweep: the desktop app saves broken streaks itself; nothing taken from the at-risk list or page links");
    return [];
  }
  const known = new Set([
    ...session.queue.map(e => e.streamer),
    ...session.slots.map(s => s.streamer),
    ...session.rescued,
  ]);
  // Streaks Twitch already confirmed as kept are not targets either.
  for (const name of await savedStreakSet()) known.add(name);
  const live = new Set((Array.isArray(settings.liveStreamers) ? settings.liveStreamers : [])
    .map(s => String(s).toLowerCase()));
  const { trackedTabs } = await loadState();
  const withTab = new Set(Object.values(trackedTabs)
    .map(t => String((t && t.originalStreamer) || "").toLowerCase()).filter(Boolean));
  const watchedNow = (s) => live.has(s) && withTab.has(s);
  const found = new Map();
  const add = (login) => {
    found.set(login, { streamer: login, url: `https://www.twitch.tv/save-streak/${login}`, kind: "ended" });
  };

  try {
    const map = await loadAtRiskStreaks();
    const now = Date.now();
    for (const e of Object.values(map)) {
      if (!e || !e.streamer || e.acknowledged_at || _entryExpired(e, now)) continue;
      const login = String(e.streamer).toLowerCase();
      if (known.has(login) || found.has(login) || watchedNow(login)) continue;
      add(login);
    }
  } catch (e) {
    await log("warn", "Rescue sweep: at-risk store read failed:", e?.message || String(e));
  }

  try {
    const tabs = await chrome.tabs.query({ url: "*://*.twitch.tv/*" });
    for (const tab of tabs) {
      let resp = null;
      try {
        resp = await chrome.tabs.sendMessage(tab.id, { action: "scanSaveStreak" });
      } catch {
        continue; // dead page or no content script; load recovery handles it
      }
      if (!resp) continue;
      const links = Array.isArray(resp.links)
        ? resp.links
        : (Array.isArray(resp.slugs) ? resp.slugs.map(login => ({ login, card_age_s: null })) : []);
      for (const link of links) {
        const s = String((link && link.login) || "").toLowerCase();
        if (!SLOT_LOGIN_RE.test(s) || known.has(s) || found.has(s) || watchedNow(s)) continue;
        const age = link.card_age_s;
        if (Number.isInteger(age) && age >= SAVE_WINDOW_HOURS * 3600) continue;
        add(s);
      }
    }
  } catch (e) {
    await log("warn", "Rescue sweep: tab scan failed:", e?.message || String(e));
  }

  return Array.from(found.values());
}

async function rotateRescue() {
  const snapshot = await loadRescueSession();
  if (!snapshot || !snapshot.active) {
    await chrome.alarms.clear(RESCUE_ROTATE_ALARM);
    return;
  }

  // Queue drained: sweep for stragglers before winding down. The sweep
  // messages every Twitch tab, so it runs on a snapshot outside the chain;
  // the step below merges its finds into the session as it is by then.
  const swept = snapshot.queue.length === 0 && !snapshot.sweepDone
    ? await sweepForSaveStreakTargets(snapshot)
    : null;

  const turn = await withRescueSession(async () => {
    const session = await loadRescueSession();
    if (!session || !session.active) return null;
    let found = null;
    if (swept) {
      const known = new Set([
        ...session.queue.map(e => e.streamer),
        ...session.slots.map(s => s.streamer),
        ...session.rescued,
      ]);
      const fresh = swept.filter(e => !known.has(e.streamer));
      if (fresh.length > 0) {
        session.queue.push(...fresh);
        found = fresh.length;
      } else if (session.queue.length === 0) {
        session.sweepDone = true;
        found = 0;
      }
    }
    // The oldest slot's turn is over.
    const oldest = session.slots.shift() || null;
    if (oldest) session.rescued.push(oldest.streamer);
    await saveRescueSession(session);
    return { found, oldest };
  });
  if (!turn) return;

  if (turn.found > 0) {
    await log("info", `Rescue sweep found ${turn.found} additional streak target(s)`);
    notifyUser(
      "Stream Monitor",
      `Streak sweep found ${turn.found} more stream(s) to rescue; continuing the rotation.`
    );
  } else if (turn.found === 0) {
    await log("info", "Rescue sweep found nothing further; winding down");
  }
  if (turn.oldest) {
    // Already out of the slots, so the onTabRemoved this causes finds
    // nothing to free.
    try {
      await chrome.tabs.remove(Number(turn.oldest.tabKey));
      await log("info", `Rescue: closed ${turn.oldest.streamer} (tab ${turn.oldest.tabKey}) after its rotation turn`);
    } catch (e) {
      await log("warn", `Rescue: failed to close tab ${turn.oldest.tabKey}:`, e?.message || String(e));
    }
    // A turn that ran its full length settles the streamer's Streaks at
    // Risk row (AUDIT S4.5); a tab closed early does not (handleRescueTabGone).
    try {
      await acknowledgeAtRiskStreak(turn.oldest.streamer);
    } catch (e) {
      logStreakStateError(e);
    }
  }

  await topUpRescueSlots();

  const watched = await endRescueSessionIfDone(true);
  if (watched !== null) {
    await log("info", `Rescue session complete: ${watched} stream(s) watched`);
    notifyUser("Stream Monitor", `Streak rescue complete: watched ${watched} stream(s).`);
  }
}

// A chain step: ends the session once nothing is open, queued or left to
// sweep, and resolves to the number of streams watched; otherwise null,
// after re-arming the one-shot rotation alarm when rearm is set.
function endRescueSessionIfDone(rearm = false) {
  return withRescueSession(async () => {
    const session = await loadRescueSession();
    if (!session || !session.active) return null;
    if (session.slots.length === 0 && session.queue.length === 0 && session.sweepDone) {
      await clearRescueSession();
      return session.rescued.length;
    }
    if (rearm) await chrome.alarms.create(RESCUE_ROTATE_ALARM, { delayInMinutes: RESCUE_ROTATE_MINUTES });
    return null;
  });
}

// A rescue tab vanished outside the rotation (user closed it, raid close,
// navigate-away untrack). Free the slot, count the streamer as done, and
// pull the next target forward.
async function handleRescueTabGone(tabKey) {
  const slot = await withRescueSession(async () => {
    const session = await loadRescueSession();
    if (!session || !session.active) return null;
    const idx = session.slots.findIndex(s => s.tabKey === tabKey);
    if (idx === -1) return null;
    const [gone] = session.slots.splice(idx, 1);
    session.rescued.push(gone.streamer);
    await saveRescueSession(session);
    return gone;
  });
  if (!slot) return;
  await log("info", `Rescue: tab ${tabKey} (${slot.streamer}) closed externally; slot freed`);
  await topUpRescueSlots();
  const watched = await endRescueSessionIfDone();
  if (watched !== null) {
    notifyUser("Stream Monitor", `Streak rescue complete: watched ${watched} stream(s).`);
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function getStreamerFromUrl(url) {
  if (!url) return null;
  // /save-streak/<streamer> deep link first — these tabs should be
  // treated identically to a normal channel tab for the streamer.
  const saveStreakMatch = url.match(SAVE_STREAK_URL_PATTERN);
  if (saveStreakMatch && saveStreakMatch[1]) {
    return saveStreakMatch[1].toLowerCase();
  }
  const match = url.match(TWITCH_URL_PATTERN);
  if (match && match[1]) {
    const username = match[1].toLowerCase();
    if (IGNORED_PATHS.has(username)) return null;
    return username;
  }
  return null;
}

function isStreamMonitorTab(url) {
  try {
    return new URL(url).searchParams.get("sm") === "1";
  } catch {
    return false;
  }
}

// "/videos/<digits>" for a VOD page URL, else null.
function vodPathOf(url) {
  const m = String(url || "").match(VOD_URL_PATTERN);
  return m ? `/videos/${m[1]}` : null;
}

// Whether url is a page Twitch moves this entry's save-streak page to in
// place (A46): a VOD (/videos/<digits>), the streamer's clip
// (/<login>/clip/<slug>), the streamer's videos (/<login>/videos), or, for
// a streak already kept, the channel (/<login>, with a modal on top). Only
// an entry opened as a save-streak page (saveStreak) makes such a visit,
// and the tab stays that visit there.
function isSaveLanding(entry, url) {
  if (!entry || entry.saveStreak !== true) return false;
  if (vodPathOf(url)) return true;
  const login = String(entry.originalStreamer || "").toLowerCase();
  const m = String(url || "").match(/^https?:\/\/(?:www\.)?twitch\.tv\/([a-zA-Z0-9_]+)(\/[^?#]*)?/);
  if (!login || !m || m[1].toLowerCase() !== login) return false;
  const rest = (m[2] || "").replace(/\/+$/, "").toLowerCase();
  return rest === "" || rest === "/videos" || /^\/clip\/[^/]+$/.test(rest);
}

// isSaveLanding for a tab whose move Stream Monitor did not just see: a VOD
// counts only when it is the one the visit moved to (the entry's landing,
// noteSaveLanding). A VOD URL names no streamer, and after a browser
// restart a stale entry's tab id can name the owner's own VOD tab. The
// landing counts whatever saveStreak says: a save turn the plan flipped to
// live keeps it until its tab is sent to the channel
// (sendSaveTurnToChannel), and a close in between must still find the tab.
function isKnownSaveLanding(entry, url) {
  const vod = vodPathOf(url);
  if (!vod) return isSaveLanding(entry, url);
  return !!entry && typeof entry.landing === "string" && entry.landing === vod;
}

// Whether a tab at url still shows what this tracked entry tracks: the
// streamer's channel or save-streak page, or a page its save visit moved to
// (isKnownSaveLanding). Tab ids are unique only within one browser session,
// so DESIGN 8.3 step 0 and the scan after a browser start ask this before
// they trust an entry's tab.
function tabShowsEntry(entry, url) {
  const streamer = String((entry && entry.originalStreamer) || "").toLowerCase();
  return getStreamerFromUrl(url) === streamer || isKnownSaveLanding(entry, url);
}

// ---------------------------------------------------------------------------
// Slot mode executor (1.12.0)
//
// With Slot mode on, the desktop publishes slot_plan in /config: which
// streamer holds each slot (Keep Open slots first, then the rotating
// ones), which tabs to close and why, and which browser profile executes
// it ("<browser>-<instanceId>"). The executing profile opens what the plan
// names and is missing, closes what it lists, and reports back; every
// other profile only shows the plan. There is no alarm of its own: the
// 1-minute config alarm is the heartbeat, and a report that carried gone
// entries fetches the desktop's new plan at once (refreshPlanSoon).
// ---------------------------------------------------------------------------

// This profile's id, memoized per worker. Always read from storage, never
// a bare module variable: tab listeners run before the init IIFE on a
// wake, and an unknown id would make a plan look as if it named another
// executor. A missing or malformed id is minted again.
let instanceIdPromise = null;

function getInstanceId() {
  if (!instanceIdPromise) {
    const pending = (async () => {
      const result = await chrome.storage.local.get("instanceId");
      if (typeof result.instanceId === "string" && INSTANCE_ID_RE.test(result.instanceId)) {
        return result.instanceId;
      }
      const bytes = new Uint8Array(4);
      crypto.getRandomValues(bytes);
      const id = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
      await chrome.storage.local.set({ instanceId: id });
      await log("info", `Minted instance id ${id} for this browser profile`);
      return id;
    })();
    instanceIdPromise = pending;
    // A failed read is tried again on the next call.
    pending.catch(() => {
      if (instanceIdPromise === pending) instanceIdPromise = null;
    });
  }
  return instanceIdPromise;
}

async function myExecutorKey() {
  return `${OPEN_TABS_BROWSER}-${await getInstanceId()}`;
}

// Whether this worker started without the browser-session marker. The
// browser clears storage.session on a restart (an extension reload or
// update clears it too), and tab and window ids from before a restart may
// now name other tabs and windows. Memoized per worker; the init IIFE asks
// first and writes the marker.
let browserSessionPromise = null;

function checkBrowserSession() {
  if (!browserSessionPromise) {
    browserSessionPromise = (async () => {
      try {
        const got = await chrome.storage.session.get("browserSession");
        if (got.browserSession === true) return false;
        await chrome.storage.session.set({ browserSession: true });
        return true;
      } catch (e) {
        await log("warn", "Browser-session marker unavailable:", e?.message || String(e));
        return true;
      }
    })();
  }
  return browserSessionPromise;
}

// Resolved once the init IIFE's tab scan has run (in a finally, so a
// failed init never holds the plan back for good). Plans apply only after
// it, so a browser restart never reads the previous session's dead
// entries as open tabs. initScanFinished is the same fact, readable
// without waiting.
let resolveInitScan = null;
let initScanFinished = false;
const initScanDone = new Promise((resolve) => {
  resolveInitScan = resolve;
});

// After a browser start the tab listeners wait for the init scan: until it
// runs, trackedTabs holds the previous session's entries, whose ids may now
// name other tabs (DESIGN 8.3 step 0, 8.8), and acting on one would send a
// false gone entry, close an unrelated tab, or keep the scan from
// re-adopting the restored copy. Resolves false when the event is older
// than the scan's view of its tab (the tab has moved on since, and a later
// event carries that change); the query and fragment are not compared, so
// Twitch stripping sm=1 does not drop the event that still carries it.
// A worker that only woke up (the marker is there) never waits: its ids
// are valid, and a removal handed to the scan would drop the entry with no
// gone entry, so the desktop would read the owner's close as a silent loss
// and reopen the tab.
async function afterInitScan(tabId, url) {
  if (initScanFinished || !(await checkBrowserSession())) return true;
  await initScanDone;
  if (!url) return true;
  try {
    const tab = await chrome.tabs.get(tabId);
    const current = String(tab.url || tab.pendingUrl || "").split(/[?#]/)[0];
    return current === String(url).split(/[?#]/)[0];
  } catch (e) {
    return false;
  }
}

async function loadSlotPlan() {
  const result = await chrome.storage.local.get("slotPlan");
  return isPlainObject(result.slotPlan) ? result.slotPlan : null;
}

function planSlots(plan) {
  return plan && Array.isArray(plan.slots) ? plan.slots.filter(isPlainObject) : [];
}

function slotLogin(slot) {
  const login = slot && typeof slot.streamer === "string" ? slot.streamer.toLowerCase() : "";
  return SLOT_LOGIN_RE.test(login) ? login : null;
}

function planSeqOf(plan) {
  return plan && Number.isInteger(plan.seq) && plan.seq >= 0 ? plan.seq : 0;
}

// A plan this extension may act on: this version, active (the desktop has
// an executor) and fresh (a desktop that stopped publishing freezes it).
function slotPlanUsable(plan) {
  return isPlainObject(plan) && plan.v === SLOT_PLAN_VERSION && plan.active === true &&
    typeof plan.generated_at === "number" &&
    Date.now() - plan.generated_at * 1000 < SLOT_PLAN_STALE_MS;
}

// planActiveHere(plan), or planActiveHere() for the stored plan: true when
// this browser profile executes it.
async function planActiveHere(plan) {
  const p = plan === undefined ? await loadSlotPlan() : plan;
  if (!slotPlanUsable(p)) return false;
  try {
    return p.executor === (await myExecutorKey());
  } catch (e) {
    return false;
  }
}

// The streamers of the stored plan when it runs here; none otherwise.
async function currentPlanStreamers() {
  const plan = await loadSlotPlan();
  if (!(await planActiveHere(plan))) return new Set();
  return new Set(planSlots(plan).map(slotLogin).filter(Boolean));
}

// The popup's slot_status route (diagnostics; the popup computes its own
// status line from storage).
async function slotStatus() {
  const plan = await loadSlotPlan();
  let executorKey = null;
  try {
    executorKey = await myExecutorKey();
  } catch (e) {
    executorKey = null;
  }
  const stale = !!plan && !(typeof plan.generated_at === "number" &&
    Date.now() - plan.generated_at * 1000 < SLOT_PLAN_STALE_MS);
  return { planActiveHere: await planActiveHere(plan), executorKey, stale };
}

async function isExtensionPaused() {
  const result = await chrome.storage.local.get("extensionPaused");
  return !!result.extensionPaused;
}

function pendingOpenFor(state, login) {
  const pending = login ? state.pendingOpens[login] : null;
  return isPlainObject(pending) && typeof pending.at === "number" &&
    Date.now() - pending.at < SLOT_PENDING_OPEN_MAX_AGE_MS ? pending : null;
}

// The pending open (openPlannedTab, openManualSaveTab) for the streamer
// this URL shows, while it is younger than SLOT_PENDING_OPEN_MAX_AGE_MS.
// Matched by login rather than by the URL string, so the browser rewriting
// the host or the query does not lose it.
async function slotPendingFor(url) {
  const login = getStreamerFromUrl(url);
  if (!login) return null;
  return pendingOpenFor(await loadSlotState(), login);
}

// Records, for the next open-tabs report, that a tracked tab went away in
// a way the desktop must know about while a plan runs here: user_closed,
// navigated, raid, window_closed, already_saved or not_eligible.
function appendGone(streamer, reason) {
  const login = String(streamer || "").toLowerCase();
  if (!login) return Promise.resolve();
  return withSlotState((state) => {
    const at = Math.floor(Date.now() / 1000);
    state.gone.push({ streamer: login, reason, at });
    if (state.gone.length > SLOT_GONE_MAX) state.gone.splice(0, state.gone.length - SLOT_GONE_MAX);
    // Kept after delivery (goneHoldsOpen). An entry older than
    // SLOT_PLAN_STALE_MS can only predate plans the pass refuses anyway.
    for (const [key, g] of Object.entries(state.recentGone)) {
      if (!isPlainObject(g) || typeof g.at !== "number" || Date.now() - g.at * 1000 >= SLOT_PLAN_STALE_MS) {
        delete state.recentGone[key];
      }
    }
    state.recentGone[login] = { reason, at };
  }).then(() => {
    log("info", `Slot plan: ${login} gone (${reason})`);
  });
}

// Whether a plan predates this streamer's last gone entry, so the apply
// pass must not reopen the streamer from it. The desktop replans before it
// answers a report that carries gone entries, but DESIGN 9.4 lets that
// replan miss its wait, and a /config fetched meanwhile (the minute
// alarm, the refresh after a late answer, the init of a background the
// close woke) still holds the plan made before the owner closed or moved
// the tab: reopening from it would bring back a stream the desktop has
// already dismissed (rules 25, 26). The desktop never names such a
// streamer again in the same second, so a plan generated in the gone
// entry's second counts as older. window_closed compares strictly: rule
// 27 reopens a first window close through a plan the desktop publishes
// within that same second.
function goneHoldsOpen(state, login, plan) {
  const g = isPlainObject(state.recentGone) ? state.recentGone[login] : null;
  if (!isPlainObject(g) || typeof g.at !== "number") return false;
  if (!plan || typeof plan.generated_at !== "number") return false;
  return g.reason === "window_closed" ? plan.generated_at < g.at : plan.generated_at <= g.at;
}

// One debug line per streamer and plan seq for a held open. In-memory: a
// recycle costs at most one repeated line.
const goneHoldLogged = {};

async function logGoneHold(state, login, plan) {
  const seq = planSeqOf(plan);
  if (goneHoldLogged[login] === seq) return;
  goneHoldLogged[login] = seq;
  const g = state.recentGone[login];
  await log("info", `Slot plan: not reopening ${login}; the plan predates its gone entry (${g.reason})`);
}

// A tracked tab closed with its window: remembered for
// WINDOW_CLOSED_TOMBSTONE_MS, so the first scan after a browser restart can
// re-adopt the restored copy (Firefox may report the quit as window
// closes).
function addWindowClosedTombstone(streamer, slot) {
  const login = String(streamer || "").toLowerCase();
  if (!login) return Promise.resolve();
  return withSlotState((state) => {
    state.windowClosed.push({ streamer: login, slot: typeof slot === "string" ? slot : null, at: Date.now() });
    if (state.windowClosed.length > SLOT_GONE_MAX) {
      state.windowClosed.splice(0, state.windowClosed.length - SLOT_GONE_MAX);
    }
  });
}

// The trackedTabs entry an adoption writes (onTabCreated, the onTabUpdated
// new-track branch, scanExistingTabs). A pending plan open gives the tab
// its slot marker; a pending manual save open marks a save-streak tab the
// owner asked for, which never holds a slot.
function adoptedTabEntry(streamer, url, { viaRescue = false, pending = null } = {}) {
  const entry = {
    originalStreamer: streamer,
    raidHopCount: 0,
    openedAt: Date.now(),
    saveStreak: SAVE_STREAK_URL_PATTERN.test(url),
  };
  if (viaRescue) entry.rescue = true;
  if (pending && pending.manual === true) {
    entry.saveStreak = true;
    entry.manualSave = true;
  } else if (pending && typeof pending.slotId === "string" && pending.slotId) {
    entry.slot = pending.slotId;
  }
  return entry;
}

// Stops tracking tabKey when it still tracks streamer. With a gone reason
// the entry is recorded in the same step, so the report this save sends
// carries it. Resolves true when it untracked the tab.
function untrackTab(tabKey, streamer, goneReason = null) {
  return withTrackedTabs(async (trackedTabs) => {
    const entry = trackedTabs[tabKey];
    if (!entry || entry.originalStreamer !== streamer) return false;
    if (goneReason) await appendGone(streamer, goneReason);
    delete trackedTabs[tabKey];
    return true;
  });
}

function stripSlotMarkers() {
  return withTrackedTabs((trackedTabs) => {
    let stripped = 0;
    for (const entry of Object.values(trackedTabs)) {
      if (entry && typeof entry.slot === "string") {
        delete entry.slot;
        stripped += 1;
      }
    }
    return stripped;
  });
}

// DESIGN 8.6: a pending swap or expiration recorded while no plan ran here
// (before the conversion, or while a stale plan re-armed Max open
// streams) must never close a slot tab later.
async function cancelLegacyRecordsForSlotTabs(tabKeys) {
  if (tabKeys.length === 0) return;
  const keys = new Set(tabKeys);
  const swaps = await loadPendingSwaps();
  const droppedSwaps = swaps.filter(s => keys.has(s.newTabKey) || keys.has(s.targetTabKey));
  if (droppedSwaps.length > 0) {
    await savePendingSwaps(swaps.filter(s => !droppedSwaps.includes(s)));
    for (const s of droppedSwaps) await chrome.alarms.clear(pendingSwapAlarmName(s.newTabKey));
  }
  const expirations = await loadPendingExpirations();
  const droppedExpirations = expirations.filter(e => keys.has(e.tabKey));
  if (droppedExpirations.length > 0) {
    await savePendingExpirations(expirations.filter(e => !droppedExpirations.includes(e)));
    for (const e of droppedExpirations) await chrome.alarms.clear(pendingExpireAlarmName(e.tabKey));
  }
  if (droppedSwaps.length > 0 || droppedExpirations.length > 0) {
    await log("info",
      `Slot plan: cancelled ${droppedSwaps.length} pending swap(s) and ${droppedExpirations.length} pending expiration(s) for slot tabs`
    );
  }
}

// DESIGN 8.6: a rescue rotation running when a plan first runs here ends.
// Its tabs lose the rescue flag and become extras, which the desktop
// adopts into slots or lists for closing; the desktop re-queues the
// offer's unfinished save-streak entries itself.
async function convertLegacyRescueSession() {
  const session = await withRescueSession(async () => {
    const current = await loadRescueSession();
    if (!current || !current.active) return null;
    await clearRescueSession();
    return current;
  });
  if (!session) return;
  const stripped = await withTrackedTabs((trackedTabs) => {
    let n = 0;
    for (const entry of Object.values(trackedTabs)) {
      if (entry && entry.rescue) {
        delete entry.rescue;
        n += 1;
      }
    }
    return n;
  });
  await log("info",
    `Slot plan: ended the rescue rotation (${session.slots.length} open, ${session.queue.length} queued); ` +
    `${stripped} rescue tab(s) are ordinary tracked tabs now`
  );
}

// DESIGN 8.2 steps 2 and 3 for the plan fetchConfig just stored. Waits
// for the init scan first.
async function followSlotPlan(plan) {
  await initScanDone;
  let here = false;
  try {
    here = await planActiveHere(plan);
  } catch (e) {
    here = false;
  }
  if (here) {
    try {
      await convertLegacyRescueSession();
    } catch (e) {
      await log("warn", "Slot plan: rescue conversion failed:", e?.message || String(e));
    }
    await applySlotPlan(plan);
    return;
  }
  try {
    await releaseSlotMarkers(plan);
  } catch (e) {
    await log("warn", "Slot plan: marker release failed:", e?.message || String(e));
  }
}

// DESIGN 8.2 step 3, for a plan this profile does not execute. A null plan
// (Slot mode off) strips the markers: those tabs become ordinary tracked
// tabs, and Max open streams applies to later arrivals. A fresh, active
// plan naming another executor (compared with a key this profile
// resolved) closes the marker tabs whose streamer it still lists, so the
// new executor's copy is the only one, and strips the rest. A stale or
// inactive plan changes nothing.
async function releaseSlotMarkers(plan) {
  const { trackedTabs } = await loadState();
  const marked = Object.entries(trackedTabs).filter(([, e]) => e && typeof e.slot === "string");
  if (marked.length === 0) return;
  if (plan === null) {
    const stripped = await stripSlotMarkers();
    if (stripped > 0) {
      await log("info", `Slot plan: Slot mode is off; ${stripped} slot tab(s) are ordinary tracked tabs again`);
    }
    return;
  }
  if (!slotPlanUsable(plan) || typeof plan.executor !== "string") return;
  let mine;
  try {
    mine = await myExecutorKey();
  } catch (e) {
    return;
  }
  if (plan.executor === mine) return;
  // Pause extension freezes closes here; the first fetch after the pause
  // does this.
  if (await isExtensionPaused()) return;
  const listed = new Set(planSlots(plan).map(slotLogin).filter(Boolean));
  let closed = 0;
  for (const [tabKey, entry] of marked) {
    const login = String(entry.originalStreamer || "").toLowerCase();
    if (listed.has(login) && (await closeTrackedTab(tabKey, login, "executor_changed"))) closed += 1;
  }
  const stripped = await stripSlotMarkers();
  await log("info",
    `Slot plan: ${plan.executor} runs the plan now; closed ${closed} slot tab(s), left ${stripped} open as ordinary tracked tabs`
  );
}

function indexTrackedTabs(trackedTabs) {
  const byStreamer = new Map();
  for (const [tabKey, entry] of Object.entries(trackedTabs || {})) {
    if (!entry) continue;
    const login = String(entry.originalStreamer || "").toLowerCase();
    if (!login) continue;
    if (!byStreamer.has(login)) byStreamer.set(login, []);
    byStreamer.get(login).push({ tabKey, entry });
  }
  return byStreamer;
}

async function trackedTabsByStreamer() {
  return indexTrackedTabs((await loadState()).trackedTabs);
}

// Applies the plan (DESIGN 8.3). Single-flight: a call while a pass runs
// (a fetchConfig from refreshPlanSoon or the minute alarm) is not dropped;
// the pass runs once more with the stored plan when it finishes. The
// guards are in-memory: a recycle mid-pass loses nothing, since the next
// config tick applies the plan again and opens only what is missing.
let slotApplyInFlight = null;
let slotApplyRerun = false;

function applySlotPlan(plan) {
  if (slotApplyInFlight) {
    slotApplyRerun = true;
    return slotApplyInFlight;
  }
  slotApplyInFlight = (async () => {
    try {
      let next = plan;
      for (;;) {
        slotApplyRerun = false;
        try {
          await applySlotPlanPass(next);
        } catch (e) {
          await log("warn", "Slot plan: apply pass failed:", e?.message || String(e));
        }
        if (!slotApplyRerun) break;
        next = await loadSlotPlan();
      }
    } finally {
      slotApplyInFlight = null;
    }
  })();
  return slotApplyInFlight;
}

async function applySlotPlanPass(firstPlan) {
  let plan = firstPlan;
  if (!(await planActiveHere(plan))) return;
  // 1. Pause extension freezes the plan in this browser: no opens and no
  // closes. The next report says busy, and the desktop holds the plan.
  if (await isExtensionPaused()) return;
  let staggerDue = false;
  for (;;) {
    // 2. The plan's streamers, and the tracked tabs by streamer.
    const planned = new Map();
    for (const slot of planSlots(plan)) {
      const login = slotLogin(slot);
      if (login && typeof slot.id === "string" && !planned.has(login)) planned.set(login, slot);
    }

    // 3. Closes.
    await applyPlanCloses(plan, await trackedTabsByStreamer(), planned);

    // 4. Duplicates: the tab opened first stays.
    const byStreamer = await trackedTabsByStreamer();
    for (const login of planned.keys()) {
      const tabs = byStreamer.get(login) || [];
      if (tabs.length < 2) continue;
      tabs.sort((a, b) => (a.entry.openedAt || 0) - (b.entry.openedAt || 0) || Number(a.tabKey) - Number(b.tabKey));
      for (const extra of tabs.slice(1)) await closeTrackedTab(extra.tabKey, login, "duplicate");
    }

    // 5. Markers: each planned streamer's tab carries its slot id (covers
    // adopted tabs and relabels). A save turn that flipped to live is no
    // longer a save-streak tab, and its tab may still be on the save-streak
    // page or wherever Twitch moved it (a VOD or a clip, A46), so it is sent
    // to the streamer's channel: a live turn shows the live stream.
    const toChannel = [];
    const markerKeys = await withTrackedTabs((trackedTabs) => {
      const keys = [];
      for (const [tabKey, entry] of Object.entries(trackedTabs)) {
        if (!entry) continue;
        const login = String(entry.originalStreamer || "").toLowerCase();
        const slot = planned.get(login);
        if (slot) {
          if (entry.slot !== slot.id) entry.slot = slot.id;
          if (slot.entry === "live" && (entry.saveStreak === true || typeof entry.landing === "string")) {
            entry.saveStreak = false;
            toChannel.push({ tabKey, login });
          }
        }
        if (typeof entry.slot === "string") keys.push(tabKey);
      }
      return keys;
    });
    await cancelLegacyRecordsForSlotTabs(markerKeys);
    for (const { tabKey, login } of toChannel) await sendSaveTurnToChannel(tabKey, login);

    // 6. Opens, Keep Open slots first, SLOT_OPEN_STAGGER_MS apart. Skipped
    // while the plan is not assigning (paused, or the desktop waiting for
    // its first report); closes above still ran. A streamer whose gone
    // entry the plan predates is not reopened (goneHoldsOpen).
    let restarted = false;
    if (plan.assigning === true) {
      for (const slot of planSlots(plan)) {
        const login = slotLogin(slot);
        if (!login || planned.get(login) !== slot) continue;
        if ((await trackedTabsByStreamer()).has(login)) continue;
        const state = await loadSlotState();
        if (pendingOpenFor(state, login)) continue;
        if (goneHoldsOpen(state, login, plan)) {
          await logGoneHold(state, login, plan);
          continue;
        }
        if (staggerDue) {
          await new Promise(r => setTimeout(r, SLOT_OPEN_STAGGER_MS));
          staggerDue = false;
          if (await isExtensionPaused()) return;
          const stored = await loadSlotPlan();
          if (planSeqOf(stored) !== planSeqOf(plan)) {
            if (!(await planActiveHere(stored))) return;
            plan = stored;
            restarted = true;
            break;
          }
          // The tab may have come in, or gone, while this pass waited.
          if ((await trackedTabsByStreamer()).has(login)) continue;
          const later = await loadSlotState();
          if (pendingOpenFor(later, login)) continue;
          if (goneHoldsOpen(later, login, plan)) {
            await logGoneHold(later, login, plan);
            continue;
          }
        }
        if (await openPlannedTab(slot)) staggerDue = true;
      }
    }
    if (restarted) continue;

    // 7.
    const seq = planSeqOf(plan);
    await withSlotState((state) => {
      state.appliedSeq = seq;
    });
    await reportOpenTabs("plan-applied");
    return;
  }
}

// Apply step 5: a save turn the plan flipped to live. Its tab goes to the
// streamer's channel (with sm=1, as reloadTrackedTab does) from wherever
// the save visit had taken it. The entry stays tracked with its slot marker
// (saveStreak is already false), and the navigation's onUpdated event names
// the same streamer, so it changes nothing there. The VOD path (landing) is
// dropped only once the navigation is issued: until then the entry still
// counts on that VOD (isKnownSaveLanding), so a close in between finds the
// tab, and a pass after a failed navigation tries again. Resolves true when
// the navigation was issued.
async function sendSaveTurnToChannel(tabKey, login) {
  const key = String(tabKey);
  const url = `https://www.twitch.tv/${login}?sm=1`;
  let from = "";
  try {
    const tab = await chrome.tabs.get(Number(key));
    from = (tab && (tab.url || tab.pendingUrl)) || "";
    await chrome.tabs.update(Number(key), { url });
  } catch (e) {
    if (!isNoSuchTabError(e)) {
      await log("warn", `Slot plan: could not send tab ${key} to ${login}'s channel:`, e?.message || String(e));
    }
    return false;
  }
  await withTrackedTabs((trackedTabs) => {
    const entry = trackedTabs[key];
    if (entry && String(entry.originalStreamer || "").toLowerCase() === login) delete entry.landing;
  });
  let path = from;
  try {
    path = new URL(from).pathname;
  } catch (e) {
    // keep the raw URL
  }
  await log("info", `Slot plan: ${login}'s save turn is now a live turn; tab ${key} sent from ${path || "?"} to /${login}`);
  return true;
}

// DESIGN 8.3 step 3. An unplanned close of a tab younger than GRACE_MS is
// deferred, a safety net against a desktop bug; it is logged once per plan
// seq.
async function applyPlanCloses(plan, byStreamer, planned) {
  const seq = planSeqOf(plan);
  const deferred = new Set();
  for (const item of Array.isArray(plan.close) ? plan.close : []) {
    if (!isPlainObject(item)) continue;
    const login = typeof item.streamer === "string" ? item.streamer.toLowerCase() : "";
    const reason = typeof item.reason === "string" && item.reason ? item.reason : "unplanned";
    // A plan never closes a streamer it assigns a slot; if one did, the
    // slot wins, since the close would only be reopened.
    if (!login || planned.has(login)) continue;
    for (const { tabKey, entry } of byStreamer.get(login) || []) {
      if (reason === "unplanned" && Date.now() - (entry.openedAt || 0) < GRACE_MS) {
        deferred.add(login);
        continue;
      }
      await closeTrackedTab(tabKey, login, reason);
    }
  }
  const firstTime = await withSlotState((state) => {
    const out = [];
    const logged = {};
    for (const login of deferred) {
      if (state.deferredLogged[login] !== seq) out.push(login);
      logged[login] = seq;
    }
    state.deferredLogged = logged;
    return out;
  });
  for (const login of firstTime) {
    await log("info",
      `Slot plan: deferred closing ${login} (unplanned); its tab is younger than ${GRACE_MINUTES} minutes`
    );
  }
}

// Closes a tracked tab the plan lists (DESIGN 8.3, in the order of
// executePendingExpiration). Resolves true when the tab was closed.
async function closeTrackedTab(tabKey, expectedStreamer, reason) {
  const key = String(tabKey);
  const streamer = String(expectedStreamer || "").toLowerCase();
  const { trackedTabs } = await loadState();
  const current = trackedTabs[key];
  if (!current || current.originalStreamer !== streamer) return false;

  // 0. Tab ids are unique only within one browser session: after a restart
  // a stale entry can name an unrelated tab. One that no longer shows this
  // streamer (a save-streak page counts, and so does a VOD or clip its save
  // visit moved to, A46) is lost: untracked, never closed.
  let tab = null;
  try {
    tab = await chrome.tabs.get(Number(key));
  } catch (e) {
    tab = null;
  }
  if (!tab || !tabShowsEntry(current, tab.url || tab.pendingUrl)) {
    const dropped = await untrackTab(key, streamer);
    if (dropped) {
      await log("info", `Slot plan: tab ${key} no longer shows ${streamer}; untracked it instead of closing it (${reason})`);
    }
    return false;
  }

  // 1. Untracked before the remove, so onTabRemoved records no gone entry.
  const removed = await withTrackedTabs((t) => {
    const entry = t[key];
    if (!entry || entry.originalStreamer !== streamer) return null;
    delete t[key];
    return entry;
  });
  if (!removed) return false;

  // 2.
  await cancelPendingSwapsForTab(key);
  await cancelPendingExpirationForTab(key);
  await clearLoadRecoveryForTab(key);
  await withTabPlacement((map) => {
    delete map[key];
  });

  // 3. A tab that cannot be closed right now (for example "Tabs cannot be
  // edited right now" during a drag) is tracked again, so the next pass
  // retries instead of leaving it open, untracked and unreported.
  try {
    await chrome.tabs.remove(Number(key));
  } catch (e) {
    if (!isNoSuchTabError(e)) {
      await withTrackedTabs((t) => {
        if (!t[key]) t[key] = removed;
      });
      await log("warn", `Slot plan: could not close ${streamer} (tab ${key}, ${reason}):`, e?.message || String(e));
      return false;
    }
  }

  // The Streaks at Risk request a manual save tab served (plan 3.10, A15):
  // untracked before the remove, so onTabRemoved cannot settle it.
  if (removed.manualSave) {
    try {
      await settleManualSaveVisit(removed);
    } catch (e) {
      logStreakStateError(e);
    }
  }

  // 4. A save turn that ran its full length: its Streaks at Risk row is
  // done, so the badge does not stay lit.
  if (removed.saveStreak && reason === "turn_over") {
    try {
      await acknowledgeAtRiskStreak(streamer);
    } catch (e) {
      logStreakStateError(e);
    }
  }

  // 5.
  await log("info", `Slot plan: closed ${streamer} (${reason})`);
  return true;
}

// Opens a plan slot's stream or save-streak page (DESIGN 8.3, the
// openRescueTab pattern). Resolves true when a tab was created. On a
// failure the next pass retries, and the desktop gives the slot up after
// 600 s.
async function openPlannedTab(slot) {
  const login = slotLogin(slot);
  if (!login || typeof slot.id !== "string") return false;
  // 1. The URL comes from the login, never from the plan's url field.
  const url = slot.entry === "save"
    ? `https://www.twitch.tv/save-streak/${login}?sm=1`
    : `https://www.twitch.tv/${login}?sm=1`;
  // 2. Before the tab exists, so an adoption racing this open gets the
  // slot marker.
  await withSlotState((state) => {
    state.pendingOpens[login] = { slotId: slot.id, url, at: Date.now() };
  });
  // 3.
  let tab = null;
  let target = null;
  try {
    target = await targetWindowForOpen(url);
    tab = target.createdTab || await chrome.tabs.create(tabCreateOptions(url, target));
  } catch (e) {
    await withSlotState((state) => {
      delete state.pendingOpens[login];
    });
    await log("warn", `Slot plan: could not open ${login} for ${slot.id}:`, e?.message || String(e));
    return false;
  }
  // 4. The flag is set here too, so a planned save tab carries it whether
  // or not the adoption wins the race.
  const tabKey = String(tab.id);
  const openedAt = Date.now();
  await withTrackedTabs((trackedTabs) => {
    trackedTabs[tabKey] = {
      ...(trackedTabs[tabKey] || {}),
      originalStreamer: login,
      raidHopCount: 0,
      openedAt,
      slot: slot.id,
      saveStreak: slot.entry === "save",
    };
  });
  await finishCreatedTab(tab, target);
  // 5.
  await withSlotState((state) => {
    delete state.pendingOpens[login];
  });
  // 6.
  let muted = false;
  try {
    muted = await muteTabIfEnabled(tab.id, login);
  } catch (e) {
    muted = false;
  }
  activatePlayerControlSoon(tab.id);
  await log("info", `Slot plan: opened ${login} in ${slot.id} (tab ${tabKey})${muted ? " (muted)" : ""}`);
  return true;
}

// Where a tab the extension creates goes, and whether it is activated
// (DESIGN 10.6). With a stream window set: that window, recreated with this
// URL as its only tab when it is gone (createdTab); the tab is activated
// there per Auto-focus unless the owner is working in that window (10.9).
// With none (or when it cannot be had): the last focused normal window, or
// when that one is private, the most recently used normal window that is
// not; with none, the browser's default. Extension-created tabs never go
// into a private window. active is the Auto-focus setting. Resolves
// {windowId (undefined for the browser's default), active, createdTab (a
// tab created together with a new window, else null), inStreamWindow}.
async function targetWindowForOpen(url) {
  const active = await shouldAutoFocus();
  if (await trustedStreamWindow()) {
    try {
      const got = await ensureStreamWindow({ url });
      if (got && typeof got.id === "number") {
        if (got.tab) return { windowId: got.id, active: false, createdTab: got.tab, inStreamWindow: true };
        let inUse = null;
        if (active) inUse = await streamWindowOwnerTab(got.id, null);
        if (inUse !== null) await log("info", "Stream window: in use; opened in the background");
        return { windowId: got.id, active: active && inUse === null, createdTab: null, inStreamWindow: true };
      }
    } catch (e) {
      await log("warn", "Stream window: could not be used for a new tab:", e?.message || String(e));
    }
  }
  let windowId;
  try {
    const last = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    if (last && last.type === "normal" && !last.incognito) {
      windowId = last.id;
    } else {
      const windows = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
      let best = null;
      let bestAt = -1;
      for (const w of windows) {
        if (w.type !== "normal" || w.incognito) continue;
        const usedAt = Math.max(0, ...(w.tabs || []).map(t => Number(t.lastAccessed) || 0));
        if (usedAt >= bestAt) {
          best = w;
          bestAt = usedAt;
        }
      }
      if (best) windowId = best.id;
    }
  } catch (e) {
    windowId = undefined;
  }
  return { windowId, active, createdTab: null, inStreamWindow: false };
}

// A tab for the stream window is created in the background and activated
// right after (finishCreatedTab): activating a tab never focuses its
// window, while a tab created active can bring a background window forward
// (headless Chromium does; Firefox is unverified, DESIGN 10.6).
function tabCreateOptions(url, target) {
  const options = { url, active: target.inStreamWindow ? false : !!target.active };
  if (typeof target.windowId === "number") options.windowId = target.windowId;
  return options;
}

// After an extension-created tab exists: its placement record, and in the
// stream window its activation per Auto-focus (see tabCreateOptions).
async function finishCreatedTab(tab, target) {
  if (!tab || typeof tab.id !== "number") return;
  await withTabPlacement((map) => {
    map[String(tab.id)] = {
      placedIn: target && target.inStreamWindow ? tab.windowId : null,
      seenIn: tab.windowId,
      ownerPlaced: false,
      failures: 0,
    };
  });
  if (target && target.inStreamWindow && target.active && !target.createdTab) {
    try {
      await chrome.tabs.update(tab.id, { active: true });
    } catch (e) {
      await log("warn", `Stream window: could not bring tab ${tab.id} to the front:`, e?.message || String(e));
    }
  }
}

// ---------------------------------------------------------------------------
// Stream window (1.12.0, DESIGN 10)
//
// The owner designates one normal window per profile in the popup ("Use
// this window for streams"). Every tab the extension adopts as a Stream
// Monitor tab is moved there, every tab it creates is created there, and a
// closed stream window is recreated at its remembered position, size and
// state (or a restored window at those bounds is adopted). Auto-focus
// brings a new stream tab to the front inside that window without focusing
// the window, so the window the owner works in keeps focus and its tab.
//
// streamWindow (storage.local, written only through withStreamWindow):
//   {id, state, left, top, width, height, normal: {left, top, width,
//    height} or null, setAt, checkedAt}
// left/top/width/height are the bounds last read while the window was not
// minimized; normal the bounds last read in state "normal". Window ids
// restart with each browser session, so after a browser start the stored id
// is cleared (forgetStaleStreamWindowId) and the window is found again by
// its bounds or recreated.
//
// tabPlacement (withTabPlacement): {tabKey: {placedIn, seenIn, ownerPlaced,
// failures}}. placedIn is the window the extension put the tab in; a tab
// found elsewhere later was moved by the owner and is never moved again.
// ---------------------------------------------------------------------------

// Whether this worker already cleared an id from before a browser start.
// In-memory: a later worker of the same browser session finds the marker
// and trusts the stored id, which this one cleared or replaced.
let staleStreamWindowCheck = null;

// Tab keys whose placement move is under way ({tabKey: count}): from just
// before the extension's own tabs.move until its tabPlacement write has
// landed. The reconciliation skips them, so it never reads a move of the
// extension's own as the owner's. In-memory: a recycle ends every move
// with it.
const placementMovesInFlight = new Map();

function beginPlacementMove(tabKey) {
  placementMovesInFlight.set(tabKey, (placementMovesInFlight.get(tabKey) || 0) + 1);
}

function endPlacementMove(tabKey) {
  const left = (placementMovesInFlight.get(tabKey) || 0) - 1;
  if (left > 0) placementMovesInFlight.set(tabKey, left);
  else placementMovesInFlight.delete(tabKey);
}

function forgetStaleStreamWindowId() {
  if (!staleStreamWindowCheck) {
    staleStreamWindowCheck = (async () => {
      if (!(await checkBrowserSession())) return;
      const cleared = await withStreamWindow((box) => {
        if (!box.value || box.value.id === null || box.value.id === undefined) return false;
        box.value = { ...box.value, id: null };
        return true;
      });
      if (cleared) await log("info", "Stream window: the browser started again, so its old window id is not trusted");
    })().catch(() => {});
  }
  return staleStreamWindowCheck;
}

// The stored streamWindow, after the stale-id check, or null when none is
// set.
async function trustedStreamWindow() {
  await forgetStaleStreamWindowId();
  const result = await chrome.storage.local.get("streamWindow");
  return isPlainObject(result.streamWindow) ? result.streamWindow : null;
}

function windowBounds(w) {
  return { left: w.left, top: w.top, width: w.width, height: w.height };
}

// Bounds a window can be recreated at: at least STREAM_WINDOW_MIN_WIDTH by
// STREAM_WINDOW_MIN_HEIGHT, and not the -32000 Windows reports for a
// minimized window.
function boundsValid(b) {
  return isPlainObject(b) && [b.left, b.top, b.width, b.height].every(Number.isFinite) &&
    b.width >= STREAM_WINDOW_MIN_WIDTH && b.height >= STREAM_WINDOW_MIN_HEIGHT &&
    Math.abs(b.left) < STREAM_WINDOW_MAX_COORD && Math.abs(b.top) < STREAM_WINDOW_MAX_COORD;
}

function boundsNear(a, b) {
  return ["left", "top", "width", "height"].every(k =>
    Number.isFinite(a[k]) && Number.isFinite(b[k]) && Math.abs(a[k] - b[k]) <= STREAM_WINDOW_MATCH_PX);
}

// The remembered rectangle (left/top/width/height), or null.
function rememberedRect(stored) {
  const rect = { left: stored.left, top: stored.top, width: stored.width, height: stored.height };
  return [rect.left, rect.top, rect.width, rect.height].every(Number.isFinite) ? rect : null;
}

// The bounds a closed stream window is recreated at (DESIGN 10.7 step 4):
// normal or minimized: the normal bounds when valid, else the last bounds;
// maximized or fullscreen: the normal bounds when valid and their centre
// lies inside the maximized rectangle (the same monitor), else that
// rectangle. null when nothing valid is known.
function recreateBounds(stored) {
  const rect = rememberedRect(stored);
  const normal = isPlainObject(stored.normal) ? stored.normal : null;
  if (stored.state === "maximized" || stored.state === "fullscreen") {
    if (boundsValid(normal) && boundsValid(rect)) {
      const cx = normal.left + normal.width / 2;
      const cy = normal.top + normal.height / 2;
      if (cx >= rect.left && cx <= rect.left + rect.width && cy >= rect.top && cy <= rect.top + rect.height) {
        return windowBounds(normal);
      }
    }
    if (boundsValid(rect)) return rect;
    return boundsValid(normal) ? windowBounds(normal) : null;
  }
  if (boundsValid(normal)) return windowBounds(normal);
  return boundsValid(rect) ? rect : null;
}

// Bounds bookkeeping (DESIGN 10.8) for the stream window as just read: the
// state always; the bounds when not minimized, and normal too in state
// "normal". Written only when something changed, and only while that
// window is still the stream window.
async function rememberStreamWindowBounds(win) {
  if (!win || typeof win.id !== "number") return;
  await withStreamWindow((box) => {
    if (!box.value || box.value.id !== win.id) return;
    const next = { ...box.value };
    if (typeof win.state === "string") next.state = win.state;
    if (win.state !== "minimized" && [win.left, win.top, win.width, win.height].every(Number.isFinite)) {
      Object.assign(next, windowBounds(win));
      if (win.state === "normal") next.normal = windowBounds(win);
    }
    if (JSON.stringify(next) !== JSON.stringify(box.value)) {
      next.checkedAt = Date.now();
      box.value = next;
    }
  });
}

// Records a new id for the stream window (adopted or recreated), unless the
// owner cleared it meanwhile.
async function rememberStreamWindowId(id) {
  return withStreamWindow((box) => {
    if (!box.value) return false;
    box.value = { ...box.value, id };
    return true;
  });
}

// The refresh-config tick's bounds read (DESIGN 10.8). A stream window that
// is gone is left alone; the next stream recreates it.
async function refreshStreamWindowBounds() {
  const stored = await trustedStreamWindow();
  if (!stored || typeof stored.id !== "number") return;
  let win = null;
  try {
    win = await chrome.windows.get(stored.id);
  } catch (e) {
    return;
  }
  await rememberStreamWindowBounds(win);
}

function isTwitchTab(tab) {
  return /^https?:\/\/(?:www\.)?twitch\.tv\//i.test(String((tab && (tab.url || tab.pendingUrl)) || ""));
}

// ensureStreamWindow(seed) resolves {id, tab} for the stream window, or
// null when none is set or it could not be had. seed is {tabId, replacing}
// (an adopted tab: a recreated window is created with it as its only tab;
// replacing marks a tracked tab re-placed after a browser start, step 3),
// {url} (a tab to create: a recreated window opens with it, returned as
// tab) or null. Single-flight (DESIGN 10.7): a call waits for the one in
// progress, so two placements in the same instant with the window closed
// share the window the first one creates. In-memory: a recycle mid-call
// leaves at worst a window the next call adopts by its bounds.
const streamWindowInFlight = makeChain();

function ensureStreamWindow(seed = null) {
  return streamWindowInFlight(() => ensureStreamWindowNow(seed));
}

async function ensureStreamWindowNow(seed) {
  // 1.
  const stored = await trustedStreamWindow();
  if (!stored) return null;
  const seedTabId = seed && typeof seed.tabId === "number" ? seed.tabId : null;
  const seedUrl = seed && typeof seed.url === "string" ? seed.url : null;

  // 2. The remembered window, when this browser session set it.
  if (typeof stored.id === "number") {
    try {
      const win = await chrome.windows.get(stored.id);
      if (win && win.type === "normal" && !win.incognito) {
        await rememberStreamWindowBounds(win);
        return { id: win.id, tab: null };
      }
    } catch (e) {
      // Gone: closed, or the browser quit.
    }
  }

  // 3. A window the browser restored at the remembered bounds, holding a
  // Twitch tab other than the one being placed, is the stream window. The
  // seed's own window counts too, but only when the seed is an already
  // tracked tab being re-placed after a browser start or an extension
  // update (seed.replacing): the window it sits in may be the stream window
  // itself, holding no other stream. A fresh adoption never counts its own
  // window, which is where the desktop just opened it.
  const rect = rememberedRect(stored);
  const normal = isPlainObject(stored.normal) ? stored.normal : null;
  const replacing = !!(seed && seed.replacing && seedTabId !== null);
  try {
    const all = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
    const fits = w => w.type === "normal" && !w.incognito &&
      ((rect && boundsNear(w, rect)) || (normal && boundsNear(w, normal)));
    let match = all.find(w => fits(w) && (w.tabs || []).some(t => t.id !== seedTabId && isTwitchTab(t)));
    if (!match && replacing) {
      match = all.find(w => fits(w) && (w.tabs || []).some(t => t.id === seedTabId && isTwitchTab(t)));
    }
    if (match) {
      // Cleared by the owner meanwhile: no stream window any more.
      if (!(await rememberStreamWindowId(match.id))) return null;
      await rememberStreamWindowBounds(match);
      await log("info", "Stream window: adopted the restored window");
      return { id: match.id, tab: null };
    }
  } catch (e) {
    await log("warn", "Stream window: could not list the windows:", e?.message || String(e));
  }

  // 4. Recreate it, unfocused, at the remembered bounds, with the seed tab
  // as its only tab (A17's fallbacks when the browser refuses an option).
  let prev = null;
  try {
    const last = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    if (last && typeof last.id === "number") prev = last.id;
  } catch (e) {
    prev = null;
  }
  const base = { type: "normal" };
  if (seedTabId !== null) base.tabId = seedTabId;
  else if (seedUrl) base.url = seedUrl;
  const bounds = recreateBounds(stored);
  const attempts = bounds
    ? [{ ...base, focused: false, ...bounds }, { ...base, focused: false }, { ...base }]
    : [{ ...base, focused: false }, { ...base }];
  let win = null;
  let used = -1;
  for (let i = 0; i < attempts.length && !win; i++) {
    try {
      win = await chrome.windows.create(attempts[i]);
      used = i;
    } catch (e) {
      if (i === attempts.length - 1) {
        await log("warn", "Stream window: could not recreate it:", e?.message || String(e));
      }
    }
  }
  if (!win) return null;
  if (!bounds || used > 0) {
    await log("info", "Stream window: remembered position is off-screen or invalid; opened at the browser default");
  }
  // Never recreated fullscreen; never created with a state and bounds
  // together (both browsers refuse that).
  try {
    if (stored.state === "maximized" || stored.state === "fullscreen") {
      await chrome.windows.update(win.id, { state: "maximized" });
    } else if (stored.state === "minimized") {
      await chrome.windows.update(win.id, { state: "minimized" });
    }
  } catch (e) {
    await log("warn", "Stream window: could not restore its state:", e?.message || String(e));
  }
  // Focus goes back to where the owner was, should the new window have
  // taken it.
  let fresh = win;
  try {
    fresh = await chrome.windows.get(win.id);
  } catch (e) {
    fresh = win;
  }
  if (fresh.focused && prev !== null && prev !== win.id) {
    try {
      await chrome.windows.get(prev);
      await chrome.windows.update(prev, { focused: true });
    } catch (e) {
      // The previous window is gone; nothing to hand back.
    }
  }
  if (!(await rememberStreamWindowId(win.id))) return null;
  await rememberStreamWindowBounds(fresh);
  await log("info",
    `Stream window: recreated it (${fresh.width}x${fresh.height} at ${fresh.left},${fresh.top}, ${fresh.state})`);
  const tab = seedUrl && Array.isArray(win.tabs) && win.tabs[0] ? win.tabs[0] : null;
  return { id: win.id, tab };
}

// The tab to leave in front when the owner is working in the stream window
// (DESIGN 10.9): the stream window is the focused window (last focused is
// not enough: with the owner in another app, no browser window has focus
// and the stream tab comes to the front as usual), and its active tab
// (other than newTabId; if that is the active one, the tab used before it)
// is not a tracked stream tab. null when the window is not in use.
async function streamWindowOwnerTab(windowId, newTabId) {
  let last = null;
  try {
    last = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
  } catch (e) {
    return null;
  }
  if (!last || last.id !== windowId || last.focused !== true) return null;
  let tabs = [];
  try {
    tabs = (await chrome.tabs.query({ windowId })).filter(t => t.id !== newTabId);
  } catch (e) {
    return null;
  }
  const owner = tabs.find(t => t.active) ||
    tabs.filter(t => typeof t.lastAccessed === "number").sort((a, b) => b.lastAccessed - a.lastAccessed)[0];
  if (!owner) return null;
  const { trackedTabs } = await loadState();
  return trackedTabs[String(owner.id)] ? null : owner.id;
}

// Moves an adopted Stream Monitor tab into the stream window (DESIGN 10.5
// step 3) and resolves its final windowId. Nothing happens for a tab in a
// private window or when no stream window is set. A tab that was the active
// tab of another window leaves that window on the tab the owner used
// before it (10.10). The placement record says where the tab went, or
// counts a failed move (retried by the scan, given up after
// STREAM_WINDOW_PLACE_MAX_FAILURES). opts.replacing: the tab was already in
// place before this browser session's worker started (a restored or kept
// tab re-placed by the init scan), so the window it sits in may be adopted
// as the stream window (ensureStreamWindow step 3).
async function placeInStreamWindow(tab, opts = {}) {
  if (!tab || typeof tab.id !== "number") return tab ? tab.windowId : undefined;
  let current;
  try {
    current = await chrome.tabs.get(tab.id);
  } catch (e) {
    return tab.windowId;
  }
  if (current.incognito) return current.windowId;
  const stored = await trustedStreamWindow();
  if (!stored) return current.windowId;
  const tabKey = String(current.id);

  // 10.10: the source window's tab to give back.
  let source = null;
  let previous = null;
  if (current.active && current.windowId !== stored.id) {
    source = current.windowId;
    try {
      const others = (await chrome.tabs.query({ windowId: source }))
        .filter(t => t.id !== current.id && typeof t.lastAccessed === "number")
        .sort((a, b) => b.lastAccessed - a.lastAccessed);
      previous = others.length > 0 ? others[0].id : null;
    } catch (e) {
      previous = null;
    }
  }

  const target = await ensureStreamWindow({ tabId: current.id, replacing: !!(opts && opts.replacing) });
  if (!target) return current.windowId;
  let now = current;
  try {
    now = await chrome.tabs.get(current.id);
  } catch (e) {
    return current.windowId;
  }
  let finalWindow = now.windowId;
  let failure = null;
  beginPlacementMove(tabKey);
  try {
    if (now.windowId !== target.id) {
      try {
        await chrome.tabs.move(now.id, { windowId: target.id, index: -1 });
        finalWindow = target.id;
      } catch (e) {
        failure = e;
      }
    }
    await withTabPlacement((map) => {
      const rec = isPlainObject(map[tabKey]) ? map[tabKey] : { placedIn: null, seenIn: now.windowId, ownerPlaced: false, failures: 0 };
      if (failure) {
        rec.seenIn = now.windowId;
        rec.failures = (Number(rec.failures) || 0) + 1;
      } else {
        rec.placedIn = target.id;
        rec.seenIn = target.id;
      }
      map[tabKey] = rec;
    });
  } finally {
    endPlacementMove(tabKey);
  }
  if (failure) {
    await log("warn", `Stream window: could not move tab ${tabKey}:`, failure?.message || String(failure));
    return finalWindow;
  }
  if (source !== null && previous !== null && source !== finalWindow) {
    try {
      const [active] = await chrome.tabs.query({ windowId: source, active: true });
      if (!active || active.id !== previous) {
        await chrome.tabs.update(previous, { active: true });
        await log("info", `Stream window: window ${source} is back on tab ${previous}`);
      }
    } catch (e) {
      // The source window closed with the move (its last tab).
    }
  }
  return finalWindow;
}

// Auto-focus for an adopted tab (DESIGN 10.9), from its final windowId. In
// the stream window: the tab comes to the front there, and the window's
// focus is never touched; unless the owner is working in the stream window
// (streamWindowOwnerTab), where it stays in the background and the owner's
// tab stays in front. Anywhere else: focusTabIfEnabled, as before 1.12.0.
async function focusPlacedTab(tab, windowId) {
  if (!(await shouldAutoFocus())) return false;
  const stored = await trustedStreamWindow();
  if (stored && typeof stored.id === "number" && windowId === stored.id) {
    const owner = await streamWindowOwnerTab(windowId, tab.id);
    if (owner !== null) {
      try {
        const now = await chrome.tabs.get(tab.id);
        if (now.active) await chrome.tabs.update(owner, { active: true });
      } catch (e) {
        // Closed meanwhile.
      }
      await log("info", `Stream window: in use; opened tab ${tab.id} in the background`);
      return false;
    }
    try {
      await chrome.tabs.update(tab.id, { active: true });
      return true;
    } catch (e) {
      await log("warn", `Failed to focus tab ${tab.id}:`, e?.message || String(e));
      return false;
    }
  }
  return focusTabIfEnabled({ ...tab, windowId });
}

// Whether two placement records say the same about a tab (placedIn, seenIn
// and failures).
function samePlacement(a, b) {
  return a.placedIn === b.placedIn && a.seenIn === b.seenIn &&
    (Number(a.failures) || 0) === (Number(b.failures) || 0);
}

// Placement reconciliation inside scanExistingTabs (DESIGN 10.11), every
// minute and at init. A tracked tab found outside the window the extension
// placed it in was moved by the owner: marked ownerPlaced, logged once,
// never moved again. An unplaced tab (no record, or a failed move) is
// placed, unless it left the window it was in when a move failed (the
// owner dragged it meanwhile) or it failed STREAM_WINDOW_PLACE_MAX_FAILURES
// times. The records are read before the tab list, so a record is never
// newer than the window it is compared with; a tab whose move of the
// extension's own is under way, or whose record changed since that read,
// waits for the next round. afterBrowserStart: the init scan after the
// browser-session marker was missing, whose tabs were in place before this
// worker started (placeInStreamWindow opts.replacing).
async function reconcileStreamWindowPlacement({ afterBrowserStart = false } = {}) {
  const stored = await trustedStreamWindow();
  if (!stored) return;
  const { trackedTabs } = await loadState();
  const result = await chrome.storage.local.get("tabPlacement");
  const placement = isPlainObject(result.tabPlacement) ? result.tabPlacement : {};
  const allTabs = await chrome.tabs.query({});
  const tabsById = new Map(allTabs.map(t => [String(t.id), t]));
  const toPlace = [];
  const ownerMoved = [];
  for (const [tabKey, entry] of Object.entries(trackedTabs)) {
    const tab = tabsById.get(tabKey);
    if (!entry || !tab || tab.incognito || placementMovesInFlight.has(tabKey)) continue;
    const rec = isPlainObject(placement[tabKey]) ? placement[tabKey] : null;
    if (rec && rec.ownerPlaced) continue;
    if (rec && typeof rec.placedIn === "number") {
      if (tab.windowId !== rec.placedIn) ownerMoved.push({ tabKey, tab, streamer: entry.originalStreamer, seen: rec });
      continue;
    }
    if (rec && (Number(rec.failures) || 0) >= STREAM_WINDOW_PLACE_MAX_FAILURES) continue;
    if (rec && (Number(rec.failures) || 0) > 0 && typeof rec.seenIn === "number" && tab.windowId !== rec.seenIn) {
      ownerMoved.push({ tabKey, tab, streamer: entry.originalStreamer, seen: rec });
      continue;
    }
    toPlace.push(tab);
  }
  if (ownerMoved.length > 0) {
    const marked = await withTabPlacement((map) => {
      const out = [];
      for (const m of ownerMoved) {
        if (placementMovesInFlight.has(m.tabKey)) continue;
        const rec = isPlainObject(map[m.tabKey]) ? map[m.tabKey] : null;
        if (!rec || rec.ownerPlaced || !samePlacement(rec, m.seen)) continue;
        rec.ownerPlaced = true;
        map[m.tabKey] = rec;
        out.push(m);
      }
      return out;
    });
    for (const m of marked) {
      await log("info", `Stream window: tab ${m.tabKey} (${m.streamer}) was moved out by hand; it stays where it is`);
    }
  }
  for (const tab of toPlace) {
    const windowId = await placeInStreamWindow(tab, { replacing: afterBrowserStart });
    if (windowId !== tab.windowId) await log("info", `Stream window: placed tab ${tab.id} there`);
  }
}

// "Use this window for streams" (stream_window_set, DESIGN 10.4): a normal,
// non-private window becomes the stream window, and every tracked tab the
// owner has not moved by hand moves into it when it is unplaced (no record,
// or a failed move, unless the tab left the window it was in when the move
// failed) or still sits in the window it was placed in (the previous stream
// window, or an earlier one after "Stop using a stream window"). A tab found
// anywhere else was dragged by the owner: it stays, and the next
// reconciliation marks it ownerPlaced (10.12). Resolves {ok: true, moved}
// or {ok: false, reason: "missing" | "not_normal" | "private"}.
async function setStreamWindow(windowId) {
  await forgetStaleStreamWindowId();
  let win = null;
  try {
    win = typeof windowId === "number" ? await chrome.windows.get(windowId) : null;
  } catch (e) {
    win = null;
  }
  if (!win) return { ok: false, reason: "missing" };
  if (win.type !== "normal") return { ok: false, reason: "not_normal" };
  if (win.incognito) return { ok: false, reason: "private" };
  await withStreamWindow((box) => {
    const now = Date.now();
    const next = {
      ...(box.value || {}),
      id: win.id,
      state: win.state,
      normal: box.value && isPlainObject(box.value.normal) ? box.value.normal : null,
      setAt: now,
      checkedAt: now,
    };
    if (win.state !== "minimized") {
      Object.assign(next, windowBounds(win));
      if (win.state === "normal") next.normal = windowBounds(win);
    }
    box.value = next;
  });

  const { trackedTabs } = await loadState();
  const allTabs = await chrome.tabs.query({});
  const tabsById = new Map(allTabs.map(t => [String(t.id), t]));
  const result = await chrome.storage.local.get("tabPlacement");
  const placement = isPlainObject(result.tabPlacement) ? result.tabPlacement : {};
  let moved = 0;
  const placed = [];
  try {
    for (const tabKey of Object.keys(trackedTabs)) {
      const tab = tabsById.get(tabKey);
      if (!tab || tab.incognito) continue;
      const rec = isPlainObject(placement[tabKey]) ? placement[tabKey] : null;
      if (rec && rec.ownerPlaced) continue;
      const unplaced = !rec || typeof rec.placedIn !== "number";
      if (!unplaced && tab.windowId !== rec.placedIn) continue;
      if (unplaced && rec && (Number(rec.failures) || 0) > 0 && typeof rec.seenIn === "number" && tab.windowId !== rec.seenIn) continue;
      beginPlacementMove(tabKey);
      if (tab.windowId !== win.id) {
        try {
          await chrome.tabs.move(tab.id, { windowId: win.id, index: -1 });
          moved += 1;
        } catch (e) {
          endPlacementMove(tabKey);
          await log("warn", `Stream window: could not move tab ${tabKey}:`, e?.message || String(e));
          continue;
        }
      }
      placed.push(tabKey);
    }
    await withTabPlacement((map) => {
      for (const tabKey of placed) {
        map[tabKey] = { placedIn: win.id, seenIn: win.id, ownerPlaced: false, failures: 0 };
      }
    });
  } finally {
    for (const tabKey of placed) endPlacementMove(tabKey);
  }
  await log("info",
    `Stream window: set to window ${win.id} (${win.width}x${win.height} at ${win.left},${win.top}); moved ${moved} tab(s) there`);
  return { ok: true, moved };
}

// "Stop using a stream window": tabs stay where they are, and new stream
// tabs open wherever the browser puts them.
async function clearStreamWindow() {
  await withStreamWindow((box) => {
    box.value = null;
  });
  await log("info", "Stream window: no longer set");
  return { ok: true };
}

// The popup's status line (stream_window_status {windowId}, the popup's
// own window).
async function streamWindowStatus(windowId) {
  const stored = await trustedStreamWindow();
  if (!stored) {
    return { set: false, id: null, state: null, bounds: null, existsNow: false, isThisWindow: false };
  }
  const id = typeof stored.id === "number" ? stored.id : null;
  let live = null;
  if (id !== null) {
    try {
      const win = await chrome.windows.get(id);
      if (win && win.type === "normal" && !win.incognito) live = win;
    } catch (e) {
      live = null;
    }
  }
  let bounds = rememberedRect(stored);
  if (live && live.state !== "minimized" && [live.left, live.top, live.width, live.height].every(Number.isFinite)) {
    bounds = windowBounds(live);
  }
  return {
    set: true,
    id,
    state: live ? live.state : (typeof stored.state === "string" ? stored.state : null),
    bounds,
    existsNow: !!live,
    isThisWindow: !!live && live.id === windowId,
  };
}

// ---------------------------------------------------------------------------
// Bell check on open (1.12.0, AUDIT B1)
//
// BELL_CHECK_AFTER_COMPLETE_MS after a tracked tab loads (and after a scan
// adopts one), the background asks its content script to open the
// notifications bell once and read the streak cards (checkBell). The bell
// inbox is account-wide, so a tab within BELL_CHECK_COVER_MS of another
// tab's successful check is asked in "covered" mode and clicks nothing. The
// popup toggle bellCheckOnOpen (absent means on) turns the requests off.
// The timer is a setTimeout, as the player activation beside it: a worker
// recycled before it fires leaves the check to the content script's
// keepalive backstop (B3), at most 2 minutes later.
// ---------------------------------------------------------------------------

// When a check last opened the bell and read a rendered list. In-memory: a
// recycle costs at most one extra check.
let lastSuccessfulBellCheckAt = 0;
// One pending request per tab; a newer one replaces it. In-memory for the
// same reason as the timer.
const bellCheckTimers = new Map();

function scheduleBellCheck(tabId) {
  const pending = bellCheckTimers.get(tabId);
  if (pending) clearTimeout(pending);
  bellCheckTimers.set(tabId, setTimeout(() => {
    bellCheckTimers.delete(tabId);
    requestBellCheck(tabId).catch(() => {});
  }, BELL_CHECK_AFTER_COMPLETE_MS));
}

// Schedules the check for a tab that finished loading before its adoption
// was written (the complete event found it untracked).
async function scheduleBellCheckIfLoaded(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && tab.status === "complete") scheduleBellCheck(tabId);
  } catch (e) {
    // Closed meanwhile.
  }
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function describeBellReply(reply) {
  if (!reply || typeof reply !== "object") return "no answer";
  if (reply.skipped) return `skipped (${reply.skipped})`;
  if (reply.reason) return String(reply.reason);
  const cards = Number(reply.cards) || 0;
  const events = Number(reply.events) || 0;
  if (reply.opened) {
    const unread = reply.unreadBefore === null || reply.unreadBefore === undefined ? "unknown" : reply.unreadBefore;
    return `opened, ${unread} unread before, ${plural(cards, "card")}, ${plural(events, "streak event")}`;
  }
  return `read the open dropdown, ${plural(cards, "card")}, ${plural(events, "streak event")}`;
}

// Asks a tracked tab's content script for its open check (plan 3.11) and
// logs one line for the reply. Resolves the reply, or null when nothing
// was asked.
async function requestBellCheck(tabId) {
  const { trackedTabs } = await loadState();
  const entry = trackedTabs[String(tabId)];
  if (!entry) return null;
  const setting = await chrome.storage.local.get("bellCheckOnOpen");
  if (setting.bellCheckOnOpen === false) return null;
  const mode = Date.now() - lastSuccessfulBellCheckAt < BELL_CHECK_COVER_MS ? "covered" : "open";
  const streamer = entry.originalStreamer || "unknown";
  let reply = null;
  try {
    reply = await chrome.tabs.sendMessage(tabId, { action: "checkBell", mode });
  } catch (e) {
    await log("info", `Bell check on open (${streamer}, tab ${tabId}): no answer (${e?.message || String(e)})`);
    return null;
  }
  // Only a check that opened the bell and read a rendered list covers the
  // next tabs; user-busy, no-bell and not-rendered cover nothing.
  if (reply && reply.opened === true && reply.rendered === true) lastSuccessfulBellCheckAt = Date.now();
  await log("info", `Bell check on open (${streamer}, tab ${tabId}): ${describeBellReply(reply)}`);
  return reply;
}

// ---------------------------------------------------------------------------
// Streaks at Risk row clicks (1.12.0, AUDIT S5, A15)
//
// save_streak_now {streamer}: when the desktop runs Slot mode (a fresh,
// active stored plan and a desktop that takes manual saves), the click is
// posted as a manual streak event, which makes the save-streak page the
// next rotating turn in whichever profile executes the plan; opening a tab
// here would add a stream outside the plan. Otherwise the page opens here
// as a tracked Stream Monitor tab (openManualSaveTab). The row shows the
// request until the visit ends (plan 3.10).
// ---------------------------------------------------------------------------

// Whether a click becomes a rotating turn: the plan is this version,
// active and fresh, whoever executes it, and the desktop takes "manual".
function manualSaveGoesToPlan(plan, sources) {
  return isPlainObject(plan) && plan.v === SLOT_PLAN_VERSION && plan.active === true &&
    typeof plan.generated_at === "number" && Date.now() - plan.generated_at * 1000 < SLOT_PLAN_STALE_MS &&
    Array.isArray(sources) && sources.includes("manual");
}

// The manual event for a row (plan 3.5): its status, count (null for a row
// made from a link), detection time and deadline.
function manualStreakEvent(login, row) {
  const event = {
    status: row && row.status === "in_danger" ? "in_danger" : "broke",
    streamer: login,
    count: row && Number.isInteger(row.count) ? row.count : null,
    source: "manual",
    detected_at: row && typeof row.detected_at === "string" ? row.detected_at : new Date().toISOString(),
  };
  if (row && typeof row.deadline_at === "string") event.deadline_at = row.deadline_at;
  return event;
}

async function saveStreakNow(streamer) {
  const login = String(streamer || "").toLowerCase();
  if (!SLOT_LOGIN_RE.test(login) || IGNORED_PATHS.has(login)) return { ok: false, reason: "bad_login" };
  const stored = await chrome.storage.local.get(["slotPlan", "streakSources", "atRiskStreaks"]);
  const rows = isPlainObject(stored.atRiskStreaks) ? stored.atRiskStreaks : {};
  const row = isPlainObject(rows[login]) ? rows[login] : null;
  if (manualSaveGoesToPlan(stored.slotPlan, stored.streakSources)) {
    const answer = await forwardStreakEvent(manualStreakEvent(login, row));
    if (answer.ok && answer.item) {
      await markAtRiskRequested(login, "slot");
      await log("info", `Streaks at Risk: ${login}'s save-streak page is the next rotating turn`);
      return { ok: true, mode: "slot" };
    }
    return { ok: false, reason: answer.reached ? "desktop_refused" : "desktop_down" };
  }
  const tab = await openManualSaveTab(login);
  if (!tab) return { ok: false, reason: "open_failed" };
  await markAtRiskRequested(login, "tab");
  return { ok: true, mode: "tab" };
}

// Opens a Streaks at Risk row's save-streak page as a Stream Monitor tab
// (A15): the pending open first (so an adoption racing it marks the tab a
// manual save, never a slot), the tab where targetWindowForOpen puts it,
// then the tracked entry, muted per Auto-mute and kept playing. Resolves
// the tab, or null when it could not be created.
async function openManualSaveTab(login) {
  const url = `https://www.twitch.tv/save-streak/${login}?sm=1`;
  await withSlotState((state) => {
    state.pendingOpens[login] = { slotId: null, url, at: Date.now(), manual: true };
  });
  let tab = null;
  let target = null;
  try {
    target = await targetWindowForOpen(url);
    tab = target.createdTab || await chrome.tabs.create(tabCreateOptions(url, target));
  } catch (e) {
    await withSlotState((state) => {
      delete state.pendingOpens[login];
    });
    await log("warn", `Streaks at Risk: could not open ${login}'s save-streak page:`, e?.message || String(e));
    return null;
  }
  const tabKey = String(tab.id);
  await withTrackedTabs((trackedTabs) => {
    trackedTabs[tabKey] = {
      ...(trackedTabs[tabKey] || {}),
      originalStreamer: login,
      raidHopCount: 0,
      openedAt: Date.now(),
      saveStreak: true,
      manualSave: true,
    };
  });
  await finishCreatedTab(tab, target);
  await withSlotState((state) => {
    delete state.pendingOpens[login];
  });
  let muted = false;
  try {
    muted = await muteTabIfEnabled(tab.id, login);
  } catch (e) {
    muted = false;
  }
  activatePlayerControlSoon(tab.id);
  await log("info", `Streaks at Risk: opened ${login}'s save-streak page in tab ${tabKey}${muted ? " (muted)" : ""}`);
  return tab;
}

// Plan 3.10: a click queued as a rotating turn goes back to a plain row
// when a fresh plan generated at least MANUAL_REQUEST_PLAN_GRACE_MS after
// the click lists the streamer in neither a slot nor the queue (the item
// expired or was dropped). Checked on every /config, in every profile.
async function dropLapsedSaveRequests(plan) {
  if (!isPlainObject(plan) || plan.v !== SLOT_PLAN_VERSION || typeof plan.generated_at !== "number" ||
      Date.now() - plan.generated_at * 1000 >= SLOT_PLAN_STALE_MS) {
    return;
  }
  const listed = new Set(planSlots(plan).map(slotLogin).filter(Boolean));
  for (const q of Array.isArray(plan.queue) ? plan.queue : []) {
    if (isPlainObject(q) && typeof q.streamer === "string") listed.add(q.streamer.toLowerCase());
  }
  const generatedMs = plan.generated_at * 1000;
  const cleared = await withStreakState(async () => {
    const map = await loadAtRiskStreaks();
    const out = [];
    for (const [key, row] of Object.entries(map)) {
      if (!isPlainObject(row) || row.requested_mode !== "slot" || typeof row.requested_at !== "number") continue;
      if (generatedMs < row.requested_at + MANUAL_REQUEST_PLAN_GRACE_MS || listed.has(key)) continue;
      delete row.requested_at;
      delete row.requested_mode;
      out.push(key);
    }
    if (out.length > 0) await saveAtRiskStreaks(map);
    return out;
  });
  if (cleared.length > 0) {
    await log("info", `Streaks at Risk: the plan no longer lists ${cleared.join(", ")}; no longer marked as opened`);
  }
}

// ---------------------------------------------------------------------------
// Config fetching: uses fetch() (XHR is not available in service workers)
// ---------------------------------------------------------------------------

// The desktop's /config as parsed JSON; throws when it does not answer 2xx.
async function getDesktopConfig() {
  const response = await fetch(CONFIG_URL, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// /config's saved_streaks (v1.11.2), or null from an older desktop.
function desktopSavedStreaks(data) {
  const saves = data && data.saved_streaks;
  return saves && typeof saves === "object" && !Array.isArray(saves) ? saves : null;
}

// Re-reads only the desktop's saved streaks. Resolves true when the desktop
// answered with a list and it was merged. Like every successful /config,
// it also re-sends reports still owed to the desktop (not waited for).
async function refreshSavedStreaksFromDesktop() {
  try {
    const fetchStartedAt = Date.now();
    const saves = desktopSavedStreaks(await getDesktopConfig());
    if (!saves) return false;
    await mergeSavedStreaksFromDesktop(saves, fetchStartedAt);
    resendPendingSavedStreaks().catch(logStreakStateError);
    return true;
  } catch (e) {
    await log("info", "Saved-streak refresh failed:", e?.message || String(e));
    return false;
  }
}

// applyPlan false (the init IIFE only) stores the plan without acting on
// it; the IIFE follows it after its tab scan.
async function fetchConfig({ applyPlan = true } = {}) {
  const hasPerm = await chrome.permissions.contains({ origins: ["http://127.0.0.1/*"] });
  await log("info", "Host permission granted:", hasPerm);

  try {
    // Taken before the request: a saved-streak report delivered after this
    // moment may be missing from the answer without having been judged.
    const fetchStartedAt = Date.now();
    const data = await getDesktopConfig();

    if (data?.streamers && Array.isArray(data.streamers)) {
      const monitored = data.streamers.map(s => s.toLowerCase());
      await saveMonitoredStreamers(monitored);

      // pinned_streamers is new in v1.5.4. Old desktops won't include it;
      // treat missing/non-array as "nothing pinned" (= same default behavior
      // as 1.4.x).
      const pinned = Array.isArray(data.pinned_streamers)
        ? data.pinned_streamers.map(s => s.toLowerCase())
        : [];
      await savePinnedStreamers(pinned);

      // Save live status from desktop app for popup display
      if (Array.isArray(data.live_streamers)) {
        await chrome.storage.local.set({ liveStreamers: data.live_streamers });
      }

      // Streaks the desktop counts as already saved (v1.11.2). Older
      // desktops send nothing here: our own detections then stand on
      // their TTL and are not re-sent, since those desktops refuse them.
      const desktopSaves = desktopSavedStreaks(data);
      if (desktopSaves) {
        try {
          await mergeSavedStreaksFromDesktop(desktopSaves, fetchStartedAt);
        } catch (e) {
          await log("warn", "Saved-streak merge failed:", e?.message || String(e));
        }
      }

      // Slot mode (1.12.0). A desktop without it sends no slot_plan (stored
      // as null) and no streak_sources (the key is removed), and
      // autoSaveStreaks is true only while the desktop publishes true.
      let plan = null;
      try {
        plan = isPlainObject(data.slot_plan) ? data.slot_plan : null;
        await chrome.storage.local.set({
          slotPlan: plan,
          autoSaveStreaks: data.auto_save_streaks === true,
        });
        if (Array.isArray(data.streak_sources)) {
          await chrome.storage.local.set({
            streakSources: data.streak_sources.filter(s => typeof s === "string"),
          });
        } else {
          await chrome.storage.local.remove("streakSources");
        }
      } catch (e) {
        await log("warn", "Slot plan: storing the plan failed:", e?.message || String(e));
      }
      let planHere = false;
      try {
        planHere = await planActiveHere(plan);
      } catch (e) {
        planHere = false;
      }

      // Streak-rescue offer from the desktop (published when the user's
      // own stream ends). Also use the tick to self-heal an active
      // session whose slots dropped below capacity. Not while a plan runs
      // here: the rotating slot replaces the rescue rotation, and a claim
      // still waiting for its ack is dropped.
      if (!planHere) {
        try {
          await maybeStartRescueFromConfig(data.rescue);
          const rescueSession = await loadRescueSession();
          if (rescueSession && rescueSession.active) {
            await topUpRescueSlots();
            await ensureRescueAlarm();
          }
        } catch (e) {
          await log("warn", "Rescue config handling failed:", e?.message || String(e));
        }
      } else {
        try {
          await dropRescueClaim();
        } catch (e) {
          await log("warn", "Rescue claim drop failed:", e?.message || String(e));
        }
      }

      // A Streaks at Risk click queued as a rotating turn that the plan
      // has dropped goes back to a plain row (plan 3.10).
      try {
        await dropLapsedSaveRequests(plan);
      } catch (e) {
        await log("warn", "Streaks at Risk: request check failed:", e?.message || String(e));
      }

      // The desktop is answering: deliver any already_saved report it has
      // not acknowledged yet, with its original detection time.
      if (desktopSaves) {
        try {
          await resendPendingSavedStreaks();
        } catch (e) {
          await log("warn", "Saved-streak re-send failed:", e?.message || String(e));
        }
      }

      await log("info", `Config loaded: ${monitored.length} monitored, ${pinned.length} pinned`);
      if (applyPlan) await followSlotPlan(plan);
      return new Set(monitored);
    }
  } catch (e) {
    await log("warn", "Config fetch failed:", e?.message || String(e));
  }
  return null;
}

// ---------------------------------------------------------------------------
// Scan existing tabs and reconcile with stored state
// ---------------------------------------------------------------------------

// Drops entries whose tab is gone and adopts Stream Monitor (sm=1) tabs
// that are not tracked yet. The tab list is read before the write step, so
// the step drops only entries that were in the map before the query: a
// tab adopted in between survives.
//
// afterBrowserStart marks the first scan after the browser-session marker
// was missing (DESIGN 8.8). Tab ids restart with each browser session, so
// an entry whose id now shows another streamer, or no Twitch channel, is
// lost too. Session restore brings stream tabs back with new ids and
// without sm=1 (Twitch strips it), so each lost entry, and each
// window-closed tombstone younger than WINDOW_CLOSED_TOMBSTONE_MS,
// re-adopts the first untracked Twitch tab of its streamer in a
// non-private window among the tabs open when the scan began, keeping its
// slot marker. Every other scan adopts sm=1 tabs only, so a plain Twitch
// tab the owner opens by hand is never tracked. previousEntries ({tabKey:
// openedAt}, read by the init before anything else ran) limits that URL
// check to the previous session's entries: a tab this worker opened during
// the init (a rescue open from the init's /config) may not have its URL yet
// and is not lost.
async function scanExistingTabs({ afterBrowserStart = false, previousEntries = null } = {}) {
  const { trackedTabs: known, monitoredStreamers } = await loadState();
  const knownKeys = new Set(Object.keys(known));
  const allTabs = await chrome.tabs.query({});
  const tabsById = new Map(allTabs.map(t => [String(t.id), t]));
  const planStreamers = await currentPlanStreamers();
  const slotState = await loadSlotState();
  const now = Date.now();
  const tombstones = afterBrowserStart
    ? slotState.windowClosed.filter(t => typeof t.streamer === "string" && typeof t.at === "number" &&
        now - t.at < WINDOW_CLOSED_TOMBSTONE_MS)
    : [];

  const scan = await withTrackedTabs((trackedTabs) => {
    const lost = [];
    for (const tabKey of Object.keys(trackedTabs)) {
      if (!knownKeys.has(tabKey)) continue;
      const entry = trackedTabs[tabKey];
      const streamer = entry && entry.originalStreamer ? String(entry.originalStreamer).toLowerCase() : null;
      const tab = tabsById.get(tabKey);
      const fromBefore = !previousEntries || (entry && previousEntries[tabKey] === entry.openedAt);
      const url = tab ? tab.url || tab.pendingUrl : "";
      if (tab && !(afterBrowserStart && fromBefore &&
          getStreamerFromUrl(url) !== streamer && !isKnownSaveLanding(entry, url))) continue;
      delete trackedTabs[tabKey];
      if (streamer) {
        lost.push({
          streamer,
          slot: entry.slot,
          saveStreak: entry.saveStreak === true,
          landing: typeof entry.landing === "string" ? entry.landing : null,
        });
      }
    }

    const adopted = [];
    if (afterBrowserStart) {
      const wanted = [...lost, ...tombstones.map(t => ({ streamer: t.streamer.toLowerCase(), slot: t.slot }))];
      const free = (t) => !t.incognito && !trackedTabs[String(t.id)];
      for (const want of wanted) {
        // A save visit Twitch had moved to a VOD comes back on that VOD,
        // whose URL names no streamer (A46): it is found by the remembered
        // path first.
        const tab = (want.landing && allTabs.find(t => free(t) && vodPathOf(t.url || "") === want.landing)) ||
          allTabs.find(t => free(t) && getStreamerFromUrl(t.url || "") === want.streamer);
        if (!tab) continue;
        const entry = {
          originalStreamer: want.streamer,
          raidHopCount: 0,
          openedAt: now,
          saveStreak: SAVE_STREAK_URL_PATTERN.test(tab.url),
        };
        // Restored on a page its save visit had moved to: still that visit.
        if (want.saveStreak && isSaveLanding({ originalStreamer: want.streamer, saveStreak: true }, tab.url)) {
          entry.saveStreak = true;
        }
        // Found on its remembered VOD: that stays its landing, also for a
        // save turn flipped to live whose tab never left the VOD.
        const landing = vodPathOf(tab.url);
        if (landing && landing === want.landing) entry.landing = landing;
        if (typeof want.slot === "string" && want.slot) entry.slot = want.slot;
        trackedTabs[String(tab.id)] = entry;
        adopted.push({ tab, streamer: want.streamer, restored: true });
      }
    }

    // Twitch tabs opened by Stream Monitor (sm=1) that aren't tracked yet.
    // We don't know when the tab was originally opened, so stamp openedAt
    // with the current time. This conservatively gives a fresh grace
    // window rather than guessing a past timestamp. saveStreak records,
    // while sm=1 is still in the URL, that the tab was opened as a
    // save-streak page (see releaseSaveStreakTab).
    for (const tab of allTabs) {
      const url = tab.url || "";
      const tabKey = String(tab.id);
      if (trackedTabs[tabKey] || !isStreamMonitorTab(url)) continue;
      const streamer = getStreamerFromUrl(url);
      if (!streamer) continue;
      if (!(monitoredStreamers.has(streamer) || planStreamers.has(streamer) ||
            (isStreamMonitorTab(url) && SAVE_STREAK_URL_PATTERN.test(url)))) continue;
      trackedTabs[tabKey] = adoptedTabEntry(streamer, url, { pending: pendingOpenFor(slotState, streamer) });
      adopted.push({ tab, streamer, restored: false });
    }
    return { adopted, count: Object.keys(trackedTabs).length };
  });

  // Consumed or not, the tombstones have done their job.
  if (afterBrowserStart) {
    await withSlotState((state) => {
      state.windowClosed = [];
    });
  }
  // Order (DESIGN 10.5): track, mute, place in the stream window, activate
  // the player; and the bell check on open (AUDIT B1).
  for (const { tab, streamer, restored } of scan.adopted) {
    let muted = false;
    try {
      muted = await muteTabIfEnabled(tab.id, streamer);
    } catch (e) {
      muted = false;
    }
    let windowId = tab.windowId;
    try {
      // A restored tab was in place before the browser started, so the
      // window it sits in may be the restored stream window.
      windowId = await placeInStreamWindow(tab, { replacing: restored });
    } catch (e) {
      await log("warn", `Stream window: placing tab ${tab.id} failed:`, e?.message || String(e));
    }
    activatePlayerControl(tab.id);
    scheduleBellCheck(tab.id);
    const moved = windowId !== tab.windowId ? " (moved to stream window)" : "";
    await log("info", restored
      ? `Re-adopted restored tab for ${streamer}${moved}`
      : `Scan: tracking tab ${tab.id} for ${streamer}${muted ? " (muted)" : ""}${moved}`);
  }
  // DESIGN 10.11: owner moves and unplaced tabs.
  try {
    await reconcileStreamWindowPlacement({ afterBrowserStart });
  } catch (e) {
    await log("warn", "Stream window: reconciliation failed:", e?.message || String(e));
  }
  await log("info", `Scan complete. Tracking ${scan.count} tab(s)`);
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

// Whether a Stream Monitor (sm=1) tab on this streamer's page is tracked
// (A43): a monitored streamer, a streamer of a plan that runs here, or any
// Stream Monitor save-streak page. The desktop also opens save-streak
// pages for streamers it does not list, and Twitch strips sm=1 within
// seconds, so no later scan could adopt such a tab.
function adoptsStreamMonitorTab(streamer, url, monitoredStreamers, planStreamers) {
  return monitoredStreamers.has(streamer) || planStreamers.has(streamer) ||
    (isStreamMonitorTab(url) && SAVE_STREAK_URL_PATTERN.test(url));
}

async function onTabCreated(tab) {
  if (!tab.url) return;

  // Cheap URL checks first: this fires for every tab the user opens
  // anywhere in the browser, and the storage reads below are only needed
  // for Stream-Monitor-opened Twitch tabs (sm=1).
  const streamer = getStreamerFromUrl(tab.url);
  if (!streamer || !isStreamMonitorTab(tab.url)) return;

  const { monitoredStreamers } = await loadState();
  const planStreamers = await currentPlanStreamers();
  if (!adoptsStreamMonitorTab(streamer, tab.url, monitoredStreamers, planStreamers)) return;

  // A rescue open racing this event keeps its rescue flag so the
  // rotation's tab is never treated as a plain tracked tab; a plan open
  // (or a manual save open) racing it gets its marks the same way. The
  // opener already chose window and focus for both.
  const viaRescue = !!(await rescuePendingStreamerFor(tab.url));
  const pending = await slotPendingFor(tab.url);
  const viaSlot = !!pending;
  const tabKey = String(tab.id);
  const adopted = await withTrackedTabs((trackedTabs) => {
    if (trackedTabs[tabKey]) return false; // the opener or a racing event tracked it
    trackedTabs[tabKey] = adoptedTabEntry(streamer, tab.url, { viaRescue, pending });
    return true;
  });
  if (!adopted) return;
  // DESIGN 10.5: mute, place in the stream window, focus from the final
  // window, activate the player.
  const muted = await muteTabIfEnabled(tab.id, streamer);
  let windowId = tab.windowId;
  if (!viaRescue && !viaSlot) windowId = await placeInStreamWindow(tab);
  const focused = viaRescue || viaSlot ? false : await focusPlacedTab(tab, windowId);
  activatePlayerControlSoon(tab.id);
  scheduleBellCheckIfLoaded(tab.id);
  const moved = windowId !== tab.windowId ? " (moved to stream window)" : "";
  await log("info", `Tab ${tab.id} created for monitored streamer: ${streamer}${muted ? " (muted)" : ""}${focused ? " (focused)" : ""}${viaRescue ? " (rescue)" : ""}${viaSlot ? " (slot)" : ""}${moved}`);
}

async function onTabUpdated(tabId, changeInfo, tab) {
  // Fast path: this fires for every tab in the browser on every load /
  // favicon / title change. Skip the storage reads entirely when the
  // event can't concern us: no URL change AND the tab isn't on Twitch.
  // (URL-change events always proceed, because a tracked tab navigating
  // AWAY from Twitch needs untracking.)
  if (!changeInfo.url && !(tab && tab.url && tab.url.includes("twitch.tv"))) {
    return;
  }
  // After a browser start, nothing below may read the previous session's
  // entries before the init scan has sorted them out.
  if (!(await afterInitScan(tabId, changeInfo.url))) return;

  // Re-activate player control when a tracked tab finishes loading
  // (e.g. after a reload triggered by keepalive or error recovery)
  if (changeInfo.status === "complete") {
    const { trackedTabs } = await loadState();
    const completed = trackedTabs[String(tabId)];
    if (completed) {
      activatePlayerControlSoon(tabId);
      // Verify the page actually loaded. Browser error pages ("Server
      // Not Found") also report status complete but never run the
      // content script; catching that here gets the first recovery
      // reload out ~10s after the failed load instead of waiting for
      // the next keepalive tick.
      setTimeout(() => {
        checkTrackedTabLoaded(String(tabId), completed.originalStreamer);
      }, LOAD_VERIFY_AFTER_COMPLETE_MS);
      // The bell check on open (AUDIT B1), once the top bar has rendered.
      scheduleBellCheck(tabId);
    }
  }

  if (!changeInfo.url) return;

  const { trackedTabs, monitoredStreamers, pinnedStreamers } = await loadState();
  const tabKey = String(tabId);
  const newStreamer = getStreamerFromUrl(changeInfo.url);
  const tracked = trackedTabs[tabKey];

  // Read extension settings
  const settings = await chrome.storage.local.get(["extensionPaused", "raidFollowThrough", "maxTabs"]);
  const extensionPaused = settings.extensionPaused || false;
  const raidFollowThrough = settings.raidFollowThrough || false;
  const maxTabs = settings.maxTabs || 0; // 0 = unlimited

  if (tracked) {
    // This tab is being tracked
    const orig = tracked.originalStreamer;
    if (isSaveLanding(tracked, changeInfo.url) || isKnownSaveLanding(tracked, changeInfo.url)) {
      // A46: Twitch moved this save-streak page in place to a VOD, a clip,
      // the streamer's videos or, for a streak already kept, the channel
      // with a modal on top. That is the same save visit, so nothing below
      // applies (no untrack, no gone entry, no releaseLowQuality, no raid):
      // the tab stays tracked and its slot marker and turn go on. So does a
      // save turn the plan just flipped to live, still on its VOD until
      // sendSaveTurnToChannel's navigation lands (isKnownSaveLanding).
      await noteSaveLanding(tabKey, orig, changeInfo.url);
      return;
    }
    if (newStreamer && newStreamer !== orig &&
        SAVE_STREAK_URL_PATTERN.test(changeInfo.url)) {
      // Another streamer's save-streak page: the owner followed a "Save
      // your streak" link (bell card, sidebar entry) in this tab. A raid
      // lands on /<channel>, never here, so leave the tab open and stop
      // tracking it, the same as navigating away. With a plan running
      // here, the desktop gets it as the owner's move (navigated), not as
      // a lost tab it would reopen beside the page the owner just opened.
      await log("info", `Tab ${tabId} moved to ${newStreamer}'s save-streak page (not a raid), untracking`);
      const planHere = await planActiveHere();
      if (await untrackTab(tabKey, orig, planHere ? "navigated" : null)) {
        await cancelPendingSwapsForTab(tabKey);
        await cancelPendingExpirationForTab(tabKey);
        await clearLoadRecoveryForTab(tabKey);
        await handleRescueTabGone(tabKey);
        if (planHere) await afterSaveTurnLeft(tracked);
        if (tracked.manualSave) await settleManualSaveVisit(tracked);
      }
      await releaseLowQuality(tabId);
    } else if (newStreamer && newStreamer !== orig) {
      // URL changed to a different streamer: a raid. While a plan runs
      // here, follow-through is not applied to any tracked tab, and the
      // desktop gets the raid as a gone entry.
      const planHere = await planActiveHere();
      if (extensionPaused) {
        if (planHere) {
          // Paused with a plan here (A42): no close, since pause means no
          // closes. The tab is the owner's from now on: untracked, its
          // quality released, and the raid goes to the desktop with the
          // busy report, so the old streamer's entry never reads as
          // watched while the tab plays the raider.
          if (await untrackTab(tabKey, orig, "raid")) {
            await cancelPendingSwapsForTab(tabKey);
            await cancelPendingExpirationForTab(tabKey);
            await clearLoadRecoveryForTab(tabKey);
            await handleRescueTabGone(tabKey);
            if (tracked.manualSave) await settleManualSaveVisit(tracked);
          }
          await releaseLowQuality(tabId);
          await log("info", `Slot plan: raid ${orig} -> ${newStreamer} while paused; untracked tab ${tabId} and left it open`);
          return;
        }
        await log("info", `Raid detected: ${tracked.originalStreamer} -> ${newStreamer} (paused, not closing tab ${tabId})`);
        return;
      }

      if (raidFollowThrough && (tracked.raidHopCount || 0) === 0 && !planHere) {
        // Follow through on one raid: update tracking to the new streamer.
        // Reset openedAt so the new streamer gets a fresh grace window
        // instead of inheriting the displaced streamer's remaining time.
        // Not opened as the new streamer's save-streak page, and not a
        // Streaks at Risk visit any more: the old streamer's visit ends at
        // the hop, so a later close never settles the raider's row.
        const followed = await withTrackedTabs((t) => {
          const entry = t[tabKey];
          if (!entry || entry.originalStreamer !== orig) return null;
          const wasManual = !!entry.manualSave;
          entry.originalStreamer = newStreamer;
          entry.raidHopCount = 1;
          entry.openedAt = Date.now();
          delete entry.saveStreak;
          delete entry.manualSave;
          return { wasManual };
        });
        if (followed) {
          await log("info", `Raid follow-through: ${orig} -> ${newStreamer}, staying on tab ${tabId}`);
          if (followed.wasManual) await settleManualSaveVisit(tracked);
        }
        return;
      }

      await log("info", `Raid detected: ${tracked.originalStreamer} -> ${newStreamer}. Closing tab ${tabId}.`);
      const untracked = await untrackTab(tabKey, orig, planHere ? "raid" : null);
      await cancelPendingSwapsForTab(tabKey);
      await cancelPendingExpirationForTab(tabKey);
      await clearLoadRecoveryForTab(tabKey);
      // Untracked before the close, so onTabRemoved cannot settle it.
      if (untracked && tracked.manualSave) await settleManualSaveVisit(tracked);
      try {
        await chrome.tabs.remove(tabId);
      } catch (e) {
        await log("warn", `Failed to close tab ${tabId}:`, e.message);
      }
    } else if (!newStreamer) {
      // Navigated away from Twitch entirely
      await log("info", `Tab ${tabId} navigated away from Twitch, untracking`);
      const planHere = await planActiveHere();
      if (await untrackTab(tabKey, orig, planHere ? "navigated" : null)) {
        await cancelPendingSwapsForTab(tabKey);
        await cancelPendingExpirationForTab(tabKey);
        await clearLoadRecoveryForTab(tabKey);
        await handleRescueTabGone(tabKey);
        if (planHere) await afterSaveTurnLeft(tracked);
        if (tracked.manualSave) await settleManualSaveVisit(tracked);
      }
      await releaseLowQuality(tabId);
    }
  } else if (newStreamer && isStreamMonitorTab(changeInfo.url)) {
    // New navigation to a Stream Monitor (sm=1) page that is tracked:
    // see adoptsStreamMonitorTab.
    const planStreamers = await currentPlanStreamers();
    if (!adoptsStreamMonitorTab(newStreamer, changeInfo.url, monitoredStreamers, planStreamers)) return;
    const now = Date.now();
    // A rescue open racing this event keeps its rescue flag so the
    // rotation's tab is exempt from max-tabs displacement below; a plan
    // open (or a manual save open) racing it gets its marks the same way.
    // The opener already chose window and focus for both, and neither
    // triggers Max open streams.
    const viaRescue = !!(await rescuePendingStreamerFor(changeInfo.url));
    const pending = await slotPendingFor(changeInfo.url);
    const viaSlot = !!pending;
    const adopted = await withTrackedTabs((t) => {
      if (t[tabKey]) return false; // the opener or a racing event tracked it
      t[tabKey] = adoptedTabEntry(newStreamer, changeInfo.url, { viaRescue, pending });
      return true;
    });
    if (!adopted) return;
    // DESIGN 10.5: mute, place in the stream window, focus from the final
    // window, activate the player.
    const muted = await muteTabIfEnabled(tabId, newStreamer);
    let windowId = tab ? tab.windowId : undefined;
    if (!viaRescue && !viaSlot) windowId = await placeInStreamWindow(tab || { id: tabId });
    const focused = viaRescue || viaSlot ? false : await focusPlacedTab(tab || { id: tabId }, windowId);
    activatePlayerControlSoon(tabId);
    scheduleBellCheckIfLoaded(tabId);
    const moved = tab && windowId !== tab.windowId ? " (moved to stream window)" : "";
    await log("info", `Tab ${tabId} navigated to monitored streamer: ${newStreamer}${muted ? " (muted)" : ""}${focused ? " (focused)" : ""}${viaRescue ? " (rescue)" : ""}${viaSlot ? " (slot)" : ""}${moved}`);

    // While a plan runs here the plan is the cap, not Max open streams.
    if (maxTabs > 0 && !viaRescue && !viaSlot && !(await planActiveHere())) {
      await enforceMaxTabs(tabKey, newStreamer, maxTabs, pinnedStreamers, now);
    }
  }
}

// The tabs.onUpdated listener: the events of one tab are handled one after
// another, in the order they came. Twitch moves a page in place twice in a
// row (a save-streak link, then the clip, VOD or channel it lands on, A46),
// and a handler that read trackedTabs before the one for the first move
// wrote it took the second move for a raid and closed the owner's tab.
// Events of different tabs still run side by side. A handler that throws
// is logged and never stops the chain. In-memory by design: a recycle drops
// only the chains of handlers that died with it, and each handler reads
// storage afresh.
const tabUpdateChains = new Map(); // tabId -> the tail of its handler chain

function queueTabUpdated(tabId, changeInfo, tab) {
  const previous = tabUpdateChains.get(tabId) || Promise.resolve();
  const run = previous
    .then(() => onTabUpdated(tabId, changeInfo, tab))
    .catch((e) => {
      log("warn", `Tab ${tabId} update handling failed:`, e?.message || String(e)).catch(() => {});
    });
  tabUpdateChains.set(tabId, run);
  run.then(() => {
    if (tabUpdateChains.get(tabId) === run) tabUpdateChains.delete(tabId);
  });
  return run;
}

// A save visit that Twitch moved in place (A46). A VOD path is kept in the
// entry (landing), so the scan after a browser restart can re-adopt the
// restored tab, whose VOD URL names no streamer; any other page drops it.
async function noteSaveLanding(tabKey, streamer, url) {
  const landing = vodPathOf(url);
  const kept = await withTrackedTabs((trackedTabs) => {
    const entry = trackedTabs[tabKey];
    if (!entry || entry.originalStreamer !== streamer) return false;
    if (landing) entry.landing = landing;
    else delete entry.landing;
    return true;
  });
  if (!kept) return;
  let path = url;
  try {
    path = new URL(url).pathname;
  } catch (e) {
    // keep the raw URL
  }
  await log("info", `Save visit for ${streamer} (tab ${tabKey}) moved to ${path}; still tracked`);
}

// Max open streams, when a newly tracked tab goes over it: try to displace
// an open tab. Protected from displacement: pinned tabs (streamers the user
// marked "Keep Open" in settings), rescue tabs (the streak-rescue rotation
// manages their lifecycle itself), manual save tabs (a Streaks at Risk
// click) and slot tabs (the desktop's plan manages them). Core invariant:
// every newly-opened tab is guaranteed at least GRACE_MINUTES of viewing
// time so the viewer builds a Twitch view streak. The new tab is never
// closed immediately by max-tabs; the three options:
//   - an unprotected tab is past its grace window: close it immediately,
//     and the new tab keeps the slot;
//   - the only unprotected tabs are still in grace: schedule a pending
//     swap for the earliest grace expiry (both tabs stay open until then);
//   - everything else is protected: schedule a pending expiration on the
//     new tab at now + GRACE_MS so it still gets its 10 minutes before
//     closing.
async function enforceMaxTabs(tabKey, newStreamer, maxTabs, pinnedStreamers, now) {
  const decision = await withTrackedTabs((trackedTabs) => {
    if (!trackedTabs[tabKey]) return null; // closed meanwhile
    const tabCount = Object.keys(trackedTabs).length;
    if (tabCount <= maxTabs) return null;
    const candidates = Object.entries(trackedTabs).map(([k, info]) => {
      const openedAt = info.openedAt || 0;
      return {
        tabKey: k,
        streamer: info.originalStreamer,
        pinned: pinnedStreamers.has(info.originalStreamer),
        shielded: !!info.rescue || !!info.manualSave || typeof info.slot === "string",
        openedAt,
        graceUntil: openedAt + GRACE_MS,
        inGrace: now - openedAt < GRACE_MS,
      };
    });
    const others = candidates.filter(c => c.tabKey !== tabKey);
    // Unprotected tabs past their grace window are the first to close.
    // FIFO eviction (oldest first) so the longest-running tab cycles out
    // and newer ones get more time to build streak.
    const displaceable = others
      .filter(c => !c.pinned && !c.shielded && !c.inGrace)
      .sort((a, b) => a.openedAt - b.openedAt);
    // Unprotected but in grace: swap at the earliest expiry.
    const inGrace = others
      .filter(c => !c.pinned && !c.shielded && c.inGrace)
      .sort((a, b) => a.graceUntil - b.graceUntil);
    if (displaceable.length > 0) {
      const target = displaceable[0];
      delete trackedTabs[target.tabKey];
      return { kind: "close", target };
    }
    if (inGrace.length > 0) return { kind: "swap", target: inGrace[0] };
    return { kind: "expire" };
  });
  if (!decision) return;

  if (decision.kind === "close") {
    const target = decision.target;
    await log("info",
      `Max tabs (${maxTabs}) reached. Closing unpinned tab ${target.tabKey} (${target.streamer}) to make room for ${newStreamer}`
    );
    notifyUser(
      "Stream Monitor",
      `Max tabs (${maxTabs}) reached. Closed ${target.streamer} to open ${newStreamer}.`
    );
    await cancelPendingSwapsForTab(target.tabKey);
    await cancelPendingExpirationForTab(target.tabKey);
    try {
      await chrome.tabs.remove(Number(target.tabKey));
    } catch (e) {
      await log("warn", `Failed to close tab ${target.tabKey}:`, e.message);
    }
  } else if (decision.kind === "swap") {
    const target = decision.target;
    const minutesLeft = Math.max(1, Math.ceil((target.graceUntil - now) / 60000));
    await log("info",
      `Max tabs (${maxTabs}) reached but unpinned ${target.streamer} (tab ${target.tabKey}) is in grace (${minutesLeft}m left). Keeping both tabs open; swap scheduled.`
    );
    notifyUser(
      "Stream Monitor",
      `Protecting ${target.streamer}'s ${GRACE_MINUTES}-min streak. Will close their tab in ~${minutesLeft}m so ${newStreamer} keeps this slot.`
    );
    await schedulePendingSwap({
      newTabKey: tabKey,
      newStreamer,
      targetTabKey: target.tabKey,
      targetStreamer: target.streamer,
      scheduledAt: target.graceUntil,
    });
  } else {
    // Every other open tab is protected, so none is displaced. The new tab
    // still gets its 10-minute streak window before closing.
    await log("info",
      `Max tabs (${maxTabs}) reached and all open tabs are pinned or rescue-protected. Keeping ${newStreamer}'s tab open for ${GRACE_MINUTES}m to preserve streak, then closing.`
    );
    notifyUser(
      "Stream Monitor",
      `Max tabs (${maxTabs}) reached. All open streams are protected, so ${newStreamer}'s tab will close in ${GRACE_MINUTES} minutes after their streak is preserved.`
    );
    await schedulePendingExpiration(tabKey, newStreamer, now + GRACE_MS);
  }
}

// removeInfo.isWindowClosing tells a window close from a tab close. The
// entry is read and deleted in one step, and the tombstone and the gone
// entry are written inside it (lock order: trackedTabs, then slotState),
// so the report this save sends always carries the gone entry and the
// desktop never reads the close as a lost tab. After a browser start the
// handler first waits for the init scan (afterInitScan), outside the step:
// the scan's own trackedTabs step would otherwise queue behind it. A
// removal the scan's tab query no longer saw leaves the entry to the scan,
// which treats it as lost and re-adopts a restored copy (DESIGN 8.8).
async function onTabRemoved(tabId, removeInfo) {
  const tabKey = String(tabId);
  const windowClosing = !!(removeInfo && removeInfo.isWindowClosing);
  await afterInitScan(tabId, null);
  const removed = await withTrackedTabs(async (trackedTabs) => {
    const tracked = trackedTabs[tabKey];
    if (!tracked) return null;
    const streamer = String(tracked.originalStreamer || "").toLowerCase();
    if (windowClosing) await addWindowClosedTombstone(streamer, tracked.slot);
    const gone = (await planActiveHere()) ? (windowClosing ? "window_closed" : "user_closed") : null;
    if (gone) await appendGone(streamer, gone);
    delete trackedTabs[tabKey];
    return { tracked, gone };
  });
  const entry = removed ? removed.tracked : null;
  if (entry) await log("info", `Tab ${tabId} closed, untracking`);
  await cancelPendingSwapsForTab(tabKey);
  await cancelPendingExpirationForTab(tabKey);
  await clearLoadRecoveryForTab(tabKey);
  await withTabPlacement((map) => {
    delete map[tabKey];
  });
  await handleRescueTabGone(tabKey);
  // The Streaks at Risk request a closed tab served (plan 3.10, A15).
  if (entry && entry.manualSave) {
    await settleManualSaveVisit(entry);
  } else if (removed && removed.gone === "user_closed") {
    await afterSaveTurnLeft(entry);
  }
}

// Pause extension changed: tell the desktop at once, so a busy change does
// not wait for the next minute's report (the report says busy "paused"
// while a plan runs here).
function onStorageChanged(changes, area) {
  if (area !== "local" || !changes.extensionPaused) return;
  if (!!changes.extensionPaused.oldValue === !!changes.extensionPaused.newValue) return;
  reportOpenTabs("paused").catch(() => {});
}

async function onAlarm(alarm) {
  // An alarm that came due while the browser was closed fires as the worker
  // starts. Its work waits for the init scan: an earlier scan would drop the
  // previous session's entries before the init scan could re-adopt their
  // restored tabs (DESIGN 8.8), and a keepalive would ping tab ids that now
  // belong to other tabs (8.3 step 0).
  await initScanDone;
  if (alarm.name === CONFIG_ALARM) {
    await log("info", "Config refresh alarm fired");
    await fetchConfig();
    // The stream window's bounds, read once a minute (DESIGN 10.8).
    try {
      await refreshStreamWindowBounds();
    } catch (e) {
      await log("warn", "Stream window: bounds check failed:", e?.message || String(e));
    }
    // Re-scan tabs in case streamers list changed
    await scanExistingTabs();
    await reportOpenTabs("refresh");
  } else if (alarm.name === KEEPALIVE_ALARM) {
    // Send keepalive ping to all tracked tabs — this drives the content
    // script's keepalive from the background, avoiding browser throttling
    // of timers in background tabs.
    const { trackedTabs } = await loadState();
    const tabIds = Object.keys(trackedTabs);
    if (tabIds.length === 0) return;
    await log("info", `Keepalive alarm: pinging ${tabIds.length} tracked tab(s)`);
    for (const tabKey of tabIds) {
      // The alive-check doubles as the load-failure probe: a tab whose
      // page never loaded (browser error page, discarded tab) has no
      // content script to answer, and checkTrackedTabLoaded reloads it
      // with backoff. Dead tabs get no keepalive message; there is
      // nothing in them to keep alive.
      const alive = await checkTrackedTabLoaded(tabKey, trackedTabs[tabKey].originalStreamer);
      if (alive) {
        sendToContentScript(Number(tabKey), { action: "keepalive" });
      }
    }
  } else if (alarm.name === RESCUE_ROTATE_ALARM) {
    await log("info", "Rescue rotation alarm fired");
    await rotateRescue();
  } else if (alarm.name.startsWith(PENDING_SWAP_ALARM_PREFIX)) {
    const newTabKey = alarm.name.slice(PENDING_SWAP_ALARM_PREFIX.length);
    await log("info", `Pending swap alarm fired for tab ${newTabKey}`);
    await executePendingSwap(newTabKey);
  } else if (alarm.name.startsWith(PENDING_EXPIRE_ALARM_PREFIX)) {
    const tabKey = alarm.name.slice(PENDING_EXPIRE_ALARM_PREFIX.length);
    await log("info", `Pending expire alarm fired for tab ${tabKey}`);
    await executePendingExpiration(tabKey);
  }
}

// ---------------------------------------------------------------------------
// CRITICAL: Register all event listeners SYNCHRONOUSLY at the top level.
// This ensures Chrome re-wires them when the service worker wakes up.
// ---------------------------------------------------------------------------

chrome.tabs.onCreated.addListener(onTabCreated);
chrome.tabs.onUpdated.addListener(queueTabUpdated); // onTabUpdated, one event of a tab at a time
chrome.tabs.onRemoved.addListener(onTabRemoved);
chrome.alarms.onAlarm.addListener(onAlarm);
chrome.storage.onChanged.addListener(onStorageChanged);

// Open welcome page on first install only (not on updates)
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") });
  }
});

// ---------------------------------------------------------------------------
// Initialization: runs every time the service worker starts (or restarts)
// ---------------------------------------------------------------------------

// The order is DESIGN 8.7: the instance id, the browser-session marker, the
// config (stored, not acted on), the prunes, the tab scan, and only then
// the plan, so a browser restart never reads the previous session's dead
// entries as open tabs and the first report lists only tabs that exist.
(async () => {
  await log("info", "Service worker starting (init)");

  let afterBrowserStart = false;
  try {
    // 1. This profile's instance id before anything else.
    try {
      await getInstanceId();
    } catch (e) {
      await log("warn", "Instance id unavailable:", e?.message || String(e));
    }

    // 2. The browser-session marker. After a browser start the remembered
    // stream window id may name another window now (DESIGN 10.7 step 2).
    afterBrowserStart = await checkBrowserSession();
    await forgetStaleStreamWindowId();

    // The previous session's entries, before this worker adds any (the
    // scan's URL check applies to them only).
    let previousEntries = null;
    if (afterBrowserStart) {
      previousEntries = {};
      for (const [tabKey, entry] of Object.entries((await loadState()).trackedTabs)) {
        previousEntries[tabKey] = entry ? entry.openedAt : undefined;
      }
    }

    // 3. Fetch config from desktop app; the plan waits for the scan below.
    await fetchConfig({ applyPlan: false });

    // Ensure the periodic alarms exist (survive service worker termination)
    const existing = await chrome.alarms.get(CONFIG_ALARM);
    if (!existing) {
      chrome.alarms.create(CONFIG_ALARM, { periodInMinutes: CONFIG_INTERVAL_MINUTES });
      await log("info", `Created '${CONFIG_ALARM}' alarm (every ${CONFIG_INTERVAL_MINUTES} min)`);
    } else {
      await log("info", `'${CONFIG_ALARM}' alarm already exists`);
    }

    const existingKeepalive = await chrome.alarms.get(KEEPALIVE_ALARM);
    if (!existingKeepalive) {
      chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_INTERVAL_MINUTES });
      await log("info", `Created '${KEEPALIVE_ALARM}' alarm (every ${KEEPALIVE_INTERVAL_MINUTES} min)`);
    } else {
      await log("info", `'${KEEPALIVE_ALARM}' alarm already exists`);
    }

    // 4. Drop pending swaps whose tabs are gone; the alarms API persists
    // alarms across restarts but the tab IDs they reference may no longer
    // be valid.
    const liveTabs = await chrome.tabs.query({});
    const liveTabIds = new Set(liveTabs.map(t => String(t.id)));

    const pending = await loadPendingSwaps();
    if (pending.length > 0) {
      const alive = pending.filter(s => liveTabIds.has(s.newTabKey) && liveTabIds.has(s.targetTabKey));
      const dropped = pending.length - alive.length;
      if (dropped > 0) {
        await savePendingSwaps(alive);
        for (const s of pending) {
          if (!alive.includes(s)) {
            await chrome.alarms.clear(pendingSwapAlarmName(s.newTabKey));
          }
        }
        await log("info", `Dropped ${dropped} stale pending swap(s) on startup`);
      }
    }

    // Same cleanup for pending expirations.
    const expirations = await loadPendingExpirations();
    if (expirations.length > 0) {
      const alive = expirations.filter(e => liveTabIds.has(e.tabKey));
      const dropped = expirations.length - alive.length;
      if (dropped > 0) {
        await savePendingExpirations(alive);
        for (const e of expirations) {
          if (!alive.includes(e)) {
            await chrome.alarms.clear(pendingExpireAlarmName(e.tabKey));
          }
        }
        await log("info", `Dropped ${dropped} stale pending expiration(s) on startup`);
      }
    }

    // Same cleanup for load-recovery state.
    const recovery = await loadLoadRecovery();
    const staleRecovery = Object.keys(recovery).filter(k => !liveTabIds.has(k));
    if (staleRecovery.length > 0) {
      for (const k of staleRecovery) {
        delete recovery[k];
      }
      await saveLoadRecovery(recovery);
      await log("info", `Dropped ${staleRecovery.length} stale load-recovery entries on startup`);
    }

    // Plan opens that never became a tab, window-closed tombstones past
    // their use, and placement records of tabs that are gone.
    const now = Date.now();
    await withSlotState((state) => {
      for (const [login, open] of Object.entries(state.pendingOpens)) {
        if (!isPlainObject(open) || typeof open.at !== "number" || now - open.at >= SLOT_PENDING_OPEN_MAX_AGE_MS) {
          delete state.pendingOpens[login];
        }
      }
      state.windowClosed = state.windowClosed.filter(t => typeof t.at === "number" &&
        now - t.at < WINDOW_CLOSED_TOMBSTONE_MS);
    });
    // After a browser start (or an extension update, which also clears the
    // session marker) every record is from before: its tab and window ids
    // may name other tabs and windows now. Kept: an ownerPlaced record of a
    // tab whose entry the scan below keeps, because it still shows the
    // streamer it tracks (the scan's own test). The owner dragged that tab
    // where it is, and such a record never uses its window ids.
    const keptOwnerPlaced = new Set();
    if (afterBrowserStart) {
      const { trackedTabs } = await loadState();
      for (const t of liveTabs) {
        const entry = trackedTabs[String(t.id)];
        if (entry && entry.originalStreamer && tabShowsEntry(entry, t.url || t.pendingUrl)) {
          keptOwnerPlaced.add(String(t.id));
        }
      }
    }
    await withTabPlacement((map) => {
      for (const k of Object.keys(map)) {
        const keep = liveTabIds.has(k) &&
          (!afterBrowserStart || (isPlainObject(map[k]) && map[k].ownerPlaced === true && keptOwnerPlaced.has(k)));
        if (!keep) delete map[k];
      }
    });

    // 5. Reconcile tracked tabs with reality
    await scanExistingTabs({ afterBrowserStart, previousEntries });
  } finally {
    initScanFinished = true;
    resolveInitScan();
  }
  await reportOpenTabs("init");

  // Reconcile an in-flight rescue session: drop slots whose tabs are
  // gone (counting them as done), make sure the rotation alarm exists,
  // and refill open slots from the queue. The tab list is read inside the
  // step, so a slot another step opened just before is not taken for gone.
  // A plan running here ends the session instead (step 6).
  const plan = await loadSlotPlan();
  if (!(await planActiveHere(plan))) {
    const rescueActive = await withRescueSession(async () => {
      const rescueSession = await loadRescueSession();
      if (!rescueSession || !rescueSession.active) return false;
      const openTabIds = new Set((await chrome.tabs.query({})).map(t => String(t.id)));
      const gone = rescueSession.slots.filter(s => !openTabIds.has(s.tabKey));
      if (gone.length > 0) {
        rescueSession.slots = rescueSession.slots.filter(s => openTabIds.has(s.tabKey));
        rescueSession.rescued.push(...gone.map(s => s.streamer));
        await saveRescueSession(rescueSession);
        await log("info", `Rescue: reconciled ${gone.length} missing slot tab(s) on startup`);
      }
      return true;
    });
    if (rescueActive) {
      await ensureRescueAlarm();
      await topUpRescueSlots();
    }
  }

  // 6. The stored plan: applied when it runs here, else its markers are
  // released as fetchConfig would.
  await followSlotPlan(plan);

  await log("info", "Service worker ready");
})();
