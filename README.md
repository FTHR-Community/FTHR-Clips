<div align="center">

# FTHR Clips

**Instant Replay, Recordings and Screenshots with easy sharing and editing tools built in, right on your Windows or Linux PC—local first.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20experimental-lightgrey)](KNOWN_ISSUES.md)
[![Release](https://img.shields.io/github/v/release/FTHR-Community/FTHR-Clips?include_prereleases&label=latest)](https://github.com/FTHR-Community/FTHR-Clips/releases)
[![CI](https://github.com/FTHR-Community/FTHR-Clips/actions/workflows/ci.yml/badge.svg)](https://github.com/FTHR-Community/FTHR-Clips/actions/workflows/ci.yml)

*A local-first replay recorder: no account, no subscription, no required cloud.*

[Download](#download) · [Linux Setup](#linux-setup) · [Full Linux Guide](docs/linux.md) · [Windows Setup](#windows-setup) · [Build from Source](#build-from-source) · [Report a Bug](https://github.com/FTHR-Community/FTHR-Clips/issues/new?template=bug_report.yml)

</div>

---

## What it does

FTHRClips runs silently in the background, capturing your game; when you press a hotkey, it saves the clip. Unlike many other applications, FTHRClips is designed to protect your privacy and does not depend on a network connection.

---

## Features

| Category | Feature |
|----------|---------|
| **Capture** | GPU-accelerated recording via NVENC / AMF / QSV (CPU OpenH264 fallback) |
| **Capture** | Crash-resilient manual recordings written directly as fragmented MP4 with live AAC; no stop-time re-encode or whole-file remux |
| **Capture** | Configurable replay history up to 5 minutes and up to 240 FPS; Low, Medium, High, or custom bitrate |
| **Capture** | H.264, HEVC and AV1 selection; Windows uses backend-defined presets |
| **Capture** | Monitor selection and scaling modes |
| **Hotkeys** | Global hotkeys via Hyprland binds (Linux) or system hooks (Windows); Windows supports controller chords |
| **Hotkeys** | Save clip · Start/stop · Dismiss notification |
| **Audio** | System-output and microphone tracks; Windows 11 per-app stems are code-ready but still require hardware qualification.|
| **Post-processing** | Watermark overlay |
| **Post-processing** | Webcam overlay (picture-in-picture) |
| **Post-processing** | Windows third-party keyboard window overlay with live chroma-key color picking and intensity control |
| **Game detection** | Auto-detects game window, prompts to switch capture focus |
| **Settings** | Presets — save/load/delete full configuration snapshots |
| **Clip browser** | Thumbnail grid, linked-folder protection, trim editor and transactional export |
| **Upload** | Optional, consent-installed Catbox or Lustful uploader; Lustful hardware identity is a second install |
| **Packaging** | Windows installer and experimental Linux AppImage build paths |

---

## Download

You can download a ready-made installer or AppImage from our downloads page at
[downloads.fthrclips.com](https://downloads.fthrclips.com), or compile the code
from this repository.


## Linux Setup

The supported Linux distribution path is the **x86_64 AppImage on a native
Wayland desktop**. The merged Linux branch has been exercised on Arch Linux
with Hyprland, PipeWire/PulseAudio, Wayland capture, and an AMD Radeon RX 7900
XTX. Other distributions, compositors, GPUs, and X11 sessions may work, but
should be treated as unqualified until tested on that hardware.

### Install and launch the AppImage

Download the current Linux pre-release from the [GitHub release page](https://github.com/FTHR-Community/FTHR-Clips/releases/tag/v1.1.1-alpha), or use the direct links below:

- [FTHRClips-1.1.1-alpha-x86_64.AppImage](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.1-alpha/FTHRClips-1.1.1-alpha-x86_64.AppImage)
- [SHA-256 checksum](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.1-alpha/FTHRClips-1.1.1-alpha-x86_64.AppImage.sha256)

Then verify and run:

```bash
sha256sum -c FTHRClips-1.1.1-alpha-x86_64.AppImage.sha256
chmod +x FTHRClips-1.1.1-alpha-x86_64.AppImage
./FTHRClips-1.1.1-alpha-x86_64.AppImage
```

If FUSE is unavailable, use the portable fallback:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./FTHRClips-<version>-x86_64.AppImage
```

The current AppImage includes the optional Linux uploader bundle. Uploading is
still disabled until you enable it and accept the relevant consent and provider
terms in the application. Catbox and custom-server uploads do not require the
separate hardware-identity component. Lustful hardware-bound uploading is not
available on Linux.

### Requirements

- x86_64 Linux with a native graphical session
- Wayland compositor exposing `wlr-screencopy` or
  `ext-image-copy-capture`, or an `org.freedesktop.portal.ScreenCast`
  backend (`xdg-desktop-portal-kde` on KDE Plasma/KWin) together with
  PipeWire and D-Bus
- PipeWire with PulseAudio compatibility, or PulseAudio
- `grim` for screenshots (the ScreenCast portal path covers video only) and
  `nc`/netcat for compositor hotkey commands
- A working FFmpeg runtime supplied by the AppImage
- AMD VA-API users should install the Mesa VA-API driver and ensure the user
  can access `/dev/dri/renderD*`; software encoding remains the fallback

Arch example:

```bash
sudo pacman -S wayland wayland-protocols pipewire pipewire-pulse \
  grim openbsd-netcat libva-mesa-driver mesa-utils
```

Debian/Ubuntu package names vary by release. Install the equivalent Wayland,
PipeWire/PulseAudio, `grim`, netcat, and Mesa VA-API packages.

### Wayland capture and audio

On Wayland, FTHR first tries `wlr-screencopy` and then
`ext-image-copy-capture`, using only protocols the compositor advertises. When
neither is offered (KWin exports capture only through the desktop portal), it
falls back to the `org.freedesktop.portal.ScreenCast` portal and reads the
frames from PipeWire. The desktop shows its own screen picker the first time;
FTHR stores the portal's restore token in `~/.fthr/portal_screencast_token`
so later starts are silent, and the picker rather than the app's monitor
setting decides which screen is captured. Declining the picker stops the
engine with that reason in the app instead of re-opening the dialog. That path
needs `xdg-desktop-portal` with a ScreenCast backend, PipeWire, and D-Bus
installed; none is bundled. It captures the selected desktop output rather than
promising arbitrary per-window capture. PipeWire's PulseAudio compatibility
layer supplies the default output sink's monitor source for desktop audio.
If the audio service cannot be opened, video capture can continue without audio.

### AMD hardware encoding

On AMD Linux systems, `auto` prefers VA-API codecs when available:

- H.264: `h264_vaapi`
- HEVC: `hevc_vaapi`
- AV1: `av1_vaapi`

The default render device is `/dev/dri/renderD128`. Select another render node
when necessary:

```bash
FTHR_VAAPI_DEVICE=/dev/dri/renderD129 ./FTHRClips-<version>-x86_64.AppImage
```

If VA-API cannot initialize, FTHR falls back to software encoding. A successful
software fallback proves recording works, but does not prove VA-API is
available. NVIDIA NVENC and Intel hardware paths are not part of the current
Linux qualification claim.

### Hotkeys

The default bindings are:

- **F9**: save clip
- **F12**: save screenshot

FTHR uses a private owner-only Unix socket under the user runtime directory.
Hyprland bindings are generated by the app. Traditional `hyprland.conf`
sessions use the generated `fthr-hotkeys.conf` source; Lua-based Hyprland
sessions use the managed FTHR block in the loaded Lua keybind configuration.
Only the FTHR-owned block is replaced, so unrelated user configuration is
preserved. Reload Hyprland after changing bindings if the compositor does not
pick them up automatically.

KDE, GNOME, and other compositors do not automatically import Hyprland binds.
Use the exact command shown in **Settings → Hotkeys** and configure that
compositor's global shortcut facility. Root access and membership in the
`input` group are not required.

### Where clips are saved

The default library is:

```text
~/FTHR_Clips/
├── <game-or-window-name>/   # saved replay clips
├── Desktop/                 # desktop captures
└── Recordings/              # manual recordings
```

You can change the library and recording directories in the application
settings. User settings are kept separately under `~/.fthr/` so updating the
AppImage does not remove them.

### Troubleshooting and diagnostics

Generate a redacted support report without `sudo`:

```bash
bash tools/linux_system_report.sh > linux-system-report.txt
bash tools/linux_appimage_report.sh ./FTHRClips-<version>-x86_64.AppImage \
  > appimage-report.txt
```

Useful checks:

```bash
pactl info
vainfo
ls -l /dev/dri/renderD*
hyprctl monitors
hyprctl binds | grep -i fthr
```

- **AppImage will not start:** try `APPIMAGE_EXTRACT_AND_RUN=1` and check that
  the file is executable.
- **No video:** confirm `WAYLAND_DISPLAY`, the compositor capture protocol,
  and the selected output. Do not run the graphical test from a headless SSH
  shell. On KDE the log should show `[Backend] Using ScreenCast portal`; if
  it reports the portal as unavailable, check that `xdg-desktop-portal` and
  `xdg-desktop-portal-kde` are installed and PipeWire is running.
- **No desktop audio:** confirm PipeWire/PulseAudio is running and inspect
  `pactl info`; FTHR uses the output sink monitor, not the microphone source.
- **VA-API unavailable:** run `vainfo`, check the Mesa driver and render-node
  permissions, then set `FTHR_VAAPI_DEVICE` if the device is not `renderD128`.
- **Hotkeys do nothing:** confirm the generated configuration is loaded and
  inspect `hyprctl binds`; restart or reload Hyprland after changing bindings.
- **Uploads unavailable:** enable the uploader in settings and complete consent;
  Lustful remains unavailable on Linux by design.

For source builds and native test commands, see [`BUILDING.md`](BUILDING.md).
For known product issues, see [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).


---

## Windows Setup

Run a locally qualified `FTHRClips-Setup-<version>-x64.exe` and follow the setup steps. FTHR Clips
installs for all users under Program Files and subsequent downloads of the same
product update or repair that installation rather than creating a second entry.
The uninstaller keeps clips, screenshots and exports. It keeps FTHR settings by
default as well; removing settings/cache is an explicit choice. Global hotkeys
work out of the box.

**Requirements:** Windows 10 version 1903+ or Windows 11. Native NVIDIA
H.264/HEVC/AV1 on a same-adapter display is the intended alpha cohort. Stall
mitigations are code-integrated and automated-tested, but the complete physical
qualification must be repeated after those fixes. AMD and Intel paths remain
hardware-unverified; hybrid or cross-adapter capture is not supported.

---

## Build from Source

### Linux

Dependencies: `cmake`, `gcc`, `ffmpeg`, `libpulse`, `wayland-protocols`, `python3 >= 3.11`, `PySide6`

> FTHR uses the FFmpeg bundled next to the capture engine, falling back to
> `ffmpeg` on your `PATH`. It no longer uses `imageio-ffmpeg`, whose bundled
> binary is a GPL build.

```bash
git clone https://github.com/FTHR-Community/FTHR-Clips.git
cd FTHR-Clips
bash build_linux.sh
```

The AppImage lands in `build_output/`.

### Windows

Visual Studio 2022 + Python 3.14. See [`BUILDING.md`](BUILDING.md) for the full walkthrough.

---

## Architecture

```
┌─────────────────────┐     Shared Memory (v4)       ┌────────────────────────┐
│   FTHR_UI (Python)  │ ◄─────────────────────────►  │  Native capture engine │
│   PySide6 frontend  │                              │  Windows: WGC + GPU    │
│   Consent / Queue   │                              │  Linux: Wayland/X11    │
│   Clip browser      │                              │  Platform audio input  │
└─────────────────────┘                              └────────────────────────┘
          │ JSON subprocess IPC
          ▼
┌─────────────────────┐       Lustful only       ┌────────────────────────┐
│ Optional Uploader   │ ───────────────────────► │ Optional Hardware ID   │
│ Catbox / Lustful    │   derived UUID only      │ local machine-ID read  │
└─────────────────────┘                          └────────────────────────┘
```

The native capture engine runs as a separate process and communicates with the
Python UI via shared memory. Windows uses WGC/DXGI capture with the selected
NVENC, AMF, or QSV encoder and WASAPI audio. Linux uses supported Wayland
capture protocols or native X11/RandR with FFmpeg and PulseAudio-compatible
audio. Platform hotkey activation is
forwarded by the UI through the same shared-memory command contract; Linux also
uses an owner-only Unix socket for compositor key bindings.

the uploader is separate from the main app and doesn't work unless deliberately installed. 
Even after it installing it only connects when you allow it to.
please note that all clips uploaded to third party services fall under their terms and privacy policy:
`THIRD_PARTY_NOTICES.md`.

---

## Contributing

Issues and PRs are welcome. Please open an issue first for significant changes so we can discuss the approach.

- Run tests: `QT_QPA_PLATFORM=offscreen pytest tests/`
- Code style: standard Python (no formatter enforced yet)
- C++ style: match the existing code

AI-Generated Contributions

The use of generative AI when contributing to this repository is generally permitted with the following exceptions:

any AI-assisted contribution that affect any security-critical systems, privacy, data handling, encryption, authentication, update mechanisms, must be reviewed by a competent human contributor, gary does not count.

All creative work that is part of your contribution, including icons, illustrations, audio, music, and sound effects, must be fully created by human artists. 
AI is a tool to assist, not a replacement for human creativity.

---

## License

**FTHR Clips' own source code is licensed under the
[GNU General Public License v3](LICENSE) (`GPL-3.0-only`).** You may use,
study, modify, and redistribute it under those terms.

Copyright © 2026 FTHR Software. The program comes without warranty; see the
complete terms in [`LICENSE`](LICENSE).

Downloadable builds contain the GPLv3-licensed FTHR application together with
third-party components that retain their own licences:

| Component | Licence | Effect on the download |
|---|---|---|
| FFmpeg (dynamically linked) | LGPLv3 | Remains under LGPLv3; notices and source availability are required. |
| PySide6 / Shiboken (separate extension modules) | LGPLv3 option selected | Remains under LGPLv3 with replacement and relinking rights. |
| Qt 6 (separate shared libraries) | LGPLv3 option selected | Remains under LGPLv3 with replacement and relinking rights. |

The build uses PySide6 6.11.1 as its sole Qt binding and includes the LGPL text,
notices, exact source locations, and separately replaceable libraries. Bundled
media is either deterministically generated by the project under MIT or is the
hash-verified Oswald 4.103 font under OFL-1.1; the asset allowlist is
`tools/release_asset_manifest.json`.

Full details and every bundled component: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
Licence texts: [`licenses/`](licenses/), also installed alongside the app.

---

## Documentation

| Document | What it is for |
|---|---|
| [`BUILDING.md`](BUILDING.md) | Building the app and both engines, on Windows and Linux |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Tree layout, entry points, the shared-memory contract, conventions |
| [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) | What is broken, unverified or missing — read before filing a bug |
| [Recording reliability changes](docs/recording-reliability-2026-09-16.md) | Manual recording, faster finalization, focus recovery, and validation results |
| [`SECURITY.md`](SECURITY.md) | Reporting a security issue |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | Every bundled component and its licence |


## Transparency
Note about AI usage

AI tools were used to review and edit parts of the code. (also the comments cuz I'm lazy)

NONE of the assets used on both app and website were created with AI.

All changes made by AI were reviewed/corrected by human contributors.
