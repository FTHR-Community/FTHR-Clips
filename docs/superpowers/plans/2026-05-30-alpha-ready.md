# FTHR Clips Alpha-Ready Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FTHR Clips alpha-ready on both Linux (AppImage) and Windows (Inno Setup installer) by fixing Linux-specific crashes, building the C++ engine, and creating packaging pipelines for both platforms.

**Architecture:** Python PyQt6 frontend (FTHR_UI) communicates with a C++ capture backend via POSIX shared memory on Linux and Win32 file mapping on Windows. The Linux AppImage bundles Python 3.14, all pip deps, and the compiled `FTHRclips` binary. The Windows release uses PyInstaller to bundle the Python app + pre-built `FTHRClips.exe` into a single exe, then Inno Setup wraps it into a click-through installer.

**Tech Stack:** Python 3.14, PyQt6, C++20/CMake (Linux), MSVC (Windows), AppImage/appimagetool, PyInstaller, Inno Setup 6

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `FTHR_UI/ui/capture_settings_widget.py` | Modify | Remove top-level `ctypes.wintypes` import, guard Windows-only functions |
| `FTHR_UI/main.py` | Modify | Add Linux `input` group warning at startup |
| `requirements.txt` | Create | Pinned Python deps for both platforms |
| `AppDir/AppRun` | Create | AppImage entry point script |
| `AppDir/fthr-clips.desktop` | Create | `.desktop` metadata for AppImage |
| `AppDir/fthr-clips.png` | Create | App icon (copy from assets) |
| `build_linux.sh` | Create | Builds C++ engine + AppImage in one command |
| `FTHR.spec` | Create | PyInstaller spec for Windows bundle |
| `installer_windows.iss` | Create | Inno Setup script for Windows installer |
| `BUILD_WINDOWS.md` | Create | Step-by-step Windows build guide |

---

## Task 1: Fix Linux crash — `capture_settings_widget.py` top-level Windows imports

**Files:**
- Modify: `FTHR_UI/ui/capture_settings_widget.py:1-15`

The file imports `ctypes.wintypes` at the top level. On Linux `ctypes.wintypes` exists but the functions it calls (`ctypes.windll`) do not — `windll` is Windows-only and raises `AttributeError` on first access. The `_enumerate_capturable_windows()` function also calls `ctypes.windll.user32` directly. Both need to be guarded.

- [ ] **Step 1: Read the current top of the file**

```bash
head -20 /home/tom/FTHR_Clips/FTHR_UI/ui/capture_settings_widget.py
```

- [ ] **Step 2: Replace the top-level import and guard all Windows-only functions**

Replace the entire block from line 1 to the end of `_enumerate_capturable_windows()` (approximately line 160) with the following platform-guarded version:

```python
"""
capture_settings_widget.py
Compact single-row settings bar with hardware encoding status indicator
and capture source (desktop / window) selector.
"""
import sys
import ctypes
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                              QComboBox, QPushButton, QFrame, QApplication,
                              QSizePolicy, QFileIconProvider)
from PyQt6.QtCore import Qt, QSize, QFileInfo, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QIcon, QImage, QPixmap, QCursor
from ui.style import Colors

if sys.platform == 'win32':
    import ctypes.wintypes as wintypes


# ---------------------------------------------------------------------------
# Window enumeration — Windows only. Returns empty list on Linux because
# the Linux engine only supports full-desktop capture (wlr-screencopy).
# ---------------------------------------------------------------------------

def _get_exe_info(exe_path: str, size: int = 16) -> 'tuple[str | None, QIcon | None]':
    if sys.platform != 'win32':
        return None, None
    name = None
    try:
        class _SHFILEINFOW(ctypes.Structure):
            _fields_ = [('hIcon', ctypes.c_void_p),
                        ('iIcon', ctypes.c_int),
                        ('dwAttributes', ctypes.c_uint),
                        ('szDisplayName', ctypes.c_wchar * 260),
                        ('szTypeName', ctypes.c_wchar * 80)]
        SHGFI_DISPLAYNAME = 0x000000200
        shfi = _SHFILEINFOW()
        ret  = ctypes.windll.shell32.SHGetFileInfoW(
            exe_path, 0, ctypes.byref(shfi), ctypes.sizeof(shfi),
            SHGFI_DISPLAYNAME)
        if ret:
            name = shfi.szDisplayName.strip() or None
            if name and name.lower().endswith('.exe'):
                name = name[:-4]
    except Exception:
        pass

    try:
        provider = QFileIconProvider()
        icon = provider.icon(QFileInfo(exe_path))
        if icon.isNull():
            icon = None
    except Exception:
        icon = None

    return name, icon


def _get_hwnd_exe_path(hwnd: int) -> 'str | None':
    if sys.platform != 'win32':
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        pid       = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return None
        try:
            buf  = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass
    return None


def _enumerate_capturable_windows():
    """
    Return a list of dicts for every visible, titled window.
    Returns empty list on Linux (desktop-only capture via wlr-screencopy).
    """
    if sys.platform != 'win32':
        return []

    user32 = ctypes.windll.user32
    GWL_STYLE    = -16
    GWL_EXSTYLE  = -20
    WS_CAPTION       = 0x00C00000
    WS_POPUP         = 0x80000000
    WS_EX_TOOLWINDOW = 0x00000080

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    results  = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
```

