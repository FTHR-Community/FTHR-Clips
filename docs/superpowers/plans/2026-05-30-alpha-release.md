# FTHR Clips 1.0.0-alpha Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 bugs that cause crashes or broken behavior on Linux, bump the version to `1.0.0-alpha`, and produce a working AppImage.

**Architecture:** Six targeted bug fixes across 3 Python files and 1 C++ file, followed by a version bump and a clean rebuild. No structural refactoring — each fix is surgical and self-contained.

**Tech Stack:** Python 3, PyQt6, subprocess, C++17, Wayland wlr-screencopy, CMake, AppImage

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/ui/clip_viewer.py` | Add `_NO_WINDOW` constant, replace 3 `creationflags` usages |
| `FTHR_UI/ui/capture_card.py` | Fix `_play_mp3` for Linux, fix sound file paths |
| `FTHR_UI/main.py` | Read `FTHR_ENGINE` env var, hide autostart checkbox on Linux, bump title |
| `FTHRcapture_linux/src/capture_engine.cpp` | Add `wl_output_listener`, wait for `done` before `capture_output` |
| `build_linux.sh` | Update output filename to `FTHRClips-1.0.0-alpha-x86_64.AppImage` |
| `installer_windows.iss` | Already has `1.0.0-alpha` — verify and leave |
| `FTHR.spec` | Add `assets/sounds` to datas |
| `FTHR_UI/assets/sounds/` | New directory — move 3 MP3 files here |

---

## Task 1: Fix `creationflags=subprocess.CREATE_NO_WINDOW` on Linux

**File:** `FTHR_UI/ui/clip_viewer.py`

`subprocess.CREATE_NO_WINDOW` is a Windows-only constant (value `0x08000000`). Lines 1098, 2264, and 2476 pass it unconditionally — on Linux this raises `AttributeError` and crashes clip export, audio detection, and GIF export.

The fix: define a module-level `_NO_WINDOW` dict (same pattern as `main.py:19`) and unpack it with `**_NO_WINDOW` in each `subprocess` call.

- [ ] **Step 1: Add `_NO_WINDOW` constant after the imports at the top of `clip_viewer.py`**

Current top of file (lines 1-5):
```python
# clip_viewer.py - FTHR clip editor
import os, threading, subprocess
import imageio_ffmpeg
from pathlib import Path
from datetime import datetime
```

Add `import sys` and the constant:
```python
# clip_viewer.py - FTHR clip editor
import os, sys, threading, subprocess
import imageio_ffmpeg
from pathlib import Path
from datetime import datetime

_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
```

- [ ] **Step 2: Fix line 1098 — Popen in export worker**

Find this block (around line 1095):
```python
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
```

Replace with:
```python
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **_NO_WINDOW,
            )
```

- [ ] **Step 3: Fix line 2264 — subprocess.run in audio track detection**

Find this block (around line 2261):
```python
                result = subprocess.run(
                    [ffmpeg, '-hide_banner', '-i', clip_path],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=4,
                )
```

Replace with:
```python
                result = subprocess.run(
                    [ffmpeg, '-hide_banner', '-i', clip_path],
                    capture_output=True, text=True,
                    timeout=4,
                    **_NO_WINDOW,
                )
