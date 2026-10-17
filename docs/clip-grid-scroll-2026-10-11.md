# ClipGrid scroll performance — 2026-10-11

The scroll regression had two main costs: every virtual date section was
recalculated twice per update, and the transparent library surface forced Qt to
repaint through its ancestors. At 200% scaling, that repaint work dominated the
GUI thread.

Changes in `FTHR_UI/ui/clip_grid.py`:

- Calculate section visibility once and release offscreen cards in one global
  pass. Resolve the viewport once per update and the card origin once per section.
- Give new card creation a 4 ms budget during scrolling, then continue on the
  next requested update. A single native card may exceed that budget; it is not
  interrupted. Pending work follows the current viewport. Idle polling stays off.
- Cache composed thumbnails in Qt's existing bounded pixmap LRU. Keys include
  source identity, dimensions, device pixel ratio, corner radius and theme color.
- Paint the library background directly and declare it opaque for opaque theme
  backgrounds. This lets Qt reuse scrolled pixels. Theme changes update the
  opacity flag and repaint the surface.

## Native application verification

`tools/profile_clip_grid_scroll.py` constructs the actual MainWindow with the
Windows Qt platform and the user's 241-clip library. Capture/hotkey/device startup
is disabled in this separate instance. Settings and a copy of the warm thumbnail
cache live in a disposable profile; original media and settings are not modified.
The user's existing capture instance remained running.

The repeatable workload sends 160 downward and 160 upward wheel events through
the application's normal SmoothScrollArea path, covering approximately 9,500
logical pixels in each direction. Profiling runs include native QWidget painting.
An 8 ms GUI timer measures event-loop delays; these measurements are not display
frame times or an FPS guarantee.

| Measurement | Original 100% | Final 100% |
| --- | ---: | ---: |
| Median virtual update | 2.52 ms | 1.49 ms |
| Maximum virtual update | 51.66 ms | 22.41 ms |
| Maximum sampled GUI timer gap | 53.68 ms | 32.66 ms |
| GUI gaps above 33 ms | 2 | 0 |

At 200%, after the layout/cache/budget changes but before opaque painting, the
median GUI timer gap was 28.76 ms, with 95 gaps above 33 ms. Opaque painting reduced
the median to 9.26 ms, the 95th percentile to 18.56 ms and the maximum to 30.42 ms,
with zero gaps above 33 ms. Live/pool counts ended at 15/10, inside the 96-card bound.

Metrics, native renders and cProfile reports are under `ui-preview/scroll-baseline`,
`ui-preview/scroll-verified` and `ui-preview/scroll-200-opaque`. Intermediate results
are also retained for comparison. These are single local profiling runs and can
vary with machine load.

Direct Windows accessibility navigation in the fresh native application verified
positions 0, 3,000, 9,000 and back to 0, including correct card placement and
thumbnail restoration across date boundaries. The Windows wheel injection tool
failed with SendInput error 87, so continuous wheel timing uses the in-process
native workload instead. The original application's view was restored to 0.

## Regression checks

79 passed, 1 skipped across card recycling, idle behavior, library scalability,
library/settings regressions, background pause, keyboard navigation and smooth
scrolling. Added checks cover one viewport lookup per update, reuse/invalidation
of composed thumbnail cache entries, and bounded card creation with subsequent
progress. Idle checks allow bounded continuation frames and still require all
layout work to stop once settled. New widget tests use pytest-qt ownership for
deterministic teardown; explicit deferred deletion caused intermittent Qt native
crashes when these tests were combined.

Ruff and Python compilation passed for the changed code and profiling harness.

The existing running application must be restarted to load the modified Python
code; verification used fresh native instances without interrupting capture.
