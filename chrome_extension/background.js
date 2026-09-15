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
// After a tracked tab reports status "complete", wait this long before
// verifying the content script is alive. Error pages report "complete"
// too, so this catches a failed open within seconds instead of waiting
// for the next keepalive tick. The delay gives document_idle injection
// time to happen on genuinely loading pages.
const LOAD_VERIFY_AFTER_COMPLETE_MS = 8000;

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

  const { trackedTabs } = await loadState();
  const target = trackedTabs[swap.targetTabKey];
  const stillTracking = target && target.originalStreamer === swap.targetStreamer;

  if (!stillTracking) {
    await log("info",
      `Pending swap fired but target ${swap.targetStreamer} (tab ${swap.targetTabKey}) is no longer tracked; slot already free`
    );
    return;
  }

  if (!trackedTabs[swap.newTabKey]) {
    await log("info",
      `Pending swap fired but new tab ${swap.newTabKey} (${swap.newStreamer}) is no longer tracked; nothing to preserve`
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

  delete trackedTabs[swap.targetTabKey];
  await saveTrackedTabs(trackedTabs);
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

  const { trackedTabs } = await loadState();
  const tracked = trackedTabs[tabKey];
  if (!tracked || tracked.originalStreamer !== exp.streamer) {
    await log("info",
      `Pending expiration fired but ${exp.streamer} (tab ${tabKey}) is no longer the tracked streamer; skipping`
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

  delete trackedTabs[tabKey];
  await saveTrackedTabs(trackedTabs);
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
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.action === "tabError" && sender.tab) {
    handleTabError(sender.tab.id);
  } else if (message.action === "reloadTab" && sender.tab) {
    reloadTrackedTab(sender.tab.id);
  } else if (message.type === "streak_event" && message.event) {
    forwardStreakEvent(message.event);
    persistAtRiskStreak(message.event);
  } else if (message.type === "ack_streak" && message.streamer) {
    acknowledgeAtRiskStreak(message.streamer).then(() => sendResponse({ ok: true }));
    return true; // async response
  } else if (message.type === "dismiss_streak" && message.streamer) {
    dismissAtRiskStreak(message.streamer).then(() => sendResponse({ ok: true }));
    return true;
  } else if (message.type === "clear_acknowledged_streaks") {
    clearAcknowledgedStreaks().then(() => sendResponse({ ok: true }));
    return true;
  }
});

// ---------------------------------------------------------------------------
// Streak event relay — POST to desktop's /streak_event so it can log and
// raise a tray notification. Best-effort: if the desktop app is not
// running the post will fail silently, which is the same behavior as the
// /config fetch on startup.
// ---------------------------------------------------------------------------

const STREAK_EVENT_URL = "http://127.0.0.1:52832/streak_event";

async function forwardStreakEvent(event) {
  try {
    const hasPerm = await chrome.permissions.contains({
      origins: ["http://127.0.0.1/*"],
    });
    if (!hasPerm) {
      await log("info", "Streak event detected but host permission not granted; skipping");
      return;
    }
    const resp = await fetch(STREAK_EVENT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) {
      await log("warn", `Streak event POST returned HTTP ${resp.status}`);
    } else {
      await log(
        "info",
        `Streak event reported: ${event.status} on ${event.streamer} (count=${event.count})`
      );
    }
  } catch (e) {
    // Desktop app not running, or network blocked. This is expected when
    // the extension is installed standalone without the companion app.
    await log("info", "Streak event POST failed (desktop app likely not running):", e.message);
  }
}

// ---------------------------------------------------------------------------
// At-risk streak state — persisted across extension restarts so the toolbar
// badge and the popup's "Streaks at Risk" section survive a service-worker
// suspend/resume cycle. Cleared when the user clicks the row (acknowledges)
// or when the entry expires (deadline + 4h buffer).
// ---------------------------------------------------------------------------

const ACK_EXPIRY_BUFFER_MS = 4 * 60 * 60 * 1000; // 4h after deadline -> drop
const STREAK_BADGE_COLOR = "#dc3545";

async function loadAtRiskStreaks() {
  const r = await chrome.storage.local.get("atRiskStreaks");
  return r.atRiskStreaks || {};
}

async function saveAtRiskStreaks(map) {
  await chrome.storage.local.set({ atRiskStreaks: map });
}

function _entryExpired(entry, now) {
  if (!entry || !entry.detected_at) return false;
  const detected = Date.parse(entry.detected_at);
  if (isNaN(detected)) return false;
  const deadlineHours = entry.deadline_hours || 24;
  return now - detected > deadlineHours * 3600 * 1000 + ACK_EXPIRY_BUFFER_MS;
}

async function pruneExpiredAtRiskStreaks() {
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

async function persistAtRiskStreak(event) {
  if (!event || !event.streamer || !event.status) return;
  const map = await pruneExpiredAtRiskStreaks();
  const key = event.streamer.toLowerCase();
  const existing = map[key];
  // Upgrade rule: a "broke" event always wins over a stale "in_danger",
  // and a newer detection replaces an older one of the same status.
  // Acknowledged entries get re-armed if a new event of the same kind
  // arrives (Twitch sometimes re-issues warnings as the deadline nears).
  map[key] = {
    streamer: key,
    status: event.status === "broke" ? "broke" : "in_danger",
    count: typeof event.count === "number" ? event.count : (existing?.count ?? 0),
    detected_at: event.detected_at || new Date().toISOString(),
    deadline_hours: typeof event.deadline_hours === "number"
      ? event.deadline_hours
      : (existing?.deadline_hours ?? 24),
    save_url: event.save_url || existing?.save_url || `https://www.twitch.tv/${key}`,
    acknowledged_at: null,
  };
  await saveAtRiskStreaks(map);
  await refreshStreakBadge(map);
}

async function acknowledgeAtRiskStreak(streamer) {
  if (!streamer) return;
  const key = streamer.toLowerCase();
  const map = await loadAtRiskStreaks();
  if (!map[key]) return;
  map[key].acknowledged_at = new Date().toISOString();
  await saveAtRiskStreaks(map);
  await refreshStreakBadge(map);
}

async function dismissAtRiskStreak(streamer) {
  if (!streamer) return;
  const key = streamer.toLowerCase();
  const map = await loadAtRiskStreaks();
  if (!map[key]) return;
  delete map[key];
  await saveAtRiskStreaks(map);
  await refreshStreakBadge(map);
}

async function clearAcknowledgedStreaks() {
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
}

async function refreshStreakBadge(mapOpt) {
  const map = mapOpt || await loadAtRiskStreaks();
  const unack = Object.values(map).filter((e) => !e.acknowledged_at).length;
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
refreshStreakBadge().catch(() => {});
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

async function maybeStartRescueFromConfig(rescueOffer) {
  if (!rescueOffer || !rescueOffer.id || !Array.isArray(rescueOffer.candidates)) return;
  let session = await loadRescueSession();
  if (session && Array.isArray(session.sourceIds) && session.sourceIds.includes(rescueOffer.id)) {
    return; // already absorbed this offer
  }

  // Acknowledge FIRST: the desktop only hands over ownership on a 204.
  // A 409 means the offer is stale (desktop already fell back, or another
  // browser profile claimed it) and we must not open anything.
  let acked = false;
  try {
    const resp = await fetch(RESCUE_ACK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: rescueOffer.id }),
      signal: AbortSignal.timeout(5000),
    });
    acked = resp.ok || resp.status === 204;
  } catch (e) {
    await log("warn", "Rescue ack POST failed:", e?.message || String(e));
  }
  if (!acked) return;

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
  const seen = new Set();
  const entries = [];
  for (const e of raw) {
    if (e.kind === "ended" && liveNow.has(e.streamer)) continue;
    if (seen.has(e.streamer)) continue;
    seen.add(e.streamer);
    entries.push(e);
  }

  if (!session || !session.active) {
    session = {
      active: true,
      sourceIds: [rescueOffer.id],
      queue: entries,
      slots: [],
      rescued: [],
      pendingOpens: {},
      sweepDone: false,
      startedAt: new Date().toISOString(),
    };
  } else {
    // A second offer arrived mid-session (the user went live again and
    // ended again). Merge new candidates, dedup against everything the
    // session already knows about.
    session.sourceIds.push(rescueOffer.id);
    const known = new Set([
      ...session.queue.map(e => e.streamer),
      ...session.slots.map(s => s.streamer),
      ...session.rescued,
    ]);
    for (const e of entries) {
      if (!known.has(e.streamer)) session.queue.push(e);
    }
    session.sweepDone = false; // new material, sweep again at next drain
  }
  await saveRescueSession(session);
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
  try {
    // Rescue tabs open in the background on purpose: the rotation fires
    // every 30 minutes and stealing focus each time would be obnoxious.
    // The player-control machinery (ensurePlaying button clicks,
    // keepalive, load recovery) is what makes an unfocused tab count,
    // same as any other tracked tab.
    tab = await chrome.tabs.create({ url, active: false });
  } catch (e) {
    await log("warn", `Rescue: failed to open tab for ${entry.streamer}:`, e?.message || String(e));
    delete session.pendingOpens[url];
    await saveRescueSession(session);
    return false;
  }

  const tabKey = String(tab.id);
  // Track directly with the rescue flag. Rescue targets may not be on the
  // monitored list at all (bell/sidebar finds), so URL-based tracking
  // would skip them; and the flag exempts the tab from max-tabs
  // displacement (its lifecycle belongs to the rotation).
  const { trackedTabs } = await loadState();
  const existing = trackedTabs[tabKey] || {};
  trackedTabs[tabKey] = {
    originalStreamer: entry.streamer,
    raidHopCount: existing.raidHopCount || 0,
    openedAt: Date.now(),
    rescue: true,
  };
  await saveTrackedTabs(trackedTabs);
  delete session.pendingOpens[url];
  session.slots.push({ tabKey, streamer: entry.streamer, openedAt: Date.now() });
  await muteTabIfEnabled(tab.id, entry.streamer);
  setTimeout(() => activatePlayerControl(tab.id), 3000);
  await log("info",
    `Rescue: opened ${entry.streamer} (${entry.kind}) in tab ${tabKey} (slot ${session.slots.length}/${RESCUE_BATCH_SIZE})`
  );
  return true;
}