```

- [ ] **Step 4: Fix line 2476 — subprocess.run in GIF/clip render**

Find this block (around line 2473):
```python
        try:
            subprocess.run(cmd, check=True, capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
```

Replace with:
```python
        try:
            subprocess.run(cmd, check=True, capture_output=True,
                           **_NO_WINDOW)
```

- [ ] **Step 5: Verify no remaining `CREATE_NO_WINDOW` in clip_viewer.py**

```bash
grep -n "CREATE_NO_WINDOW" /home/tom/FTHR_Clips/FTHR_UI/ui/clip_viewer.py
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/ui/clip_viewer.py
git commit -m "fix: guard subprocess.CREATE_NO_WINDOW behind win32 check in clip_viewer"
```

---

## Task 2: Fix Windows MCI sound and relocate sound files

**Files:** `FTHR_UI/ui/capture_card.py`, `FTHR_UI/assets/sounds/` (new), `FTHR.spec`

Two problems in one: (a) `_play_mp3` uses `ctypes.windll` which only exists on Windows, crashing on Linux; (b) sound files are at the repo root which is wrong inside an AppImage. Fix both together.

- [ ] **Step 1: Move the three MP3 files into `FTHR_UI/assets/sounds/`**

```bash
mkdir -p /home/tom/FTHR_Clips/FTHR_UI/assets/sounds
mv "/home/tom/FTHR_Clips/Clip Captured (2).mp3" /home/tom/FTHR_Clips/FTHR_UI/assets/sounds/clip_captured.mp3
mv "/home/tom/FTHR_Clips/screenshot saved (1).mp3" /home/tom/FTHR_Clips/FTHR_UI/assets/sounds/screenshot_saved.mp3
mv "/home/tom/FTHR_Clips/Error.mp3" /home/tom/FTHR_Clips/FTHR_UI/assets/sounds/error.mp3
```

- [ ] **Step 2: Add `sys` and `subprocess` imports at the top of `capture_card.py`**

Find the existing imports at the top:
```python
import ctypes
from pathlib import Path
```

Replace with:
```python
import ctypes
import sys
import subprocess as _subprocess
from pathlib import Path
```

- [ ] **Step 3: Replace the sound path constants and `_play_mp3` function in `capture_card.py`**

Find these lines (around line 40):
```python
# Sound files live at the repo root (FTHR_Clips/), two levels above this file.
_ROOT           = Path(__file__).parent.parent.parent
_SND_CLIP       = _ROOT / 'Clip Captured (2).mp3'
_SND_SCREENSHOT = _ROOT / 'screenshot saved (1).mp3'
_SND_ERROR      = _ROOT / 'Error.mp3'

_MCI_ALIAS = 'fthr_card'


# ---------------------------------------------------------------------------
# Sound helper
# ---------------------------------------------------------------------------

def _play_mp3(path: Path) -> None:
    """Fire-and-forget MP3 via Windows MCI. Silent on any failure."""
    if not path.exists():
        return
    try:
        mci = ctypes.windll.winmm.mciSendStringW
        mci(f'close {_MCI_ALIAS}', None, 0, None)
        mci(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}', None, 0, None)
        mci(f'play {_MCI_ALIAS}', None, 0, None)
    except Exception:
        pass
```

Replace with:
```python
# Sound files live in assets/sounds/ alongside the UI source.
_SND_DIR        = Path(__file__).parent.parent / 'assets' / 'sounds'
_SND_CLIP       = _SND_DIR / 'clip_captured.mp3'
_SND_SCREENSHOT = _SND_DIR / 'screenshot_saved.mp3'
_SND_ERROR      = _SND_DIR / 'error.mp3'

_MCI_ALIAS = 'fthr_card'


# ---------------------------------------------------------------------------
# Sound helper
# ---------------------------------------------------------------------------

def _play_mp3(path: Path) -> None:
    """Fire-and-forget MP3. Uses Windows MCI on Windows, ffplay on Linux."""
    if not path.exists():
        return
    if sys.platform == 'win32':
        try:
            mci = ctypes.windll.winmm.mciSendStringW
            mci(f'close {_MCI_ALIAS}', None, 0, None)
            mci(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}', None, 0, None)
            mci(f'play {_MCI_ALIAS}', None, 0, None)
        except Exception:
            pass
    else:
        try:
            _subprocess.Popen(
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', str(path)],
                stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # ffplay not installed — silent fallback
        except Exception:
            pass
```

- [ ] **Step 4: Remove the now-unused `import ctypes` if it's only used for sound**

Check whether `ctypes` is used anywhere else in `capture_card.py`:
```bash
grep -n "ctypes" /home/tom/FTHR_Clips/FTHR_UI/ui/capture_card.py
```

If the only remaining hit is the `import ctypes` line at the top, remove it. If it's used elsewhere, leave it.

- [ ] **Step 5: Update `FTHR.spec` to include `assets/sounds`**

Open `FTHR.spec` and find the `datas` list:
```python
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),   'assets'),
        (str(ASSETS_DIR / 'fonts'),            'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),            'assets/icons'),
    ],
