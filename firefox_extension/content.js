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
  // True inside a twitch.tv frame embedded by another page (a multistream
  // site framing Twitch chat, v1.11.1). Comparing window references never
  // throws, even when the parent is cross-origin.
  const IN_FRAME = window !== window.top;

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

  // Only the top-level page answers the background's tab messages; a
  // frame answering first would shadow it.
  if (!IN_FRAME) browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
        claimBonusIfPresent("keepalive");
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

      case "scanSaveStreak": {
        // Rescue sweep: report every /save-streak/<slug> link visible in
        // this page (sidebar "Save your Streak" entry, bell cards,
        // notification page rows). The background dedups across tabs.
        const slugs = new Set();
        for (const a of document.querySelectorAll('a[href*="/save-streak/"]')) {
          const href = a.getAttribute("href") || "";
          const m = href.match(/\/save-streak\/([a-zA-Z0-9_]+)/i);
          if (m) slugs.add(m[1].toLowerCase());
        }
        sendResponse({ slugs: Array.from(slugs) });
        break;
      }

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

  // The count may carry thousands separators ("1,024-stream"); a plain
  // count matches exactly as before.
  const STREAK_BROKE_RE =
    /Your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+on\s+([^\s!.,]+)\s+broke/i;
  const STREAK_IN_DANGER_RE =
    /Your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+on\s+([^\s!.,]+)\s+(?:ends|expires)\s+in\s+(\d+)\s*(h|hours?|d|days?|m|mins?|minutes?)/i;

  // A streak count as matched above, separators removed.
  function parseStreakCount(text) {
    return parseInt(text.replace(/,/g, ""), 10);
  }

  function parseStreakText(text) {
    if (!text || text.length > 600) return null;
    let m = STREAK_BROKE_RE.exec(text);
    if (m) {
      return {
        status: "broke",
        streamer: m[2].toLowerCase(),
        count: parseStreakCount(m[1]),
        deadline_hours: 24,
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
        count: parseStreakCount(m[1]),
        deadline_hours: hours,
      };
    }
    return null;
  }

  const _seenStreakEvents = new Set();

  function _streakKey(ev) {
    return `${ev.status}:${ev.streamer}:${ev.count}`;
  }

  function parseTimeAgoSeconds(text) {
    if (!text) return null;
    if (/just\s*now/i.test(text)) return 0;
    const m = text.match(
      /(\d+)\s*(seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|hrs?|h\b|days?|d\b|weeks?|w\b|months?|mo\b|years?|y\b)\s*ago/i
    );
    if (!m) return null;
    const n = parseInt(m[1], 10);
    const unit = m[2].toLowerCase();
    if (/^s(ec|$)/.test(unit)) return n;
    if (/^m(in|$)/.test(unit)) return n * 60;
    if (/^h(our|r|$)/.test(unit)) return n * 3600;
    if (/^d(ay|$)/.test(unit)) return n * 86400;
    if (/^w(eek|$)/.test(unit)) return n * 604800;
    if (/^mo(nth)?$/.test(unit)) return n * 2592000;
    if (/^y(ear|$)/.test(unit)) return n * 31536000;
    return null;
  }

  function getCardAgeSeconds(streakEl) {
    let node = streakEl;
    for (let i = 0; i < 8 && node; i++) {
      const text = (node.textContent || "").slice(0, 1500);
      const age = parseTimeAgoSeconds(text);
      if (age !== null) return age;
      node = node.parentElement;
    }
    return null;
  }

  function scanForStreakEvents() {
    const candidates = document.body
      ? document.body.querySelectorAll("a, p, span, div, article, li")
      : [];
    const found = [];
    for (const el of candidates) {
      if (el.children.length > 4) continue;
      const text = (el.textContent || "").trim();
      if (text.length < 12 || !/streak/i.test(text)) continue;
      const ev = parseStreakText(text);
      if (ev) {
        // Skip stale notification cards. Twitch keeps old "your N-stream
        // streak broke" cards around in the bell inbox indefinitely.
        const ageSec = getCardAgeSeconds(el);
        if (ageSec !== null && ageSec > 24 * 3600) {
          continue;
        }
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
      browser.runtime.sendMessage({
        type: "streak_event",
        event: {
          ...ev,
          detected_at: new Date().toISOString(),
          page_url: window.location.href,
        },
      }).catch(() => {});
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
    if (_streakScanTimer) return;
    setTimeout(scanAndReport, 5000);
    _streakScanTimer = setInterval(scanAndReport, 60000);

    if (typeof MutationObserver === "function" && document.body) {
      const obs = new MutationObserver((mutations) => {
        if (obs._pending) return;
        // Relevance gate: Twitch chat churns the DOM constantly, and a
        // full-document sweep every debounce window burns CPU for hours
        // on a live chat page. Only schedule a sweep when some ADDED
        // node's text actually mentions "streak". The 60s interval scan
        // above remains the correctness backstop.
        let relevant = false;
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            const text = node.textContent;
            if (text && text.length >= 12 && /streak/i.test(text)) {
              relevant = true;
              break;
            }
          }
          if (relevant) break;
        }
        if (!relevant) return;
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
  // Save-streak page check (v1.11.2). Twitch's own "Save your streak" link
  // sometimes lands on a card reading "No Content Eligible / You've already
  // maintained your N-stream streak with X. Keep'em going by watching more
  // live streams!" That means the streak is safe and the "broke" card that
  // led here is stale. Report it once per visit to a /save-streak/<login>
  // page so the background can close the tab (or end its rescue turn) and
  // the desktop can ignore further "broke" cards for X until X goes live
  // again. Twitch changes pages in place (history.pushState, no new
  // document), so the top frame follows its path and arms a fresh check
  // on every arrival at a save-streak page.
  // -----------------------------------------------------------------------
  const ALREADY_SAVED_RE =
    /already\s+maintained\s+your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+with\s+([^\s!.,]+)/i;
  // Twitch renders the card after the channel page itself: scan at these
  // delays after arriving, then on relevant mutations.
  const SAVE_STREAK_SCAN_DELAYS_MS = [3000, 8000, 15000];
  // Backstop for a card that renders late or changes its text in place,
  // which the observer (added nodes only) cannot see. Bounded so a page
  // left open for hours stops scanning.
  const SAVE_STREAK_RESCAN_MS = 30000;
  const SAVE_STREAK_RESCAN_WINDOW_MS = 10 * 60 * 1000;
  // In-app navigation fires no event a content script can hear, so the
  // top frame compares its path on this cadence.
  const ROUTE_POLL_MS = 1000;

  let _saveStreakRoute = null; // routeKey() the current check belongs to
  let _saveStreakReported = false; // this visit already reported
  let _saveStreakTimers = []; // one-shot scans for this visit
  let _saveStreakRescanTimer = null;
  let _saveStreakMutationScan = null; // the pending 500 ms rescan, if any
  let _saveStreakObserver = null;

  // {count, streamer} for a block of text with the "already maintained"
  // wording, else null. The streamer here is Twitch's display name; the
  // caller prefers the login from the page path.
  function parseAlreadySavedText(text) {
    if (!text || text.length > 600) return null;
    const m = ALREADY_SAVED_RE.exec(text);
    if (!m) return null;
    return { count: parseStreakCount(m[1]), streamer: m[2].toLowerCase() };
  }

  function isSaveStreakPage() {
    const seg = window.location.pathname.split("/").filter(Boolean);
    return seg[0] === "save-streak" && !!seg[1];
  }

  // The path without a trailing slash or letter case, so Twitch tidying
  // the URL in place does not count as a new visit.
  function routeKey() {
    return window.location.pathname.split("/").filter(Boolean).join("/").toLowerCase();
  }

  // Twitch display names are the login in other capitals, or a localized
  // name that cannot be compared. A login-like name that does not start
  // with this page's login belongs to another streamer: a card left over
  // from the page before an in-app navigation.
  function cardNameFitsLogin(name, login) {
    return !/^[a-z0-9_]+$/.test(name) || name.startsWith(login);
  }

  function scanForAlreadySaved() {
    if (_saveStreakReported || !document.body || !isSaveStreakPage()) return false;
    // A scan queued for an earlier path must not report under this one;
    // the route watcher re-arms for the new path.
    if (routeKey() !== _saveStreakRoute) return false;
    const login = currentStreamerSlug();
    const candidates = document.body.querySelectorAll("p, span, div, h1, h2, h3, h4, article");
    for (const el of candidates) {
      if (el.children.length > 4) continue;
      const text = (el.textContent || "").trim();
      if (text.length < 20 || !/maintained/i.test(text)) continue;
      const parsed = parseAlreadySavedText(text);
      if (!parsed || !cardNameFitsLogin(parsed.streamer, login)) continue;
      _saveStreakReported = true;
      stopSaveStreakCheck();
      const streamer = login || parsed.streamer;
      try {
        browser.runtime.sendMessage({
          type: "streak_event",
          event: {
            status: "already_saved",
            streamer,
            count: parsed.count,
            detected_at: new Date().toISOString(),
            page_url: window.location.href,
          },
        }).catch(() => {});
        console.log(LOG_PREFIX, `Streak already saved for ${streamer} (${parsed.count}-stream); reported`);
      } catch (e) {
        console.warn(LOG_PREFIX, "Failed to report saved streak:", e && e.message);
      }
      return true;
    }
    return false;
  }

  // Cancels every pending scan and detaches the observer.
  function stopSaveStreakCheck() {
    for (const t of _saveStreakTimers) clearTimeout(t);
    _saveStreakTimers = [];
    if (_saveStreakRescanTimer) {
      clearInterval(_saveStreakRescanTimer);
      _saveStreakRescanTimer = null;
    }
    if (_saveStreakMutationScan) {
      clearTimeout(_saveStreakMutationScan);
      _saveStreakMutationScan = null;
    }
    if (_saveStreakObserver) {
      _saveStreakObserver.disconnect();
      _saveStreakObserver = null;
    }
  }

  // Starts the check for the page the top frame is on now (a no-op beyond
  // cleanup when that is not a save-streak page).
  function armSaveStreakCheck() {
    stopSaveStreakCheck();
    _saveStreakRoute = routeKey();
    _saveStreakReported = false;
    if (!isSaveStreakPage()) return;
    for (const delay of SAVE_STREAK_SCAN_DELAYS_MS) {
      _saveStreakTimers.push(setTimeout(scanForAlreadySaved, delay));
    }
    const rescanUntil = Date.now() + SAVE_STREAK_RESCAN_WINDOW_MS;
    _saveStreakRescanTimer = setInterval(() => {
      if (Date.now() > rescanUntil) {
        clearInterval(_saveStreakRescanTimer);
        _saveStreakRescanTimer = null;
        return;
      }
      scanForAlreadySaved();
    }, SAVE_STREAK_RESCAN_MS);
    if (typeof MutationObserver === "function" && document.body) {
      const obs = new MutationObserver((mutations) => {
        // One full-page rescan per burst of relevant mutations.
        if (_saveStreakMutationScan) return;
        let relevant = false;
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            const text = node.textContent;
            if (text && /maintained/i.test(text)) {
              relevant = true;
              break;
            }
          }
          if (relevant) break;
        }
        if (!relevant) return;
        _saveStreakMutationScan = setTimeout(() => {
          _saveStreakMutationScan = null;
          scanForAlreadySaved();
        }, 500);
      });
      obs.observe(document.body, { childList: true, subtree: true });
      _saveStreakObserver = obs;
    }
  }

  function startSaveStreakCheck() {
    armSaveStreakCheck();
    setInterval(() => {
      if (routeKey() !== _saveStreakRoute) armSaveStreakCheck();
    }, ROUTE_POLL_MS);
  }

  // -----------------------------------------------------------------------
  // Bell surveillance — Twitch only renders notification cards into the DOM
  // when the bell dropdown is open. On hidden tabs (visibilityState ===
  // "hidden"), we programmatically click the bell to surface the cards,
  // run the existing scanner, then click again to close. This catches
  // streak warnings without requiring the user to ever open the bell
  // themselves. Foreground tabs are never auto-clicked.
  // -----------------------------------------------------------------------

  const BELL_AUTO_CLICK_MIN_INTERVAL_MS = 60 * 1000;
  const BELL_OPEN_RENDER_DELAY_MS = 2000;
  let _lastBellAutoClickAt = 0;
  let _lastBellBadgeCount = null;

  function findBellButton() {
    return document.querySelector(
      '[data-a-target="onsite-notifications-toggle__button"], ' +
      '[data-a-target="onsite-notifications-toggle"], ' +
      'button[aria-label*="otification" i]'
    );
  }

  function parseBellBadgeCount(bellEl) {
    if (!bellEl) return null;
    const label = bellEl.getAttribute("aria-label") || "";
    const m = label.match(/(\d+)\s*unread/i);
    if (m) return parseInt(m[1], 10);
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
      _lastBellBadgeCount = count || 0;
      return;
    }
    _lastBellAutoClickAt = now;
    const alreadyOpen = isBellDropdownOpen();
    try {
      if (!alreadyOpen) bell.click();
      await new Promise((r) => setTimeout(r, BELL_OPEN_RENDER_DELAY_MS));
      scanAndReport();
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

    setInterval(maybeAutoOpenBell, 60 * 1000);

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

  // -----------------------------------------------------------------------
  // Channel points bonus (v1.11.0): click Twitch's "Claim Bonus" chest when
  // it appears in the chat's points area, on any Twitch tab. Off via the
  // popup's "Auto-claim bonus points" toggle (default on).
  //
  // The claimable state is Twitch's own button; this only clicks it, the
  // same way the player strategy clicks Twitch's own play button. The chest
  // icon's class is the primary anchor because it is locale-independent;
  // the English aria-label is only a fallback. The chest stays claimable
  // until clicked, so a 5s poll (throttled to about once a minute in a
  // background tab) plus the unthrottled keepalive tick is plenty.
  // -----------------------------------------------------------------------

  const BONUS_POLL_MS = 5000;
  const BONUS_CLICK_COOLDOWN_MS = 10000;
  let autoClaimBonusEnabled = true;
  let _lastBonusClickMs = 0;
  let _bonusPollTimer = null;

  function findBonusClaimButton(root) {
    const doc = root || document;
    const icon = doc.querySelector(".claimable-bonus__icon");
    const fromIcon = icon && icon.closest("button");
    if (fromIcon) return fromIcon;
    return doc.querySelector('button[aria-label*="Claim Bonus" i]');
  }

  function currentStreamerSlug() {
    const seg = window.location.pathname.split("/").filter(Boolean);
    if ((seg[0] === "save-streak" || seg[0] === "embed" || seg[0] === "popout") && seg[1]) {
      return seg[1].toLowerCase();
    }
    return seg[0] ? seg[0].toLowerCase() : "";
  }

  function claimBonusIfPresent(reason) {
    if (!autoClaimBonusEnabled) return false;
    const now = Date.now();
    if (now - _lastBonusClickMs < BONUS_CLICK_COOLDOWN_MS) return false;
    let button = null;
    try {
      button = findBonusClaimButton();
    } catch (e) {
      return false;
    }
    // getClientRects is empty for display:none, unlike offsetParent, which
    // is also null for position:fixed ancestors.
    if (!button || button.disabled || button.getClientRects().length === 0) return false;
    _lastBonusClickMs = now;
    button.click();
    console.log(LOG_PREFIX, `Claimed channel points bonus (${reason})`);
    try {
      browser.runtime.sendMessage({
        type: "bonus_claimed",
        streamer: currentStreamerSlug(),
        page_url: window.location.href,
      });
    } catch (e) {
      // Background asleep or the extension reloading: the claim itself
      // already happened, only the log line is lost.
    }
    return true;
  }

  function startBonusClaimer() {
    if (_bonusPollTimer) return;
    browser.storage.local.get("autoClaimBonus")
      .then((result) => {
        autoClaimBonusEnabled = (result && result.autoClaimBonus) ?? true;
      })
      .catch(() => {});

    browser.storage.onChanged.addListener((changes, area) => {
      if (area === "local" && changes.autoClaimBonus) {
        autoClaimBonusEnabled = changes.autoClaimBonus.newValue ?? true;
      }
    });
    _bonusPollTimer = setInterval(() => claimBonusIfPresent("poll"), BONUS_POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") claimBonusIfPresent("visible");
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseStreakText, parseBellBadgeCount, findBonusClaimButton, parseAlreadySavedText };
  }

  // -----------------------------------------------------------------------
  // Init — start error checking immediately, playback control on command
  // -----------------------------------------------------------------------

  console.log(LOG_PREFIX, IN_FRAME ? "Content script loaded in a frame on" : "Content script loaded on", window.location.href);
  // Inside an embedded Twitch frame only the bonus claimer runs: the
  // player, streak and bell features belong to the top-level page.
  if (!IN_FRAME) {
    startErrorChecking();
    startStreakMonitor();
    startSaveStreakCheck();
    startBellSurveillance();
  }
  startBonusClaimer();
})();
