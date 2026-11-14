# Capture overlap merging

The clip grid shows a small link button on clips with verified overlapping capture intervals. Clicking it or choosing **Merge overlapping clips…** from the context menu starts a merge. When several partners are available, a menu lets the user choose one. The result is saved under the library's `Merged` folder and opened in the full clip editor, regardless of the basic-viewer preference.

The original files remain unchanged. Exports shows progress and cancellation. Failed or cancelled work never opens the editor or replaces an existing file. If editor slots fill while the job runs, the result remains saved and the app explains how to open it.

## Provenance

New Windows and Linux encoded replay saves include the MP4 format tag `fthr_capture_timeline`:

```json
{"version":1,"generation_id":"35b611f2-a164-4978-93e6-835968bfa884","start_tick":100,"end_tick":400,"time_base_num":1,"time_base_den":60}
```

The interval is half-open. Its start maps to playback zero. The identifier belongs to the encoded ring and changes on reset/recovery; a queued save retains its own snapshot identity. Ticks use the original presentation clock, which may compact capture pauses. They are not UTC timestamps.

Discovery requires a valid tag, matching generation/video format, a zero video playback origin, and a duration consistent with the interval. It runs outside the GUI thread with cancellation and identity-based caches. A sweep bounds suggestions for very dense groups instead of comparing every clip to every other clip.

The merger orders intervals and keeps their union. It re-encodes the retained slices to cut at presentation boundaries, preserving independent audio sources and padding missing/late tracks with silence. Native audio processing descriptions are retained, but old clip-local sample bounds are removed. HDR merges require a compatible HEVC encoder. Source file identities are checked before work and before publication, and library mutation claims protect all inputs. The output is staged and published with a no-replace rename.

Ordinary edited exports remove this capture tag because their trim/speed/segment mapping may no longer describe the original interval. Original-file exports retain it. Native clip finalization preserves the MP4 format tag.

## Coverage and limits

- Applies to new encoded replay clips made with the updated capture engine, including software-encoded replays.
- Old clips with only filename timestamps are not guessed to overlap.
- Raw-frame fallback saves and continuous manual recordings currently omit provenance. Their save paths do not yet provide the same verified snapshot-to-playback interval, so they do not receive overlap badges.
- Different capture generations do not merge automatically even if their wall-clock times appear related.
- The result retains video/audio, not the source editor projects or their unsaved edits.

## Existing multi-selection

The library supports Ctrl-click, Shift-click, checkbox selection, and **Select all results**. Selected cards have a full-card outline. Use **Actions → Add to album**, or drag the selection to an album. **Done selecting** leaves selection mode and clears the checkboxes; Escape remains available. Albums are virtual folders; these operations do not move the underlying files. There is no bulk **Move to filesystem folder** action in the current grid toolbar.

Search and Reset share the upper toolbar with media filtering and sorting. The media filter menu contains **Games**, then **Tags**; tag choices can be combined and cleared. The separate Tags button and Custom/Saved search-preset controls are no longer shown. Stored search presets remain in the library database.

## Verification

- Windows Release x64 solution build; 198 native capture-timeline checks and 51 monitor resolver checks.
- Linux engine build and five targeted native tests, including real MP4 metadata read-back from direct, memory, and disk replay saves.
- Python tests cover invalid/stale/legacy metadata, rational intervals, contained/gapped/cross-generation clips, bounded discovery, cancellation, replacement prevention, mutation claims, and metadata removal from edited exports.
- Real FFmpeg fixtures verify overlap removal, scene order, six-second union duration from overlapping four-second inputs, two audio tracks, late microphone alignment, video-only merges, and unchanged original hashes.
- An offscreen Qt integration test drives discovery, the badge action, production software encoding, and the full-editor handoff. The new card was rendered and visually inspected using synthetic media.

Run `python -m pytest tests/test_clip_overlap.py tests/test_clip_overlap_ui.py tests/test_export_queue.py`. Native tests accept `--capture-timeline-only` and `--monitor-resolver-only`. Build/test evidence is in the parent workspace under `capture-timeline-*`; the isolated Windows build does not replace the currently running app or rebuild its installer.