```

Add the sounds entry:
```python
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),   'assets'),
        (str(ASSETS_DIR / 'fonts'),            'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),            'assets/icons'),
        (str(ASSETS_DIR / 'sounds'),           'assets/sounds'),
    ],
```

- [ ] **Step 6: Verify paths resolve correctly**

```bash
python3 -c "
from pathlib import Path
f = Path('/home/tom/FTHR_Clips/FTHR_UI/ui/capture_card.py')
snd = f.parent.parent / 'assets' / 'sounds'
print('sounds dir:', snd)
print('exists:', snd.exists())
print('files:', list(snd.iterdir()))
"
```

Expected output: path exists and lists 3 `.mp3` files.

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/assets/sounds/
git add FTHR_UI/ui/capture_card.py
git add FTHR.spec
git commit -m "fix: relocate sounds to assets/sounds, make _play_mp3 cross-platform"
```

---

## Task 3: Read `FTHR_ENGINE` env var for AppImage engine path

**File:** `FTHR_UI/main.py`

The `AppRun` script sets `FTHR_ENGINE=/path/to/FTHRclips` but `main.py` never reads it. The engine is always "not found" when running from the AppImage. Fix: check the env var first, before falling back to development paths.

- [ ] **Step 1: Find the engine path block in `main.py`** (around line 1180)

```python
        if sys.platform == 'win32':
            project_root   = Path(__file__).parent.parent / 'FTHRcapture'
            possible_paths = [
                project_root / 'x64' / 'Release' / 'FTHRClips.exe',
                project_root / 'x64' / 'Debug'   / 'FTHRClips.exe',
                project_root / 'Release'          / 'FTHRClips.exe',
                project_root / 'Debug'            / 'FTHRClips.exe',
            ]
        else:
            linux_root     = Path(__file__).parent.parent / 'FTHRcapture_linux'
            possible_paths = [
                linux_root / 'build' / 'FTHRclips',
            ]
```

- [ ] **Step 2: Add `FTHR_ENGINE` env var as first candidate on Linux**

Replace the `else` branch:
```python
        else:
            linux_root     = Path(__file__).parent.parent / 'FTHRcapture_linux'
            _env_engine    = os.environ.get('FTHR_ENGINE', '')
            possible_paths = [
                *([ Path(_env_engine) ] if _env_engine else []),
                linux_root / 'build' / 'FTHRclips',
            ]
```

Note: `os` is already imported in `main.py` (line 14: `import os`).

- [ ] **Step 3: Verify the fix works with the extracted AppImage**

```bash
FTHR_ENGINE=/tmp/squashfs-root/usr/bin/FTHRclips python3 -c "
import os, sys
from pathlib import Path
sys.path.insert(0, '/tmp/squashfs-root/usr/share/fthr-clips/FTHR_UI')
_env_engine = os.environ.get('FTHR_ENGINE', '')
linux_root  = Path('/tmp/squashfs-root/usr/share/fthr-clips/FTHR_UI').parent / 'FTHRcapture_linux'
possible_paths = [
    *([ Path(_env_engine) ] if _env_engine else []),
    linux_root / 'build' / 'FTHRclips',
]
for p in possible_paths:
    if p.exists():
        print('Engine found:', p)
        break
"
```

Expected: `Engine found: /tmp/squashfs-root/usr/bin/FTHRclips`

