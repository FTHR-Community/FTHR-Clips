# UI CPU and consistency follow-up — 12 October 2026

Follow-up: fixes and verification are recorded in `ui-followup-fixes-2026-10-12.md`. The observations below describe the pre-fix source; rerunning the standalone state reproduction now reports the corrected behavior.

Application source was not changed. This audit inspected the current source, saved earlier source and local implementation reports, ran the focused regression suites, and reproduced switching/state behavior in isolated offscreen Qt processes. There is no Git repository in this folder, so dates below come from saved artifacts rather than commit history. No running FTHR/Python process was found during inspection; the executable used for the reported spikes could not be identified.

## Findings and priorities

### 1. Verify which build is being used before evaluating the fixes

The local `dist/FTHRClips/FTHRClips.exe` is timestamped October 11 at 14:38; the installer in `Output` is timestamped October 11 at 15:04. Editor source was modified October 12. The performance, memory and efficiency implementation reports all explicitly state that the executable/installer was not rebuilt. A restart of the bundled executable therefore cannot load these newer Python source changes.

Saved source in `Output/ui-audit-baseline` constructs a complete ClipViewer for each replacement, reapplies the host stylesheet and takes outgoing/incoming snapshots. Its audio-manifest hash also lacks cancellation. Those are credible causes of expensive switching in an older build. The historical isolated benchmark recorded 84.4 ms at 1080p and 103.8 ms at 4K for animated replacement. Those are previously recorded results, not a new comparison of two native builds.

Next action: reproduce with an explicitly identified source build or rebuilt bundle and record its build identity. Do not assume the October 12 fixes are deployed.

### 2. Full-window capture remains the main measured synchronous switching cost

`FTHR_UI/ui/clip_editor_window.py:64` grabs the outgoing window before every visible animated navigation. GPU composition and the live incoming page reduce later copying but do not remove this synchronous capture.

Fresh measurements on current source, twelve replacements per case:

| Case | Median replacement | Median snapshot |
|---|---:|---:|
| 1080p, no transition | 4.42 ms | — |
| 1080p, animated | 14.28 ms | 9.27 ms |
| 4K, animated | 28.25 ms | 22.91 ms |

At 4K, capture alone exceeded a 16.7 ms frame budget. Rapid navigation also calls `_finish_clip_slide` before starting another 220 ms slide, so interrupted transitions finish their geometry and restart; uneven motion is plausible even if key dispatch is correct.

These measurements disable media opening, probing, audio and filmstrip generation. Animation playback is ended immediately after replacement. They exclude native GPU/compositor cost and do not measure process-tree CPU or first-frame latency. Other regression work ran concurrently, so interpret exact timings as indicative. One page and 125 widget descendants remained after each workload.

Next action: compare the same real-media workload with the existing Settings → Performance → Reduce interface animations preference, then profile native capture and media startup separately. This is an available diagnostic toggle; it was not changed on the user's profile.

### 3. Proxy preparation can still create a delayed CPU spike while browsing

`clip_viewer.py:4691` starts optional proxy preparation after 650 ms. `_prepare_playback_source` does not require playback to be idle. `core/playback_proxy.py:102` probes the source; eligible long-GOP clips up to five minutes can then be fully software-encoded (`:132`). Process count, thread count, priority and cancellation are already bounded, but they do not eliminate the CPU cost. The delay explains why some clips can feel cheap initially and become expensive shortly afterwards.

This is source-confirmed scheduling, not a measured attribution of the user's spike. Rapid visits shorter than the delay cancel the work; longer visits can start it. Next action: measure Qt process and ffmpeg/ffprobe child CPU separately on cold versus warm proxy caches, and consider admission based on playback/interaction activity in addition to elapsed time.

### 4. Reusing the editor loses fullscreen restore state — reproduced

`clip_viewer.py:4549` resets `_fullscreen_normal_geometry` and `_fullscreen_was_maximized` every time `_initialize_clip` runs, even though the containing window survives clip replacement. `_exit_fullscreen` depends on those values.

Reproduction: maximize editor → enter fullscreen → replace clip → leave fullscreen. Before replacement, saved-maximized was true and geometry was present. After replacement, saved-maximized was false and geometry was None. Leaving fullscreen produced a normal window instead of restoring maximized state. This was reproduced offscreen; native mixed-monitor geometry still needs verification.

Fix direction: retain window-lifetime state on the persistent host, or initialize it only when constructing the page. It must not be reset with per-clip editing state.

### 5. Temporary quick-crop feedback survives into another clip — reproduced

`clip_viewer.py:7853` changes the button to `saved`, then uses a clip-owned delayed callback to restore the label. Switching cancels that callback, but `_reset_clip_ui` never restores this button's label. The controls now survive navigation.

Reproduction: save a quick crop → immediately replace clip → wait 1.9 seconds. The button remains `saved` instead of `Save as quick crop`. The reproduction uses an in-memory settings stub and does not save user preferences.

Fix direction: reset all transient feedback in the reusable page's reset routine. Keep cancellation guards; allowing the old callback to update the new clip would reintroduce stale-result bugs.

## What has already improved

Current source reuses the editor controls and renderer, skips identical host stylesheets, cancels obsolete probes/hashing, guards stale callbacks and delays optional proxy work. Timeline rendering and caches are already optimized and bounded. The older suggestions to introduce these mechanisms should not be treated as new work. Default clip navigation also suppresses key autorepeat; the audit did not establish duplicate key dispatch as the cause of the spikes.

The October 12 UI efficiency changes add reduced animations and lazy settings construction. The memory changes evict hidden editor GPU resources after 15 seconds; reopening after that grace period intentionally reconstructs the video surface, so warm and idle reopen costs differ. This is a memory/latency tradeoff, not a confirmed defect.

## Validation and artifacts

- 94 existing tests passed across editor switching performance, keyboard navigation, UI consistency, editor sessions and UI memory. The two newly reproduced state cases are not covered by those existing tests.
- `Output/followup_editor_switch_cost.py`: isolated timing harness copied from the existing audit, with separate output names to preserve historical measurements.
- `Output/followup-editor-switch-cost-timed.json`: fresh timing results.
- `Output/followup_state_repro.py` and `Output/followup-state-repro.json`: standalone state reproductions and observed values.
- No application code, user media, settings or packaged binaries were changed.
