# Editor switching fixes — 12 October 2026

Implemented the actionable source findings from `ui-followup-audit-2026-10-12.md`.

## Behavior changes

- Individual clip changes retain the full-resolution slide. Subsequent navigation within 250 ms switches immediately without another window snapshot. Every requested clip still opens, including direction reversals. Finishing the old slide restores page geometry and releases its image before reuse. Animation resumes after navigation settles; the reduced-animation preference still applies.
- Fullscreen restore geometry and the maximized flag survive clip replacement. Exiting fullscreen restores maximized state directly, avoiding a Windows state-transition race caused by calling showNormal immediately before showMaximized.
- The quick-crop save button resets when clips change. Repeated saves restore the canonical label instead of accidentally retaining the temporary `saved` text.
- Optional playback proxies are admitted only after media is ready and playback is neither active nor queued. Pausing schedules preparation after 1.5 seconds; paused seeks restart that delay. Starting playback cancels pending/running proxy preparation through its own token without cancelling the clip's other media work. Late results cannot overwrite a newer attempt, and callback receivers are released on completion/cancellation. Original playback, cached proxies and full-resolution exports are retained.

## Verification

- 131 focused tests passed: editor switching and sessions, navigation, consistency, memory, playback performance and lifecycle, and proxy generation. `Output/followup-fix-focused.xml`.
- 20 native Windows tests passed after the final change: actual generated-video playback through repeated switches, final video color, retained GPU output/effects, transition clipping and cleanup, GPU resource lifecycle, and the new fullscreen/navigation regressions. `Output/followup-fix-native.xml`.
- Broad run: 1,441 passed, 13 skipped, six failed. The failures were the existing undocumented Ko-fi asset, three previously documented order-dependent hover checks, and two event-loop timing checks. All ten hover/event-loop tests passed together in isolation. The broad run overlapped other verification, so the timing failures are not evidence of a new capture regression; neither are they silently counted as passes. Logs: `Output/followup-fix-full.txt`, `Output/followup-fix-isolated.txt`.
- Ruff and Python compilation passed for changed production files; Ruff passed for both changed test modules.
- Both standalone state reproductions now restore maximized state and show `Save as quick crop` after replacement, including the native Windows reproduction.

The final focused/native runs include the callback-receiver lifetime refinement made after the broad run. No claim is made that the entire repository test suite is green or that every physical monitor/codec configuration is validated.

## Switching measurements

The same isolated offscreen workload performs twelve rapid replacements, processing events and ending any slide after each replacement. Current burst policy skips captures after the first step:

| Rapid navigation | Before median | After median | After maximum |
|---|---:|---:|---:|
| 1080p | 14.28 ms | 3.51 ms | 16.02 ms |
| 4K | 28.25 ms | 3.60 ms | 29.19 ms |

The first isolated animated step still incurs full-resolution capture. This is a change in rapid-navigation presentation, not a faster snapshot implementation. Results exclude media/probing/audio, native GPU composition and animation playback; they are not whole-app CPU or first-correct-frame measurements. The original before run overlapped tests, so exact speedup ratios should not be treated as a controlled native benchmark. Artifacts: `Output/followup-fixed-editor-switch-cost-timed.json` and `Output/followup_fixed_editor_switch_cost.py`.

## Running the fixed version

`launch_windows.bat --diagnose` confirms **source checkout** mode using `C:\Python314\pythonw.exe` on this machine. Launch with `launch_windows.bat` to load the updated application source. Existing source processes must be restarted to pick up the changes.

The old bundled executable/installer was not rebuilt or installed. The source release gate reports one blocking pre-existing undocumented asset, `FTHR_UI/assets/icons/kofi_symbol.png`; its redistribution origin was not invented or approved by this task. Inspection also found stale Quick/QML exclusions and binary filters in `FTHR.spec`, despite the current GPU UI requiring those modules. Packaging needs its runtime inventory/filter updated and verified before shipping a new bundle. These constraints do not affect the verified source launcher. User settings and media were not changed.