- [ ] **Step 4: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "fix: read FTHR_ENGINE env var for engine path in AppImage"
```

---

## Task 4: Hide "Autostart with Windows" checkbox on Linux

**File:** `FTHR_UI/main.py`

The autostart checkbox is shown on all platforms and has no Linux implementation.

- [ ] **Step 1: Find the autostart checkbox** (around line 2599)

```python
        # ── System ────────────────────────────────────────────────────────
        layout.addWidget(_flat_section_header('System'))
        layout.addSpacing(12)
        self.autostart_check = QCheckBox('Autostart with Windows')
        layout.addWidget(self.autostart_check)
```

- [ ] **Step 2: Wrap in a platform check**

Replace with:
```python
        # ── System ────────────────────────────────────────────────────────
        layout.addWidget(_flat_section_header('System'))
        layout.addSpacing(12)
        self.autostart_check = QCheckBox('Autostart with Windows')
        if sys.platform != 'win32':
            self.autostart_check.setVisible(False)
        layout.addWidget(self.autostart_check)
```

- [ ] **Step 3: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "fix: hide Windows-only autostart checkbox on Linux"
```

---

## Task 5: Fix `zwlr_screencopy` — wait for `wl_output.done` before capture

**File:** `FTHRcapture_linux/src/capture_engine.cpp`

**Root cause:** After `wl_registry_bind` for `wl_output`, Hyprland sends a sequence of `wl_output` events (`geometry`, `mode`, `scale`, etc.) followed by a `done` event to signal the output is fully configured. The engine calls `capture_output()` without waiting for `done` — Hyprland rejects the request with "invalid arguments" because the output object is not yet fully committed from the compositor's perspective.

**Fix:** Add a `wl_output_listener` that sets `output_done = true` on the `done` callback. After the two `wl_display_roundtrip` calls, dispatch until `output_done` is true before proceeding to `capture_output`.

- [ ] **Step 1: Add `output_done` flag to `WaylandCtx`**

Find the `WaylandCtx` struct (around line 35):
```cpp
struct WaylandCtx {
    // Globals
    wl_display*                  display   = nullptr;
    wl_registry*                 registry  = nullptr;
    wl_compositor*               compositor= nullptr;
    wl_shm*                      shm       = nullptr;
    wl_output*                   output    = nullptr;
    zwlr_screencopy_manager_v1*  sc_mgr    = nullptr;
    zxdg_output_manager_v1*      xdg_out_mgr = nullptr;
```

Add the flag after `output`:
```cpp
struct WaylandCtx {
    // Globals
    wl_display*                  display   = nullptr;
    wl_registry*                 registry  = nullptr;
    wl_compositor*               compositor= nullptr;
    wl_shm*                      shm       = nullptr;
    wl_output*                   output    = nullptr;
    bool                         output_done = false;
    zwlr_screencopy_manager_v1*  sc_mgr    = nullptr;
    zxdg_output_manager_v1*      xdg_out_mgr = nullptr;
```

- [ ] **Step 2: Add `wl_output_listener` callbacks and listener struct**

Add these functions and the listener struct **after** `kRegistryListener` (around line 98, before the screencopy frame listeners):

```cpp
// ---------------------------------------------------------------------------
// wl_output listener — we only need the 'done' event
// ---------------------------------------------------------------------------

static void output_geometry(void*, wl_output*, int32_t, int32_t, int32_t, int32_t,
                             int32_t, const char*, const char*, int32_t) {}
static void output_mode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
static void output_done(void* data, wl_output*) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    ctx->output_done = true;
}
static void output_scale(void*, wl_output*, int32_t) {}

static const wl_output_listener kOutputListener = {
    output_geometry,
    output_mode,
    output_done,
    output_scale,
};

```

- [ ] **Step 3: Register the listener after binding `wl_output` in `registry_global`**

Find this block in `registry_global` (around line 71):
```cpp
    } else if (strcmp(interface, wl_output_interface.name) == 0) {
        if (!ctx->output) {
            // Bind to first output
            ctx->output = static_cast<wl_output*>(
                wl_registry_bind(registry, name, &wl_output_interface,
                                 std::min(version, 3u)));
        }
    }
```

