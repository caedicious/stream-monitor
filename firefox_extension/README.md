# Stream Monitor Tab Closer - Firefox Extension

Automatically closes Twitch tabs when the streamer raids someone else.

## How It Works

This extension works together with the Stream Monitor desktop app:

1. Stream Monitor opens a Twitch tab when your streamer goes live
2. This extension tracks that tab
3. If the URL changes to a different streamer (e.g., during a raid), the tab is automatically closed

## Installation

### Option 1: Temporary (for testing)
1. Open Firefox
2. Go to `about:debugging`
3. Click "This Firefox"
4. Click "Load Temporary Add-on"
5. Navigate to the `firefox_extension` folder and select `manifest.json`

**Note:** Temporary extensions are removed when Firefox restarts.

### Option 2: Permanent (self-signed)
1. Go to `about:config` in Firefox
2. Set `xpinstall.signatures.required` to `false`
3. Go to `about:addons`
4. Click the gear icon → "Install Add-on From File"
5. Select `stream_monitor_tab_closer.xpi`

### Option 3: Publish to Firefox Add-ons (for distribution)
Submit the extension to [addons.mozilla.org](https://addons.mozilla.org) for review and signing.

## Requirements

- Stream Monitor desktop app must be running
- The extension connects to `localhost:52832` to get the list of monitored streamers

## Permissions

- `tabs` - Required to monitor and close tabs
- `http://127.0.0.1/*` - Required to communicate with Stream Monitor app

## Troubleshooting

**Extension not closing tabs:**
- Make sure Stream Monitor desktop app is running
- Check the Browser Console (Ctrl+Shift+J) for "[Stream Monitor]" messages
- Verify the streamer is in your monitored list

**"Could not connect to Stream Monitor app" message:**
- This is normal if Stream Monitor isn't running
- Start the Stream Monitor app first
