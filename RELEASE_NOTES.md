# FTHR Clips 1.1.1-alpha

This is the first patched alpha release.

This release includes a Linux audio/video synchronization fix. Desktop audio
capture now compensates for measured PulseAudio delivery latency and uses a
bounded recording buffer before extracting replay audio.

## What is in here

- Replay capture and transactional clip saving
- Windows WGC/DXGI capture with H.264, HEVC and AV1 paths
- Manual recording that keeps the active stream and writes a stable MP4 output
- System audio and microphone tracks with in-app playback
- Monitor screenshots, crop editing and background operation
- Persistent autostart and settings
- Overlay previews, webcam and click burn-ins
- Installer and Linux AppImage build definitions with release checks in place

## Changes since 1.0.0-alpha (Linux ScreenCast portal capture)

- Compositors that advertise neither `wlr-screencopy` nor
  `ext-image-copy-capture` — KDE Plasma/KWin in particular — are now captured
  through the `org.freedesktop.portal.ScreenCast` portal and PipeWire. The
  desktop's screen picker appears once; the restore token is kept in
  `~/.fthr/portal_screencast_token` so later starts are silent.
- `libpipewire-0.3` and `libdbus-1` are loaded at runtime only when that path
  is used. Systems without them keep the existing backends.
- When no capture path works, or the screen picker is declined, the app's
  CAPTURE FAILED message now carries the engine's reason instead of a generic
  text, and a declined picker is not retried.

## Current status

This build is ready for public testing.

- NVIDIA H.264/HEVC/AV1 stall fixes are integrated and covered by automated
  lifecycle tests. A complete physical Windows qualification after those fixes
  has **not** run yet.
- HEVC editor playback and audible microphone content also require a new
  physical run; earlier failures are not evidence that the current source is
  fixed or still broken.
- AMD and Intel paths are automated-tested but hardware-unverified.
- Local Windows artifacts remain unsigned.
- The Linux engine builds and all native CTests pass in WSL2. The AppImage also
  passes construction, licence, and extraction checks there, but native
  Wayland/X11 capture, audio, hotkeys, and multi-monitor behaviour remain
  physically unverified.


