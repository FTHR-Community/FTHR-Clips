# Linux Engine Hardening, Phase 3: Implementation Plan (packaging, docs, qualification, release)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the pinned LGPL `ffmpeg`/`ffprobe` inside the AppImage so exports stop depending on the distro's GPL build, bring the Linux docs and gates in line with what the code does, qualify the result on the maintainer machine, and tag `v1.1.3-alpha`.

**Architecture:** `tools/fetch_third_party.py --ffmpeg-linux` installs the pinned BtbN tarball into `FTHRcapture_linux/third_party/ffmpeg/{lib,include,bin}` and patches the two CLI tools' RPATH. `FTHR_linux.spec` collects the engine, mixer and the seven libraries into `_internal/`. `tools/verify_release_licenses.py --appdir` is the gate that hash-verifies the libraries and licence-checks every FFmpeg-named binary; `check_ffmpeg_executable` already exists for CLI binaries. `FTHR_UI/core/ffmpeg_tools.py` looks for `ffmpeg` in `_MEIPASS/engine`, `_MEIPASS`, then beside the executable, then the dev tree, then PATH. The AppImage is built only by CI on ubuntu-22.04 (glibc 2.35 gate).

**Tech Stack:** Python 3.14 venv `.venv-linux`, pytest, ruff, patchelf (venv wheel), readelf, the pinned FFmpeg.

**Spec:** `docs/superpowers/specs/2026-10-12-linux-engine-hardening-design.md`, "Phase 3".

## Global Constraints

- Branch from `origin/linux`; one PR per task group; Linux CI green before merge; author `kalopsia-space`; no AI attribution.
- Never add a GPL component. The bundled CLI must be the pinned LGPL build and must pass `verify_release_licenses.py --appdir` and `verify_linux_glibc.py`.
- The licence gate's hash list stays exact for the seven libraries; the CLI tools are verified by banner, configure flags, NEEDED and RPATH (their bytes change with the patchelf version, so they are not hash-pinned; say so in the manifest).
- Docs describe current behaviour only.

## Review Focus

1. A source checkout without the pinned tree: `ffmpeg_tools` must still fall back to PATH with the existing warning, never crash. Pinned by the existing `tests/test_ffmpeg_tools.py` (run it).
2. An AppImage where `_internal/ffmpeg` exists but the libraries beside it are missing (hand-trimmed bundle): the CLI must fail to load rather than pick up `/usr/lib` FFmpeg; the RPATH must not include a system path. Pinned in Task 1's gate test (RPATH entries all `$ORIGIN`-relative).
3. `verify_release_licenses.py --all` must inspect the AppDir the build script actually writes. Pinned in Task 3's test.
4. The `ffmpeg` CLI's `NEEDED` must list only the pinned SONAMEs plus libc-family; any `.so.60/.58` system generation is a failure. Pinned in Task 1's gate test.
5. The qualification pass must use a CI-built AppImage, not a local build (local builds cannot pass the glibc gate on this host). Stated in Task 5.

---

## PR G: bundled ffmpeg and ffprobe (Tasks 1 and 2; branch `fix/linux-bundle-ffmpeg-cli`)

### Task 1: Collect the CLI tools and gate them

**Files:**
- Modify: `tools/fetch_third_party.py:245-268` (`_ensure_linux_ffmpeg_tool_rpaths`)
- Modify: `FTHR_linux.spec:115-135` (binaries)
- Modify: `tools/ffmpeg_manifest_linux.json` (new `cli_tools` section)
- Modify: `tools/verify_release_licenses.py:684-791` (`check_linux_ffmpeg_libs` / new `check_linux_ffmpeg_cli`)
- Modify: `FTHR_UI/core/ffmpeg_tools.py:50` (comment only: the engine and tools sit in `_internal/`, not beside AppRun)
- Test: `tests/test_license_gate_linux.py` (append), `tests/test_third_party_fetch.py` (append)

**Interfaces:**
- Produces: in the AppDir, `_internal/ffmpeg` and `_internal/ffprobe` with `DT_RPATH` = `$ORIGIN/../lib:$ORIGIN` (dev tree resolves `bin/../lib`; bundle resolves `_internal/`).
- Produces: `check_linux_ffmpeg_cli(root: Path, rep: Report, label: str) -> None` in the gate: for each of `ffmpeg`, `ffprobe` under the artifact root (recursive, basename match), require: `check_ffmpeg_executable` passes (banner/flags), `NEEDED` FFmpeg entries ⊆ manifest `libraries_linked_by_engine ∪ libraries_transitively_required ∪ {libpostproc…}` and none in `SYSTEM_FFMPEG_SONAMES`, every RPATH entry starts with `$ORIGIN`. Missing tools in an `--appdir` run are a failure (the bundle must ship them); missing in `--tree` is a warning (same as today for the libraries).

