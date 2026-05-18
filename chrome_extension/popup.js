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
  // Default ON for autoFocus — see shouldAutoFocus in background.js
  autoFocusEl.checked = result.autoFocusTabs ?? true;
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

// --- At-risk streaks ---
//
// Sort order:
//   1. Unacknowledged BROKE first (sorted by detected_at ascending so the
//      oldest is highest — least save-window remaining).
//   2. Unacknowledged IN_DANGER next (sorted by remaining deadline ascending
//      so the most urgent is highest).
//   3. Acknowledged entries last (they stay visible so the user can re-open
//      the save URL if they want, but greyed out).
function sortAtRiskStreaks(entries, now) {
  // Three priority tiers, smaller score = higher in the list:
  //   BROKE (unacked)     : score = detected_at ms (range ~1.7e12 in 2026)
  //   IN_DANGER (unacked) : score = 2e15 + ms-remaining (always > any broke)
  //   acknowledged        : score = 3e15 (sinks to the bottom)
  const score = (e) => {
    if (e.acknowledged_at) return 3e15;
    if (e.status === "broke") {
      // Earlier detection -> higher priority (least save-window remaining).
      return Date.parse(e.detected_at) || 0;
    }
    const detected = Date.parse(e.detected_at) || 0;
    const deadlineMs = detected + (e.deadline_hours || 24) * 3600 * 1000;
    return 2e15 + Math.max(0, deadlineMs - now);
  };
  return entries.slice().sort((a, b) => score(a) - score(b));
}

function formatDeadline(entry, now) {
  if (entry.status === "broke") {
    // Save window: 24h from detected_at
    const detected = Date.parse(entry.detected_at) || now;
    const hoursLeft = Math.max(
      0,
      Math.round((detected + 24 * 3600 * 1000 - now) / (3600 * 1000))
    );
    return hoursLeft > 0 ? `${hoursLeft}h to save` : "expired";
  }
  // in_danger: deadline = detected_at + deadline_hours
  const detected = Date.parse(entry.detected_at) || now;
  const hoursLeft = Math.max(
    0,
    Math.round(
      (detected + (entry.deadline_hours || 24) * 3600 * 1000 - now) /
        (3600 * 1000)
    )
  );
  return hoursLeft > 0 ? `${hoursLeft}h left` : "expiring";
}

async function loadAtRiskStreaks() {
  const r = await chrome.storage.local.get("atRiskStreaks");
  return r.atRiskStreaks || {};
}

async function renderAtRiskStreaks() {
  const map = await loadAtRiskStreaks();
  const section = document.getElementById("at-risk-section");
  const list = document.getElementById("at-risk-list");
  const entries = Object.values(map);
  if (entries.length === 0) {
    section.style.display = "none";
    list.innerHTML = "";
    return;
  }
  section.style.display = "block";
  const now = Date.now();
  const sorted = sortAtRiskStreaks(entries, now);
  list.innerHTML = "";
  for (const entry of sorted) {
    const item = document.createElement("div");
    item.className = "at-risk-item" + (entry.acknowledged_at ? " acknowledged" : "");
    item.title = entry.acknowledged_at
      ? `Re-open ${entry.streamer} in a new tab`
      : `Open ${entry.streamer} and acknowledge the streak alert`;

    const name = document.createElement("span");
    name.className = "at-risk-name";
    name.textContent = `${entry.streamer} (${entry.count})`;
    item.appendChild(name);

    const status = document.createElement("span");
    status.className = `at-risk-status ${entry.status}`;
    status.textContent = entry.status === "broke" ? "BROKE" : "IN DANGER";
    item.appendChild(status);

    const deadline = document.createElement("span");
    deadline.className = "at-risk-deadline";
    deadline.textContent = formatDeadline(entry, now);
    item.appendChild(deadline);

    item.addEventListener("click", () => openAtRiskStreak(entry));
    list.appendChild(item);
  }
}

async function openAtRiskStreak(entry) {
  const url = entry.save_url || `https://www.twitch.tv/${entry.streamer}`;
  try {
    await chrome.tabs.create({ url, active: true });
  } catch (e) {
    console.error("Failed to open at-risk streak URL:", e);
  }
  // Tell the background script to mark this one acknowledged. Don't await
  // the response before closing the popup; the storage write is best-effort.
  try {
    chrome.runtime.sendMessage({ type: "ack_streak", streamer: entry.streamer });
  } catch (_) {
    // ignore
  }
  window.close();
}

function refreshAll() {
  renderAtRiskStreaks();
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
