# Building FTHR Clips

Two native engines and one Python frontend. The Windows and Linux builds are
independent — neither artifact contains the other's engine.

- [Common setup](#common-setup) · [Windows](#windows) · [Linux](#linux)
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
3.12, which is the floor, and is what the Linux verification ran on.

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

### Verify before releasing — either platform

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

The first command independently freezes and seals the optional uploader and
Lustful Hardware Identity packages, then binds both archive hashes into Core.
`FTHR.spec` refuses to build if either dormant package is missing. The normal
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
`Output\FTHRClips-Setup-1.1.0-alpha-x64.exe` for the current version.

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

## Linux

### 1. System packages

pip cannot supply these.

```bash
# Debian / Ubuntu
sudo apt install build-essential cmake pkg-config \
  libavcodec-dev libavfilter-dev libavformat-dev libavutil-dev libavdevice-dev \
  libswscale-dev libswresample-dev \
  libwayland-dev wayland-protocols libwayland-bin \
  libpulse-dev libportaudio2 \
  libegl1 libxcb-cursor0 libxkbcommon-x11-0

# Arch
sudo pacman -S base-devel cmake pkgconf ffmpeg wayland wayland-protocols \
  libpulse portaudio

# Fedora
sudo dnf install gcc-c++ cmake pkgconf ffmpeg-devel wayland-devel \
  wayland-protocols-devel pulseaudio-libs-devel portaudio
```

`libportaudio2` is easy to miss: `sounddevice` imports fine without it and then
fails at runtime with `OSError: PortAudio library not found`. `pip install
sounddevice` does not fix it — the missing piece is the system library.

Optional helpers, each enabling one feature: `hyprctl` (Hyprland binds),
`xdotool` + `xprop` (X11 window and game detection), `grim` (Wayland
screenshots), `openbsd-netcat` (hotkey socket client), `xdg-utils` (open clips
folder). Check what you have:

```bash
bash tools/linux_system_report.sh
```

### 2. Build the capture engine

Use CMake 3.21 or newer (the pinned-library lookup uses `find_library(NO_CACHE)`).

First install the pinned LGPL FFmpeg the engine is built against (AUDIT-014):

```bash
python tools/fetch_third_party.py --ffmpeg-linux
```

```bash
cmake -S FTHRcapture_linux -B FTHRcapture_linux/build       -DCMAKE_BUILD_TYPE=Release       -DFTHR_FFMPEG_ROOT="$PWD/FTHRcapture_linux/third_party/ffmpeg"
cmake --build FTHRcapture_linux/build --parallel
ctest --test-dir FTHRcapture_linux/build --output-on-failure
ldd FTHRcapture_linux/build/FTHRclips | grep 'not found'   # must print nothing
```

Output: `FTHRcapture_linux/build/FTHRclips`.

A **Release build without `-DFTHR_FFMPEG_ROOT` is refused**, on purpose: the
distribution's FFmpeg is a GPL build and linking it silently is what AUDIT-014
recorded. A Debug/RelWithDebInfo build may use the system FFmpeg and says so
loudly — never package one.

Notes on the build:

- `wayland-scanner` reads the XML definitions from
  `FTHRcapture_linux/protocols/` and writes generated bindings under the CMake
  build directory. The source checkout can remain read-only and a build cannot
  rewrite tracked bindings when scanner versions differ.
- The engine carries an `$ORIGIN`-relative `RPATH` (DT_RPATH, not RUNPATH) so
  it loads the bundled FFmpeg wherever the bundle is mounted, and so transitive
  dependencies inherit the search path. No absolute build-host path is baked in.
- `target_compile_options` used to append `-O2` *after* the `-O3 -DNDEBUG` that
  `CMAKE_BUILD_TYPE=Release` contributes, silently downgrading Release builds.
  The redundant `-O2` was removed; `-Wall -Wextra` stayed.
- The binary is not stripped; `FTHR_linux.spec` strips it when bundling.
- Ubuntu 24.04's `wayland-scanner` 1.22 may print two XML DTD validation warnings
  for the newer `deprecated-since` attribute. The generated bindings still
  compile; compiler/linker warnings are not expected.

### 3. Run from source

```bash
python FTHR_UI/main.py
```

The UI spawns the engine itself. To run the engine standalone for diagnostics
(positional argv):

```
FTHRclips <fps> <buffer_s> <w> <h> <bitrate_kbps> <_> <_> <_> <scaling>
          <output> <codec_pref> <preset> <multiband> <audio_enabled>
```

```bash
FTHRcapture_linux/build/FTHRclips 30 10 1280 720 6000 0 0 0 0 "" 0 4 0 1
```

The alpha build tries `wlr-screencopy` and then
`ext-image-copy-capture`, using only protocols the compositor advertises.
KDE Plasma/KWin sessions that expose capture only through the ScreenCast
portal/PipeWire are not supported by the current alpha engine and require a
future portal/PipeWire video backend. FFmpeg `x11grab` is compiled out by
default because AUDIT-044 has no proven bounded-cancellation path. Unsupported
sessions fail clearly after bounded recovery instead of falling back to X11.
Developers can compile the known-unbounded backend only with
`-DFTHR_EXPERIMENTAL_X11GRAB=ON`; such a build is not an alpha release build.

### 4. Audio

The engine resolves the PulseAudio default **sink**, then opens that sink's
`monitor_source_name` (PipeWire's PulseAudio compatibility layer works). It
never treats the default microphone/source as desktop audio. Resolution/open is
performed synchronously; failure emits a structured startup warning and capture
continues video-only.

On some distributions your user must be in the `audio` group:

```bash
sudo usermod -aG audio "$USER"   # then log out and back in
```

### 5. AppImage

```bash
bash build_linux.sh
```

Steps: dependency check → engine build → PyInstaller bundle → strip unused
libraries → smoke test → assemble AppDir → **licence verification** →
`appimagetool`. The filename carries the version from `FTHR_UI/version.py`.

#### The licence gate (AUDIT-014)

`build_linux.sh` runs `tools/verify_release_licenses.py --appdir` before packing
and refuses to build if a GPL FFmpeg made it into the bundle. As of 2026-08-06
that gate passes, because the Linux build no longer uses the distribution's
FFmpeg at all:

* `tools/fetch_third_party.py --ffmpeg-linux` installs a pinned **LGPL** FFmpeg
  (BtbN `n8.1.2-34-g9b6c8969e0`, LGPLv3, glibc 2.28 baseline) into
  `FTHRcapture_linux/third_party/ffmpeg`, verifying the archive and all seven
  libraries against `tools/ffmpeg_manifest_linux.json`.
* CMake **refuses a Release build** without `-DFTHR_FFMPEG_ROOT` rather than
  silently linking `/usr/lib`.
* The engine carries an `$ORIGIN` RPATH, so it loads the bundled libraries even
  where a system FFmpeg exists.
* The gate hash-verifies every shipped library and checks the licence of the
  FFmpeg copies that arrive inside the PySide6 Qt runtime and OpenCV wheels.

If it does fail, it is telling you something real. Do not add exceptions to it.

### Verified build state

Ubuntu 24.04 / WSL2, 2026-08-06:

| Step | Result |
|---|---|
| CMake configure from an empty directory | OK |
| Build | OK — 0 errors, 2 warnings |
| `ldd` missing libraries | 0 |
| Engine starts, creates `/dev/shm/FTHR_SharedMemory_v3` | OK |
| PyInstaller bundle | OK — 580 MB, engine included |
| AppDir contents (licences, icon, `.desktop`, no user data) | OK |
| Licence gate | **PASS** — 83 checks, 0 failed, 0 warnings |
| AppImage produced | **YES** — 219 MB, starts (offscreen), engine loads all 7 bundled FFmpeg libraries |

Current platform evidence and remaining physical gaps are recorded in
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md). Historical build numbers are not release
evidence for the current commit.
