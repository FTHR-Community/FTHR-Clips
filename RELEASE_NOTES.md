# FTHR Clips 1.1.0-alpha

This is the first patched alpha release.

## What is in here

- Replay capture and transactional clip saving
- Windows WGC/DXGI capture with H.264, HEVC and AV1 paths
- Manual recording that keeps the active stream and writes a stable MP4 output
- System audio and microphone tracks with in-app playback
- Monitor screenshots, crop editing and background operation
- Persistent autostart and settings
- Overlay previews, webcam and click burn-ins
- Installer and Linux AppImage build definitions with release checks in place

## Changes since 1.0.0-alpha (Linux hotkeys)

- Global hotkeys now go through the XDG Desktop Portal
  (`org.freedesktop.portal.GlobalShortcuts`) on KDE Plasma 6, GNOME 48+ and
  other desktops with a portal backend. The desktop owns the key grab and lists
  FTHR's actions in its own shortcut settings.
- The `keyboard` library and its `/dev/input` access are gone. Membership in the
  `input` group is no longer required and the app can no longer observe
  keystrokes it did not register.
- The AppImage installs `fthr-clips.desktop` on first run and starts inside its
  own systemd scope so the portal can identify it. Desktops without a portal
  backend keep the Unix-socket fallback (Hyprland binds are still generated).
- Once the desktop knows a shortcut it keeps that key; changing it happens in the
  desktop's shortcut settings (Settings → Hotkeys → Edit in desktop settings).

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


