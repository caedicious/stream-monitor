# Why a Stream Might Not Open

This document is a checklist of every plausible reason Stream Monitor might
fail to open a tab when one of your monitored streamers goes live, ordered
roughly by likelihood.

For each case it notes:

- **What happens:** the failure mode from the user's perspective.
- **Detection today:** whether the activity log (`stream_activity.jsonl`)
  catches it as of v1.5.2.
- **Mitigation / future detection:** what would help.

The activity log is the primary tool for cross-checking against streamer
broadcast histories. Open `View Activity Log` from the tray menu, or visit
`http://127.0.0.1:52832/activity`.

---

## Browser-level failures

These are the most user-visible because the desktop app does its job (asks
the OS to open a URL) but the browser doesn't actually load the URL.

### B1. Firefox "Restore previous session?" prompt

After Firefox crashes or is force-closed, the next launch shows a modal
prompt before any normal page load. URLs requested by external apps are
often queued behind it and may never resolve.

- **Detection today:** partial. The activity log records
  `tab_open_attempt` with `success: true` (because `webbrowser.open()`
  reported the browser was invoked successfully). But there's no record
  of whether the tab actually loaded.
- **Mitigation:** see [Round-trip tab confirmation](#round-trip-tab-open-confirmation)
  below. The browser extension would confirm when it sees the new tab,
  letting the desktop app log `tab_open_unconfirmed` if no confirmation
  arrives.

### B2. Browser "Update is ready, restart to apply" state

Chrome and Firefox both have a state where pending updates queue an
"update available, restart" prompt. New tabs from external apps may go
to this update flow rather than the requested URL.

- **Detection:** identical situation to B1.
- **Mitigation:** identical to B1.

### B3. Browser hung or unresponsive

Browser process exists but is locked up. `webbrowser.open()` returns true
because the OS handed off the URL request, but the browser never
processes it.

- **Detection:** identical to B1.
- **Mitigation:** identical to B1.

### B4. No default browser configured

If Windows has no default browser set, `webbrowser.open()` may silently
fail or open the "Choose an app" dialog instead of loading the URL.

- **Detection today:** activity log records `tab_open_attempt` with the
  return value of `webbrowser.open()`. On Windows the return value is
  unreliable in this scenario, so `success: true` may still be logged
  even when no browser opens.
- **Mitigation:** verify that `os.startfile` / Windows shell
  associations resolve at app startup, log `default_browser_check`
  result. Optionally let the user pick a specific browser by name in
  settings (`webbrowser.get('firefox')`).

### B5. PC locked or user logged out

The browser opens the tab but the user can't see it. This isn't actually
a failure: the player runs and the streak counts. Including for
completeness because users sometimes report it as a failure.

- **Detection:** N/A, not a failure.
- **Mitigation:** N/A.

---

## Network-level failures

The app can't talk to Twitch, so it never sees the streamer go live.

### N1. Twitch API down or rate-limited mid-check

`_api_get` raises a `RequestException`. `check_streams` catches it and
re-raises so `_monitor_loop` can count consecutive errors and notify the
user.

- **Detection today:** `api_error` event is logged with the streak
  counter. `api_recovered` is logged when the streak goes back to zero.
  Gaps in normal `stream_live` / `stream_offline` events between these
  bracketed errors tell you exactly when the API was unreliable.
- **Note:** the catch in `check_streams` returns
  `{name: False for name in self.streamers}` on exception in some paths,
  which counts as "everyone went offline" momentarily. If a streamer
  was live at the time, the next successful check will fire a fresh
  offline -> live transition, which is correct.

### N2. Twitch token expired

Auto-refreshed since v1.4.1. `_api_get` retries the call after getting a
new token on a 401.

- **Detection today:** the 401 surfaces as an `api_error` event briefly,
  followed by `api_recovered`. The actual auto-refresh is logged in the
  debug log (`stream_monitor.log`) but not the activity log.

### N3. Local network drop / DNS failure

Same shape as N1. The desktop app retries on the next interval and
recovers automatically when the network returns.

---

## Application-level failures

The app itself isn't running, isn't checking, or isn't checking what
you'd expect.

### A1. App not running

User killed it, didn't restart it, post-update gap, etc.

- **Detection today:** `app_started` events bracket every run. Time
  gaps between an `app_stopped` (or the implicit stop from a crash)
  and the next `app_started` show downtime windows. Cross-reference
  these against the streamer's broadcast history.
- **Note on crashes:** the `app_stopped` event only fires on clean
  exit (via the tray menu's Exit item). If the app crashes, you'll
  see a long gap with no events at all. The gap itself is the
  detection signal.

### A2. App paused (manually or auto-paused)

Stream is detected as live but the tab is intentionally not opened.

- **Detection today:** `tab_open_skipped` event is logged with
  `reason: paused` or `reason: auto_paused`. `auto_paused_started`
  and `auto_paused_ended` events bracket auto-pause windows so you can
  see when your own channel was live.

### A3. Streamer not in monitored list

Streamer went live but isn't in your config.

- **Detection today:** every `config_loaded` event records the full
  `streamers` list at the time. If the missed streamer was not in any
  recent `config_loaded`, they were never monitored at the time of the
  broadcast.

### A4. Typo in streamer name

Same effect as A3 from the API's perspective: that name never matches a
live stream.

- **Detection today:** `config_loaded` records the exact strings
  configured. Eyeball the list for typos.
- **Mitigation:** at config save time, the desktop app could call the
  Twitch Users endpoint to verify each name exists. Currently it does
  not.

### A5. Check interval too long for a brief stream

Default is 60 seconds. A stream that goes live and offline within 60s is
invisible to the polling loop.

- **Detection today:** none — by definition there are no events.
- **Mitigation:** none reasonable. 60s is already aggressive for the
  Twitch API rate limits with multi-streamer configs. Streams that
  brief are extremely rare.

### A6. App crashed mid-cycle

Same detection signature as A1.

- **Detection today:** large gap with no events, no `app_stopped`. The
  debug log (`stream_monitor.log`) will have the traceback if Python
  caught it.

### A7. First-observation race

When the app starts up while a streamer is already live, `was_live`
initializes to `False`, and the first check sets it to `True`. The
condition `not state.was_live and not state.browser_opened` is true on
that first check, so the tab opens. **This works correctly today.**

---

## OS-level failures

### O1. Windows Update / forced restart overnight

PC restarts, app comes back when user logs in (auto-start runs from
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`).

- **Detection today:** gap pattern in the activity log; `app_started`
  logs the resume. If the user's own `app_stopped` is missing before
  the gap, the restart was unexpected.

### O2. PC sleep / hibernate

API polling pauses while asleep. Streams that went live AND offline
entirely during sleep are missed; streams that are still live on wake
fire as fresh `stream_live` events.

- **Detection today:** **`wake_detected`** events fire when a single
  loop iteration takes longer than `2 * check_interval` seconds. Cross-
  reference `wake_detected` events with broadcast histories to see if
  any streams were missed inside the sleep window.

### O3. Anti-virus or firewall blocking

Rare. If the firewall blocks `api.twitch.tv`, every check fails like
N1.

- **Detection today:** continuous `api_error` events with no
  `api_recovered`, while the desktop app's tray notification surfaces
  the issue.

---

## Streamer-side cases

### S1. Streamer is live as "Unlisted"

Twitch API still reports the stream as live, so the tab opens to the
public URL. The viewer may see a "this stream is unlisted" page rather
than the actual content.

- **Detection today:** `tab_open_attempt` succeeds. From the activity
  log alone you can't distinguish this from a normal open.
- **Mitigation:** none on our side; this is Twitch's choice.

### S2. Twitch URL pattern changes

Currently `https://www.twitch.tv/<channel>` works. Has not changed
historically.

---

## Round-trip tab-open confirmation

This is the proposed Phase-2 mechanism for catching the
browser-level failures (B1-B4) that the activity log alone cannot
detect.

Mechanism:

1. Desktop calls `webbrowser.open(url)` and logs `tab_open_attempt`
   with a unique `attempt_id`.
2. Desktop posts the pending attempt to its own `/pending_opens`
   endpoint.
3. Browser extension polls `/pending_opens` (or piggy-backs on its
   existing `/config` poll). When it sees a tab open whose URL matches
   a pending attempt, it POSTs to `/confirm_open` with the
   `attempt_id`.
4. Desktop logs `tab_open_confirmed`. If no confirmation arrives within
   90 seconds, logs `tab_open_unconfirmed`.

The confirmation pattern catches:

- B1 (Firefox restore-session): no extension activity in the new tab,
  no confirmation, `tab_open_unconfirmed` fires.
- B2 (browser update prompt): same shape.
- B3 (browser hung): same shape.
- B4 (no default browser): same shape.

Cost: new endpoints on both sides, polling discipline in the extension,
and a 90-second window of uncertainty before the unconfirmed event
fires. Worth building if the activity log shows unexplained gaps that
look like B-class failures.

---

## How to use the activity log to investigate a missed stream

1. Get the streamer's broadcast time (Twitch dashboard or third-party
   stream history sites have this).
2. Open the activity log viewer.
3. Filter by streamer, then look for a `stream_live` event near the
   start of their broadcast.
4. If `stream_live` is present:
   - Look for the matching `tab_open_attempt` immediately after.
   - If present with `success: true`, the failure is browser-level
     (Firefox restore-session, etc.) — see B1-B4.
   - If `tab_open_skipped`, you were paused at the time.
5. If `stream_live` is absent:
   - Check for `wake_detected` near the broadcast time. If present,
     your PC was asleep and missed it.
   - Check whether the streamer's name is in the most recent
     `config_loaded`. If not, they weren't monitored.
   - Check for `api_error` events around that time. If present, the
     Twitch API was unreliable.
   - If none of the above, look for a long gap with no events at all
     (suggests the app wasn't running).
