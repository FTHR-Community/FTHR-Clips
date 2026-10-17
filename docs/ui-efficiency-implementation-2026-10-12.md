# Further UI CPU and memory improvements — 12 October 2026

Implemented all seven recommendations from the follow-up UI audit. Changes are in application source; the bundled executable and installer have not been rebuilt. Capture, recording, export processing and source media files are unchanged.

## Changes

1. **Duplicate keyboard preview frames are skipped.** The UI compares the source timestamp, window identity, chroma color and intensity before repeating resize, color conversion, chroma processing and display allocation. Changing preview settings refreshes the same source frame immediately. Released images are reconstructed even when the source timestamp is unchanged. Hidden previews do not process frames. The capture worker and its frame-access API are unchanged.
2. **Settings categories are constructed on demand.** General is the initial page; Clip, Audio, Visuals, Customize, Performance and Shortcuts remain lightweight placeholders until requested. Rapid tab switching constructs only the final requested category. Existing controls are reused thereafter. Direct category navigation and historical explicit control access also construct the required page. Background pause, device-handshake callbacks and status updates do not create unopened categories. Audio and encoder controls synchronize with whichever categories already exist. Pending codec/preset changes survive opening another category; Performance can apply its preset without constructing Clip.
3. **Overlay preview backgrounds are display-bounded.** The unified canvas retains source paths, decodes for its display/DPI tier through the existing byte-bounded image cache, and reloads after resize or idle release. Missing or invalid custom backgrounds fall back to the bundled image. Background references and matching cached images are released after 15 seconds hidden. Source files and output quality are unaffected.
4. **The owned keyboard source image is shared with its display widget.** This removes the extra deep copy on every refreshed preview. The default image API still deep-copies caller-provided images, preserving independence for externally backed pixel buffers. The hidden source-preview widget also releases its image after 15 seconds.
5. **Settings → Performance → Reduce interface animations** is persistent and off by default. It makes snapshot hover changes, library share-button hover changes, Settings/shell/editor slides, editor minimize/restore, and popup/editor opening fades immediate. Enabling it during a transition completes the geometry/visibility handoff, removes retained snapshot images, and retires reusable transition render surfaces. Re-enabling normal animations reconstructs those surfaces. Editor restore retains the saved normal/maximized window state and edit state.
6. **Live overlay updates repaint their affected regions.** Camera and keyboard frames request only their own rectangles. Painting skips image layers and other live overlays outside the dirty region; overlapping layers still draw in their existing order. This avoids adding another static image cache solely for compositing.
7. **Timeline mappings invalidate on edits.** A list-compatible interval collection tracks structural changes and segment-field mutations, including direct mutations, replacement, split/delete, undo and clip changes. Unchanged mapping checks no longer scan all intervals. Weak observers avoid retaining abandoned collections and support intervals shared between collections. Long playhead seeks dirty the two marker rectangles instead of the whole strip between them. Retained-duration labels reuse the mapped total.

## Correctness and regression checks

- **149 focused checks passed**, including the new behavior tests, settings navigation and themes, audio/encoder synchronization, editor sessions and undo, timeline thumbnail reuse, bounded image cleanup, and background suspension. Results: `Output/ui-efficiency-final-focused.xml`.
- **Nine native Windows checks passed**, including actual GPU snapshot surface retirement/recreation, live video transitions, and reduced-animation editor restore/navigation. Results: `Output/ui-efficiency-final-native.xml`.
- **Full suite: 1,437 passed, 13 skipped, five failed.** The failures are the previously documented stale monitor-identity fixture, undocumented Ko-fi asset, and three order-dependent hover tests. The seven hover tests pass together in an isolated process (`Output/ui-efficiency-hover-isolated.xml`). No additional failures remain in the full run. Results: `Output/ui-efficiency-final-full.xml`.
- All changed source and test modules pass Ruff.

The first verification runs found and corrected lazy-loading regressions in direct stack navigation and early encoder results. New tests also caught observer cleanup treating two equal interval lists as the same observer; cleanup now uses reference identity. Three older test fixtures were updated for lazy category construction without weakening their behavior assertions.

## Isolated construction measurement

With hardware/device workers disabled and six measured repetitions after warm-up, constructing the initial General-only Settings view took a median **93.0 ms** and created **182 descendant widgets**. Constructing all seven categories in the same implementation took **540.0 ms** and created **1,105 descendants**. Work for an unopened category is deferred to its first visit; once created, its controls remain reusable.

Ten thousand unchanged mapping checks on a 1,000-segment timeline took approximately **3.0 ms**. These are isolated UI measurements, not end-to-end CPU percentages or process-memory savings. Results: `Output/ui-efficiency-benchmark.json`.

Application-source backup before this implementation: `Output/ui-efficiency-before-2026-10-12.zip`.
