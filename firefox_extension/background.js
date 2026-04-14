/**
 * Stream Monitor Tab Closer
 * Automatically closes Twitch tabs when the streamer raids someone else.
 *
 * Built for Manifest V3 event pages — all state is persisted to
 * browser.storage.local so it survives background page suspension.
 */

const CONFIG_URL = "http://127.0.0.1:52832/config";
const TWITCH_URL_PATTERN = /^https?:\/\/(?:www\.)?twitch\.tv\/([a-zA-Z0-9_]+)/;
const CONFIG_ALARM = "refresh-config";
const KEEPALIVE_ALARM = "keepalive";
const CONFIG_INTERVAL_MINUTES = 1;
const KEEPALIVE_INTERVAL_MINUTES = 2;
const MAX_LOG_ENTRIES = 200;

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
    const { debugLog = [] } = await browser.storage.local.get("debugLog");
    debugLog.push({ ts: new Date().toISOString(), level, msg });
    // Keep only the most recent entries
    if (debugLog.length > MAX_LOG_ENTRIES) {
      debugLog.splice(0, debugLog.length - MAX_LOG_ENTRIES);
    }
    await browser.storage.local.set({ debugLog });
  } catch (e) {
    console.error(prefix, "Failed to write debug log:", e);
  }
}

// ---------------------------------------------------------------------------
// State helpers — persist trackedTabs and monitoredStreamers to storage
// ---------------------------------------------------------------------------

async function loadState() {
  const { trackedTabs = {}, monitoredStreamers = [] } =
    await browser.storage.local.get(["trackedTabs", "monitoredStreamers"]);
  return {
    trackedTabs,                               // { [tabId]: { originalStreamer } }
    monitoredStreamers: new Set(monitoredStreamers), // Set<string>
  };
}

async function saveTrackedTabs(trackedTabs) {
  await browser.storage.local.set({ trackedTabs });
}

async function saveMonitoredStreamers(streamersSet) {
  await browser.storage.local.set({ monitoredStreamers: Array.from(streamersSet) });
}

async function shouldAutoMute() {
  const { autoMute = false } = await browser.storage.local.get("autoMute");
  return autoMute;
}

async function muteTabIfEnabled(tabId) {
  if (await shouldAutoMute()) {
    await browser.tabs.update(tabId, { muted: true });
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Content script messaging — send commands to Twitch tabs
// ---------------------------------------------------------------------------

async function sendToContentScript(tabId, message) {
  try {
    return await browser.tabs.sendMessage(tabId, message);
  } catch (e) {
    // Content script may not be loaded yet (e.g., tab still loading)
    await log("warn", `Failed to message tab ${tabId}:`, e.message);
    return null;
  }
}

async function activatePlayerControl(tabId) {
  await sendToContentScript(tabId, { action: "ensurePlaying" });

  const { lowQuality = false } = await browser.storage.local.get("lowQuality");
  if (lowQuality) {
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
    await browser.tabs.reload(tabId);
  } catch (e) {
    await log("warn", `Failed to reload tab ${tabId}:`, e.message);
  }
}

// Listen for error reports from content scripts
browser.runtime.onMessage.addListener((message, sender) => {
  if (message.action === "tabError" && sender.tab) {
    handleTabError(sender.tab.id);
  }
});

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
// Config fetching
// ---------------------------------------------------------------------------

async function fetchConfig() {
  // Check if host permission is granted
  const hasPerm = await browser.permissions.contains({ origins: ["http://127.0.0.1/*"] });
  await log("info", "Host permission granted:", hasPerm);

  try {
    // Use XMLHttpRequest — more reliable for cross-origin in Firefox extensions
    const data = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("GET", CONFIG_URL);
      xhr.responseType = "json";
      xhr.timeout = 5000;
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(xhr.response);
        } else {
          reject(new Error(`HTTP ${xhr.status}`));
        }
      };
      xhr.onerror = () => reject(new Error("XHR network error"));
      xhr.ontimeout = () => reject(new Error("XHR timeout"));
      xhr.send();
    });

    if (data?.streamers && Array.isArray(data.streamers)) {
      const streamersSet = new Set(data.streamers.map(s => s.toLowerCase()));
      await saveMonitoredStreamers(streamersSet);

      // Save live status from desktop app for popup display
      if (Array.isArray(data.live_streamers)) {
        await browser.storage.local.set({ liveStreamers: data.live_streamers });
      }

      await log("info", "Config loaded, streamers:", Array.from(streamersSet));
      return streamersSet;
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
    (await browser.tabs.query({})).map(t => String(t.id))
  );
  for (const tabId of Object.keys(trackedTabs)) {
    if (!existingTabIds.has(tabId)) {
      delete trackedTabs[tabId];
      changed = true;
    }
  }

  // Add Twitch tabs opened by Stream Monitor (sm=1) that aren't tracked yet
  const twitchTabs = await browser.tabs.query({ url: "*://*.twitch.tv/*" });
  for (const tab of twitchTabs) {
    const streamer = getStreamerFromUrl(tab.url);
    if (streamer && monitoredStreamers.has(streamer) && isStreamMonitorTab(tab.url) && !trackedTabs[String(tab.id)]) {
      trackedTabs[String(tab.id)] = { originalStreamer: streamer, raidHopCount: 0 };
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
    trackedTabs[String(tab.id)] = { originalStreamer: streamer, raidHopCount: 0 };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tab.id);
    // Delay slightly so the page has time to load the video player
    setTimeout(() => activatePlayerControl(tab.id), 3000);
    await log("info", `Tab ${tab.id} created for monitored streamer: ${streamer}${muted ? " (muted)" : ""}`);
  }
}

