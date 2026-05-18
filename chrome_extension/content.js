/**
 * Stream Monitor — Twitch Player Content Script
 *
 * Injected into Twitch pages to control the video player.
 * Ensures the video is playing and unmuted at the player level so the
 * viewer is counted by Twitch. Browser-level tab muting (via the
 * auto-mute toggle) is separate — the tab can be muted for the user
 * while the Twitch player itself stays unmuted.
 *
 * Also supports low-quality mode to reduce bandwidth.
 */

(() => {
  "use strict";

  const POLL_INTERVAL_MS = 3000;
  const MAX_RETRIES = 20; // Stop retrying after ~60s if video never appears
  const LOG_PREFIX = "[Stream Monitor Content]";

  let ensurePlaybackEnabled = false;
  let lowQualityEnabled = false;
  let lowQualityApplied = false;
  let pollTimer = null;
  let retryCount = 0;
  let keepaliveTimer = null;
  let lastVideoTime = -1;

  // -----------------------------------------------------------------------
  // Video element helpers
  // -----------------------------------------------------------------------

  function getVideoElement() {
    // Return the main stream video, not an ad overlay's video. Twitch
    // renders a separate <video> inside ad overlays (outstream-ax-overlay,
    // ax-overlay) and document.querySelector("video") might find that one
    // first, causing the content script to chase ghost "paused" states on
    // an element that is not the main stream.
    const videos = Array.from(document.querySelectorAll("video"));
    for (const v of videos) {
      const adAncestor = v.closest(
        '[data-a-target*="ax-overlay"], [data-a-target*="outstream"]'
      );
      if (!adAncestor) return v;
    }
    // If everything is inside an ad overlay, fall back to the first video.
    return videos[0] || null;
  }

  let lastPlayButtonClick = 0;
  let lastMuteButtonClick = 0;
  let lastVideoCurrentTime = -1;
  let lastVideoTimeChangeMs = 0;
  let lastReloadRequestMs = 0;

  function findPlayLabelButton() {
    // Returns a Twitch play/pause button whose aria-label starts with "Play"
    // (meaning Twitch thinks the stream is currently paused — clicking will
    // resume). There can be two such buttons in the DOM (main player and an
    // ad overlay), so we iterate and take the first "Play" one.
    // This is more reliable than reading video.paused because Twitch's React
    // state can disagree with the raw video element, and React wins — any
    // video.play() we do gets immediately undone by Twitch's reconciler.
    const buttons = document.querySelectorAll('[data-a-target="player-play-pause-button"]');
    for (const btn of buttons) {
      const label = (btn.getAttribute("aria-label") || "").toLowerCase();
      if (label.startsWith("play")) {
        return { btn, label };
      }
    }
    return null;
  }

  function findUnmuteLabelButton() {
    // Same pattern as play: Twitch's React state, not the raw video element,
    // drives the speaker icon in the player UI. If the mute/unmute button's
    // aria-label starts with "Unmute", Twitch thinks the player is muted and
    // clicking will unmute it. Setting video.muted = false on the element
    // alone is not enough — Twitch's reconciler re-mutes it.
    const buttons = document.querySelectorAll('[data-a-target="player-mute-unmute-button"]');
    for (const btn of buttons) {
      const label = (btn.getAttribute("aria-label") || "").toLowerCase();
      if (label.startsWith("unmute")) {
        return { btn, label };
      }
    }
    return null;
  }

  function ensurePlayerUnmuted(video) {
    // Keep the Twitch PLAYER unmuted at all times. Tab-level muting is
    // handled separately by the background script via chrome.tabs.update.
    if (video && video.muted) {
      video.muted = false;
    }
    if (video && video.volume < 0.01) {
      video.volume = 0.05;
    }
    const now = Date.now();
    if (now - lastMuteButtonClick > 2000) {
      const info = findUnmuteLabelButton();
      if (info) {
        lastMuteButtonClick = now;
        info.btn.click();
        console.log(LOG_PREFIX, `Clicked Twitch unmute button (label="${info.label}")`);
      }
    }
  }

  function ensurePlaying(video) {
    if (!video) return;

    // Always ensure the Twitch player is unmuted — viewer count depends on
    // it. Tab silence for the user is handled by chrome.tabs.update at the
    // browser level, which does not affect the player's mute state.
    ensurePlayerUnmuted(video);

    // Source of truth for play state: Twitch's button label. If any
    // play/pause button says "Play", Twitch thinks the stream is paused and
    // we should click to resume. The button click routes through Twitch's
    // React state machine, which is the only reliable way to keep the video
    // playing — direct video.play() gets undone by Twitch's reconciler.
    const now = Date.now();
    if (now - lastPlayButtonClick > 2000) {
      const info = findPlayLabelButton();
      if (info) {
        lastPlayButtonClick = now;
        info.btn.click();
        console.log(LOG_PREFIX, `Clicked Twitch play button (label="${info.label}")`);
        lastVideoTimeChangeMs = now;
        return;
      }
    }

    // Track currentTime progression for diagnostic purposes only. We used
    // to request a tab reload after 45s of no progression, but that fired
    // false positives on streams that were playing fine (ads, buffer
    // hiccups, measuring the wrong video element, etc.) and caused reload
    // loops. The background script no longer reloads based on content-
    // script heuristics — only on explicit user action. If the stream is
    // genuinely broken, unmute/play button clicks above will recover it.
    if (video.currentTime !== lastVideoCurrentTime) {
      lastVideoCurrentTime = video.currentTime;
      lastVideoTimeChangeMs = now;
    } else {
      const stuckMs = now - lastVideoTimeChangeMs;
      if (stuckMs > 60000 && stuckMs % 60000 < 5000) {
        // Log a warning every minute so stalls are visible in DevTools,
        // but DO NOT request a reload.
        console.warn(LOG_PREFIX, `Video currentTime has not advanced for ${Math.round(stuckMs / 1000)}s (not reloading)`);
      }
    }
  }

  // -----------------------------------------------------------------------
  // Low quality mode — interact with Twitch's settings menu via DOM
  // -----------------------------------------------------------------------

  // Read the human-readable label of a quality option, regardless of
  // whether it's a role="menuitemradio" element (textContent works
  // directly) or an <input type="radio"> wrapped in a <label> (textContent
  // of the closest label).
  function getQualityLabel(opt) {
    if (opt.getAttribute && opt.getAttribute("role") === "menuitemradio") {
      return (opt.textContent || "").trim();
    }
    const lbl = opt.closest && opt.closest("label");
    if (lbl) return (lbl.textContent || "").trim();
    return (opt.getAttribute && opt.getAttribute("aria-label")) || (opt.textContent || "").trim();
  }

  // Parse a quality label (e.g. "720p60", "480p", "1080p60 (Source)") into
  // a numeric resolution. Lower number = lower quality. Auto and Source are
  // never the "lowest" pick (Source is full quality regardless of where it
  // sits in the list, and Auto adapts to bandwidth). Anything we can't
  // parse returns Infinity so it sorts to the back.
  function parseQualityRank(label) {
    const lower = label.toLowerCase();
    if (lower.includes("auto")) return Infinity;
    if (lower.includes("source")) return Infinity;
    const m = label.match(/(\d+)\s*p/i);
    return m ? parseInt(m[1], 10) : Infinity;
  }

  // Pick the option with the smallest parsed resolution. The Twitch quality
  // menu is not always sorted; "Source" is sometimes pinned to the bottom,
  // which is why "last item in the list" is unreliable.
  function pickLowestQualityOption(opts) {
    let best = null;
    let bestRank = Infinity;
    for (const opt of opts) {
      const label = getQualityLabel(opt);
      const rank = parseQualityRank(label);
      if (rank < bestRank) {
        bestRank = rank;
        best = opt;
      }
    }
    return { option: best, label: best ? getQualityLabel(best) : null };
  }

  function applyLowQuality() {
    if (lowQualityApplied) return;

    // Try to find and click the settings button
    const settingsBtn = document.querySelector('[data-a-target="player-settings-button"]');
    if (!settingsBtn) {
      console.log(LOG_PREFIX, "Settings button not found yet, will retry");
      return;
    }

    settingsBtn.click();

    // Wait for the settings menu to open
    setTimeout(() => {
      // Find the "Quality" menu item
      const qualityItem = document.querySelector('[data-a-target="player-settings-menu-item-quality"]');
      if (!qualityItem) {
        // Close settings if quality item not found
        settingsBtn.click();
        console.log(LOG_PREFIX, "Quality menu item not found, will retry");
        return;
      }

      qualityItem.click();

      // Wait for quality options to appear
      setTimeout(() => {
        const radios = document.querySelectorAll('[data-a-target="player-settings-menu"] input[type="radio"]');
        if (radios.length > 0) {
          const { option, label } = pickLowestQualityOption(radios);
          if (option) {
            option.click();
            lowQualityApplied = true;
            console.log(LOG_PREFIX, `Set to lowest quality: ${label}`);
            return;
          }
        }

        // Fallback selector: role="menuitemradio" items
        const items = document.querySelectorAll('[data-a-target="player-settings-menu"] [role="menuitemradio"]');
        if (items.length > 0) {
          const { option, label } = pickLowestQualityOption(items);
          if (option) {
            option.click();
            lowQualityApplied = true;
            console.log(LOG_PREFIX, `Set to lowest quality (menuitemradio): ${label}`);
            return;
          }
        }

        // Close the menu if we couldn't parse any options. Will retry on
        // the next poll iteration.
        settingsBtn.click();
        console.log(LOG_PREFIX, "Quality options not found or unparseable, will retry");
      }, 300);
    }, 300);
  }

  // -----------------------------------------------------------------------
  // Polling loop — re-asserts playback state periodically
  // -----------------------------------------------------------------------

  function poll() {
    const video = getVideoElement();

    if (!video) {
      retryCount++;
      if (retryCount >= MAX_RETRIES) {
        console.log(LOG_PREFIX, "Video element not found after max retries, stopping poll");
        stopPolling();
        return;
      }
      return;
    }

    retryCount = 0;

    if (ensurePlaybackEnabled) {
      ensurePlaying(video);
    }

    if (lowQualityEnabled && !lowQualityApplied) {
      applyLowQuality();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    retryCount = 0;
    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    // Run immediately
    poll();
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // -----------------------------------------------------------------------
  // Player keepalive — prevent Twitch from marking viewer as idle
  // Runs every 2 minutes even in background tabs. Simulates viewer
  // activity so Twitch continues counting you as a viewer.
  // -----------------------------------------------------------------------

  const KEEPALIVE_INTERVAL_MS = 120000; // 2 minutes

  function dismissOverlays() {
    // "Click to unmute" banner
    const unmuteBanner = document.querySelector('[data-a-target="player-unmute-overlay-button"]');
    if (unmuteBanner) {
      unmuteBanner.click();
      console.log(LOG_PREFIX, "Keepalive: dismissed unmute overlay");
    }

    // "Stream has encountered an error" / refresh prompt
    const refreshBtn = document.querySelector('[data-a-target="player-overlay-content-gate"] button');
    if (refreshBtn) {
      refreshBtn.click();
      console.log(LOG_PREFIX, "Keepalive: clicked refresh/error overlay button");
    }

    // Content gate / mature content warning
    const contentGateBtn = document.querySelector('[data-a-target="content-classification-gate-overlay-start-watching-button"]');
    if (contentGateBtn) {
      contentGateBtn.click();
      console.log(LOG_PREFIX, "Keepalive: dismissed content gate");
    }

    // Player-error recovery (handles "Error #2000" overlay and similar).
    // Two-tier: first click the reload button if found, then if the error
    // is still present 30s later, hard-reload the page. Both layers fire
    // regardless of tab focus.
    attemptPlayerRecovery();
  }

  // -----------------------------------------------------------------------
  // Player-error recovery
  // -----------------------------------------------------------------------
  //
  // Detects Twitch player-error overlays ("(Error #2000)", "Click Here to
  // Reload Player", etc.) and tries two layers of recovery:
  //
  //   1. Click the reload-player button if found. This is the gentle path
  //      and usually works.
  //   2. If the error is STILL detectable after ERROR_RELOAD_GRACE_MS, do a
  //      hard window.location.reload(). This catches cases where Twitch's
  //      React state is wedged or the button click was registered but had
  //      no visible effect.
  //
  // Both layers fire on background tabs because content scripts run
  // independently of focus, and chrome.alarms (which drives the keepalive)
  // is not focus-gated either. setTimeout in background tabs may be
  // throttled slightly, so the 30s grace may stretch to ~60s on a
  // backgrounded tab — still acceptable for recovery.

  const ERROR_RELOAD_GRACE_MS = 30000;       // wait this long after click before hard-reload
  const ERROR_RELOAD_COOLDOWN_MS = 5 * 60 * 1000; // never hard-reload more than once per 5 min
  let _errorRecoveryTimeoutId = null;
  let _lastReloadAt = 0;

  // Look for unambiguous error markers, not just generic words. Avoids
  // false positives if a stream title or chat message contains "error".
  function detectPlayerError() {
    const playerArea = document.querySelector(
      '.video-player__container, [data-a-target="video-player"]'
    );
    if (!playerArea) return false;
    const text = (playerArea.textContent || "").toLowerCase();
    return (
      /\(error #\d+\)/.test(text) ||
      text.includes("click here to reload player") ||
      text.includes("click here to reload stream")
    );
  }

  function findReloadButton() {
    const matchesReload = (btn) => {
      const text = (btn.textContent || "").toLowerCase().trim();
      return text.includes("reload player") || text.includes("reload stream");
    };
    const playerContainer = document.querySelector(
      '.video-player__container, [data-a-target="video-player"]'
    );
    if (playerContainer) {
      for (const btn of playerContainer.querySelectorAll('button')) {
        if (matchesReload(btn)) return btn;
      }
    }
    for (const btn of document.querySelectorAll('button')) {
      if (!matchesReload(btn)) continue;
      if (btn.offsetParent === null) continue; // hidden
      return btn;
    }
    return null;
  }

  function attemptPlayerRecovery() {
    if (!detectPlayerError()) {
      // Clean state — cancel any pending hard-reload check.
      if (_errorRecoveryTimeoutId) {
        clearTimeout(_errorRecoveryTimeoutId);
        _errorRecoveryTimeoutId = null;
      }
      return;
    }

    // Error is present. Try the gentle fix if a button is available.
    const reloadBtn = findReloadButton();
    if (reloadBtn) {
      reloadBtn.click();
      console.log(
        LOG_PREFIX,
        `Recovery: clicked reload-player button ("${(reloadBtn.textContent || "").trim()}")`
      );
    } else {
      console.log(LOG_PREFIX, "Recovery: error overlay detected but no reload button found");
    }

    // Schedule (or extend) the hard-reload check.
    if (_errorRecoveryTimeoutId) clearTimeout(_errorRecoveryTimeoutId);
    _errorRecoveryTimeoutId = setTimeout(() => {
      _errorRecoveryTimeoutId = null;
      if (!detectPlayerError()) {
        console.log(LOG_PREFIX, "Recovery: error overlay cleared, no hard-reload needed");
        return;
      }
      const now = Date.now();
      if (now - _lastReloadAt < ERROR_RELOAD_COOLDOWN_MS) {
        const remainSec = Math.ceil((ERROR_RELOAD_COOLDOWN_MS - (now - _lastReloadAt)) / 1000);
        console.warn(
          LOG_PREFIX,
          `Recovery: error overlay still present but hard-reload on cooldown (${remainSec}s remaining)`
        );
        return;
      }
      _lastReloadAt = now;
      console.warn(
        LOG_PREFIX,
        "Recovery: error overlay still present after grace window, hard-reloading page"
      );
      hardReload();
    }, ERROR_RELOAD_GRACE_MS);
  }

  // Reload the current page while ensuring ?sm=1 is in the URL.
  // Twitch's SPA strips the query param via history.replaceState shortly
  // after the page loads, so a plain window.location.reload() would
  // reload the sm-less URL. We re-add it so the extension's tab-tracking
  // logic continues to identify this tab as a Stream-Monitor tab on the
  // next navigation event, and so restoring a closed tab via the
  // browser's session history keeps it tracked.
  function hardReload() {
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.get("sm") === "1") {
        window.location.reload();
        return;
      }
      url.searchParams.set("sm", "1");
      window.location.href = url.toString();
    } catch (e) {
      console.warn(
        LOG_PREFIX,
        "Failed to construct sm=1 URL, falling back to plain reload:",
        e && e.message
      );
      window.location.reload();
    }
  }

  function keepalive() {
    const video = getVideoElement();
    if (!video) return;

    // Dismiss any overlays blocking the player
    dismissOverlays();

    // Check if video has stalled (currentTime hasn't changed)
    if (video.currentTime === lastVideoTime && !video.paused && lastVideoTime > 0) {
      console.log(LOG_PREFIX, "Keepalive: video appears stalled, attempting recovery");
      // Try seeking slightly to kick the buffer
      try {
        video.currentTime = video.currentTime;
      } catch (e) {
        // Ignore seek errors on live streams
      }
    }
    lastVideoTime = video.currentTime;

    // Simulate minimal viewer activity — move mouse over the player
    // This triggers Twitch's internal activity tracking
    const player = document.querySelector('.video-player__container') ||
                   document.querySelector('[data-a-target="video-player"]');
    if (player) {
      player.dispatchEvent(new MouseEvent("mousemove", {
        bubbles: true, clientX: 100, clientY: 100
      }));
      console.log(LOG_PREFIX, "Keepalive: simulated mousemove on player");
    }

    // Ensure video is still playing and unmuted at player level
    if (ensurePlaybackEnabled) {
      ensurePlaying(video);
    }
  }

  function startKeepalive() {
    if (keepaliveTimer) return;
    // Keepalive is now driven by the background script via chrome.alarms,
    // which are not throttled in background tabs. We keep a local fallback
    // setInterval as a safety net, but the primary trigger is the
    // "keepalive" message from background.js.
    keepaliveTimer = setInterval(keepalive, KEEPALIVE_INTERVAL_MS);
    console.log(LOG_PREFIX, "Keepalive registered (background alarm + local fallback)");
  }

  // -----------------------------------------------------------------------
  // Error detection — notify background script if the stream has an error
  // -----------------------------------------------------------------------

  function checkForErrors() {
    // Only treat very specific error overlays as genuine errors. The
    // previous implementation also matched the generic .content-overlay-gate
    // class (which fires on content-classification warnings that appear on
    // many normal streams) and a readyState < 2 heuristic (which fires
    // during normal page load), producing false-positive reloads.
    //
    // We now rely on the currentTime-progression stuck-detector in
    // ensurePlaying() to trigger reloads. That check is strict (45s of no
    // progress + 2min cooldown) so it only fires on genuine stalls.
    const errorSelectors = [
      '[data-a-target="player-error-message"]',
    ];
    for (const selector of errorSelectors) {
      const el = document.querySelector(selector);
      if (el && el.offsetParent !== null) {
        return true;
      }
    }
    return false;
  }

  // Error reporting is intentionally disabled: the currentTime-progression
  // check in ensurePlaying() is the only reload-trigger path now. We keep
  // checkForErrors() exported via getStatus for popup diagnostics.
  function startErrorChecking() {
    // no-op
  }

  // -----------------------------------------------------------------------
  // Message handler — receives commands from the background script
  // -----------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    switch (message.action) {
      case "ensurePlaying":
        ensurePlaybackEnabled = true;
        startPolling();
        startKeepalive();
        sendResponse({ ok: true });
        break;

      case "setLowQuality":
        lowQualityEnabled = message.enabled !== false;
        lowQualityApplied = false; // Reset so it re-applies
        if (lowQualityEnabled) {
          startPolling();
        }
        sendResponse({ ok: true });
        break;

      case "keepalive":
        // Triggered by background script's alarm — not throttled
        keepalive();
        sendResponse({ ok: true });
        break;

      case "getStatus":
        sendResponse({
          hasVideo: !!getVideoElement(),
          ensurePlayback: ensurePlaybackEnabled,
          lowQuality: lowQualityEnabled,
          lowQualityApplied,
          keepaliveActive: !!keepaliveTimer,
          hasError: checkForErrors(),
        });
        break;

      default:
        sendResponse({ ok: false, error: "unknown action" });
    }
    return false; // Synchronous response
  });

  // -----------------------------------------------------------------------
  // Streak monitor — detect Twitch's "your N-stream streak on X broke /
  // ends in Yh" notifications anywhere on the page (bell dropdown, the
  // notifications panel, the inventory page) and relay them to the desktop
  // app so it can log + notify the user. Twitch exposes no public API for
  // viewing streaks, so we DOM-scrape with text-based regexes that don't
  // depend on Twitch's React class names.
  // -----------------------------------------------------------------------

  // Match the visible text of a streak notification card. We keep the
  // regexes tolerant: Twitch occasionally swaps "Stream" for "Live", and
  // the rescue-window phrasing varies between "next Yh", "next Y hours",
  // and "next Y days".
  const STREAK_BROKE_RE =
    /Your\s+(\d+)[- ](?:stream|live)\s+streak\s+on\s+([^\s!.,]+)\s+broke/i;
  const STREAK_IN_DANGER_RE =
    /Your\s+(\d+)[- ](?:stream|live)\s+streak\s+on\s+([^\s!.,]+)\s+(?:ends|expires)\s+in\s+(\d+)\s*(h|hours?|d|days?|m|mins?|minutes?)/i;

  // Parse a single block of text. Returns null if no streak match.
  function parseStreakText(text) {
    if (!text || text.length > 600) return null; // Reject huge blobs
    let m = STREAK_BROKE_RE.exec(text);
    if (m) {
      return {
        status: "broke",
        streamer: m[2].toLowerCase(),
        count: parseInt(m[1], 10),
        deadline_hours: 24, // Twitch's documented rescue window
      };
    }
    m = STREAK_IN_DANGER_RE.exec(text);
    if (m) {
      const n = parseInt(m[3], 10);
      const unit = m[4].toLowerCase();
      let hours = n;
      if (unit.startsWith("d")) hours = n * 24;
      else if (unit.startsWith("m")) hours = Math.max(1, Math.round(n / 60));
      return {
        status: "in_danger",
        streamer: m[2].toLowerCase(),
        count: parseInt(m[1], 10),
        deadline_hours: hours,
      };
    }
    return null;
  }

  const _seenStreakEvents = new Set();

  function _streakKey(ev) {
    return `${ev.status}:${ev.streamer}:${ev.count}`;
  }

  function scanForStreakEvents() {
    // Look at any element whose text node mentions "streak". This is cheap
    // (text-content filter first) and avoids walking the entire DOM.
    const candidates = document.body
      ? document.body.querySelectorAll("a, p, span, div, article, li")
      : [];
    const found = [];
    for (const el of candidates) {
      // Only leaf-ish nodes — skip containers with many children to avoid
      // matching the same text 10x at different depths.
      if (el.children.length > 4) continue;
      const text = (el.textContent || "").trim();
      if (text.length < 12 || !/streak/i.test(text)) continue;
      const ev = parseStreakText(text);
      if (ev) {
        // Refine the streamer slug if a nearby anchor link gives us a
        // cleaner version than the parsed text (handles display-name
        // capitalization and the /save-streak/<slug> path pattern).
        const link =
          el.tagName === "A"
            ? el
            : el.querySelector("a[href^='/'], a[href^='https://www.twitch.tv/']");
        if (link) {
          const href = link.getAttribute("href") || "";
          const path = href
            .replace(/^https?:\/\/[^/]+/i, "")
            .replace(/^\/+/, "")
            .split(/[?#]/)[0];
          const segments = path.split("/");
          if (segments[0] === "save-streak" && segments[1] && /^[a-z0-9_]+$/i.test(segments[1])) {
            ev.streamer = segments[1].toLowerCase();
          } else if (segments[0] && /^[a-z0-9_]+$/i.test(segments[0])) {
            ev.streamer = segments[0].toLowerCase();
          }
        }
        // Twitch's notification-cards link to /save-streak/<streamer>,
        // which lands the user on the streamer's channel with the
        // "save your streak" UI surfaced. The pattern is stable enough
        // that we always synthesize it from the slug rather than
        // round-tripping whatever href the card happened to carry.
        ev.save_url = `https://www.twitch.tv/save-streak/${ev.streamer}`;
        found.push(ev);
      }
    }
    return found;
  }

  function reportStreakEvent(ev) {
    const key = _streakKey(ev);
    if (_seenStreakEvents.has(key)) return;
    _seenStreakEvents.add(key);
    try {
      chrome.runtime.sendMessage({
        type: "streak_event",
        event: {
          ...ev,
          detected_at: new Date().toISOString(),
          page_url: window.location.href,
        },
      });
      console.log(LOG_PREFIX, "Streak event reported:", key);
    } catch (e) {
      console.warn(LOG_PREFIX, "Failed to report streak event:", e && e.message);
    }
  }

  function scanAndReport() {
    try {
      for (const ev of scanForStreakEvents()) reportStreakEvent(ev);
    } catch (e) {
      console.warn(LOG_PREFIX, "Streak scan failed:", e && e.message);
    }
  }

  let _streakScanTimer = null;
  function startStreakMonitor() {
    // Periodic re-scan: catches the bell dropdown opening, new notifications
    // arriving via WebSocket, and the user navigating to the notifications
    // page. Cheap because the scan is just a text-content filter.
    if (_streakScanTimer) return;
    // Initial scan after a short delay so the page has time to render.
    setTimeout(scanAndReport, 5000);
    _streakScanTimer = setInterval(scanAndReport, 60000);

    // Also react immediately to DOM mutations so freshly-opened notifications
    // are caught without waiting for the next interval tick.
    if (typeof MutationObserver === "function" && document.body) {
      const obs = new MutationObserver(() => {
        // Debounce: only one scan per 2s burst.
        if (obs._pending) return;
        obs._pending = true;
        setTimeout(() => {
          obs._pending = false;
          scanAndReport();
        }, 2000);
      });
      obs.observe(document.body, { childList: true, subtree: true });
    }
  }

  // -----------------------------------------------------------------------
  // Bell surveillance — Twitch only renders notification cards into the DOM
  // when the bell dropdown is open. On hidden tabs (visibilityState ===
  // "hidden"), we programmatically click the bell to surface the cards,
  // run the existing scanner, then click again to close. This catches
  // streak warnings without requiring the user to ever open the bell
  // themselves. Foreground tabs are never auto-clicked — the user would
  // see the dropdown flicker, and they can open it themselves.
  // -----------------------------------------------------------------------

  const BELL_AUTO_CLICK_MIN_INTERVAL_MS = 60 * 1000;
  const BELL_OPEN_RENDER_DELAY_MS = 2000;
  let _lastBellAutoClickAt = 0;
  let _lastBellBadgeCount = null;

  function findBellButton() {
    // Twitch's data-a-target is the most stable selector. Fall back to a
    // generic aria-label match if the attribute changes.
    return document.querySelector(
      '[data-a-target="onsite-notifications-toggle__button"], ' +
      '[data-a-target="onsite-notifications-toggle"], ' +
      'button[aria-label*="otification" i]'
    );
  }

  function parseBellBadgeCount(bellEl) {
    if (!bellEl) return null;
    // Aria-label commonly reads "Notifications: 3 unread" or
    // "3 unread Notifications" depending on locale. Both end up matching
    // the same digit-before-"unread" pattern.
    const label = bellEl.getAttribute("aria-label") || "";
    const m = label.match(/(\d+)\s*unread/i);
    if (m) return parseInt(m[1], 10);
    // Some builds render a small numeric badge as a child span instead
    // of (or in addition to) the aria-label.
    for (const child of bellEl.querySelectorAll("span, div")) {
      const t = (child.textContent || "").trim();
      if (/^\d{1,3}$/.test(t)) return parseInt(t, 10);
    }
    return 0;
  }

  function getBellBadgeCount() {
    return parseBellBadgeCount(findBellButton());
  }

  function isBellDropdownOpen() {
    // The dropdown lives in a portaled overlay. Twitch labels it with
    // role=dialog and an aria-label that mentions notifications. We also
    // accept the data-a-target attribute if it's present.
    return !!document.querySelector(
      '[data-a-target="onsite-notifications-popover"], ' +
      '[role="dialog"][aria-label*="otification" i]'
    );
  }

  async function maybeAutoOpenBell() {
    if (document.visibilityState !== "hidden") return;
    const now = Date.now();
    if (now - _lastBellAutoClickAt < BELL_AUTO_CLICK_MIN_INTERVAL_MS) return;
    const bell = findBellButton();
    if (!bell) return;
    const count = parseBellBadgeCount(bell);
    if (!count || count <= 0) {
      // Nothing unread, nothing to surface. Update baseline so the next
      // real increase fires a click.
      _lastBellBadgeCount = count || 0;
      return;
    }
    _lastBellAutoClickAt = now;
    const alreadyOpen = isBellDropdownOpen();
    try {
      if (!alreadyOpen) bell.click();
      await new Promise((r) => setTimeout(r, BELL_OPEN_RENDER_DELAY_MS));
      scanAndReport();
      // Close the dropdown only if we opened it ourselves. If the user
      // had it open before tabbing away we leave it alone.
      if (!alreadyOpen && isBellDropdownOpen()) {
        bell.click();
      }
      _lastBellBadgeCount = parseBellBadgeCount(findBellButton()) || 0;
      console.log(
        LOG_PREFIX,
        `Bell auto-surveillance fired (${count} unread before, ${_lastBellBadgeCount} after)`
      );
    } catch (e) {
      console.warn(LOG_PREFIX, "Bell auto-click failed:", e && e.message);
    }
  }

  function startBellSurveillance() {
    // Establish a baseline once the bell exists, then fire if the tab is
    // already hidden with unread notifications waiting (catches the case
    // where Stream Monitor opens a stream tab in the background and the
    // user never sees the bell at all).
    const baseline = () => {
      const bell = findBellButton();
      if (!bell) {
        setTimeout(baseline, 5000);
        return;
      }
      _lastBellBadgeCount = parseBellBadgeCount(bell) || 0;
      if (document.visibilityState === "hidden" && _lastBellBadgeCount > 0) {
        maybeAutoOpenBell();
      }
      attachBellBadgeObserver(bell);
    };
    setTimeout(baseline, 10000);

    // Periodic backstop: every minute, retry on hidden tabs. The internal
    // throttle inside maybeAutoOpenBell prevents this from spamming.
    setInterval(maybeAutoOpenBell, 60 * 1000);

    // React the moment the tab transitions to hidden. Most often, the
    // user has just alt-tabbed away from a Twitch stream and any unread
    // notifications can now be surfaced silently.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        setTimeout(maybeAutoOpenBell, 3000);
      }
    });
  }

  function attachBellBadgeObserver(bell) {
    if (typeof MutationObserver !== "function") return;
    const obs = new MutationObserver(() => {
      const count = parseBellBadgeCount(bell);
      if (count === null) return;
      if (_lastBellBadgeCount !== null && count > _lastBellBadgeCount) {
        // Badge bumped, almost always a new notification arrived via WS.
        maybeAutoOpenBell();
      }
      _lastBellBadgeCount = count;
    });
    obs.observe(bell, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["aria-label"],
    });
  }

  // Expose parser for jsdom tests when running outside the browser. Guarded
  // so production browser execution is unaffected.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseStreakText, parseBellBadgeCount };
  }

  // -----------------------------------------------------------------------
  // Init — start error checking immediately, playback control on command
  // -----------------------------------------------------------------------

  console.log(LOG_PREFIX, "Content script loaded on", window.location.href);
  startErrorChecking();
  startStreakMonitor();
  startBellSurveillance();
})();