- [ ] **Step 1: Failing tests.** In `tests/test_license_gate_linux.py`, build a fake appdir in `tmp_path` using the fixtures the file already has for libraries (grep for how it fabricates `.so` files and the manifest), add fake `ffmpeg`/`ffprobe` entries, and monkeypatch `_elf_needed`/`_elf_rpath`/`check_ffmpeg_executable` to return controlled values. Cases: (a) tools present, RPATH `$ORIGIN/../lib:$ORIGIN`, NEEDED pinned → no failure; (b) RPATH contains `/usr/lib` → failure mentioning RPATH; (c) NEEDED contains `libavcodec.so.60` → failure; (d) tools absent in appdir mode → failure "AppDir does not ship ffmpeg". In `tests/test_third_party_fetch.py`, assert the patchelf invocation passes `--set-rpath '$ORIGIN/../lib:$ORIGIN'` (monkeypatch `subprocess.run` and capture argv).
- [ ] **Step 2: run, fail. Step 3: implement:**
  - `fetch_third_party.py`: RPATH string becomes `'$ORIGIN/../lib:$ORIGIN'`; docstring explains the two layouts.
  - `FTHR_linux.spec`: after `_FFMPEG_LIBS`, `_FFMPEG_TOOLS = [p for p in (_FFMPEG_ROOT / 'bin' / n for n in ('ffmpeg', 'ffprobe'))]`; refuse with a clear message if either is missing; add `*[(str(p), '.') for p in _FFMPEG_TOOLS]` to `binaries`. Note `strip=True` applies; that is fine because the tools are verified by banner, not hash.
  - `ffmpeg_manifest_linux.json`: add `"cli_tools": {"shipped": ["ffmpeg", "ffprobe"], "rpath": "$ORIGIN/../lib:$ORIGIN", "verified_by": "banner, configure flags, NEEDED, RPATH; not hash-pinned because patchelf output varies by patchelf version"}`.
  - Gate: implement `check_linux_ffmpeg_cli` and call it from the `--appdir` block (after `check_linux_ffmpeg_libs`) and from `--tree` (warning mode).
  - `ffmpeg_tools.py:50` comment: "PyInstaller layout: the engine, FFmpeg libraries and CLI tools sit in `_internal/` next to the executable."
- [ ] **Step 4: tests pass; `python tools/verify_release_licenses.py --tree .` still passes locally. Step 5: commit** `build(linux): ship the pinned ffmpeg and ffprobe in the AppImage and gate them`.

### Task 2: Notices and docs for the Linux FFmpeg

**Files:**
- Modify: `THIRD_PARTY_NOTICES.md` (new "### FFmpeg (Linux engine and AppImage)" after the Windows FFmpeg entry; "Verifying this file" gains `--appdir build_output/AppDir`)
- Modify: `docs/linux.md` (Runtime requirements: the AppImage carries `ffmpeg`/`ffprobe`; a source checkout needs a system `ffmpeg` on PATH for exports and watermarking)
- Modify: `README.md` line ~101 (same one-line fact)