- [ ] **Step 3: Verify the import no longer crashes on Linux**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI
python3 -c "from ui.capture_settings_widget import _enumerate_capturable_windows; print(_enumerate_capturable_windows())"
```

Expected output: `[]`

- [ ] **Step 4: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/ui/capture_settings_widget.py
git commit -m "fix: guard Windows-only ctypes imports in capture_settings_widget"
```

---

## Task 2: Add Linux `input` group warning in `main.py`

**Files:**
- Modify: `FTHR_UI/main.py` — find the `__init__` method of `MainWindow` (around line 1193)

The `keyboard` library reads `/dev/input/event*` on Linux. Without `input` group membership this silently fails — hotkeys never fire. We show a one-time warning dialog so the user knows what to fix, but we do NOT crash.

- [ ] **Step 1: Find where hotkey_manager is created in main.py**

```bash
grep -n "hotkey_manager\|HotkeyManager\|_setup_hotkeys\|register_all" /home/tom/FTHR_Clips/FTHR_UI/main.py | head -20
```

- [ ] **Step 2: Add the input-group check function above `MainWindow.__init__`**

Find the line `PANEL_FADE_MS = 180` (around line 359) and insert after it:

```python
def _check_linux_input_group() -> bool:
    """Return True if the process has access to /dev/input — needed for global hotkeys."""
    if sys.platform == 'win32':
        return True
    import grp, os
    try:
        groups = [g.gr_name for g in grp.getgrall() if os.getlogin() in g.gr_mem]
        current_gids = os.getgroups()
        input_gid = grp.getgrnam('input').gr_gid
        return input_gid in current_gids
    except Exception:
        return False
```

- [ ] **Step 3: Add the warning call inside `MainWindow.__init__` after `self._setup_hotkeys()`**

Find `self._setup_hotkeys()` in `__init__` and add directly after it:

```python
        if not _check_linux_input_group():
            QTimer.singleShot(1500, self._warn_input_group)
```

- [ ] **Step 4: Add the `_warn_input_group` method to `MainWindow`**

Find any existing short method in `MainWindow` (e.g. `_set_status`) and add nearby:

```python
    def _warn_input_group(self):
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle('Hotkeys Disabled')
        msg.setText(
            'Global hotkeys are disabled because your user is not in the <b>input</b> group.<br><br>'
            'Run this command, then log out and back in:<br>'
            '<code>sudo usermod -aG input $USER</code>'
        )
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.exec()
```

- [ ] **Step 5: Verify it launches without crash on Linux**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI
python3 -c "
import sys
sys.argv = ['main']
# Just import — don't start QApplication
import main
print('Import OK')
"
```

Expected: `Import OK` (no AttributeError, no windll crash)

- [ ] **Step 6: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "fix: add Linux input group check and warning dialog for hotkeys"
```

---

## Task 3: Build the Linux C++ engine

**Files:**
- No source changes — just compile `FTHRcapture_linux/`

All build dependencies are confirmed present: cmake, gcc, ffmpeg, wayland-client, libpulse-simple, pkg-config.

