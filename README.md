<div align="center">

# FTHR Clips

**Instant replay for Linux and Windows. Hit a hotkey, save the last 30 seconds.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)](https://github.com/FTHR-Community/FTHR-Clips/releases)
[![Release](https://img.shields.io/github/v/release/FTHR-Community/FTHR-Clips?include_prereleases&label=latest)](https://github.com/FTHR-Community/FTHR-Clips/releases)
[![CI](https://github.com/FTHR-Community/FTHR-Clips/actions/workflows/ci.yml/badge.svg)](https://github.com/FTHR-Community/FTHR-Clips/actions/workflows/ci.yml)

*Think ShadowPlay — but open source, no cloud, and it actually works on Linux.*

[Download](#download) · [Linux Setup](#linux-setup) · [Windows Setup](#windows-setup) · [Build from Source](#build-from-source) · [Report a Bug](https://github.com/FTHR-Community/FTHR-Clips/issues/new?template=bug_report.yml)

</div>

---

## What it does

FTHR Clips records your screen in the background at all times. When something clip-worthy happens, you hit a hotkey and it saves the last N seconds as a clip. No upload required, no account, no subscription.

It runs as a tray icon. You forget it's there until you need it.

---

## Features

| Category | Feature |
|----------|---------|
| **Capture** | GPU-accelerated recording via NVENC / AMF / QSV (CPU x264 fallback) |
| **Capture** | Configurable buffer: 15 s – 5 min, up to 1080p 60fps |
| **Capture** | H.264, HEVC, AV1 codec selection with P1–P7 quality presets |
| **Capture** | Monitor selection and scaling modes |
| **Hotkeys** | Global hotkeys via Hyprland binds (Linux) or system hooks (Windows) |
| **Hotkeys** | Save clip · Extended clip · Start/stop · Dismiss notification |
| **Audio** | Per-category audio mixing: Game / Discord / Browser / Music / Other |
| **Audio** | Mic overlay with independent volume |
| **Audio** | Multiband audio capture with per-app routing |
| **Post-processing** | Watermark overlay |
| **Post-processing** | Auto-crop (removes black bars) |
| **Post-processing** | Webcam overlay (picture-in-picture) |
| **Game detection** | Auto-detects game window, prompts to switch capture focus |
| **Anti-cheat** | Pauses recording automatically when game loses focus |
| **Settings** | Presets — save/load/delete full configuration snapshots |
| **Clip browser** | Thumbnail grid with trim editor and export |
| **Upload** | Optional background upload to your own endpoint |
| **Installer** | One-click installer on Windows, AppImage on Linux |

---

## Download

Grab the latest build from the [Releases](https://github.com/FTHR-Community/FTHR-Clips/releases) page.

| Platform | File |
|----------|------|
| Linux (Wayland) | `FTHRClips-1.0.0-alpha-x86_64.AppImage` |
| Windows 10/11 | `FTHRClips_Setup.exe` |

---

## Linux Setup

```bash
chmod +x FTHRClips-1.0.0-alpha-x86_64.AppImage
./FTHRClips-1.0.0-alpha-x86_64.AppImage
```

**Global hotkeys** require the `input` group. One-time setup — log out and back in after:

```bash
sudo usermod -aG input $USER
```

**Requirements:**
- Wayland compositor: Hyprland, KDE Plasma 6, or GNOME 45+
- `wlr-screencopy` protocol support
- PulseAudio (for multiband audio)
- NVIDIA GPU recommended (NVENC). AMD (AMF) and Intel (QSV) also supported. CPU fallback always available.

**Hotkeys (default):**

| Key | Action |
|-----|--------|
| `F9` | Save clip (last 30 s) |
| `F10` | Save extended clip (configurable length) |
| `F11` | Start / stop capture |
| `F8` | Dismiss notification |

---

## Windows Setup

Run `FTHRClips_Setup.exe` and click through the installer. Global hotkeys work out of the box.

**Requirements:** Windows 10 version 1903+ or Windows 11.

---

## Build from Source

### Linux

Dependencies: `cmake`, `gcc`, `ffmpeg`, `libpulse`, `wayland-protocols`, `python3 >= 3.11`, `PyQt6`

```bash
git clone https://github.com/FTHR-Community/FTHR-Clips.git
cd FTHR-Clips
bash build_linux.sh
```

The AppImage lands in `build_output/`.

### Windows

Visual Studio 2022 + Python 3.11. See [`BUILD_WINDOWS.md`](BUILD_WINDOWS.md) for the full walkthrough.

---

## Architecture

```
┌─────────────────────┐     Shared Memory (v3)     ┌────────────────────────┐
│   FTHR_UI (Python)  │ ◄─────────────────────────► │  FTHRcapture (C++)     │
│   PyQt6 frontend    │                              │  wlr-screencopy engine │
│   Settings / Upload │     Unix Socket (hotkeys)    │  FFmpeg encoder        │
│   Clip browser      │ ◄────────────────────────    │  PulseAudio multi-cap  │
└─────────────────────┘                              └────────────────────────┘
```

The C++ capture engine runs as a separate process and communicates with the Python UI via shared memory. Hotkeys are received by the UI via a Unix socket and forwarded as commands through shared memory.

---

## Contributing

Issues and PRs are welcome. Please open an issue first for significant changes so we can discuss the approach.

- Run tests: `QT_QPA_PLATFORM=offscreen pytest tests/`
- Code style: standard Python (no formatter enforced yet)
- C++ style: match the existing code

---

## License

[MIT](LICENSE) — do whatever you want with it.
