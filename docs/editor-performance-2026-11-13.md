# Editor performance changes and validation

13 November 2026. Changes apply to the source checkout; this work does not rebuild or replace an installed executable.

## Behavior

- Preview quality offers Auto, 540p, 720p and Original, separately from export quality. The preference persists across clips. Original bypasses optional performance copies while retaining required playback compatibility handling.
- Auto can prepare a smaller SDR preview for high resolution, high pixel-rate/dense-codec footage, or measured long keyframe gaps. Explicit modes cap the preview dimensions without upscaling; portrait orientation and aspect ratio are retained. Required HDR compatibility/tone mapping remains separate.
- Optional previews prepare while paused, use short keyframe intervals, publish atomically, and are canceled when playback or paused playhead scrubbing begins. Mode and source identity are included in the cache key. Source clips and export inputs remain original.
- Paused playhead dragging requests sampled frames through one decode worker with one replaceable destination, then performs an exact native seek on release. Source changes, direction reversals and release invalidate obsolete work. This avoids repeatedly flushing the Windows video/audio backend.
- Software effect frames have a separate single-worker pool, independent of global background preparation. Auto/selected preview caps reduce software processing size during playback; the rendering cap is removed when paused. A selected proxy still contains proxy detail: choose Original to inspect source detail.
- Existing GPU effects remain in place. A new GPU renderer and rendered-effect section cache are not part of this change.

Resource limits: optional performance proxy source duration at most 15 minutes, encoding wall time at most 120 seconds, output size at most 2 GiB; existing cache pruning bounds combined completed files to eight entries/2 GiB where files can be removed. Failures retain usable source/compatibility playback. Long recordings beyond the duration bound retain their existing playback path.

## Reproducible evidence

Evidence is under `build/editor-performance-2026-11-13/`. Baseline copies of both modified implementation modules were retained before editing. The machine is an Intel i7-11800H (8 cores/16 logical processors), with Intel UHD and NVIDIA RTX 3050 Laptop graphics. Real desktop tests used Qt's Windows media backend; the synthetic fixtures use the bundled FFmpeg.

`tools/benchmark_editor_responsiveness.py` opens the actual editor with isolated in-memory settings, records real frame-delivery diagnostics, simulates 60 continuous paused drag inputs, verifies the final seek position, and saves JSON and a screenshot. Optional mode-switch tests verify preserved original dimensions, edit state and transport position. It accepts saved baseline modules for comparison. The fixture and media cache are task-specific; user recordings are not altered.

| Check | Observation |
|---|---|
| Continuous scrub, generated 1440p60 clip | Baseline: one distinct preview timestamp over roughly one second of dragging. Updated warm-proxy GPU and software checks: nine. Both final seek errors were 0 ms. |
| Native 4K30 smoke | 4K source using Auto preview: eight distinct drag preview timestamps, 0 ms final error, no reported media errors. |
| Quality changes | All four modes retained original source dimensions and edit state, with paused position exactly 2000 ms. A switch during playback retained progress rather than jumping to the preceding seek. |
| Software decoder throughput | Three sequential runs per input: 1440p60 original median 1475 ms, Auto/720p 448 ms, 540p 331 ms. All outputs retained 600 frames and 10.000 s. Source hash unchanged. These are decode-to-null timings, not application FPS. |
| Cold proxy preparation | About 1.86 s for Auto/720p and 1.83 s for 540p on the generated ten-second 1440p60 fixture. |

The throughput commands and raw results are in `proxy-throughput.json`. Initial application CPU samples were lower with proxies but are short, noisy observations and do not establish a universal CPU reduction. The software sample includes both the smaller decode input and the rendering changes, so it cannot isolate either contribution.

## Validation coverage

The baseline core editor suite passed 86 tests. A separate audio/export/HDR/timeline run passed 105 tests during implementation. Proxy-specific validation passed 43 tests including generated high-resolution, portrait, no-upscale, two-audio-track/delayed-PCM, AAC packet preservation, cancellation, atomic publication, cache identity and source-integrity checks.

Final integrated regression: **274 passed, 7 skipped in 33.24 seconds**, recorded in `integrated-regressions.xml`. Two skips were hardware Qt tests in the headless suite; the separate Windows desktop run passed all three tests in `test_quick_video_preview.py`, including both hardware tests (`hardware-qt.xml`). The remaining five skips require an explicitly selected freshly built native audio bridge DLL; that separate native build was not performed. Targeted Ruff checks passed for the two implementation modules, changed tests and benchmark tool.

New regressions cover real Qt mouse dragging with samples before release and one final native seek, reversal/release stale-result rejection, canceling proxy work on drag, preview progress while the global worker pool is saturated, no overlapping conversion after rapid clip reuse, playback-resolution limits, quality preference persistence, preservation of crop/edit state and live position, cache-failure fallback, rebuilding an evicted compatibility copy, and applying a prepared preview after releasing a trim handle.

Real desktop mode-switch smoke passed at the editor's 960-pixel minimum width and at 1440 pixels. Screenshots were inspected: the Preview control fits beside transport/status, and the 200 ms source-transition capture retains a still image while the native frame/fallback becomes ready. Evidence: `final-modes-narrow.json/.png`, `final-modes-wide.json/.png` and `final-modes-wide-pause-200ms.png`. The updated source modules' diffs against the saved baseline are `clip-viewer.diff` and `playback-proxy.diff`.

Sampled scrubbing can be slower on original long-GOP/HDR media than on a prepared proxy. Its decode lane remains bounded and can briefly wait for an already-running initial/exact frame decode. This pass does not promise native-frame-rate scrubbing.

The synthetic fixtures establish functional behavior on this machine. They do not establish performance on every GPU/driver, remote desktop environment, or unusual imported codec, and they are not a listening assessment of audio quality. No capture-engine changes or installed-app replacement were made.
