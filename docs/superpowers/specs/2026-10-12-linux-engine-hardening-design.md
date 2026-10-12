# Linux engine hardening before the UI overhaul

Date: 2026-10-12. Base: `linux` at `eb17039` (1.1.2-alpha).

## Goal

Make the Linux capture engine and everything the UI depends on from it
(shared-memory IPC, engine launch and connect path, hotkey socket, Hyprland
binding, AppImage packaging) correct and physically qualified before the UI
overhaul starts, so the overhaul lands on a stable base and never has to
debug capture.

Success means:

- The open Linux PR queue is merged, fixed-and-merged, or explicitly deferred
  with a comment saying why.
- Every engine defect listed below is fixed on the `linux` branch with a test
  that fails without the fix, or with a documented physical check when no
  test harness exists.
- A full session on the maintainer's Hyprland + AMD VA-API machine passes the
  qualification checklist in Phase 3.
- A `1.1.3-alpha` tag whose CI-built AppImage passes the glibc and licence
  gates, published manually as the release process requires.

Out of scope: Windows behaviour (the shared-memory layout stays byte-identical
on both platforms), UI widgets in `FTHR_UI/main.py` and `FTHR_UI/ui/` except
where a fix forces a touch, and new features (#58 window previews, #27
deduplicator, #43 thirty-minute buffer), which wait for the overhaul.

## Working rules

- Branch from `linux`; one pull request per cluster below; CI green before
  merge; squash or merge as the contributor's PR style dictates. All commits,
  PR text, reviews and comments are authored as `kalopsia-space` with no AI
  attribution.
- Contributor commits that are cherry-picked keep their original author.
- Native changes follow the existing style: pure policy structs with CTest
  coverage where a compositor or encoder is not available, physical checks on
  the maintainer machine where it is.
- The shared-memory struct, command IDs and `FTHR_SharedMemory_v4` name do
  not change. Nothing here needs a contract bump.

## Phase 0: PR queue

| PR | Action |
|---|---|
| #41 autoplay in editor | Already approved and green. Merge. |
| #19 desktop identity | Approved, 8 lines; its CI failures predate fixes on `linux`. Rebase onto `linux`, rerun CI, merge. |
| #42 VA-API `vaMapBuffer2` guard | The author kept the RAM guard that was rejected in review. Cherry-pick the symbol-probe commit and the test-hygiene bits into a maintainer branch with the author's authorship, merge that, and comment on #42 explaining the split. The RAM guard and buffer cap stay unmerged. |
| #57 Wayland screenshots + mic/camera | Org-owned branch. Two Linux CI tests fail on a missing `mic_recorder._sd` attribute. Review the whole change; fix the failing tests on the branch if the failure is test-side; request changes if it is product-side. Merge when green. |
| #16 GlobalShortcuts portal hotkeys | Rebase the contributor's commits onto current `linux` in an org branch, resolve conflicts, run the suite, physically test on Hyprland via `xdg-desktop-portal-hyprland`. Merge if it works; this removes the `input` group requirement. Done in Phase 2 because it touches `hotkey_manager.py`. |
| #43, #27, #58 | Comment: deferred until after the UI overhaul; #43 also waits on a memory-budget design. Leave open. |
| Issue #28 GLIBC_2.44 | Fixed by 1.1.2. Comment with the release link and close. |
| Issue #52, #56 | Close when #57 merges, if its review confirms the fix. |

## Phase 1: engine correctness

All in `FTHRcapture_linux/`. Each item names its test.

### 1.1 Encoder failures stop being invisible

`CaptureEngine::RunCaptureGeneration` discards `Encoder::EncodeFrame`'s return
value, so a broken encoder keeps `CAPTURE_HEALTH_ACTIVE` with an empty ring.

Design: add `EncodeFailurePolicy` to `recovery_policy.h`: a pure counter that
returns `true` (give up) after `max(fps / 2, 5)` consecutive encode failures
and resets on success. The generation loop consults it after each
`EncodeFrame`; on give-up it sets the failure reason
`"The video encoder (<codec>) stopped accepting frames"` and ends the
generation as `Failed`, so normal recovery runs and the UI eventually sees
`BACKEND_FAILED` with that reason.

Test: `capture_recovery_test.cpp` gains cases for the counter (threshold,
reset on success, minimum of 5).

### 1.2 Save no longer races recovery

`SaveClip` runs on the command thread and reads encoder extradata, size and
codec while the capture thread may be closing or reopening the encoder.

Design: the capture thread publishes an `EncoderSnapshot` (extradata, width,
height, codec id, generation number) under a mutex immediately after
`Encoder::Open` succeeds and clears it on `Close`. `EncodedPacket` gains a
`generation` field. `SaveClip` copies the snapshot under the lock, takes the
ring snapshot, and drops packets whose generation differs from the snapshot's.
If the snapshot is empty or no packets remain, the save fails with
`"Capture restarted; the replay buffer is still rebuilding"`.

Test: `linux_encoded_ring_duration_test.cpp` gains a case where packets of two
generations are pushed and `TakeSnapshot` is filtered to the newer one.

### 1.3 Recovery keeps the replay history when it can

Every generation restart clears the ring and reopens the encoder. A
compositor hiccup or output hotplug therefore wipes the buffer.

The reference incident is the maintainer's session of 2026-10-11 (1.1.1
AppImage, Hyprland, wlr-screencopy, h264_vaapi, 3.67 million frames over
7.5 h). The engine log in `~/.fthr/diagnostics/session-20261011T130520Z-*`
shows the exact sequence:

