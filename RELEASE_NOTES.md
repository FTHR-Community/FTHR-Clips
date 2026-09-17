# FTHR Clips 1.1.0-alpha

This is the first patched alpha release.

## What is in here

- Replay capture and transactional clip saving
- Windows WGC/DXGI capture with H.264, HEVC and AV1 paths
- AMD AMF hardware encoder NV12 pipeline fix eliminating video striping
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
- AMD AMF hardware encoding verified on Windows 11 (build 10.0.26200) with
  AMD Radeon RX 7800 XT (driver 32.0.31041.1004); the NV12 pipeline fix
  resolves vertical striping and corrupt frame playback. Intel path remains
  automated-tested but hardware-unverified.
- Local Windows artifacts remain unsigned.
- The Linux engine builds and all native CTests pass in WSL2. The AppImage also
  passes construction, licence, and extraction checks there, but native
  Wayland/X11 capture, audio, hotkeys, and multi-monitor behaviour remain
  physically unverified.


