# Stream Monitor

[![Release](https://img.shields.io/github/v/release/caedicious/stream-monitor?include_prereleases)](https://github.com/caedicious/stream-monitor/releases/)
[![Downloads](https://img.shields.io/github/downloads/caedicious/stream-monitor/total)](https://github.com/caedicious/stream-monitor/releases)
[![Firefox Add-on](https://img.shields.io/amo/v/stream-monitor-tab-closer?label=Firefox)](https://addons.mozilla.org/firefox/addon/stream-monitor-tab-closer/)
[![Chrome Web Store](https://img.shields.io/chrome-web-store/v/aaaaibcmmahcedpcdfcbhnjfkgmcgcii?label=Chrome)](https://chromewebstore.google.com/detail/stream-monitor-companion/aaaaibcmmahcedpcdfcbhnjfkgmcgcii)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-blue)

## 📥 [Download the latest release](https://github.com/caedicious/stream-monitor/releases/latest)

**The easiest way to install Stream Monitor is to grab the installer from the [Releases page](https://github.com/caedicious/stream-monitor/releases/latest)**, or just click the **Release** badge at the top of this README. No Python or build tools needed. Just download, run the installer, and you're set.

> [!NOTE]
> Want to verify your download is genuine? Hashes for every release artifact are committed to [`SHA256SUMS.txt`](SHA256SUMS.txt) at the root of this repo. Run `Get-FileHash StreamMonitorInstaller.exe -Algorithm SHA256` in PowerShell and compare against the matching line.
>
> Pre-release builds (when available) are listed on the [Releases page](https://github.com/caedicious/stream-monitor/releases) with a "Pre-release" tag.

A Windows application that monitors Twitch streamers and automatically opens their stream in your browser when they go live. Pair it with the companion browser extension to auto-close tabs on raids and keep background streams counted as views.

## Features

### Desktop app
- **System Tray App**: Runs quietly in the background
- **Auto-Start**: Launches when you log into Windows
- **Easy Setup**: Guided wizard walks you through configuration
- **Unlimited Streamers**: Monitor as many streamers as you want
- **Smart Detection**: Only opens browser when streamer goes from offline → live (no spam)
- **Auto-Pause When Live**: Stops opening streams while you're live on your own channel
- **Slot Mode** (optional, off by default): Keeps at most three stream tabs open on a busy night (2 Keep Open slots and 1 rotating slot by default). The Keep Open slots show your highest-ranked Keep Open streamers who are live. Everyone else who is live, including Keep Open streamers who did not fit, gets one turn in the rotating slot (30 minutes by default), in list order. A higher-ranked Keep Open streamer takes over a Keep Open slot only after the stream there has had its minutes (Minutes per turn, 30 by default). When everyone has had a turn, the rotating slot stays on your highest-ranked live stream until someone new goes live or a broken streak needs its turn. A stream that ends frees its slot, and a slot tab you close yourself stays closed for the rest of that broadcast. Closing a whole browser window that holds slot tabs works differently while the browser keeps running: those streams reopen once, in the stream window (recreated where it was if that is the window you closed), or in your current window when no stream window is set. Close the window on them again in the same broadcast and those streams stay closed, and their slots go to other live streams. Closing a browser's last window quits the browser on Windows, so nothing reopens right away: about 2 to 3 minutes later the desktop app opens the planned streams itself, which starts the browser again, and it does this after every quit unless monitoring is paused. The desktop app decides and the browser extension (1.12 or newer) opens and closes the tabs; until one reports, streams open the usual way and the tray says so once
- **Automatic Streak Saves** (optional, off by default): When Twitch's notifications show that a watch streak broke or is about to end, Stream Monitor opens that streamer's save-streak page for one turn, and ends the turn as soon as Twitch says the streak is already kept. Twitch may move the page on to a clip or a past broadcast of that streamer; the tab stays that streamer's save turn. In Slot mode the page takes a turn in the rotating slot (one due within 6 hours goes ahead of live streams still waiting for their turn), and a stream that ends before its turn gets a save-streak turn too. Outside Slot mode the page opens as a Stream Monitor tab; when that streamer is live and already has a tab, the live tab comes first and the page is checked once the stream ends
- **Settings GUI**: Right-click tray icon to change streamers anytime
- **Auto-Update Check**: Notifies you when a new version is available

### Browser extension (Firefox + Chrome / Brave / Edge / Opera)
- **Raid Detection**: Automatically closes tabs when a streamer raids someone else
- **Keeps You Counted**: Keeps the Twitch player unmuted at the player level so you stay in the viewer count, even when the tab is muted at the browser level
- **Auto-Mute Tabs**: Optionally mute every stream tab at the browser level so a dozen streams don't shout at you
- **Low Quality Mode**: Optionally drop every opened stream to the lowest quality to save bandwidth
- **Raid Follow-Through**: Optionally stay for exactly one raid hop before closing
- **Auto-Claim Bonus Points**: The extension clicks the channel points chest when it appears, on any Twitch tab, including Twitch chat embedded on other sites (toggle in the extension popup)
- **Max Tabs Limit**: Cap how many concurrent stream tabs can be open at once (not used while Slot mode runs in that browser). When a new stream goes over the limit, the open stream lowest in your streamer list is the one that closes, the new one included, and only after it has had 30 minutes for the streak. A tab left on a stream that ended, or an older second tab of the same stream, goes first. Keep Open streamers are never closed for the limit
- **Stream Window**: Click "Use this window for streams" in the extension popup to pick one browser window for streams. Stream tabs the desktop app opens move there, and tabs the extension opens itself are created there. With Auto-focus on, a new stream comes to the front inside that window without pulling you out of the window you are working in, and that window keeps the tab you were on. If you close the stream window, the next stream to open reopens it at its last position and size (in Slot mode, while the browser keeps running, its streams come back once right away, see Slot Mode). A tab you drag out of it stays where you put it. It works with or without Slot mode, one window per browser profile, and private windows can't be used. The desktop app opens streams in your Windows default browser, so pick the window in that browser. It works best on a screen you are not working on: a covered or minimized window plays its streams as hidden tabs
- **Notifications Check on Open**: About 12 seconds after a stream tab opens, the extension opens Twitch's notifications bell once, reads any broken or at-risk streak cards, and closes it again. If you are using that tab when the check is due (typing, clicking, or with the chat box selected), it waits up to a minute for you to stop, then skips the check on that tab until the page reloads. It reads the list without closing it if you already have it open, and skips the check when another tab checked in the last minute, since the notifications are the same in every tab. Streaks it finds are sent to the desktop app, and the ones that still need saving show under Streaks at Risk in the popup. Opening the bell may mark your notifications as read on your other devices, as the checks on hidden tabs already could. Toggle "Check notifications when a stream opens" in the popup (on by default)
- **Streaks at Risk**: Clicking a row in the popup opens that streamer's save-streak page in a Stream Monitor tab (muted if Auto-mute is on, kept playing), or in Slot mode makes it the next rotating turn. The row counts as handled only after the page has had its visit or Twitch says the streak is kept; closing the tab early leaves it waiting

## For Users

Download the installer from the Releases page and run it. The setup wizard will guide you through:

1. Choosing which streamers to monitor
2. Creating a free Twitch Developer application
3. Entering your API credentials

After setup, Stream Monitor runs in your system tray and automatically starts when you log in.

### Browser Extension (Optional)

The companion browser extension auto-closes tabs when a streamer raids, keeps background streams counted as viewers, and adds auto-mute / low-quality / max-tabs controls and auto-claims the channel points bonus. It talks only to the desktop app running on your own PC (`http://127.0.0.1:52832`), so nothing leaves your machine.

The desktop app sends one anonymous ping a day (a random install ID, the version, and the OS name) to the developer's server so active installs can be counted. Nothing else is sent, no IP address is stored, and the "Send anonymous install ping" checkbox in Settings turns it off. Details in [PRIVACY.md](PRIVACY.md).

**Firefox**
Install from the Mozilla Add-ons store:
https://addons.mozilla.org/firefox/addon/stream-monitor-tab-closer/

**Chrome / Brave / Edge / Opera / other Chromium browsers**
Install from the Chrome Web Store:
https://chromewebstore.google.com/detail/stream-monitor-companion/aaaaibcmmahcedpcdfcbhnjfkgmcgcii

The desktop app must be running for the extension to do anything. It pulls your monitored streamer list from the local config server on port 52832. It also tells the desktop app which streams already have a tab open, so relaunching the app does not open them a second time.

### Updating

When a new version is available, you'll see "Update available" in the tray tooltip. Click "Check for Updates" in the menu to download the new installer. Your settings will be preserved during the update.

### Changing Settings

Right-click the Stream Monitor icon in your system tray and select "Settings" to:
- Add or remove streamers
- Update your Twitch credentials
- Change the check interval
- Turn on "Save broken streaks automatically (opens Twitch's save-streak page for one turn)" (off by default)
- Turn Slot mode on or off and size its slots with the "Slot mode..." button

The row next to that button shows the current setting, for example "Slot mode: off" or "Slot mode: 2 Keep Open + 1 rotating, 30 min". The Slot mode dialog has:
- **Slot mode**: the checkbox that turns it on (needs browser extension 1.12 or newer)
- **Keep Open slots** (0 to 2, default 2): tabs for your highest-ranked Keep Open streamers who are live
- **Rotating slots** (1 to 3, default 1): tabs where everyone else who is live takes turns; there is always at least one
- **Minutes per turn** (5 to 120, default 30): the length of a turn, and how long a stream keeps its Keep Open slot before a higher-ranked Keep Open streamer can take it

Keep Open and Rotating slots together can be at most 3. OK keeps the values, and Save in the Settings window writes them. Your streamer list order is the priority (drag a row to move it), and the Keep Open button marks the streamers who compete for the Keep Open slots. A Keep Open slot with no Keep Open streamer live serves turns too, and goes back to a Keep Open streamer when the current turn ends. While Slot mode is on, "Auto-open VOD if stream missed" is grayed out: with "Save broken streaks automatically" on, a stream that ended before its turn gets a save-streak turn instead, and the dialog shows a tip when that setting is off.

---

## For Developers: Building the Installer

### Prerequisites

1. **Python 3.10+** - https://python.org
2. **Inno Setup** - https://jrsoftware.org/isinfo.php (for creating the installer)

### Build Steps

1. **Clone/download this repository**

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   pip install pyinstaller
   ```

3. **Generate the icon:**
   ```bash
   python create_icon.py
   ```

4. **Build the executables:**
   ```bash
   build.bat
   ```
   
   Or manually:
   ```bash
   pyinstaller --onefile --windowed --name "StreamMonitor" --icon=icon.ico stream_monitor_tray.py
   pyinstaller --onefile --windowed --name "StreamMonitorSetup" --icon=icon.ico setup_wizard.py
   ```

5. **Create the installer:**
   - Open `installer.iss` in Inno Setup Compiler
   - Click Build → Compile
   - The installer will be created in `installer_output/StreamMonitorInstaller.exe`

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

### Project Structure

```
stream-monitor/
├── stream_monitor_tray.py      # Main tray application
├── settings_editor.py          # Settings GUI (launched as a separate process)
├── setup_wizard.py             # First-run setup wizard
├── about.html                  # Welcome / about page served on 127.0.0.1:52832
├── create_icon.py              # Desktop icon generator (icon.ico)
├── create_chrome_icons.py      # Chrome extension icon generator
├── build_chrome_zip.py         # Packages the Chrome Web Store submission zip
├── generate_checksums.py       # SHA256 checksums for release artifacts
├── build.bat                   # Build script (PyInstaller + .xpi)
├── installer.iss               # Inno Setup installer script
├── requirements.txt            # Runtime dependencies
├── requirements-dev.txt        # Dev / test dependencies
├── pytest.ini                  # Pytest config
├── chrome_extension/           # Chromium companion extension source
├── firefox_extension/          # Firefox companion extension source
├── tests/                      # Pytest suite
├── PRIVACY.md                  # Privacy policy for the app + extensions
└── README.md                   # This file
```

### Configuration Storage

User configuration is stored in:
- Windows: `%APPDATA%\StreamMonitor\config.json`

Startup shortcut is created in:
- Windows: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`

---

## How It Works

1. The app authenticates with Twitch using your Client ID and Secret
2. Every 60 seconds (configurable), it checks if monitored streamers are live
3. When a streamer transitions from offline → live, it opens their stream in your default browser
4. When they go offline, the state resets so it can trigger again next time
5. With Slot mode on and a 1.12 browser extension reporting, the app decides instead which live streams have a tab, and the extension opens and closes tabs to match

## Twitch API Usage

This app uses the Twitch Helix API to check stream status. It:
- Uses Client Credentials flow (no user login required)
- Only calls the `/helix/streams` endpoint
- Makes ~1 API call per minute (well under rate limits)

## License

Released under the [MIT License](LICENSE). You're free to use, modify, and redistribute the code; see the LICENSE file for the full terms.