async function topUpRescueSlots() {
  let session = await loadRescueSession();
  if (!session || !session.active) return;
  while (session.slots.length < RESCUE_BATCH_SIZE && session.queue.length > 0) {
    const entry = session.queue.shift();
    await openRescueTab(session, entry);
    await saveRescueSession(session);
    if (session.slots.length < RESCUE_BATCH_SIZE && session.queue.length > 0) {
      // Stagger consecutive opens so the players start cleanly. If the
      // service worker dies mid-stagger, the next config tick's top-up
      // resumes where this left off.
      await new Promise(r => setTimeout(r, RESCUE_OPEN_STAGGER_MS));
      session = await loadRescueSession();
      if (!session || !session.active) return;
    }
  }
}

// Sweep for leftover streak-rescue targets: the sidebar "Save your
// Streak" entry, /save-streak/ links anywhere in open Twitch tabs, and
// the extension's own bell-scraped at-risk store.
async function sweepForSaveStreakTargets(session) {
  const known = new Set([
    ...session.queue.map(e => e.streamer),
    ...session.slots.map(s => s.streamer),
    ...session.rescued,
  ]);
  const found = new Map();

  try {
    const map = await loadAtRiskStreaks();
    for (const e of Object.values(map)) {
      if (e && e.streamer && !e.acknowledged_at && !known.has(e.streamer) && !found.has(e.streamer)) {
        found.set(e.streamer, {
          streamer: e.streamer,
          url: e.save_url || `https://www.twitch.tv/save-streak/${e.streamer}`,
          kind: "ended",
        });
      }
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
      if (resp && Array.isArray(resp.slugs)) {
        for (const slug of resp.slugs) {
          const s = String(slug).toLowerCase();
          if (s && !known.has(s) && !found.has(s)) {
            found.set(s, {
              streamer: s,
              url: `https://www.twitch.tv/save-streak/${s}`,
              kind: "ended",
            });
          }
        }
      }
    }
  } catch (e) {
    await log("warn", "Rescue sweep: tab scan failed:", e?.message || String(e));
  }

  return Array.from(found.values());
}