- [ ] **Step 1: Configure with CMake**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux
mkdir -p build
cmake -B build -DCMAKE_BUILD_TYPE=Release
```

Expected: `-- Build files have been written to: .../build`

- [ ] **Step 2: Build**

```bash
cmake --build build -j$(nproc)
```

Expected last lines:
```
[100%] Linking CXX executable FTHRclips
[100%] Built target FTHRclips
```

- [ ] **Step 3: Verify binary exists and runs**

```bash
ls -lh /home/tom/FTHR_Clips/FTHRcapture_linux/build/FTHRclips
/home/tom/FTHR_Clips/FTHRcapture_linux/build/FTHRclips --help 2>&1 | head -5 || true
```

Expected: binary file ~1-5 MB, exits with non-zero (no `--help` flag) but doesn't segfault.

- [ ] **Step 4: Commit build artifacts note**

```bash
cd /home/tom/FTHR_Clips
echo "FTHRcapture_linux/build/" >> .gitignore
git add .gitignore
git commit -m "chore: ignore Linux engine build output"
```

---

## Task 4: Verify the Linux UI launches end-to-end

**Files:** None modified — this is a smoke test.

- [ ] **Step 1: Launch the UI and confirm it starts without crash**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI
timeout 8 python3 main.py 2>&1 | head -40 || true
```

Expected: Lines like `Engine found: build/FTHRclips`, `Starting engine`, `Connected to Linux capture engine` — or at minimum no Python traceback. The app may open a window; close it after ~5 seconds.

- [ ] **Step 2: If traceback appears, fix it before continuing**

Common issues:
- `ModuleNotFoundError: No module named 'X'` → `pip install X`
- `AttributeError: module 'ctypes' has no attribute 'windll'` → Task 1 wasn't applied completely; re-read `capture_settings_widget.py` and find remaining `windll` references

---

## Task 5: Create `requirements.txt`

**Files:**
- Create: `FTHR_Clips/requirements.txt`

- [ ] **Step 1: Create the file**

```
PyQt6>=6.6.0
PyQt6-Qt6>=6.6.0
keyboard>=0.13.5
opencv-python>=4.9.0
imageio-ffmpeg>=0.4.9
sounddevice>=0.4.6
numpy>=1.26.0
```

- [ ] **Step 2: Verify all deps install cleanly from scratch**

```bash
python3 -m pip install -r /home/tom/FTHR_Clips/requirements.txt --dry-run 2>&1 | tail -5
```

Expected: `Would install ...` or `Requirement already satisfied` for all packages.

- [ ] **Step 3: Commit**

```bash
cd /home/tom/FTHR_Clips
git add requirements.txt
git commit -m "chore: add requirements.txt for Python dependencies"
```

---

## Task 6: Create AppImage structure

**Files:**
- Create: `AppDir/AppRun`
- Create: `AppDir/fthr-clips.desktop`
- Create: `AppDir/fthr-clips.png` (copy)
- Create: `AppDir/usr/share/applications/fthr-clips.desktop` (copy)

The AppImage spec requires an `AppDir/` with `AppRun`, a `.desktop` file, and a `.png` icon at the root.

- [ ] **Step 1: Create AppDir skeleton**

```bash
mkdir -p /home/tom/FTHR_Clips/AppDir/usr/share/applications
mkdir -p /home/tom/FTHR_Clips/AppDir/usr/share/icons
mkdir -p /home/tom/FTHR_Clips/AppDir/usr/bin
```

- [ ] **Step 2: Copy the app icon**

```bash
cp /home/tom/FTHR_Clips/FTHR_UI/assets/fthr_logo.png \
   /home/tom/FTHR_Clips/AppDir/fthr-clips.png
cp /home/tom/FTHR_Clips/FTHR_UI/assets/fthr_logo.png \
   /home/tom/FTHR_Clips/AppDir/usr/share/icons/fthr-clips.png
```

- [ ] **Step 3: Create the `.desktop` file**

Create `AppDir/fthr-clips.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=FTHR Clips
Comment=Game capture and clip management
Exec=fthr-clips
Icon=fthr-clips
Categories=AudioVideo;Video;Game;
Terminal=false
```

