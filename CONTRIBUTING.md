# Contributing to FTHR Clips

Build and test instructions, repository layout, and contribution conventions.

## Authoritative layout

```
FTHR_Clips/
├── FTHR_UI/                    Python frontend (PySide6) — the entry point
│   ├── main.py                 MainWindow, top bar, hotkey wiring, save flow
│   ├── version.py              SINGLE SOURCE OF TRUTH for the product version
│   ├── core/                   Non-visual logic
│   │   ├── capture_bridge.py   Shared-memory IPC client to the C++ engine
│   │   ├── ffmpeg_tools.py     Shells out to the bundled LGPL ffmpeg/ffprobe
│   │   ├── settings_manager.py ~/.fthr/settings.json persistence
│   │   ├── single_instance.py  Win32 mutex / POSIX flock startup guard
│   │   └── ...
│   ├── ui/                     Widgets
│   │   ├── style.py            Design tokens — add colours/sizes HERE
│   │   └── ...
│   └── assets/                 Icons, fonts, notification sounds
├── FTHRcapture/                Windows capture engine (C++17, Visual Studio)
│   ├── FTHRcapture.sln
│   └── FTHRclips/
│       ├── include/            Headers, incl. shared_memory.h (IPC contract)
│       ├── src/                DXGI/WGC capture, NVENC/AMF/QSV encode
│       └── third_party/ffmpeg/ LGPL FFmpeg — headers+libs tracked, bin/ fetched
├── FTHRcapture_linux/          Linux capture engine (C++17, CMake)
│   ├── CMakeLists.txt
│   ├── src/                    X11 / wlroots / ext-image-copy-capture backends
│   └── protocols/              Generated Wayland protocol bindings (tracked)
├── tests/                      pytest suite
├── tools/                      Release verification and build helpers
├── FTHR.spec / FTHR_linux.spec PyInstaller bundles
├── installer_windows.iss       Inno Setup installer
├── build_linux.sh              AppImage builder
└── AppDir/                     AppImage .desktop + icon (source only)
```

### Entry points

| What | Where |
|---|---|
| Application | `FTHR_UI/main.py` (`python FTHR_UI/main.py`) |
| Windows engine | `FTHRcapture/FTHRclips/src/main.cpp` → `FTHRcapture/x64/Release/FTHRclips.exe` |
| Linux engine | `FTHRcapture_linux/src/main.cpp` → `FTHRcapture_linux/build/FTHRclips` |
| Product version | `FTHR_UI/version.py` |
| IPC contract | `FTHR_UI/core/capture_bridge.py` ↔ `FTHRcapture/FTHRclips/include/shared_memory.h` |

### Generated — never edit, never commit

`build/`, `dist/`, `Output/`, `FTHRcapture/x64/`, `FTHRcapture/FTHRclips/x64/`,
`FTHRcapture_linux/build/`, `AppDir/AppRun`, `AppDir/usr/`, `.vs/`,
`build/version_win.txt`, `__pycache__/`, `*.AppImage`, and the FFmpeg runtime
under `FTHRcapture/FTHRclips/third_party/ffmpeg/bin/`.

## Setup

Python **3.14** is what the alpha is built with. 3.12 and 3.13 are also
exercised in CI. Install from the lock files, not from a loose `pip install`:

```bash
git clone <repo-url>
cd FTHR_Clips
python -m pip install -r requirements-alpha.txt -r requirements-dev.txt
```

`requirements.in` is the human-edited list of direct dependencies.
`requirements-alpha.txt` is the pinned lock and the only thing a release build
installs from. Never add a `>=` constraint to the lock.

> `imageio-ffmpeg` must **not** be installed into a build environment. It
> bundles a GPLv3 FFmpeg. The app uses the LGPL `ffmpeg.exe` / `ffprobe.exe`
> shipped next to the engine — see `FTHR_UI/core/ffmpeg_tools.py` and AUDIT-005.

### Tests

```bash
python -m pytest tests/
```

`QT_QPA_PLATFORM=offscreen` is set automatically by `tests/conftest.py`.

### Lint

```bash
python -m ruff check .
```

### Windows engine

Needs Visual Studio 2022 (Desktop C++ workload) and the FFmpeg runtime:

```powershell
python tools\fetch_third_party.py --all     # FFmpeg DLLs + vc_redist
& "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" `
    FTHRcapture\FTHRcapture.sln -p:Configuration=Release -p:Platform=x64
python -m PyInstaller FTHR.spec --clean
```

The FFmpeg headers and import libraries are tracked in git, so the engine
compiles from a clean clone. The 152 MB of runtime DLLs are not; the fetch
script pulls them from the exact BtbN release pinned by sha256 in
`tools/ffmpeg_manifest.json`.

See [`BUILDING.md`](BUILDING.md) for the full walkthrough.

### Linux engine

```bash
# Deps: cmake, gcc, pkg-config, ffmpeg, libpulse, wayland-protocols, wayland-scanner
cd FTHRcapture_linux
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

Full AppImage:

