# FTHR Clips

FTHR Clips records your screen in the background so you can clip the last 30 seconds whenever something good happens. Think Shadowplay, but it actually works on Linux. No cloud, no accounts, no bullshit — just a hotkey and your clips.

## Features

- Always-on background recording at 60fps, GPU-accelerated with NVENC (falls back to x264 on CPU)
- Instant replay: hit a hotkey, save the last N seconds as a clip
- Clip grid to browse everything you've saved
- Built-in clip viewer + light editor for trimming
- Audio mixing so you can balance desktop sound and mic
- Themes and a customization page to make it yours
- Runs on Linux (Wayland) and Windows 10+

## Download

Grab the latest build from the [Releases](../../releases) page:

- **Linux:** `FTHRClips-1.0.0-alpha-x86_64.AppImage`
- **Windows:** `FTHRClips_Setup.exe`

## Linux Setup

```bash
chmod +x FTHRClips-1.0.0-alpha-x86_64.AppImage
./FTHRClips-1.0.0-alpha-x86_64.AppImage
```

For global hotkeys to work, you need to be in the `input` group. One-time setup:

```bash
sudo usermod -aG input $USER
```

Then log out and back in (group changes don't take effect until you re-login).

Requires a Wayland compositor — Hyprland, KDE Plasma, or GNOME 45+.

## Windows Setup

Run `FTHRClips_Setup.exe` and click through the installer. That's it. Hotkeys work out of the box.

## Building from source

### Linux

You'll need: `cmake`, `gcc`, `ffmpeg`, `pulseaudio`, `python3`, `PyQt6` (and the usual dev headers). Then:

```bash
bash build_linux.sh
```

### Windows

Visual Studio 2022 and Python 3.11. See [`BUILD_WINDOWS.md`](BUILD_WINDOWS.md) for the full walkthrough.

## Known issues / alpha stuff

- Global hotkeys on Linux need you to be in the `input` group (see Linux Setup above).
- Window capture (recording a specific app instead of the whole screen) is Windows only for now. On Linux it always grabs the full desktop.
- The screenshot feature is a placeholder. It doesn't really do anything yet.
- This is an alpha. Things might break. If they do, [open an issue](../../issues) and we'll fix it.
