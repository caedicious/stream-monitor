// --- Constants ---
// This browser's name as the background reports it to the desktop
// (OPEN_TABS_BROWSER); the plan's executor key is "<browser>-<instanceId>".
const POPUP_BROWSER = "chrome";
// A slot plan this old is stale (the background's SLOT_PLAN_STALE_MS).
const POPUP_SLOT_PLAN_STALE_MS = 300000;
// An at-risk row stops showing this long after its deadline (the
// background's ACK_EXPIRY_BUFFER_MS).
const ACK_EXPIRY_BUFFER_MS = 14400000;
// The save window of a broke row stored without a deadline_at (the
// desktop's SAVE_WINDOW_HOURS; the 24 h is unverified).
const SAVE_WINDOW_HOURS = 24;
// How long the popup waits for the background. A status query answers at
// once; an action may wait for the desktop (a manual save waits for its
// replan, up to 2 s on a 5 s client timeout) or move tabs and create a
// window (stream_window_set).
const STATUS_REPLY_TIMEOUT_MS = 1000;
const ACTION_REPLY_TIMEOUT_MS = 8000;
const STATUS_UNAVAILABLE_TEXT = "Status unavailable";
const NO_ANSWER_TEXT = "No answer from the extension.";
// Storage keys whose change redraws a section while the popup is open.
const AT_RISK_RENDER_KEYS = ["atRiskStreaks", "slotPlan", "streakSources"];
const SLOTS_RENDER_KEYS = ["slotPlan", "instanceId", "extensionPaused"];
const STREAM_WINDOW_RENDER_KEYS = ["streamWindow"];

// Sends a message to the background and resolves its reply, or null when
// no object came back within timeoutMs (an older background without the
// route, a worker that is still starting, a listener that never answers).
function askBackground(message, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (reply) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(reply && typeof reply === "object" ? reply : null);
    };
    timer = setTimeout(() => finish(null), timeoutMs);
    try {
      Promise.resolve(chrome.runtime.sendMessage(message)).then(finish, () => finish(null));
    } catch (_) {
      finish(null);
    }
  });
}

// --- Settings ---
const autoMuteEl = document.getElementById("auto-mute");
const autoFocusEl = document.getElementById("auto-focus");
const extensionPausedEl = document.getElementById("extension-paused");
const lowQualityEl = document.getElementById("low-quality");
const raidFollowEl = document.getElementById("raid-follow");
const autoClaimBonusEl = document.getElementById("auto-claim-bonus");
const autoClaimBonusNoteEl = document.getElementById("auto-claim-bonus-note");
const maxTabsEl = document.getElementById("max-tabs");
const notificationsEl = document.getElementById("notifications-enabled");
const bellCheckOnOpenEl = document.getElementById("bell-check-on-open");

async function loadSettings() {
  const result = await chrome.storage.local.get([
    "autoMute", "autoFocusTabs", "extensionPaused", "lowQuality", "raidFollowThrough", "maxTabs",
    "autoClaimBonus", "bonusClaimCount", "bellCheckOnOpen"
  ]);
  autoMuteEl.checked = result.autoMute || false;
  // Default ON for autoFocus, see shouldAutoFocus in background.js
  autoFocusEl.checked = result.autoFocusTabs ?? true;
  // Default ON: only an explicit false turns the open check off, as the
  // background and the content script read it.
  bellCheckOnOpenEl.checked = result.bellCheckOnOpen !== false;
  extensionPausedEl.checked = result.extensionPaused || false;
  lowQualityEl.checked = result.lowQuality || false;
  raidFollowEl.checked = result.raidFollowThrough || false;
  // Default ON, like auto-focus.
  autoClaimBonusEl.checked = result.autoClaimBonus ?? true;
  const claimed = Number(result.bonusClaimCount) || 0;
  if (claimed > 0) {
    autoClaimBonusNoteEl.textContent =
      `Clicks the channel points chest when it appears, on any Twitch tab. Claimed so far: ${claimed}`;
  }
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

autoClaimBonusEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ autoClaimBonus: autoClaimBonusEl.checked });
});

