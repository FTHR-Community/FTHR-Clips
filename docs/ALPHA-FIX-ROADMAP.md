# FTHR Clips Alpha Fix Roadmap

Last reviewed: 2026-10-10 (Windows backlog; Linux entries unchanged)

This roadmap tracks the public community reports against the current
`integrate/latest-project-source` alpha tree. A merged community change is not
considered fixed in this tree until it has been ported and regression-tested.
Hardware-specific reports remain field-validation pending until an affected
system confirms a new build.

The anonymized 51-report field baseline and its cross-platform acceptance
matrix are maintained in
[`ALPHA-FIELD-BUG-TRIAGE-2026-09-20.md`](ALPHA-FIELD-BUG-TRIAGE-2026-09-20.md).

## P0 — Linux alpha release blockers

### Portable AppImage baseline — issue #28

Status: **BUILD GATE IMPLEMENTED / REGRESSION TESTED — RELEASE BLOCKER OPEN**

The AppImage must not inherit a newer host glibc requirement from the local
Python/PyInstaller toolchain. Release CI now builds on the exact Ubuntu 22.04
runner and checks the AppDir plus final AppImage against the manifest-declared
`GLIBC_2.35` ceiling. This is a build/reproducibility fix, not physical field
qualification and does not by itself close issue #28.

Acceptance criteria:

- inspect bundled ELF symbol requirements and enforce the manifest-declared
  `GLIBC_2.35` ceiling;
- fail before checksum/success publication when the AppDir or final AppImage
  is missing, unreadable, has no ELF, has a `readelf` error, or exceeds the
  ceiling;
- keep the release baseline at x86_64 / `ubuntu-22.04` because the pinned
  PySide6 6.11.1 x86_64 wheel declares `manylinux_2_34`;
- launch through FUSE and `APPIMAGE_EXTRACT_AND_RUN=1` on an older reference
  distribution and Fedora 44;
- verify the bundled Qt Wayland and XCB platform plugins on those systems;
- generate and verify the AppImage together with a SHA-256 checksum for local
  builders; GitHub CI validates the build but does not upload or publish the
  resulting image.

Remaining physical launch/capture qualification is still required before this
issue can be closed. Tracking:
https://github.com/FTHR-Community/FTHR-Clips/issues/28

The AppImage supply-chain fetch is now **IMPLEMENTED / REGRESSION TESTED**:
`appimagetool` 1.9.1 and the type-2 runtime 20251108 are pinned by commit,
size, and SHA-256 in `tools/appimage_tool_manifest.json`. The build fetches
both through `tools/fetch_third_party.py`, verifies cached and downloaded bytes,
and supplies the verified runtime explicitly with `--runtime-file`; no mutable
`continuous` URL is accepted. The runtime licence is shipped in the AppImage
and checked by the release licence gate. The actual AppImage CI build and
physical Linux/Fedora field validation remain pending, and the final GLIBC
scan still checks the embedded runtime.

### KDE/KWin Wayland capture — issues #20 and #29

Status: **IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING**

The current tree now keeps the protocol-specific `wlr-screencopy` and
`ext-image-copy-capture` backends and adds the community ScreenCast
portal/PipeWire backend from PR #31, adapted to the newer recovery and health
code. Deterministic portal, recovery, shutdown and frame-helper tests pass.
Issue #29 is the same missing-backend class observed on another AMD/KDE system;
physical KDE/AMD/NVIDIA confirmation still requires a new AppImage and affected
field systems.

Acceptance criteria:

- KDE/KWin returns non-black desktop frames through the ScreenCast portal;
- portal cancellation produces one actionable failure, not repeated pickers;
- the restore token is private and a later start can reuse it;
- engine shutdown interrupts all portal waits within their documented bound;
- a saved replay decodes and has the requested duration;
- AMD and NVIDIA KDE field reports are re-tested with the new AppImage.

Tracking:

- https://github.com/FTHR-Community/FTHR-Clips/issues/20
- https://github.com/FTHR-Community/FTHR-Clips/issues/29
- https://github.com/FTHR-Community/FTHR-Clips/pull/31

## P0 — Windows capture correctness

### Persistent monitor identity after disconnect or disable

Status: **FIX IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING**

A non-empty saved Windows monitor identity is no longer silently replaced with
the primary monitor when that output is temporarily disconnected, disabled or
missing from the active topology. The source selector keeps the saved target
visible as unavailable, and the launch configuration passes the original
identity to the native resolver so diagnostics report the real failure. An
empty first-run setting still selects the current default monitor, and the
legacy GDI-name migration remains supported when it matches an active output.

Acceptance criteria:

- unplugging or disabling the selected output does not rewrite the setting;
- the unavailable target is visible and an active replacement requires an
  explicit user selection;
- native startup evidence retains the requested monitor identity;
- enable/disable and hotplug are physically re-tested on single-, multi- and
  hybrid-GPU Windows systems.

### Frozen old frame while audio continues

Status: **INSTRUMENTED / REGRESSION TESTED — FIELD ROOT CAUSE UNVERIFIED**

The WGC loop can reuse `latest_texture` when no new source frame arrives. Those
repeated frames still produce encoder packets, so the packet-based watchdog can
look healthy while the picture remains frozen and audio advances. This is a
code-supported failure mechanism, but it is not proof that every external
black/frozen-video report has the same cause. Automatic restart is intentionally
deferred because a genuinely static desktop must not be treated as a stall.

The bounded health snapshot now reports real-source progress, WGC cached-frame
observations, encoder output, replay-ring acceptance and the age of each stage.
Deterministic policy tests separate a quiet DXGI desktop from WGC stale-source,
capture-to-encoder, encoder-output and replay-ring candidates. This improves the
next field bundle; it is not a claim that the external freeze is fixed.

Acceptance criteria:

- retain the implemented source/encoder/ring counters and bounded diagnostic
  classification without per-frame disk logging;
- add the remaining row-pitch/crop/scaling pixel-pattern tests;
- define a bounded recovery policy that distinguishes a static desktop from a
  failed capture source before enabling automatic restart;
- physically validate 1080p and 1440p capture on available NVIDIA hardware,
  followed by AMD, Intel and hybrid-GPU field validation.

## P1 — Windows input reliability

### F7/F8 and function-key hotkey persistence

Status: **FIX IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING**

F7 and F8 were valid registrations but were already assigned to hidden Game
Detection actions. Rebinding Capture Clip to either key therefore triggered the
correct duplicate guard and restored F9, while the conflicting action was not
visible to the user. `CONFIRM GAME` and `DISMISS GAME` are now visible and
editable in the Hotkeys popup. Deterministic coverage verifies F1-F12,
Alt+function-key conversion, save/reload persistence and explicit duplicate
errors. Reports involving keyboard firmware layers or another process owning a
global chord still require affected-system validation.

