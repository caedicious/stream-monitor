# Stream Monitor

[![Release](https://img.shields.io/github/v/release/caedicious/stream-monitor?include_prereleases)](https://github.com/caedicious/stream-monitor/releases/)
[![Downloads](https://img.shields.io/github/downloads/caedicious/stream-monitor/total)](https://github.com/caedicious/stream-monitor/releases)
[![License](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-blue)

A Windows application that monitors Twitch streamers...

A Windows application that monitors Twitch streamers...

A Windows application that monitors Twitch streamers and automatically opens their stream in your browser when they go live.

## Features

- **System Tray App**: Runs quietly in the background
- **Auto-Start**: Launches when you log into Windows
- **Easy Setup**: Guided wizard walks you through configuration
- **Unlimited Streamers**: Monitor as many streamers as you want
- **Smart Detection**: Only opens browser when streamer goes from offline → live (no spam)
- **Settings GUI**: Right-click tray icon to change streamers anytime
- **Auto-Update Check**: Notifies you when a new version is available
- **Raid Detection** (Firefox Extension): Automatically closes tabs when streamer raids someone else

## For Users

Download the installer from the Releases page and run it. The setup wizard will guide you through:

1. Choosing which streamers to monitor
2. Creating a free Twitch Developer application
3. Entering your API credentials

After setup, Stream Monitor runs in your system tray and automatically starts when you log in.

### Firefox Extension (Optional)

If you want tabs to auto-close when a streamer raids someone else:

1. Download `stream_monitor_tab_closer.xpi` from the Releases page
2. Open Firefox → `about:addons`
3. Click gear icon → "Install Add-on From File"
4. Select the downloaded .xpi file

The extension communicates with the desktop app to know which streamers you're monitoring.

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

### Project Structure

```
stream_monitor_app/
├── stream_monitor_tray.py   # Main tray application
├── setup_wizard.py          # Setup/configuration wizard
├── requirements.txt         # Python dependencies
├── create_icon.py           # Icon generator
├── build.bat                # Build script
├── installer.iss            # Inno Setup installer script
└── README.md                # This file
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

MIT License - feel free to modify and distribute.