1. `[WlrBackend] Wayland frame buffer request timed out` ends the generation.
2. Recovery attempt 1: `[WlrBackend] Wayland registry discovery timed out`,
   then `[ExtBackend] Wayland registry discovery timed out`. The compositor
   was not answering at all, so this was transient.
3. The factory fell through to the ScreenCast portal, which issued
   `SelectSources` and waited 10 s for a dialog nobody answered.
4. That timeout is classed as retry-pointless, so the engine exited after one
   attempt with "the screen sharing dialog was not answered in time", and the
   UI showed CAPTURE FAILED with no automatic restart.

So a transient compositor stall became a permanent failure because recovery
re-ran first-time backend selection, including the dialog-bearing portal.

Design:

1. Split "generation" into backend generation and encoder generation. The
   ring is cleared and the encoder reopened only when the new backend's native
   size differs from the open encoder's source size, or the encoder itself
   failed. A backend-only restart with the same geometry keeps the encoder
   open and the ring intact; the monotonic PTS scheme already renders the gap
   as a gap.
2. `CaptureEngine` remembers the `BackendType` of the last generation that
   delivered frames. During recovery the factory is asked for that type only
   (`CreateBackend(type, ...)`), never the full fallback chain. The portal is
   reached only during first-time selection, which is the only time its
   dialog is acceptable.
3. Recovery failures caused by the compositor being unreachable or the output
   being absent (registry discovery timeout, output not found, display
   disconnected) do not consume the three attempts. The engine stays in
   `CAPTURE_HEALTH_RECOVERING`, publishes the reason
   `"Waiting for the compositor"` or `"Waiting for output <name> to return"`
   through `engine_string`, and retries every 2 s until it succeeds or the UI
   shuts it down. Genuine backend errors still exhaust after three attempts.
4. First step of implementation is reproducing the incident on the
   maintainer machine with the standalone engine: Hyprland `dpms off`, lock
   screen, monitor unplug, and system suspend and resume. Each is recorded
   with the path the engine takes before and after the change.

Tests: `capture_recovery_test.cpp` gains "unreachable compositor does not
consume attempts" and "recovery requests the remembered backend type only";
the factory split is unit-testable because `CreateBackend` takes the type.
Physical: the four reproductions above.

