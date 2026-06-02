// --- Settings ---
const autoMuteEl = document.getElementById("auto-mute");
const autoFocusEl = document.getElementById("auto-focus");
const extensionPausedEl = document.getElementById("extension-paused");
const lowQualityEl = document.getElementById("low-quality");
const raidFollowEl = document.getElementById("raid-follow");
const maxTabsEl = document.getElementById("max-tabs");
const notificationsEl = document.getElementById("notifications-enabled");

async function loadSettings() {
  const result = await browser.storage.local.get([
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

autoFocusEl.addEventListener("change", async () => {
  await browser.storage.local.set({ autoFocusTabs: autoFocusEl.checked });
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

// Click handler for the streamer list. Always opens a plain Twitch URL
// (no ?sm=1) so the extension's auto-mute, low-quality, max-tabs, and
// raid-close behaviors don't apply — the user clicked because they want
// to actually watch this streamer, not background-monitor them. Existing
// tracked tabs are deliberately not reused for the same reason; their
// player has already been muted/lowered for background viewing.
async function openOrFocusStreamer(name) {
  try {
    await browser.tabs.create({ url: `https://www.twitch.tv/${name}`, active: true });
    window.close();
  } catch (e) {
    console.error("Failed to open streamer:", e);
  }
}

// --- Streamer list with live status ---
async function loadStreamerList() {
  const {
    monitoredStreamers = [],
    liveStreamers = [],
    muteExemptStreamers = [],
    autoMute = false,
  } = await browser.storage.local.get([
    "monitoredStreamers",
    "liveStreamers",
    "muteExemptStreamers",
    "autoMute",
  ]);

  const listEl = document.getElementById("streamer-list");
  if (monitoredStreamers.length === 0) {
    listEl.textContent = "(no streamers monitored)";
    return;
  }

  const muteExempt = new Set(
    (Array.isArray(muteExemptStreamers) ? muteExemptStreamers : []).map(s =>
      String(s).toLowerCase()
    )
  );
  const liveSet = new Set(liveStreamers.map(s => s.toLowerCase()));
  listEl.innerHTML = "";
  for (const streamer of monitoredStreamers) {
    const slug = streamer.toLowerCase();
    const isLive = liveSet.has(slug);
    const isExempt = muteExempt.has(slug);
    const item = document.createElement("div");
    item.className = "streamer-item clickable";
    item.title = isLive
      ? `Open ${streamer}'s stream`
      : `Open ${streamer}'s channel page`;
    const dot = document.createElement("span");
    dot.className = `status-dot ${isLive ? "live" : "offline"}`;
    item.appendChild(dot);
    const label = document.createElement("span");
    label.textContent = isLive ? `${streamer} (LIVE)` : streamer;
    item.appendChild(label);

    const muteBtn = document.createElement("button");
    muteBtn.className = "streamer-mute-toggle" + (isExempt ? " exempt" : "");
    muteBtn.textContent = isExempt ? "\u{1F50A}" : "\u{1F507}";
    muteBtn.title = isExempt
      ? `Currently EXEMPT from auto-mute. Click to mute ${streamer}'s tabs again.`
      : autoMute
        ? `Currently auto-muted. Click to exempt ${streamer} (audio allowed).`
        : `Auto-mute is off globally. Click to mark ${streamer} as always-exempt.`;
    muteBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      toggleMuteExemption(slug);
    });
    item.appendChild(muteBtn);

    item.addEventListener("click", () => openOrFocusStreamer(slug));
    listEl.appendChild(item);
  }
}

async function toggleMuteExemption(streamer) {
  const slug = streamer.toLowerCase();
  const { muteExemptStreamers = [] } = await browser.storage.local.get("muteExemptStreamers");
  const current = new Set(
    (Array.isArray(muteExemptStreamers) ? muteExemptStreamers : []).map(s =>
      String(s).toLowerCase()
    )
  );
  const nowExempt = !current.has(slug);
  if (nowExempt) current.add(slug);
  else current.delete(slug);
  await browser.storage.local.set({ muteExemptStreamers: Array.from(current) });

  try {
    const { autoMute = false } = await browser.storage.local.get("autoMute");
    const tabs = await browser.tabs.query({ url: "*://*.twitch.tv/*" });
    const match = new RegExp(`^https?://(?:www\\.)?twitch\\.tv/${slug}(?:[/?#]|$)`, "i");
    for (const t of tabs) {
      if (!t.url || !match.test(t.url)) continue;
      if (nowExempt) {
        await browser.tabs.update(t.id, { muted: false });
      } else if (autoMute) {
        await browser.tabs.update(t.id, { muted: true });
      }
    }
  } catch (e) {
    console.warn("Failed to apply mute exemption to open tabs:", e);
  }

  await loadStreamerList();
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

// --- At-risk streaks ---
function sortAtRiskStreaks(entries, now) {
  // Three priority tiers, smaller score = higher in the list:
  //   BROKE (unacked)     : score = detected_at ms (range ~1.7e12 in 2026)
  //   IN_DANGER (unacked) : score = 2e15 + ms-remaining (always > any broke)
  //   acknowledged        : score = 3e15 (sinks to the bottom)
  const score = (e) => {
    if (e.acknowledged_at) return 3e15;
    if (e.status === "broke") {
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
    const detected = Date.parse(entry.detected_at) || now;
    const hoursLeft = Math.max(
      0,
      Math.round((detected + 24 * 3600 * 1000 - now) / (3600 * 1000))
    );
    return hoursLeft > 0 ? `${hoursLeft}h to save` : "expired";
  }
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
  const r = await browser.storage.local.get("atRiskStreaks");
  return r.atRiskStreaks || {};
}

async function renderAtRiskStreaks() {
  const map = await loadAtRiskStreaks();
  const section = document.getElementById("at-risk-section");
  const list = document.getElementById("at-risk-list");
  const clearAckedEl = document.getElementById("at-risk-clear-acked");
  const entries = Object.values(map);
  if (entries.length === 0) {
    section.style.display = "none";
    list.innerHTML = "";
    if (clearAckedEl) clearAckedEl.style.display = "none";
    return;
  }
  section.style.display = "block";
  const now = Date.now();
  const sorted = sortAtRiskStreaks(entries, now);
  list.innerHTML = "";
  let anyAcked = false;
  for (const entry of sorted) {
    if (entry.acknowledged_at) anyAcked = true;
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

    const dismiss = document.createElement("button");
    dismiss.className = "at-risk-dismiss";
    dismiss.textContent = "×";
    dismiss.title = `Dismiss ${entry.streamer} from this list`;
    dismiss.addEventListener("click", (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      dismissAtRiskStreak(entry.streamer);
    });
    item.appendChild(dismiss);

    item.addEventListener("click", () => openAtRiskStreak(entry));
    list.appendChild(item);
  }
  if (clearAckedEl) {
    clearAckedEl.style.display = anyAcked ? "block" : "none";
  }
}

async function dismissAtRiskStreak(streamer) {
  try {
    await browser.runtime.sendMessage({ type: "dismiss_streak", streamer });
  } catch (_) {
    // ignore
  }
  renderAtRiskStreaks();
}

async function clearAcknowledgedStreaks() {
  try {
    await browser.runtime.sendMessage({ type: "clear_acknowledged_streaks" });
  } catch (_) {
    // ignore
  }
  renderAtRiskStreaks();
}

async function openAtRiskStreak(entry) {
  const url = entry.save_url || `https://www.twitch.tv/${entry.streamer}`;
  try {
    await browser.tabs.create({ url, active: true });
  } catch (e) {
    console.error("Failed to open at-risk streak URL:", e);
  }
  try {
    browser.runtime.sendMessage({ type: "ack_streak", streamer: entry.streamer });
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

document.getElementById("at-risk-clear-acked-link").addEventListener("click", (e) => {
  e.preventDefault();
  clearAcknowledgedStreaks();
});

refreshAll();