Acceptance criteria:

- every globally registered user-configurable action is visible in the popup;
- F1-F12 and Alt+F1-F12 survive save and application restart;
- duplicate F7/F8 assignments show the existing actionable conflict error;
- field-test a standard keyboard and a reported layer/remapping keyboard on
  Windows before marking the external report validated.

## P1 — Packaging and deterministic reliability

### AppImage checksum and build result — issue #6 / PR #35

Status: **IMPLEMENTED / REGRESSION TESTED — FULL APPIMAGE QUALIFICATION PENDING**

The Linux build now requires `sha256sum`, writes a checksum beside the locally
built AppImage and reports both paths without the truncated output seen in
the community build. This closes the checksum/output part of issue #6, but it
does not close the glibc or physical launch matrix.

Acceptance criteria:

- the `.sha256` file contains the AppImage hash and portable basename;
- shell syntax and the packaging contract test pass on Linux;
- the locally built artifact and checksum are verified together in CI without
  uploading the AppImage to GitHub.

Tracking:

- https://github.com/FTHR-Community/FTHR-Clips/issues/6
- https://github.com/FTHR-Community/FTHR-Clips/pull/35

### Bundled FFmpeg CLI RPATH — issue #30

Status: **IMPLEMENTED / REGRESSION TESTED**

The behavior from community commit `6cf020a` is integrated: bundled `ffmpeg`
and `ffprobe` use `$ORIGIN/../lib` without depending on an inherited
`LD_LIBRARY_PATH`. The fetcher verifies the pinned manifest before patching the
two executable files and leaves the shared-library bytes unchanged.

Acceptance criteria:

- both executables contain the expected relative RPATH;
- both run `-version` with `LD_LIBRARY_PATH` removed;
- the pinned shared-library bytes and manifest hashes remain unchanged;
- the build fails early with a clear message when `patchelf` is unavailable.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/30

### Hardware-dependent encoder timestamp test — issue #17

Status: **IMPLEMENTED / REGRESSION TESTED**

The generic timestamp contract test now forces the software encoder. Physical
VA-API qualification remains a separate test and does not make the deterministic
test initialize a real AMD or Intel GPU. The pinned-FFmpeg native suite passes;
VA-API was correctly skipped in WSL because no physical render device exists.

Acceptance criteria:

- the timestamp contract test runs without a GPU device;
- the full Linux native test suite passes;
- VA-API remains covered by its dedicated capability/physical test.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/17

### KDE shortcut setup guidance — PR #36

Status: **IMPLEMENTED / REGRESSION TESTED**

KDE users are now directed to the current Plasma path under Keyboard and
Shortcuts when FTHR cannot register a global key automatically. This is a
guidance fix only; it does not claim that the XDG GlobalShortcuts integration
from PR #16 is ready. The Linux startup report also labels compositor helpers
as `feature-only`, so a KDE user is no longer told that missing `hyprctl`
blocks the application.

Acceptance criteria:

- both the warning and generated setup instructions use the current KDE path;
- generated commands still target the private per-user FTHR socket;
- Linux hotkey instruction tests pass.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/pull/36

### Current native AppImage qualification — issue #6

Status: **PARTIAL — MATRIX INCOMPLETE**

Keep the redacted Linux report and checksum workflow, but do not call the
AppImage native-qualified until the following matrix has recorded real results:

- Ubuntu/Debian and one rolling distribution;
- X11 plus Wayland on GNOME, KDE and Hyprland where supported;
- NVIDIA, AMD and Intel encoder paths;
- PulseAudio/PipeWire desktop audio;
- FUSE launch and extract-and-run fallback;
- replay save, screenshot, hotkey and multi-monitor checks.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/6

### Linux optional uploader — issue #18

Status: **CURRENT REPORTED IMPORT CRASH IS STALE; PRODUCT SUPPORT INCOMPLETE**

The exact missing-constant crash belongs to an older generated artifact and is
not present in this source tree. The current uploader path is still Windows-
oriented, however. Before enabling upload in a Linux alpha, either port and
validate the consent-gated Linux plugin bundle or explicitly disable the
unsupported action with an honest UI message.

Acceptance criteria for enabling it:

- the Linux plugin is built, hash-bound, extracted below `XDG_DATA_HOME` (or
  `~/.local/share`) and its entry point is executable;
- plugin IPC and consent checks pass without weakening the existing boundary;
- AppImage startup succeeds when the optional plugin is absent.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/18

## P2 — Product behavior

### Upload the edited result — issue #34

Status: **ROADMAP — NOT A SAFE ONE-LINE FIX**

Upload currently receives the original clip path while edited output is created
by a separate asynchronous export. The correct implementation must export a
validated non-destructive result first, enqueue exactly that file once, and
surface export or upload failure without changing the original clip.

Acceptance criteria:

- an edited clip uploads the validated exported copy;
- an unedited clip can still use the original fast path;
- export failure never starts an upload;
- repeated clicks cannot create duplicate export/upload jobs;
- deterministic tests record the actual path given to the uploader.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/34

## Superseded report

### Legacy `Colors.BG_CARD` launch crash — issue #14

Status: **OLD 1.0.0 ARTIFACT — VERIFY BEFORE CLOSING**

The referenced symbol is not used by the current source. Close the report only
after a newly built AppImage passes a frozen-bundle startup smoke test; source
inspection alone is not release evidence.

Tracking: https://github.com/FTHR-Community/FTHR-Clips/issues/14

## Separate follow-up work

- XDG GlobalShortcuts/portal hotkeys from PR #16 require a clean rebase and
  security/packaging review. Do not copy the currently dirty PR wholesale.
- KWin screenshots still need a portal-compatible path. Working replay capture
  does not prove that the separate `grim`/Qt screenshot fallback is correct.
- Linux audio timing changes require a real A/V synchronization measurement in
  addition to deterministic unit tests.
- Community fixes do not alter the unresolved asset/license provenance gate.

## Current Windows stabilization backlog — 2026-10-10

This continues the existing roadmap, using the intentional local worktree on
`integrate/latest-project-source`, HEAD
`d990630f5befd0d19ddf371dddf1e39dd5f87c02`. No commit or push is authorized.
Code/regression completion is separate from physical and external-user
qualification. Existing packages and the running AV-Sync-Gate `-01` session
are preserved. Linux, per-app audio and broad editor redesign are out of scope.

### A — Viewer startup and paused seek

