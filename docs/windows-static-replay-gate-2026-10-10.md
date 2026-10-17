# Windows static replay / Error 015 — candidate gate

2026-10-10. **PARTIAL: deterministic static-image defect corrected; physical save NOT RUN.**
Build `-02` remains an unchanged **diagnostic build**, not a demonstrated save fix.
The new `-03` is a **fix candidate**, not release qualification.

## Proven defect and boundary

The image-update-only DXGI path produces no replay packets during legitimate
long static sections. A deterministic run with 5 seconds of moving images
followed by 30 seconds of unchanged images and timely completed DXGI timeout/
pointer-only observations is rejected `STALE_END`. The same previous policy
also rejects a genuine capture interruption; it cannot preserve static replay.

The candidate samples an **owned same-device image**, at paced acquisition
slots, only after a valid image seed and recent, completed no-change API calls.
It does not change `LastPresentTime` or count a held image as a new source update.
Presentation QPC and raw source QPC/counters remain separate. Capture failure,
missing/late API completion, copy/release error, recovery and generation changes
disarm reuse. No timer-only or outage backfill is permitted.

This follows the documented [AcquireNextFrame no-change/timeout behavior](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe).
An [acquired texture cannot be reused after ReleaseFrame](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-releaseframe);
the candidate copies into an owned GPU texture before that boundary.
The thread-local timer uses [high-resolution waitable timing where supported](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createwaitabletimerexw),
with a bounded ordinary-timer/sleep fallback and no global timer-resolution change.

**Limits:** completed API no-change observations do not prove that intended
content is static or that a driver/compositor is behaving correctly. Long
slideshow pauses are not distinguishable from a static desktop by this API.
Recent under-target changing content is classified
`DEGRADED_CAPTURE_CADENCE`; old-source/live-held content is explicitly
`DXGI_IMAGE_IDLE`, never falsely labeled new-source `HEALTHY`.
The f774 report's approximately 20-second missing image-update interval remains
unattributed: this independent policy defect does not prove why the original
reference was not updating DISPLAY1.

## Actual validation

The C++ testing workflow preserved a failing before-fix test, then exercised
the production cadence helper, **real replay-ring selection** and the exact
production health-input builder/predicate. Packets are modeled, not real NVENC
output; this is not a physical save, mux or full-decode result.

| Automated scenario | Before / candidate result |
|---|---|
| 5 s moving + 30 s static, live API | Before: 300 modeled packets, STALE_END. Candidate: 300 real image observations + 1,793 held presentations, 2,093 modeled packets; accepted. |
| Actual injected 2 s API failure, then resume | Zero presentations/submissions during outage; 2.01667 s selected gap; LARGE_TIMELINE_GAP rejection retained. |
| Steady 10/30/40/60 FPS at requested 120/144 FPS | All rejected LOW_EFFECTIVE_RATE; not inflated into successful high-FPS history. |
| Pure static at 120/144 FPS | Accepted after valid seed and live API observations. |
| Moving 90/120/144/240/500 FPS sampled at 60 FPS | Accepted; modeled PTS clamp lead <= 1 tick, media/wall divergence <= 5.56 ms. |

An intermediate candidate failed the 10 -> 144 FPS sparse-source regression.
That RED artifact was preserved; the final quiet qualification fixes that
false acceptance without modifying the save predicate or its limits.
Unseeded caches, delayed/missing completions, release/copy failures, generation
changes, recovery and encoder-without-output cases are also covered.

Executed for this candidate:

- Native Release x64 engine build: **PASS**.
- Full native suite: **20,086 checks PASS**, including **476 replay-policy checks**.
- Seven relevant Python test files: **120 passed**, one deprecation warning.
  Full Python suite: **NOT RUN** in this candidate gate.
- Shared-memory / response-publication contracts: **PASS**; `FTHR_SharedMemory_v4` unchanged.
- New Windows package license/runtime/asset checks: **91 checks, 0 failures, 0 warnings**.
- Source tree + new package: **198 checks, 0 failures, 3 existing Linux-on-Windows warnings**.
  This does not qualify Linux or newly resolve asset/legal provenance.
- `git diff --check`: **PASS**.

Actual DXGI acquisition, NVENC submission, real F9 save, MP4 decode, A/V marker
comparison and physical performance: **NOT RUN**. These tests do not establish
that the external/user-reported save or desync problem is fixed.

The owned 1080p BGRA texture is 8.29 MB. At 60 changed frames/s, the extra
GPU-to-GPU copy payload is approximately 497.7 MB/s (approximately 995.3 MB/s
read + write traffic); unchanged held images do not recopy. No new CPU frame
copy or IPC is introduced on the same-device GPU path. Actual cost: **NOT MEASURED**.

## Exact separate test build

Launch:
[C:\Users\Tom\Documents\FTHR-Clips-master\dist\FTHRClips-Static-Replay-Gate-20261010-03\FTHRClips\FTHRClips.exe](<C:/Users/Tom/Documents/FTHR-Clips-master/dist/FTHRClips-Static-Replay-Gate-20261010-03/FTHRClips/FTHRClips.exe>)

- Engine SHA-256: `97BC5D6769792182DE42CB7C6BEA317E22AC6616D12B16741FC930103B618B9B`.
- UI SHA-256: `53D7240D04D6D663A653C26F7D5ED45BD018187E1DE3AFF176A2AF555F993D61`.
- Native engine built in a new temporary output directory, then packaged in a
  new directory; existing native output, builds `-01`/`-02` and media preserved.
