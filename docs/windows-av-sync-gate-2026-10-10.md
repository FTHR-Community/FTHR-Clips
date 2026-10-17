# FTHR WINDOWS P0 — PHYSICAL AUDIO/VIDEO SYNC ROOT CAUSE & FIX

Date: 2026-10-10. This gate is deliberately **PARTIAL**: a final-mix timing
defect is reproduced and corrected; the reported Warzone content desync is
not yet causally attributed or physically requalified.

## Repository and scope

- Repository: `C:\Users\Tom\Documents\FTHR-Clips-master`
- Branch: `integrate/latest-project-source`
- Starting and final committed HEAD: `d990630f5befd0d19ddf371dddf1e39dd5f87c02`
- Preflight: 255 worktree entries (188 modified, 67 untracked). These are
  intentional existing work, not a clean committed baseline.
- Status at original gate completion: 256 entries (189 modified, 67 untracked); the only new status
  entry is `tests/test_combined_audio_policy.py`. The report is inside the
  already-untracked `docs/` directory. No preflight entry disappeared.
- No reset, clean, stash, discard, staging, commit or push. Existing RCs and
  the source MP4 are retained. No running FTHR session was stopped.
- This gate changes only native-audio finalization in Python, bounded saved
  stream diagnostics, tests, and this report. Native capture/audio/video,
  microphone gain, the continuity policy, Error 015 protection, the Windows
  120-FPS viewer implementation, Linux and the shared-memory ABI are untouched.

## Affected physical recording

- File: `C:\Users\Tom\FTHR_Clips\cod\cod_clip_from_09Oct2026_21-15-43.mp4`
- SHA-256: `E6828F5324D4EBE18ED9CADC83FEEDD16674590D5B6790CC61367E6380F5795D`
- Size: 224,174,525 bytes. Requested replay: 30 seconds.
- Session: `14200338-6376-40a0-a466-7096b5dcdf97`.
- UI Save #3: engine acknowledgement 52 ms; completion 1,615 ms. Native task
  ID was not retained; UI save number is not proof of the native task ID.
- Logs show DXGI Desktop Duplication, `native-nvenc` H.264, 1920x1080 and
  60 FPS for this generation. An earlier 120-FPS request is not this save's
  actual configuration.
- Monitor: persistent AOC2702 device path; `\\.\DISPLAY1`, output index 0,
  NVIDIA adapter LUID high=0, low=95130 for monitor/capture/encoder.
- UI log says two native audio streams were combined. Final file contains
  one stereo 48-kHz AAC `Combined Audio` stream. Its original system and
  microphone components cannot be independently recovered from this file.
- Affected session's engine path is under
  `dist\FTHRClips-Alpha-Stability-Gate-20261009-01\FTHRClips\_internal\engine`.
  Historical UI/engine hashes: **UNVERIFIED**. Today's hashes of that path
  do not establish which bytes created the historical recording.
- External-player desync: **developer reported**; exact whole-clip direction,
  magnitude and drift remain objectively unverified.

## MP4 validation and content evidence

Strict full video and audio decodes both exit 0. Warning retained:
`UDTA parsing failed retrying raw`; no decode failure.

- H.264 Main, no B-frames, yuv420p/BT.709, nominal 60/1, average 36620/613.
- Video time base 1/90000; 1,831 packets including decoder preroll;
  presentation duration 30.000 s.
- Earliest video packet -0.650 s is discarded decoder preroll, not 650 ms of
  visible leading footage. First decoded visible video PTS is 0.
- AAC time base 1/48000; 1,407 packets; duration 29.994 s.
- AAC first packet -0.021333 s carries Skip Samples=1024; decoded audio starts
  at 0. Priming must not be subtracted a second time.
- Six positive video timestamp gaps, maximum 66.666 ms; no PTS regressions.
  Audio cadence is continuous at approximately 21.333 ms per AAC packet.
- These structural facts do **not** prove content synchronization.

Sign convention: `delta_ms = audio_event_time_ms - video_event_time_ms`.
Positive is late audio; negative is early audio.