bellCheckOnOpenEl.addEventListener("change", async () => {
  await chrome.storage.local.set({ bellCheckOnOpen: bellCheckOnOpenEl.checked });
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
      // ignore; the toggle stays off
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
    muteBtn.textContent = isExempt ? "\u{1F50A}" : "\u{1F507}"; // speaker vs muted speaker
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
// raid-close behaviors don't apply: the user clicked because they want
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
  const result = await chrome.storage.local.get([
    "debugLog", "trackedTabs", "slotPlan", "slotState", "streamWindow"
  ]);
  const debugLog = result.debugLog || [];
  const trackedTabs = result.trackedTabs || {};

  const stateEl = document.getElementById("state");
  const stateText = (slotStatus) => [
    `Tracked tabs: ${JSON.stringify(trackedTabs, null, 2)}`,
    `Slot plan: ${JSON.stringify(result.slotPlan ?? null, null, 2)}`,
    `Slot state: ${JSON.stringify(result.slotState ?? null, null, 2)}`,
    `Stream window: ${JSON.stringify(result.streamWindow ?? null, null, 2)}`,
    `Slot status: ${slotStatus}`,
  ].join("\n\n");
  stateEl.textContent = stateText("checking...");

  const logEl = document.getElementById("log");
  if (debugLog.length === 0) {
    logEl.textContent = "(no log entries)";
  } else {
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

  // The background's own view of the plan, for diagnostics.
  const slotStatus = await askBackground({ type: "slot_status" }, STATUS_REPLY_TIMEOUT_MS);
  stateEl.textContent = stateText(slotStatus ? JSON.stringify(slotStatus) : STATUS_UNAVAILABLE_TEXT);
}

// --- At-risk streaks ---
//
// The background keeps atRiskStreaks; the popup only reads it. Order:
//   1. Unacknowledged rows by the time left to deadline_at, earliest first.
//      A row stored before deadline_at existed keeps the old order after
//      them: broke rows by detection, then in-danger rows by time left.
//   2. Acknowledged rows last (still shown, greyed out).
// Rows past their deadline plus ACK_EXPIRY_BUFFER_MS are not shown.

function atRiskDeadlineMs(entry) {
  const at = Date.parse(entry && entry.deadline_at);
  return Number.isFinite(at) ? at : null;
}

// The deadline of a row without deadline_at, by the old rule: the save
// window after the detection for a broke card, deadline_hours for an
// in-danger one.
function legacyDeadlineMs(entry, now) {
  const detected = Date.parse(entry.detected_at) || now;
  const hours = entry.status === "broke"
    ? SAVE_WINDOW_HOURS
    : (entry.deadline_hours || SAVE_WINDOW_HOURS);
  return detected + hours * 3600 * 1000;
}

function atRiskExpired(entry, now) {
  const deadline = atRiskDeadlineMs(entry);
  if (deadline !== null) return now > deadline + ACK_EXPIRY_BUFFER_MS;
  // The old rule, for a row stored before deadline_at existed.
  if (!entry || !entry.detected_at) return false;
  const detected = Date.parse(entry.detected_at);
  if (isNaN(detected)) return false;
  const deadlineHours = entry.deadline_hours || SAVE_WINDOW_HOURS;
  return now - detected > deadlineHours * 3600 * 1000 + ACK_EXPIRY_BUFFER_MS;
}

function sortAtRiskStreaks(entries, now) {
  // [group, value], smaller first. Group 0: rows with deadline_at, by the
  // time left. Group 1: rows without it, by the old score (broke by
  // detection, then in-danger by time left). Group 2: acknowledged rows.
  const legacyScore = (e) => {
    if (e.status === "broke") return Date.parse(e.detected_at) || 0;
    const detected = Date.parse(e.detected_at) || 0;
    const deadlineMs = detected + (e.deadline_hours || SAVE_WINDOW_HOURS) * 3600 * 1000;
    return 2e15 + Math.max(0, deadlineMs - now);
  };
  const key = (e) => {
    if (e.acknowledged_at) return [2, 0];
    const deadline = atRiskDeadlineMs(e);
    if (deadline !== null) return [0, deadline - now];
    return [1, legacyScore(e)];
  };
  return entries
    .map((e) => ({ e, k: key(e) }))
    .sort((a, b) => a.k[0] - b.k[0] || a.k[1] - b.k[1] ||
      String(a.e.streamer).localeCompare(String(b.e.streamer)))
    .map((x) => x.e);
}

function formatDeadline(entry, now) {
  const deadline = atRiskDeadlineMs(entry) ?? legacyDeadlineMs(entry, now);
  const left = deadline - now;
  const broke = entry.status === "broke";
  if (left <= 0) return broke ? "expired" : "expiring";
  const suffix = broke ? "to save" : "left";
  if (left < 3600 * 1000) return `${Math.max(1, Math.floor(left / 60000))}m ${suffix}`;
  return `${Math.floor(left / (3600 * 1000))}h ${suffix}`;
}

// A row made from a save-streak link has no count: it shows the login alone.
function atRiskRowName(entry) {
  return Number.isInteger(entry.count) ? `${entry.streamer} (${entry.count})` : String(entry.streamer);
}

function atRiskRequestedLabel(entry) {
  if (!entry.requested_at) return "";
  if (entry.requested_mode === "slot") return "Opened: next rotating turn";
  if (entry.requested_mode === "tab") return "Opened";
  return "";
}

// A click becomes the next rotating turn when the desktop runs Slot mode:
// a fresh, active plan and a desktop that takes manual saves (A15), in
// whichever browser the plan runs. Otherwise the background opens a tab.
function manualSaveIsTurn(plan, sources, now) {
  return !!plan && typeof plan === "object" && plan.v === 1 && plan.active === true &&
    Number.isFinite(plan.generated_at) &&
    now - plan.generated_at * 1000 < POPUP_SLOT_PLAN_STALE_MS &&
    Array.isArray(sources) && sources.includes("manual");
}

const SAVE_STREAK_FAILURE_TEXT = {
  bad_login: "Not a Twitch login, so there is no save page to open.",
  desktop_refused: "The desktop app did not take it. Try again in a minute.",
  desktop_down: "The desktop app is not answering.",
  open_failed: "The tab could not be opened.",
};

function showAtRiskMessage(text) {
  const el = document.getElementById("at-risk-message");
  el.textContent = text;
  el.style.display = text ? "block" : "none";
}

async function renderAtRiskStreaks() {
  const stored = await chrome.storage.local.get(AT_RISK_RENDER_KEYS);
  const map = stored.atRiskStreaks || {};
  const section = document.getElementById("at-risk-section");
  const list = document.getElementById("at-risk-list");
  const clearAckedEl = document.getElementById("at-risk-clear-acked");
  // A failure line lasts until the next render.
  showAtRiskMessage("");
  const now = Date.now();
  const entries = Object.values(map).filter(
    (e) => e && typeof e === "object" && e.streamer && !atRiskExpired(e, now)
  );
  if (entries.length === 0) {
    section.style.display = "none";
    list.innerHTML = "";
    if (clearAckedEl) clearAckedEl.style.display = "none";
    return;
  }
  section.style.display = "block";
  const asTurn = manualSaveIsTurn(stored.slotPlan, stored.streakSources, now);
  const sorted = sortAtRiskStreaks(entries, now);
  list.innerHTML = "";
  let anyAcked = false;
  for (const entry of sorted) {
    if (entry.acknowledged_at) anyAcked = true;
    const item = document.createElement("div");
    item.className = "at-risk-item" + (entry.acknowledged_at ? " acknowledged" : "");
    item.title = asTurn
      ? `Make ${entry.streamer}'s save-streak page the next rotating turn`
      : `Open ${entry.streamer}'s save-streak page in a Stream Monitor tab`;

    const name = document.createElement("span");
    name.className = "at-risk-name";
    name.textContent = atRiskRowName(entry);
    item.appendChild(name);

    const status = document.createElement("span");
    status.className = `at-risk-status ${entry.status}`;
    status.textContent = entry.status === "broke" ? "BROKE" : "IN DANGER";
    item.appendChild(status);

    const requestedText = atRiskRequestedLabel(entry);
    if (requestedText) {
      const requested = document.createElement("span");
      requested.className = "at-risk-requested";
      requested.textContent = requestedText;
      item.appendChild(requested);
    }

    const deadline = document.createElement("span");
    deadline.className = "at-risk-deadline";
    deadline.textContent = formatDeadline(entry, now);
    item.appendChild(deadline);

    // Dismiss button. stopPropagation so the row's click handler doesn't
    // also fire (which would open the page the user is trying to dismiss).
    const dismiss = document.createElement("button");
    dismiss.className = "at-risk-dismiss";
    dismiss.textContent = "\u00d7";
    dismiss.title = `Dismiss ${entry.streamer} from this list`;
    dismiss.addEventListener("click", (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      dismissAtRiskStreak(entry.streamer);
    });
    item.appendChild(dismiss);

    item.addEventListener("click", () => saveStreakNow(entry.streamer));
    list.appendChild(item);
  }
  if (clearAckedEl) {
    clearAckedEl.style.display = anyAcked ? "block" : "none";
  }
}

// A row click. The background opens the save-streak page as a tracked tab,
// or queues it as the next rotating turn in Slot mode, and marks the row;
// the popup never opens the page itself. The row stays until the visit
// finishes or Twitch says the streak is kept.
const saveStreakInFlight = new Set();

async function saveStreakNow(streamer) {
  if (saveStreakInFlight.has(streamer)) return;
  saveStreakInFlight.add(streamer);
  try {
    const reply = await askBackground({ type: "save_streak_now", streamer }, ACTION_REPLY_TIMEOUT_MS);
    if (!reply) {
      showAtRiskMessage(NO_ANSWER_TEXT);
    } else if (reply.ok) {
      await renderAtRiskStreaks();
    } else {
      showAtRiskMessage(
        SAVE_STREAK_FAILURE_TEXT[reply.reason] ||
          `Not opened (${String(reply.reason || "no reason given")}).`
      );
    }
  } finally {
    saveStreakInFlight.delete(streamer);
  }
}

async function dismissAtRiskStreak(streamer) {
  try {
    await chrome.runtime.sendMessage({ type: "dismiss_streak", streamer });
  } catch (_) {
    // ignore: the background may be napping; the storage write goes
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

// --- Slots (the desktop app's Slot mode) ---
//
// Everything here comes from the stored slotPlan, instanceId and
// extensionPaused, computed the way the background's planActiveHere
// computes it, so the two never disagree about one plan.

function slotPlanStale(plan, now) {
  return !Number.isFinite(plan.generated_at) ||
    now - plan.generated_at * 1000 >= POPUP_SLOT_PLAN_STALE_MS;
}

function popupExecutorKey(instanceId) {
  return typeof instanceId === "string" && /^[0-9a-f]{8}$/.test(instanceId)
    ? `${POPUP_BROWSER}-${instanceId}`
    : null;
}

function slotStatusText(plan, instanceId, now) {
  if (slotPlanStale(plan, now)) return "Waiting for the desktop app";
  if (plan.state === "absent") return "The desktop app runs Slot mode but no browser is reporting";
  if (plan.state === "none_ever" || plan.state === "waiting") return "Waiting for the desktop app";
  if (plan.pause === "live") return "Paused: you are live";
  const mine = popupExecutorKey(instanceId);
  return mine && plan.executor === mine ? "Run by this browser" : "Run by another browser";
}

// Whole minutes until an epoch-seconds time, at least 1.
function minutesUntil(epochSeconds, now) {
  return Math.max(1, Math.ceil((epochSeconds * 1000 - now) / 60000));
}

function timeLeftText(epochSeconds, now) {
  const left = epochSeconds * 1000 - now;
  if (left >= 3600 * 1000) return `${Math.floor(left / (3600 * 1000))} h left`;
  if (left > 0) return `${Math.max(1, Math.floor(left / 60000))} min left`;
  return "due now";
}

function describeRotatingSlot(slot, now) {
  if (!slot.streamer) return "empty";
  if (slot.mode === "idle") return `idle on ${slot.streamer}`;
  const parts = [slot.streamer];
  if (slot.entry === "save") parts.push("save-streak");
  if (Number.isFinite(slot.turn_ends_at)) parts.push(`${minutesUntil(slot.turn_ends_at, now)} min left`);
  else if (!Number.isFinite(slot.confirmed_at)) parts.push("opening");
  return parts.join(", ");
}

function describeQueueEntry(entry, now) {
  if (entry.entry !== "save") return String(entry.streamer);
  if (!Number.isFinite(entry.deadline_at)) return `${entry.streamer} (save-streak)`;
  return `${entry.streamer} (save-streak, ${timeLeftText(entry.deadline_at, now)})`;
}

// The Slots lines: Keep Open occupants (and who waits for a Keep Open
// slot), the rotating slots (lent Keep Open slots included), the queue,
// who was watched this broadcast, and the status.
function slotLines(plan, now) {
  const slots = Array.isArray(plan.slots) ? plan.slots.filter((s) => s && typeof s === "object") : [];
  const lines = {};
  const keepSlots = slots.filter((s) => s.kind === "keep" && !s.lent);
  if (slots.some((s) => s.kind === "keep")) {
    const names = keepSlots.filter((s) => s.streamer).map((s) => s.streamer);
    const waits = slots.filter((s) => s.waiting).map((s) => (
      Number.isFinite(s.hold_until)
        ? `${s.waiting} waits ${minutesUntil(s.hold_until, now)} min`
        : `${s.waiting} waits`
    ));
    lines.keep = `Keep Open: ${names.length ? names.join(", ") : "none"}` +
      (waits.length ? ` (${waits.join(", ")})` : "");
  } else {
    lines.keep = "";
  }
  const rotating = slots.filter((s) => s.kind === "cycle" || s.lent === true);
  lines.rotating = `Rotating: ${rotating.length ? rotating.map((s) => describeRotatingSlot(s, now)).join("; ") : "none"}`;
  const queue = Array.isArray(plan.queue) ? plan.queue.filter((q) => q && q.streamer) : [];
  lines.next = `Next: ${queue.length ? queue.map((q) => describeQueueEntry(q, now)).join(", ") : "none"}`;
  const served = Array.isArray(plan.served) ? plan.served.filter(Boolean) : [];
  lines.watched = `Watched this broadcast: ${served.length ? served.join(", ") : "none"}`;
  return lines;
}

async function renderSlots() {
  const { slotPlan, instanceId, extensionPaused } = await chrome.storage.local.get(SLOTS_RENDER_KEYS);
  const section = document.getElementById("slots-section");
  if (!slotPlan || typeof slotPlan !== "object") {
    section.style.display = "none";
    return;
  }
  section.style.display = "block";
  const now = Date.now();
  const lines = slotLines(slotPlan, now);
  const keepEl = document.getElementById("slots-keep");
  keepEl.textContent = lines.keep;
  keepEl.style.display = lines.keep ? "block" : "none";
  document.getElementById("slots-rotating").textContent = lines.rotating;
  document.getElementById("slots-next").textContent = lines.next;
  document.getElementById("slots-watched").textContent = lines.watched;
  const status = slotStatusText(slotPlan, instanceId, now);
  document.getElementById("slots-status").textContent = status;
  document.getElementById("slots-paused-note").style.display =
    status === "Run by this browser" && extensionPaused === true ? "block" : "none";
}

// --- Stream window ---
//
// The background owns streamWindow; the popup asks it for the status and
// sends the two buttons as messages.

const STREAM_WINDOW_FAILURE_TEXT = {
  not_normal: "Only a normal browser window can hold stream tabs.",
  private: "Private windows can't hold stream tabs.",
  missing: "This window could not be found.",
};

function streamWindowStatusText(s) {
  if (!s.set) return "Not set: streams open wherever the browser puts them.";
  if (!s.existsNow) return "Closed: it reopens at its last position with the next stream.";
  if (s.isThisWindow) return "This window.";
  if (s.state === "minimized") return "Minimized: streams there play as hidden tabs.";
  const b = s.bounds;
  if (b && [b.left, b.top, b.width, b.height].every(Number.isFinite)) {
    return `Another window (${b.width}x${b.height} at ${b.left},${b.top}).`;
  }
  return "Another window.";
}

function showStreamWindowMessage(text) {
  const el = document.getElementById("stream-window-message");
  el.textContent = text;
  el.style.display = text ? "block" : "none";
}

async function currentWindowOrNull() {
  try {
    return await chrome.windows.getCurrent();
  } catch (_) {
    return null;
  }
}

async function renderStreamWindow() {
  const statusEl = document.getElementById("stream-window-status");
  const useBtn = document.getElementById("stream-window-use");
  const stopBtn = document.getElementById("stream-window-stop");
  const current = await currentWindowOrNull();
  const isPrivate = !!(current && current.incognito);
  useBtn.disabled = !current || isPrivate;
  document.getElementById("stream-window-private").style.display = isPrivate ? "block" : "none";
  const reply = await askBackground(
    { type: "stream_window_status", windowId: current ? current.id : null },
    STATUS_REPLY_TIMEOUT_MS
  );
  let isSet;
  if (reply) {
    statusEl.textContent = streamWindowStatusText(reply);
    isSet = !!reply.set;
  } else {
    statusEl.textContent = STATUS_UNAVAILABLE_TEXT;
    const { streamWindow } = await chrome.storage.local.get("streamWindow");
    isSet = !!streamWindow;
  }
  stopBtn.style.display = isSet ? "" : "none";
}

async function useThisWindowForStreams() {
  const useBtn = document.getElementById("stream-window-use");
  showStreamWindowMessage("");
  const current = await currentWindowOrNull();
  if (!current) {
    showStreamWindowMessage(STREAM_WINDOW_FAILURE_TEXT.missing);
    return;
  }
  if (current.incognito) {
    showStreamWindowMessage(STREAM_WINDOW_FAILURE_TEXT.private);
    return;
  }
  useBtn.disabled = true;
  const reply = await askBackground(
    { type: "stream_window_set", windowId: current.id },
    ACTION_REPLY_TIMEOUT_MS
  );
  if (!reply) {
    showStreamWindowMessage(NO_ANSWER_TEXT);
  } else if (reply.ok) {
    const moved = Number(reply.moved) || 0;
    showStreamWindowMessage(moved > 0 ? `Moved ${moved} stream tab${moved === 1 ? "" : "s"} here.` : "");
  } else {
    showStreamWindowMessage(STREAM_WINDOW_FAILURE_TEXT[reply.reason] || "The window was not set.");
  }
  await renderStreamWindow();
}

async function stopUsingStreamWindow() {
  const stopBtn = document.getElementById("stream-window-stop");
  showStreamWindowMessage("");
  stopBtn.disabled = true;
  const reply = await askBackground({ type: "stream_window_clear" }, ACTION_REPLY_TIMEOUT_MS);
  stopBtn.disabled = false;
  if (!reply) {
    showStreamWindowMessage(NO_ANSWER_TEXT);
    return;
  }
  await renderStreamWindow();
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
  renderSlots();
  loadStreamerList();
  renderStreamWindow();
  loadDebugInfo();
}

// Redraw a section when the background changes what it shows.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  const touched = (keys) => keys.some((key) => Object.prototype.hasOwnProperty.call(changes, key));
  if (touched(AT_RISK_RENDER_KEYS)) renderAtRiskStreaks();
  if (touched(SLOTS_RENDER_KEYS)) renderSlots();
  if (touched(STREAM_WINDOW_RENDER_KEYS)) renderStreamWindow();
});

document.getElementById("refresh").addEventListener("click", refreshAll);

document.getElementById("stream-window-use").addEventListener("click", useThisWindowForStreams);
document.getElementById("stream-window-stop").addEventListener("click", stopUsingStreamWindow);

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