- UI/runtime are byte-identical copies of `-02`; no PyInstaller rebuild.
  The existing corrected production finalizer is preserved.
- **Authoritative identity:** `FTHR_STATIC_REPLAY_GATE_MANIFEST.json` beside
  this executable. Inherited AV-sync/Error015 manifests describe older engine
  baselines only. Error015 manifest renamed in the NEW copy to
  `BASELINE_FTHR_ERROR015_DIAGNOSTIC_MANIFEST.json`; old build unchanged.

Source delta for this task (all paths under
`C:\Users\Tom\Documents\FTHR-Clips-master`):

- `FTHRcapture/FTHRclips/src/capture_engine.cpp`: owned DXGI sampler, bounded wait, separate counters.
- `FTHRcapture/FTHRclips/include/capture_engine.h`: private cache/cadence state.
- `FTHRcapture/FTHRclips/include/dxgi_replay_cadence.h`: platform-neutral tested decision helper.
- `FTHRcapture/FTHRclips/include/encoded_replay_health.h`: extracted exact existing health-input preparation, reused by tests.
- `FTHRcapture_common/capture_pipeline_health.h`: diagnostic held/source distinction.
- `FTHRcapture/FTHRclips_tests/tests/windows_replay_policy_test.cpp`: RED/GREEN and failure/sparse/motion controls.
- This report and the new package's manifest.

`replay_health.h` and `frame_rate_scheduler.h` hash-match the task baseline:
**STALE_END protection and limits unchanged**. No audio/gain/continuity/
resampler/finalizer/encoder-PTS/viewer/Linux/ABI changes.
Protected finalizer sources, original reference, old ZIP, old Warzone media
and existing engine/UI binaries were re-hashed unchanged.

## Tom's next real run

At the final process/settings check, the active app/engine still belong to
`FTHRClips-AV-Sync-Gate-20261010-01`, not `-02` or `-03`.
Saved settings still select **Window / Combined audio**. Nothing was stopped
or reconfigured. No new native recording or new diagnostic ZIP was found;
the latest ZIP is still the historical f774 report.

When ready, normally exit the old app including its tray instance, then:

1. Launch the exact `-03` executable above. Select **Desktop**, **AOC 27G2G8 /
   DISPLAY1**, **1920x1080 / 60 FPS / DXGI / H.264 NVENC / 30 s replay**.
   Do not use the left-hand LG monitor.
2. Settings -> Audio -> Audio Capture: enable **Keep system and microphone
   audio as separate tracks**. This selects `audio_capture_mode=separated`
   and `separate_audio_enabled=true`. Keep gains unchanged.
   The earlier Combined setting is not a cause of STALE_END.
3. After any capture/settings restart, warm the buffer for at least 30 seconds.
   Play this additional verified moving control fullscreen on DISPLAY1,
   with system audio audible:
   `C:\Users\Tom\AppData\Local\Temp\FTHR-moving-reference-6112363e27f143d590f13228abd3b436\controlled_av_reference_moving_40s_1080p60.mp4`.
   Press **F9 at approximately reference second 35**.
4. Independently test a static desktop for at least 30 seconds and save once.
   A moving-control success does not qualify this static case.
   The original flash reference is retained at
   `C:\Users\Tom\AppData\Local\Temp\FTHR-av-sync-warzone-20261010-01\controlled_av_reference_v2_irregular_40s_1080p60.mp4`.
5. Regardless of success/failure, export via Settings -> General ->
   Troubleshooting -> **EXPORT DIAGNOSTIC REPORT**. Save the NEW ZIP.
   If successful, preserve the new MP4 under the currently configured
   `C:\Users\Tom\FTHR_Clips` root and its `.mp4.fthr-audio.json` sidecar.
   Do not overwrite any original.

**If Error 015 recurs:** analyze that new session's acquisition/source/held/
encode/ring/save evidence, scoped by engine generation and request ID.
Do not request a nonexistent successful MP4 or substitute the old f774 ZIP.

**If save succeeds:** verify current engine identity, session/save association,
track sources and sidecar/media hashes, then decode and confirm actual reference
markers. A present invalid sidecar stops analysis. Process a byte-identical copy
with `C:\Users\Tom\Documents\FTHR-Clips-master\tools\finalize_native_audio_retest.py`
into a new directory through the real production worker; re-hash originals.
Measure native system versus combined flash/tone deltas near start/middle/end,
using decoded presentation times, audio-track origins and AAC priming once.
Report milliseconds, uncertainty/ambiguity and combined-minus-native difference.
Runner PASS alone proves execution, not sync. A native offset does not alone
prove capture fault: reference-player/output, capture/replay/mux and finalizer
boundaries remain separate. Microphone needs its own known reference event.

## Separate final status

- **Save blocker:** physical outcome OPEN; next -03 save/ZIP required.
- **Static image:** policy defect reproduced, fix candidate regression tested;
  real static F9 save **NOT RUN**. API no-change evidence has the limits above.
- **System audio sync:** physical native/combined measurement **NOT RUN**.
- **Microphone sync:** **UNVERIFIED**; no microphone reference event measured.

Branch `integrate/latest-project-source`, HEAD
`d990630f5befd0d19ddf371dddf1e39dd5f87c02`, existing dirty worktree retained.
No staged changes. **COMMIT: NONE. PUSH: NONE.**
