// --- Settings ---
const autoMuteEl = document.getElementById("auto-mute");

async function loadSettings() {
  const { autoMute = false } = await browser.storage.local.get("autoMute");
  autoMuteEl.checked = autoMute;
}

autoMuteEl.addEventListener("change", async () => {
  await browser.storage.local.set({ autoMute: autoMuteEl.checked });
});

loadSettings();

// --- Debug info ---
async function loadDebugInfo() {
  const { debugLog = [], trackedTabs = {}, monitoredStreamers = [] } =
    await browser.storage.local.get(["debugLog", "trackedTabs", "monitoredStreamers"]);

  const stateEl = document.getElementById("state");
  stateEl.textContent =
    `Streamers: ${monitoredStreamers.length > 0 ? monitoredStreamers.join(", ") : "(none)"}\n` +
    `Tracked tabs: ${JSON.stringify(trackedTabs, null, 2)}`;

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

document.getElementById("refresh").addEventListener("click", loadDebugInfo);

document.getElementById("clear").addEventListener("click", async () => {
  await browser.storage.local.set({ debugLog: [] });
  loadDebugInfo();
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

loadDebugInfo();
