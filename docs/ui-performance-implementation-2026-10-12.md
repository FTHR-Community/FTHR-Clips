# Editor UI performance implementation — 12 October 2026

Implemented the changes proposed in `ui-performance-audit-2026-10-11.md` in application source. No installer or executable was rebuilt.

## Changes

- Reuse the editor page, controls and video presentation surface across clip navigation. Reset clip-specific state and preserve per-session edits, undo, position and metadata. Save text that is still being edited to the outgoing clip before reuse. Retain one inactive editor shell for minimize/restore; other sessions remain data.
- Skip unchanged Qt stylesheets and cache bundled icon path resolution.
- Compose full-resolution transition snapshots through a shared Qt Quick engine, with a raster fallback. Release snapshots on completion, interruption and destruction, including never-shown surfaces.
- Cancel obsolete media metadata probes and audio-manifest hashing, and guard asynchronous results and deferred callbacks with their owning clip's cancellation token.
- Serialize fallback frame decoding, wait briefly for native playback before requesting a fallback first frame, and delay optional proxy preparation until navigation settles.
- Persist changed metadata/draft entries transactionally in SQLite instead of rewriting the whole JSON library. Existing `~/.fthr/clip_metadata.json` is imported on first save and left intact as a legacy backup; subsequent edits live in `~/.fthr/clip_metadata.sqlite3`. This is a storage migration, so older application versions will not see later SQLite edits through the old JSON file. Transactions use synchronous FULL and rollback on failure.

## Measured switching work

Same isolated offscreen widget benchmark, 12 switches per case, without media decoding/probing/audio or native GPU composition:

| Case | Before median | After median |
|---|---:|---:|
| 1080p, animated | 84.4 ms | 15.3 ms |
| 4K, animated | 103.8 ms | 30.5 ms |
| 1080p, no transition capture | 68.8 ms | 2.4 ms |

These are reductions in synchronous widget switching work, not whole-application CPU percentages or first-frame latency. Full-resolution capture still costs time and temporary memory. Results: `Output/ui-audit-implementation-benchmark.json`; historical baseline: `Output/audit-editor-switch-cost-timed.json`. Snapshot hashes across dates cannot establish pixel equality because the UI includes date text.

## Validation

- Native Windows tests cover GPU slide positions, restore transparency, interrupted/reused surfaces, destruction cleanup, and rapid switching between actual generated videos while retaining GPU playback and effects. Eight native tests passed (`Output/ui-audit-native-final.xml`).
- Regression tests cover retained widget identity, reset clip state, stale callback suppression, uncommitted metadata, decoder fallback avoidance, cancellation, editor sessions, persistence migration and transactional rollback.
- Changed Python source and tests pass Ruff.
- Final broad run: 1,390 passed, nine skipped, five failed (`Output/ui-audit-final-tests.xml`). Failures are the undocumented `kofi_symbol.png` asset, three order-dependent hover tests, and a capture-config test rejected for stale monitor identity. The monitor failure also reproduces using the saved pre-change source; its capture-config method is unchanged. The hover tests pass in the focused run. A teardown test's minimal stand-in needed the new metadata-flush method; that fixture was updated and its 50-cycle lifecycle test passes. The focused lifecycle/editor/hover run passed all 25 tests.
- Native normal-window restore geometry also fails on the saved pre-change source on this machine; the maximized case passes. The offscreen session suite passes. This platform behavior remains unresolved.

The follow-up UI-only memory proposals are in `ui-memory-audit-2026-10-12.md`. They were subsequently implemented; see `ui-memory-implementation-2026-10-12.md`, including the new user setting for disabling timeline previews. No claim of quantified end-to-end RAM savings is made: measure real workloads, process private bytes and GPU memory before making that claim.