async function rotateRescue() {
  const session = await loadRescueSession();
  if (!session || !session.active) {
    await chrome.alarms.clear(RESCUE_ROTATE_ALARM);
    return;
  }

  // Queue drained: sweep for stragglers before winding down.
  if (session.queue.length === 0 && !session.sweepDone) {
    const found = await sweepForSaveStreakTargets(session);
    if (found.length > 0) {
      session.queue.push(...found);
      await log("info", `Rescue sweep found ${found.length} additional streak target(s)`);
      notifyUser(
        "Stream Monitor",
        `Streak sweep found ${found.length} more stream(s) to rescue; continuing the rotation.`
      );
    } else {
      session.sweepDone = true;
      await log("info", "Rescue sweep found nothing further; winding down");
    }
    await saveRescueSession(session);
  }

  // The oldest slot's turn is over.
  if (session.slots.length > 0) {
    const oldest = session.slots.shift();
    session.rescued.push(oldest.streamer);
    await saveRescueSession(session);
    try {
      await chrome.tabs.remove(Number(oldest.tabKey));
      await log("info", `Rescue: closed ${oldest.streamer} (tab ${oldest.tabKey}) after its rotation turn`);
    } catch (e) {
      await log("warn", `Rescue: failed to close tab ${oldest.tabKey}:`, e?.message || String(e));
    }
  }

  await topUpRescueSlots();

  const after = await loadRescueSession();
  if (!after || !after.active) return;
  if (after.slots.length === 0 && after.queue.length === 0 && after.sweepDone) {
    await log("info", `Rescue session complete: ${after.rescued.length} stream(s) watched`);
    notifyUser("Stream Monitor", `Streak rescue complete: watched ${after.rescued.length} stream(s).`);
    await clearRescueSession();
  } else {
    await chrome.alarms.create(RESCUE_ROTATE_ALARM, { delayInMinutes: RESCUE_ROTATE_MINUTES });
  }
}