- [ ] **Step 1:** Write the entry: version `n8.1.2-34-g9b6c8969e0`, release `autobuild-2026-07-31-14-10`, asset name, sha256 prefix with pointer to `tools/ffmpeg_manifest_linux.json`, licence LGPL v3 or later (`--enable-version3`), what is shipped (seven libraries, two CLI tools), source route (same as the Windows entry's wording), and that the engine and `libFTHRPlaybackMixer.so` link it via `$ORIGIN` RPATH.
- [ ] **Step 2:** `python tools/verify_release_licenses.py --tree .` and `tests/test_license_gate_linux.py` pass (the gate checks notice presence for manifest components; confirm by reading `check_linux_manifest` at line 641 and satisfy it).
- [ ] **Step 3: commit** `docs: record the Linux FFmpeg build and the bundled CLI tools in the notices`. Push, open PR G.

## PR H: docs and gate hygiene (Tasks 3 and 4; branch `docs/linux-build-truth`)

### Task 3: `--all` checks the real AppDir; CI cache key

**Files:**
- Modify: `tools/verify_release_licenses.py:857` (`root / 'build' / 'AppDir'` → `root / 'build_output' / 'AppDir'`)
- Modify: `.github/workflows/ci.yml` (AppImage job `cache-dependency-path` gains `requirements-dev.txt`)
- Test: `tests/test_license_gate_linux.py` (append: `--all` resolves `build_output/AppDir`; monkeypatch `main` argv and the check functions to record the path), `tests/test_ci_contract.py` or `tests/test_linux_glibc_gate.py` (append: the AppImage job's cache path lists both requirement files)

- [ ] Steps: failing tests, implement, pass, commit `build: licence gate --all inspects build_output/AppDir; AppImage job cache covers dev requirements`.

### Task 4: BUILDING.md, system report, stale comments

**Files:**
- Modify: `BUILDING.md` Linux section (lines ~243-375)
- Modify: `tools/linux_system_report.sh` ("engine will try" lines)
- Modify: `FTHR_linux.spec:41-44`, `build_linux.sh:216-219` (comment: "Ubuntu 22.04 build baseline")
- Test: `tests/test_linux_alpha_runtime_policy.py::test_linux_report_describes_native_x11_without_xwayland_claim` (keep passing), add an assertion that the report mentions the portal

- [ ] **BUILDING.md:** replace the x11grab paragraph (lines ~312-316) with: `FTHR_X11_CAPTURE` defaults ON and builds the process-isolated x11grab backend for native X11 sessions; it is never used under XWayland; `-DFTHR_X11_CAPTURE=OFF` removes it. Remove the systemd-scope paragraph (~309-312); state that the portal restore token lives in `~/.fthr/portal_screencast_token` and the picker appears once per app identity. Replace "Ubuntu 24.04's wayland-scanner" note with the 22.04 baseline and the fact that CI builds the release AppImage on ubuntu-22.04 with a GLIBC 2.35 ceiling; add a sentence that a local `build_linux.sh` on a newer glibc host fails the gate by design and is for engine/bundle debugging only. Replace the "Verified build state" table with a pointer to the `linux-appimage-package` CI job and `docs/linux.md`. List the real `build_linux.sh` steps (dependency checks, pinned FFmpeg fetch, CMake, ctest, linkage check, uploader bundle, PyInstaller, library strip, GPL copy removal, ELF strip repair, AppDir, licence gate, pinned appimagetool, glibc gate). Add `FTHR_CAPTURE_BACKEND` to the standalone-engine diagnostics paragraph.
- [ ] **linux_system_report.sh:** Wayland line: `'wlr-screencopy, then ext-image-copy-capture, then the ScreenCast portal'`.
- [ ] Spec/script comments: "Ubuntu 22.04 build baseline".
- [ ] Run `tests/test_linux_alpha_runtime_policy.py tests/test_linux_appimage_report.py tests/test_apprun_platform_override.py`; commit `docs(linux): BUILDING.md matches the build script and the engine's backends`. Push, open PR H.

## Task 5: Qualification pass (maintainer machine, CI-built AppImage)

Download the `fthr-linux-appimage-<sha>` artifact from the `linux-appimage-package` job of the `linux` head after PRs A to H are merged (`gh run download`), `chmod +x`, and run each check; record each result in a comment on the tracking issue (open one: "1.1.3-alpha Linux qualification") with the engine log excerpt.

- [ ] Launch; `~/.fthr/logs/fthr.log` shows the engine found in `_internal`, backend wlr-screencopy, h264_vaapi.
- [ ] F9 saves a clip from DP-3; F12 saves a screenshot (grim).
- [ ] Switch monitor to HDMI-A-1 in settings; save again.
- [ ] `FTHR_CAPTURE_BACKEND=ext` AppImage run: static screen 30 s, frame counter keeps rising, save succeeds (already proven on the integration build; repeat on the artifact).
- [ ] `FTHR_CAPTURE_BACKEND=portal` run: picker appears once; choose DP-3; restore token written 0600; relaunch shows no picker.
- [ ] Freeze Hyprland 10 s (`kill -STOP`): log shows "Waiting for the compositor" then "keeping encoder and replay history"; a 20 s save afterwards contains footage from before the freeze.
- [ ] `pipewire-pulse` stopped before launch, started 30 s later: audio attaches; a clip saved afterwards has audible desktop audio.
- [ ] Encoder preference "software" and "AMD": both produce playable clips.
- [ ] Export with watermark from the clip editor: `fthr.log` shows the bundled `_internal/ffmpeg` path, not `/usr/bin/ffmpeg`.
- [ ] 2-hour idle soak: no health transition in `events.jsonl`.
- [ ] `bash tools/linux_appimage_report.sh <AppImage>` output attached.

## Task 6: Release

- [ ] Branch `release/1.1.3-alpha` from `linux`: `FTHR_UI/version.py` → `__version__ = '1.1.3-alpha'`, `VERSION_INFO = (1, 1, 3)`, `BUILD_DATE` = tag date; `installer_windows.iss` version literal (the consistency check requires it); `RELEASE_NOTES.md` new top section listing the Phase 0 to 3 changes in user terms (compositor stall recovery, encoder failure visibility, save/recovery race, audio late attach, fsync, ext static screen, bundled ffmpeg, Hyprland binds, portal screenshots and mic selection from #57, desktop identity from #19/#59, VA-API probe from #42/#60, `FTHR_CAPTURE_BACKEND`); `docs/linux.md` download links to the new tag; `KNOWN_ISSUES.md` remove the KDE picker-stall item if the qualification shows it fixed, keep otherwise.
- [ ] `python tools/verify_version_consistency.py`, `verify_release_licenses.py --tree .`, `verify_shared_memory_contract.py`, `verify_engine_response_contract.py`, `verify_exception_handling.py`, `scan_repo_hygiene.py`, full pytest, ruff.
- [ ] PR to `linux`, merge, then `git tag v1.1.3-alpha <merge sha>`, push the tag; the `tag-guard` job must pass; download the AppImage artifact and its `.sha256`; publish the GitHub pre-release manually as the docs require (maintainer action; the plan stops at "artifact ready and verified", and the release page text is drafted in the PR body).

## Execution

PR G and PR H are independent of each other and of Phases 1 and 2; implement in parallel worktrees once Phase 1 PRs are merged (the docs in H describe Phase 1 behaviour). Task 5 after everything is merged. Task 6 after Task 5 passes. Fresh reviewer per PR.
