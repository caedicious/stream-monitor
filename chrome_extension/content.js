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

  // -----------------------------------------------------------------------
  // Low quality, bounded. The quality step used to retry on every poll
  // (3 s) for as long as it failed, opening and closing the player's
  // settings menu each time, and every activation from the background
  // (each time a tracked tab finished loading) reset it, so a page whose
  // quality menu could not be found or read redid it forever. Now:
  // - nothing is tried until a stream picture shows (a non-ad video with
  //   videoHeight above 0) and no ad is on screen, so a page with nothing
  //   playing never has its menu opened;
  // - each channel page gets LOW_QUALITY_FAST_ATTEMPTS spaced attempts,
  //   then one every LOW_QUALITY_RETRY_GAP_MS, only while the tab is in
  //   the background so a viewer never sees the menu flash; never two at
  //   once;
  // - an attempt waits up to LOW_QUALITY_ELEMENT_WAIT_MS for the menu to
  //   render, and stops at once when the list offers nothing below Source
  //   (then it tries again in an hour);
  // - the menu is opened and closed by its actual state, never a blind
  //   toggle, and an attempt cut off by a page change closes what it
  //   opened;
  // - a repeated activation for the same channel does not start over; a
  //   new channel (a reload, or an in-place move the poll notices) does;
  // - an already selected lowest option is left alone, a quality the
  //   viewer picks by hand is respected until the page reloads (the
  //   extension's own recovery reloads included), and turning Low quality
  //   mode off stops it in open tabs, even mid-attempt.
  // Nothing is stored in the page.
  // -----------------------------------------------------------------------
  const LOW_QUALITY_FAST_ATTEMPTS = 5;
  const LOW_QUALITY_ATTEMPT_GAPS_MS = [0, 5000, 15000, 45000, 120000];
  const LOW_QUALITY_RETRY_GAP_MS = 3 * 60 * 1000;
  const LOW_QUALITY_NOTHING_LOWER_GAP_MS = 60 * 60 * 1000;
  const LOW_QUALITY_ELEMENT_WAIT_MS = 3000;
  const LOW_QUALITY_ELEMENT_POLL_MS = 250;
  const LOW_QUALITY_MENU = '[data-a-target="player-settings-menu"]';
  const LOW_QUALITY_AD_PLAYER = '[data-a-target*="ax-overlay"], [data-a-target*="outstream"]';
  const LOW_QUALITY_AD_LABELS = '[data-a-target="video-ad-label"], [data-a-target="video-ad-countdown"]';
  let lowQualityPage = null;
  let lowQualityAttempts = 0;
  let lowQualityNextAttemptAt = 0;
  let lowQualityInFlight = 0; // token of the attempt in progress, 0 when none
  let lowQualityTokenSeq = 0;
  let lowQualityWaitLogged = false;
  let lowQualityUserPicked = false;
  let lowQualityReleased = false; // the background let this tab go

  // The channel the tab shows (save-streak, embed and popout pages count as
  // their channel), so URL tidying and sub-pages do not start over.
  function lowQualityPageKey() {
    return (currentStreamerSlug() || window.location.pathname).toLowerCase();
  }

  // Start over only when the tab shows a different channel. A repeated
  // activation for the same one keeps its attempts and its outcome.
  function syncLowQualityPage() {
    const page = lowQualityPageKey();
    if (page === lowQualityPage) return;
    lowQualityPage = page;
    lowQualityApplied = false;
    lowQualityAttempts = 0;
    lowQualityNextAttemptAt = 0;
    lowQualityInFlight = 0; // an attempt on the previous page no longer counts
    lowQualityWaitLogged = false;
  }

  function isShown(el) {
    return !!el && el.getClientRects().length > 0;
  }

  function settingsMenuOpen() {
    return isShown(document.querySelector(LOW_QUALITY_MENU));
  }

  // Open or close the settings menu by its actual state, never a blind
  // toggle: a stray click must not turn every later attempt inside out.
  function setSettingsMenu(open) {
    if (settingsMenuOpen() === open) return;
    const btn = document.querySelector('[data-a-target="player-settings-button"]');
    if (btn) btn.click();
  }

  // A non-ad video with a picture. Not getVideoElement: in Firefox that
  // takes the first video, which can be an idle ad player.
  function streamPictureShowing() {
    return Array.from(document.querySelectorAll("video")).some(
      (v) => v.videoHeight > 0 && !v.closest(LOW_QUALITY_AD_PLAYER));
  }

  // An ad on screen: its label, or a picture in the ad player. The ad
  // player itself sits in the page even when idle, so it does not count.
  function adShowing() {
    return Array.from(document.querySelectorAll(LOW_QUALITY_AD_LABELS)).some(isShown) ||
      Array.from(document.querySelectorAll("video")).some(
        (v) => v.videoHeight > 0 && !!v.closest(LOW_QUALITY_AD_PLAYER));
  }

  // Calls done(result) as soon as find() returns one, or done(null) after
  // LOW_QUALITY_ELEMENT_WAIT_MS.
  function waitForElement(find, done, waitedMs = 0) {
    const found = find();
    if (found || waitedMs >= LOW_QUALITY_ELEMENT_WAIT_MS) {
      done(found || null);
      return;
    }
    setTimeout(() => waitForElement(find, done, waitedMs + LOW_QUALITY_ELEMENT_POLL_MS), LOW_QUALITY_ELEMENT_POLL_MS);
  }

  // The lowest option the open Quality menu offers (radio inputs first,
  // then role="menuitemradio" items); { none: true } when options are
  // there but nothing ranks below Source; null while nothing is rendered.
  function findLowestOffered() {
    let rendered = false;
    for (const [selector, kind] of [
      [LOW_QUALITY_MENU + ' input[type="radio"]', ""],
      [LOW_QUALITY_MENU + ' [role="menuitemradio"]', " (menuitemradio)"],
    ]) {
      const opts = document.querySelectorAll(selector);
      if (opts.length === 0) continue;
      rendered = true;
      const { option, label } = pickLowestQualityOption(opts);
      if (option) return { option, label, kind };
    }
    return rendered ? { none: true } : null;
  }

  // Twitch keeps the checked state on the radio inside the row, not on
  // the menuitemradio element that gets picked.
  function isOptionChecked(opt) {
    return opt.checked === true || opt.getAttribute("aria-checked") === "true" ||
      !!(opt.querySelector && opt.querySelector('input[type="radio"]:checked'));
  }

  function applyLowQuality() {
    syncLowQualityPage();
    if (lowQualityApplied || lowQualityInFlight || lowQualityUserPicked) return;
    const now = Date.now();
    if (now < lowQualityNextAttemptAt) return;

    if (!streamPictureShowing() || adShowing() || !document.querySelector('[data-a-target="player-settings-button"]')) {
      // Nothing is opened until a stream plays, so waiting costs no
      // attempt; say so once per page, not on every poll.
      if (!lowQualityWaitLogged) {
        lowQualityWaitLogged = true;
        console.log(LOG_PREFIX, "Waiting for a playing stream before setting the quality");
      }
      return;
    }
    // After the first few tries, only try while nobody is looking.
    if (lowQualityAttempts >= LOW_QUALITY_FAST_ATTEMPTS && document.visibilityState !== "hidden") return;

    lowQualityAttempts++;
    const fast = lowQualityAttempts < LOW_QUALITY_FAST_ATTEMPTS;
    lowQualityNextAttemptAt = now + (fast ? LOW_QUALITY_ATTEMPT_GAPS_MS[lowQualityAttempts] : LOW_QUALITY_RETRY_GAP_MS);
    const token = ++lowQualityTokenSeq;
    lowQualityInFlight = token;
    const current = () => lowQualityInFlight === token;
    // Cut off by a page change: close what this attempt opened, unless a
    // newer attempt already runs.
    const abandon = () => {
      if (lowQualityInFlight === 0) setSettingsMenu(false);
    };
    const attempt = `attempt ${lowQualityAttempts}`;
    const fail = (why, gapMs) => {
      setSettingsMenu(false);
      lowQualityInFlight = 0;
      if (gapMs) lowQualityNextAttemptAt = Date.now() + gapMs;
      console.log(LOG_PREFIX, `${why} (${attempt})`);
    };

    const pick = (picked) => {
      if (!current()) {
        abandon();
        return;
      }
      if (!lowQualityEnabled) {
        fail("Low quality mode turned off");
        return;
      }
      if (!picked) {
        fail("Quality options not found");
        return;
      }
      if (picked.none) {
        fail("Nothing below Source is offered; trying again in an hour", LOW_QUALITY_NOTHING_LOWER_GAP_MS);
        return;
      }
      if (isOptionChecked(picked.option)) {
        setSettingsMenu(false); // already there: close without changing anything
        console.log(LOG_PREFIX, `Already at the lowest quality${picked.kind}: ${picked.label}`);
      } else {
        picked.option.click();
        console.log(LOG_PREFIX, `Set to lowest quality${picked.kind}: ${picked.label}`);
        // Twitch closes the menu after a pick; close it if it did not.
        setTimeout(() => {
          if (!lowQualityInFlight) setSettingsMenu(false);
        }, 500);
      }
      lowQualityApplied = true;
      lowQualityInFlight = 0;
    };

    setSettingsMenu(true);
    // A menu that was already open may already show the quality list.
    const qualityItemOrList = () =>
      findLowestOffered() || document.querySelector('[data-a-target="player-settings-menu-item-quality"]');
    waitForElement(qualityItemOrList, (found) => {
      if (!current()) {
        abandon();
        return;
      }
      if (!lowQualityEnabled) {
        fail("Low quality mode turned off");
        return;
      }
      if (!found) {
        fail("Quality menu item not found");
        return;
      }
      if (found.option || found.none) {
        pick(found);
        return;
      }
      found.click();
      waitForElement(findLowestOffered, pick);
    });
  }

  // A quality option the viewer clicks by hand is theirs: this page is
  // left alone from then on. Twitch handles the click on the whole row,
  // which is much wider than the option's text, so a click anywhere in a
  // row that holds exactly one option counts (a toggle row holds none).
  // The extension's own clicks are not trusted events.
  function isQualityPickClick(target) {
    const menu = target && target.closest ? target.closest(LOW_QUALITY_MENU) : null;
    if (!menu || target === menu) return false;
    if (target.closest('input[type="radio"], [role="menuitemradio"]')) return true;
    for (let el = target; el && el !== menu; el = el.parentElement) {
      const n = el.querySelectorAll('input[type="radio"]').length ||
        el.querySelectorAll('[role="menuitemradio"]').length;
      if (n === 1) return true; // the one row this click belongs to
      if (n > 1) return false; // a container of several rows
    }
    return false;
  }

  // Top-level page only, like the rest of the player features.
  if (!IN_FRAME) {
    document.addEventListener("click", (e) => {
      if (!e.isTrusted || lowQualityUserPicked || !isQualityPickClick(e.target)) return;
      lowQualityUserPicked = true;
      console.log(LOG_PREFIX, "Quality picked by hand in this tab; low quality mode leaves it alone");
    }, true);

    // Turning Low quality mode off stops the step in open tabs; turning it
    // on resumes it in tabs the extension is already controlling.
    try {
      (typeof browser !== "undefined" ? browser : chrome).storage.onChanged.addListener((changes, area) => {
        if (area !== "local" || !changes.lowQuality) return;
        if (!changes.lowQuality.newValue) {
          lowQualityEnabled = false;
        } else if (ensurePlaybackEnabled && !lowQualityReleased) {
          lowQualityEnabled = true;
          startPolling();
        }
      });
    } catch (e) {
      // No storage events in this context: the activation message still
      // carries the setting.
    }
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

    if (lowQualityEnabled) {
      // Notice an in-place move to another channel even after this one is done.
      syncLowQualityPage();
      if (!lowQualityApplied) applyLowQuality();
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

  // Only the top-level page answers the background's tab messages; a
  // frame answering first would shadow it.
  if (!IN_FRAME) chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    switch (message.action) {
      case "ensurePlaying":
        ensurePlaybackEnabled = true;
        startPolling();
        startKeepalive();
        sendResponse({ ok: true });
        break;

      case "setLowQuality":
        lowQualityEnabled = message.enabled !== false;
        lowQualityReleased = !lowQualityEnabled;
        // The background sends this on every activation (each time a tracked
        // tab finishes loading). It must not start the quality step over for
        // the same channel; a different channel does (syncLowQualityPage).
        syncLowQualityPage();
        if (lowQualityEnabled) {
          startPolling();
        }
        sendResponse({ ok: true });
        break;

      case "keepalive":
        // Triggered by background script's alarm — not throttled
        keepalive();
        claimBonusIfPresent("keepalive");
        maybeRunBellBackstop();
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
          bellFound: !!findOpenCheckBell(),
          lastBellCheckAt: _lastBellCheckAt,
          lastBellCheckResult: _lastBellCheckResult,
        });
        break;

      case "scanSaveStreak": {
        // Rescue sweep: every /save-streak/<login> link where a card could
        // be (the dropdown, a notifications page outside chat) or in the
        // sidebar, with the age of the card it sits in. A link pasted in
        // chat is never listed. The background dedups across tabs.
        sendResponse(saveStreakScanReply());
        break;
      }

      case "checkBell":
        // The open check answers when it is done (up to about a minute
        // while you use the page): the only action that answers late.
        runOpenBellCheck(message.mode === "covered" ? "covered" : "open").then(sendResponse);
        return true;

      default:
        sendResponse({ ok: false, error: "unknown action" });
    }
    return false; // Synchronous response
  });

  // -----------------------------------------------------------------------
  // Streak monitor. Reads Twitch's "Your N-stream streak on X broke / ends
  // in Yh" notification cards and relays them to the background, which
  // posts them to the desktop app. Twitch offers no streak API, so this is
  // a text scrape that does not depend on Twitch's class names.
  //
  // Scope (1.12): a candidate counts only inside the notifications dropdown
  // (source "bell"), or on a page that lists notifications and shows no
  // chat (STREAK_PAGE_SCAN_PATHS, source "page") outside anything that looks
  // like chat. Nothing else is read, a channel page's chat above all, so a
  // chatter typing a card's sentence creates no event. Save-streak links
  // count in those places and in the sidebar.
  // -----------------------------------------------------------------------

  // The card sentences. tests/test_streak_regex_fixtures.py runs them
  // through Python's re, so they use only syntax both engines accept. The
  // count may carry thousands separators ("1,024-stream"); "on" or "with"
  // comes before the name; an in-danger window reads "ends in", "expires
  // in", "in the next" or "within the next", and its unit ends at a word
  // boundary, so "3 months" is never read as minutes.
  const STREAK_BROKE_RE =
    /Your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+(?:on|with)\s+([^\s!.,]+)\s+broke/i;
  const STREAK_IN_DANGER_RE =
    /Your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+(?:on|with)\s+([^\s!.,]+)\s+(?:(?:will\s+)?(?:ends?|expires?)\s+)?(?:in|within)\s+(?:the\s+next\s+)?(\d+)\s*(hours?|hrs?|h|days?|d|minutes?|mins?|m)\b/i;

  // A login as Twitch allows it, and the first path segments that are never
  // a streamer (the desktop's RESERVED_LOGINS holds the same 13 names).
  const STREAK_LOGIN_RE = /^[a-z0-9_]{1,25}$/;
  const STREAK_RESERVED_PATHS = Object.freeze([
    "directory", "videos", "settings", "subscriptions", "inventory", "drops",
    "wallet", "save-streak", "popout", "embed", "moderator", "team", "search",
  ]);

  // Where candidates are read. The dropdown uses the selectors the open
  // check tests; the page list names pages that show notifications and no
  // chat (every other page, a channel above all, counts as a chat page);
  // the chat selector is a second guard only. Today's dropdown (recorded on
  // 2026-10-01, plan A46) is an unlabeled role=dialog portal under the body
  // holding the center-window balloon and one persistent-notification block
  // per card; the two legacy selectors stay.
  const STREAK_POPOVER_SELECTOR =
    '[data-a-target="onsite-notifications-popover"], [role="dialog"][aria-label*="otification" i], ' +
    '[data-test-selector="center-window__balloon"], ' +
    '[role="dialog"]:has([data-test-selector="persistent-notification"])';
  // One notification card in today's dropdown: the card root when it holds
  // one sentence.
  const STREAK_CARD_SELECTOR = '[data-test-selector="persistent-notification"]';
  const STREAK_PAGE_SCAN_PATHS = Object.freeze([
    "directory", "drops", "inventory", "notifications", "search", "settings", "subscriptions", "wallet",
  ]);
  const STREAK_CHAT_SELECTOR =
    '[data-a-target="chat-scroller"], [data-test-selector="chat-scrollable-area__message-container"], ' +
    '.chat-scrollable-area__message-container, .stream-chat, [role="log"], [class*="chat-line" i], ' +
    '[class*="chat-scrollable" i], [class*="video-chat" i], [data-a-target*="chat" i], [data-test-selector*="chat" i]';
  // The sidebar's "Save your Streak" links. The live check (plan A46)
  // recorded them in a "Watch Streaks at risk" group linking to VODs, never
  // to /save-streak/, so nothing there is reported today; the older
  // containers stay.
  const STREAK_SIDEBAR_SELECTOR =
    '[role="group"][aria-label="Watch Streaks at risk" i], #side-nav, [data-a-target="side-nav-bar"], ' +
    'nav[aria-label*="side" i]';

  // One card is the largest element around its sentence that holds no
  // second sentence, at most this many characters and no chat.
  const STREAK_CARD_ROOT_MAX_CHARS = 1500;
  // A card's record is forgotten this long after it was last seen (48 h).
  const STREAK_DEDUP_TTL_MS = 172800000;
  // In-danger deadlines are whole hours: a deadline earlier than the stored
  // one by more than this (plus the age unit) is a new deadline, anything
  // less is label drift.
  const STREAK_DEADLINE_SLACK_MS = 3600000;
  // Text about a streak that matches no sentence is reported (first 120
  // characters, once per page) so a Twitch rewording shows in the debug log.
  const STREAK_UNPARSED_MAX_CHARS = 120;
  const STREAK_NEAR_MISS_RE = /\b(?:broke|end(?:s|ed)?|expire[sd]?|maintained|sav(?:e|ed|ing))\b/i;

  // Elements a sentence can sit in.
  const CARD_CANDIDATES = "a, p, span, div, article, li";

  // A streak count as matched above, separators removed.
  function parseStreakCount(text) {
    return parseInt(text.replace(/,/g, ""), 10);
  }

  // Parse a single block of text. Returns null if no streak match.
  function parseStreakText(text) {
    if (!text || text.length > 600) return null; // Reject huge blobs
    let m = STREAK_BROKE_RE.exec(text);
    if (m) {
      return {
        status: "broke",
        streamer: m[2].toLowerCase(),
        count: parseStreakCount(m[1]),
        deadline_hours: 24, // the save window, an unverified belief
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

  // How many matches of the given sentence patterns a text holds.
  function countSentences(text, patterns) {
    let n = 0;
    for (const re of patterns) n += (String(text).match(new RegExp(re.source, "gi")) || []).length;
    return n;
  }

  function countStreakSentences(text) {
    return countSentences(text, [STREAK_BROKE_RE, STREAK_IN_DANGER_RE]);
  }

  function countMaintainedSentences(text) {
    return countSentences(text, [ALREADY_SAVED_RE]);
  }

  function _streakKey(ev) {
    return `${ev.status}:${ev.streamer}:${ev.count}`;
  }

  // Fire-and-forget message to the background. A background that is asleep
  // or reloading costs this one message, never an error in the page.
  function notifyBackground(message) {
    try {
      const sent = chrome.runtime.sendMessage(message);
      if (sent && typeof sent.catch === "function") sent.catch(() => {});
      return true;
    } catch (e) {
      return false;
    }
  }

  // The element itself when it matches selector, then its matching
  // descendants, in page order.
  function candidatesIn(root, selector) {
    const out = [];
    if (!root || root.nodeType !== 1) return out;
    if (root.matches(selector)) out.push(root);
    for (const el of root.querySelectorAll(selector)) out.push(el);
    return out;
  }

  function firstPathSegment() {
    const seg = window.location.pathname.split("/").filter(Boolean);
    return seg[0] ? seg[0].toLowerCase() : "";
  }

  // "bell" for an element inside the notifications dropdown, "page" for one
  // on a notifications page and outside the chat guard, else null (the
  // element is not read).
  function streakCandidateSource(el) {
    if (!el || typeof el.closest !== "function") return null;
    if (el.closest(STREAK_POPOVER_SELECTOR)) return "bell";
    if (STREAK_PAGE_SCAN_PATHS.includes(firstPathSegment()) && !el.closest(STREAK_CHAT_SELECTOR)) return "page";
    return null;
  }

  // A save-streak link counts where a card does, and in the sidebar outside
  // the chat guard.
  function streakLinkInScope(a) {
    if (streakCandidateSource(a)) return true;
    return !!a.closest(STREAK_SIDEBAR_SELECTOR) && !a.closest(STREAK_CHAT_SELECTOR);
  }

  // The card that holds el: walks up while the parent still holds exactly
  // one sentence (counted by `count`), at most STREAK_CARD_ROOT_MAX_CHARS
  // characters and no chat, never up to `boundary` (the dropdown) or into
  // the page body. For a notification card (`bounded`) the walk also stops
  // before a parent that holds another notification (crossesCardEdge).
  // Age, link and name are read inside this root only, so a neighbor never
  // lends its age or link. Today's dropdown marks each card
  // (STREAK_CARD_SELECTOR): that block is the root whenever it holds this
  // one sentence within the same limits.
  function cardRootFor(el, boundary, count, bounded = false) {
    const card = el.closest(STREAK_CARD_SELECTOR);
    if (card && card !== boundary && (!boundary || boundary.contains(card))) {
      const text = card.textContent || "";
      if (text.length <= STREAK_CARD_ROOT_MAX_CHARS && count(text) <= 1 &&
        !card.matches(STREAK_CHAT_SELECTOR) && !card.querySelector(STREAK_CHAT_SELECTOR)) {
        return card;
      }
    }
    let root = el;
    while (root !== boundary) {
      const parent = root.parentElement;
      if (!parent || parent === boundary || parent === document.body || parent === document.documentElement) break;
      const text = parent.textContent || "";
      if (text.length > STREAK_CARD_ROOT_MAX_CHARS || count(text) > 1) break;
      if (parent.matches(STREAK_CHAT_SELECTOR) || parent.querySelector(STREAK_CHAT_SELECTOR)) break;
      if (bounded && crossesCardEdge(root, parent)) break;
      root = parent;
    }
    return root;
  }

  // Whether parent holds another notification beside root. Each child of
  // parent other than root counts once, however many labels it shows:
  // - an age in such a child while root shows its own, or in two of them
  //   while root shows none (an undated card stays undated rather than
  //   borrow a neighbor's age);
  // - a /save-streak/ link to a login root does not link, while root links
  //   one, or to two logins while root links none.
  // The card's own label and link are usually siblings of the sentence, so
  // a single one of them never stops the walk.
  function crossesCardEdge(root, parent) {
    const rootLogins = saveStreakLoginsIn(root);
    let aged = 0;
    const others = new Set();
    for (const child of parent.children) {
      if (child === root) continue;
      if (showsAge(child)) aged++;
      for (const login of saveStreakLoginsIn(child)) {
        if (!rootLogins.has(login)) others.add(login);
      }
    }
    if (aged >= (showsAge(root) ? 1 : 2)) return true;
    return others.size >= (rootLogins.size ? 1 : 2);
  }

  // el and its ancestors up to root, nearest first; just root when el is
  // not inside it.
  function levelsUp(el, root) {
    if (!root) return [];
    const inside = el && (el === root || (typeof root.contains === "function" && root.contains(el)));
    const out = [];
    for (let node = inside ? el : root; node; node = node.parentElement) {
      out.push(node);
      if (node === root) break;
    }
    return out;
  }

  // "just now", "20 hours ago", "an hour ago", "yesterday", "2 months ago",
  // "1 yr ago": {seconds, unit}, the unit being the label's own (a floored
  // "N units ago" places the card within one unit); null when nothing reads
  // as an age.
  const TIME_AGO_UNITS = [
    [/^(?:s|secs?|seconds?)$/, 1],
    [/^(?:m|mins?|minutes?)$/, 60],
    [/^(?:h|hrs?|hours?)$/, 3600],
    [/^(?:d|days?)$/, 86400],
    [/^(?:w|wks?|weeks?)$/, 604800],
    [/^(?:mos?|months?)$/, 2592000],
    [/^(?:y|yrs?|years?)$/, 31536000],
  ];
  function parseTimeAgo(text) {
    if (!text) return null;
    if (/\bjust\s*now\b/i.test(text)) return { seconds: 0, unit: 60 };
    if (/\byesterday\b/i.test(text)) return { seconds: 86400, unit: 86400 };
    const m = /(?:(\d+)\s*|\b(an?)\s+)(seconds?|secs?|minutes?|mins?|months?|mos?|hours?|hrs?|days?|weeks?|wks?|years?|yrs?|s|m|h|d|w|y)\s*ago\b/i.exec(text);
    if (!m) return null;
    const n = m[1] !== undefined ? parseInt(m[1], 10) : 1;
    const word = m[3].toLowerCase();
    for (const [re, unit] of TIME_AGO_UNITS) {
      if (re.test(word)) return { seconds: n * unit, unit };
    }
    return null;
  }

  // The age in seconds of an "X ago" text, or null. Kept for callers of the
  // pre-1.12 name.
  function parseTimeAgoSeconds(text) {
    const age = parseTimeAgo(text);
    return age ? age.seconds : null;
  }

  // An absolute date in a title or aria-label ("Sep 29, 2026, 10:30 AM"):
  // {at, unit}, the unit being the finest field it shows; null when the text
  // holds no year or does not parse.
  function parseAbsoluteDate(value) {
    if (!value || !/\b(?:19|20)\d{2}\b/.test(value)) return null;
    let at = Date.parse(value);
    if (isNaN(at)) at = Date.parse(value.replace(/\s+at\s+/i, " "));
    if (isNaN(at)) return null;
    const unit = /\d{1,2}:\d{2}:\d{2}/.test(value) ? 1 : /\d{1,2}:\d{2}/.test(value) ? 60 : 86400;
    return { at, unit };
  }

  // The card sentences as the age reader finds them. In textContent a unit
  // can run straight into the next label ("ends in 5 hours2 hours ago"), so
  // the in-danger pattern drops its closing word boundary here.
  const STREAK_SENTENCE_TEXT_RES = [STREAK_BROKE_RE, STREAK_IN_DANGER_RE].map(
    (re) => new RegExp(re.source.replace(/\\b$/, ""), "gi"));

  // Every card sentence in text, as matched.
  function streakSentencesIn(text) {
    const out = [];
    for (const re of STREAK_SENTENCE_TEXT_RES) out.push(...(String(text || "").match(re) || []));
    return out;
  }

  // text with every card sentence replaced by a separator, so a streamer's
  // name ("yesterdayjam", "justnowgaming", "5hago") never reads as an age.
  function stripStreakSentences(text) {
    let out = String(text || "");
    for (const re of STREAK_SENTENCE_TEXT_RES) out = out.replace(re, " | ");
    return out;
  }

  // The age shown in node's subtree when read at nowMs: {seconds, unit} or
  // null. A machine-readable time wins (time[datetime], exact to the
  // second), then a title or aria-label holding an absolute date, then an
  // "X ago" label.
  function ageInSubtree(node, nowMs) {
    if (!node) return null;
    for (const timeEl of candidatesIn(node, "time[datetime]")) {
      const stamp = Date.parse(timeEl.getAttribute("datetime") || "");
      if (!isNaN(stamp)) return { seconds: Math.max(0, (nowMs - stamp) / 1000), unit: 1 };
    }
    for (const el of candidatesIn(node, "[title], [aria-label]")) {
      const abs = parseAbsoluteDate(el.getAttribute("title")) || parseAbsoluteDate(el.getAttribute("aria-label"));
      if (abs) return { seconds: Math.max(0, (nowMs - abs.at) / 1000), unit: abs.unit };
    }
    // Leaves first: Twitch's text runs together in textContent ("now2 hours").
    // A card sentence is never an age, nor is any leaf inside one (a name in
    // its own element): a streamer called "yesterdayjam" or "justnowgaming"
    // leaves the card its own label's age.
    const whole = node.textContent || "";
    const sentences = streakSentencesIn(whole);
    for (const el of candidatesIn(node, "*")) {
      if (el.children.length) continue;
      const text = (el.textContent || "").trim();
      if (!text || sentences.some((s) => s.includes(text))) continue;
      const age = parseTimeAgo(stripStreakSentences(text));
      if (age) return age;
    }
    return parseTimeAgo(stripStreakSentences(whole));
  }

  function showsAge(node) {
    return !!ageInSubtree(node, 0);
  }

  // The age of the card at root when read at nowMs: {seconds, unit} or null.
  // Read nearest the sentence first: from el up to root, the first element
  // whose subtree shows an age, so the card's own label wins over anything
  // else the root holds.
  function cardAgeFor(el, root, nowMs) {
    for (const node of levelsUp(el, root)) {
      const age = ageInSubtree(node, nowMs);
      if (age) return age;
    }
    return null;
  }

  // The unit sent with an age: 1, 60, 3600 or 86400. A week label survives
  // the age gate only on an in-danger card with a longer deadline; it goes
  // out as days, the unit the desktop would infer from the seconds.
  function reportedAgeUnit(unit) {
    return unit > 86400 ? 86400 : unit;
  }

  function isStreakLogin(login) {
    return STREAK_LOGIN_RE.test(login) && !STREAK_RESERVED_PATHS.includes(login);
  }

  // The path segments of a twitch.tv link (relative or absolute), else null.
  function twitchPathSegments(href) {
    const h = String(href || "").trim();
    let path;
    if (/^\/(?!\/)/.test(h)) {
      path = h;
    } else {
      const m = /^https?:\/\/(?:www\.)?twitch\.tv(\/[^?#]*)?/i.exec(h);
      if (!m) return null;
      path = m[1] || "/";
    }
    return path.split(/[?#]/)[0].split("/").filter(Boolean);
  }

  // The streamer of a card: {streamer, login_verified}. A /save-streak/<login>
  // link in the card gives the login. Any other link counts only when its
  // first path segment is the sentence's name, which names the same login,
  // so a /popout/ or /team/ link never replaces the name. A reserved or
  // non-login name is still reported, unverified, so the desktop can log it.
  function streakLoginFor(name, hrefs) {
    for (const href of hrefs || []) {
      const seg = twitchPathSegments(href);
      if (seg && seg[1] && seg[0].toLowerCase() === "save-streak") {
        const login = seg[1].toLowerCase();
        if (isStreakLogin(login)) return { streamer: login, login_verified: true };
      }
    }
    const parsed = String(name || "").toLowerCase();
    return { streamer: parsed, login_verified: isStreakLogin(parsed) };
  }

  // The logins that node's /save-streak/<login> links name.
  function saveStreakLoginsIn(node) {
    const out = new Set();
    for (const a of candidatesIn(node, "a[href]")) {
      const seg = twitchPathSegments(a.getAttribute("href"));
      if (!seg || !seg[1] || seg[0].toLowerCase() !== "save-streak") continue;
      const login = seg[1].toLowerCase();
      if (isStreakLogin(login)) out.add(login);
    }
    return out;
  }

  // The links of the card at root, nearest the sentence first (as the age),
  // so the card's own save-streak link wins.
  function cardHrefs(el, root) {
    const seen = new Set();
    const hrefs = [];
    for (const node of levelsUp(el, root)) {
      for (const a of candidatesIn(node, "a[href]")) {
        if (seen.has(a)) continue;
        seen.add(a);
        hrefs.push(a.getAttribute("href"));
      }
    }
    return hrefs;
  }

  // The event for one card (the runtime message contract), read at nowMs;
  // el is the element holding the sentence. card_age_s is always a whole
  // number of seconds.
  function streakCardEvent(parsed, root, source, nowMs, el = root) {
    const age = cardAgeFor(el, root, nowMs);
    const who = streakLoginFor(parsed.streamer, cardHrefs(el, root));
    return {
      status: parsed.status,
      streamer: who.streamer,
      count: parsed.count,
      deadline_hours: parsed.deadline_hours,
      card_age_s: age ? Math.max(0, Math.floor(age.seconds)) : null,
      card_age_unit_s: age ? reportedAgeUnit(age.unit) : null,
      login_verified: who.login_verified,
      source,
      // Always built from the login, never copied from the card.
      save_url: `https://www.twitch.tv/save-streak/${who.streamer}`,
      detected_at: new Date(nowMs).toISOString(),
      page_url: window.location.href,
    };
  }

  // The age gate: a broke card 24 h old or more ("1 day ago" included) and
  // an in-danger card at least its own deadline old are past saving. Twitch
  // keeps old cards in the inbox; without this they re-alert on every read.
  // A card of unknown age passes.
  function passesAgeGate(ev) {
    if (ev.card_age_s === null || ev.card_age_s === undefined) return true;
    if (ev.status === "broke") return ev.card_age_s < 86400;
    return ev.card_age_s < ev.deadline_hours * 3600;
  }

  // Card identity (shared with the desktop's card_relation). A record is
  // {seen_at, break_at, unit, deadline_at}; break_at is the earliest time
  // the card can have been posted and unit its age unit (0 when unknown),
  // both in one time unit. Two readings whose possible posting intervals
  // overlap can be one card: "same". An unknown age on either side is
  // "same" too.
  function streakCardRelation(record, incoming) {
    const rUnit = (record && record.unit) || 0;
    const iUnit = (incoming && incoming.unit) || 0;
    if (!rUnit || !iUnit) return "same";
    if (incoming.break_at >= record.break_at + rUnit) return "newer";
    if (incoming.break_at + iUnit <= record.break_at) return "older";
    return "same";
  }

  // A same or older card whose deadline is earlier than the stored one by
  // more than the slack plus the larger unit (records in milliseconds).
  function streakIsEscalation(record, incoming) {
    if (streakCardRelation(record, incoming) === "newer") return false;
    const before = record.deadline_at;
    const after = incoming.deadline_at;
    if (typeof before !== "number" || typeof after !== "number" || !isFinite(before) || !isFinite(after)) return false;
    return before - after > STREAK_DEADLINE_SLACK_MS + Math.max(record.unit || 0, incoming.unit || 0);
  }

  // The record of an event read at nowMs, in milliseconds.
  function streakCardRecord(ev, nowMs) {
    const known = ev.card_age_s !== null && ev.card_age_s !== undefined;
    const unit = known ? (ev.card_age_unit_s || 0) * 1000 : 0;
    const breakAt = known ? nowMs - ev.card_age_s * 1000 - unit : nowMs;
    const hours = ev.status === "in_danger" ? ev.deadline_hours : 24;
    return { seen_at: nowMs, break_at: breakAt, unit, deadline_at: breakAt + hours * 3600000 };
  }

  // Per page: the card records by status:login:count, the last sighting of
  // each save-streak link, and the near misses already reported.
  const _streakRecords = new Map();
  const _linkSightings = new Map();
  const _reportedUnparsed = new Set();
  let _streakEventsSent = 0;

  function pruneStreakRecords(nowMs) {
    for (const [key, rec] of _streakRecords) {
      if (nowMs - rec.seen_at > STREAK_DEDUP_TTL_MS) _streakRecords.delete(key);
    }
    for (const [login, at] of _linkSightings) {
      if (nowMs - at > STREAK_DEDUP_TTL_MS) _linkSightings.delete(login);
    }
  }

  // The streamer of a record key (status:login:count).
  function streakKeyStreamer(key) {
    return key.slice(key.indexOf(":") + 1, key.lastIndexOf(":"));
  }

  // Whether a card goes out: a new key, a newer card under a known key, or
  // an escalated deadline. A same or older card only refreshes the last
  // sighting (a newer one replaces the record). A card older than one the
  // page already holds for the same streamer under another key (an
  // in-danger card behind the broke card, a lower count behind a higher
  // one) is out of date and never goes out, so a scan that sees only part
  // of a list (cards removed one by one) keeps one event per streamer.
  function shouldSendCard(ev, nowMs) {
    const key = _streakKey(ev);
    const incoming = streakCardRecord(ev, nowMs);
    for (const [k, rec] of _streakRecords) {
      if (k !== key && streakKeyStreamer(k) === ev.streamer && streakCardRelation(rec, incoming) === "older") {
        return false;
      }
    }
    const stored = _streakRecords.get(key);
    if (!stored || streakCardRelation(stored, incoming) === "newer") {
      _streakRecords.set(key, incoming);
      return true;
    }
    stored.seen_at = nowMs;
    if (!streakIsEscalation(stored, incoming)) return false;
    stored.deadline_at = incoming.deadline_at;
    return true;
  }

  function reportStreakEvent(ev, nowMs) {
    if (!shouldSendCard(ev, nowMs)) return false;
    notifyBackground({ type: "streak_event", event: ev });
    _streakEventsSent++;
    console.log(LOG_PREFIX, "Streak event reported:", _streakKey(ev), `(${ev.source})`);
    return true;
  }

  // One link event per login per page. A link carries no count.
  function reportLinkEvent(login, nowMs) {
    const seen = _linkSightings.has(login);
    _linkSightings.set(login, nowMs);
    if (seen) return false;
    notifyBackground({
      type: "streak_event",
      event: {
        status: "broke",
        streamer: login,
        count: null,
        source: "link",
        login_verified: true,
        card_age_s: null,
        card_age_unit_s: null,
        detected_at: new Date(nowMs).toISOString(),
        page_url: window.location.href,
        save_url: `https://www.twitch.tv/save-streak/${login}`,
      },
    });
    _streakEventsSent++;
    console.log(LOG_PREFIX, "Save-streak link reported:", login);
    return true;
  }

  function reportNearMiss(text) {
    const short = text.slice(0, STREAK_UNPARSED_MAX_CHARS);
    if (_reportedUnparsed.has(short)) return;
    _reportedUnparsed.add(short);
    console.log(LOG_PREFIX, "Unparsed streak text:", short);
    notifyBackground({ type: "streak_unparsed", text: short, page_url: window.location.href });
  }

  // The elements under root (root included) with at most 4 children whose
  // text mentions "streak": [{el, text}] in page order. One scan reads each
  // element's text once for its card and near-miss passes.
  function streakTextCandidates(root) {
    const out = [];
    for (const el of candidatesIn(root, CARD_CANDIDATES)) {
      if (el.children.length > 4) continue;
      const text = (el.textContent || "").trim();
      if (text.length < 12 || !/streak/i.test(text)) continue;
      out.push({ el, text });
    }
    return out;
  }

  // Every card among the candidates that scope accepts: [{parsed, root,
  // source, el}], one per card root, in page order. An element holding two
  // sentences is a container, not a card; its cards are read one by one.
  // Only the innermost element around a sentence starts a walk: an outer
  // one (a list holding a single card) would take in its neighbors.
  function findStreakCards(candidates, scope) {
    const matched = [];
    for (const { el, text } of candidates) {
      if (countStreakSentences(text) !== 1) continue;
      const parsed = parseStreakText(text);
      if (parsed) matched.push({ el, parsed });
    }
    const cards = [];
    const roots = new Set();
    for (const m of matched) {
      if (matched.some((o) => o !== m && m.el.contains(o.el))) continue;
      const el = m.el;
      const source = scope(el);
      if (!source || el.querySelector(STREAK_CHAT_SELECTOR)) continue;
      const cardRoot = cardRootFor(el, el.closest(STREAK_POPOVER_SELECTOR), countStreakSentences, true);
      if (roots.has(cardRoot)) continue;
      roots.add(cardRoot);
      cards.push({ parsed: m.parsed, root: cardRoot, source, el });
    }
    return cards;
  }

  function insideAny(roots, el) {
    return roots.some((r) => r === el || r.contains(el));
  }

  // P7: every /save-streak/<login> link under root that linkScope accepts
  // (by default the dropdown, a notifications page outside chat, or the
  // sidebar outside chat), with the card it sits in, if any, and that
  // card's age: [{login, el, inCard, card_age_s}]. `cards` are the cards
  // the caller already found under root; without them the page is read.
  // The periodic scan reports the links outside cards; scanSaveStreak
  // lists them all.
  function collectSaveStreakSlugs(root = document.body, linkScope = streakLinkInScope, cards = null, nowMs = Date.now()) {
    const known = cards || findStreakCards(streakTextCandidates(root), streakCandidateSource);
    const out = [];
    for (const a of candidatesIn(root, 'a[href*="/save-streak/"]')) {
      const seg = twitchPathSegments(a.getAttribute("href"));
      if (!seg || !seg[1] || seg[0].toLowerCase() !== "save-streak") continue;
      const login = seg[1].toLowerCase();
      if (!isStreakLogin(login) || !linkScope(a)) continue;
      const card = known.find((c) => c.root === a || c.root.contains(a));
      const age = card ? cardAgeFor(card.el || card.root, card.root, nowMs) : null;
      out.push({ login, el: a, inCard: !!card, card_age_s: age ? Math.max(0, Math.floor(age.seconds)) : null });
    }
    return out;
  }

  // The scanSaveStreak reply: {slugs, links: [{login, card_age_s}]}, each
  // link once per login and age.
  function saveStreakScanReply() {
    const links = [];
    const seen = new Set();
    for (const { login, card_age_s } of collectSaveStreakSlugs()) {
      const key = `${login}:${card_age_s}`;
      if (seen.has(key)) continue;
      seen.add(key);
      links.push({ login, card_age_s });
    }
    return { slugs: Array.from(new Set(links.map((l) => l.login))), links };
  }

  // Text that talks about a streak and a break, end, expiry or save but
  // matches no sentence (a count-less rewording included). Only the
  // innermost such element counts, in scope and outside every card.
  function findNearMisses(candidates, scope, cardRoots) {
    const hits = [];
    for (const candidate of candidates) {
      const el = candidate.el;
      const text = candidate.text.replace(/\s+/g, " ");
      if (text.length < 20 || text.length > 600) continue;
      if (!STREAK_NEAR_MISS_RE.test(text) || parseStreakText(text) || parseAlreadySavedText(text)) continue;
      if (!scope(el) || insideAny(cardRoots, el)) continue;
      hits.push({ el, text });
    }
    return hits.filter((h) => !hits.some((o) => o !== h && h.el.contains(o.el)));
  }

  // One scan of root (the page by default): cards (one event per streamer,
  // the newest by known age, else the first in the page), save-streak links
  // outside cards, and near misses, each sent only as the dedup allows.
  // Links and near misses need every card around them in view, so a scan
  // of a smaller subtree (an added or removed node) reads them only when
  // that subtree is the dropdown itself; the full scan 2 s later reads the
  // rest. opts.scope and opts.linkScope replace the A39 scope (a removed
  // subtree). Returns {cards, events}.
  function scanAndReport(opts = {}) {
    const result = { cards: 0, events: 0 };
    const root = opts.root || document.body;
    if (!root) return result;
    const scope = opts.scope || streakCandidateSource;
    const linkScope = opts.linkScope || streakLinkInScope;
    try {
      const nowMs = Date.now();
      pruneStreakRecords(nowMs);
      const candidates = streakTextCandidates(root);
      const cards = findStreakCards(candidates, scope);
      result.cards = cards.length;
      const newest = new Map();
      for (const card of cards) {
        const ev = streakCardEvent(card.parsed, card.root, card.source, nowMs, card.el);
        if (!passesAgeGate(ev)) continue;
        const prev = newest.get(ev.streamer);
        if (!prev || (ev.card_age_s !== null && (prev.card_age_s === null || ev.card_age_s < prev.card_age_s))) {
          newest.set(ev.streamer, ev);
        }
      }
      for (const ev of newest.values()) {
        if (reportStreakEvent(ev, nowMs)) result.events++;
      }
      if (root !== document.body && !root.matches(STREAK_POPOVER_SELECTOR)) return result;
      for (const link of collectSaveStreakSlugs(root, linkScope, cards, nowMs)) {
        if (link.inCard) continue; // the card's own event covers it
        if (reportLinkEvent(link.login, nowMs)) result.events++;
      }
      const cardRoots = cards.map((c) => c.root);
      for (const miss of findNearMisses(candidates, scope, cardRoots)) reportNearMiss(miss.text);
    } catch (e) {
      console.warn(LOG_PREFIX, "Streak scan failed:", e && e.message);
    }
    return result;
  }

  function mentionsStreak(node) {
    const text = node && node.textContent;
    return !!text && text.length >= 12 && /streak/i.test(text);
  }

  // What an added subtree is read with: the dropdown around it, the whole
  // page when it sits on a listed page outside chat, else the dropdown
  // inside it or the subtree alone (where only a dropdown counts).
  function streakScanRootFor(el) {
    const around = el.closest(STREAK_POPOVER_SELECTOR);
    if (around) return around;
    if (streakCandidateSource(el) === "page") return document.body;
    return el.querySelector(STREAK_POPOVER_SELECTOR) || el;
  }

  // A removed subtree is read only when it left the open dropdown, or when
  // it is or holds the dropdown itself (closed), and then only what sat
  // inside the dropdown, as "bell". A detached node has no ancestors, so
  // this rests on the mutation's target and the node itself; a chat line
  // trimmed from the buffer is never read.
  function scanRemovedSubtree(target, node) {
    const fromOpenDropdown = !!(target && typeof target.closest === "function" &&
      target.closest(STREAK_POPOVER_SELECTOR));
    const holdsDropdown = node.matches(STREAK_POPOVER_SELECTOR) || !!node.querySelector(STREAK_POPOVER_SELECTOR);
    if (!fromOpenDropdown && !holdsDropdown) return;
    const inDropdown = (el) => (fromOpenDropdown || el.closest(STREAK_POPOVER_SELECTOR) ? "bell" : null);
    scanAndReport({ root: node, scope: inDropdown, linkScope: inDropdown });
  }

  let _streakScanTimer = null;
  function startStreakMonitor() {
    // Periodic re-scan: catches notifications arriving over the socket and
    // a move to a notifications page.
    if (_streakScanTimer) return;
    setTimeout(() => scanAndReport(), 5000);
    _streakScanTimer = setInterval(() => scanAndReport(), 60000);

    if (typeof MutationObserver === "function" && document.body) {
      let pending = false;
      const obs = new MutationObserver((mutations) => {
        // Twitch chat churns the DOM constantly, so only subtrees whose text
        // mentions "streak" are touched. Each is read at once (a dropdown
        // closed within a second is still read), and a full scan follows
        // 2 s after the burst. An added subtree is read with everything
        // around it that counts: the whole dropdown, or the whole page on a
        // listed page, once per batch, so cards that Twitch inserts one by
        // one still give one event per streamer.
        let relevant = false;
        const roots = new Set();
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            if (!mentionsStreak(node)) continue;
            relevant = true;
            const el = node.nodeType === 1 ? node : node.parentElement;
            if (el && el.isConnected) roots.add(streakScanRootFor(el));
          }
          for (const node of m.removedNodes) {
            if (node.nodeType === 1 && mentionsStreak(node)) scanRemovedSubtree(m.target, node);
          }
        }
        for (const root of roots) {
          if (root.isConnected) scanAndReport({ root });
        }
        if (!relevant || pending) return;
        pending = true;
        setTimeout(() => {
          pending = false;
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
  // on every move.
  //
  // 1.12: the sentence counts only in Twitch's own card (the heading in the
  // same card, outside chat), and a page that shows the heading alone, with
  // no video playing, is reported as not_eligible: nothing to watch.
  //
  // Today (recorded on 2026-10-01, plan A46) Twitch moves a save-streak page
  // in place to the clip, VOD or channel it plays, and for a streak already
  // kept to the channel page with an aria-modal dialog on top holding the
  // heading and the maintained sentence. That dialog is read on any page,
  // right after each move: chat never sits inside such a dialog. Its name
  // must fit the channel the page shows, if it shows one.
  // -----------------------------------------------------------------------
  const ALREADY_SAVED_RE =
    /already\s+maintained\s+your\s+(\d{1,3}(?:,\d{3})+|\d+)[- ](?:stream|live)\s+streak\s+with\s+([^\s!.,]+)/i;
  const ALREADY_SAVED_MODAL_SELECTOR = '[role="dialog"][aria-modal="true"], .tw-modal';
  const NOT_ELIGIBLE_HEADING_RE = /no\s+content\s+eligible/i;
  // Two scans at least this far apart must both see the heading alone.
  const NOT_ELIGIBLE_MIN_GAP_MS = 5000;
  // Twitch renders the card after the channel page itself: scan at these
  // delays after arriving, then on relevant mutations.
  const SAVE_STREAK_SCAN_DELAYS_MS = [3000, 8000, 15000];
  // Backstop for a card that renders late or changes its text in place,
  // which the observer (added nodes only) cannot see. Bounded so a page
  // left open for hours stops rescanning; the observer stays.
  const SAVE_STREAK_RESCAN_MS = 30000;
  const SAVE_STREAK_RESCAN_WINDOW_MS = 10 * 60 * 1000;
  // In-app navigation fires no event a content script can hear, so the
  // top frame compares its path on this cadence.
  const ROUTE_POLL_MS = 1000;

  let _saveStreakRoute = null; // routeKey() the current check belongs to
  let _saveStreakReported = false; // this visit already reported
  let _notEligibleReported = false; // this visit already reported nothing to watch
  let _notEligibleSeenAt = 0; // first scan of this run that saw the heading alone
  let _saveStreakTimers = []; // one-shot scans for this visit
  let _saveStreakRescanTimer = null;
  let _saveStreakMutationScan = null; // the pending 500 ms rescan, if any
  let _saveStreakObserver = null;
  // "login:count" of every kept streak this page reported, so a dialog that
  // stays up through a move (or is drawn again) reports once.
  const _keptStreaksReported = new Set();

  // {count, streamer} for a block of text with the "already maintained"
  // wording, else null. The streamer here is Twitch's display name; the
  // caller prefers the login from the page path.
  function parseAlreadySavedText(text) {
    if (!text || text.length > 600) return null;
    const m = ALREADY_SAVED_RE.exec(text);
    if (!m) return null;
    return { count: parseStreakCount(m[1]), streamer: m[2].toLowerCase() };
  }

  // The "No Content Eligible" heading without the maintained sentence.
  function isNotEligibleText(text) {
    const t = String(text || "");
    return NOT_ELIGIBLE_HEADING_RE.test(t) && !/maintained/i.test(t);
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

  // Whether root shows the "No Content Eligible" heading outside chat.
  function rootShowsEligibilityHeading(root) {
    for (const el of candidatesIn(root, "*")) {
      if (el.children.length > 4 || !NOT_ELIGIBLE_HEADING_RE.test(el.textContent || "")) continue;
      if (el.closest(STREAK_CHAT_SELECTOR) || el.querySelector(STREAK_CHAT_SELECTOR)) continue;
      return true;
    }
    return false;
  }

  // A maintained sentence counts only outside the chat guard and in a card
  // that also shows the heading, so a chatter typing it records no save.
  function isSaveStreakCardSentence(el) {
    if (el.closest(STREAK_CHAT_SELECTOR)) return false;
    return rootShowsEligibilityHeading(cardRootFor(el, null, countMaintainedSentences));
  }

  function anyVideoShowing() {
    return Array.from(document.querySelectorAll("video")).some((v) => v.videoHeight > 0);
  }

  // The heading shows outside chat, its card holds no maintained sentence,
  // and no video plays.
  function notEligibleShowing() {
    if (!document.body || anyVideoShowing()) return false;
    for (const el of document.body.querySelectorAll("h1, h2, h3, h4, p, span, div")) {
      if (el.children.length > 4) continue;
      const text = el.textContent || "";
      if (text.length > STREAK_CARD_ROOT_MAX_CHARS || !NOT_ELIGIBLE_HEADING_RE.test(text)) continue;
      if (el.closest(STREAK_CHAT_SELECTOR) || el.querySelector(STREAK_CHAT_SELECTOR)) continue;
      if (isNotEligibleText(cardRootFor(el, null, countMaintainedSentences).textContent)) return true;
    }
    return false;
  }

  // Sends not_eligible once per visit, on the second of two scans at least
  // NOT_ELIGIBLE_MIN_GAP_MS apart that both see nothing to watch.
  function checkNotEligible(login) {
    if (!notEligibleShowing()) {
      _notEligibleSeenAt = 0;
      return false;
    }
    const now = Date.now();
    if (!_notEligibleSeenAt) {
      _notEligibleSeenAt = now;
      return false;
    }
    if (now - _notEligibleSeenAt < NOT_ELIGIBLE_MIN_GAP_MS) return false;
    _notEligibleReported = true;
    stopSaveStreakCheck();
    notifyBackground({ type: "not_eligible", streamer: login, page_url: window.location.href });
    console.log(LOG_PREFIX, `Nothing to watch on the save-streak page for ${login}; reported`);
    return true;
  }

  // The channel this page shows: the login of /save-streak/<login>,
  // /embed/<login> or /popout/<login>, else a first path segment that is a
  // login and no reserved path (a channel or one of its clips); null on any
  // other page (/videos/<id>, /directory).
  function pageChannelLogin() {
    const seg = window.location.pathname.split("/").filter(Boolean).map((s) => s.toLowerCase());
    const login = seg[0] === "save-streak" || seg[0] === "embed" || seg[0] === "popout" ? seg[1] : seg[0];
    return login && isStreakLogin(login) ? login : null;
  }

  // {count, streamer} of an already-kept dialog: the heading and exactly one
  // maintained sentence inside it, at most STREAK_CARD_ROOT_MAX_CHARS
  // characters and nothing like chat; else null. The streamer is Twitch's
  // display name.
  function readKeptStreakDialog(dialog) {
    const text = dialog.textContent || "";
    if (text.length > STREAK_CARD_ROOT_MAX_CHARS || !NOT_ELIGIBLE_HEADING_RE.test(text)) return null;
    if (countMaintainedSentences(text) !== 1) return null;
    if (dialog.matches(STREAK_CHAT_SELECTOR) || dialog.querySelector(STREAK_CHAT_SELECTOR)) return null;
    const m = ALREADY_SAVED_RE.exec(text);
    return m ? { count: parseStreakCount(m[1]), streamer: m[2].toLowerCase() } : null;
  }

  // The already-kept dialog on this page as {streamer, count}, or null. On
  // a page that shows a channel the dialog's name must fit that channel,
  // which gives the login; elsewhere the name must be a login itself.
  function findKeptStreakDialog() {
    const pageLogin = pageChannelLogin();
    for (const dialog of document.querySelectorAll(ALREADY_SAVED_MODAL_SELECTOR)) {
      const parsed = readKeptStreakDialog(dialog);
      if (!parsed) continue;
      if (pageLogin ? !cardNameFitsLogin(parsed.streamer, pageLogin) : !isStreakLogin(parsed.streamer)) continue;
      return { streamer: pageLogin || parsed.streamer, count: parsed.count };
    }
    return null;
  }

  // Sends already_saved and ends this visit's scans.
  function reportAlreadySaved(streamer, count) {
    _saveStreakReported = true;
    _keptStreaksReported.add(`${streamer}:${count}`);
    stopSaveStreakCheck();
    const sent = notifyBackground({
      type: "streak_event",
      event: {
        status: "already_saved",
        streamer,
        count,
        detected_at: new Date().toISOString(),
        page_url: window.location.href,
      },
    });
    if (sent) console.log(LOG_PREFIX, `Streak already saved for ${streamer} (${count}-stream); reported`);
    else console.warn(LOG_PREFIX, `Failed to report the saved streak for ${streamer}`);
  }

  // The already-kept dialog, on any page, once per login and count per page.
  function scanForKeptStreakDialog() {
    if (_saveStreakReported || _notEligibleReported || !document.body) return false;
    // A scan queued for an earlier path must not report under this one;
    // the route watcher re-arms for the new path.
    if (routeKey() !== _saveStreakRoute) return false;
    const kept = findKeptStreakDialog();
    if (!kept || _keptStreaksReported.has(`${kept.streamer}:${kept.count}`)) return false;
    reportAlreadySaved(kept.streamer, kept.count);
    return true;
  }

  // The dialog on any page, then, on a save-streak page, Twitch's card on
  // the page itself and the nothing-to-watch check.
  function scanForAlreadySaved() {
    if (scanForKeptStreakDialog()) return true;
    if (_saveStreakReported || _notEligibleReported || !document.body || !isSaveStreakPage()) return false;
    if (routeKey() !== _saveStreakRoute) return false;
    const login = currentStreamerSlug();
    const candidates = document.body.querySelectorAll("p, span, div, h1, h2, h3, h4, article");
    for (const el of candidates) {
      if (el.children.length > 4) continue;
      const text = (el.textContent || "").trim();
      if (text.length < 20 || !/maintained/i.test(text)) continue;
      const parsed = parseAlreadySavedText(text);
      if (!parsed || !cardNameFitsLogin(parsed.streamer, login)) continue;
      if (!isSaveStreakCardSentence(el)) continue;
      reportAlreadySaved(login || parsed.streamer, parsed.count);
      return true;
    }
    return checkNotEligible(login);
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

  // Starts the check for the page the top frame is on now. Every page looks
  // for the already-kept dialog at once (it opens with the move that brought
  // the page here), then at the delays, through the rescan window, and on
  // mutations until it reports or the path changes. The observer outlives
  // the window on every page: a move to /save-streak/<login> and back that
  // fits inside one route poll re-arms nothing, so only the observer sees
  // the dialog it brings.
  function armSaveStreakCheck() {
    stopSaveStreakCheck();
    _saveStreakRoute = routeKey();
    _saveStreakReported = false;
    _notEligibleReported = false;
    _notEligibleSeenAt = 0;
    if (scanForKeptStreakDialog()) return;
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
  // Bell checks. Twitch renders notification cards only while the bell
  // dropdown is open, so the script opens it to read them:
  // - the open check (1.12): once per page, when the background asks
  //   (checkBell, shortly after a Stream Monitor tab finishes loading) or
  //   from the keepalive backstop. It runs on a tab you can see too, and
  //   reads cards you already saw elsewhere, but waits while you type or
  //   click in the page, never clicks a dropdown you opened, closes only
  //   what it opened and hands focus back unless you moved it meanwhile;
  // - the hidden-tab checks: while the tab is hidden and the badge shows
  //   unread, 10 s after load, every minute, 3 s after the tab is hidden and
  //   when the count rises. Today's bell (recorded on 2026-10-01, plan A46)
  //   has no badge at all, so a bell without any badge markup gets a slow
  //   check instead: at most once per BELL_HIDDEN_NO_BADGE_INTERVAL_MS,
  //   counted from the page load or this script's last click on the bell.
  // Both wait for the list to render instead of a fixed delay, share the
  // one-minute throttle (stamped after a click) and never run at once.
  // -----------------------------------------------------------------------

  const BELL_AUTO_CLICK_MIN_INTERVAL_MS = 60 * 1000;
  // The bell: the two legacy data-a-targets and today's exact aria-label,
  // anywhere in the page; then a button whose label mentions notifications,
  // but only inside the top nav (its data-a-target first, then any nav), so
  // a channel's own notification toggle (the bell beside Follow) is never
  // taken for it, even while the top bar is still rendering. The first one
  // found wins. Every bell lookup goes through this list.
  const BELL_BUTTON_SELECTORS = Object.freeze([
    '[data-a-target="onsite-notifications-toggle__button"]',
    '[data-a-target="onsite-notifications-toggle"]',
    'button[aria-label="Open Notifications" i]',
    '[data-a-target="top-nav-container"] button[aria-label*="otification" i]',
    'nav button[aria-label*="otification" i]',
  ]);
  // Today's dropdown: its balloon renders with the header at once and the
  // cards after a fetch.
  const BELL_BALLOON_SELECTOR = '[data-test-selector="center-window__balloon"]';
  // The hidden-tab check on a bell with no badge markup at all: 10 minutes.
  const BELL_HIDDEN_NO_BADGE_INTERVAL_MS = 600000;
  // Open check: the bell may render late; look again this often, this many
  // times, before answering no-bell.
  const BELL_OPEN_CHECK_RETRY_MS = 5000;
  const BELL_OPEN_CHECK_RETRIES = 4;
  // While you use the page (typing, or a key or click in the last 5 s), look
  // again every 10 s, for at most a minute.
  const BELL_USER_BUSY_POLL_MS = 10000;
  const BELL_USER_BUSY_MAX_MS = 60000;
  const BELL_USER_ACTIVE_WINDOW_MS = 5000;
  // Render wait: poll the dropdown's text; go on once it is non-empty and
  // unchanged for 750 ms, after at least 1 s and at most 8 s.
  const BELL_RENDER_POLL_MS = 250;
  const BELL_RENDER_STABLE_MS = 750;
  const BELL_RENDER_MIN_MS = 1000;
  const BELL_RENDER_MAX_MS = 8000;
  // A dropdown that rendered nothing gets one more try this much later,
  // whatever the unread count says then.
  const BELL_RETRY_AFTER_EMPTY_MS = 60000;
  // The keepalive backstop runs the open check only on a page this old.
  const BELL_BACKSTOP_MIN_PAGE_AGE_MS = 30000;

  const _pageStartedAt = Date.now();
  let _lastBellAutoClickAt = 0;
  let _lastNoBadgeCheckAt = 0; // the last hidden-tab check on a bell without badge markup
  let _lastBellBadgeCount = null;
  let _bellBusy = false; // a check holds the bell
  let _openCheckDone = false; // this page's open check has finished
  let _openCheckRun = null; // the open check in progress
  let _openCheckEmptyTries = 0;
  let _openCheckNoBellTries = 0;
  let _bellMissingSent = false;
  let _lastBellCheckAt = null;
  let _lastBellCheckResult = null;
  let _lastUserActivityAt = 0;
  let _emptyRetryTimer = null; // the hidden-tab check's retry after an empty render
  let _openCheckRetryTimer = null; // the open check's retry after an empty render
  let _bellMountMissed = false; // the last click's dropdown never showed during its render wait
  let _observedBell = null;
  let _bellBadgeObserver = null;
  let bellCheckOnOpen = true; // popup toggle "Check notifications when a stream opens"

  const waitMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // The top-bar bell, by BELL_BUTTON_SELECTORS in order. A page whose only
  // notification button is a channel's own toggle has no bell: null.
  function findBellButton() {
    for (const selector of BELL_BUTTON_SELECTORS) {
      const bell = document.querySelector(selector);
      if (bell) return bell;
    }
    return null;
  }

  // The open check finds the bell the same way (today's bell has no
  // data-a-target, plan A46).
  function findOpenCheckBell() {
    return findBellButton();
  }

  // The unread count on the bell: the number in "N unread" or in the badge
  // ("9+" and "99+" read as 9 and 99), 1 for a badge without digits (a dot)
  // or a label that says unread without a number, 0 only for an explicit
  // zero, null when nothing tells.
  function parseBellBadgeCount(bellEl) {
    if (!bellEl) return null;
    const label = (bellEl.getAttribute && bellEl.getAttribute("aria-label")) || "";
    let m = label.match(/(\d+)\s*\+?\s*unread/i);
    if (m) return parseInt(m[1], 10);
    const badge = bellEl.querySelector('[class*="badge" i], [data-a-target*="badge" i]');
    if (badge) {
      m = (badge.textContent || "").match(/\d+/);
      return m ? parseInt(m[0], 10) : 1;
    }
    for (const child of bellEl.querySelectorAll("span, div")) {
      m = (child.textContent || "").trim().match(/^(\d{1,3})\+?$/);
      if (m) return parseInt(m[1], 10);
    }
    return /unread/i.test(label) ? 1 : null;
  }

  function getBellBadgeCount() {
    return parseBellBadgeCount(findBellButton());
  }

  function isBellDropdownOpen() {
    // The dropdown lives in a portaled overlay: its data-a-target, a
    // role=dialog with an aria-label that mentions notifications, or today's
    // unlabeled dialog with its balloon (STREAK_POPOVER_SELECTOR).
    return !!document.querySelector(STREAK_POPOVER_SELECTOR);
  }

  // The dropdown's text as the render wait reads it. Today's balloon shows
  // its header at once and the cards after a fetch, so there only the
  // cards count; a balloon without cards reads as empty.
  function dropdownText() {
    const pop = document.querySelector(STREAK_POPOVER_SELECTOR);
    if (!pop) return "";
    if (!pop.matches(BELL_BALLOON_SELECTOR) && !pop.querySelector(BELL_BALLOON_SELECTOR)) {
      return (pop.textContent || "").trim();
    }
    let text = "";
    for (const card of pop.querySelectorAll(STREAK_CARD_SELECTOR)) text += (card.textContent || "") + "\n";
    return text.trim();
  }

  // Resolves true once the dropdown's text is non-empty and unchanged for
  // BELL_RENDER_STABLE_MS, after at least BELL_RENDER_MIN_MS. At
  // BELL_RENDER_MAX_MS it goes on with what the dropdown shows: true when it
  // is open and holds text (a list that rendered late or is still changing),
  // false when nothing rendered. False at once when the dropdown closed after
  // it showed. The dropdown can mount after the click returns (Twitch's
  // update, a chunk still loading), so the first look comes one poll after
  // the click and a dropdown not there yet reads as empty. A wait that
  // never saw it sets _bellMountMissed.
  function waitForBellRender() {
    return new Promise((resolve) => {
      const started = Date.now();
      let lastText = null;
      let stableSince = started;
      let seenOpen = false;
      _bellMountMissed = false;
      const finish = (rendered) => {
        if (!seenOpen) _bellMountMissed = true;
        resolve(rendered);
      };
      const tick = () => {
        const now = Date.now();
        const open = isBellDropdownOpen();
        if (open) {
          seenOpen = true;
        } else if (seenOpen) {
          finish(false);
          return;
        }
        const text = open ? dropdownText() : "";
        if (text !== lastText) {
          lastText = text;
          stableSince = now;
        }
        if (text && now - started >= BELL_RENDER_MIN_MS && now - stableSince >= BELL_RENDER_STABLE_MS) {
          finish(true);
          return;
        }
        if (now - started >= BELL_RENDER_MAX_MS) {
          finish(!!text);
          return;
        }
        setTimeout(tick, BELL_RENDER_POLL_MS);
      };
      setTimeout(tick, BELL_RENDER_POLL_MS);
    });
  }

  // The dropdown first, then the whole page. Returns the dropdown's scan.
  function scanDropdownThenPage() {
    const pop = document.querySelector(STREAK_POPOVER_SELECTOR);
    const found = pop ? scanAndReport({ root: pop }) : { cards: 0, events: 0 };
    scanAndReport();
    return found;
  }

  function closeBellIfOpen(bell) {
    if (!isBellDropdownOpen()) return;
    const current = findOpenCheckBell() || findBellButton() || bell;
    if (current) current.click();
  }

  // Gives focus back to the element that had it before the click, when the
  // click moved it.
  function restoreFocus(previous) {
    const now = document.activeElement;
    if (now === previous) return;
    if (previous && previous !== document.body && previous.isConnected && typeof previous.focus === "function") {
      previous.focus({ preventScroll: true });
    } else if (now && now !== document.body && typeof now.blur === "function") {
      now.blur();
    }
  }

  function recordUserActivity(e) {
    if (e && e.isTrusted === false) return;
    _lastUserActivityAt = Date.now();
    // A dropdown that shows from now on may be one you opened.
    _bellMountMissed = false;
  }

  // A dropdown that the last click asked for and that showed only after its
  // render wait is this script's own, unless you pressed a key or clicked
  // since that click: then it may be one you opened, and it stays open.
  // The script's own clicks are untrusted, so they never count as yours.
  function lateMountIsOurs() {
    return _bellMountMissed && _lastUserActivityAt < _lastBellAutoClickAt;
  }

  // Passive listeners from load on, so the open check can tell whether you
  // are using the page.
  function startUserActivityWatch() {
    document.addEventListener("keydown", recordUserActivity, { capture: true, passive: true });
    document.addEventListener("pointerdown", recordUserActivity, { capture: true, passive: true });
  }

  function editableFocused() {
    const el = document.activeElement;
    if (!el || el === document.body) return false;
    const tag = String(el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable === true;
  }

  // You are using this page: it is visible and has focus, and an input or
  // editable element (the chat box) has focus or you pressed a key or
  // clicked in the last few seconds. A tab in a window you are not using
  // has no focus, so it is checked at once.
  function userBusyInPage() {
    if (document.visibilityState !== "visible") return false;
    if (typeof document.hasFocus === "function" && !document.hasFocus()) return false;
    return editableFocused() || Date.now() - _lastUserActivityAt < BELL_USER_ACTIVE_WINDOW_MS;
  }

  function bellReply(fields) {
    return { ok: false, opened: false, unreadBefore: null, rendered: false, cards: 0, events: 0, ...fields };
  }

  // After a dropdown that rendered nothing, each path tries once more
  // BELL_RETRY_AFTER_EMPTY_MS later, even at a zero count, and only its own
  // way. The hidden-tab check's retry stays hidden-only, so it never clicks
  // a tab you are looking at and never starts an open check on a page the
  // background did not pick.
  function scheduleRetryAfterEmpty() {
    if (_emptyRetryTimer) return;
    _emptyRetryTimer = setTimeout(() => {
      _emptyRetryTimer = null;
      maybeAutoOpenBell({ afterEmpty: true });
    }, BELL_RETRY_AFTER_EMPTY_MS);
  }

  // The open check's retry: only after an open check that checkBell or the
  // keepalive started, and once per page (_openCheckEmptyTries).
  function scheduleOpenCheckRetry() {
    if (_openCheckRetryTimer) return;
    _openCheckRetryTimer = setTimeout(() => {
      _openCheckRetryTimer = null;
      if (bellCheckOnOpen) runOpenBellCheck("open");
    }, BELL_RETRY_AFTER_EMPTY_MS);
  }

  async function openBellCheck(mode) {
    // Another tab's check read the account-wide inbox a moment ago.
    if (mode === "covered") {
      _openCheckDone = true;
      return bellReply({ ok: true, skipped: "covered" });
    }
    let bell = findOpenCheckBell();
    for (let i = 0; !bell && i < BELL_OPEN_CHECK_RETRIES; i++) {
      await waitMs(BELL_OPEN_CHECK_RETRY_MS);
      bell = findOpenCheckBell();
    }
    if (!bell) {
      // Logged out, or Twitch changed the bell. The flag stays unset once,
      // so the keepalive backstop tries one more time.
      if (++_openCheckNoBellTries >= 2) _openCheckDone = true;
      if (!_bellMissingSent) {
        _bellMissingSent = true;
        notifyBackground({ type: "bell_missing", page_url: window.location.href });
      }
      return bellReply({ reason: "no-bell" });
    }
    const busySince = Date.now();
    while (userBusyInPage()) {
      if (Date.now() - busySince >= BELL_USER_BUSY_MAX_MS) {
        // The hidden-tab checks take over once you leave the tab.
        _openCheckDone = true;
        return bellReply({ reason: "user-busy" });
      }
      await waitMs(BELL_USER_BUSY_POLL_MS);
    }
    while (_bellBusy) await waitMs(BELL_RENDER_POLL_MS);
    _bellBusy = true;
    const eventsBefore = _streakEventsSent;
    try {
      bell = findOpenCheckBell() || bell;
      const unreadBefore = parseBellBadgeCount(bell);
      const ownLate = lateMountIsOurs();
      _bellMountMissed = false;
      if (isBellDropdownOpen()) {
        // You opened it: read it, never click it or close it. One that the
        // last click asked for and that showed only after its render wait,
        // with no key or click of yours since, is this script's own: read
        // it, then close it.
        const found = scanDropdownThenPage();
        if (ownLate) {
          const focusedBefore = document.activeElement;
          closeBellIfOpen(bell);
          restoreFocus(focusedBefore);
        }
        _openCheckDone = true;
        return bellReply({
          ok: true, unreadBefore, rendered: !!dropdownText(), cards: found.cards,
          events: _streakEventsSent - eventsBefore,
        });
      }
      const focused = document.activeElement;
      const clickedAt = Date.now();
      bell.click();
      _lastBellAutoClickAt = Date.now();
      const rendered = await waitForBellRender();
      const found = rendered ? scanDropdownThenPage() : { cards: 0 };
      closeBellIfOpen(bell);
      // You pressed a key or clicked during the wait: focus is yours, leave it.
      if (_lastUserActivityAt < clickedAt) restoreFocus(focused);
      // No badge reads as 0 here, so the next notification is a rise.
      const after = parseBellBadgeCount(findBellButton());
      _lastBellBadgeCount = after === null ? 0 : after;
      if (!rendered) {
        if (++_openCheckEmptyTries >= 2) _openCheckDone = true;
        else scheduleOpenCheckRetry();
        return bellReply({ reason: "not-rendered", opened: true, unreadBefore });
      }
      _openCheckDone = true;
      return bellReply({
        ok: true, opened: true, unreadBefore, rendered: true, cards: found.cards,
        events: _streakEventsSent - eventsBefore,
      });
    } finally {
      _bellBusy = false;
    }
  }

  // The open check, once per page (a reload gets a fresh one). Concurrent
  // requests share the run in progress.
  function runOpenBellCheck(mode) {
    if (_openCheckDone) return Promise.resolve(bellReply({ ok: true, skipped: "done" }));
    if (_openCheckRun) return _openCheckRun;
    const finish = (reply) => {
      _openCheckRun = null;
      _lastBellCheckAt = Date.now();
      _lastBellCheckResult = reply.skipped || reply.reason || (reply.opened ? "opened" : "read-open");
      console.log(LOG_PREFIX, `Bell check on open: ${_lastBellCheckResult}, ${reply.unreadBefore === null ? "unknown" : reply.unreadBefore} unread before, ${reply.cards} cards, ${reply.events} streak events`);
      return reply;
    };
    _openCheckRun = openBellCheck(mode).then(finish, (e) => {
      console.warn(LOG_PREFIX, "Bell check on open failed:", e && e.message);
      return finish(bellReply({}));
    });
    return _openCheckRun;
  }

  // Keepalive backstop: the keepalive reaches every Stream Monitor tab every
  // 2 minutes and survives a background restart, so a lost request costs
  // at most one tick. Off with the popup toggle. An open check waiting for
  // its retry after an empty render gets that retry, not an early rerun.
  function maybeRunBellBackstop() {
    if (_openCheckDone || _openCheckRun || _openCheckRetryTimer || !bellCheckOnOpen) return;
    if (Date.now() - _pageStartedAt < BELL_BACKSTOP_MIN_PAGE_AGE_MS) return;
    runOpenBellCheck("open");
  }

  // Whether the slow check of a bell without badge markup is due: none in
  // the last BELL_HIDDEN_NO_BADGE_INTERVAL_MS, counted from the page load or
  // this script's last click on the bell, whichever is later.
  function noBadgeCheckDue(now) {
    const last = Math.max(_pageStartedAt, _lastBellAutoClickAt, _lastNoBadgeCheckAt);
    return now - last >= BELL_HIDDEN_NO_BADGE_INTERVAL_MS;
  }

  // The hidden-tab check: only while hidden, only on unread (or once after a
  // dropdown that rendered nothing), at most once a minute. A bell with no
  // badge markup at all (count null, today's bell) gets the slow check; an
  // explicit 0 never clicks.
  async function maybeAutoOpenBell(opts = {}) {
    if (document.visibilityState !== "hidden" || _bellBusy) return;
    const now = Date.now();
    if (now - _lastBellAutoClickAt < BELL_AUTO_CLICK_MIN_INTERVAL_MS) return;
    const bell = findBellButton();
    if (!bell) return;
    const count = parseBellBadgeCount(bell);
    const slowCheck = count === null && noBadgeCheckDue(now);
    if (!opts.afterEmpty && !(count > 0) && !slowCheck) {
      // Nothing unread, nothing to surface. Update the baseline so the next
      // real increase fires a click (no badge reads as 0 here).
      _lastBellBadgeCount = count === null ? 0 : count;
      return;
    }
    if (slowCheck) _lastNoBadgeCheckAt = now;
    const alreadyOpen = isBellDropdownOpen();
    // A dropdown that the last click asked for and that showed only after
    // its render wait, with no key or click of yours since, is this
    // script's own.
    const ownLate = alreadyOpen && lateMountIsOurs();
    _bellMountMissed = false;
    _bellBusy = true;
    try {
      let rendered = true;
      if (!alreadyOpen) {
        bell.click();
        _lastBellAutoClickAt = Date.now();
        rendered = await waitForBellRender();
      }
      if (rendered) scanDropdownThenPage();
      // Close the dropdown only if this script opened it. If you had it open
      // before leaving the tab, it stays.
      if (!alreadyOpen || ownLate) closeBellIfOpen(bell);
      // No badge reads as 0 here, so the next notification is a rise.
      const after = parseBellBadgeCount(findBellButton());
      _lastBellBadgeCount = after === null ? 0 : after;
      if (!rendered && !opts.afterEmpty) scheduleRetryAfterEmpty();
      console.log(
        LOG_PREFIX,
        `Bell auto-surveillance fired (${count === null ? "unknown" : count} unread before, ${after === null ? "unknown" : after} after${slowCheck ? ", no badge: the slow check" : ""}${rendered ? "" : ", nothing rendered"})`
      );
    } catch (e) {
      console.warn(LOG_PREFIX, "Bell auto-click failed:", e && e.message);
    } finally {
      _bellBusy = false;
    }
  }

  function startBellSurveillance() {
    // Establish a baseline once the bell exists, then fire if the tab is
    // already hidden with unread notifications waiting (or a bell without
    // badge markup whose slow check is due).
    const baseline = () => {
      const bell = findBellButton();
      if (!bell) {
        setTimeout(baseline, 5000);
        return;
      }
      const count = parseBellBadgeCount(bell);
      _lastBellBadgeCount = count === null ? 0 : count;
      if (document.visibilityState === "hidden" && (count > 0 || count === null)) {
        maybeAutoOpenBell();
      }
      attachBellBadgeObserver(bell);
    };
    setTimeout(baseline, 10000);

    // Every minute: follow a remounted bell and retry on hidden tabs (the
    // throttle inside keeps this from spamming).
    setInterval(() => {
      ensureBellBadgeObserver();
      maybeAutoOpenBell();
    }, 60 * 1000);

    // React the moment the tab is hidden: unread notifications can now be
    // surfaced without anyone seeing the dropdown.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        setTimeout(() => maybeAutoOpenBell(), 3000);
      } else {
        // You can see the tab now: a dropdown open from here on may be yours.
        _bellMountMissed = false;
      }
    });
  }

  function attachBellBadgeObserver(bell) {
    if (typeof MutationObserver !== "function" || !bell) return;
    if (_bellBadgeObserver) _bellBadgeObserver.disconnect();
    _observedBell = bell;
    _bellBadgeObserver = new MutationObserver(() => {
      if (!bell.isConnected) {
        ensureBellBadgeObserver();
        return;
      }
      // No badge reads as 0 for the rise baseline (the hidden path still
      // clicks only on a count above 0, or for a bell without badge markup
      // on its slow check).
      const raw = parseBellBadgeCount(bell);
      const count = raw === null ? 0 : raw;
      if (_lastBellBadgeCount !== null && count > _lastBellBadgeCount) {
        // The badge rose: almost always a new notification.
        maybeAutoOpenBell();
      }
      _lastBellBadgeCount = count;
    });
    _bellBadgeObserver.observe(bell, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["aria-label"],
    });
  }

  // Twitch can remount the top bar; the observer then watches a bell that
  // left the page. Find the bell in the page now and watch that one.
  function ensureBellBadgeObserver() {
    if (_observedBell && _observedBell.isConnected) return;
    const bell = findBellButton();
    if (!bell) return;
    attachBellBadgeObserver(bell);
    const raw = parseBellBadgeCount(bell);
    const count = raw === null ? 0 : raw;
    if (_lastBellBadgeCount !== null && count > _lastBellBadgeCount) maybeAutoOpenBell();
    _lastBellBadgeCount = count;
  }

  // The popup toggle "Check notifications when a stream opens" (default
  // on) gates the keepalive backstop; the background reads it before it
  // asks. The hidden-tab checks do not depend on it.
  function startBellCheckSetting() {
    try {
      chrome.storage.local.get("bellCheckOnOpen", (result) => {
        bellCheckOnOpen = !(result && result.bellCheckOnOpen === false);
      });
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area === "local" && changes.bellCheckOnOpen) {
          bellCheckOnOpen = changes.bellCheckOnOpen.newValue !== false;
        }
      });
    } catch (e) {
      // No storage in this context: the default (on) stays.
    }
  }

  // Expose parser for jsdom tests when running outside the browser. Guarded
  // so production browser execution is unaffected.
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
      chrome.runtime.sendMessage({
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
    chrome.storage.local.get("autoClaimBonus", (result) => {
      autoClaimBonusEnabled = (result && result.autoClaimBonus) ?? true;
    });

    chrome.storage.onChanged.addListener((changes, area) => {
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
    module.exports = {
      parseStreakText, parseBellBadgeCount, findBonusClaimButton, parseAlreadySavedText,
      parseTimeAgo, streakLoginFor, isNotEligibleText, streakCandidateSource, streakCardRelation,
      STREAK_RESERVED_PATHS, parseTimeAgoSeconds, streakIsEscalation, streakCardEvent, passesAgeGate,
      scanAndReport, waitForBellRender, saveStreakScanReply, findBellButton, isBellDropdownOpen,
      findKeptStreakDialog,
    };
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
    startBellCheckSetting();
    startUserActivityWatch();
  }
  startBonusClaimer();
})();
