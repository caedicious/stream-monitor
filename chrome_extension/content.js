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
  // Init — start error checking immediately, playback control on command
  // -----------------------------------------------------------------------

  console.log(LOG_PREFIX, "Content script loaded on", window.location.href);
  startErrorChecking();
})();
