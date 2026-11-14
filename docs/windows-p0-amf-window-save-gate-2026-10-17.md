# Windows P0: AMF submission, source authority, WGC lifecycle and save outcomes

This is a targeted local implementation/qualification record, not a release
approval. No commit, push, publication, Linux changes or shared-memory ABI
changes are authorized. External reports remain field-validation pending.

## Source and preservation

The user selected the byte-exact ZIP/current `windows` snapshot as the basis:
`840c00fc15f3830b54b37eb614ee4dbabcc0a418`, tree
`ef8f2fcf1fd3c2a170aa6b55368fea48211fcbac`.

The isolated source lives at
`C:\Users\Tom\Documents\FTHR-Windows-P0-AMF-20261017-01\source`.
Its original 1,064 files came from
`FTHRClips-2.0.0-alpha-Source-2026-10-17.zip`, SHA-256
`21DF2ADC0A08D7F899825FEF41E4A6B7CA3C060FA052AF450D04C4765FAE45ED`.
Local Git info/attributes preserve the raw ZIP line endings; source attributes
are not rewritten. The original SOURCE_MANIFEST describes that baseline, not
the subsequently modified candidate.

`C:\Users\Tom\Documents\FTHR-Clips-master` is not the implementation target.
It remains on `integrate/latest-project-source`, HEAD
`d990630f5befd0d19ddf371dddf1e39dd5f87c02`, with 565 pre-existing worktree
entries and no staged files. Its original source hash inventory is retained
outside both repositories. Existing media, settings and packages are preserved.

## Verified field evidence

The three original archives were validated/extracted separately. Analysis and
the session-aware event projection remain outside the source repository.
The duplicate `(1)`/unsuffixed first download has identical bytes.

| Archive/session prefix | ZIP SHA-256 | Evidence |
|---|---|---|
| 20261017-184009 / ac5660c3 | 38E0F3C2D009D22B9B1F5956C45E40EBFD8E62BF6550892334548FEA837AD73F | 25 native SubmitInput error-18 messages; 24 structured encoder runtime failures |
| 20261017-193557 / c6249f4d | 5481B7819EA5BC6EB0D3FD31E6305117A7B93A4DB3492CD5FD940B85C3B3B51B | No SubmitInput error 18 in this session; WGC size mismatch and partial/rejected saves |
| 20261007-224543 / 1e85ca56 | 11ED51041F9F848C3F9CA303B0E4D24F3314C5D70BBCF7F89F9B89E7BE6F272F | Startup failed on missing persistent monitor identity, before AMF submission |

Both current reports identify an AMD Radeon(TM) Graphics adapter, driver
31.0.21923.11000, and DISPLAY1 at 1920x1080/144 Hz. WMI's VRAM field is not a
reliable dedicated-VRAM measurement. Capture/encoder LUID 0:68345 agrees where
recorded. The recorded engine path is
`C:\Program Files\FTHRClips\_internal\engine\FTHRClips.exe`.
The reports do not record its SHA-256: today's disk hash cannot identify that
historical binary. Current app version is 2.0.0-alpha; historical is 1.1.0-alpha.

### ac5660c3-1fb3-4734-b4a6-a32506843868

32 native engine starts and repeated fresh engine generations occur. The 25
native AMF failures comprise 23 H.264 and two HEVC submissions, after successful
initialization, on monitor and window paths. Initialization/handshake alone
therefore did not prove working content. The underlying DirectX operation is
not identified by the native enum alone. Administrator rights are not proven
to be a cause or remedy.

All four requested 240-second saves used **software-openh264**, not AMF:

| UTC request | Process generation / native task | Actual backend | Native video presentation duration |
|---|---|---|---|
| 16:34:08.880 | 43 / 1 | WGC_WINDOW | 3.68333 s |
| 16:36:42.422 | 49 / 1 | WGC_MONITOR | 2.85000 s |
| 16:36:57.805 | 49 / 2 | WGC_MONITOR | 18.25000 s |
| 16:39:17.284 | 57 / 1 | WGC_MONITOR | 1.73333 s |

These had native mux/post-finalization completion events and partial-history
notes. That is not proof of full duration, actual moving content, audio sync or
strict decoding; the corresponding original media was not supplied here.
Native task IDs restart with the engine: task 1 is not globally unique.

### c6249f4d-ac24-462e-a723-61914951b389

