# UI port — October 11, 2026

The reference UI from `C:/Users/nombo/Downloads/FTHR-Clips-experimental/FTHR-Clips-experimental` is now integrated into this source checkout.

## Included

- Current near-black green surfaces, mint accent, Oswald Regular/SemiBold/Bold, supplied SVG icons, rounded controls, compact title strip and recording toolbar.
- Persistent Clips/Settings sidebar, reusable settings pages, Shortcuts category and customizable application-local keyboard bindings.
- Up to three editor sessions in the sidebar: park/restore edits and position, switch clips in one persistent window, autoplay newly opened clips, and release multimedia resources when parked.
- Thumbnail-to-editor actions, filename rename through double-click/F2, upload/copy-link and confirmed delete actions, preserving manifests, metadata and upload history.
- Smooth scrolling, coalesced virtual library updates, recycled cards and bounded thumbnail work.
- Theme migration preserves intentional custom colors, fonts, sounds and icons.

## Preserved

All 338 native capture files match `HANDOFF-MANIFEST.json`. Existing engine launch/shutdown generations, storage validation, Windows DPI/window geometry, error incident recovery, microphone gain safety and asynchronous camera startup remain. The stable playback renderer, frame backpressure, paused seek handling, shared-memory contract, audio finalization and Error015 safeguards were retained. Existing fullscreen behavior works through the persistent editor window.

The optional uploader keeps this newer checkout's existing backend/provider contract. The reference's older capture and playback implementations were not copied.

## Validation

The complete existing suite plus imported tests passed in partitioned runs: the main run excludes the three library idle tests, which passed separately; the additional embedded-editor fullscreen regression also passed. Total: 1,354 passed, 50 skipped. Ruff and the exception-handling contract pass. Native-source identities and important backend method bodies were checked against the original handoff.

The Windows Qt offscreen process intermittently aborted when all library idle tests ran in the same process as the full suite; those tests passed in the separate library run. Platform, optional plugin, native compiler and unbuilt audio bridge checks are skipped as reported in the saved logs.

FFmpeg/FFprobe from the existing reference `dist/topbar/FTHRClips/_internal/engine` were added to PATH only in test subprocesses for real media tests. No older capture executable was copied or used to validate native capture.

Real Qt screenshots are under `ui-preview/oswald/`, including `settings.png`, `library-small.png`, `shortcuts.png`, `clip-card.png`, and `clip-editor.png`. Recreate them with `python tools/render_ui_preview.py`; it uses an isolated profile and stubs capture services. Validation logs are under `ui-preview/validation/`.

This is a source update, not a rebuilt installer or native runtime. Follow BUILDING.md and the existing HANDOFF.md for native build and hardware qualification. Original changed application files are backed up in `.ui-port/originals.zip`.