### 1.4 ext-image-copy-capture on a still screen

The ext backend waits 2 s per frame, but the protocol allows the compositor
to wait indefinitely for new content. A static desktop can exhaust recovery.

Design: on completion of a frame the backend copies it into an owned
`last_frame_` buffer. A dispatch `Timeout` (not `Disconnected`, `Error` or
`SessionStopped`) while a frame request is outstanding returns `last_frame_`
stamped with the current monotonic time and leaves the request outstanding;
the next `CaptureFrame` keeps waiting on it instead of creating a second one.
If no frame has ever completed the timeout is still a failure. This matches
the portal backend's repeat-last-frame behaviour.

Also add `FTHR_CAPTURE_BACKEND=wlr|ext|portal|x11` as a diagnostic override
honoured by `CreateBestBackend`, logged when set, documented in
`docs/linux.md`. Without it the ext path is untestable on Hyprland, which
prefers wlr.

Test: physical, with `FTHR_CAPTURE_BACKEND=ext` on Hyprland 0.56, leave the
screen static for 30 s and confirm frames keep flowing and a save succeeds.

### 1.5 Smaller engine fixes, one PR

- Frame throttle re-anchors after a stall: if a frame arrives more than two
  frame periods after `next_encode_ns`, set `next_encode_ns` to its timestamp.
- Audio: a monitor that cannot be opened at startup enters the same reconnect
  loop as a mid-session server loss, so audio attaches when PulseAudio
  returns. The startup warning is still emitted once. The audio ring is sized
  to `buffer_seconds + 10` seconds instead of a fixed 300 s.
- Portal backend: `wall_time_ns` is clamped to never go backwards between a
  real and a repeated frame.
- Transactional save on Linux: `fsync` the temp file before close and the
  directory after `renameat2`.
- `save_clip.cpp`: check the return values of `avcodec_alloc_context3`,
  `swr_init` and `av_frame_get_buffer`; report through the existing error
  string.
- Commands 4 to 9 that the Linux engine does not implement respond with
  `STATUS_UPDATE` so a caller never waits forever. The UI does not send them
  on Linux today; this is contract hygiene.
- VA-API preset: map P1 to P7 onto `compression_level` 7 to 1 for `*_vaapi`
  encoders, so the preset is not silently ignored on the qualified AMD path.

Tests: `audio_timing_test.cpp` for the ring size; `linux_encoder_timestamp_test`
for the re-anchor; the rest are covered by existing tests or physical checks.

### 1.6 Merge the `vaMapBuffer2` probe from #42

Cherry-pick as described in Phase 0. The `dl` link and the probe land with the
author's commit.

## Phase 2: engine-facing Python

All in `FTHR_UI/core/` unless stated.

- **Bridge replacement race.** `main.py` replaces `self.bridge` every 1.5 s
  in `_update_status` while the `fthr-engine-connect` thread calls
  `initialize()` on it. Design: `_update_status` does not touch the bridge
  while a connect worker for the current engine generation is alive; the
  worker owns the bridge until it reports connected or gives up. Test in
  `tests/test_capture_bridge.py` or a new `tests/test_engine_connect.py` with
  a fake process.
- **Compositor detection.** `compositor.py` checks for a `kwin_wayland`
  binary before checking for GNOME. Reorder: environment variables first,
  binary probe last. Add `XDG_SESSION_TYPE` to the Wayland decision so it
  agrees with `x11_monitor.is_native_x11_session`. Test in a new
  `tests/test_compositor_detection.py`.
- **Hyprland binds.** Generate binds for every socket action the UI exposes
  on Linux (confirm and dismiss game detection, not just save actions); skip
  actions with an empty key; check for the current socket path, not the
  legacy `fthr_hotkey.sock`; skip `hyprctl reload` when the generated content
  is unchanged. Extend `tests/test_hotkey_config_migration.py` or add
  `tests/test_hyprland_binds.py`.
