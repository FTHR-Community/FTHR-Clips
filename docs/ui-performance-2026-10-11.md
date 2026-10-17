# UI performance changes — 11 October 2026

The editor now avoids repeated timeline work, reduces minimize snapshot memory,
reuses media inspection results, and releases media workers without waiting on
the GUI thread during ordinary editor closure. Edits, undo history, audio mixing,
GPU effects, exports and transition animations remain available.

1. **Timeline rendering.** Static filmstrip, segment, trim and overview artwork
   is cached separately from the playhead. Playhead changes invalidate only the
   old and new marker area. Segment mappings use cumulative lengths and binary
   searches. Cache keys check actual segment values so direct edits, undo, trim,
   selection, zoom, resize, thumbnail changes, theme changes and DPI changes
   cannot reuse obsolete artwork.
2. **Minimize capture.** The editor is rendered directly into a snapshot bounded
   to 560×350 logical pixels. The compact transition avoids allocating a full
   desktop snapshot before scaling it. A native Windows hardware test verifies
   that Qt Quick video remains visible in this capture. Restore still uses a
   full-resolution snapshot to preserve its expanding animation; converting
   that animation to a separate GPU renderer is outside these changes.
3. **Clip replacement.** The outgoing edit and transition image are captured,
   then the old player is torn down before the new editor is constructed.
   Retained native video frames are cleared. If construction fails, newly
   created pages are cleaned up and the previous edit, position and undo/redo
   history are restored.
4. **Inspection reuse.** Successful audio discovery is cached in a bounded
   128-entry LRU using both media and sidecar identities. Source identity includes
   path, size, modification/creation timestamps and inode. Missing, changed or
   newly created sidecars invalidate reuse. Cancelled results and failed probes
   do not seed the cache. Audio probing uses the cancellable owned subprocess
   runner. Successful proxy decisions that require no proxy are cached separately.
5. **Optional media work.** Proxy encoding and filmstrip generation share a
   two-process budget, with one lane for each. A long proxy cannot block the
   timeline lane. Interactive frame requests and probes bypass this budget.
   Optional children run below normal priority on Windows. Filmstrip frames are
   read incrementally; completed samples survive cancellation and a later job
   seeks past that completed prefix. Only completed strips are presented as a
   finished timeline. Complete and partial strips share the existing eight-clip
   memory cache limit.
6. **Worker lifetime.** Audio shutdown immediately stops output and wakes the
   decoder. The detached controller remains owned until native decoding closes,
   so late callbacks cannot target a destroyed QObject. Export cancellation from
   the editor requests cleanup on its worker. Closing during export hides the
   window and finishes destruction after cleanup, without joining on the GUI
   thread. File mutations check retired audio workers and active exports before
   changing files. Explicit shutdown/mutation paths retain bounded waits.

The reproducible timeline benchmark is:

```powershell
python tools/benchmark_ui_timeline.py --output Output/ui-performance-timeline-after.json
```

It renders a 1400×116 offscreen timeline with every third segment deleted and
compares the median of seven repeated renders. With deleted segments hidden:

| Segments | Original repeated render | Updated repeated render | Updated fresh render |
| --- | ---: | ---: | ---: |
| 100 | 4.792 ms | 0.104 ms | 1.854 ms |
| 300 | 29.666 ms | 0.105 ms | 2.905 ms |
| 1,000 | 296.699 ms | 0.147 ms | 7.950 ms |

These measure timeline drawing, not whole-application CPU or RAM consumption.
Saved measurements are in `Output/ui-performance-timeline-before.json` and
`Output/ui-performance-timeline-after.json`. Four baseline/new renderer
comparisons across zoom, width and deleted-segment display were pixel-identical.

Validation completed:

- 159 focused performance, lifecycle, audio, export, library and timeline tests
  passed after the implementation.
- A further 72 focused tests passed, including file operations and failed page
  construction with a partially created editor.
- Four Qt Quick tests passed with `QT_QPA_PLATFORM=windows`, including hardware
  shader output and compact GPU video capture.
- Ruff checks passed for the changed source and test files.
- The full suite recorded 1,382 passed, five skipped and four failures. The
  asset-manifest failure for `kofi_symbol.png` and three hover tests reproduced
  with the saved original source. The hover tests pass in isolation. The
  temporary baseline checkout also lacked bundled native tools, causing an
  additional FFmpeg-discovery failure and some integration skips there.

Regression output is saved in `Output/ui-performance-regressions.xml`,
`Output/ui-performance-focused.xml` and `Output/ui-performance-baseline.xml`.
