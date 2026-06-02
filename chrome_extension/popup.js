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
  const result = await chrome.storage.local.get([
    "monitoredStreamers",
    "liveStreamers",
    "muteExemptStreamers",
    "autoMute",
  ]);
  const monitoredStreamers = result.monitoredStreamers || [];
  const liveStreamers = result.liveStreamers || [];
  const muteExempt = new Set(
    (Array.isArray(result.muteExemptStreamers) ? result.muteExemptStreamers : []).map(s =>
      String(s).toLowerCase()
    )
  );
  const autoMute = !!result.autoMute;

  const listEl = document.getElementById("streamer-list");
  if (monitoredStreamers.length === 0) {
    listEl.textContent = "(no streamers monitored)";
    return;
  }

  const liveSet = new Set(liveStreamers.map(s => s.toLowerCase()));
  listEl.innerHTML = "";
  for (const streamer of monitoredStreamers) {
    const slug = streamer.toLowerCase();
    const isLive = liveSet.has(slug);
    const isExempt = muteExempt.has(slug);
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

    // Per-streamer mute-exempt toggle. Speaker icon: filled = will be
    // auto-muted (follows global setting); slashed = exempt (audio
    // allowed even when global auto-mute is on). Only meaningful when
    // global autoMute is on; we still show it when it's off so users
    // can pre-mark streamers before turning auto-mute on.
    const muteBtn = document.createElement("button");
    muteBtn.className = "streamer-mute-toggle" + (isExempt ? " exempt" : "");
    muteBtn.textContent = isExempt ? "\u{1F50A}" : "\u{1F507}"; // 🔊 vs 🔇
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
  const result = await chrome.storage.local.get("muteExemptStreamers");
  const current = new Set(
    (Array.isArray(result.muteExemptStreamers) ? result.muteExemptStreamers : []).map(s =>
      String(s).toLowerCase()
    )
  );
  const nowExempt = !current.has(slug);
  if (nowExempt) {
    current.add(slug);
  } else {
    current.delete(slug);
  }
  await chrome.storage.local.set({ muteExemptStreamers: Array.from(current) });

  // Apply immediately to currently-open tabs for this streamer. Any tab
  // whose URL matches /<slug>/ on twitch.tv gets unmuted (if newly
  // exempt) or re-muted (if exemption was removed AND global autoMute
  // is on).
  try {
    const autoMute = (await chrome.storage.local.get("autoMute")).autoMute || false;
    const tabs = await chrome.tabs.query({ url: "*://*.twitch.tv/*" });
    const match = new RegExp(`^https?://(?:www\\.)?twitch\\.tv/${slug}(?:[/?#]|$)`, "i");
    for (const t of tabs) {
      if (!t.url || !match.test(t.url)) continue;
      if (nowExempt) {
        await chrome.tabs.update(t.id, { muted: false });
      } else if (autoMute) {
        await chrome.tabs.update(t.id, { muted: true });
      }
    }
  } catch (e) {
    console.warn("Failed to apply mute exemption to open tabs:", e);
  }

  // Re-render the list so the toggle reflects the new state.
  await loadStreamerList();
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

    // Dismiss button. stopPropagation so the row's click handler doesn't
    // also fire (which would open the URL the user is trying to dismiss).
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
    await chrome.runtime.sendMessage({ type: "dismiss_streak", streamer });
  } catch (_) {
    // ignore — background may be napping; the storage write goes
    // through regardless via the message handler waking the SW.
  }
  renderAtRiskStreaks();
}

async function clearAcknowledgedStreaks() {
  try {
    await chrome.runtime.sendMessage({ type: "clear_acknowledged_streaks" });
  } catch (_) {
    // ignore
  }
  renderAtRiskStreaks();
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

async function renderSoundWarning() {
  const { soundBlocked } = await chrome.storage.local.get("soundBlocked");
  const warningEl = document.getElementById("sound-warning");
  if (!warningEl) return;
  warningEl.style.display = soundBlocked ? "block" : "none";
}

function refreshAll() {
  renderSoundWarning();
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

document.getElementById("at-risk-clear-acked-link").addEventListener("click", (e) => {
  e.preventDefault();
  clearAcknowledgedStreaks();
});

document.getElementById("sound-warning-link").addEventListener("click", (e) => {
  e.preventDefault();
  // Chrome supports the siteDetails deep-link, which lands the user on a
  // page that shows every per-site permission for twitch.tv with the
  // Sound dropdown immediately accessible. Falls back to the generic
  // content/sound page if that URL isn't resolvable.
  const url =
    "chrome://settings/content/siteDetails?site=https%3A%2F%2Fwww.twitch.tv";
  chrome.tabs.create({ url }, () => {
    if (chrome.runtime.lastError) {
      chrome.tabs.create({ url: "chrome://settings/content/sound" });
    }
  });
  window.close();
});

refreshAll();
