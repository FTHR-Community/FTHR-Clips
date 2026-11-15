
## Contribute a plugin

Want to submit your own plugin? See [CONTRIBUTING.md](CONTRIBUTING.md) for the folder scheme, required details and pull request process.

## Install

Download the individual ZIP using GitHub's **Download raw file** button. In FTHR Clips 2.1.0, open **Settings → Plugins → Choose ZIP…**, select it, and choose **Install plugin** or **Update plugin**. Keep it enabled. Do not extract the ZIP or install this entire folder as one plugin. Ultra Performance Mode pauses feature plugins.

### Stream Deck

Install the FTHR ZIP, then open `stream-deck/com.fthr.clips.streamDeckPlugin` in Elgato Stream Deck. Both apps must run under the same Windows account. Requires Windows 10+ and Stream Deck 7.1+. Keep FTHR running, then add its replay, recording, highlight, screenshot, or status actions to your keys. No connection settings are required.

The companion is version 1.0.0.0. Its current official SDK dependency has an unresolved high-severity [Zod memory-exhaustion advisory](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5). This remains a testable first release, not a fully qualified public release. No incompatible dependency override has been applied.

### Border-Free Capture

Uses DXGI to capture the entire selected monitor without the Windows Graphics Capture yellow border. Other visible windows and notifications are recorded too. The cursor is included; window/game targeting and cropping are unavailable while enabled. Changes apply to new capture sessions. A DXGI failure stops capture instead of switching back to WGC. Saved capture preferences are preserved.

### RNNoise

After installing, enable **RNNoise microphone noise reduction** in Audio / Microphone settings. Processing starts off. It reduces speech background noise on identified microphone tracks during clip preview and export; original recordings remain unchanged. Combined game-and-microphone tracks cannot be isolated. Reopen the clip after changing plugin settings. The model and its license are included in the plugin.

### Gary Mode

Open **Settings → Audio** to enable and configure Gary Mode below microphone monitoring. Adjust MIN/MAX on the live-level meter. Existing Gary preferences are reused, so installation does not necessarily turn the effect on. Enable audio capture for its desktop response. Disabling the plugin removes its controls and overlay while retaining preferences.

## Christmas theme 

make everything jolly :D

## Verification

i tested it, works on my machine. If it doesn't work for you it's user error.
