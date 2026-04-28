# Stream Monitor

[![Release](https://img.shields.io/github/v/release/caedicious/stream-monitor?include_prereleases)](https://github.com/caedicious/stream-monitor/releases/)
[![Downloads](https://img.shields.io/github/downloads/caedicious/stream-monitor/total)](https://github.com/caedicious/stream-monitor/releases)
[![Firefox Add-on](https://img.shields.io/amo/v/stream-monitor-tab-closer?label=Firefox)](https://addons.mozilla.org/firefox/addon/stream-monitor-tab-closer/)
[![Chrome Web Store](https://img.shields.io/chrome-web-store/v/aaaaibcmmahcedpcdfcbhnjfkgmcgcii?label=Chrome)](https://chromewebstore.google.com/detail/stream-monitor-companion/aaaaibcmmahcedpcdfcbhnjfkgmcgcii)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-blue)

## 📥 [Download the latest release](https://github.com/caedicious/stream-monitor/releases/latest)

**The easiest way to install Stream Monitor is to grab the installer from the [Releases page](https://github.com/caedicious/stream-monitor/releases/latest)**, or just click the **Release** badge at the top of this README. No Python or build tools needed. Just download, run the installer, and you're set.

> [!TIP]
> 🧪 **Want to try experimental features early?** Grab the [latest pre-release](https://github.com/caedicious/stream-monitor/releases/tag/v1.5.4-pre) for new functionality that hasn't yet been fully tested. Pre-releases ship ahead of the Chrome Web Store and Firefox Add-ons versions, so store users stay on the stable line until the features are verified.

> [!NOTE]
> Want to verify your download is genuine? Hashes for every release artifact are committed to [`SHA256SUMS.txt`](SHA256SUMS.txt) at the root of this repo. Run `Get-FileHash StreamMonitorInstaller.exe -Algorithm SHA256` in PowerShell and compare against the matching line.

A Windows application that monitors Twitch streamers and automatically opens their stream in your browser when they go live. Pair it with the companion browser extension to auto-close tabs on raids and keep background streams counted as views.

## Features

### Desktop app
- **System Tray App**: Runs quietly in the background
- **Auto-Start**: Launches when you log into Windows
- **Easy Setup**: Guided wizard walks you through configuration
- **Unlimited Streamers**: Monitor as many streamers as you want
- **Smart Detection**: Only opens browser when streamer goes from offline → live (no spam)
- **Auto-Pause When Live**: Stops opening streams while you're live on your own channel
- **Settings GUI**: Right-click tray icon to change streamers anytime
- **Auto-Update Check**: Notifies you when a new version is available

### Browser extension (Firefox + Chrome / Brave / Edge / Opera)
- **Raid Detection**: Automatically closes tabs when a streamer raids someone else
- **Keeps You Counted**: Keeps the Twitch player unmuted at the player level so you stay in the viewer count, even when the tab is muted at the browser level
- **Auto-Mute Tabs**: Optionally mute every stream tab at the browser level so a dozen streams don't shout at you
- **Low Quality Mode**: Optionally drop every opened stream to the lowest quality to save bandwidth
- **Raid Follow-Through**: Optionally stay for exactly one raid hop before closing
- **Max Tabs Limit**: Cap how many concurrent stream tabs can be open at once

## For Users

Download the installer from the Releases page and run it. The setup wizard will guide you through:

1. Choosing which streamers to monitor
2. Creating a free Twitch Developer application
3. Entering your API credentials

After setup, Stream Monitor runs in your system tray and automatically starts when you log in.

### Browser Extension (Optional)

The companion browser extension auto-closes tabs when a streamer raids, keeps background streams counted as viewers, and adds auto-mute / low-quality / max-tabs controls. It talks only to the desktop app running on your own PC (`http://127.0.0.1:52832`), so nothing leaves your machine.

**Firefox**
Install from the Mozilla Add-ons store:
https://addons.mozilla.org/firefox/addon/stream-monitor-tab-closer/

**Chrome / Brave / Edge / Opera / other Chromium browsers**
Install from the Chrome Web Store:
https://chromewebstore.google.com/detail/stream-monitor-companion/aaaaibcmmahcedpcdfcbhnjfkgmcgcii

The desktop app must be running for the extension to do anything. It pulls your monitored streamer list from the local config server on port 52832.

### Updating

When a new version is available, you'll see "Update available" in the tray tooltip. Click "Check for Updates" in the menu to download the new installer. Your settings will be preserved during the update.

### Changing Settings

Right-click the Stream Monitor icon in your system tray and select "Settings" to:
- Add or remove streamers
- Update your Twitch credentials
- Change the check interval

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

## Twitch API Usage

This app uses the Twitch Helix API to check stream status. It:
- Uses Client Credentials flow (no user login required)
- Only calls the `/helix/streams` endpoint
- Makes ~1 API call per minute (well under rate limits)

## License

Released under the [MIT License](LICENSE). You're free to use, modify, and redistribute the code; see the LICENSE file for the full terms.