Replace with:
```cpp
    } else if (strcmp(interface, wl_output_interface.name) == 0) {
        if (!ctx->output) {
            ctx->output = static_cast<wl_output*>(
                wl_registry_bind(registry, name, &wl_output_interface,
                                 std::min(version, 3u)));
            wl_output_add_listener(ctx->output, &kOutputListener, ctx);
        }
    }
```

- [ ] **Step 4: Wait for `output_done` before calling `capture_output` in `CaptureLoop`**

Find this block in `CaptureLoop` (around line 240):
```cpp
    ctx.registry = wl_display_get_registry(ctx.display);
    wl_registry_add_listener(ctx.registry, &kRegistryListener, &ctx);
    wl_display_roundtrip(ctx.display);
    wl_display_roundtrip(ctx.display);

    if (!ctx.sc_mgr) {
```

Replace with:
```cpp
    ctx.registry = wl_display_get_registry(ctx.display);
    wl_registry_add_listener(ctx.registry, &kRegistryListener, &ctx);
    wl_display_roundtrip(ctx.display);
    wl_display_roundtrip(ctx.display);

    // Wait for wl_output to be fully committed before using it.
    // Hyprland sends a 'done' event after all output properties are sent.
    // Calling capture_output before 'done' yields "invalid arguments".
    while (ctx.output && !ctx.output_done)
        wl_display_dispatch(ctx.display);

    if (!ctx.sc_mgr) {
```

- [ ] **Step 5: Rebuild the C++ engine**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux
cmake --build build -j$(nproc) 2>&1 | tail -5
```

Expected: build succeeds, last lines show `[100%] Linking CXX executable FTHRclips` or similar with no errors.

- [ ] **Step 6: Test the engine directly**

```bash
WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 \
  /home/tom/FTHR_Clips/FTHRcapture_linux/build/FTHRclips 2>&1 &
sleep 3
kill %1
```

Expected output contains `[FTHR] Ready. Waiting for commands...` and **no** `zwlr_screencopy_manager_v1: error` line.

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/capture_engine.cpp
git commit -m "fix: wait for wl_output.done before capture_output to fix zwlr invalid arguments"
```

---

## Task 6: Bump version to 1.0.0-alpha

**Files:** `FTHR_UI/main.py`, `build_linux.sh`

- [ ] **Step 1: Update window title in `main.py`**

Find line 1214:
```python
        self.setWindowTitle('FTHR Clips')
```

Replace with:
```python
        self.setWindowTitle('FTHR Clips 1.0.0-alpha')
```

- [ ] **Step 2: Update app name in `main()` at bottom of `main.py`**

Find line 3477:
```python
    app.setApplicationName('FTHR Clips')
```

Replace with:
```python
    app.setApplicationName('FTHR Clips')
    app.setApplicationVersion('1.0.0-alpha')
```

- [ ] **Step 3: Update output filename in `build_linux.sh`**

Find this line near the bottom of `build_linux.sh`:
```bash
OUTPUT="$SCRIPT_DIR/FTHRClips-x86_64.AppImage"
```

Replace with:
```bash
OUTPUT="$SCRIPT_DIR/FTHRClips-1.0.0-alpha-x86_64.AppImage"
```

Also update the printf line in the success box:
```bash
printf "║  Output: %-40s║\n" "FTHRClips-x86_64.AppImage"
```
Replace with:
```bash
printf "║  Output: %-40s║\n" "FTHRClips-1.0.0-alpha-x86_64.AppImage"
```

- [ ] **Step 4: Verify `installer_windows.iss` already has the right version**

```bash
grep "MyAppVersion" /home/tom/FTHR_Clips/installer_windows.iss
```

