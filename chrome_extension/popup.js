// --- Settings ---
const autoMuteEl = document.getElementById("auto-mute");
const autoFocusEl = document.getElementById("auto-focus");
const extensionPausedEl = document.getElementById("extension-paused");
const lowQualityEl = document.getElementById("low-quality");
const raidFollowEl = document.getElementById("raid-follow");
const maxTabsEl = document.getElementById("max-tabs");
const notificationsEl = document.getElementById("notifications-enabled");

async function loadSettings() {
  const result = await chrome.storage.local.get([
    "autoMute", "autoFocusTabs", "extensionPaused", "lowQuality", "raidFollowThrough", "maxTabs"
  ]);
  autoMuteEl.checked = result.autoMute || false;
  autoFocusEl.checked = result.autoFocusTabs || false;
  extensionPausedEl.checked = result.extensionPaused || false;
  lowQualityEl.checked = result.lowQuality || false;
  raidFollowEl.checked = result.raidFollowThrough || false;
  maxTabsEl.value = result.maxTabs || 0;

  // Reflect the actual notifications permission state, not a stored
  // preference. The user may have revoked the permission via browser
  // settings, in which case the toggle should be off.
  try {
    notificationsEl.checked = await chrome.permissions.contains({
      permissions: ["notifications"],
    });
  } catch (e) {
    notificationsEl.checked = false;
  }
}

autoMuteEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ autoMute: autoMuteEl.checked });
});

autoFocusEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ autoFocusTabs: autoFocusEl.checked });
});

extensionPausedEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ extensionPaused: extensionPausedEl.checked });
});

lowQualityEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ lowQuality: lowQualityEl.checked });
});

raidFollowEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ raidFollowThrough: raidFollowEl.checked });
});

maxTabsEl.addEventListener("change", async () => {
  const val = Math.max(0, Math.min(20, parseInt(maxTabsEl.value) || 0));
  maxTabsEl.value = val;
  await chrome.storage.local.set({ maxTabs: val });
});

notificationsEl.addEventListener("change", async () => {
  // permissions.request must be called from a user gesture. The change
  // event on a clicked checkbox qualifies, so we can request here.
  if (notificationsEl.checked) {
    let granted = false;
    try {
      granted = await chrome.permissions.request({ permissions: ["notifications"] });
    } catch (e) {
      granted = false;
    }
    if (!granted) {
      // User dismissed the permission prompt; revert the toggle.
      notificationsEl.checked = false;
    }
  } else {
    try {
      await chrome.permissions.remove({ permissions: ["notifications"] });
    } catch (e) {
      // ignore — toggle stays off
    }
  }
});

loadSettings();

// --- Streamer list with live status ---
async function loadStreamerList() {
  const result = await chrome.storage.local.get(["monitoredStreamers", "liveStreamers"]);
  const monitoredStreamers = result.monitoredStreamers || [];
  const liveStreamers = result.liveStreamers || [];

  const listEl = document.getElementById("streamer-list");
  if (monitoredStreamers.length === 0) {
    listEl.textContent = "(no streamers monitored)";
    return;
  }

  const liveSet = new Set(liveStreamers.map(s => s.toLowerCase()));
  listEl.innerHTML = "";
  for (const streamer of monitoredStreamers) {
    const isLive = liveSet.has(streamer.toLowerCase());
    const item = document.createElement("div");
    item.className = "streamer-item clickable";
    item.title = isLive
      ? `Open ${streamer}'s stream (or focus the existing tab)`
      : `Open ${streamer}'s channel page`;
    const dot = document.createElement("span");
    dot.className = `status-dot ${isLive ? "live" : "offline"}`;
    item.appendChild(dot);
    const label = document.createElement("span");
    label.textContent = isLive ? `${streamer} (LIVE)` : streamer;
    item.appendChild(label);
    item.addEventListener("click", () => openOrFocusStreamer(streamer.toLowerCase()));
    listEl.appendChild(item);
  }
}

// Click handler for the streamer list. Always opens a plain Twitch URL
// (no ?sm=1) so the extension's auto-mute, low-quality, max-tabs, and
// raid-close behaviors don't apply — the user clicked because they want
// to actually watch this streamer, not background-monitor them. Existing
// tracked tabs are deliberately not reused for the same reason; their
// player has already been muted/lowered for background viewing.
async function openOrFocusStreamer(name) {
  try {
    await chrome.tabs.create({ url: `https://www.twitch.tv/${name}`, active: true });
    window.close();
  } catch (e) {
    console.error("Failed to open streamer:", e);
  }
}

// --- Debug info ---
async function loadDebugInfo() {
  const result = await chrome.storage.local.get(["debugLog", "trackedTabs"]);
  const debugLog = result.debugLog || [];
  const trackedTabs = result.trackedTabs || {};

  const stateEl = document.getElementById("state");
  stateEl.textContent = `Tracked tabs: ${JSON.stringify(trackedTabs, null, 2)}`;

  const logEl = document.getElementById("log");
  if (debugLog.length === 0) {
    logEl.textContent = "(no log entries)";
    return;
  }

  logEl.innerHTML = "";
  // Show newest first, limit to last 50 in popup
  const entries = debugLog.slice(-50).reverse();
  for (const entry of entries) {
    const div = document.createElement("div");
    div.className = `log-entry ${entry.level}`;
    const time = entry.ts.split("T")[1]?.replace("Z", "") || entry.ts;
    div.textContent = `[${time}] ${entry.msg}`;
    logEl.appendChild(div);
  }
}

function refreshAll() {
  loadStreamerList();
  loadDebugInfo();
}

document.getElementById("refresh").addEventListener("click", refreshAll);

document.getElementById("clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ debugLog: [] });
  refreshAll();
});

document.getElementById("open-full").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: chrome.runtime.getURL("debug.html") });
  window.close();
});

document.getElementById("credit-link").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "http://127.0.0.1:52832/about" });
  window.close();
});

refreshAll();