| Region | Actual visual PTS | Audio candidate | Delta / qualification |
|---|---:|---:|---|
| Near start | Ammo 26→25 at 2.916667 s | 2.889 or 2.954 s | -27.7 or +37.3 ms; pairing ambiguous |
| Middle | Muzzle/ammo 38→37 at 12.516667 s | 12.526 s | approximately +9.3 ms; candidate, not known-source proof |
| Last gameplay burst | Flash 17.600 s; ammo 17.616667 s | weak attack 17.617 s | +17 or +0.3 ms depending visual marker; pairing ambiguous |
| Actual clip end | Menus/cinematic, no verified common source event | unavailable | not measured |

Visual uncertainty is at least one 60-FPS frame (16.667 ms); audio candidates
use 5-ms RMS windows/1-ms hops. Mixed sound and unknown intrinsic game
render/audio scheduling introduce additional, unquantified pairing uncertainty.
Do not characterize the offset as constant, drifting, stepping or startup-only
from these candidates. System-only and microphone-only offsets remain unknown.

Local decoded-media evidence (not uploaded and never placed in normal
diagnostic ZIPs): `%TEMP%\FTHR-av-sync-warzone-20261010-01`.

## Clock and boundary audit

| Boundary | Current implementation / qualification |
|---|---|
| DXGI source | `capture_engine.cpp`: source `LastPresentTime` in raw QPC ticks goes to GPU/CPU encoder; pointer-only updates do not become new image frames. |
| Encoder PTS | `hardware_encoder.cpp::ComputePts`: capture-QPC epoch → elapsed × FPS / QPC frequency; repeated/nonincreasing ticks are clamped to last PTS+1. NVENC completion retains submitted timestamp and slot source-QPC, not completion wall time. |
| Replay ring | `encoded_ring_buffer.cpp`: video media start derives from newest selected PTS; QPC presentation end derives from requested target end. Their difference must be measured in the new physical run; a save-request time is not proof of source-frame time. |
| System audio | `audio_capture.cpp`: WASAPI packet QPC is in 100-ns units, with local QPC fallback when zero; persistent format conversion/canonical 48-kHz stereo/FIFO/AAC. TIMESTAMP_ERROR does not currently quarantine a nonzero packet QPC. Its occurrence in this save is unknown. |
| Microphone | `windows_microphone_audio_provider.cpp`: independent WASAPI QPC origin, conversion, continuity/drift, configured gain, FIFO and AAC. Timestamp-error counters do not themselves make flagged timestamps trustworthy. |
| Sample positions | `audio_timeline.cpp::MapAudioSourcePresentationRange`: signed rounded `(window QPC seconds - source origin/1e7) × sample rate`. Partial AAC frames queue in `AudioEncoder`; packet PTS represents sample position, not encode wall latency. |
| Native selection | System snapshot selection clamps negative start samples to zero and also uses that clamped value for its presentation origin; microphone keeps a signed mapped origin. A system source starting inside the replay window is therefore a separate early-origin risk, **not corrected or claimed causal here**. |
| Native mux | `capture_engine.cpp::MuxEncodedClip`: copied video PTS relative to video presentation start; audio packet PTS minus each contract's presentation sample start. Native preroll is not visible footage. No arbitrary subtraction of encoder/resampler wall latency is warranted. |
| Final mix | `_combine_native_audio_worker`: selected system/mic tracks → amix → existing limiter → AAC; video copied, `-shortest`. This gate corrects loss of relative first-input offsets at this boundary. |

SWR output frames, persistent delay and partial AAC FIFO were reviewed.
No demonstrated frame-count/unit conversion error for the affected session
was recovered. WGC's sampler timestamp behavior is not attributed to this
DXGI recording. Existing native tests exercise conversion, drift and restart
policy; they are not physical Warzone source-time evidence.

The historical native MP4 and selected packets were not retained. Detailed
session events reached their 4-MiB cap at approximately 21:02, before the
21:15 save; rotated engine logs no longer contain that save's complete trace.
Consequently historical replay-window, post-native-mux and per-source
content alignment cannot be reconstructed. Missing coverage is not zero events.

## Proven defect and minimal correction

**First proven incorrect boundary in the controlled late-track reproducer:**
native MP4 → final combined audio. This is not proof of the first bad boundary
in the historical Warzone recording.

