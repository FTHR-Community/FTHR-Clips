# Error 015 — A/V retest blocker, 2026-10-10

**PARTIAL.** Two diagnostic defects are reproduced and corrected. The sparse/stale replay remains refused before the writer. A successful physical save and native/combined A/V measurements are **NOT RUN**; the save blocker is not claimed fixed.

## Baseline and evidence

- Repository: `C:\Users\Tom\Documents\FTHR-Clips-master`.
- Branch: `integrate/latest-project-source`; HEAD: `d990630f5befd0d19ddf371dddf1e39dd5f87c02`.
- 257 existing worktree entries preserved; nothing staged. No commit/push.
- ZIP: `C:\Users\Tom\Desktop\FTHR-Clips-Diagnostics-20261010-134446-f7746a8b.zip`.
  SHA-256: `81FEA52B547FC48FF49A5BBBAAC368139AF43D8B6D4F6D148F0B0D0565B0FCB0`.
- Session: `f7746a8b-a1fd-4463-921f-10a8314e20f9`.
- Report: DXGI, native NVENC H.264, 1920x1080/60; DISPLAY1/output 0/AOC2702; monitor/capture/encoder LUID high=0, low=94934.
- Live processes still use the original AV-Sync-Gate package. Its engine currently hashes to `F9C0C147C3B35434D32BF1AF94F1CE857A3B251F360E7B187D108B4B95A5C09C`. The ZIP does not itself retain an engine hash; current process/file checks are not a retrospective hash embedded in that report.

## Correlated timeline

Times: Europe/Berlin. Line numbers: 1-based events.jsonl.

| Time | Event | Evidence |
|---|---|---|
| 13:42:27.468 | Engine process 1 starts | Handshake/UI generation 1; 50,000 kbps |
| 13:43:12.449 | Process 1 stops normally | Exit 0 |
| 13:43:13.454 | Process 2 starts | Handshake/UI generation 2; 25,000 kbps |
| 13:43:36.891 / 37.023 | UI Save 1 / rejection, line 75 | Native request 1; capture active 21.9339465 s; STALE_END; writer_invoked=false |
| 13:43:51.096 | Process 2 stops normally | Exit 0 |
| 13:43:52.095 | Process 3 starts | Handshake/UI generation 3; native capture generation resets to 1 |
| 13:44:34.216 | Health, line 120 | 1,313 acquire successes; 1,282 pointer-only; source/submissions/packets/ring=23; source age 20,138 ms; incorrectly HEALTHY |
| 13:44:37.236 / 37.317 | UI Save 2 / rejection, line 127 | Native request resets to 1 in process 3; capture active 43.7205249 s; STALE_END; writer_invoked=false |

Correlate session + engine process/generation + native request, not request 1 alone.
Save 1 also has only 19.083333 s media coverage, a 3.5 s PTS gap and 2.8961127 s end staleness. Warmup alone cannot explain Save 2.

## Exact failed boundary

CaptureEngine::SaveClip → ring snapshot → presentation-packet selection → replay_health::Evaluate, before queue/writer/finalizer/audio mixing:

- Save 2 target end QPC 539577129004 minus selected last QPC 539553804112, divided by 10,000,000 ticks/s = **2.3324892 s**, above the unchanged 1 s bound.
- 22 selected health packets; PTS 1223→2486 at 60 units/s = **21.05 s**. Minimum requested coverage = **26.983333 s**.
- Largest QPC gap: 539344311448→539548385515 = **20.4074067 s**.
- Largest PTS gap: 1229→2454 = **20.4166667 s**, above the unchanged 250 ms bound.
- The newer ring image at QPC 539577808131 is **67.9127 ms after the frozen request end**. Excluding it is correct, not evidence of a selection bug.
- Old rate=0 was an uncomputed default: calculation occurred after STALE_END's return. The selected-media rate is 21/21.05 = approximately **0.997625 packets/s**, not a measured zero-image rate.

Changing only the end check would still leave inadequate coverage and a huge gap.
NVENC outputs follow submissions; these diagnostics do not demonstrate an NVENC or audio failure.

## What DXGI actually offered

