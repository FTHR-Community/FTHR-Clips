# Building FTHR Clips

A native Windows capture engine and a Python frontend. This project builds
for Windows 10 and later (64-bit).

- [Common setup](#common-setup) · [Windows](#windows)
- Current physical gaps and limits: [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)

---

## Common setup

### Python dependencies

Install from the pinned lock files, never a loose `pip install` — unpinned
installs were AUDIT-009 and made UI bug reports irreproducible:

```bash
python -m pip install -r requirements-alpha.txt -r requirements-dev.txt
```

`requirements.in` is the human-edited list of direct dependencies;
`requirements-alpha.txt` is the lock and the only file a release build installs
from.

> **Do not install `imageio-ffmpeg`.** It bundles a GPLv3 FFmpeg build. Having it
> in the environment risks PyInstaller pulling it into the bundle and placing the
> whole artifact under the GPL (AUDIT-005). It is excluded in `FTHR.spec` and
> absent from both lock files on purpose.

**Python 3.14 is the alpha build interpreter** — the shipped Windows bundle
contains `python314.dll` and `cpython-314` bytecode, so building on anything else
produces a different artifact from the one that was tested. CI also exercises
3.12, which is the supported development floor.

### Third-party binaries

The FFmpeg runtime (152 MB) and the MSVC redistributable are not tracked in git.
The FFmpeg *headers and import libraries* are, so the Windows engine compiles from
a clean clone — you only need the runtime to link, run and package:

```bash
python tools/fetch_third_party.py --all
```

Every FFmpeg file is verified against the sha256 in `tools/ffmpeg_manifest.json`.
A mismatch aborts: it means either a corrupt download or a different build from
the one the licence paperwork describes.

On Windows, the separately mutable Microsoft VC++ permalink is verified by a
valid Microsoft Authenticode signature and its exact SHA-256/version is
recorded locally before it can enter an installer.

### Verify before releasing — Windows

```bash
python tools/verify_release_licenses.py --tree .
python tools/verify_version_consistency.py
python tools/verify_shared_memory_contract.py
python tools/verify_engine_response_contract.py
python tools/verify_exception_handling.py
python tools/scan_repo_hygiene.py
python -m pytest tests/
python -m ruff check .
```

All must pass. `KNOWN_ISSUES.md` lists the physical checks that automation
cannot prove. AUDIT-013 selected PySide6/LGPLv3 and hash-locks every
release asset in `tools/release_asset_manifest.json`. Before packaging, also
verify that the committed generated media matches its reviewed source:

```bash
python tools/generate_release_assets.py --check
```

Regenerate those files only after intentionally reviewing a generator change:

```bash
python tools/generate_release_assets.py
```

---

## Windows

Produces a Windows-only installer bundling `FTHRcapture/` (DXGI engine). All
Only Windows files are packaged.

### Prerequisites

| Tool | Source |
|---|---|
| Visual Studio 2022, Desktop C++ workload | https://visualstudio.microsoft.com |
| Python 3.14 x64 | https://python.org/downloads |
| Inno Setup 6 | https://jrsoftware.org/isinfo.php |
| FFmpeg runtime + VC++ redistributable | `python tools/fetch_third_party.py --all` |

### 1. Build the capture engine

Open `FTHRcapture\FTHRcapture.sln`, set **Release | x64**, Build Solution.
Or from a Visual Studio Developer PowerShell/Command Prompt:

```powershell
msbuild FTHRcapture\FTHRcapture.sln `
    -p:Configuration=Release -p:Platform=x64
```

Output: `FTHRcapture\x64\Release\FTHRclips.exe`.

> The engine links against the FFmpeg DLLs in
> `FTHRclips\third_party\ffmpeg\bin\`. A post-build step copies them next to the
> exe. If you run the exe before rebuilding, copy `*.dll` from that folder into
> `x64\Release\` by hand.

### 2. Bundle with PyInstaller

```powershell
python tools/build_optional_uploaders.py
python -m PyInstaller FTHR.spec --clean
```

Output: `dist\FTHRClips\`. The spec generates the Windows VERSIONINFO resource
from `FTHR_UI/version.py`, so the `.exe` reports its version in Explorer →
Properties → Details.

The first command freezes and seals the optional uploader package, then binds
its archive hash into Core.
`FTHR.spec` refuses to build if the dormant package is missing. The normal
installer build entrypoint runs this command automatically.

Optional size reduction:

```powershell
$int = "dist\FTHRClips\_internal"
Remove-Item "$int\libopencv_dnn*","$int\libopencv_ml*","$int\libopencv_calib3d*", `
            "$int\libopencv_features2d*","$int\libopencv_stitching*", `
            "$int\Qt6Quick*","$int\Qt6Qml*","$int\Qt6Pdf*" -ErrorAction SilentlyContinue
```

### 3. Create the installer

Use the release build entrypoint:

```powershell
python tools/build_windows_installer.py
```

It verifies the Microsoft VC++ redistributable, the bundle licence manifest,
and the Windows lifecycle contract before invoking Inno Setup. Output:
`Output\FTHRClips-Setup-2.0.0-alpha-x64.exe` for the current version.

The frameless, app-style setup starts with a required Privacy Policy checkbox
linking to `https://policies.fthrclips.com`, then presents the application and
clip-library folders together on one screen. The optional desktop shortcut
remains unchecked by default and is not added to the compact UI. Stock Windows
welcome, directory, ready, and finish pages are hidden. The selected clip
directory is seeded into the current user's `.fthr/settings.json` without
removing an existing clip library.

For a publishable artifact, provide the operator-owned signing command and
require a valid signature; no certificate belongs in this repository:

```powershell
python tools/build_windows_installer.py `
  --sign-command '<your approved signing command containing {file}>' `
  --require-signed
```

> `installer_windows.iss` carries the product version as a literal because Inno
> Setup cannot import Python. `tools/verify_version_consistency.py` fails the
> build if it disagrees with `FTHR_UI/version.py`.

### What the installer contains

Windows DXGI capture engine · Python runtime and dependencies · Qt6 Widgets (no
Wayland/QML) · two dormant consent-gated optional upload packages · Microsoft
Visual C++ redistributable · Start Menu and optional desktop shortcut ·
versioned Inno uninstaller. Clips, screenshots, exports and
sidecars under `%USERPROFILE%\FTHR_Clips` are never uninstaller targets;
settings/cache in `%USERPROFILE%\.fthr` are retained unless the user explicitly
chooses their removal.

The installer lifecycle verifier checks update/repair policy, autostart
ownership, signing state, package identity, user-data preservation, and the
reviewed bundle boundary.

### Notes

- DXGI desktop capture requires Windows 10 or later.
- Global hotkeys work unprivileged on Windows.
- The selected monitor and encoder must belong to the same adapter. NVIDIA is
  physically qualified; AMD/Intel are code-ready but require their own hardware
  qualification. Hybrid/cross-adapter fallback is deliberately refused.
- Windows encoder backends currently own their preset choice; the alpha UI does
  not expose a P1–P7 selector that the engine would ignore.

---


The location screen includes an optional desktop shortcut checkbox, unchecked
by default. The installer uses a dark green surface, Oswald headings, and
three numbered steps; body copy and folder paths use Segoe UI.