PARTIAL. Implemented source-scoped native callbacks and a durable paused-seek
destination across asynchronous source preparation. A bounded paused-only
target-frame decoder supplies a poster when the native backend acknowledges a
seek without presenting its frame; it never starts playback. Source/seek epochs
reject late frame-zero and in-flight software results. The existing neutral
native player/output remains attached. Twelve new regressions plus existing
Viewer/lifecycle checks pass (63 focused tests). A real Windows-Qt run on a
copied 1080p60 fixture painted the paused 20 s target before play in 625 ms;
its image exactly matched independently decoded frame 1200 (MAE 0). No
autoplay occurred. Cold construction took 1.71 s, READY 2.59 s and first poster
paint 2.73 s; first native delivery followed explicit play by 95 ms. This does
not prove that the reported first-seconds stutter is eliminated.

### B — Windows Videos default

IMPLEMENTED — PHYSICAL VALIDATION PENDING. New Windows installations and the
explicit USE DEFAULT action resolve Videos, then use its `FTHR Clips` child.
Existing settings keep custom/legacy roots; no media migration or silent fallback.
The existing Qt Windows implementation resolves MoviesLocation through
SHGetKnownFolderPath(FOLDERID_Videos); redirected locations are supported.
Folder preparation and settings persistence fail visibly and preserve the old
selection on ordinary failures. Derived recording paths follow the root;
explicit custom recording paths remain separate. Invalid paths remain repairable.
The worker's scoped suite passed 111 tests (one skipped); root independently
re-ran storage/settings/library/uploader/save tests successfully. Redirected
Videos and permission failure are deterministic tests, not physical relocation.
The prepared Settings signal now adopts the exact persisted root without a
second filesystem probe before adoption. A disappearing mount leaves settings,
folder edit and library on that same selected root, with an actionable error,
not an old-root fallback. Direct grid callers retain writable preflight. Raw
invalid custom recording settings are preserved for repair, not stringified.
Real Qt signal handoff and permission-loss regressions pass. An expanded
integration run exposed an ordering-sensitive thumbnail guard failure; the
test-owned worker cleanup in G addresses its demonstrated isolation mechanism.
The same nine-file storage/library/uploader set now passes 113 tests, one skip.

### C — Error classification and actual recovery

IMPLEMENTED — PHYSICAL VALIDATION PENDING. All 23 public meanings are unchanged.
Failure events retain technical cause, subsystem, operation identity and separate
process/backend generations, even with notifications muted. Keyed launch/capture
incidents clear individually only after repeated current-generation progress
spanning three seconds; failed saves and unrelated errors remain. Diagnostic
history is retained. Restart actions do not themselves dismiss the incident.
Queued startup callbacks and hardware timers now recheck exact process/bridge
ownership at delivery. Windows mapping initialization/shutdown is serialized,
and closing an old bridge cannot erase its replacement singleton. The integrated
focused suite passes 142 tests, including simulated WinError 4551, overlapping
errors, failed saves and deterministic replacement/mapping races. No engine ABI,
capture behavior or recovery policy is changed. Physical recovery remains pending.

The keyed encoder-init notice also now has a specific recovery rule. Only its
exact Error 001 / ENCODER_INIT_FAILED incident opts in; clearing requires a
newer actual engine process, the native hardware-active bit, strict HEALTHY
state and the same sustained advancing-frame window. Missing hardware evidence,
content suspicion, disconnects and generation/counter resets cannot clear it.
The legacy `nvenc_active` ABI field represents native hardware-encoder success
across vendors; these tests do not qualify physical AMD/Intel capture. Failure
and recovery events remain in history, even when UI notices are muted.

Integration lint also caught and corrected a new B startup regression: the
prepared clips root must remain bound for stale-partial cleanup. It was not an
unverified pre-existing failure.

### D — Error 015 and physical A/V evidence

PARTIAL. The `-03` static-replay candidate and its replay guards are preserved;
see `windows-static-replay-gate-2026-10-10.md`. Its deterministic static/source
interruption controls do not qualify a real DXGI/NVENC save. No new static or
moving native capture/diagnostic pair has been identified. Real F9 saves,
complete decode, replay coverage and source-versus-held timestamps remain
NOT RUN for that candidate. Do not substitute the historical f774 ZIP.

The existing combined-audio finalizer defect is reproduced and regression
tested; its connection to the historical Warzone desync is still UNVERIFIED.
Required next evidence: native separated-track flash/tone recording and
same-session ZIP, then byte-identical-copy production finalization and decoded
native/combined marker offsets near start/middle/end. Microphone synchronization
needs its own known reference event. Runner PASS is not physical sync proof.

### E — AMD and capture-side size boundary

PARTIAL — SOURCE REVIEW; AMD HARDWARE VALIDATION PENDING. The available machine
has an RTX 4060 Ti, not Radeon hardware. The current crop/RowPitch/scaler/AMF
source and output dimensions appear consistent by inspection; mock AMF tests
do not validate actual Radeon video processing or nonblack moving picture.

One remaining boundary needs qualification: WGC monitor initialization sizes
its pool from GraphicsCaptureItem.Size but retains source dimensions from DXGI
desktop-coordinate extents. Later exact frame/texture checks reject a mismatch.
Unlike window WGC, monitor WGC does not establish agreement at initialization.
Different API sizes alone do not prove a DPI bug. No speculative correction is
made: a scaled-monitor run must record both sizes and the exact rejected check.
Missing identity, scaling, black/uniform output, encoder stalls and recovery
remain separate failure classes; no dark/static-scene corruption inference.
AMD/NVIDIA physical core-function parity remains a P0 release prerequisite.

### F — Adjusted high-FPS preview

PARTIAL — LESS WASTED WORK; STILL SOFTWARE-LIMITED. A cold three-second local Windows
sample using one generated 1920x1080/120 FPS fixture with contrast 40 and
saturation 35 produced 48 unique software paints (~16 FPS), 80 completed
workers and 79 pending replacements, with 60.5 ms p95 frame age. Computing
results that never reach a paint is demonstrated; steady-state comparison is
still required before claiming an improvement. Neutral output bypassed all
software workers. Its sink callbacks are not proof of unique native paints.
No effects are disabled or silently changed; no GPU renderer rewrite is planned.
Clean repeated measurements, after compilation finished and with one second
of playback warmup excluded, confirm the waste: two five-second effect windows
completed 213/207 conversions but painted only 122/116 unique images (24.53/
23.20 FPS), p95 image age 54.61/57.44 ms. Neutral generated zero software jobs;
its sink/source timestamps were ~120 FPS, not measured display FPS. All steady
windows had zero heartbeat stalls. Initial Play still had 272–424 ms event-loop
outliers in both modes; startup stutter is not proven fixed by the seek changes.

