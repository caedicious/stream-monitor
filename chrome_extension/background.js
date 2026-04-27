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
const MAX_LOG_ENTRIES = 200;

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

const IGNORED_PATHS = new Set([
  "directory", "videos", "settings", "subscriptions",
  "inventory", "drops", "wallet",
]);

// ---------------------------------------------------------------------------
// Debug logging — writes to console AND a circular buffer in storage
// ---------------------------------------------------------------------------

async function log(level, ...args) {
  const msg = args.map(a => (typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
  const prefix = `[Stream Monitor]`;

  if (level === "error") console.error(prefix, ...args);
  else if (level === "warn") console.warn(prefix, ...args);
  else console.log(prefix, ...args);

  try {
    const result = await chrome.storage.local.get("debugLog");
    const debugLog = result.debugLog || [];
    debugLog.push({ ts: new Date().toISOString(), level, msg });
    // Keep only the most recent entries
    if (debugLog.length > MAX_LOG_ENTRIES) {
      debugLog.splice(0, debugLog.length - MAX_LOG_ENTRIES);
    }
    await chrome.storage.local.set({ debugLog });
  } catch (e) {
    console.error(prefix, "Failed to write debug log:", e);
  }
}

// ---------------------------------------------------------------------------
// State helpers — persist trackedTabs and monitoredStreamers to storage
// ---------------------------------------------------------------------------

async function loadState() {
  const result = await chrome.storage.local.get(["trackedTabs", "monitoredStreamers"]);
  // monitoredStreamers is stored as an ordered array. The array position
  // encodes priority: index 0 = rank #1, index 1 = rank #2, etc. This drives
  // the max-tabs displacement logic below.
  const raw = result.monitoredStreamers;
  const ordered = Array.isArray(raw) ? raw : [];
  return {
    trackedTabs: result.trackedTabs || {},
    monitoredStreamers: new Set(ordered), // fast membership check
    orderedStreamers: ordered,            // priority order (index 0 = rank #1)
  };
}

async function saveTrackedTabs(trackedTabs) {
  await chrome.storage.local.set({ trackedTabs });
}

async function saveMonitoredStreamers(orderedList) {
  await chrome.storage.local.set({ monitoredStreamers: orderedList });
}

// Rank helpers. A lower rank number = higher priority. Streamers no longer
// in the monitored list (e.g. user removed them but their tab is still open)
// sort last.
function rankOf(name, orderedStreamers) {
  const idx = orderedStreamers.indexOf(name);
  return idx === -1 ? Infinity : idx;
}

function rankLabel(rank) {
  return rank === Infinity ? "unranked" : `#${rank + 1}`;
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

async function shouldAutoMute() {
  const result = await chrome.storage.local.get("autoMute");
  return result.autoMute || false;
}

async function muteTabIfEnabled(tabId) {
  if (await shouldAutoMute()) {
    await chrome.tabs.update(tabId, { muted: true });
    return true;
  }
  return false;
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
  }
});

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
// Utility
// ---------------------------------------------------------------------------

function getStreamerFromUrl(url) {
  const match = url?.match(TWITCH_URL_PATTERN);
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
      // Preserve list order from the desktop app: position = priority rank.
      const ordered = data.streamers.map(s => s.toLowerCase());
      await saveMonitoredStreamers(ordered);

      // Save live status from desktop app for popup display
      if (Array.isArray(data.live_streamers)) {
        await chrome.storage.local.set({ liveStreamers: data.live_streamers });
      }

      await log("info", "Config loaded, streamers:", ordered);
      return new Set(ordered);
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
      const muted = await muteTabIfEnabled(tab.id);
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

  const { trackedTabs, monitoredStreamers } = await loadState();
  const streamer = getStreamerFromUrl(tab.url);

  if (streamer && monitoredStreamers.has(streamer) && isStreamMonitorTab(tab.url)) {
    trackedTabs[String(tab.id)] = { originalStreamer: streamer, raidHopCount: 0, openedAt: Date.now() };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tab.id);
    setTimeout(() => activatePlayerControl(tab.id), 3000);
    await log("info", `Tab ${tab.id} created for monitored streamer: ${streamer}${muted ? " (muted)" : ""}`);
  }
}

