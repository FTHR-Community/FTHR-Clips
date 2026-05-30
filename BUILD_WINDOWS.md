# Building FTHR Clips for Windows

## Prerequisites

| Tool | Download |
|---|---|
| Visual Studio 2022 (Desktop C++ workload) | https://visualstudio.microsoft.com |
| Python 3.11 x64 | https://python.org/downloads |
| Inno Setup 6 | https://jrsoftware.org/isinfo.php |

> **Note:** Use Python 3.11, not 3.14 — PyInstaller's hooks are more stable on 3.11.

## Step 1 — Install Python dependencies

```cmd
pip install -r requirements.txt
pip install pyinstaller
```

## Step 2 — Build the C++ capture engine

1. Open `FTHRcapture\FTHRcapture.sln` in Visual Studio 2022
2. Set configuration to **Release | x64**
3. **Build → Build Solution**
4. Confirm the output exists:
   ```
   FTHRcapture\x64\Release\FTHRClips.exe
   ```

## Step 3 — Bundle with PyInstaller

```cmd
pyinstaller FTHR.spec --clean
```

This produces `dist\FTHRClips\FTHRClips.exe` plus all dependencies.

## Step 4 — Create the installer

1. Open **Inno Setup Compiler**
2. **File → Open** → select `installer_windows.iss`
3. **Build → Compile**
4. Output: `Output\FTHRClips_Setup.exe`

## What the installer does

- Installs to `C:\Program Files\FTHRClips\` (or user-chosen path)
- Creates Start Menu shortcuts
- Optional desktop shortcut
- Adds an uninstaller (removes `%APPDATA%\fthr` on uninstall)

## Notes

- The C++ engine (`FTHRClips.exe`) is automatically placed next to the Python app by the PyInstaller spec — no manual copying needed
- DXGI desktop capture requires Windows 10 or later (enforced by the installer)
- Global hotkeys work without extra permissions on Windows
- NVENC (GPU encoding) is auto-detected at runtime; falls back to CPU x264 if unavailable