The adjusted surface now bounds admission while its prepared image awaits a
paint; newer frames replace one pending request. Hidden/minimized surfaces do
not continue ordinary conversions, while forced paused posters remain allowed.
Source/seek/edit/shutdown transitions invalidate pending admission. A bounded
wakeup prevents a visible nonpainting surface from permanently blocking work.
The post-paint/new-arrival ordering race and repeated/empty-paint teardown are
covered by new regressions. This does not change effect values or the native
neutral renderer.

An identical saved harness compared two fresh-process pairs against an explicitly
reconstructed semantic pre-F control, retaining A's source/seek fixes. At the
same 10 ms event-pump setting, five-second adjusted windows started 329 versus
201 worker tasks in total (-39%). Mean unique software paint rate was 20.58
versus 20.03 FPS (-2.7%), and mean p95 image age 56.33 versus 54.09 ms. This is
an efficiency/latency trade-off, not a visible-FPS improvement. Neutral windows
had zero software jobs, normal ~5 s position advance and no heartbeat stalls.
Sink callback throughput is not measured native display FPS. Earlier unmatched
measurements, including a 12.64 FPS/90.18 ms run with a different pump, remain
retained; they are not substituted for this paired comparison. High-FPS adjusted
rendering and startup stutter still need further work; no large GPU rewrite is
performed in this sprint.

### G — Small date-section library regression

DONE — IMPLEMENTED AND TESTED. The named
`test_many_small_date_sections_do_not_reserve_offscreen_card_budget` passes
on the current unmodified baseline. It was included with the Viewer and
Stage 4 lifecycle baseline checks: 45 passed. Keep full-suite ordering and
worker-cleanup coverage; do not loosen assertions or expand Stage 7.

Two expanded runs also failed the strict partial-file thumbnail decoder guard.
A separate deterministic barrier reproducer showed that an unfinished initial
scan for a prior test's completed `old.mp4` can run under a later test's global
decoder monkeypatch. It proves this isolation mechanism, not the unidentified
filename from the original failure. The partial-file tests now own and shut
down every ClipGrid before monkeypatch teardown, then deliver deferred Qt
destruction. The original strict decoder assertion is unchanged; no production
thumbnail/library scheduling or virtualization architecture is changed. The
expanded nine-file set passes 113 tests, one skip; full-suite ordering remains
part of the final integration gate.

### H — Error and Windows troubleshooting documentation

DONE — IMPLEMENTED AND TESTED. The existing documentation page keeps
the exact 23 public four-field registry entries. Each card adds implementation-
supported causes, automatic attempts, user actions, when to export diagnostics
and a useful-ticket checklist. Search includes this guidance; rendering uses
text rather than interpolated HTML. Windows Basics covers the actual SOURCE /
Desktop / monitor / APPLY workflow, storage and explicit USE DEFAULT, microphone
100% unity / digital boost, same-adapter ownership, recovery and unqualified
hardware/DPI/Viewer/save boundaries. Diagnostic export remains local through
Settings / General / Troubleshooting and the existing DRAG ZIP handoff.
No website deployment or publication is performed. The focused documentation /
registry suite passes five tests; Node syntax and the Node VM rendering, search,
navigation and literal-text escaping checks pass. This is not a full graphical
browser qualification.

### Integrated Windows backlog test package

One new package is prepared only after the integrated gates, without overwriting
previous packages or stopping the running AV-Sync-Gate-20261010-01 application:

`dist/FTHRClips-Windows-Backlog-20261010-1ef261a0/FTHRClips/FTHRClips.exe`

It uses a fresh current-worktree Release x64 engine and playback mixer; the
private temporary spec selects those exact artifacts. The package manifest will
record executable hashes, branch/HEAD and dirty source identity. This is a
diagnostic/test handoff, not a release-ready build. Original media, live settings,
the production combined-audio finalizer and FTHR_SharedMemory_v4 remain intact.
Linux/RPATH and asset/licence status are unchanged by this Windows sprint.

Integrated gates on the final actionable source: full Python suite 1,262 passed,
45 skipped, zero failures (78.09 seconds; three deprecation warnings). No tests
were deselected. The fresh current Windows native suite passed 20,086 checks;
Release x64 engine and mixer builds succeeded. Ruff, Python compilation, Node
syntax, shared-memory/engine-response/exception/version contracts and diff
whitespace checks pass. The exception allowance remains 92, not an increased
baseline. Source licence/asset verification passes 107 checks with zero failures
and three Linux-on-Windows / absent-Linux-engine warnings. This does not change
the existing legal/asset approval status or qualify Linux execution.

Latest-package whole-application startup/capture/save and decoded native-versus-
combined A/V tests remain NOT RUN: the older live session is intentionally not
stopped or mistaken for this new package. Static/moving F9 clips and retained
source/held-frame evidence, matching diagnostics, AMD physical parity, Intel /
hybrid hardware and mixed-DPI monitor WGC qualification remain open. The local
real Windows Qt Viewer measurements are narrower evidence, not capture tests.
The package-local manifest records packaging checks and exact binary identities.

## Windows master stabilization sprint — 2026-10-10

This section supersedes neither the historical evidence nor A–H above. It records
the next integration on top of those completed changes. No commit, push, settings
reset, media replacement, Linux work or public release is authorized/performed.
Starting branch `integrate/latest-project-source`, HEAD
`d990630f5befd0d19ddf371dddf1e39dd5f87c02`; 268 intentional status entries,
275 expanded file identities, zero staged files. Recoverable copies and the
per-file identity record are in
`%TEMP%/FTHR-master-sprint-5e405f7fd7b3469c953048ce33c4263f/baseline`.
The completed backlog package is `FTHRClips-Windows-Backlog-20261010-1ef261a0`.
The observed running UI is still **AV-Sync-Gate-20261010-01**, not that package;
PIDs 10396/27568 were deliberately not stopped. A disk hash is not a historical
session's recorded hash. The final candidate identity/gates are recorded below
and in its package-local BUILD-MANIFEST.json.

### Research → code decisions

All sources accessed 2026-10-10. Paths below are repository-relative. No SDK,
FFmpeg, Qt or driver upgrade is made. Packaged PySide/Qt is **6.11.1**, NVENC
header/API **13.0**, Windows FFmpeg **n8.1.2-21-gce3c09c101**. Online Qt 6.11
documentation now identifies 6.11.2 and is not patch-exact 6.11.1 evidence.

