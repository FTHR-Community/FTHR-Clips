# FTHR Clips 1.1.3-alpha

Linux hardening release before the UI overhaul. Windows artifacts are unchanged apart from shared test and packaging fixes.

## Linux capture engine

- Capture survives a compositor stall. Recovery retries the backend that last
  delivered frames instead of re-running first-time selection (which could
  reach the ScreenCast portal dialog and end capture), waits out an
  unreachable compositor or a missing output every two seconds, and keeps the
  replay history when only the backend restarts. Reproduced and verified on
  Hyprland with a frozen compositor.
- Encoder failures are visible. A failing encoder ends the capture generation
  with a reason instead of reporting healthy frames into an empty buffer.
- Saving no longer races recovery; clips are muxed from a per-generation
  snapshot of the encoder parameters.
- ext-image-copy-capture keeps capturing on a static screen at the configured
  frame rate.
- Desktop audio attaches when PulseAudio/PipeWire-Pulse appears after startup;
  the audio buffer is sized to the replay window.
- Clips are fsynced before they are published; AAC setup failures fall back to
  video-only instead of failing the save.
- VA-API on older libva (Ubuntu 22.04) no longer aborts the engine: the
  `vaMapBuffer2` symbol is probed first (from #42 by @Victor7996).
- `FTHR_CAPTURE_BACKEND=wlr|ext|portal|x11` forces one backend for
  diagnostics.
- Known limitation: the encoder preset is not applied to VA-API encoders
  (driver semantics differ between radeonsi and Intel).

## Linux app

- Global hotkeys through the XDG GlobalShortcuts portal on desktops with a
  backend (KDE Plasma 6, GNOME 48+); the `keyboard` library and `/dev/input`
  access are gone, so the `input` group is no longer needed (from #16 by
  @NorkelL). Hyprland keeps the generated socket binds, which now cover every
  action and reload only when they change.
- Wayland screenshots on KDE Plasma and GNOME through the Screenshot portal;
  microphone selection by name; camera refresh fixes (#57; addresses #52 and
  #56, field validation welcome).
- The engine connection no longer races the status poll during startup.
- Compositor detection follows the session, not installed binaries (a machine
  with Plasma packages in a GNOME or Sway session is no longer treated as KDE).
- Uploader settings are written owner-only.
- Desktop identity and launcher categories fixed (from #19 by @Vaspyyy).

## Linux packaging and docs

- The AppImage ships the pinned LGPL `ffmpeg` and `ffprobe`, so exports and
  watermarking no longer use the distribution's GPL build.
- `BUILDING.md` and `docs/linux.md` describe the real build steps, the glibc
  2.35 baseline, backends and diagnostics; the licence gate's `--all` mode
  inspects the real AppDir; the third-party notices record the Linux FFmpeg
  build.

## Known limitations

- NVIDIA and Intel hardware encoding are unqualified on Linux.
- Window-specific capture is not available on Linux; the whole output is
  captured.

# Previous release: FTHR Clips 1.1.2-alpha

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

## Changes since 1.0.0-alpha (Linux hotkeys)

- Global hotkeys now go through the XDG Desktop Portal
  (`org.freedesktop.portal.GlobalShortcuts`) on KDE Plasma 6, GNOME 48+ and
  other desktops with a portal backend. The desktop owns the key grab and lists
  FTHR's actions in its own shortcut settings.
- The `keyboard` library and its `/dev/input` access are gone. Membership in the
  `input` group is no longer required and the app can no longer observe
  keystrokes it did not register.
- The AppImage installs `fthr-clips.desktop` on first run so the portal can
  identify it. Hyprland keeps the Unix-socket path with generated binds;
  other desktops without a portal backend keep the socket fallback.
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


