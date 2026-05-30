# FTHR Clips 1.0.0-alpha Release Design

**Date:** 2026-05-30  
**Version:** 1.0.0-alpha  
**Platforms:** Linux (AppImage x86_64) + Windows 10+ (Inno Setup installer)  
**Distribution:** GitHub Releases, Discord server

---

## Goal

Fix all known bugs that cause crashes or broken behavior on Linux and Windows, bump the version to `1.0.0-alpha`, and produce two release artifacts ready for public distribution.

---

## Bug Fixes

### Bug 1 — `creationflags=subprocess.CREATE_NO_WINDOW` crashes on Linux
**File:** `FTHR_UI/ui/clip_viewer.py` (lines 1098, 2264, 2476)  
**Problem:** `subprocess.CREATE_NO_WINDOW` is a Windows-only constant. Passing it on Linux raises `AttributeError` and crashes clip export and audio detection.  
**Fix:** Replace all three occurrences with the `_NO_WINDOW` dict pattern already defined in `main.py:19`:
```python
_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
```
Import or replicate this constant in `clip_viewer.py` and use `**_NO_WINDOW` as kwargs.

---

### Bug 2 — Windows MCI sound crashes on Linux
**File:** `FTHR_UI/ui/capture_card.py`  
**Problem:** `_play_mp3()` calls `ctypes.windll.winmm.mciSendStringW` which only exists on Windows. On Linux this raises `AttributeError` and crashes the notification card.  
**Fix:** Wrap with a platform check. On Linux, attempt playback via `subprocess` with `ffplay -nodisp -autoexit` (silent fallback if not available). If neither works, skip silently.

---

### Bug 3 — `FTHR_ENGINE` env var never read — engine not found in AppImage
**File:** `FTHR_UI/main.py` (~line 1189)  
**Problem:** `AppRun` exports `FTHR_ENGINE=/path/to/FTHRclips` but `main.py` ignores this env var and only searches hardcoded development paths. The engine is never found when running from the AppImage.  
**Fix:** Add `os.environ.get('FTHR_ENGINE')` as the first candidate in `possible_paths` on Linux, before the development path fallbacks.

---

### Bug 4 — "Autostart with Windows" checkbox shown on Linux
**File:** `FTHR_UI/main.py` (~line 2600)  
**Problem:** The autostart checkbox is always visible regardless of platform, and has no Linux implementation.  
**Fix:** Wrap the checkbox creation and its layout row in `if sys.platform == 'win32':`. On Linux, hide the widget entirely.

---

### Bug 5 — Sound file path wrong inside AppImage
**File:** `FTHR_UI/ui/capture_card.py`  
**Problem:** `_ROOT = Path(__file__).parent.parent.parent` resolves correctly in development (points to repo root where the MP3s live) but is wrong when running from the AppImage (the MP3s are not included).  
**Fix:**
1. Move `Clip Captured (2).mp3`, `screenshot saved (1).mp3`, and `Error.mp3` into `FTHR_UI/assets/sounds/`.
2. Update `_ROOT`/path references in `capture_card.py` to `Path(__file__).parent / 'assets' / 'sounds'`.
3. Update `build_linux.sh` — the `cp -r FTHR_UI` line already copies all assets, so no extra copy needed once files are moved.
4. Update `FTHR.spec` (Windows PyInstaller) to include `assets/sounds` in `datas`.

---

### Bug 6 — `zwlr_screencopy_manager_v1: invalid arguments` — capture fails on Wayland
**File:** `FTHRcapture_linux/src/capture_engine.cpp`  
**Problem:** The engine binds to `wl_output` via the registry and immediately calls `zwlr_screencopy_manager_v1_capture_output()` without waiting for the output to be fully initialized. The `wl_output` object emits a `done` event when all its properties are committed — calling `capture_output` before this event results in "invalid arguments" from the compositor (Hyprland).  
**Fix:**
1. Add a `wl_output_listener` with a `done` callback that sets a flag `output_done = true`.
2. After binding `wl_output` in the registry handler, add the listener.
3. Before calling `capture_output`, dispatch until `output_done` is true.

---

## Versioning

Update `1.0.0-alpha` in these three places:

| File | Field |
|------|-------|
| `installer_windows.iss` | `#define MyAppVersion "1.0.0-alpha"` (already set) |
| `build_linux.sh` | Output filename → `FTHRClips-1.0.0-alpha-x86_64.AppImage` |
| `FTHR_UI/main.py` | Window title → `FTHR Clips 1.0.0-alpha` |

---

## Build Process

### Linux (on this machine)
1. Apply all bug fixes
2. Rebuild C++ engine: `cd FTHRcapture_linux && cmake --build build -j$(nproc)`
3. Run: `bash build_linux.sh`
4. Output: `FTHRClips-1.0.0-alpha-x86_64.AppImage`

### Windows (after reboot)
1. Open `FTHRcapture/FTHRcapture.sln` in Visual Studio 2022 → Release x64 → Build
2. `pip install -r requirements.txt pyinstaller`
3. `pyinstaller FTHR.spec --clean`
4. Open `installer_windows.iss` in Inno Setup → Compile
5. Output: `Output/FTHRClips_Setup.exe`

---

## GitHub Release

- **Tag:** `v1.0.0-alpha`
- **Title:** `FTHR Clips 1.0.0-alpha`
- **Assets:**
  - `FTHRClips-1.0.0-alpha-x86_64.AppImage`
  - `FTHRClips_Setup.exe`
- **Release Notes:** Installation instructions for both platforms, note about `sudo usermod -aG input $USER` for Linux hotkeys, mark as pre-release.

---

## Known Issues (to document in Release Notes)

- Global hotkeys on Linux require the user to be in the `input` group (`sudo usermod -aG input $USER`, then re-login)
- Window capture source (selecting a specific window) is Windows-only; Linux always captures the full desktop
- Screenshot feature is not yet implemented (shows "Coming Soon" dialog)