```bash
./build_linux.sh
```

See [`BUILDING.md`](BUILDING.md).

## The shared-memory contract

The UI and the engine talk through one fixed-layout struct in shared memory.
There is no serialisation and no handshake. **If the two definitions drift by a
single byte, every field after the drift point reads garbage — silently, at
runtime, with no build error.**

Three definitions must stay in lockstep:

| Side | File |
|---|---|
| Python | `FTHR_UI/core/capture_bridge.py` — `SharedMemoryLayout` (separate win32 / posix branches) |
| Windows engine | `FTHRcapture/FTHRclips/include/shared_memory.h` |
| Linux engine | `FTHRcapture_linux/src/shared_memory.h` |

Rules:

1. **Never reorder fields.** Append only.
2. **Never change a field's type or array extent** without changing all three.
3. **Never reuse command IDs 4–9.** Those are the retired `SET_*` commands.
   They are dead, but an engine built before they were retired still acts on
   those numbers — reusing one would make an old binary do the wrong thing
   instead of ignoring an unknown command.
4. **Bump `CaptureBridge.SHARED_MEM_NAME`** (`FTHR_SharedMemory_v4` → `_v5`)
   whenever the layout changes, so an old engine and a new UI cannot map the
   same region. Do **not** bump it for a product version change — it is not
   the product version.
5. Run the checker before you push:

   ```bash
   python tools/verify_shared_memory_contract.py
   ```

   It parses both C++ headers, models the compiler's alignment, and compares
   field order, types, array extents, offsets and total size against the ctypes
   struct — for both platforms, from any platform. It also runs as part of
   `pytest` (`tests/test_shared_memory_contract.py`).

Current layout: 29 fields, 2736 B on Windows, 4272 B on Linux.

The positional engine arguments are documented in
[`docs/engine-startup.md`](docs/engine-startup.md).

## Other conventions

- **Settings live in `~/.fthr/settings.json`** via `SettingsManager`. Don't add
  a second config file — one was already removed.
- **Design tokens come from `FTHR_UI/ui/style.py`.** Don't inline new colours
  or font sizes; add them there.
- **The product version comes from `FTHR_UI/version.py`.** Don't type a version
  literal anywhere else. `tools/verify_version_consistency.py` will fail you.
- Sound assets live in `FTHR_UI/assets/sounds/`.

## Making changes

1. Branch: `git checkout -b fix/my-fix`
2. Make your changes
3. `python -m pytest tests/` and `python -m ruff check .`
4. `python tools/scan_repo_hygiene.py --staged` before committing
5. Commit with a clear message (style below)
6. Open a pull request

## Commit style

```
type: short description

Longer explanation if needed.
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, `ci`

Examples:
```
fix: clip_ready never set when multiband mux fails
feat: add per-monitor capture selection
build: pin alpha dependencies exactly (AUDIT-009)
```

## What we're looking for

- Bug fixes
- Linux hardware compatibility (AMD/Intel GPU capture, other Wayland compositors)
- Windows testing and fixes
- Python and native C++ test coverage
- Documentation improvements

## What to avoid

- Large refactors without prior discussion
- Adding dependencies without a strong reason — and never a GPL one, see
  `THIRD_PARTY_NOTICES.md`
- Changing UI layout without screenshots in the PR
- Committing anything under the "generated" list above

## Code style

- **Python:** match the surrounding code. `ruff` enforces a deliberately narrow
  rule set (real bugs, not cosmetics) — see `pyproject.toml`.
- **C++:** C++17, 4-space indent, snake_case.

### Comments and docstrings

Write for someone who knows the language but doesn't know this project yet.
Explain a module's role or a function's contract when its name isn't enough.
Inside an implementation, focus on reasons, constraints, and surprising behavior.

- Keep comments close to the code they explain, usually one to three lines.
  Put longer architecture notes, protocols, and walkthroughs in `docs/` and
  link them from the relevant code.
- Preserve details a reader can't infer: units, ownership, thread affinity,
  ordering, lifetime, compatibility rules, and why an error is safe to ignore.
- Don't narrate assignments, repeat a function name, or add a docstring just
  to fill a template. Short section labels can help navigate a long file;
  decorative dividers and numbered step-by-step commentary usually don't.
- Describe current behavior. Git history and issues hold development phases,
  before/after stories, and abandoned approaches. Keep an issue reference when
  it helps explain a constraint that still applies.
- Use plain language. Contractions such as "can't" and "doesn't" are fine.
  Avoid jokes, slogans, exaggerated guarantees, and unexplained emphasis.
- Update comments with behavior changes. Preserve license notices, upstream
  comments, generated files, and directives such as `noqa` and `type: ignore`.

For example, `# Set the command` repeats the next assignment.
`# Publish the command last; the engine may read its payload immediately.`
explains an ordering rule worth keeping.

These conventions follow the practical guidance in the
[Google C++ style guide](https://google.github.io/styleguide/cppguide#Comments)
and the [Linux kernel commenting guide](https://docs.kernel.org/process/coding-style.html#commenting).
