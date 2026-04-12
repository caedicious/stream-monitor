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

  // -----------------------------------------------------------------------
  // Video element helpers
  // -----------------------------------------------------------------------

  function getVideoElement() {
    return document.querySelector("video");
  }

  function ensurePlaying(video) {
    if (!video) return;

    // Unmute the player (this is the Twitch player mute, not the browser tab)
    if (video.muted) {
      video.muted = false;
      console.log(LOG_PREFIX, "Unmuted video player");
    }

    // Set volume to audible level if it was zeroed
    if (video.volume < 0.01) {
      video.volume = 0.05;
      console.log(LOG_PREFIX, "Set video volume to 5%");
    }

    // Ensure video is playing
    if (video.paused) {
      video.play().then(() => {
        console.log(LOG_PREFIX, "Started video playback");
      }).catch((e) => {
        console.warn(LOG_PREFIX, "Could not auto-play:", e.message);
      });
    }
  }

  // -----------------------------------------------------------------------
  // Low quality mode — interact with Twitch's settings menu via DOM
  // -----------------------------------------------------------------------

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
        // Look for quality options — try to find the lowest available
        const qualityOptions = document.querySelectorAll('[data-a-target="player-settings-menu"] input[type="radio"]');
        if (qualityOptions.length === 0) {
          // Try alternate selector for quality list items
          const qualityLabels = document.querySelectorAll('[data-a-target="player-settings-menu"] [role="menuitemradio"]');
          if (qualityLabels.length > 0) {
            // Pick the last option (lowest quality)
            const lowest = qualityLabels[qualityLabels.length - 1];
            lowest.click();
            lowQualityApplied = true;
            console.log(LOG_PREFIX, "Set to lowest quality via menuitemradio");
            return;
          }

          // Close the menu if we can't find options
          settingsBtn.click();
          console.log(LOG_PREFIX, "Quality options not found, will retry");
          return;
        }

        // Select the last radio button (lowest quality)
        const lowest = qualityOptions[qualityOptions.length - 1];
        lowest.click();
        lowQualityApplied = true;
        console.log(LOG_PREFIX, "Set to lowest quality via radio button");
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

      case "getStatus":
        sendResponse({
          hasVideo: !!getVideoElement(),
          ensurePlayback: ensurePlaybackEnabled,
          lowQuality: lowQualityEnabled,
          lowQualityApplied,
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
