# Privacy Policy

**Stream Monitor** and **Stream Monitor Companion** (the browser extension)
are designed around a simple principle: the user's data stays on the user's
own computer.

This policy covers both the desktop application and the companion browser
extensions for Chrome (and Chromium-based browsers such as Brave, Edge, and
Opera) and Firefox.

_Last updated: 15 April 2026._

## What data is handled

The extension stores the following information locally on your device using
your browser's built-in extension storage (`chrome.storage.local` /
`browser.storage.local`):

- The list of Twitch streamers you have configured in the desktop app.
- The IDs of browser tabs that were opened by the desktop app, along with the
  streamer each tab was opened for.
- Your extension preferences (auto-mute tabs, low-quality playback,
  raid-follow-through, maximum tab count).
- A small rotating log (up to 200 entries) of the extension's own activity,
  used for troubleshooting. You can clear this at any time from the
  extension's debug page.

The desktop application stores your Twitch API credentials and your list of
monitored streamers in a configuration file on your own PC
(`%APPDATA%\StreamMonitor\config.json` on Windows).

## What data is transmitted

- The extension contacts **only** `http://127.0.0.1:52832` — a local web
  server provided by the Stream Monitor desktop application running on the
  same PC. This contact never leaves your machine.
- The desktop application contacts the official **Twitch Helix API**
  (`https://api.twitch.tv/helix/*`) to check which of your configured
  streamers are live. This uses only the Twitch API credentials you provided
  during setup; no personal information about you is sent.
- Neither the extension nor the desktop application contacts any server
  operated by the developer or by any third party (other than Twitch).

## What data is NOT collected

- We do **not** collect, store, or transmit any personally identifiable
  information.
- We do **not** track your browsing history.
- We do **not** use analytics, telemetry, advertising networks, or any
  third-party trackers.
- We do **not** sell, share, or transfer user data to anyone.
- We do **not** use your data for any purpose unrelated to the extension's
  single purpose (coordinating Twitch tabs opened by the Stream Monitor
  desktop app).

## Permissions used by the extension

- **`tabs`** — to detect when a tab opened by the desktop app navigates to a
  different Twitch channel (a raid) so the tab can be closed, and to mute
  tabs at the browser level when auto-mute is enabled.
- **`storage`** — to save your preferences and the list of tracked tabs
  locally on your device.
- **`alarms`** — to periodically refresh the streamer list from the local
  desktop app and to keep Twitch players alive in background tabs.
- **`http://127.0.0.1/*`** — to contact the Stream Monitor desktop
  application running on your own PC.

## Your data, your control

Uninstalling the extension removes all data it stored in your browser.
Uninstalling the desktop application or deleting the
`%APPDATA%\StreamMonitor` folder removes all data it stored on your PC.

## Contact

Questions or concerns about this policy can be raised as an issue on the
project's GitHub repository:

https://github.com/caedicious/stream-monitor/issues
