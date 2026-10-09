# FTHR Clips 1.1.2-alpha

This is the Linux compatibility rebuild of the alpha release. Linux packaging is
built on Ubuntu 22.04 and rejects bundled ELF objects requiring GLIBC newer than
2.35, preventing the GLIBC_2.44 launch failure in the previous AppImage.

This is the first patched alpha release.

This release includes a Linux audio/video synchronization fix. Desktop audio
capture now compensates for measured PulseAudio delivery latency and uses a
bounded recording buffer before extracting replay audio.

## Linux fixes in this build

- **Launches on stable distributions again.** Built on Ubuntu 22.04 with a
  GLIBC 2.35 ceiling over every bundled binary (fixes the `GLIBC_2.44` error
  on Fedora, Ubuntu and similar, #28).
- **numpy and Qt Multimedia load inside the AppImage.** Packaging left four
  stripped wheel libraries unloadable, which broke playback mixing, the clip
  editor, microphone recording and the webcam overlay (#53).
- **Capture survives resolution, scale and HDR changes** without corrupting
  frames or clearing the replay buffer. Compositors offering RGB-order or
  10-bit buffers, or y-inverted frames, now record with correct colours and
  orientation (#51).
- **Desktop audio reconnects** after the audio server drops and follows the
  default output when you switch to headphones or Bluetooth. Audio gaps are
  saved as silence, so sound stays in sync with video (#51).
- **The app no longer fails to connect** to a running engine when it polls
  during engine startup (#50).
- **Game detection toggle** no longer errors when changed at runtime, and
  reports when the tools it needs are missing (#49).
- A capture failure now reports its reason after a focus pause (#49).

Known Linux limitations: screenshots on KDE Plasma and GNOME Wayland (#52),
automatic hotkeys outside Hyprland (#16), and window/game-specific capture
(the whole output is captured).

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
- AMD AMF hardware encoding verified on Windows 11 (build 10.0.26200) with
  AMD Radeon RX 7800 XT (driver 32.0.31041.1004); the NV12 pipeline fix
  resolves vertical striping and corrupt frame playback. Intel path remains
  automated-tested but hardware-unverified.
- Local Windows artifacts remain unsigned.
- The Linux engine builds and all native CTests pass in WSL2. The AppImage also
  passes construction, licence, and extraction checks there, but native
  Wayland/X11 capture, audio, hotkeys, and multi-monitor behaviour remain
  physically unverified.


