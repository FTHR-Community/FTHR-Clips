# Editor CPU and memory audit — 11 October 2026

The strongest remaining opportunities are repeated stylesheet application, reconstruction of the editor on every clip switch, full-window transition buffers, and media work that continues after its clip is abandoned. Application source was not modified. The experiments alter behavior only inside a standalone benchmark process.

## Scope and measurements

Reviewed the current Python/Qt editor, keyboard dispatch, minimize/restore lifecycle, transition renderer, first-frame and paused-seek decoding, metadata/audio discovery, proxy generation, and draft persistence. Existing timeline caching, GPU effects, bounded software-preview admission, and asynchronous teardown were taken into account; they are already implemented, rather than new recommendations.

The benchmark uses the actual editor widgets with cached metadata and disables media opening, probes, audio discovery, autoplay, and filmstrip preparation. Qt runs offscreen. Twelve replacements are measured per case, with deferred deletions processed between replacements. These are synchronous GUI-thread timings, not end-to-end first-frame latency, whole-app CPU percentages, or a native Windows/GPU benchmark. The transition is finished immediately after each measured replacement, so its 220 ms animation playback is excluded. The initial construction warms each case; cases run sequentially in one process.

| Case | Median synchronous replacement |
| --- | ---: |
| 1920×1080, no transition snapshots | 68.8 ms |
| 1920×1080, current switching path | 84.4 ms |
| 3840×2160, current switching path | 103.8 ms |
| 1920×1080, skip unchanged host stylesheet | 55.2 ms |
| 1920×1080, also cache bundled icon paths | 52.3 ms |

The two experimental changes reduced the measured 1080p operation by approximately 38%. Baseline and both experimental variants produced identical final snapshot hashes in this fixture. This is evidence for a useful optimization, not a promise of a 38% reduction in overall application CPU. Theme changes, custom icons, different DPI settings and native video still need validation before shipping.

An earlier cProfile run attributed approximately 503 ms across twelve replacements to stylesheet calls. Construction alone cost approximately 32 ms per replacement. The fixture contains 125 QWidget descendants. Snapshot calls took approximately 9 ms each at 1080p and 18 ms each at 4K in the final timing run.

The current path retained one live editor page after deferred deletion in each case. At 4K, sampled working set went from 144.8 MiB before the loop to 153.5 MiB after twelve switches and 111.1 MiB after closing and explicit garbage collection. These samples do not measure transient peaks or demonstrate the absence of a native decoder/GPU leak. Private committed memory and resident working set are recorded separately in the JSON.

## Recommended changes, in priority order

### 1. Skip redundant window stylesheet application

**Measured; relatively small change.** `ClipEditorWindow.replace_clip` reapplies the host stylesheet at `FTHR_UI/ui/clip_editor_window.py:109`, after the new page has already applied its own stylesheet. The outgoing page is still parented at this point. Reapplying an identical sheet triggers avoidable style processing through the widget hierarchy.

Keep the theme factory current, but call `setStyleSheet` only when the resulting stylesheet changes. Longer term, give the stable host ownership of the common editor stylesheet. Retain theme refresh and inherited styling for newly attached controls. The isolated comparison reduced the operation from 84.4 to 55.2 ms without changing the fixture's rendered pixels.

### 2. Reuse the editor controls and GPU presentation surface

**Confirmed repeated work; larger architectural change.** `FTHR_UI/ui/clip_editor_window.py:70` constructs a complete `ClipViewer` on every navigation. Its constructor calls `_setup_ui` and `_apply_styles` at `FTHR_UI/ui/clip_viewer.py:4487`. This recreates controls, signals, timers, QMediaPlayer, audio output and, on the supported native path, a QQuickWidget/QML scene.

Separate the stable UI from per-clip state and resources. Rebind labels, controls, timeline data and player source while retaining the widget tree and renderer. Keep generation checks, file-handle release, export guards, rollback on failed open, and undo/draft ownership. Retaining the renderer does not require retaining the old decoder.

Minimize currently closes the editor at `FTHR_UI/main.py:4298`; restore constructs it again at line 9285. A bounded reusable shell can reduce restore churn while keeping dormant sessions data-only. Do not retain a full live decoder for every minimized session as a shortcut: that trades CPU spikes for greater RAM use.

### 3. Reduce transition copying while preserving the animations

**Confirmed allocation/rendering cost; native improvement needs profiling.** Navigation takes outgoing and incoming window snapshots at `FTHR_UI/ui/clip_editor_window.py:57` and line 137. Restore takes another full-window snapshot at `FTHR_UI/main.py:9327`. `SnapshotSlide` repaints two images; expanding `SnapshotFlight` uses a large translucent native surface (`FTHR_UI/ui/shell_transitions.py:27`).

Two physical 3840×2160, 32-bit snapshots contain about 63.3 MiB of pixels before backing stores, video surfaces and compositor buffers. Physical pixel dimensions, including device pixel ratio, determine this cost. During an interrupted slide, the previous snapshots can overlap the next capture until cleanup runs.

Explore GPU texture composition/reuse for full-size slides and expanding restore, and explicitly release superseded buffers as soon as their final composite is captured. Preserve animation, resolution and timing; simply shrinking these snapshots is not the quality-preserving recommendation. Compact minimize capture is already implemented and should remain.

### 4. Cancel abandoned hashing and metadata probes promptly

