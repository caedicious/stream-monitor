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
const CONFIG_INTERVAL_MINUTES = 1;
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

  // Add any Twitch tabs for monitored streamers that aren't tracked yet
  const twitchTabs = await browser.tabs.query({ url: "*://*.twitch.tv/*" });
  for (const tab of twitchTabs) {
    const streamer = getStreamerFromUrl(tab.url);
    if (streamer && monitoredStreamers.has(streamer) && !trackedTabs[String(tab.id)]) {
      trackedTabs[String(tab.id)] = { originalStreamer: streamer };
      const muted = await muteTabIfEnabled(tab.id);
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

  if (streamer && monitoredStreamers.has(streamer)) {
    trackedTabs[String(tab.id)] = { originalStreamer: streamer };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tab.id);
    await log("info", `Tab ${tab.id} created for monitored streamer: ${streamer}${muted ? " (muted)" : ""}`);
  }
}

async function onTabUpdated(tabId, changeInfo, _tab) {
  if (!changeInfo.url) return;

  const { trackedTabs, monitoredStreamers } = await loadState();
  const tabKey = String(tabId);
  const newStreamer = getStreamerFromUrl(changeInfo.url);
  const tracked = trackedTabs[tabKey];

  if (tracked) {
    // This tab is being tracked
    if (newStreamer && newStreamer !== tracked.originalStreamer) {
      // URL changed to a different streamer — raid detected
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
  } else if (newStreamer && monitoredStreamers.has(newStreamer)) {
    // New navigation to a monitored streamer in an untracked tab
    trackedTabs[tabKey] = { originalStreamer: newStreamer };
    await saveTrackedTabs(trackedTabs);
    const muted = await muteTabIfEnabled(tabId);
    await log("info", `Tab ${tabId} navigated to monitored streamer: ${newStreamer}${muted ? " (muted)" : ""}`);
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

// ---------------------------------------------------------------------------
// Initialization — runs every time the event page starts (or restarts)
// ---------------------------------------------------------------------------

(async () => {
  await log("info", "Event page starting (init)");

  // Fetch config from desktop app
  await fetchConfig();

  // Ensure the periodic alarm exists (survives suspension)
  const existing = await browser.alarms.get(CONFIG_ALARM);
  if (!existing) {
    browser.alarms.create(CONFIG_ALARM, { periodInMinutes: CONFIG_INTERVAL_MINUTES });
    await log("info", `Created '${CONFIG_ALARM}' alarm (every ${CONFIG_INTERVAL_MINUTES} min)`);
  } else {
    await log("info", `'${CONFIG_ALARM}' alarm already exists`);
  }

  // Reconcile tracked tabs with reality
  await scanExistingTabs();

  await log("info", "Event page ready");
})();