Expected: `#define MyAppVersion   "1.0.0-alpha"` — already correct, no change needed.

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py build_linux.sh
git commit -m "chore: bump version to 1.0.0-alpha"
```

---

## Task 7: Build the Linux AppImage and smoke test

- [ ] **Step 1: Run the build script**

```bash
cd /home/tom/FTHR_Clips
bash build_linux.sh
```

Expected: ends with the `╔══ FTHR Clips AppImage Ready! ══╗` box and produces `FTHRClips-1.0.0-alpha-x86_64.AppImage`.

- [ ] **Step 2: Confirm the AppImage is executable and the right size**

```bash
ls -lh /home/tom/FTHR_Clips/FTHRClips-1.0.0-alpha-x86_64.AppImage
```

Expected: file exists, executable bit set, size ~700 KB–1 MB.

- [ ] **Step 3: Run the AppImage and verify no crash**

```bash
WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 \
  /home/tom/FTHR_Clips/FTHRClips-1.0.0-alpha-x86_64.AppImage &
sleep 5
pgrep -a python3 | grep main && echo "UI running OK"
```

Expected: `UI running OK`.

- [ ] **Step 4: Verify engine connects (no "Engine not found" in output)**

Run the AppImage and check stderr/stdout:

```bash
WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 \
  /home/tom/FTHR_Clips/FTHRClips-1.0.0-alpha-x86_64.AppImage > /tmp/fthr_test.log 2>&1 &
sleep 6
grep -E "Engine|Connected|error|Error" /tmp/fthr_test.log
pkill -f FTHRClips
```

Expected: lines containing `Engine found` and `Connected` — no `error` lines.

- [ ] **Step 5: Kill test processes and clean up**

```bash
pkill -f FTHRClips 2>/dev/null
pkill -f "main.py" 2>/dev/null
rm -f /tmp/fthr_test.log
echo "Clean."
```

- [ ] **Step 6: Final commit tag**

```bash
cd /home/tom/FTHR_Clips
git tag v1.0.0-alpha
echo "Tagged v1.0.0-alpha"
```

---

## Windows Build Checklist (manual — run after rebooting to Windows)

After completing Tasks 1–7 on Linux, reboot to Windows and follow these steps. The Python fixes (Tasks 1–4) are already committed and will be present.

- [ ] Open `FTHRcapture\FTHRcapture.sln` in Visual Studio 2022
- [ ] Set configuration to **Release | x64**, click **Build → Build Solution**
- [ ] Confirm `FTHRcapture\x64\Release\FTHRClips.exe` exists
- [ ] Run: `pip install -r requirements.txt pyinstaller`
- [ ] Run: `pyinstaller FTHR.spec --clean`
- [ ] Confirm `dist\FTHRClips\FTHRClips.exe` exists
- [ ] Open Inno Setup → `installer_windows.iss` → **Build → Compile**
- [ ] Confirm `Output\FTHRClips_Setup.exe` exists
- [ ] Quick smoke test: run `FTHRClips_Setup.exe`, install, launch, verify window opens

---

## GitHub Release Checklist

- [ ] Go to `https://github.com/<your-repo>/releases/new`
- [ ] Tag: `v1.0.0-alpha` (select the tag created in Task 7 Step 6)
- [ ] Title: `FTHR Clips 1.0.0-alpha`
- [ ] Mark as **Pre-release**
- [ ] Upload `FTHRClips-1.0.0-alpha-x86_64.AppImage` (from Linux build)
- [ ] Upload `FTHRClips_Setup.exe` (from Windows build)
- [ ] Add release notes:

```
## FTHR Clips 1.0.0-alpha

First public alpha release. Screen capture and clip replay for Linux (Wayland) and Windows 10+.

### Install

**Linux:** Download `FTHRClips-1.0.0-alpha-x86_64.AppImage`, make executable, run:
\`\`\`
chmod +x FTHRClips-1.0.0-alpha-x86_64.AppImage
./FTHRClips-1.0.0-alpha-x86_64.AppImage
\`\`\`

For global hotkeys (one-time setup):
\`\`\`
sudo usermod -aG input $USER
\`\`\`
Then log out and back in.

**Windows:** Run `FTHRClips_Setup.exe` and follow the installer.

### Known Issues
- Global hotkeys on Linux require the `input` group (see above)
- Window capture source is Windows-only; Linux captures the full desktop
- Screenshot feature shows "Coming Soon"
```
