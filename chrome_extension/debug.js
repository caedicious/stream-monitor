async function loadDebugInfo() {
  const result = await chrome.storage.local.get(["debugLog", "trackedTabs", "monitoredStreamers"]);
  const debugLog = result.debugLog || [];
  const trackedTabs = result.trackedTabs || {};
  const monitoredStreamers = result.monitoredStreamers || [];

  const stateEl = document.getElementById("state");
  stateEl.textContent =
    `Monitored streamers: ${monitoredStreamers.length > 0 ? monitoredStreamers.join(", ") : "(none)"}\n\n` +
    `Tracked tabs: ${JSON.stringify(trackedTabs, null, 2)}`;

  const logEl = document.getElementById("log");
  if (debugLog.length === 0) {
    logEl.textContent = "(no log entries)";
    return;
  }

  logEl.innerHTML = "";
  // Show newest first
  for (let i = debugLog.length - 1; i >= 0; i--) {
    const entry = debugLog[i];
    const div = document.createElement("div");
    div.className = `log-entry ${entry.level}`;
    div.textContent = `[${entry.ts}] [${entry.level}] ${entry.msg}`;
    logEl.appendChild(div);
  }
}

document.getElementById("refresh").addEventListener("click", loadDebugInfo);

document.getElementById("clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ debugLog: [] });
  loadDebugInfo();
});

loadDebugInfo();