With the pinned FFmpeg and the previous exact production graph, a late 880-Hz
microphone marker appeared at 0.975 s instead of its measured native absolute
position of 1.448 s (473 ms early);
the independent 440-Hz marker remained at 0.955 s. Container envelopes could
still look aligned. `-copyts` alone did not preserve the late track's samples
in the mix.

The production helper now prepares each **multi-input** track using
`aresample=first_pts=0:async=0` before unchanged amix/limiter. This realizes
the shared demuxed presentation origin by padding/trimming the leading sample
interval, instead of rebasing every source's first actual sample to zero.
There is no hardcoded delay, per-track STARTPTS reset, continuous async drift
compensation, gain change or capture-engine resampler change. The single-input
`anull` path remains unchanged.

The reproduced late marker moves to 1.455 s (7 ms late against that native
reference); the 440-Hz marker stays at 0.955 s. Existing limiter
lookahead/codec/block-measurement latency is kept
separate from the removed approximately 480-ms displacement.

[FFmpeg's resampler documentation](https://ffmpeg.org/ffmpeg-resampler.html)
documents first_pts as leading padding/trimming and async=0 as disabling
ongoing timestamp compensation. Runtime decoded-marker tests, not that
documentation alone, validate this exact bundled build.

Bounded pre/post-mix stream diagnostics now include absolute container stream
index, start/duration/end in **seconds**, at most 16 tracks with truncation
count, and the same privacy-sanitized clip path for save correlation. These
are stream metadata, not waveform measurements or per-frame logs.

Changed files for this gate:

- `FTHR_UI/core/combined_audio_policy.py`
- `FTHR_UI/core/av_sync_diagnostics.py` (already untracked at preflight)
- `FTHR_UI/main.py` (only combine helper import, filter call and timeline fields)
- `tests/test_combined_audio_policy.py`
- `tests/test_av_sync_diagnostics.py` (already untracked at preflight)
- this report

## Validation

Native Release solution build: PASS. Full native suite: **20,019 checks**, exit 0.
Shared-memory and engine response-publication contract checks: PASS.
Existing targeted audio/save/viewer/contracts regression group: **209 passed**.
Focused A/V diagnostics and combination-policy tests: **33 passed**, including
all **12 decoded-marker matrix cases** and actual production-worker publication.
Full current Python suite: **1,176 passed, 45 skipped**, 3 deprecation warnings,
exit 0 (64.17 seconds). Ruff and `git diff --check`: PASS.

The Python-testing workflow guided a failing production-equivalent old-graph
control before the minimal correction, followed by actual decoded content
checks rather than only comparing stream envelopes. Matrix cases independently
decode native system/mic AAC and final combined AAC, with start/mid/end markers.
Cases include aligned/early/late sources, different source offsets, common
+10-second origin, 44.1/48/96-kHz synthetic media conversion, 60/120/144 FPS,
negative video preroll/B-frame presentation order and 15/30/60-second clips.
Audio tolerance is 30 ms for the matrix (20 ms for the focused late-marker
regression); flash tolerance is one video frame plus 2 ms. Ten-ms spectral
blocks, AAC framing and retained limiter latency are included in these bounds.
The reference source is independently verified to at most 0.125-ms direct
audio threshold displacement; it is not assigned the coarse matrix tolerance.

These FFmpeg fixtures do not exercise physical WASAPI source restart or an
actual replay-ring rollover. Existing native regression suites cover source
timing/drift, conversion, FIFO/AAC and replay policy; this gate does not claim a
new physical multi-minute drift or first-run/restart qualification. The 45
skipped Python tests are not reported as passing.

Package build: PASS, separate PyInstaller destination. Package license/runtime/
asset verification: **91 checks, 0 failures, 0 warnings**. This does not reopen
or newly resolve asset provenance/legal status. Installer lifecycle source and
bundle contracts pass **with release-blocking unsigned UI/engine/mixer warnings**.

## Exact test build

- Launch:
  `C:\Users\Tom\Documents\FTHR-Clips-master\dist\FTHRClips-AV-Sync-Gate-20261010-01\FTHRClips\FTHRClips.exe`
- UI SHA-256:
  `53D7240D04D6D663A653C26F7D5ED45BD018187E1DE3AFF176A2AF555F993D61`
- Native and packaged engine SHA-256 (identical):
  `F9C0C147C3B35434D32BF1AF94F1CE857A3B251F360E7B187D108B4B95A5C09C`
- Native bytes are unchanged: the proven fix is Python finalization, not an
  engine replacement. The packaged Python archive was inspected for the new
  helper and exact timestamp-alignment graph.
- Packaged production source hashes at build:
  - `main.py`: `FAE673D8364530BDB0797A17C32D0825A52D6AFBB77A9BF9858D0FD40A5A17B7`
  - `combined_audio_policy.py`: `680B4E057B6A6231BF026A463284B2C53003CB7E2C5D87A96760DC8D06C65A14`
  - `av_sync_diagnostics.py`: `F729C421BB017488A30993A1548F7C3E1FBD72469B4B8B726AC08C8A98991310`
- Pinned FFmpeg CLI: `n8.1.2-21-gce3c09c101-20260630`, SHA-256
  `ABE4F6DC7CA6D807C9492E56F96DB9030D7C5FABA942254AA00D4474042048D`.
- Build manifest: `FTHR_AV_SYNC_GATE_MANIFEST.json` beside the new executable.
  It records the committed HEAD plus the exact three production-source hashes,
  bundle identities, test results and physical limitations.
- Readable dirty-source identity: SHA-256
  `82848c442fe9d194848127bc1b1f3d8e132ab5bdf06f9a9881cdb1aceb56182f`
  over 258 nonignored modified/untracked files. Algorithm: `git ls-files
  --modified --others --exclude-standard`, PowerShell sorted unique paths,
  UTF-8 LF lines `lowercase-file-sha256 space relative-path`, excluding this
  self-referential report. Two pre-existing unreadable reparse entries,
  `x86_64-linux-gnu` and U+F05C, were explicitly excluded and left untouched.
  This is not asserted to be a complete hash of those unreadable objects.

## One bounded physical experiment still needed

**PHYSICAL RETEST: NOT RUN.** Available developer machine was queried:
Windows 10 Pro 19045, i9-9900K/16 logical CPUs, RTX 4060 Ti,
driver 32.0.16.1656, current display 1920x1080/239 Hz. This is inventory,
not a capture qualification or external hardware validation.

Verified source:
`%TEMP%\FTHR-av-sync-warzone-20261010-01\controlled_av_reference_v2_irregular_40s_1080p60.mp4`

Source SHA-256: `1E1CFCC8D155EF723546E283CDA9BBF0339CE4E2B0B94040F03BB623051CE36F`.
40 s, 1080p60, 48-kHz stereo AAC. All 17 irregular flash markers have exact
decoded expected video PTS; direct audio threshold error is at most +0.125 ms,
with no measured drift. AAC priming is decoded exactly once. Generation,
decoded analysis and irregular marker timestamps are retained beside it.

One narrowly targeted artifact can distinguish the remaining boundaries:

1. Start the exact new executable above. Keep the current gains, 60 FPS,
   1080p, DXGI/NVENC and the correct monitor. Use desktop capture for the
   reference player, not the game-only target.
2. In Settings → Audio → Audio Capture, temporarily enable **Keep system and
   microphone audio as separate tracks**. This retains the native MP4 rather
   than irreversibly mixing away the evidence. Return the checkbox to its
   previous setting afterward; other source settings must stay identical.
3. Play the 40-second reference full-screen in an external player on the
   selected monitor, with system audio audible. After full 30-second warmup,
   press F9 once at approximately 35 seconds, before playback ends.
4. Export the same session's report through Settings → General →
   Troubleshooting → **EXPORT DIAGNOSTIC REPORT**.
5. Provide that single **separated MP4 plus its diagnostic ZIP**. Media is not
   automatically included in the ZIP. A separated recording alone is **not**
   physical validation of the combined-audio fix. Preserve it and its exact
   source sidecar; process a byte-identical copy of that same recording through
   the actual corrected production finalizer, not a reconstructed FFmpeg graph.

This short experiment supplies native content, per-source timing and the
shared replay/QPC trace. It can distinguish upstream source/window displacement
from final-mix displacement. Microphone speech remains an independent,
unmeasured source unless its relationship to a known event is established.
The source file's decoded alignment does not independently qualify an external
player's display/audio-output scheduling. That remains another possible latency
boundary; comparing native media with corrected production finalization of
that exact same recording
isolates the finalizer without assuming the player or physical devices are ideal.

## Retest addendum: validate both sides of the finalizer

Physical native recording: **NOT AVAILABLE YET**. No old Warzone file or
generated fixture is substituted for the pending flash/tone capture.
Physical native-versus-combined result: **NOT RUN**.

The comparison must retain these independent artifacts:

1. Original native MP4 and original `.fthr-audio.json` sidecar, unchanged.
2. Complete FFprobe stream metadata, source-to-container-index mapping and
   evidence of sidecar validity against the native media hash. A missing
   sidecar permits only explicit recognized track labels; unlabeled source
   identity stays unknown. A present invalid sidecar must stop the retest.
3. Separate test MP4 produced by invoking
   `MainWindow._combine_native_audio_worker` on a byte-identical copy with its
   verified sidecar. Do not implement an approximately equivalent command or
   bypass stream selection, filtering, AAC encoding or publication. Visual
   overlays are disabled for this isolated audio-finalizer comparison.
4. Original-media and original-sidecar hashes checked again after finalization,
   plus exact finalizer-source and result identities.

Measure decoded visible flash PTS and corresponding audible marker times near
the beginning, middle and end wherever verified markers survive the selected
replay window. Use real decoded PTS rather than frame index divided by FPS.
Report `audio_time_ms - video_time_ms`, with positive meaning late audio.

For each usable marker report native system-audio delta, combined delta and
`combined_delta - native_system_delta`. Native per-track PCM measurements must
restore the track's actual presentation origin and handle AAC priming exactly
once. Equal stream start times or matching durations do not substitute for
decoded event measurements.

| Evidence | Permitted conclusion |
|---|---|
| System flash/tone offset already present in native recording | Offset exists before finalization; does not alone distinguish external-player timing from capture/window timing. |
| Native system markers align, combined markers shift | Finalization introduced or changed the offset. |
| Native and combined exhibit the same offset within measured uncertainty | Finalization preserves that measured offset; upstream cause remains open. |
| Only some regions contain usable markers | Report measured regions and missing coverage; do not infer whole-clip drift. |
| Mic track exists but has no known matching reference event | Identify and retain it; physical microphone synchronization remains **UNVERIFIED**. |

The flash/tone stimulus qualifies only the system-loopback relationship. Do not
infer microphone synchronization from its start PTS, audible speech or leaked
speaker beeps. If microphone content makes a combined marker ambiguous, flag
that marker instead of assigning a mixed onset to the system source.

Prepared runner: `tools/finalize_native_audio_retest.py`. It invokes the actual
production worker on the copy. It does not construct a substitute FFmpeg mix
command or instantiate a capture/UI session. Its output directory must be new;
overlapping/existing destinations, invalid sidecars and sidecars pointing to
nonexistent/non-audio streams are refused.

After the physical clip is available, invoke from the repository:

```powershell
python .\tools\finalize_native_audio_retest.py "<actual-native-MP4-path>" "<new-output-directory>"
```

The two arguments are placeholders until the new physical recording exists.
Do not use the historical combined Warzone clip or the reference stimulus as
the native input. The separate finalized test file keeps the original basename
inside that new directory. Its `native_audio_retest_evidence.json` retains full
native/final stream metadata, the verified native manifest, track provenance,
selected source count, source/result hashes and finalizer source-file hashes.
The copied sidecar can be consumed by normal production publication; the
original sidecar remains untouched and its verified contents are retained.

`FINALIZED` / `finalizer_execution_status=PASS` describe execution only.
`physical_validation` and `content_sync_validation` remain `NOT RUN`; this
metadata runner does not itself measure decoded physical markers. A one-input
recording is explicitly marked `single_input_path_only` and cannot qualify the
multi-input alignment fix.

Deterministic validation: **40 focused tests passed**, including real production
finalization of a generated three-track native file. Native and combined video
markers and distinct system/mic tone markers are decoded at start/middle/end;
an unknown auxiliary source is retained in native evidence and excluded from
the manifest-selected combination. Input MP4/sidecar preservation is checked
on success and simulated warning failure. Destination/invalid-manifest refusal
cases also pass. Ruff, CLI help and diff checks pass. Audio comparison tolerance
is 30 ms; visual tolerance is one 60-FPS frame plus 2 ms. These are synthetic
regression results, not physical system or microphone qualification.

Full Python regression for this addendum: **1,183 passed, 45 skipped**, three
deprecation warnings, exit 0 (103.80 seconds). The full native suite/build were
not rerun for this tooling-only addendum; the original gate's native results
above remain historical results on unchanged native code.
No production/native/Linux/ABI code or existing test-package binary was changed.
The original gate's package/source hashes above remain valid for its production
finalizer. The addendum modifies only this report, the existing A/V test file
and the new offline retest tool. COMMIT: NONE. PUSH: NONE.
Addendum final repository state: branch/HEAD unchanged; 257 status entries,
all 256 addendum-baseline entries retained, only the new retest tool added.
Nothing staged. The Warzone source MP4's SHA-256 remains unchanged.

## Current repository evidence recheck — 2026-10-10

Repository: `C:\Users\Tom\Documents\FTHR-Clips-master`.
Branch: `integrate/latest-project-source`.
HEAD: `d990630f5befd0d19ddf371dddf1e39dd5f87c02`.
Preflight: 257 existing entries (189 modified, 68 untracked); nothing staged.
Existing changes were preserved. This recheck changes only this report.

The configured clip root and recording directory were searched recursively;
Desktop and Downloads were also searched for MP4s. The latest configured clip
remains `cod_clip_from_09Oct2026_21-15-43.mp4`, with unchanged SHA-256
`E6828F5324D4EBE18ED9CADC83FEEDD16674590D5B6790CC61367E6380F5795D`.
No new native flash/tone capture or associated new sidecar was found in these
locations. No historical combined clip or generated reference was substituted.
The saved settings still select combined audio; separate tracks must be enabled
for the pending physical recording. No FTHRClips/FTHRClipMixer process was found
at the process check; no session was stopped or launched.

The existing `FTHRClips-AV-Sync-Gate-20261010-01` UI, packaged engine, native
Release engine and reference stimulus hashes match the identities above.
All three production source hashes in the test-build manifest match the
current files. This identifies an existing dirty-source test build, not a
clean HEAD-only build or a physically tested build in this recheck.
No existing executable, reference, clip, sidecar or settings file was changed.

Executed now: the two focused A/V Python test files (40 tests, exit 0, one
pytest-asyncio deprecation warning), retest-runner CLI help, targeted Ruff and
`git diff --check` passed. Generated regression media are synthetic only.
Full Python suite, native suite and a new Release build: **NOT RUN** in this
recheck; no production/native change required rebuilding the identified build.
Physical native-versus-production-finalized comparison, start/middle/end
offset measurements and microphone physical synchronization: **NOT RUN**.
No new cause was proven and no timing fix was implemented. Follow the physical
recording instructions above and retain the native MP4, its sidecar if present,
and the same-session diagnostic ZIP. The historical real desync remains open.
COMMIT: NONE. PUSH: NONE.

## Final qualification

- Finalizer timing defect: **PROVEN; FIX IMPLEMENTED; REGRESSION TESTED**.
- Historical Warzone desync root cause / direction / magnitude: **UNVERIFIED**.
- Reported physical issue is not claimed fixed without the controlled capture.
- Native input-source QPC validity, PTS-clamping accumulation and requested
  replay end versus last encoded source time remain independent measurement
  boundaries; no speculative engine changes were made.
- 120-FPS viewer: source preserved, existing regressions exercised; not
  newly physically qualified by this gate.
- Audio continuity/gain, Error 015, Linux and licensing status: unchanged.
- Shared memory: `FTHR_SharedMemory_v4`, contract intact.
- Public release readiness: NO (physical sync still open; unsigned build).
- COMMIT: NONE. PUSH: NONE.