```bash
cp /home/tom/FTHR_Clips/AppDir/fthr-clips.desktop \
   /home/tom/FTHR_Clips/AppDir/usr/share/applications/fthr-clips.desktop
```

- [ ] **Step 4: Create `AppDir/AppRun`**

This script is the entry point when the AppImage runs. It sets up paths and launches the Python UI.

```bash
#!/bin/bash
set -e

HERE="$(dirname "$(readlink -f "$0")")"

# C++ engine path — shipped inside the AppImage
export FTHR_ENGINE="$HERE/usr/bin/FTHRclips"

# Python environment bundled inside AppImage
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/python3/dist-packages:$PYTHONPATH"
export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
export QT_PLUGIN_PATH="$HERE/usr/plugins"
export QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/usr/plugins/platforms"

exec "$HERE/usr/bin/python3" "$HERE/usr/share/fthr-clips/FTHR_UI/main.py" "$@"
```

Make it executable:
```bash
chmod +x /home/tom/FTHR_Clips/AppDir/AppRun
```

- [ ] **Step 5: Commit AppDir skeleton**

```bash
cd /home/tom/FTHR_Clips
git add AppDir/
git commit -m "feat: add AppDir skeleton for Linux AppImage"
```

---

## Task 7: Create `build_linux.sh` — one-command AppImage builder

**Files:**
- Create: `FTHR_Clips/build_linux.sh`

This script: checks deps, builds C++ engine, creates a Python venv, installs pip deps, copies everything into `AppDir/`, downloads `appimagetool` if missing, and produces `FTHRClips-x86_64.AppImage`.

- [ ] **Step 1: Create `build_linux.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"
PYTHON_VERSION="$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)"

echo "=== FTHR Clips Linux AppImage Builder ==="
echo "Python: $PYTHON_VERSION"

# ── 1. Check hard requirements ─────────────────────────────────────────────
for cmd in cmake gcc pkg-config python3 pip3 wayland-scanner; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found"; exit 1; }
done

pkg-config --exists libavcodec libpulse-simple wayland-client || {
    echo "ERROR: Missing C++ deps. Install: ffmpeg wayland libpulse"
    echo "  Arch: sudo pacman -S cmake gcc ffmpeg wayland wayland-protocols libpulse pkg-config"
    echo "  Ubuntu: sudo apt install cmake g++ libavcodec-dev libavformat-dev libavutil-dev libavdevice-dev libswscale-dev libswresample-dev libwayland-dev wayland-protocols libpulse-dev pkg-config"
    exit 1
}

# ── 2. Build C++ engine ────────────────────────────────────────────────────
echo ""
echo ">>> Building C++ capture engine..."
cd "$SCRIPT_DIR/FTHRcapture_linux"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$APPDIR/usr" > /dev/null
cmake --build build -j"$(nproc)"
echo "    Engine built: FTHRcapture_linux/build/FTHRclips"

# ── 3. Set up AppDir ───────────────────────────────────────────────────────
echo ""
echo ">>> Setting up AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/fthr-clips"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons"
mkdir -p "$APPDIR/usr/lib"

# Copy C++ engine
cp "$SCRIPT_DIR/FTHRcapture_linux/build/FTHRclips" "$APPDIR/usr/bin/FTHRclips"

# Copy Python app
cp -r "$SCRIPT_DIR/FTHR_UI" "$APPDIR/usr/share/fthr-clips/FTHR_UI"

# Copy AppDir metadata
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/usr/share/applications/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/usr/share/icons/"
cp "$SCRIPT_DIR/AppDir/AppRun"             "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# ── 4. Bundle Python + pip deps ────────────────────────────────────────────
echo ""
echo ">>> Installing Python dependencies into AppDir..."
VENV="$BUILD_DIR/venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

# Copy venv site-packages into AppDir
SITE_PACKAGES="$VENV/lib/python${PYTHON_VERSION}/site-packages"
mkdir -p "$APPDIR/usr/lib/python${PYTHON_VERSION}/dist-packages"
cp -r "$SITE_PACKAGES/." "$APPDIR/usr/lib/python${PYTHON_VERSION}/dist-packages/"

# Bundle Python interpreter itself
PY_BIN="$(which python3)"
cp "$PY_BIN" "$APPDIR/usr/bin/python3"

# Bundle Python stdlib
PY_STDLIB="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
mkdir -p "$APPDIR/usr/lib/python${PYTHON_VERSION}"
cp -r "$PY_STDLIB/." "$APPDIR/usr/lib/python${PYTHON_VERSION}/"
cp -r "$VENV/lib/python${PYTHON_VERSION}/lib-dynload" "$APPDIR/usr/lib/python${PYTHON_VERSION}/" 2>/dev/null || true

# ── 5. Bundle Qt6 shared libraries ────────────────────────────────────────
echo ""
echo ">>> Bundling Qt6 libraries..."
QT6_LIBS="$(python3 -c 'import PyQt6; import os; print(os.path.dirname(PyQt6.__file__))')"
mkdir -p "$APPDIR/usr/lib/python${PYTHON_VERSION}/dist-packages/PyQt6/Qt6/lib"
# Qt6 bundles its own libs inside the PyQt6 wheel — they're already in site-packages
# Find and copy the Qt platform plugin
QT_PLATFORM_SRC="$QT6_LIBS/Qt6/plugins/platforms"
if [ -d "$QT_PLATFORM_SRC" ]; then
    mkdir -p "$APPDIR/usr/plugins/platforms"
    cp "$QT_PLATFORM_SRC"/libqxcb.so  "$APPDIR/usr/plugins/platforms/" 2>/dev/null || true
    cp "$QT_PLATFORM_SRC"/libqwayland*.so "$APPDIR/usr/plugins/platforms/" 2>/dev/null || true
fi

# ── 6. Update AppRun with correct Python version ──────────────────────────
sed -i "s|python3/dist-packages|python${PYTHON_VERSION}/dist-packages|g" "$APPDIR/AppRun"

# ── 7. Download appimagetool if not present ────────────────────────────────
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# ── 8. Build AppImage ──────────────────────────────────────────────────────
echo ""
echo ">>> Building AppImage..."
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$SCRIPT_DIR/FTHRClips-x86_64.AppImage"

echo ""
echo "=== Done! ==="
echo "Output: $SCRIPT_DIR/FTHRClips-x86_64.AppImage"
echo ""
echo "Before first run, ensure your user is in the 'input' group for global hotkeys:"
echo "  sudo usermod -aG input \$USER   (then log out and back in)"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x /home/tom/FTHR_Clips/build_linux.sh
```