**Confirmed cancellation gaps; runtime impact depends on files and storage.** On an audio-discovery cache miss, `FTHR_UI/core/ffmpeg_playback.py:428` validates the sidecar. `media_sha256` at `FTHR_UI/core/audio_manifest.py:53` reads the entire video in 1 MiB blocks without checking cancellation. Cancelling the editor suppresses delivery but cannot stop this ongoing read/hash. Several quickly abandoned large clips can therefore keep consuming CPU and disk bandwidth.

Thread the cancellation token through playback-side manifest validation and check it between blocks and before starting validation. Preserve full integrity verification for completed results and exports; never cache partial or cancelled hashes. Share concurrent validation of the same media/sidecar identity where practical, while preserving cancellation ownership for other consumers.

Cold metadata has a similar gap: `_ClipMetadataWorker` calls `probe_video_metadata` at `FTHR_UI/ui/clip_viewer.py:741`; the implementation at `FTHR_UI/core/media_metadata.py:159` uses `subprocess.run` with a four-second timeout, without the editor token. Use the existing owned cancellable subprocess runner. Successful metadata is already cached, so this matters most for cold or slow files.

### 5. Avoid unnecessary first-frame decoders and bound fallback work across switches

**Confirmed opportunity; measure with actual codecs.** `_on_media_status_changed` calls `_request_initial_preview_frame` before updating playback readiness. When no native frame has arrived, this can start `_InitialFrameDecodeWorker` even with autoplay queued. That worker opens a separate OpenCV decoder, reads a full-size frame, converts to RGB and copies it. Paused-seek fallback also creates a fresh decoder per admitted request (`FTHR_UI/ui/clip_viewer.py:644`, line 681 and line 5774).

Give pending native/autoplay frame delivery a bounded opportunity to complete before starting the fallback. Keep a deadline so paused or failing backends still display a real frame. Scope fallback admission across retired and current pages, use latest-request semantics, and add cancellation checkpoints after opening/seeking/reading and before conversion. An already blocking OpenCV call cannot be interrupted merely by discarding its result; if required, move that fallback behind a cancellable process boundary. Preserve exact paused-seek targets, full image quality and the existing fallback functionality.

### 6. Schedule optional proxy preparation after navigation settles

**Confirmed startup scheduling; benefit is workload-dependent.** Every newly constructed editor schedules `_prepare_playback_source` immediately (`FTHR_UI/ui/clip_viewer.py:4569`, line 5668). On a cold decision, `FTHR_UI/core/playback_proxy.py:102` launches a GOP probe and may start a full proxy encode. Encoding already has concurrency and priority limits, but it is still admitted while the user is browsing between clips.

Delay optional proxy work until the destination has remained selected briefly, promote it when seeking needs it, and deduplicate in-flight work for the same identity. Continue immediate playback of the original and retain eventual proxy preparation. Do not debounce away deliberate navigation steps or change autoplay behavior. Keyboard auto-repeat is already suppressed for these actions; increasing repeat filtering is not the primary fix.

### 7. Cache bundled icon path resolution outside paint

**Measured small improvement.** `FTHR_UI/ui/icons.py:29` resolves the module's filesystem path on every request. `icon_pixmap` also checks file existence and modification time before reaching its existing raster cache. Dropdown painting repeatedly traverses this path.

Cache the immutable bundled asset root and name-to-path mapping. Keep custom-icon change detection and invalidate themed pixmaps appropriately. Caching paths alone improved the fixture from 55.2 to 52.3 ms after the stylesheet change. Removing filesystem checks for mutable custom icons without replacement invalidation would break functionality and is not recommended.

### 8. Make edit persistence proportional to the changed draft

**Secondary opportunity for large libraries or slow disks.** Switching/minimizing flushes dirty drafts through `FTHR_UI/ui/clip_viewer.py:6444`. `ClipMetadataManager._save` at `FTHR_UI/core/clip_metadata_manager.py:25` serializes and rewrites the entire metadata dictionary on the calling thread.

Use transactional per-draft persistence, or an ordered writer with a durable journal and an explicit shutdown flush. Keep crash recovery and edit durability; merely delaying the write without durable ownership would weaken current behavior. Existing 450 ms edit debouncing is already useful and should remain.

## Verification and follow-up measurement

The existing `test_editor_sessions.py`, `test_video_playback_performance.py`, and `test_ui_performance_optimizations.py` suites passed together on the unchanged application source. They validate existing behavior, not production versions of the proposals.

Artifacts:

- `Output/audit_editor_switch_cost.py`: reproducible isolated benchmark and process-local prototypes.
- `Output/audit-editor-switch-cost-timed.json`: final timings, memory samples and snapshot equality.
- `Output/audit-editor-switch-cost.json`: initial instrumented baseline.
- `Output/audit-editor-1920-1.txt`: initial cumulative profile.

Reproduce the final timing run in PowerShell:

```powershell
$env:AUDIT_PROFILE='0'
python Output/audit_editor_switch_cost.py
```

Before claiming whole-application savings, profile native Windows playback at representative resolution, frame rate, codec, audio-track count and monitor DPI. Include rapid alternating Tab+Left/Right, fresh clips with manifests, edited clips, repeated minimize/restore, and longer steady playback. Measure UI event latency, first correct frame, process-tree CPU, peak private bytes, resident working set, GPU memory, outstanding media jobs and recovery after idle. Check the running executable corresponds to the audited source. No end-to-end native-video measurements were made in this audit.