- **Hotkey messaging.** One truth. After #16 the message is "global hotkeys
  use the desktop portal; no root or input group needed". If #16 does not
  merge, the message states the `keyboard` library needs the `input` group
  and the build banner and docs say the same.
- **Uploader secrets.** `upload_manager._atomic_json` writes `settings.json`
  (holds `catbox_userhash` and the auth header) with the default umask. Write
  it 0600. Test with `os.stat` in `tests/test_upload_manager_retry.py`.
- **#16 GlobalShortcuts portal.** Rebase and test as in Phase 0. The physical
  check is: bind F9 and F12 through the portal on Hyprland, save a clip and a
  screenshot, confirm no `keyboard` import happens on Linux.

## Phase 3: packaging, docs, qualification, release

- **Ship ffmpeg and ffprobe.** The pinned BtbN tarball already contains them
  and `fetch_third_party.py` patches their RPATH. `FTHR_linux.spec` adds them
  under `_internal/engine/` (the first place `ffmpeg_tools.py` looks) with
  RPATH re-patched to `$ORIGIN/..` so they find the libraries in `_internal/`.
  `tools/ffmpeg_manifest_linux.json` gains their sha256 and
  `verify_release_licenses.py` verifies them like the libraries. The distro
  `ffmpeg` fallback remains only for source checkouts. `THIRD_PARTY_NOTICES.md`
  gains the Linux FFmpeg entry (n8.1.2-34, LGPLv3, manifest path).
- **Docs.** `BUILDING.md`: remove the non-existent `FTHR_EXPERIMENTAL_X11GRAB`
  option and describe `FTHR_X11_CAPTURE` (default ON); list the real
  `build_linux.sh` steps; state that the glibc gate means a shippable
  AppImage comes only from CI or a glibc ≤ 2.35 host; drop the systemd-scope
  paragraph that no code implements; replace the stale Ubuntu 24.04 "verified
  build state" with a pointer to CI. `docs/linux.md`: document
  `FTHR_CAPTURE_BACKEND`, the output-absent recovery behaviour, the bundled
  ffmpeg, and the hotkey truth from Phase 2. `tools/linux_system_report.sh`:
  mention the portal backend in the "engine will try" line. Comments that say
  "Ubuntu 24.04 baseline" change to 22.04.
- **CI hygiene.** `verify_release_licenses.py --all` looks for
  `build/AppDir`; point it at `build_output/AppDir`. Make the AppImage job's
  pip cache key include `requirements-dev.txt`.
- **Qualification checklist** (maintainer machine, Hyprland 0.56, AMD RX
  7900 XTX, two 1080p outputs, PipeWire): fresh AppImage from CI launches;
  wlr-screencopy capture on each output; `FTHR_CAPTURE_BACKEND=ext` static
  screen 30 s then save; `FTHR_CAPTURE_BACKEND=portal` picker flow and
  restore token; DPMS off and on without losing the buffer; monitor unplug
  and replug recovers; PulseAudio restart mid-session keeps audio; encoder
  forced to software and to VA-API; F9 and F12 through the portal; export
  with watermark uses the bundled ffmpeg (check `engine.log` path); 2-hour
  idle soak with no health transition.
- **Release.** Bump `FTHR_UI/version.py` to `1.1.3-alpha` with the date,
  write `RELEASE_NOTES.md`, run the release verification scripts, tag
  `v1.1.3-alpha` on `linux`, let CI build the AppImage, publish manually.

## Order and dependencies

Phase 0 first because later branches would conflict with #57 and #16.
Phase 1 items 1.1 through 1.3 share `capture_engine.cpp` and ship as one PR;
1.4 and 1.5 are separate PRs; 1.6 can land any time. Phase 2 items are
independent PRs; #16 is last in Phase 2. Phase 3 after everything else.
Physical checks happen at the end of each phase on the maintainer machine
with the project venv at `.venv-linux` and the engine built in
`FTHRcapture_linux/build`.