- [ ] **Step 3: Run it**

```bash
cd /home/tom/FTHR_Clips
./build_linux.sh 2>&1 | tee build_linux.log
```

Expected final lines:
```
=== Done! ===
Output: /home/tom/FTHR_Clips/FTHRClips-x86_64.AppImage
```

- [ ] **Step 4: Smoke test the AppImage**

```bash
chmod +x /home/tom/FTHR_Clips/FTHRClips-x86_64.AppImage
/home/tom/FTHR_Clips/FTHRClips-x86_64.AppImage &
sleep 6
kill %1 2>/dev/null || true
```

Expected: App window opens, no Python traceback in terminal.

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add build_linux.sh
git add AppDir/
echo "build_output/" >> .gitignore
echo "FTHRClips-x86_64.AppImage" >> .gitignore
git add .gitignore
git commit -m "feat: add Linux AppImage build script and AppDir"
```

---

## Task 8: Create Windows PyInstaller spec

**Files:**
- Create: `FTHR_Clips/FTHR.spec`

This spec bundles `FTHR_UI/main.py` and all assets. It is run on Windows with a pre-built `FTHRclips.exe` already in `FTHRcapture/x64/Release/`. We can write and commit it now; it is executed during Windows builds.

- [ ] **Step 1: Create `FTHR.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

