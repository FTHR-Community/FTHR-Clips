# Settings animation regression — 12 October 2026

Fixed animated main/settings navigation and animated settings-category changes. Animations remain enabled, with the same 220 ms duration, easing, directions and full-resolution images.

## Cause and scoped change

The recent memory optimization replaced the incoming screenshot with a live incoming QWidget page. SnapshotSlide then moved that entire page on every frame underneath a transparent Qt Quick surface. Large settings trees and the clip library repeatedly repainted during the animation, particularly at higher DPI. Disabling animations avoided that path.

ShellPageStack and SlidingStackedWidget now capture both pages once, leave the live incoming page at its final geometry, and animate an opaque raster overlay. Filling the stack's palette background is necessary even when a grabbed pixmap has an alpha channel: without the opaque-paint declaration, Qt still repaints the underlying controls each frame. Device pixel ratio is preserved; images are not downsampled.

SnapshotSlide's default GPU/live-page mode remains in use for editor/video transitions. Capture, export, settings persistence, lazy category construction, and background-worker scheduling were not changed. Resize, hide, interruption and reduced-motion handling release both snapshots. No persistent screenshot cache was added.

The tradeoff is a second temporary image during these shell/settings slides. A physical 3840×2160 RGBA image has about 31.6 MiB of pixel payload. It is released with the outgoing image when the transition ends. Capturing the second image can add startup work; this fix targets repainting and stutter throughout the animation rather than eliminating snapshot cost.

## Native measurements

The benchmark constructs the real MainWindow and uses a read-only copy of the warm thumbnail cache and the user's library paths in a disposable profile. Capture, hotkey, microphone and device-probe startup are disabled. Pages are warmed before measurement so lazy page construction is not confused with animation cost. Eight main/settings switches and twelve category switches are measured, each with a 300 ms observation window. Python profiling and paint-event counting are enabled in all reported comparisons.

Average measurements at **200% scaling**, 1280×976 logical window:

| Measurement | Before: main/settings | After: main/settings | Before: categories | After: categories |
|---|---:|---:|---:|---:|
| Process CPU time per observation | 363.3 ms | 218.8 ms | 316.4 ms | 145.8 ms |
| Widget paint events | 486.1 | 222.0 | 434.6 | 135.5 |
| Animation updates per slide | 7.4 | 14.0 | 10.3 | 14.0 |
| Worst animation-update gap per slide | 37.0 ms | 17.1 ms | 35.1 ms | 17.0 ms |
| Synchronous switch setup | 69.2 ms | 79.8 ms | 26.6 ms | 33.0 ms |

At **100% scaling**, average CPU time fell from 296.9 to 140.6 ms for main/settings and from 200.5 to 74.2 ms for categories. Synchronous setup was 58.9 ms and 18.9 ms respectively after the change, versus 64.0 ms and 20.2 ms before.

CPU figures include the process's other UI work during the observation window and may exceed wall time because of threads. Animation-update timestamps are not a display-present/FPS guarantee. These are individual local instrumented runs, not controlled hardware-wide performance claims. Final before/after measurements were taken without a simultaneous test suite. The settled 2560×1952 settings screenshots at 200% were pixel-identical.

## Verification

- **122 focused tests passed** across settings transitions, lazy settings behavior, keyboard navigation, consistency/themes, UI memory and editor sessions. `Output/settings-transition-focused.xml`.
- **14 native Windows tests passed at 200% scaling**, including transition pixels, no live-page repaints/movement during raster animation, input/state preservation, interruption, resize/hide cleanup, real clip playback through repeated switches, GPU effects and GPU surface lifecycle. `Output/settings-transition-native.xml`.
- The reduced-motion GPU lifecycle test now exercises the editor, which still uses GPU transitions. An existing native pixel assertion was corrected to sample physical pixels using the image's device pixel ratio.
- Ruff passed for changed source/tests; Python compilation passed for changed production files.
- No packaged executable was rebuilt. Restart the source app through `launch_windows.bat` to load the changes.

## Artifacts

- `Output/settings-transition-before-2026-10-12.zip`: pre-change production source and the affected memory test.
- `Output/profile_settings_transitions.py`: isolated native benchmark. `--mode current` runs deployed behavior; the other modes record process-local experiments. Historical baseline results used pre-change source from the backup.
- `Output/settings-live-before/`, `Output/settings-live-200/`: original native measurements.
- `Output/settings-final-100/`, `Output/settings-final-200/`: final native metrics, profiles and screenshots.
- `Output/settings-frozen-gpu/`, `Output/settings-frozen-raster/`, `Output/settings-raster-200/`: experimental comparisons. Early experiment runs completed measurement but reported a temporary log-file cleanup error; the harness now closes its log before cleanup.
