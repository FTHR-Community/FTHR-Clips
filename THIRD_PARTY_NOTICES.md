# Third-Party Notices — FTHR Clips

FTHR Clips bundles and links against third-party software. This file lists
every component that is **actually distributed** in a release artifact, its
licence, and where to find the full licence text.

FTHR Clips' own source code is licensed **GPL-3.0-only** (see
[`LICENSE`](LICENSE)). Third-party components and project assets explicitly
identified below retain their respective licences.

Full licence texts live in [`licenses/`](licenses/), and are installed
alongside the application (Windows: `licenses\` in the install directory;
Linux: `licenses/` inside the AppImage).

Last verified: **2026-08-24**.

---

## Licence of the distributed build

> **Read this before publishing a build.**

The Qt binding/runtime and FFmpeg are distributed under LGPL options:

| Component | Licence | Consequence for the distributed binary |
|---|---|---|
| FFmpeg (dynamically linked) | LGPLv3-or-later | Remains under LGPL; notices, source availability, and relinking conditions apply. |
| PySide6 / Shiboken (separate extension modules) | LGPL-3.0-only option selected | Remains under LGPL; recipients may replace the components and reverse engineer for debugging modifications to them. |
| Qt 6 (separate shared libraries) | LGPL-3.0-only option selected | Remains under LGPL; notices, corresponding source, and replaceability/relinking conditions apply. |

The engine FFmpeg build is LGPLv3 with no
GPL components (see below). It is loaded as separate DLLs/shared libraries, so
the LGPL relinking requirement is met.

FTHR selects the LGPLv3 option offered for PySide6, Shiboken, and Qt 6.11.1.
The application does not statically link or modify those components. Exact
upstream source locations are recorded in
[`licenses/Qt6-SOURCE.txt`](licenses/Qt6-SOURCE.txt). The full LGPLv3 text is
[`licenses/Qt6-LICENSE.txt`](licenses/Qt6-LICENSE.txt).

This is a technical compliance inventory, not legal advice. Project-generated
media and the separately licensed Oswald font are documented under
[Project assets](#project-assets) and hash-locked in the release manifest.

---

## Distributed components

### FFmpeg

| | |
|---|---|
| **Version** | `n8.1.2-21-gce3c09c101` (release branch 8.1, build dated 2026-06-30) |
| **Source** | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds), release `autobuild-2026-06-30-13-34`, asset `ffmpeg-n8.1.2-21-gce3c09c101-win64-lgpl-shared-8.1.zip` |
| **SHA-256 (zip)** | `27bcaf58b5140171…` — full value recorded in [`tools/ffmpeg_manifest.json`](tools/ffmpeg_manifest.json) |
| **Licence** | **LGPL v3 or later** — self-reported by the binary as `libavcodec license: LGPL version 3 or later` |
| **Linkage** | Dynamic. The C++ engine links the import libraries; the DLLs ship beside it. The Python UI shells out to `ffmpeg.exe`. |
| **Used for** | Video/audio encoding and muxing in the capture engine; mic mux, multiband audio mix, watermark, webcam overlay, clip export/share in the UI. |
| **Licence text** | [`licenses/FFmpeg-LICENSE.txt`](licenses/FFmpeg-LICENSE.txt) |
| **Shipped files** | `avcodec-62.dll`, `avdevice-62.dll`, `avfilter-11.dll`, `avformat-62.dll`, `avutil-60.dll`, `swresample-6.dll`, `swscale-9.dll`, `ffmpeg.exe`, `ffprobe.exe` |

Build configuration — the flags that matter, verified with `ffmpeg -buildconf`:

```
absent : --enable-gpl          absent : --enable-libx264
absent : --enable-nonfree      absent : --enable-libx265
absent : --enable-libxvid      absent : --enable-libxavs2
present: --enable-version3     present: --enable-libopenh264
```

`--enable-version3` upgrades the licence from LGPLv2.1 to **LGPLv3**. It does
**not** make the build GPL — `ffmpeg -L` reports the GNU *Lesser* General Public
License v3. It is required by the LGPL builds BtbN publishes and cannot be
switched off without compiling FFmpeg from source.

**Software encoders in this build** (no GPL encoders are present):

| Codec | Software encoder | Licence |
|---|---|---|
| H.264 | `libopenh264` (Cisco OpenH264) | BSD-2-Clause; [`licenses/OpenH264-LICENSE.txt`](licenses/OpenH264-LICENSE.txt) |
| H.265 | `libkvazaar` | LGPLv2.1 |
| AV1 | `libsvtav1`, `libaom-av1`, `librav1e` | BSD-3-Clause / BSD-2-Clause |
| AAC | FFmpeg native `aac` | LGPL (part of FFmpeg) |

Hardware encoders (NVENC, AMF, QSV, VAAPI, MediaFoundation) are unaffected and
remain fully available.

> **Patent note, not a licence note:** H.264/H.265 are covered by patent pools.
> Cisco distributes OpenH264 binaries under terms where it covers the AVC
> licensing fees, but that arrangement applies to *Cisco's own* binary
> downloads. FFmpeg builds that compile OpenH264 in do not automatically
> inherit it. This is unchanged from the previous x264-based build and is
> outside the scope of the LGPL fix — but it is worth a deliberate decision
> before a large public release.

### Qt 6

| | |
|---|---|
| **Version** | 6.11.1 (official Qt runtime supplied with PySide6 6.11.1) |
| **Source** | [Qt 6.11.1 source](https://download.qt.io/official_releases/qt/6.11/6.11.1/single/qt-everywhere-src-6.11.1.tar.xz) |
| **Licence** | LGPL-3.0-only option selected |
| **Linkage** | Dynamic (shared libraries bundled by PyInstaller) |
| **Used for** | Entire GUI, multimedia playback |
| **Licence/source notice** | [`licenses/Qt6-LICENSE.txt`](licenses/Qt6-LICENSE.txt), [`licenses/Qt6-SOURCE.txt`](licenses/Qt6-SOURCE.txt), [`licenses/Qt6-THIRD-PARTY-NOTICES.txt`](licenses/Qt6-THIRD-PARTY-NOTICES.txt) |

### FFmpeg bundled inside Qt Multimedia

A **second, independent** FFmpeg comes in with the official PySide6 Qt runtime:
Qt
Multimedia uses it for media playback (the clip preview player). It is separate
from the engine's copy and carries different SONAMEs, so both coexist without
conflict.

| | |
|---|---|
| **Files** | `avcodec-61.dll`, `avformat-61.dll`, `avutil-59.dll`, `swresample-5.dll`, `swscale-8.dll`, `ffmpegmediaplugin.dll` |
| **Location** | `_internal/PySide6/` under the Qt runtime/plugin directories |
| **Licence** | **LGPL v2.1 or later** — self-reported by the binaries as `libavcodec license: LGPL version 2.1 or later` |
| **Origin** | Built and shipped by the Qt Company as part of Qt 6 |
| **Used for** | `QMediaPlayer` playback in the clip viewer |
| **Licence text** | Covered by [`licenses/Qt6-LICENSE.txt`](licenses/Qt6-LICENSE.txt); the LGPL text also applies — see [`licenses/FFmpeg-LICENSE.txt`](licenses/FFmpeg-LICENSE.txt) |
| **Verified** | Contains no `--enable-gpl` and no x264/x265 |

### PySide6 and Shiboken

| | |
|---|---|
| **Version** | PySide6, PySide6-Addons, PySide6-Essentials, and shiboken6 6.11.1 |
| **Source** | [Qt for Python 6.11.1 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz) |
| **Licence** | LGPL-3.0-only option selected from the offered LGPL/GPL/commercial choices |
| **Linkage** | Separate Python extension modules, bundled |
| **Used for** | Python bindings for Qt — the UI framework |
| **Licence/source notice** | [`licenses/PySide6-NOTICE.txt`](licenses/PySide6-NOTICE.txt), [`licenses/Qt6-LICENSE.txt`](licenses/Qt6-LICENSE.txt), [`licenses/Qt6-SOURCE.txt`](licenses/Qt6-SOURCE.txt) |

### NumPy

| | |
|---|---|
| **Version** | 2.4.4 · **Licence** BSD-3-Clause |
| **Used for** | Audio buffer maths (mic capture, multiband mixing) |
| **Licence text** | [`licenses/numpy-LICENSE.txt`](licenses/numpy-LICENSE.txt) |

### OpenCV (`opencv-python-headless`)

| | |
|---|---|
| **Version** | 4.13.0.92 · **Licence** Apache-2.0 |
| **Used for** | Webcam capture, thumbnail generation, frame decoding |
| **Licence texts** | [`licenses/opencv-LICENSE-Apache2.txt`](licenses/opencv-LICENSE-Apache2.txt), third-party components in [`licenses/opencv-LICENSE.txt`](licenses/opencv-LICENSE.txt) |

### python-sounddevice

| | |
|---|---|
| **Version** | 0.5.5 · **Licence** MIT |
| **Used for** | Microphone capture |
| **Licence text** | [`licenses/sounddevice-LICENSE.txt`](licenses/sounddevice-LICENSE.txt) |
| **Note** | Wraps **PortAudio** (MIT). On Linux `libportaudio.so.2` is bundled explicitly by `FTHR_linux.spec`. |

### keyboard

| | |
|---|---|
| **Version** | 0.13.5 · **Licence** MIT |
| **Used for** | Global hotkeys (the only hotkey path on Windows) |
| **Licence text** | [`licenses/keyboard-LICENSE.txt`](licenses/keyboard-LICENSE.txt) |

### cffi and pycparser

| | |
|---|---|
| **Versions** | cffi 2.0.0; pycparser 3.0 |
| **Licences** | cffi: MIT No Attribution; pycparser: BSD-3-Clause |
| **Used for** | Transitive runtime dependencies of python-sounddevice |
| **Licence texts** | [`licenses/cffi-LICENSE.txt`](licenses/cffi-LICENSE.txt), [`licenses/pycparser-LICENSE.txt`](licenses/pycparser-LICENSE.txt) |

### NVIDIA Video Codec SDK header

| | |
|---|---|
| **File** | `FTHRcapture/FTHRclips/include/nvenc/nvEncodeAPI.h` |
| **Copyright** | © 2010–2024 NVIDIA Corporation |
| **Licence** | MIT-style permissive grant, stated in the header itself |
| **Linkage** | **None at build time.** NVENC is resolved at runtime via `LoadLibraryA("nvEncodeAPI64.dll")` against the user's installed driver. The header is source-only and the NVIDIA runtime is **not** redistributed. |
| **Licence text** | [`licenses/NVIDIA-NVENC-SDK-LICENSE.txt`](licenses/NVIDIA-NVENC-SDK-LICENSE.txt) |

### Wayland protocol definitions and generated bindings

| | |
|---|---|
| **Files** | `FTHRcapture_linux/protocols/*.xml` and generated client bindings compiled into the Linux engine |
| **Copyright** | The wlroots, Chromium OS, and Wayland contributors named in the source notices |
| **Licence** | MIT-style permissive grants stated in the protocol sources |
| **Used for** | Linux compositor capture and related Wayland protocol integration |
| **Licence notices** | [`licenses/Wayland-Protocols-NOTICES.txt`](licenses/Wayland-Protocols-NOTICES.txt) |

### PipeWire and D-Bus headers (Linux engine)

| | |
|---|---|
| **Files** | System headers `pipewire-0.3/`, `spa-0.2/` and `dbus-1.0/` read at build time only; nothing from either project is copied into this repository or the AppImage |
| **Copyright** | PipeWire: Wim Taymans and contributors. D-Bus: Red Hat, Inc. and contributors |
| **Licence** | PipeWire headers: MIT. libdbus: dual AFL-2.1 / GPL-2.0-or-later, used under the AFL-2.1 option |
| **Linkage** | **None at build time.** The engine resolves `libpipewire-0.3.so.0` and `libdbus-1.so.3` with `dlopen()`/`dlsym()` on the user's system when the ScreenCast portal capture backend is needed, and reports that backend as unavailable otherwise. The libraries are **not** redistributed. |
| **Used for** | `org.freedesktop.portal.ScreenCast` session setup over the session bus and PipeWire video stream capture on compositors without a capture protocol |

### Microsoft Visual C++ Redistributable

| | |
|---|---|
| **File** | `redist/vc_redist.x64.exe`, executed by the Windows installer |
| **Licence** | Microsoft Visual Studio redistributable terms |
| **Note** | Redistributed unmodified as permitted for VC++ runtime redistribution. |

---

## Project assets

### FTHR-generated media

The shipped logos, icons, installer artwork, repository social preview, and
four PCM WAV notification sounds are reproducibly generated from code in
`tools/generate_release_assets.py`. The generator uses no external creative
input, font, image, sound sample, or icon library. These outputs are FTHR
project material under MIT; the notice and exact file list are in
`licenses/FTHR-GENERATED-ASSETS.txt`.

The predecessor images and four MP3 files had no usable authorship, source, or
redistribution evidence. All predecessor image bytes were replaced, the MP3s
were removed, and none is allowlisted for a release. Machine-readable evidence
is recorded in `tools/release_asset_manifest.json` and enforced by
`tools/verify_release_licenses.py`; any failure keeps public release blocked.

### Original Gary artwork

`FTHR_UI/assets/gary.png` is original FTHR artwork, confirmed by the project
owner on 2026-09-17. It is maintained as a source image rather than generated
by the release asset script. It uses the project's MIT asset terms; see
`licenses/FTHR-GENERATED-ASSETS.txt` for the copyright and licence notice.

### Oswald Bold

| | |
|---|---|
| **File/version** | `Oswald-Bold.ttf`, version 4.103 |
| **Copyright** | Copyright 2016 The Oswald Project Authors |
| **Source** | Google Fonts' `googlefonts/OswaldFont`, revision `89795261ac9eeb9aa8cd99f43982c4e4b0e53261` |
| **Licence** | SIL Open Font License 1.1 |
| **Verification** | Repository and pinned upstream files have SHA-256 `eb7d46f856dd57f18a8c03d033c57802692bf127f01dbd95ba8984338e6b5135` |
| **Licence text** | `licenses/Oswald-OFL-1.1.txt` |

The authoritative per-file paths, hashes, origins, usage, and platform mappings
are in `tools/release_asset_manifest.json`.

---

## Not distributed

Listed so future audits do not have to re-derive it:

- **`imageio-ffmpeg`** — *removed* on 2026-08-05. Its bundled binary is a
  gyan.dev build with `--enable-gpl --enable-libx264 --enable-libx265`
  (GPLv3). It is now excluded in both PyInstaller specs and removed from
  `requirements.txt`. See `FTHR_UI/core/ffmpeg_tools.py`.
- **System FFmpeg on Linux** — if the bundled binary is absent, FTHR falls back
  to `ffmpeg` on `PATH`. That copy belongs to the user's distribution and is not
  redistributed by this project.
- **Development tooling** — pytest, ruff, PyInstaller, Inno Setup, MSVC.
- **PipeWire and libdbus runtime libraries** — loaded from the user's system
  with `dlopen()` by the Linux engine when the ScreenCast portal backend runs;
  never bundled. See the headers entry above.

---

## Verifying this file

[`tools/verify_release_licenses.py`](tools/verify_release_licenses.py) checks
the selected Qt binding and runtime modules, shipped artifacts for GPL build
flags/x264/x265, every approved release asset hash, and the required licence
files described here. Run it before every release:

```bash
python tools/verify_release_licenses.py --tree .
python tools/verify_release_licenses.py --windows-dist dist/FTHRClips
```

**This is a technical verification, not legal advice.**
