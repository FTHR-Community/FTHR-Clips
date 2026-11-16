# Building FTHR Clips

This branch contains the Windows native engine and Python frontend.

- [Windows setup](#windows-setup) · [Windows](#windows)
- Current physical gaps and limits: [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)
- Website update manifest and publishing steps: [`docs/app-updates.md`](docs/app-updates.md)

---

## Windows setup

### Python dependencies

Install from the pinned lock files, never a loose `pip install` — unpinned
installs were AUDIT-009 and made UI bug reports irreproducible:

```bash
python -m pip install -r requirements-alpha.txt -r requirements-dev.txt
```

To build the optional uploader or run its SFTP integration tests, also install
`python -m pip install -r requirements-uploader.txt`. These SSH/keyring
dependencies remain outside the Core runtime lock. On Windows, run
`python tools/build_optional_uploaders.py` before the full test suite so the
frozen uploader test exercises the real sealed package.

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
3.12, which is the supported floor.

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
Linux files are excluded.

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

If a running development app holds the default output DLLs open, build the
solution with an explicit absolute `/p:OutDir=...` directory. Set
`FTHR_NATIVE_BUILD_DIR` to that same directory before running PyInstaller or
the installer build entrypoint. The spec requires both `FTHRclips.exe` and
`FTHRPlaybackMixer.dll` there and packages them together.

> The engine links against the FFmpeg DLLs in
> `FTHRclips\third_party\ffmpeg\bin\`. A post-build step copies them next to the
> exe. If you run the exe before rebuilding, copy `*.dll` from that folder into
> `x64\Release\` by hand.

### Optional Windows GPU and window qualification

The AMD production/array/NV12 runner is documented in
[`docs/windows-p0-amd-retest-2026-10-17.md`](docs/windows-p0-amd-retest-2026-10-17.md).
It requires actual AMD hardware and reports that requirement explicitly on
other adapters.

For a controlled, video-only WGC window lifecycle test:

```powershell
python tools/run_windows_window_capture_smoke.py --encoder amd `
    --output "$env:TEMP\FTHR-Window-AMD-20261018-01"
```

Use a new external output directory for every run. The tool builds against the
production engine, opens its own animated window, checks resize/reconfiguration,
minimize/restore, saves three clips, and checks rejection after source closure.
It strictly decodes the saved files and records the actual durations in
`result.json`. A prebuilt helper can be supplied with `--helper`. Select
`--encoder nvidia` explicitly for NVIDIA regression testing; those results do
not qualify AMD. These checks do not measure audio sync or game performance.

### 2. Bundle with PyInstaller

```powershell
python tools/build_optional_uploaders.py
python -m PyInstaller FTHR.spec --clean
```

Output: `dist\FTHRClips\`. The spec generates the Windows VERSIONINFO resource
from `FTHR_UI/version.py`, so the `.exe` reports its version in Explorer →
Properties → Details.

Run `powershell -ExecutionPolicy Bypass -File tools/update_latest_build_shortcut.ps1`
to create **FTHR Clips - Latest Build** on the Desktop and in the Start menu.
It opens this workspace's packaged output at its stable path, including later
rebuilds. Quit an already-running installed or development instance before
opening it; the app permits only one instance per user session.

The first command independently freezes and seals the optional uploader
package, then binds its archive hash into Core.
`FTHR.spec` refuses to build if the dormant package is missing. The normal
installer build entrypoint runs this command automatically.

Optional size reduction:

```powershell
$int = "dist\FTHRClips\_internal"
Remove-Item "$int\libopencv_dnn*","$int\libopencv_ml*","$int\libopencv_calib3d*", `
            "$int\libopencv_features2d*","$int\libopencv_stitching*", `
            "$int\Qt6Pdf*" -ErrorAction SilentlyContinue
```

Do not remove Qt Quick, QML, Quick Widgets or the `ui/qml` directory: the
video preview uses them even though most of the interface uses Qt Widgets.

### 3. Create the installer

Use the release build entrypoint:

```powershell
python tools/build_windows_installer.py
```

It verifies the Microsoft VC++ redistributable, the bundle licence manifest,
and the Windows lifecycle contract before invoking Inno Setup. Output:
`Output\FTHRClips-Setup-2.1.2-x64.exe` for the current version.

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
Uninstall automatically removes the current user's FTHR settings, logs,
diagnostics, caches and installed FTHR feature/uploader data. The installed
`uninstall_cleanup.ps1` helper first requests the app's normal full shutdown,
including tray-only sessions, and waits for its processes to exit. Shutdown or
cleanup failure stops uninstall before application files are removed so it can
be retried. Interactive and silent uninstall use the same cleanup policy.

Cleanup is bounded to app-owned locations and known temporary-file patterns.
Configured media locations, clips, recordings, screenshots, exports and media
sidecars are preserved. Directory links are never followed into other folders.
The shared Visual C++ runtime and separately managed third-party applications
are not uninstalled.

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
