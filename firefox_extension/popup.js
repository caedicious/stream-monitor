// --- Settings ---
const autoMuteEl = document.getElementById("auto-mute");
const extensionPausedEl = document.getElementById("extension-paused");
const lowQualityEl = document.getElementById("low-quality");
const raidFollowEl = document.getElementById("raid-follow");
const maxTabsEl = document.getElementById("max-tabs");
const notificationsEl = document.getElementById("notifications-enabled");

async function loadSettings() {
  const result = await browser.storage.local.get([
    "autoMute", "extensionPaused", "lowQuality", "raidFollowThrough", "maxTabs"
  ]);
  autoMuteEl.checked = result.autoMute || false;
  extensionPausedEl.checked = result.extensionPaused || false;
  lowQualityEl.checked = result.lowQuality || false;
  raidFollowEl.checked = result.raidFollowThrough || false;
  maxTabsEl.value = result.maxTabs || 0;

  // Reflect the actual notifications permission state, not a stored
  // preference. The user may have revoked the permission via browser
  // settings, in which case the toggle should be off.
  try {
    notificationsEl.checked = await browser.permissions.contains({
      permissions: ["notifications"],
    });
  } catch (e) {
    notificationsEl.checked = false;
  }
}

autoMuteEl.addEventListener("change", async () => {
  await browser.storage.local.set({ autoMute: autoMuteEl.checked });
});

extensionPausedEl.addEventListener("change", async () => {
  await browser.storage.local.set({ extensionPaused: extensionPausedEl.checked });
});

lowQualityEl.addEventListener("change", async () => {
  await browser.storage.local.set({ lowQuality: lowQualityEl.checked });
});

raidFollowEl.addEventListener("change", async () => {
  await browser.storage.local.set({ raidFollowThrough: raidFollowEl.checked });
});

maxTabsEl.addEventListener("change", async () => {
  const val = Math.max(0, Math.min(20, parseInt(maxTabsEl.value) || 0));
  maxTabsEl.value = val;
  await browser.storage.local.set({ maxTabs: val });
});

notificationsEl.addEventListener("change", async () => {
  // permissions.request must be called from a user gesture. The change
  // event on a clicked checkbox qualifies, so we can request here.
  if (notificationsEl.checked) {
    let granted = false;
    try {
      granted = await browser.permissions.request({ permissions: ["notifications"] });
    } catch (e) {
      granted = false;
    }
    if (!granted) {
      // User dismissed the permission prompt; revert the toggle.
      notificationsEl.checked = false;
    }
  } else {
    try {
      await browser.permissions.remove({ permissions: ["notifications"] });
    } catch (e) {
      // ignore — toggle stays off
    }
  }
});

loadSettings();

// --- Streamer list with live status ---
async function loadStreamerList() {
  const { monitoredStreamers = [], liveStreamers = [] } =
    await browser.storage.local.get(["monitoredStreamers", "liveStreamers"]);

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
    item.className = "streamer-item";
    item.innerHTML =
      `<span class="status-dot ${isLive ? "live" : "offline"}"></span>` +
      `<span>${streamer}${isLive ? " (LIVE)" : ""}</span>`;
    listEl.appendChild(item);
  }
}

// --- Debug info ---
async function loadDebugInfo() {
  const { debugLog = [], trackedTabs = {} } =
    await browser.storage.local.get(["debugLog", "trackedTabs"]);

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
  await browser.storage.local.set({ debugLog: [] });
  refreshAll();
});

document.getElementById("open-full").addEventListener("click", (e) => {
  e.preventDefault();
  browser.tabs.create({ url: browser.runtime.getURL("debug.html") });
  window.close();
});

document.getElementById("credit-link").addEventListener("click", (e) => {
  e.preventDefault();
  browser.tabs.create({ url: "http://127.0.0.1:52832/about" });
  window.close();
});

refreshAll();
