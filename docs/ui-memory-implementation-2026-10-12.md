# UI memory implementation — 12 October 2026

Follow-up: shell/settings slides now use two temporary full-resolution snapshots to avoid repainting live settings trees during animation. Editor slides retain the live incoming video page. See `settings-transition-fix-2026-10-12.md` for native measurements, cleanup tests and the memory tradeoff.

Implemented the six follow-up memory recommendations in application source, plus a persistent user switch for the editor filmstrip. The installer and bundled executable have not been rebuilt.

## Timeline preview setting

**Settings → Clip → Clip Editor → Show timeline preview strip** is enabled by default. Turning it off cancels in-flight thumbnail decoding, rejects stale worker results, releases local filmstrip pixmaps and scaled images, and clears the shared filmstrip cache. Subsequent clip switches and editor reopenings do not start filmstrip jobs until it is enabled again. Audio preparation still runs: its idle scheduling previously shared the filmstrip timer. Time labels, seeking, trim handles, splits, deletion, zoom, undo and export remain available.

The preference is stored as `timeline_preview_strip_enabled` and applies immediately to editors using that settings manager, including the parked shell.

## Memory changes

- Timeline image reuse has a **20 MiB** byte budget and an eight-entry limit; scaled timeline images have a **4 MiB** budget. Image entries expire after two idle minutes through one application maintenance timer. Oversized individual entries are not retained. The active timeline holds its own display images, so cache eviction does not remove the currently displayed strip.
- A hidden editor has a **15-second** grace period before releasing its timeline display resources. The inactive GPU video surface is retired and rebuilt on the next clip load. Qt scene-graph invalidation alone left the render target and driver allocations alive in the stress test. A lightweight hidden native placeholder keeps resize/event handling safe; editor controls and session state remain reusable. Active editing, exports and restore animations do not trigger this cleanup.
- Library recent thumbnails have an **8 MiB** byte budget. After 15 seconds in the background, recent images and offscreen card images are cleared and spare controls are reduced to one row. The currently displayed viewport, metadata, scroll position, active rename and mutation controls remain intact. Thumbnail jobs are requested again on resume. Only a discarded card's own rendered image is removed from the shared Qt pixmap cache.
- Overlay image previews are decoded for display size and device pixel ratio using 128-pixel tiers. Resizing or enlarging a layer requests the appropriate tier. The cache holds at most **16,000,000 bytes** and 12 entries, with idle expiry. Hidden overlay previews release their own layer references after 15 seconds and reconstruct on show. Original overlay files and export inputs are unchanged.
- Screenshot editing retains a viewport-sized preview and source dimensions. The crop selection accounts for letterboxing and retains exact source coordinates through repeated resizes. A worker reads the original image only during saving, applies orientation, crops and writes the staged PNG. Full-resolution output, alpha and the existing publication/collision behavior are preserved. Display previews adapt to resize and DPI changes.
- Shell, settings and editor slides retain **one outgoing full-resolution snapshot** and animate the live incoming page. No incoming screenshot is captured. Layout stays fixed while its position changes. Completion, interruption, resize and hide restore the page geometry and release provider images; the GPU and raster composition paths are covered. This removes one full-window image allocation per slide without reducing image resolution.

## Validation

- **121 final focused tests passed**, including settings persistence, cancellation during buffered worker output, no filmstrip jobs across disabled clip switches, audio preparation with thumbnails disabled, cache eviction/expiry, idle state restoration, offscreen library recovery, overlay resizing and idle reconstruction, 8K original-pixel cropping, repeated crop resizes, EXIF orientation, live-slide clipping and interrupted layouts. Results: `Output/ui-memory-focused.xml`.
- **11 native Windows GPU tests passed**, including live incoming video with one provider image, scene-graph invalidation, parked-surface destruction and recreation, rapid switching between real videos, GPU effects and transition cleanup. Results: `Output/ui-memory-native.xml`.
- The broad suite recorded **1,405 passed, 12 skipped and five failed**. The failures are the previously documented stale monitor-identity test, undocumented Ko-fi asset, and three hover tests that pass in isolation. Two tests with hand-constructed library fixtures were updated to provide the new one-shot idle timer; their pause/resume ownership checks pass. Final crop/DPI refinements were additionally checked in the focused run after this broad run. Results: `Output/ui-memory-full.xml`.
- All changed application modules and tests pass Ruff.

## Synthetic Windows memory check

`Output/ui_memory_stress.py` generates its own two 1080p videos and exercises 50 rapid animated clip switches and 50 hidden/restore cycles. It shortens only the test's idle grace period and verifies playback, reused controls and preserved edits. It reads process private bytes, working set and Windows GPU Process Memory counters separately.

With surface retirement, private bytes across restore-cycle samples 10 through 50 remained approximately **1,343–1,345 MiB** instead of continuing to rise. The shared timeline cache was empty after idle cleanup. Shared GPU memory fell from approximately **649 MiB active to 507 MiB idle**; dedicated GPU usage was zero on this machine. Process and driver pools retain allocations after cleanup, so these figures are not predictions of normal-user savings or a claim that all GPU allocations return immediately to the initial baseline. Results: `Output/ui-memory-stress.json`. The earlier scene-graph-only experiment is retained as `Output/ui-memory-stress-invalidation-only.json`.

Source backup before implementation: `Output/ui-memory-before-2026-10-12.zip`.