engine.log retains all 512 bounded records at each rejection. events.jsonl contains only the first 128 of each array; its last returned acquisition is **not** the last acquisition at save time.

- Save 1 trace: 66 timeouts, 352 pointer-only successes, 45 accepted images, 49 image pacing rejections.
- Save 2 trace: 290 timeouts, 195 pointer-only successes, 16 accepted images, 11 image pacing rejections.
- Save 2 contains API completions around the request and an accepted image just after it. Its 20.407 s gap already occurs between offered image presentations. No evidence shows FTHR rejecting 20 seconds of normally offered moving images.
- Pointer-only observations have LastPresentTime=0 / AccumulatedFrames=0 and are excluded before pacing/encoding. WAIT_TIMEOUT is no new DXGI image, not by itself a failed capture.

See [Microsoft's AcquireNextFrame contract](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe) and [frame-info semantics](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/ns-dxgi1_2-dxgi_outdupl_frame_info).
API activity does not prove the intended player/game is on that output or rendering correctly.

DISPLAY1 is the primary AOC 27G2G8 at (0,0), 1920x1080. The LG ultrawide is left at (-2560,0). Player placement/play state was not retained: **UNVERIFIED**.

## Proven defects and minimal correction

1. The original reference is static between flashes. A video-only decode of 90 frames from 10.2–11.7 s yields one image hash. Its drawbox expression uses t as thickness, not elapsed time ([FFmpeg documentation](https://www.ffmpeg.org/ffmpeg-filters.html#drawbox)). It is not a continuous-motion stimulus. This still cannot explain 20 seconds without its irregular flashes if they were actually playing on DISPLAY1.
2. The periodic classifier returned HEALTHY whenever all DXGI image/submission/output ages were old, without positive API-alive or encoder-idle evidence.
3. STALE_END returned before calculating an otherwise valid packet rate.

Task changes:
- `FTHRcapture_common/capture_pipeline_health.h`: DXGI_IMAGE_IDLE requires recent acquisition, idle API evidence and positive encoder-idle evidence. Missing proof becomes CAPTURE_SOURCE_STALL_CANDIDATE; pending encoder work remains an output-stall candidate.
- `FTHRcapture/FTHRclips/src/capture_engine.cpp`: reuse existing watchdog evidence in bounded 10 s snapshots; emit evidence flags and rate-measured state. Empty/unimplemented encoder diagnostics cannot prove idle; pending work requires actually positive resource/stage fields, not absence of idle proof.
- `FTHRcapture_common/replay_health.h`: calculate validated selected-media rate before stale rejection; explicitly distinguish unmeasured rate.
- `FTHRcapture/FTHRclips_tests/tests/windows_replay_policy_test.cpp`: regression cases, including exact Save 2 endpoints/gap with synthetic intermediate points.
- This report. Build metadata is inside the new ignored package.

No changes to acquisition/pacing, real source timestamps, recovery, save acceptance order/limits, CFR expansion, audio gain/continuity, finalizer, Linux or ABI.
DXGI_IMAGE_IDLE is **not** save readiness or proof that expected moving content was captured. Sparse static replay remains rejected. A genuine static-frame recording contract needs separate evidence/design, not fabricated source progress.

## Verified additional moving control

Original retained unchanged:
`C:\Users\Tom\AppData\Local\Temp\FTHR-av-sync-warzone-20261010-01\controlled_av_reference_v2_irregular_40s_1080p60.mp4`.
SHA-256 `1E1CFCC8D155EF723546E283CDA9BBF0339CE4E2B0B94040F03BB623051CE36F`.

Additional reference:
`C:\Users\Tom\AppData\Local\Temp\FTHR-moving-reference-6112363e27f143d590f13228abd3b436\controlled_av_reference_moving_40s_1080p60.mp4`.
SHA-256 `04AACCE5DF0DE26A9E6F588B15C4A4A000560907BC7CE1937AC828F07ABD76AF`.

Decoded 2,400 frames; 17/17 flashes at expected presentation timestamps and 17/17 tones.
All 2,332 nonflash full-resolution hashes are unique; maximum identical run=1.
Video onset error 0 ms at 60-FPS quantization; tone onset error 0 ms with 2-ms RMS windows, **not zero measurement uncertainty**. First decoded audio PTS=0; AAC priming was not subtracted again.
Scripts/verification.json are beside the reference. This is a synthetic stimulus check, not physical capture, player-timing proof or a successful save.

## Separate test build and actual gates

Launch:
`C:\Users\Tom\Documents\FTHR-Clips-master\dist\FTHRClips-Error015-Gate-20261010-02\FTHRClips\FTHRClips.exe`.

Fresh current-source Release x64 engine plus a **new copy** of the verified old UI/runtime; no existing build overwritten.
UI SHA-256 `53D7240D04D6D663A653C26F7D5ED45BD018187E1DE3AFF176A2AF555F993D61`.
Packaged/native engine SHA-256 `25F1EE47918F1C4899D137A58186893A9D6652B0CD343D8CAFCCA105FEDB95AD`.
Authoritative manifest: FTHR_ERROR015_GATE_MANIFEST.json beside the executable.
BASELINE_FTHR_AV_SYNC_GATE_MANIFEST.json is historical, not the new engine identity.
The preliminary Error015-Gate-20261010-01 package is also retained unchanged. Use **-02**, which includes the final unknown-encoder-evidence review.

Executed now:
- RED: old policy fails measured-rate assertion and unproven-HEALTHY regression.
- Fresh Windows engine Release x64 build and native Release suite: **20,032 checks, exit 0**.
- **120 relevant Python tests passed**: save, capture-health, contracts, combined policy and A/V diagnostics; one asyncio deprecation warning.
- Shared-memory/response-publication contracts pass; FTHR_SharedMemory_v4 unchanged.
- Binary contains both classifications, acquisition evidence and rate-measured tokens.
- Windows package gate: **91 checks, 0 failures/warnings**.
- Source + package license gate: **198 checks, 0 failures, 3 warnings** for Linux executables on Windows/missing Linux linkage target. No new Linux qualification or legal clearance.
- git diff --check passes. Existing FFmpeg conversion and temporary-build-directory warnings retained.
- Finalizer/tool sources, ZIP, original reference, old package and default native Release hashes unchanged.

Real new save: **NOT RUN — physical GUI interaction required**.
The computer-use skill requires node_repl, which is unavailable here; no alternate input automation was used.
The old active FTHR session remains running with combined audio, untouched. It is **not** the new Error015 engine.
Full Python suite and physical native/combined comparison: **NOT RUN**.
Microphone physical sync: **UNVERIFIED**.

## Next physical save attempt

1. When ready, exit the old FTHR normally, including the tray, then launch the new executable above. Do not run both builds against the same capture mapping.
2. Desktop capture; primary AOC 27G2G8 / DISPLAY1 (not the left LG); 1080p60, DXGI / H.264 NVENC, 30-second replay.
3. Settings → Audio → Audio Capture: enable **Keep system and microphone audio as separate tracks**, gain unchanged, before warmup. The checkbox sets audio_capture_mode=separated → separate_audio_enabled=true → engine argument 1. Previous false / “Audio mode: Combined” was consistent and unrelated to STALE_END.
4. Warm the buffer for at least 30 s after any engine restart. Play the **new moving reference** full-screen on DISPLAY1 with audible system audio; verify the background moves. Press F9 once near reference second 35.
5. Export via Settings → General → Troubleshooting → EXPORT DIAGNOSTIC REPORT, **even if F9 fails**. Keep ZIP and save/session identity; no successful MP4 is presumed.
6. Only after success, retain `C:\Users\Tom\FTHR_Clips\Desktop\desktop_clip_from_<timestamp>.mp4` and its `.mp4.fthr-audio.json` sidecar. This follows the actual configured clips_directory. Verify native track identities/markers, then process a byte-identical copy with tools/finalize_native_audio_retest.py into a fresh directory and measure native/combined decoded markers. Runner PASS is not sync proof.

A successful moving counterexperiment would isolate the static/reference limitation; it would **not** prove a production save fix. If it fails, inspect the new acquire/encoder evidence and full engine.log tail before changing capture.

**COMMIT: NONE. PUSH: NONE. Real Error015/A/V gate remains open.**