async function onTabUpdated(tabId, changeInfo, _tab) {
  // Re-activate player control when a tracked tab finishes loading
  // (e.g. after a reload triggered by keepalive or error recovery)
  if (changeInfo.status === "complete") {
    const { trackedTabs } = await loadState();
    if (trackedTabs[String(tabId)]) {
      setTimeout(() => activatePlayerControl(tabId), 3000);
    }
  }

  if (!changeInfo.url) return;

  const { trackedTabs, monitoredStreamers, orderedStreamers } = await loadState();
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
    }
  } else if (newStreamer && monitoredStreamers.has(newStreamer) && isStreamMonitorTab(changeInfo.url)) {
    // New navigation to a monitored streamer opened by Stream Monitor (sm=1)
    const now = Date.now();
    trackedTabs[tabKey] = { originalStreamer: newStreamer, raidHopCount: 0, openedAt: now };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tabId);
    setTimeout(() => activatePlayerControl(tabId), 3000);
    await log("info", `Tab ${tabId} navigated to monitored streamer: ${newStreamer}${muted ? " (muted)" : ""}`);

    // Enforce max tabs: when at capacity, try to displace the lowest-
    // priority open tab. Priority comes from the order of the streamer
    // list in the desktop app's config; the first streamer is rank #1
    // and so on. Core invariant (v1.5.1.1): every newly-opened monitored
    // tab is guaranteed at least GRACE_MINUTES of viewing time so the
    // viewer can build a Twitch view streak. That means we never close
    // the new tab immediately here; the only options are:
    //   - displace an existing tab with strictly-worse rank (new tab
    //     keeps its slot indefinitely);
    //   - all worse-ranked tabs are in grace → schedule a pending swap
    //     for the earliest expiring one (new tab keeps its slot, target
    //     closes when its grace ends);
    //   - no existing tab has strictly-worse rank than new (new is the
    //     lowest or tied-lowest priority) → schedule a pending
    //     expiration for new at now + GRACE_MS so it gets a full
    //     10-minute viewing window, then auto-closes.
    if (maxTabs > 0) {
      const tabCount = Object.keys(trackedTabs).length;
      if (tabCount > maxTabs) {
        const newRank = rankOf(newStreamer, orderedStreamers);

        const candidates = Object.entries(trackedTabs).map(([k, info]) => {
          const openedAt = info.openedAt || 0;
          return {
            tabKey: k,
            streamer: info.originalStreamer,
            rank: rankOf(info.originalStreamer, orderedStreamers),
            openedAt,
            graceUntil: openedAt + GRACE_MS,
            inGrace: now - openedAt < GRACE_MS,
          };
        });

        const others = candidates.filter(c => c.tabKey !== tabKey);
        // Displaceable: past grace AND worse-ranked than new. Worst-first.
        const displaceable = others
          .filter(c => !c.inGrace && c.rank > newRank)
          .sort((a, b) => b.rank - a.rank);
        // Shielded: in grace AND worse-ranked than new. Earliest-grace-first.
        const shielded = others
          .filter(c => c.inGrace && c.rank > newRank)
          .sort((a, b) => a.graceUntil - b.graceUntil);

        if (displaceable.length > 0) {
          const target = displaceable[0];
          await log("info",
            `Max tabs (${maxTabs}) reached. Displacing ${rankLabel(target.rank)} ${target.streamer} (tab ${target.tabKey}) to make room for ${rankLabel(newRank)} ${newStreamer}`
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
            `Max tabs (${maxTabs}) reached but ${rankLabel(target.rank)} ${target.streamer} (tab ${target.tabKey}) is in grace (${minutesLeft}m left). Keeping both tabs open; swap scheduled.`
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
          // No existing tab has strictly-worse rank than new. New is the
          // lowest-priority live streamer (or tied-lowest, e.g. duplicate
          // streamer tabs or multiple unranked tabs). Give new its
          // guaranteed 10-minute viewing window, then auto-close.
          await log("info",
            `Max tabs (${maxTabs}) reached. ${rankLabel(newRank)} ${newStreamer} is the lowest priority; keeping tab open for ${GRACE_MINUTES}m to preserve streak, then closing.`
          );
          notifyUser(
            "Stream Monitor",
            `Max tabs (${maxTabs}) reached. Keeping ${newStreamer}'s tab open for ${GRACE_MINUTES} minutes to preserve their streak, then closing to free the slot.`
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
    for (const tabId of tabIds) {
      sendToContentScript(Number(tabId), { action: "keepalive" });
    }
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

  await log("info", "Service worker ready");
})();
