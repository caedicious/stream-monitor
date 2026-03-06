/**
 * Stream Monitor Tab Closer
 * Automatically closes Twitch tabs when the streamer raids someone else.
 */

const CONFIG_URL = "http://127.0.0.1:52832/config";
const TWITCH_URL_PATTERN = /^https?:\/\/(?:www\.)?twitch\.tv\/([a-zA-Z0-9_]+)/;

// Tracked tabs: tabId -> { originalStreamer: string, isMonitored: boolean }
const trackedTabs = new Map();

// Monitored streamers (lowercase)
let monitoredStreamers = new Set();

/**
 * Extract streamer username from a Twitch URL
 */
function getStreamerFromUrl(url) {
  const match = url.match(TWITCH_URL_PATTERN);
  if (match && match[1]) {
    const username = match[1].toLowerCase();
    // Ignore non-channel pages
    const ignoredPaths = ['directory', 'videos', 'settings', 'subscriptions', 'inventory', 'drops', 'wallet'];
    if (ignoredPaths.includes(username)) {
      return null;
    }
    return username;
  }
  return null;
}

/**
 * Fetch config from Stream Monitor app
 */
async function fetchConfig() {
  try {
    const response = await fetch(CONFIG_URL);
    if (response.ok) {
      const data = await response.json();
      if (data.streamers && Array.isArray(data.streamers)) {
        monitoredStreamers = new Set(data.streamers.map(s => s.toLowerCase()));
        console.log("[Stream Monitor] Loaded streamers:", Array.from(monitoredStreamers));
      }
    }
  } catch (error) {
    // Stream Monitor app probably not running, that's fine
    console.log("[Stream Monitor] Could not connect to Stream Monitor app");
  }
}

/**
 * Check if a streamer is being monitored
 */
function isMonitoredStreamer(username) {
  return monitoredStreamers.has(username.toLowerCase());
}

/**
 * Handle tab creation
 */
function onTabCreated(tab) {
  if (!tab.url) return;
  
  const streamer = getStreamerFromUrl(tab.url);
  if (streamer && isMonitoredStreamer(streamer)) {
    trackedTabs.set(tab.id, {
      originalStreamer: streamer,
      isMonitored: true
    });
    console.log(`[Stream Monitor] Tracking tab ${tab.id} for streamer: ${streamer}`);
  }
}

/**
 * Handle tab URL updates
 */
function onTabUpdated(tabId, changeInfo, tab) {
  // Only care about URL changes
  if (!changeInfo.url) return;
  
  const newStreamer = getStreamerFromUrl(changeInfo.url);
  const tracked = trackedTabs.get(tabId);
  
  if (tracked && tracked.isMonitored) {
    // This tab was opened for a monitored streamer
    if (newStreamer && newStreamer !== tracked.originalStreamer) {
      // URL changed to a different streamer (raid!)
      console.log(`[Stream Monitor] Raid detected: ${tracked.originalStreamer} -> ${newStreamer}. Closing tab.`);
      browser.tabs.remove(tabId);
      trackedTabs.delete(tabId);
    } else if (!newStreamer) {
      // Navigated away from Twitch entirely
      trackedTabs.delete(tabId);
    }
  } else if (newStreamer && isMonitoredStreamer(newStreamer)) {
    // New navigation to a monitored streamer
    trackedTabs.set(tabId, {
      originalStreamer: newStreamer,
      isMonitored: true
    });
    console.log(`[Stream Monitor] Now tracking tab ${tabId} for streamer: ${newStreamer}`);
  }
}

/**
 * Handle tab removal
 */
function onTabRemoved(tabId) {
  trackedTabs.delete(tabId);
}

/**
 * Initialize extension
 */
async function init() {
  console.log("[Stream Monitor] Tab Closer extension starting...");
  
  // Fetch initial config
  await fetchConfig();
  
  // Refresh config periodically (every 60 seconds)
  setInterval(fetchConfig, 60000);
  
  // Set up tab listeners
  browser.tabs.onCreated.addListener(onTabCreated);
  browser.tabs.onUpdated.addListener(onTabUpdated);
  browser.tabs.onRemoved.addListener(onTabRemoved);
  
  // Check existing tabs
  const tabs = await browser.tabs.query({ url: "*://*.twitch.tv/*" });
  for (const tab of tabs) {
    const streamer = getStreamerFromUrl(tab.url);
    if (streamer && isMonitoredStreamer(streamer)) {
      trackedTabs.set(tab.id, {
        originalStreamer: streamer,
        isMonitored: true
      });
      console.log(`[Stream Monitor] Found existing tab ${tab.id} for streamer: ${streamer}`);
    }
  }
  
  console.log("[Stream Monitor] Tab Closer extension ready");
}

// Start the extension
init();
