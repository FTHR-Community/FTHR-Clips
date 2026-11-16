# Backend resilience implementation and verification — 2026-11-15

Implemented in the source checkout. No installer was packaged, installed, or published.

## Changes

1. **AMD scaling:** initialization creates and validates a reusable BGRA render-target input texture/view for video processing. Ineligible captured textures (including shader-resource-only textures from this incident) are copied on the GPU. Compatible inputs still use their original view, and native-size capture retains its existing direct-copy path. Resource cleanup covers reinitialization.
2. **Display/focus recovery:** transient monitor geometry changes gate stale frames and can resume the existing encoder. Stable geometry changes request a serialized source rebuild. Device removal/reset/access loss are classified for recovery. An already-consumed request cannot bypass bounded retry scheduling via the UI heartbeat. See `backend-recovery-verification.md` for thresholds and ownership checks.
3. **Replay RAM:** automatic memory mode chooses the longest replay duration that fits the same conservative admission estimate (minimum five seconds). This is selected once per native generation. Explicit budgets, disk mode, and explicit low-resource override behavior stay intact. Audio retention and video presentation use the adapted duration; decoder preroll is not exposed as extra history. Original save duration is retained in diagnostics. No automatic codec, bitrate, resolution, or saved-preference change occurs.
4. **Visible duration:** a persistent notice explains the requested/effective replay duration and opens Storage settings, including tray startup. A later full-duration startup clears it. Every initialized generation publishes its confirmed duration so diagnostics cannot retain an earlier reduction.
5. **Previous-session evidence:** diagnostic ZIPs include bounded prior-session engine/events logs and identity when available, using existing sanitization. Only validated app session directories with matching session-start evidence are accepted; linked logs/directories and arbitrary files are excluded. Existing retention remains bounded.

## Documentation cross-check

- Microsoft input-view resource requirements explain why shader-resource-only BindFlags=8 is not a supported input-view combination. Default usage with render-target binding is supported: https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11videodevice-createvideoprocessorinputview
- AMD's AMF converter and encoder documentation describe native GPU surfaces and conversion/encoding as distinct operations. This fix addresses our D3D11 preparation stage before FFmpeg submits frames to AMF; it does not change AMF encoding properties or pretend that a local Intel test qualifies AMF encoding: https://github.com/GPUOpen-LibrariesAndSDKs/AMF/blob/master/amf/doc/AMF_Video_Converter_API.md and https://github.com/GPUOpen-LibrariesAndSDKs/AMF/blob/master/amf/doc/AMF_Video_Encode_API.md
- Microsoft's WGC resizing/device-loss guidance supports recreating resources when capture geometry/device changes: https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture#react-to-capture-item-resizing-or-device-lost

## Verification completed

- Isolated Release x64 production build: passed; final build log empty (no compiler/linker diagnostics).
- Native suite: **31,077 checks passed**, including AMF input preparation 308, replay memory 1,955, and capture backend policy 38. Local Intel hardware reproduced the old E_INVALIDARG call and verified corrected 1600x900 to 1280x720 output with pixel readback, array slices, intermediate reuse, and eligible-source direct handling. Existing native test build emitted debug-symbol PDB warnings; runtime suite passed. The production build used a clean isolated intermediate directory.
- Recovery/UI Python suite: **187 passed**. Exact command in the recovery report.
- Diagnostics/media-lifecycle/storage Python suite: **46 passed** (`test_field_diagnostics.py`, `test_media_resource_lifecycle.py`, `test_replay_budget_ui.py`, `test_replay_storage_settings.py`). Includes prior-session redaction/link exclusions and restoration of full-duration diagnostic state.
- Final-source Intel and NVIDIA physical WGC WINDOW harnesses: **passed** initial capture, resize, minimize, restore, three saved clips with full FFmpeg decoding, and closed-source rejection. These are video-only fixtures.
- Shared-memory layout, engine response-publication, exception-handling contracts: passed. Changed Python files: Ruff passed.

## Evidence locations

All paths below are relative to the parent of this checkout:

- `backend-forgiving-baseline/`: original edited files for review/recovery.
- `backend-forgiving-changes.patch`: unified source/test diff against those originals.
- `backend-forgiving-production/bin/FTHRclips.exe`: rebuilt native engine and accompanying runtime files.
- `backend-forgiving-production-final-build.log`: final production build.
- `backend-forgiving-final-native-tests.log`: final full native suite.
- `backend-forgiving-final-intel/result.json` and `backend-forgiving-final-nvidia/result.json`: final hardware fixture outcomes, decoded-media evidence and logs.

## Remaining release qualification

This machine has Intel UHD and NVIDIA RTX 3050 Laptop graphics, no Radeon. Do not describe the fix as Radeon-qualified yet. On the affected Radeon 780M, test native, 1080p-to-720p and 900p-to-720p H.264 capture, repeated Fortnite focus/fullscreen mode transitions, saves across transitions, and manual recording. Run a longer recording with audio and inspect synchronization, resource growth and valid saved clips. Sleep/wake and physical monitor disconnect/reconnect also remain hardware checks. Ordinary WGC WINDOW fixture results do not prove the protected-game WGC MONITOR route. The complete repository Python/release packaging suite was not run; focused suites and capture contracts were run for this change.
