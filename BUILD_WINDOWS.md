# Building FTHR Clips for Windows

> **Platform note:** This build produces a Windows-only installer.
> It bundles `FTHRcapture\` (Windows DXGI engine) and excludes all Linux files.
> For Linux builds see `build_linux.sh`.

## Prerequisites

| Tool | Download |
|---|---|
| Visual Studio 2022 (Desktop C++ workload) | https://visualstudio.microsoft.com |
| Python 3.11 x64 | https://python.org/downloads |
| Inno Setup 6 | https://jrsoftware.org/isinfo.php |
| VC++ Redistributable | https://aka.ms/vs/17/release/vc_redist.x64.exe → `redist\vc_redist.x64.exe` |

> Use Python 3.11, not 3.14 — PyInstaller hooks are more stable on 3.11.

## Step 1 — Install Python dependencies

```cmd
pip install PyQt6 keyboard opencv-python-headless imageio-ffmpeg sounddevice numpy pyinstaller
```

## Step 2 — Build the Windows C++ capture engine

1. Open `FTHRcapture\FTHRcapture.sln` in Visual Studio 2022
2. Set configuration to **Release | x64**
3. **Build → Build Solution**
4. Confirm output: `FTHRcapture\x64\Release\FTHRClips.exe`

## Step 3 — Bundle with PyInstaller

```cmd
pyinstaller FTHR.spec --clean
```

Output: `dist\FTHRClips\` — Python app + Windows engine, ~80-120 MB.

**Post-build cleanup (run in PowerShell to further reduce size):**
```powershell
$int = "dist\FTHRClips\_internal"
# Remove unused OpenCV modules
Remove-Item "$int\libopencv_dnn*"    -ErrorAction SilentlyContinue
Remove-Item "$int\libopencv_ml*"     -ErrorAction SilentlyContinue
Remove-Item "$int\libopencv_calib3d*" -ErrorAction SilentlyContinue
Remove-Item "$int\libopencv_features2d*" -ErrorAction SilentlyContinue
Remove-Item "$int\libopencv_stitching*" -ErrorAction SilentlyContinue
# Remove Qt QML/Quick modules
Remove-Item "$int\Qt6Quick*"  -ErrorAction SilentlyContinue
Remove-Item "$int\Qt6Qml*"    -ErrorAction SilentlyContinue
Remove-Item "$int\Qt6Pdf*"    -ErrorAction SilentlyContinue
```

## Step 4 — Create the installer

1. Place `vc_redist.x64.exe` in `redist\`
2. Open **Inno Setup Compiler** → `installer_windows.iss` → **Build → Compile**
3. Output: `Output\FTHRClips_Setup.exe`

## What the installer contains

- Windows DXGI capture engine (`FTHRClips.exe`)
- Python runtime + all dependencies
- Qt6 Widgets (no Wayland/QML)
- Visual C++ Redistributable
- Start Menu + optional desktop shortcut
- Uninstaller (removes `%APPDATA%\fthr` on uninstall)

## Notes

- DXGI desktop capture requires Windows 10 or later
- Global hotkeys work without extra permissions on Windows
- NVENC auto-detected at runtime, falls back to CPU x264
- The Linux engine (`FTHRcapture_linux/`) is never included in the Windows build