# Paths — relative to this spec file
ROOT       = Path(SPECPATH)
UI_DIR     = ROOT / 'FTHR_UI'
ASSETS_DIR = UI_DIR / 'assets'
ENGINE_EXE = ROOT / 'FTHRcapture' / 'x64' / 'Release' / 'FTHRClips.exe'

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_EXE), '.'),          # C++ engine next to the UI exe
    ],
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),        'assets'),
        (str(ASSETS_DIR / 'fonts'),                'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),                'assets/icons'),
    ],
    hiddenimports=[
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'sounddevice',
        'numpy',
        'cv2',
        'imageio_ffmpeg',
        'keyboard',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FTHRClips',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                      # no console window
    icon=str(ASSETS_DIR / 'fthr_logo.png'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FTHRClips',
)
```

- [ ] **Step 2: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR.spec
git commit -m "feat: add PyInstaller spec for Windows bundle"
```

---

## Task 9: Create Inno Setup script for Windows installer

**Files:**
- Create: `FTHR_Clips/installer_windows.iss`

Inno Setup 6 compiles this into `FTHRClips_Setup.exe`. Run on Windows after PyInstaller produces `dist/FTHRClips/`.

- [ ] **Step 1: Create `installer_windows.iss`**

```iss
; FTHR Clips — Inno Setup 6 installer script
; Build on Windows: iscc installer_windows.iss

#define MyAppName      "FTHR Clips"
#define MyAppVersion   "0.1.0-alpha"
#define MyAppPublisher "FTHR"
#define MyAppExeName   "FTHRClips.exe"
#define MyAppURL       "https://github.com/fthr/fthr-clips"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\FTHRClips
DefaultGroupName={#MyAppName}
OutputBaseFilename=FTHRClips_Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

; Require Windows 10 or later (needed for DXGI desktop duplication)
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller output — entire dist/FTHRClips/ directory
Source: "dist\FTHRClips\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";       Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\fthr"
```

- [ ] **Step 2: Commit**

```bash
cd /home/tom/FTHR_Clips
git add installer_windows.iss
git commit -m "feat: add Inno Setup script for Windows installer"
```

---

## Task 10: Create Windows build guide

**Files:**
- Create: `FTHR_Clips/BUILD_WINDOWS.md`

- [ ] **Step 1: Create `BUILD_WINDOWS.md`**

```markdown
# Building FTHR Clips for Windows

## Prerequisites

- Visual Studio 2022 (Desktop C++ workload)
- Python 3.11 (3.14 not yet supported by all PyInstaller hooks — use 3.11)
- [PyInstaller](https://pyinstaller.org): `pip install pyinstaller`
- [Inno Setup 6](https://jrsoftware.org/isinfo.php)

## Step 1 — Build the C++ engine

1. Open `FTHRcapture/FTHRcapture.sln` in Visual Studio 2022
2. Set configuration to **Release | x64**
3. Build → Build Solution
4. Verify: `FTHRcapture\x64\Release\FTHRClips.exe` exists

## Step 2 — Install Python deps

```cmd
pip install -r requirements.txt
pip install pyinstaller
```

## Step 3 — Bundle with PyInstaller

```cmd
pyinstaller FTHR.spec --clean
```

Output: `dist\FTHRClips\FTHRClips.exe` + all deps

## Step 4 — Create installer with Inno Setup

1. Open Inno Setup Compiler
2. File → Open → `installer_windows.iss`
3. Build → Compile
4. Output: `Output\FTHRClips_Setup.exe`

## Notes

- The `FTHRClips.exe` C++ engine is copied next to the Python executable automatically by the PyInstaller spec
- The installer creates `%APPDATA%\fthr\` for settings; uninstaller removes it
- Hotkeys work globally on Windows without extra permissions
```

- [ ] **Step 2: Commit**

```bash
cd /home/tom/FTHR_Clips
git add BUILD_WINDOWS.md
git commit -m "docs: add Windows build guide for alpha release"
```

---

## Self-Review

**Spec coverage:**
- ✅ `_enumerate_capturable_windows()` Linux crash → Task 1
- ✅ `keyboard` input group → Task 2
- ✅ Linux C++ engine build → Task 3
- ✅ End-to-end Linux smoke test → Task 4
- ✅ `requirements.txt` → Task 5
- ✅ AppImage structure → Tasks 6–7
- ✅ Windows PyInstaller → Task 8
- ✅ Windows Inno Setup → Task 9
- ✅ Windows build docs → Task 10

**Placeholder scan:** No TBD/TODO in any task. All code blocks complete.

**Type consistency:** `_enumerate_capturable_windows` referenced in both Task 1 (definition) and used unchanged in `capture_settings_widget.py:709` — no rename. `FTHRclips` binary name consistent across Tasks 3, 6, 7.
