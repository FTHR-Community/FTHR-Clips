# FTHR Clips 1.0.0-alpha

First public alpha. Screen capture and instant replay for Linux (Wayland) and Windows 10+.

## Install

- **Linux:** download `FTHRClips-1.0.0-alpha-x86_64.AppImage`, then `chmod +x` it and run.
- **Windows:** download `FTHRClips_Setup.exe` and run the installer.

### Linux hotkey setup

Global hotkeys need you to be in the `input` group. Run this once, then log out and back in:

```bash
sudo usermod -aG input $USER
```

## What's working

- Background recording (NVENC GPU, x264 CPU fallback)
- Clip saving via hotkey
- Clip viewer + editor
- Audio mixing (desktop + mic)
- Themes and the customization page

## Known issues

- Global hotkeys on Linux require the `input` group (see setup above).
- Window capture (recording a specific app) is Windows only for now — Linux always grabs the full desktop.
- The screenshot feature is a placeholder.

---

This is alpha software. Expect rough edges. If something breaks, open an issue.