| Evidence class / primary source | Contract and exact applicability | Local path / decision / verification |
| --- | --- | --- |
| DOCUMENTED API CONTRACT: [FFmpeg AMF at ce3c09c101](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavcodec/amfenc.c), [AMF hwcontext](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavutil/hwcontext_amf.c) | The supplied D3D11 device initializes AMF; a retained AVFrame references the input until output. Queue pressure/drain is not necessarily immediate 1:1 output. | `ffmpeg_amf_replay_encoder.cpp`, Initialize/SendGpuFrame/ReceivePacket/Flush; preserve ownership and add delayed-output, drain, error and restart regressions. These are mock-wrapper tests, not Radeon picture qualification. |
| DOCUMENTED API CONTRACT: [FFmpeg configure at the pin](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/configure), [AMF v1.4.36](https://raw.githubusercontent.com/GPUOpen-LibrariesAndSDKs/AMF/v1.4.36/amf/public/include/core/Version.h), [GPUOpen AMF](https://gpuopen.com/advanced-media-framework/) | Minimum build headers are 1.4.36; this is not an exact universal runtime/driver floor. Current 1.5.3 release guidance is newer than the package. | `tools/ffmpeg_manifest.json`, bundled ffversion.h, native codec open/capability reporting. Require actual codec/runtime/adapter results; no vendor-model hacks or mandatory latest-driver inference. |
| DOCUMENTED API CONTRACT: [Microsoft video-processor input](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11videodevice-createvideoprocessorinputview), [FFmpeg D3D11 context at the pin](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavutil/hwcontext_d3d11va.c) | DEFAULT non-MSAA inputs may have BindFlags=0. Hardware frames carry texture plus array slice, not CPU plane strides. | AMF owned eight-slice BGRA pool and converter output RENDER_TARGET inspected. Do not speculate that zero input bind flags cause black AMD frames. Scaled/nonblack Radeon output remains VALIDATION PENDING. |
| DOCUMENTED API CONTRACT / PLAUSIBLE HYPOTHESIS: [NVENC 13.0 §§6.1–6.3](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html) | Guide discusses AcquireNextFrame and bitstream operations sharing a DirectX device across threads. Its async and threading sections differ on doNotWait guidance. | `hardware_encoder.cpp` Initialize/RetrieveOutput/DrainThread and `capture_engine.cpp` CaptureThread share the device. No reproduced deadlock/root cause; no blanket locking, serialization or Flush change. Retain this review lead. |
| DOCUMENTED API CONTRACT: [AcquireNextFrame](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe), [frame metadata](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/ns-dxgi1_2-dxgi_outdupl_frame_info), [ReleaseFrame](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-releaseframe) | Timeout/pointer-only is not a new desktop image; borrowed pixels cannot be used after releasing the frame. | CaptureThread copies to owned texture before release; existing -03 held presentation remains distinct from source progress. No pacing, timestamps or Error-015 thresholds changed. |
| CONFIRMED LOCAL DEFECT: [WGC target/lifetime contracts](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture) | A selected WINDOW is not equivalent to an ungated full desktop. Current failed-window fallback erased HWND or protected-game focus gating; DXGI has no matching window/foreground contract. | CaptureEngine::Initialize: fail the unavailable Window request explicitly; Desktop Auto keeps exact-monitor WGC→DXGI fallback. Test pure startup decision matrix and production wiring. |
| DOCUMENTED API CONTRACT: [optional IsBorderRequired](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession.isborderrequired?view=winrt-26100), [WGC source time](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframe.systemrelativetime?view=winrt-26100) | Optional border interface is newer than capture. WGC has compositor source time; FTHR presently observes post-poll QPC instead. | InitializeWGC/ApplyCaptureBorderPolicy/CaptureThreadWGC. Border-policy rejection is not proof WGC is unavailable. Timestamp provenance and monitor item.Size vs DXGI extents are open boundaries, not proven A/V/DPI causes; do not change encoder PTS here. |
| DOCUMENTED API CONTRACT: [WASAPI GetBuffer](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiocaptureclient-getbuffer), [default endpoint](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immdeviceenumerator-getdefaultaudioendpoint) | Packet QPC is 100 ns, not raw QPC ticks; invalid timestamp/device/service/endpoint failures differ. | Existing audio capture/provider/timeline paths inspected; preserve gain/continuity and field errors. No generic resampler/offset repair; endpoint/speech/device-change physical evidence remains separate. |
| DOCUMENTED API CONTRACT: [pinned amix](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavfilter/af_amix.c) | Mixing must preserve per-input timestamp origins, not infer sync from equal stream starts/durations. | `combined_audio_policy.py` and production finalizer's existing origin normalization retained. Its controlled regression is not proof of historical Warzone content sync. |
| DOCUMENTED API CONTRACT: [Qt Multimedia](https://doc.qt.io/qt-6.11/qtmultimedia-index.html), [MFStartup](https://learn.microsoft.com/en-us/windows/win32/api/mfapi/nf-mfapi-mfstartup) | Module/bundle import, selected media backend, OS API and codec availability are different dependencies. MFStartup can fail when media components are absent. | `main.py` selects windows backend; optional sound/theme module imports QtMultimedia. Developer MFStartup/MFShutdown probe returned S_OK; this is not stripped-OS/playback qualification. Graceful whole-module absence remains open. No OS-edition rejection or universal Media Feature Pack advice. |
| CONFIRMED LOCAL DEFECT: [RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey), [Python 3.14 os.replace](https://docs.python.org/3.14/library/os.html#os.replace) | OS registration success is not persistence success. Existing set_hotkey returned success despite failed atomic save; empty unbind did not survive reload. | `core/hotkey_manager.py`: transactional failure result, restore previous keyboard/controller state, report failed restoration, preserve explicit unbind. New filesystem-failure tests reproduce the old defect. |
| TOOL APPLICABILITY: [RGD manual](https://gpuopen.com/manuals/rgd_manual/rgd_manual-help_manual/) | RGD consumes captured AMD .rgd artifacts for supported crash/API paths; an MCP cannot create absent dumps or make D3D11/AMF supported. | Local Suite-2026-05-28 archive and MCP 1.0.0.2 README inspected (requires RGD≥1.6.3). No matching Radeon/dump; CLI exact version not executed. No install, TDR, driver reset, system-wide trace, upload or D3D12 rewrite. |

### Shipped / source / package / runtime reconciliation

| Issue and available identity | Classification / earliest demonstrated boundary | Current evidence and next proof |
| --- | --- | --- |
| f7746a8b, Error 015, old AV-Sync package path, ZIP has no historical engine hash | OLD BUILD — RELATED LOCAL FIX EXISTS, RETEST PENDING; STALE_END before writer with 20.4167 s packet gap | -02 diagnostic corrections and -03 static presentation candidate exist; latest physical static/moving F9 saves and full decodes NOT RUN. Do not call a measured-image rate 0 when evaluation aborted before calculating it. |
| AMD RX 570/6800/6800 XT/7900 XTX startup/black/scaling reports; exact historical hashes mostly absent | INSUFFICIENT BUILD IDENTITY / HARDWARE VALIDATION REQUIRED | Current exact adapter, AMF 8-bit SDR, owned surfaces/keyframes/cadence are source-audited; no proved Radeon pixel root cause. New single package for affected users; field validation pending. |
| NVIDIA 4551 external RTX3090/AMD iGPU, Windows11 26200.9278 | INSTRUMENTED — FIELD ROOT CAUSE UNVERIFIED; failing API not yet supplied | Preserve 4551/hex/symbol/API/topology. ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION is classification, not proof of security software, DXGI, WGC or CreateProcess cause. |
| Old laggy/stale clips and ACCESS_LOST reports | OLD BUILD / INSUFFICIENT BUILD IDENTITY depending on report | New cadence/source/held/encode/ring diagnostics available. No new physical NVENC-root-cause claim; latest 60/120/144 FPS production F9 gates pending. |
| Native/combined Warzone A/V report | INSUFFICIENT MATCHING CONTENT EVIDENCE | Controlled different-audio-origin finalizer defect fixed earlier; historical Warzone cause and native physical marker comparison remain unverified. Microphone needs a separate known reference event. |
| Viewer/storage/keyed recovery/troubleshooting | LOCAL FIX / REGRESSION TESTED in completed A–H backlog | Packaged code verified earlier; latest package GUI/manual keyboard/saved capture still separate. Startup/effected high-FPS performance limits remain documented, not erased. |
| Window silently becomes desktop, failed hotkey persistence, failed camera settings start | CURRENT CODE DEFECT — FIX IMPLEMENTED / REGRESSION TESTED | These are code-boundary fixes, not resolution of all external AMD/NVIDIA failures. |

### Windows exposed functional contracts

This is a source/control inventory, not a physical PASS matrix. Coverage means
the named test area exercises the relevant logic; it does not imply every real
state/hardware combination was executed. All listed capture/save/playback
features still require a latest-package manual gate. Real uploads, deletions,
theme imports and account operations were not run on user data.

| Visible control / promise | Handler → persisted validation → backend/result/status/error | Prerequisite / regression area / open boundary |
| --- | --- | --- |
| F9 / normal clip save, selected 5–300 s | HotkeyPopup/HotkeyManager → MainWindow._on_hotkey_save_clip/_save_clip → CaptureHealth admission/SaveState → native SaveClip/health/transactional writer → written/finalizing/ready or Error015 | Connected healthy generation/keyframe/coverage; save_state/save_event_loop/replay policy tests. Latest real save/decode/rollover/20-save run pending. No separate extended-replay control/action is exposed in this Windows source. |
| RECORD / start-stop shortcuts; separate recording profile | _toggle/_start/_stop_manual_recording → requested capture profile and recording path → bridge Start/StopRecording/native ContinuousRecordingWriter → response pump/finalization → saved or failure | Windows encoder/audio/output readiness; capture_settings/recording contracts. Pause/resume is not a manual Windows control: focus-pause is unsupported on Windows alpha, not promised as working. |
| Screenshot shortcut | _on_hotkey_save_screenshot → selected persistent monitor resolve/reserved paths → Windows grab/screenshot writer → _publish_screenshot or actionable failure | Correct monitor, writable output; screenshot_target/save tests; physical selected monitor/DPI pending. |
| SOURCE Desktop/window/monitor/APPLY | SourcePopup/_requested_capture_config → persistent monitor/HWND/settings → Initialize/Apply tracker → actual topology/health → precise failure | Windows interop/active target/adapter. No hidden desktop substitution on Window failure; monitor/backend policy tests. Desktop Auto remains fallback-capable. |
| Game detection, accept/dismiss, custom crop rules | GameDetector/_on_game_detected/_on_confirm_game_detection/_on_custom_game_rules_changed → stored mode/rules → new capture generation | Foreground/game/window capability; game_detector/focus tests. Anti-cheat/privacy restrictions not bypassed; real game/alt-tab pending. |
| Clip/recording FPS, resolution, bitrate/quality | CaptureSettingsPopup profile handlers → CaptureConfig validation/requested-active tracker → native dimensions/FPS/bitrate | Current UI policy accepts 15–240 FPS, not proof of 240 FPS hardware. capture_settings/popup/native bounds tests; actual 120/144, 1440p/scaling pending. |
| Encoder/codec/NVENC preset/APPLY/Refresh | SettingsPage._start_encoder_probe/_sync_encoder_controls/_on_encoder_apply → encoder capability cache/settings → actual codec open | Supported runtime/selected adapter, actual dimensions; encoder_capabilities/codec/native wrapper tests. Cached/capability success ≠ moving-frame AMD/NVIDIA parity. |
| Scaling/crop/output canvas | persisted crop/scaling CaptureConfig → source geometry/video processor → encoder dimensions | capture_scale_geometry/AMF/QSV mocks; no continuously decoded corruption detector. Mixed-DPI WGC monitor size agreement unqualified. |
| System audio and separated/combined checkbox | _save_audio_settings → normalize_audio_capture_mode/separate_audio_enabled → native stems/sidecar → optional production finalizer | Endpoint/runtime; audio_manifest/combined_audio_policy/native timeline tests. Separation setting must be true for physical native A/V test; defaults/current live session are Combined. |
| Mic device/mute/gain/monitor loopback | endpoint discovery/_on_mic_device_changed/_on_mic_volume_changed → persisted endpoint and unity/boost policy → native mic/preview provider | Permissions/endpoint/source; windows_microphone_devices/microphone_gain/audio tests. No spoken-mic/reference/device-change physical claim. Gain100%=unity; boost warning retained. |
| Webcam/device/overlay position-size | SettingsPage camera handlers → device/index/enable/rect → CameraRecorder async result and bounded preview → overlay finalization | Optional camera/provider; camera_recorder and new camera failure tests. Initialization failure must be visible and not leave enabled status; physical camera/media sync pending. |
| Image/keyboard/Gary overlays and preview | _on_image_overlay/_on_keyboard_overlay/_save_gary_settings → stored validated layers/rects/sensitivity → existing frame/input/audio observations and finalization | Appropriate input/device/file availability; overlay/keyboard/camera/gain tests. No overlays disabled for performance claim; user-content assets not bundled/licence-approved by this audit. |
| Keyboard/controller rebind/conflicts/unbind | HotkeyPopup → set_hotkey normalization/duplicate/OS registration → atomic config → action dispatch | Native target/input/controller; native_hotkeys/visibility/migration/new persistence tests. Filesystem/registration failure rolls back and reports, not fake success. Real F9 E2E still pending. |
| Viewer open/first image/play/pause/seek/speed/audio stems | ClipGrid._on_clip_opened → ClipViewer source/seek generations → Qt windows backend and optional FFmpeg/QAudioSink mixer → READY/playing/error | Qt/plugin/media/runtime; stage4/startup_seek/proxy/mix tests. Paused poster measured in A; physical encoded capture playback and mic sync separate. Known mixer limits remain. |
| Viewer ESC/close/fullscreen/window/DPI | viewer close/shutdown/fullscreen handlers → owned helpers/player/mixer cancellation → window lifecycle | stage4_player_lifecycle/windows_dpi_windowing tests; invisible playback report field validation pending. Native presentation FPS is not sink callback FPS. |
| Viewer color adjustments | LiveVideoPreview bounded latest-only admission → worker conversion → unique prepared image paint | preview backpressure/P1 tests; same-workload A–H measurements show fewer jobs, NOT FPS improvement; cold startup/adjusted120 FPS unresolved. |
| Trim/export/preset/quality/cancel | _build_export_cmd/_validate_export_output → bounded ExportLifecycle/task/subprocess → staged output/full-decode check → complete/failure/cancel | Bundled tools/encoder/storage; export_profiles/stage4/clip_viewer_audio_export tests; real user media export not exercised this sprint. |
| SHARE/upload/link/cancel/retry | clip share click/export → upload manager/optional plugin → bounded subprocess/status → URL or failure | Explicit consent/auth/service/plugin/config; uploader/post_route tests use doubles. No account/media sent. Network success not implied by tests. |
| Delete clip and linked-import behavior | ClipCard._confirm_delete → confirmation/file-sidecar operation → ClipGrid._on_clip_deleted/cache update | clip_files/library tests sandbox only; no user clips deleted. |
| Import folder/scan/filter/sort/refresh | _on_import_folder_add/_on_import_folder_scan → stored explicit roots → generation-aware batched LibraryIndex/ClipGrid → counts/cards/errors | library scalability/cache/partial tests; user D:\/125 GB scan not rerun; no arbitrary filenames exported. |
| Open folder/Explorer/exported paths | _open_recordings_folder / card open/link actions → resolved current persisted directories | Valid selected/custom paths; Windows storage tests. No implicit migration or silent old-root fallback. |
| Storage browse/USE DEFAULT/migration | _on_clips_folder_browse/_on_clips_folder_use_default → actual Windows Videos known folder, writable prepare + atomic settings → exact new grid root | Windows storage/settings/partial tests; existing/custom folders retained; redirected-folder physical test pending. |
| Startup/background/tray/single-instance/full exit | main/SingleInstance/tray handlers → persisted lifecycle → owned engine/UI/card/jobs shutdown | single_instance/background/lifecycle tests; old live session not stopped. New packaged startup/end-to-end migration pending. |
| Error recovery / diagnostics export | exact incident key + sustained process/generation progress → keyed ErrorBar; Settings General Troubleshooting → local bounded ZIP | recovery/engine_diagnostics/privacy tests; no upload or media in diagnostics; restored status does not prove saved content. |
| Theme typography/colors/icons/sounds/import-export | CustomizePage → ThemeManager validated JSON/ZIP/assets → applied QSS/sounds/font registration | theme_typography/alpha_ui tests. No visual redesign; real user theme import/export not executed. Whole-app settings import/export is not an exposed separate control; preset save/load/delete is. |
| Preset save/load/delete | SettingsPage._on_preset_save/load/delete → PresetsManager alpha filter → settings/capture configuration | tests/preset/capture policy; retired multiband/auto-crop cannot be reenabled by old presets. No new multi-audio feature. |

### Qualification and performance boundaries

| Area | Current verification class / required next evidence |
| --- | --- |
| NVIDIA developer RTX4060Ti / Windows10 Pro19045 / driver32.0.16.1656 | Local source and deterministic gates; new package moving1080p60/static/repeatedF9/full-decode/A/V NOT RUN until old app is closed normally and new session is bound to package. 120/144/scaling/recovery separate cases. |
| AMD supported H264, same-adapter SDR; other codecs only after real capability open | SOURCE AUDIT COMPLETE; deterministic wrapper regressions do not prove pixels. AMD HARDWARE VALIDATED/FIELD VALIDATED remain pending. Prepare field handoff, not an SDK requirement for testers. |
| Intel / hybrid / mixed monitors-DPI / HDR | HARDWARE VALIDATION REQUIRED. No cross-adapter transport or HDR support inferred from 8-bit BT.709 SDR path. |
| Windows11/N/modified OS | Runtime API/plugin/endpoint capability requirements recorded; physical these combinations NOT RUN. Local MFStartup/MFShutdown=S_OK, not broad OS qualification. |
| Save health/static replay/system & mic content sync | Static/pipeline tests and existing finalizer regression retained; latest production save/decoded visual-system markers NOT RUN. Microphone UNVERIFIED without reference. |
| Performance | No new before/after recording-performance claim in this sprint. Earlier equivalent A–H Viewer control is retained: conversion jobs329→201 (-39%), unique paints20.58→20.03 FPS (-2.7%), p95 age56.33→54.09 ms. This is prior efficiency evidence, not current gaming overhead, capture-FPS or adjusted120 FPS proof. |

GPU encode/copy/3D percentages, game frametime impact, VRAM/handle soak, capture
and save p50/p95/p99 at identical workload are **NOT MEASURED this sprint**.
No performance change, quality/FPS downgrade, CPU readback, unlimited queue,
global Flush, real-time priority or timer-resolution shortcut is introduced.
One current candidate will carry every accepted fix; no current fixture/mock
result is substituted for native capture, keyboard F9 or external field evidence.

### Integration gates, final candidate and manual handoff

Completed on top of A–H, without speculative vendor/timing changes:

1. **Window source safety — FIX IMPLEMENTED / REGRESSION TESTED.** A failed
   WGC Window request cannot erase its HWND and become full-monitor capture;
   a protected game's failed focus-gated WGC capture cannot become ungated
   DXGI. Failure preserves native API context and explains the explicit Desktop
   alternative. Desktop Auto retains exact-monitor WGC→DXGI fallback. The
   test-only backend preference isolates DXGI/WGC without altering the shared
   memory contract, UI defaults or timing. Ten native startup-policy checks
   pass. The smoke runner now rejects missing/empty clips, strictly decodes all
   available audio plus video, records each failure and never substitutes a
   different forced backend. Its 12 Python tests pass. Real capture NOT RUN.
2. **Hotkey persistence — FIX IMPLEMENTED / REGRESSION TESTED.** Six failure
   tests reproduced the previous false-success behavior. Atomic-write failure
   now returns failure, restores the previous keyboard/controller state and
   keeps the old file. Failed native restoration is visible. Explicit keyboard
   unbinding survives reload instead of reverting to the default. No key/F9
   was injected into the live application. Final integration review also
   reproduced the UI filter dropping these non-public warning titles; a narrow
   Windows warning route now shows the bounded message and OPEN HOTKEYS action,
   respects notification muting and preserves the coded-error/non-Windows route.
3. **Camera settings — FIX IMPLEMENTED / REGRESSION TESTED.** Failed current
   asynchronous opens disable/uncheck the camera, clear the preview and show
   a bounded actionable warning through the existing ErrorBar. Qt signals keep
   changes on the UI thread; superseded/disabled/hidden-page callbacks cannot
   mutate the current page. Hiding settings does not disable capture intent.
   Eight focused tests pass; physical camera/device/saved overlay NOT RUN.
   Provider-level hangs/exceptions without a callback are not claimed solved.
4. **AMF regression gap — REGRESSION TESTED, no production AMF patch.** Tests
   exercise delayed output, borrowed packet bytes, backpressure drain/retry,
   EOS-only output, receive/flush failure and fresh-generation PTS/config/
   keyframe ownership in the actual wrapper with injected sessions. The native
   AMF area passes 170 checks. This does not simulate real GPU surfaces or prove
   nonblack moving Radeon output, driver cancellation or vendor parity.

Final integrated Python suite: **1,292 passed, 45 skipped, zero failures/errors,
zero deselection**, 91.892 s; three deprecation warnings. The prior 1,288-pass
run is retained separately; it preceded the final hotkey warning wiring tests.
Fresh Release x64
native tests: **20,120 checks**, including 170 AMF and ten backend-policy checks.
Engine and playback mixer builds succeeded. The first isolated test-binary
launch lacked the vendored FFmpeg DLL search path (0xC0000135); rerunning with
that dependency path succeeded. This was a test-output deployment issue, not
a reproduced product startup failure. Compiler warnings are retained in the
logs (vendored FFmpeg conversions and MSBuild temporary-directory warning).

Ruff, compileall, Node syntax, shared-memory/engine-response/exception/version
gates and git diff whitespace checks pass. Engine-response scan covers 106
sources; exception allowance remains **92/92**. Source licensing gate passes
107 checks, zero failures and three Linux availability warnings. Package-level
gate and exact frozen-code/DLL identities are recorded in BUILD-MANIFEST.json;
technical checks are not a new legal/signing approval.

One new private test candidate, not a public release:

`C:\Users\Tom\Documents\FTHR-Clips-master\dist\FTHRClips-Windows-Master-20261010-5e405f7f-02\FTHRClips\FTHRClips.exe`

Its TEST-README.md and BUILD-MANIFEST.json bind the dirty source fingerprint,
fresh native engine/mixer, bundled runtime/tool hashes and executed gates.
The old running application and all previous candidates/media/settings remain
untouched. The current candidate has not been launched as the full GUI or used
for production F9 capture: **NOT RUN**, not a startup/save PASS.
The earlier unsuffixed integration package is retained with SUPERSEDED.txt;
it lacks the final warning-route correction and is not the handoff candidate.
Only the -02 package above is intended for the field handoff. Initial component
verifier failures (archive entry/interpreter import resolution) are retained,
not concealed or described as proof of a product startup failure.

### Short AMD field handoff

Close the old app normally, including the tray instance; keep existing settings
and recordings. Run only the new executable above (send the entire FTHRClips
folder, not just the EXE; no Python/SDK/Visual Studio is required). Preserve the
previous failing build/session identity. Begin with **SOURCE → Desktop → intended
monitor → APPLY, 1080p60, H.264**, requesting AMD/AMF on the same AMD adapter;
the existing Auto backend may use WGC or exact-monitor DXGI and the diagnostic
report must identify the actual backend. Forced DXGI/WGC remains a developer
qualification harness option, not an exposed field UI switch.

Use 30-second replay, known continuously moving gameplay, warm up for at least
30 seconds and press F9 three times several seconds apart. Check the selected
monitor, visible movement and picture size; note every attempted/failed save.
Export **Settings → General → Troubleshooting → EXPORT DIAGNOSTIC REPORT** even
if successful. Return GPU/driver, old/new build, requested/actual backend/encoder,
attempts/successes and reasons. Request a clip/sidecar only with explicit consent
for strict full decode, temporal coverage and A/V review. Do not delete settings,
disable Windows security or silently reduce quality. No package is sent to a
tester and no field result is claimed by preparing these instructions.

### Grouped manual gate / promotion blockers

- **Source/save — VALIDATION PENDING:** current package startup/migration; modest
  same-adapter NVIDIA and AMD moving/static controls, correct monitor, repeated
  F9, manual recording, full decode/content coverage and held-vs-source evidence.
  AMD local physical testing is BLOCKED — AMD HARDWARE REQUIRED, but the packaged
  handoff is available for affected users. Latest NVIDIA physical run is NOT RUN.
  Supported 120/144 FPS, scaling, alt-tab/device loss and soak are separate cases,
  not prerequisites imposed on every first AMD tester.
- **A/V — VALIDATION PENDING:** separated native flash/tone capture with retained
  sidecar/session; byte-identical copy through the production finalizer; decoded
  start/middle/end marker differences and uncertainty. System audio remains
  unmeasured in the latest run; microphone UNVERIFIED without its own event.
  Warzone cause, WGC source-time provenance and signed/clamped origins remain
  separate open hypotheses; no blanket delay/resampler fix is applied.
- **Controls/compatibility — VALIDATION PENDING:** Viewer seek/play/ESC/fullscreen,
  active effects versus neutral high-FPS, new hotkey persistence across restart,
  optional camera failure/success, storage errors, diagnostics and Windows11/N/
  modified systems. Whole optional Qt-module absence and mixed-DPI WGC monitor
  dimensions still require separate qualification/implementation where proven.
- **Performance — NOT MEASURED this sprint:** identical-workload game frametimes,
  GPU encode/copy/3D and VRAM, save p50/p95/p99, source/encode/ring cadence and
  handle/memory soak. No new recording-speed/CPU-saving claim is made.
- **Release:** AMD/NVIDIA core parity, latest production-save/content sync,
  unresolved field reports (including 4551), and independent signing/asset/legal
  approval prevent promotion. Backend release ready **NO**. Linux/RPATH and
  asset/licence status remain unchanged. No commit, staging or push.