// A rescue tab vanished outside the rotation (user closed it, raid close,
// navigate-away untrack). Free the slot, count the streamer as done, and
// pull the next target forward.
async function handleRescueTabGone(tabKey) {
  const session = await loadRescueSession();
  if (!session || !session.active) return;
  const idx = session.slots.findIndex(s => s.tabKey === tabKey);
  if (idx === -1) return;
  const [slot] = session.slots.splice(idx, 1);
  session.rescued.push(slot.streamer);
  await saveRescueSession(session);
  await log("info", `Rescue: tab ${tabKey} (${slot.streamer}) closed externally; slot freed`);
  await topUpRescueSlots();
  const after = await loadRescueSession();
  if (after && after.active && after.slots.length === 0 && after.queue.length === 0 && after.sweepDone) {
    notifyUser("Stream Monitor", `Streak rescue complete: watched ${after.rescued.length} stream(s).`);
    await clearRescueSession();
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

// ---------------------------------------------------------------------------
// Config fetching — uses fetch() (XHR is not available in service workers)
// ---------------------------------------------------------------------------

async function fetchConfig() {
  const hasPerm = await chrome.permissions.contains({ origins: ["http://127.0.0.1/*"] });
  await log("info", "Host permission granted:", hasPerm);

  try {
    const response = await fetch(CONFIG_URL, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();

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

      // Streak-rescue offer from the desktop (published when the user's
      // own stream ends). Also use the tick to self-heal an active
      // session whose slots dropped below capacity.
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

      await log("info", `Config loaded: ${monitored.length} monitored, ${pinned.length} pinned`);
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

async function scanExistingTabs() {
  const { trackedTabs, monitoredStreamers } = await loadState();
  let changed = false;

  // Remove tracked tabs that no longer exist
  const existingTabIds = new Set(
    (await chrome.tabs.query({})).map(t => String(t.id))
  );
  for (const tabId of Object.keys(trackedTabs)) {
    if (!existingTabIds.has(tabId)) {
      delete trackedTabs[tabId];
      changed = true;
    }
  }

  // Add Twitch tabs opened by Stream Monitor (sm=1) that aren't tracked yet.
  // We don't know when the tab was originally opened, so stamp openedAt with
  // the current time. This conservatively gives a fresh grace window rather
  // than guessing a past timestamp.
  const twitchTabs = await chrome.tabs.query({ url: "*://*.twitch.tv/*" });
  for (const tab of twitchTabs) {
    const streamer = getStreamerFromUrl(tab.url);
    if (streamer && monitoredStreamers.has(streamer) && isStreamMonitorTab(tab.url) && !trackedTabs[String(tab.id)]) {
      trackedTabs[String(tab.id)] = { originalStreamer: streamer, raidHopCount: 0, openedAt: Date.now() };
      const muted = await muteTabIfEnabled(tab.id, streamer);
      activatePlayerControl(tab.id);
      await log("info", `Scan: tracking tab ${tab.id} for ${streamer}${muted ? " (muted)" : ""}`);
      changed = true;
    }
  }

  if (changed) await saveTrackedTabs(trackedTabs);
  await log("info", `Scan complete. Tracking ${Object.keys(trackedTabs).length} tab(s)`);
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

async function onTabCreated(tab) {
  if (!tab.url) return;

  // Cheap URL checks first — this fires for every tab the user opens
  // anywhere in the browser, and the storage read below is only needed
  // for Stream-Monitor-opened Twitch tabs (sm=1).
  const streamer = getStreamerFromUrl(tab.url);
  if (!streamer || !isStreamMonitorTab(tab.url)) return;

  const { trackedTabs, monitoredStreamers } = await loadState();

  if (monitoredStreamers.has(streamer)) {
    // A rescue open racing this event keeps its rescue flag so the
    // rotation's tab is never treated as a plain tracked tab.
    const viaRescue = !!(await rescuePendingStreamerFor(tab.url));
    trackedTabs[String(tab.id)] = {
      originalStreamer: streamer,
      raidHopCount: 0,
      openedAt: Date.now(),
      ...(viaRescue ? { rescue: true } : {}),
    };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tab.id, streamer);
    const focused = viaRescue ? false : await focusTabIfEnabled(tab);
    setTimeout(() => activatePlayerControl(tab.id), 3000);
    await log("info", `Tab ${tab.id} created for monitored streamer: ${streamer}${muted ? " (muted)" : ""}${focused ? " (focused)" : ""}${viaRescue ? " (rescue)" : ""}`);
  }
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

  // Re-activate player control when a tracked tab finishes loading
  // (e.g. after a reload triggered by keepalive or error recovery)
  if (changeInfo.status === "complete") {
    const { trackedTabs } = await loadState();
    const completed = trackedTabs[String(tabId)];
    if (completed) {
      setTimeout(() => activatePlayerControl(tabId), 3000);
      // Verify the page actually loaded. Browser error pages ("Server
      // Not Found") also report status complete but never run the
      // content script; catching that here gets the first recovery
      // reload out ~10s after the failed load instead of waiting for
      // the next keepalive tick.
      setTimeout(() => {
        checkTrackedTabLoaded(String(tabId), completed.originalStreamer);
      }, LOAD_VERIFY_AFTER_COMPLETE_MS);
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
    if (newStreamer && newStreamer !== tracked.originalStreamer) {
      // URL changed to a different streamer — raid detected
      if (extensionPaused) {
        await log("info", `Raid detected: ${tracked.originalStreamer} -> ${newStreamer} (paused, not closing tab ${tabId})`);
        return;
      }

      if (raidFollowThrough && (tracked.raidHopCount || 0) === 0) {
        // Follow through on one raid — update tracking to new streamer.
        // Reset openedAt so the new streamer gets a fresh grace window
        // instead of inheriting the displaced streamer's remaining time.
        tracked.originalStreamer = newStreamer;
        tracked.raidHopCount = 1;
        tracked.openedAt = Date.now();
        await saveTrackedTabs(trackedTabs);
        await log("info", `Raid follow-through: ${tracked.originalStreamer} -> ${newStreamer}, staying on tab ${tabId}`);
        return;
      }

      await log("info", `Raid detected: ${tracked.originalStreamer} -> ${newStreamer}. Closing tab ${tabId}.`);
      delete trackedTabs[tabKey];
      await saveTrackedTabs(trackedTabs);
      await cancelPendingSwapsForTab(tabKey);
      await cancelPendingExpirationForTab(tabKey);
      await clearLoadRecoveryForTab(tabKey);
      try {
        await chrome.tabs.remove(tabId);
      } catch (e) {
        await log("warn", `Failed to close tab ${tabId}:`, e.message);
      }
    } else if (!newStreamer) {
      // Navigated away from Twitch entirely
      await log("info", `Tab ${tabId} navigated away from Twitch, untracking`);
      delete trackedTabs[tabKey];
      await saveTrackedTabs(trackedTabs);
      await cancelPendingSwapsForTab(tabKey);
      await cancelPendingExpirationForTab(tabKey);
      await clearLoadRecoveryForTab(tabKey);
      await handleRescueTabGone(tabKey);
    }
  } else if (newStreamer && monitoredStreamers.has(newStreamer) && isStreamMonitorTab(changeInfo.url)) {
    // New navigation to a monitored streamer opened by Stream Monitor (sm=1)
    const now = Date.now();
    // A rescue open racing this event keeps its rescue flag so the
    // rotation's tab is exempt from max-tabs displacement below.
    const viaRescue = !!(await rescuePendingStreamerFor(changeInfo.url));
    trackedTabs[tabKey] = {
      originalStreamer: newStreamer,
      raidHopCount: 0,
      openedAt: now,
      ...(viaRescue ? { rescue: true } : {}),
    };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tabId, newStreamer);
    const focused = viaRescue ? false : await focusTabIfEnabled(tab);
    setTimeout(() => activatePlayerControl(tabId), 3000);
    await log("info", `Tab ${tabId} navigated to monitored streamer: ${newStreamer}${muted ? " (muted)" : ""}${focused ? " (focused)" : ""}${viaRescue ? " (rescue)" : ""}`);

    // Enforce max tabs: when at capacity, try to displace an unpinned
    // open tab. Pinned tabs (streamers the user marked "Keep Open" in
    // settings) are protected from displacement, and so are rescue tabs
    // (the streak-rescue rotation manages their lifecycle itself). A
    // rescue tab arriving is never the trigger either. Core invariant:
    // every newly-opened tab is guaranteed at least GRACE_MINUTES of
    // viewing time so the viewer builds a Twitch view streak. The new
    // tab is never closed immediately by max-tabs; the three options:
    //   - an unpinned tab is past its grace window → close it
    //     immediately, new tab keeps the slot;
    //   - the only unpinned tabs are still in grace → schedule a pending
    //     swap for the earliest grace expiry (both tabs stay open until);
    //   - everything else is pinned or rescue-protected → schedule a
    //     pending expiration on the new tab at now + GRACE_MS so it
    //     still gets its 10 minutes before closing.
    if (maxTabs > 0 && !viaRescue) {
      const tabCount = Object.keys(trackedTabs).length;
      if (tabCount > maxTabs) {
        const candidates = Object.entries(trackedTabs).map(([k, info]) => {
          const openedAt = info.openedAt || 0;
          return {
            tabKey: k,
            streamer: info.originalStreamer,
            pinned: pinnedStreamers.has(info.originalStreamer),
            rescue: !!info.rescue,
            openedAt,
            graceUntil: openedAt + GRACE_MS,
            inGrace: now - openedAt < GRACE_MS,
          };
        });

        const others = candidates.filter(c => c.tabKey !== tabKey);
        // Unpinned, non-rescue tabs past their grace window are the first
        // to close. FIFO eviction (oldest first) so the longest-running
        // tab cycles out and newer ones get more time to build streak.
        const displaceable = others
          .filter(c => !c.pinned && !c.rescue && !c.inGrace)
          .sort((a, b) => a.openedAt - b.openedAt);
        // Unpinned, non-rescue but in grace: swap at the earliest expiry.
        const shielded = others
          .filter(c => !c.pinned && !c.rescue && c.inGrace)
          .sort((a, b) => a.graceUntil - b.graceUntil);

        if (displaceable.length > 0) {
          const target = displaceable[0];
          await log("info",
            `Max tabs (${maxTabs}) reached. Closing unpinned tab ${target.tabKey} (${target.streamer}) to make room for ${newStreamer}`
          );
          notifyUser(
            "Stream Monitor",
            `Max tabs (${maxTabs}) reached. Closed ${target.streamer} to open ${newStreamer}.`
          );
          delete trackedTabs[target.tabKey];
          await saveTrackedTabs(trackedTabs);
          await cancelPendingSwapsForTab(target.tabKey);
          await cancelPendingExpirationForTab(target.tabKey);
          try {
            await chrome.tabs.remove(Number(target.tabKey));
          } catch (e) {
            await log("warn", `Failed to close tab ${target.tabKey}:`, e.message);
          }
        } else if (shielded.length > 0) {
          const target = shielded[0];
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
          // Every other open tab is pinned (Keep Open) or belongs to the
          // streak-rescue rotation, so we don't displace any of them.
          // The new tab still gets its 10-minute streak window before
          // closing.
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
    }
  }
}

async function onTabRemoved(tabId) {
  const { trackedTabs } = await loadState();
  const tabKey = String(tabId);
  if (trackedTabs[tabKey]) {
    delete trackedTabs[tabKey];
    await saveTrackedTabs(trackedTabs);
    await log("info", `Tab ${tabId} closed, untracking`);
  }
  await cancelPendingSwapsForTab(tabKey);
  await cancelPendingExpirationForTab(tabKey);
  await clearLoadRecoveryForTab(tabKey);
  await handleRescueTabGone(tabKey);
}

async function onAlarm(alarm) {
  if (alarm.name === CONFIG_ALARM) {
    await log("info", "Config refresh alarm fired");
    await fetchConfig();
    // Re-scan tabs in case streamers list changed
    await scanExistingTabs();
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
chrome.tabs.onUpdated.addListener(onTabUpdated);
chrome.tabs.onRemoved.addListener(onTabRemoved);
chrome.alarms.onAlarm.addListener(onAlarm);

// Open welcome page on first install only (not on updates)
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") });
  }
});

// ---------------------------------------------------------------------------
// Initialization — runs every time the service worker starts (or restarts)
// ---------------------------------------------------------------------------

(async () => {
  await log("info", "Service worker starting (init)");

  // Fetch config from desktop app
  await fetchConfig();

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

  // Reconcile tracked tabs with reality
  await scanExistingTabs();

  // Drop pending swaps whose tabs are gone; the alarms API persists alarms
  // across restarts but the tab IDs they reference may no longer be valid.
  const liveTabIds = new Set((await chrome.tabs.query({})).map(t => String(t.id)));

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

  // Reconcile an in-flight rescue session: drop slots whose tabs are
  // gone (counting them as done), make sure the rotation alarm exists,
  // and refill open slots from the queue.
  const rescueSession = await loadRescueSession();
  if (rescueSession && rescueSession.active) {
    const gone = rescueSession.slots.filter(s => !liveTabIds.has(s.tabKey));
    if (gone.length > 0) {
      rescueSession.slots = rescueSession.slots.filter(s => liveTabIds.has(s.tabKey));
      rescueSession.rescued.push(...gone.map(s => s.streamer));
      await saveRescueSession(rescueSession);
      await log("info", `Rescue: reconciled ${gone.length} missing slot tab(s) on startup`);
    }
    await ensureRescueAlarm();
    await topUpRescueSlots();
  }

  await log("info", "Service worker ready");
})();
