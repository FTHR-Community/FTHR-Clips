# FTHR Clips 2.0.0-alpha

## 16 October 2026

- Add hardware-accelerated clip exports with Very fast, High quality and Lossless choices for export and sharing.
- Keep exports running while browsing clips, with optional simultaneous exports and a progress window.
- Fix the Discord share window opening behind the editor.
- Match export window controls to the app and correct the editor's maximize button.
- Simplify the interface performance descriptions.

## 13 October 2026

- Add interface performance presets and individual controls with estimated impact labels, including a lightweight clip viewer.
- Support trusted SSH known_hosts entries for SFTP when the fingerprint field is blank, and remember keys after successful fingerprint verification.
- Rebuild the Upload Extension as version 1.3.1 with the updated SFTP verification.

This alpha release includes the current editor performance and SFTP updates.

## What is in here

- Replay capture and transactional clip saving
- Windows WGC/DXGI capture with H.264, HEVC and AV1 paths
- Manual recording that keeps the active stream and writes a stable MP4 output
- System audio and microphone tracks with in-app playback
- Monitor screenshots, crop editing and background operation
- Persistent autostart and settings
- Overlay previews, webcam and click burn-ins
- Installer and Linux AppImage build definitions with release checks in place

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