Seven 45-second requests: first two were rejected before the writer with
LOW_EFFECTIVE_RATE (approximately 41.3 and 67.4 FPS against a 90-FPS request).
Five later software-openh264 clips had native/post-finalization completion.
The native mux reports video durations of 2.75556, 1.57778, 9.42222, 4.28889
and 2.13333 seconds. The subsequent `post_final_mix_av_timeline` observations
(events 197, 244, 271, 356, 394) report final video presentation durations of
2.733333, 1.555556, 9.411111, 4.266667 and 2.111111 seconds. These are different
measurement stages, not interchangeable duration values; all are partial
against the requested 45 seconds. Stream start/end agreement is not a decoded
content-sync proof.
The 9.42222-second save is process generation 19/task 2; its preceding
1.57778-second save is the same generation/task 1. Other successful saves are
generations 17, 27 and 29, each task 1.

The AMF attempt in this session opened but received no useful frames; no
SubmitInput-18 message belongs to this session. Older AMF messages in the
cumulative UI log must not be counted again. Event 415 records a window
ContentSize transition from startup 160x28 to 1920x1080. Small dimensions alone
do not establish minimization. FTHR's own tray-hide log is not evidence that
the target window was minimized. The reported auto-selection problem has a
source-supported faulty transition, but not every source change in these logs
can be attributed to automatic rather than manual selection.

### Historical comparison

Session `1e85ca56-3a91-450b-8243-9b02058164ee` has nine engine starts/eight
restarts with an empty monitor device path. WGC_MONITOR and its DXGI fallback
both fail at monitor resolution, before an actual encoder is established.
It is not an independent AMF SubmitInput reproduction.

## Upstream boundary evidence

The package pins FFmpeg `n8.1.2-21-gce3c09c101` through
`tools/ffmpeg_manifest.json`. Its ten runtime file hashes are verified before
copying dependencies into the isolated build.

- [Pinned amfenc.c](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavcodec/amfenc.c)
  handles texture-array indices and retains AVFrame references until AMF output;
  fatal native SubmitInput results are logged then flattened to AVERROR_UNKNOWN.
