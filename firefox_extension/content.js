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
    return document.querySelector("video");
  }

  let playFailCount = 0;

  function ensurePlaying(video) {
    if (!video) return;

    // Unmute and set volume only when video is already playing —
    // if paused, we handle mute state carefully in the play logic below
    if (!video.paused) {
      if (video.muted) {
        video.muted = false;
        console.log(LOG_PREFIX, "Unmuted video player");
      }
      if (video.volume < 0.01) {
        video.volume = 0.05;
        console.log(LOG_PREFIX, "Set video volume to 5%");
      }
    }

    // Ensure video is playing
    if (video.paused) {
      // Browsers block unmuted autoplay in background tabs. Muted autoplay
      // is always allowed. Strategy: mute the video element, start playback,
      // then unmute. The browser tab is already muted via auto-mute so the
      // user won't hear anything during the brief muted window.
      const wasMuted = video.muted;
      video.muted = true;
      video.play().then(() => {
        // Playback started — now unmute the player so Twitch counts the viewer
        video.muted = false;
        console.log(LOG_PREFIX, "Started video playback (mute-start-unmute)");
        playFailCount = 0;
      }).catch((e) => {
        video.muted = wasMuted; // restore original state on failure
        playFailCount++;
        console.warn(LOG_PREFIX, `Could not auto-play (attempt ${playFailCount}):`, e.message);
        if (playFailCount >= 3) {
          console.log(LOG_PREFIX, "Requesting background to reload tab");
          browser.runtime.sendMessage({ action: "reloadTab" }).catch(() => {});
          playFailCount = 0;
        }
      });
    } else {
      playFailCount = 0;
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
  // independently of focus, and browser.alarms (which drives the keepalive)
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
      window.location.reload();
    }, ERROR_RELOAD_GRACE_MS);
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
    // Keepalive is now driven by the background script via browser.alarms,
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
    // Twitch shows these elements when the stream is offline or errored
    const errorSelectors = [
      '[data-a-target="player-overlay-content-gate"]',
      '[data-a-target="player-error-message"]',
      '.content-overlay-gate',
    ];

    for (const selector of errorSelectors) {
      const el = document.querySelector(selector);
      if (el && el.offsetParent !== null) {
        return true;
      }
    }

    // Check if video element exists but has stalled
    const video = getVideoElement();
    if (video && video.readyState < 2 && !video.paused && video.currentTime === 0) {
      return true;
    }

    return false;
  }

  // Periodically check for errors and report to background
  let errorCheckTimer = null;
  let lastErrorReport = 0;
  const ERROR_CHECK_INTERVAL_MS = 15000;
  const ERROR_REPORT_COOLDOWN_MS = 60000;

  function startErrorChecking() {
    if (errorCheckTimer) return;
    errorCheckTimer = setInterval(() => {
      if (checkForErrors()) {
        const now = Date.now();
        if (now - lastErrorReport > ERROR_REPORT_COOLDOWN_MS) {
          lastErrorReport = now;
          console.log(LOG_PREFIX, "Stream error detected, notifying background");
          browser.runtime.sendMessage({ action: "tabError" }).catch(() => {});
        }
      }
    }, ERROR_CHECK_INTERVAL_MS);
  }

  // -----------------------------------------------------------------------
  // Message handler — receives commands from the background script
  // -----------------------------------------------------------------------

  browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