async function onTabUpdated(tabId, changeInfo, _tab) {
  if (!changeInfo.url) return;

  const { trackedTabs, monitoredStreamers } = await loadState();
  const tabKey = String(tabId);
  const newStreamer = getStreamerFromUrl(changeInfo.url);
  const tracked = trackedTabs[tabKey];

  // Read extension settings
  const settings = await browser.storage.local.get(["extensionPaused", "raidFollowThrough", "maxTabs"]);
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
        // Follow through on one raid — update tracking to new streamer
        tracked.originalStreamer = newStreamer;
        tracked.raidHopCount = 1;
        await saveTrackedTabs(trackedTabs);
        await log("info", `Raid follow-through: ${tracked.originalStreamer} -> ${newStreamer}, staying on tab ${tabId}`);
        return;
      }

      await log("info", `Raid detected: ${tracked.originalStreamer} -> ${newStreamer}. Closing tab ${tabId}.`);
      delete trackedTabs[tabKey];
      await saveTrackedTabs(trackedTabs);
      try {
        await browser.tabs.remove(tabId);
      } catch (e) {
        await log("warn", `Failed to close tab ${tabId}:`, e.message);
      }
    } else if (!newStreamer) {
      // Navigated away from Twitch entirely
      await log("info", `Tab ${tabId} navigated away from Twitch, untracking`);
      delete trackedTabs[tabKey];
      await saveTrackedTabs(trackedTabs);
    }
  } else if (newStreamer && monitoredStreamers.has(newStreamer) && isStreamMonitorTab(changeInfo.url)) {
    // New navigation to a monitored streamer opened by Stream Monitor (sm=1)
    trackedTabs[tabKey] = { originalStreamer: newStreamer, raidHopCount: 0 };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tabId);
    setTimeout(() => activatePlayerControl(tabId), 3000);
    await log("info", `Tab ${tabId} navigated to monitored streamer: ${newStreamer}${muted ? " (muted)" : ""}`);

    // Enforce max tabs
    if (maxTabs > 0) {
      const tabCount = Object.keys(trackedTabs).length;
      if (tabCount > maxTabs) {
        await log("info", `Max tabs (${maxTabs}) exceeded, closing newest tab ${tabId}`);
        delete trackedTabs[tabKey];
        await saveTrackedTabs(trackedTabs);
        try {
          await browser.tabs.remove(tabId);
        } catch (e) {
          await log("warn", `Failed to close excess tab ${tabId}:`, e.message);
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
  }
}

// ---------------------------------------------------------------------------
// CRITICAL: Register all event listeners SYNCHRONOUSLY at the top level.
// This ensures Firefox re-wires them when the event page wakes up.
// ---------------------------------------------------------------------------

browser.tabs.onCreated.addListener(onTabCreated);
browser.tabs.onUpdated.addListener(onTabUpdated);
browser.tabs.onRemoved.addListener(onTabRemoved);
browser.alarms.onAlarm.addListener(onAlarm);

// Open welcome page on first install only (not on updates)
browser.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    browser.tabs.create({ url: browser.runtime.getURL("welcome.html") });
  }
});

// ---------------------------------------------------------------------------
// Initialization — runs every time the event page starts (or restarts)
// ---------------------------------------------------------------------------

(async () => {
  await log("info", "Event page starting (init)");

  // Fetch config from desktop app
  await fetchConfig();

  // Ensure the periodic alarms exist (survive suspension)
  const existing = await browser.alarms.get(CONFIG_ALARM);
  if (!existing) {
    browser.alarms.create(CONFIG_ALARM, { periodInMinutes: CONFIG_INTERVAL_MINUTES });
    await log("info", `Created '${CONFIG_ALARM}' alarm (every ${CONFIG_INTERVAL_MINUTES} min)`);
  } else {
    await log("info", `'${CONFIG_ALARM}' alarm already exists`);
  }

  const existingKeepalive = await browser.alarms.get(KEEPALIVE_ALARM);
  if (!existingKeepalive) {
    browser.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_INTERVAL_MINUTES });
    await log("info", `Created '${KEEPALIVE_ALARM}' alarm (every ${KEEPALIVE_INTERVAL_MINUTES} min)`);
  } else {
    await log("info", `'${KEEPALIVE_ALARM}' alarm already exists`);
  }

  // Reconcile tracked tabs with reality
  await scanExistingTabs();

  await log("info", "Event page ready");
})();