- [Pinned D3D11 hardware pool](https://raw.githubusercontent.com/FFmpeg/FFmpeg/ce3c09c101/libavutil/hwcontext_d3d11va.c)
  respects the configured texture flags. A speculative flag change is not a
  demonstrated fix.
- [AMF Result.h](https://github.com/GPUOpen-LibrariesAndSDKs/AMF/blob/master/amf/public/include/core/Result.h)
  defines enum 18 as AMF_DIRECTX_FAILED, not AMF_INPUT_FULL (25). This is not an
  HRESULT. The exact AMF SDK header revision used by the pinned third-party
  FFmpeg build is not independently established by the project manifest.
- [Microsoft screen capture guidance](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)
  requires respecting ContentSize, releasing frames before pool recreation and
  not accessing returned frame surfaces. Undefined texture padding is not valid
  content.
- [FFmpeg send/receive contract](https://ffmpeg.org/doxygen/trunk/group__lavc__encdec.html)
  requires draining output after EAGAIN without losing or duplicating pending
  input. EAGAIN is not a generic AMF failure.

## Implementation and qualification

Local integration is verified within the limits below. The final Python run completed with 1,752
passed, 61 skipped and two existing Qt mouse-event deprecation warnings. Ruff
passes. The final native Release build/test completed 22,755
checks, including 262 AMF checks and 51 WGC lifecycle checks. WARP's separate
video-processor pixel section is explicitly skipped because that device does
not expose ID3D11VideoDevice; it is not counted as physical VP qualification.

The controlled NVIDIA/Windows 10 WGC test reproduced the original startup
failure: the item's 656x399 allocation estimate differed from the first frame's
642x392 ContentSize. Current window startup uses the delivered ContentSize,
owns its valid GPU rectangle before returning the frame, and seeds an initially
static window without waiting for another repaint. Physical run 07 saved and
fully decoded both its 640x360 static control and its 642x392 native/source
control (one-second requested video presentation duration each). The native
source resize correctly requested an incompatible-geometry new generation.
Initial minimization was explicitly rejected with restore-and-retry guidance.

The earlier uninitialized fixed-output run returned
0x8001010E/RPC_E_WRONG_THREAD at frame-pool Recreate. This exact failure still
has a defensive bounded fresh-generation response instead of three identical
in-place retries. Other failed attempts retain their bounded budget.
Run 10 used one explicit fresh engine instance, then physically observed pause,
restore, fresh frames and two production SaveClip outputs: a full one-second
clip and a 6.283333-second partial against a 30-second request. Both strictly
decode, contain the controlled moving/nonblack gold-marker pattern and have
60 visible frames/second with maximum decoded PTS gaps of 16.667 ms. The
static and native/source controls also strictly decode for one second each.
These are video-only controlled tests, not GUI/F9, gameplay or audio proof.

Run 10 subsequently crashed during closed-window shutdown, so it is not a
lifecycle pass. WER exception 0xC0000005 at RVA 0x4dc26 resolves through the
matching local PDB to ShutdownWGC line 6352, the Closed-token removal call.
Its original WER report and local symbolization are retained externally.
Run 09's earlier save-queue rejection was a test setup error: Shutdown makes
that queue terminal, while production restarts a process/new engine instance.
The harness was corrected, not the production queue. Previous item.Size,
agile-reference and fresh-wrapper experiments remain preserved, not claimed
as fixes. The owned-apartment counterexample was executed before the narrowly
scoped entrypoint correction; handler removal was not weakened. Those failed
binaries/logs are retained.

The decisive run 11 counterexample changed only the caller's WinRT lifetime:
CoGetApartmentType initially returned CO_E_NOTINITIALIZED, then an explicitly
owned RoInitialize(MTA) succeeded. With Closed-token removal unchanged,
in-place Recreate, minimize/restore, saves and closed-window shutdown all
passed. The previously uninitialized creator/caller thread was therefore a
demonstrated contract defect, not grounds to omit event cleanup or blame AMD.
[Microsoft's RoInitialize contract](https://learn.microsoft.com/en-us/windows/win32/api/roapi/nf-roapi-roinitialize)
requires initialization before WinRT interaction and a matching uninitialize
for every successful call, including S_FALSE.

The minimal production correction is a noncopyable scoped WinRtApartmentGuard
at the native engine and AMD helper entrypoints, before any WinRT/engine local;
it outlives their interface destruction. It preserves the real initialization
HRESULT and balances only successful calls. Existing WASAPI MTA ownership was
reviewed for compatibility, not changed. Tests cover nested success, implicit
MTA baselines and failed MTA initialization against a caller-owned STA.

Run 12 uses this identical production guard, not the earlier probe's separate
implementation. All four physical controlled-window cases exited zero:
in-place resize/minimize/restore/close, static startup, native/source sizing,
and initial-minimized rejection. It needed no engine restart in the fixed-output
case. Strict -xerror full decoding and bounded decoded-pattern checks passed
on all four generated MP4s. The moving saves are 1.000000 s (full request 1 s,
60 visible frames) and 6.183333 s (partial request 30 s, 371 visible frames).
The static control is 640x360/1.000000 s and native/source control
642x392/1.000000 s, each 60 visible frames. All visible PTS increase strictly;
largest gaps are 16.667 ms. The controlled paint source changes at about 10 Hz;
60 FPS describes output cadence, not 60 distinct source presentations.
Static mode explicitly makes no dynamic-content claim. No audio was enabled.
The report does not claim exact paint-phase/wall-clock correspondence, GUI/F9
behavior, a long soak, AMD validation or gameplay performance from this harness.

The untouched HardwareEncoder recording regression passed eight real NVIDIA
GPU/CPU-input geometry scenarios with strict decoding. This is not AMD,
gameplay, GUI/F9, long-soak or audio-sync validation. The audio continuity,
combined-audio production finalizer AST, neutral Viewer, replay-health policy,
Linux files, shared-memory ABI, assets and license paperwork pass 185 explicit
byte/AST preservation checks. Asset provenance/release permission is unchanged.

The old project source inventory still matches its pre-task SHA-256
`c98be227d9001b8c60dc72be1c4dddd9cfd128dead0c95e93b2862d9d91b577b`
(1,065 files, 565 existing worktree entries, zero staged paths).

No AMD physical success, microphone sync or release readiness is inferred from
compilation, mocks or NVIDIA-only tests. Artifact identity is recorded separately
after packaging; the baseline SOURCE_MANIFEST is not presented as this build.

## Newly supplied community PR 87 (read-only review)

[PR 87](https://github.com/FTHR-Community/FTHR-Clips/pull/87) targets the exact
selected Windows baseline 840c00fc. The reviewed head is
`1d3e6c2e45e13a6642fc799daf9033ed69824b43`, one changed AMF C++ file, 102 added
and 13 removed lines. It has no reported CI checks or new regression tests.
Its author reports controlled texture readback/packet comparison and clean
H.264/HEVC/AV1 recordings on an RX 7800 XT after replacing an eight-slice BGRA
array with eight individual textures. The report excludes window capture,
gameplay load, long runs and other AMD GPUs. That is valuable external hardware
evidence for scrambled pictures, not local reproduction or proof of the
unresolved SubmitInput(18) DirectX failure in the supplied reports.

The PR's AVBuffer lease approach keeps accepted frames alive and leaves EAGAIN
input intact under the current single-threaded session contract. Its additional
Sleep-only full-pool wait and post-send preparation-failure handling need
targeted tests; full-pool reachability with async_depth=4 is not established.
Its raw-pointer vector insertion also lacks exception-safe COM ownership if
allocation throws. Neither concern is asserted as the reported AMD root cause.

The baseline comment that this FFmpeg requires a nonzero fixed pool is too
strong. The exact pinned
[D3D11 header](https://github.com/FFmpeg/FFmpeg/blob/ce3c09c101/libavutil/hwcontext_d3d11va.h#L23)
and implementation provide individual textures when no fixed size/texture is
set: frames_init creates an array only for a positive initial_pool_size;
d3d11va_pool_alloc otherwise calls d3d11va_alloc_single. The zero-sized metadata
allocation is supported by av_realloc's size+!size behavior. This is a simpler
comparison candidate, not an implemented AMD fix; unlike the PR's custom pool
it has no explicit eight-texture allocation ceiling. Runtime verification of
this isolated FFmpeg boundary is separate from AMF hardware validation.

An executed RTX 4060 Ti probe against the exact DLL/headers now confirms the
distinction: initial size eight gives one eight-slice texture; one gives just
one simultaneous frame plus seven -12 allocation failures; zero gives eight
different single-slice textures with index zero. Three unref/reacquire cycles
reuse the same resources. This exercised no AMF encoder, PR code or Radeon
hardware, and proves neither scrambled-picture correction nor SubmitInput(18)
resolution. Logs, executable/header/runtime hashes and the initial preserved
compile-setup failure live under the external pr87-pool-probe evidence folder.

The user explicitly approved local integration/testing of the reviewed
single-texture approach, without a GitHub merge, commit or push. The local
integration uses the pinned standard AVBufferPool descriptor contract and
a hard eight-texture limit, not unrestricted dynamic growth or the PR's
manual busy-list/Sleep loop. Credit for the single-texture finding belongs to
PR author vxnsin/Vensin. This is a reviewed adaptation, not a literal PR merge.

Each leased descriptor owns a GPU-only BGRA ArraySize=1 texture with slice zero.
The frames-context final-free hook closes its caller-provided pool; outstanding
AVFrame clones/AVBufferRef leases keep their resources and pool state alive.
No ninth texture is allocated. Failed reservations roll back, including an
injected CreateTexture2D E_OUTOFMEMORY case. Optional prefetch exhaustion is
deferred until normal output drain rather than poisoning an accepted frame.
Pending PTS/keyframe state survives EAGAIN; fatal errors remain fatal.

Tests exercised the actual production factory, pinned FFmpeg hardware-frame
APIs on NVIDIA, and a separate mandatory WARP factory/lease contract (without
pretending WARP has the video interfaces required by FFmpeg). AMF encoding,
Radeon async completion and the external picture/submission failures remain
unverified locally. There is no new full-frame CPU readback or IPC transfer in
production, and no throughput improvement is claimed from pool tests.
No GitHub merge or review comment has been performed.

## Four focused work items

| Item | Proven boundary and implemented change | Remaining qualification |
|---|---|---|
| Explicit source authority | Queued automatic selection could supersede explicit choice. Persist manual/automatic authority; revalidate source revision, HWND/PID/executable/class/creation identity and process generation at handoff. Game metadata does not authorize source switching. Windows-only startup guard preserves Linux behavior. | Deterministic UI/controller tests pass; manual game/F9 UI field retest pending. |
| Window lifecycle | First ContentSize can differ from Item.Size; borrowed/padded surfaces need valid-rect ownership. Seed an owned first frame, release leases before recreation, pause actual minimized windows and use generation-local close signals/bounded recovery. Shipped native callers now own WinRT initialization through teardown. | Run 12 controlled NVIDIA resize/minimize/restore/close and static/native controls pass, with strict full/partial decode. GUI/F9, gameplay, other systems and AMD remain pending. |
| AMF | Native 18 is AMF_DIRECTX_FAILED, not backpressure or an HRESULT. Preserve first-generation resource/error evidence; retain pending input across EAGAIN; integrate community single-texture layout with bounded standard ownership. | 262 AMF checks pass. Actual AMD corruption/SubmitInput18 fix and A/B/C stages require AMD hardware. |
| Honest saves | Generic successful completion did not establish full requested presentation history. Measure the first video's final presentation duration with bounded ffprobe jobs; expose full/partial/unknown/rejected/finalization-failed outcomes separately. | Deterministic publication tests and real full/partial SaveClip+decode pass; GUI notification/F9 physical retest pending. |

## Final automated and physical evidence

- Full Python: 1,752 passed, 61 skipped, two existing QMouseEvent deprecation
  warnings; final run 210.34 seconds, exit zero. No failing tests deselected.
- Release x64 native: 22,755 checks, including AMF 262 and WGC lifecycle 51,
  exit zero. Hardware pool/lease integration is NVIDIA, not AMD encoding.
- Evidence utilities outside the checkout: 17 passed, one symlink-creation
  test skipped because the host lacked that privilege; no elevation used.
- Ruff, shared-memory v4 contract, engine response publication contract,
  version consistency, exception baseline (92), tracked-repository hygiene,
  generated asset consistency and diff whitespace checks pass.
- Source license/package-policy gate: 198 checks, zero failures, one warning
  for absent Linux FFmpeg (Linux packaging is out of scope). This does not
  resolve or newly approve asset provenance, redistribution or public release.
- 185 protected byte/AST comparisons pass, including the real combined-audio
  worker. Old project inventory matches its pre-task canonical hash exactly.
- Native executable entrypoint/device enumeration smoke exits zero. The final
  AMD helper exits 20/AMD_HARDWARE_REQUIRED: A/B/C and save denominator zero,
  not a hardware pass.
- Instrumented coverage percentage, sanitizer run and a separate configured
  static type-checker run: NOT RUN. Selected secret-pattern scan found no
  matches in the 30 changed/new paths; it is not a comprehensive security audit.

Final controlled-window evidence is under the external
`E:\FTHR-Windows-P0-AMF-20261017-01\wgc-physical-results-12-production-guard`
and `wgc-*-validation-12.json` files. Earlier failures, WER and intermediate
executables are retained, not overwritten or used as final passes. The final
physical test has no parallel build/test load. It runs production CaptureEngine
and SaveClip in a controlled own-window harness, not the GUI or F9 handler.

| Final physical clip | Request | Video presentation | Visible frames | Largest visible PTS gap | Result |
|---|---:|---:|---:|---:|---|
| moving.mp4 | 1 s | 1.000000 s | 60 | 16.667 ms | Full, strict decode/nonblack moving pattern |
| moving-partial.mp4 | 30 s | 6.183333 s | 371 | 16.667 ms | Partial, strict decode/nonblack moving pattern |
| static.mp4 | 1 s | 1.000000 s | 60 | 16.667 ms | Full, strict decode/known static pattern |
| native-source.mp4 | 1 s | 1.000000 s | 60 | 16.667 ms | Full, strict decode/native 642x392 content |

Remaining qualification: actual AMD A/B/C encode and source/transform/pixel
results; AMD SubmitInput18 root cause and picture corruption; manual source
authority/F9/notifications in the shipped GUI; gameplay/load/long-run behavior;
new physical system-audio/microphone synchronization and 120-FPS Viewer tests.
Those are NOT RUN here or FIELD VALIDATION PENDING, not extrapolated passes.
The existing audio/Viewer fixes pass automated regression/preservation only.
No administrator, driver-reinstall, protection-bypass or silent software
fallback recommendation is made. The package is a targeted test candidate,
not backend release readiness or demonstrated AMD/NVIDIA parity.

## Final candidate / handoff

One Windows PyInstaller bundle was built after production code freeze, retaining
version 2.0.0-alpha. Its launch path is
`E:\FTHR-Windows-P0-AMF-20261017-01\delivery\FTHRClips\FTHRClips.exe`.

- UI SHA-256:
  `DC1BB3E8206839B685B6A516DB4FAB3020D701EE86317531D11F94A83502FC2F`.
- Native and packaged engine SHA-256, byte-identical:
  `D40D65BE010B393084112678D7AF53B349FB165C8EBA0BC65E92E2DD9394EE99`.
- AMD helper SHA-256:
  `9E16089EBDB18ED42F58EDF3432666162E9563ED1781B543156B216DF54DF115`.
- Runtime identity verification finds every required packaged FFmpeg copy
  identical to its pin. FFplay is not a required shipped tool. The native/helper
  PE files have no version resource; their hashes identify them, not an invented
  embedded version. The GUI reports 2.0.0-alpha.
- Actual Windows-artifact license/runtime/asset-policy gate: 121 checks,
  zero failures/warnings, exactly 54 approved asset files. No new legal release
  permission is inferred from the existing technical allowlist.
- Nine exact bundled Qt/playback DLLs load in an isolated Python process;
  packaged native device enumeration exits zero. These are loader/CLI checks,
  not GUI launch, Python extension import, playback rendering or F9 tests.
- The embedded GUI PYZ actually contains core.source_authority and
  core.save_outcome. The packager warned about an unused Qt labs
  assetdownloader plugin and its warning-format handler; the build completed.
  That optional module is neither referenced by FTHR QML nor shipped.

The first archive collection refused NumPy's mandatory license-notice directory
named src before writing any ZIP. Only the external archiver was refined to
allow src below _internal/<package>.dist-info/licenses; a new exact-byte notice
regression passes and ordinary project src remains blocked. No notices were
removed and no existing runtime, asset or release gate was weakened. The
original collection error log is preserved.

Developer hardware was rechecked: Microsoft Windows 10 Pro 10.0.19045,
RTX 4060 Ti, NVIDIA driver 32.0.16.1656, no AMD adapter. This describes only the
machine physically used above, not the external Radeon reports.

The delivery's BUILD-IDENTITY.json records the final branch/HEAD/status and
1,073-file canonical source inventory, compiled and packaged hashes, actual
dependency versions, included fixes and explicit limits. Evidence documentation
was finalized after the binary build; compilation inputs were frozen before
it. This inventory is not a reproducible-build proof. The source commit remains
840c00fc15f3830b54b37eb614ee4dbabcc0a418 on windows, with 21 modified and nine
new intended files, zero staged. No staging, history or remote update was made.

Changed production areas: main.py/game_detector/settings/source_authority for
source ownership; capture_engine/main.cpp/wgc_window_lifecycle for windows;
ffmpeg_amf_replay_encoder header/implementation for AMF; media_metadata and
save_outcome for final presentation duration. Associated Python/native tests,
native project wiring, the AMD helper/runner and two handoff documents changed.
uploader_bundle_manifest only binds the rebuilt existing optional bundle hash;
it is not uploader feature work. Exact paths/hashes are in the source inventory.

The full delivery can be archived into the uniquely named local test ZIP;
its archive digest/inventory remain external so no self-referential source hash
is claimed. It includes runtime, build identity, a short AMD field guide and
the prebuilt optional helper/Python runner, not original reports, captured media,
settings, logs, symbols, intermediates or the source checkout.

**One next action:** run docs/windows-p0-amd-retest-2026-10-17.md on the affected
AMD PC: explicit Desktop, H.264 AMF, 1080p60, warm 40 seconds for the requested
30-second history, three saves, then explicitly select Window and test restore.
Return diagnostic export plus unchanged attempted clips. Strict decode,
meaningful moving content, actual full/partial duration and audio sync are
separate acceptance checks. If AMF fails, one bounded reproduction is enough.

AMD PHYSICAL VALIDATION: BLOCKED — AMD HARDWARE REQUIRED.
External hardware reports: FIELD VALIDATION PENDING.
Backend release readiness: NO. Asset/license status: UNCHANGED.
COMMIT: NONE. PUSH: NONE. PUBLICATION: NONE.
