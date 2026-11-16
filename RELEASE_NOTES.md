# FTHR Clips 2.1.2

## 16 November 2026 — FFmpeg maintenance and export diagnostics

- Update the Windows LGPL shared FFmpeg runtime, development headers, and import libraries to 8.1.3-35, including upstream decoder security fixes.
- Preserve validation and finalization failure reasons in export errors and diagnostics instead of replacing them with normal FFmpeg output.

## 15 November 2026 — capture recovery and AMD scaling

- Correct GPU texture preparation for AMD video scaling while preserving the native-resolution fast path.
- Recover from transient monitor geometry changes and serialize bounded retries after persistent display/device changes; prevent heartbeat polling from bypassing retry limits.
- Protect active saves and manual recordings, and cancel queued recovery when capture intent changes.
- In automatic RAM mode, reduce replay duration to fit available memory without changing recording quality or saved preferences. Show the effective duration and keep audio/video presentation aligned.
- Include validated, bounded previous-session engine logs in diagnostic exports and report the confirmed replay duration after each restart.
- Native regression checks and Intel/NVIDIA window-capture fixtures pass. Radeon 780M encoding and protected Fortnite mode transitions still require qualification on that hardware.

## 11 November 2026 — opt-in update checks

- Disable automatic update checks by default, including older preferences without privacy consent.
- Explain internet access and request metadata before enabling background checks.
- Keep update downloads and installer launch under user control.

## 7 November 2026 — settings and capture interface

- Organize settings into consistent folded sections, including game profiles and allowed games inside Clip.
- Move Save clips with gaps and Save low-FPS clips into Video encoding.
- Refine dropdown menus, top-bar panel attachment, scroll areas, and compact numeric steppers.
- Show app icons in Allowed Games and use a switch for its capture restriction.
- Keep one animated capture card and remove the extra saved-duration notification.

## Next update — working source, 19 October 2026

- Mark favorites from library cards or the editor and filter the library to favorites. New favorites are protected by default; favoriting and deletion protection remain independent, with explicit unprotection available.
- Release automatically added favorite protection when the favorite is removed, restoring the active red delete button. Preserve separately chosen protection and older protection whose origin is unknown. Place the card's favorite button in the upper-right corner of its thumbnail.
- Check current protection and file identity before library/editor deletion, post-upload auto-delete and orphan audio-manifest cleanup. Keep protected files and retain local files when the deletion context cannot be verified.
- Preserve editor drafts when deletion cannot proceed. Refuse teardown after a failed save and restore the existing editor session if protection changes before the file can be removed.
- Give clips stable IDs in the existing SQLite metadata store. Back up databases and legacy metadata before an explicit, versioned migration; retain tags, editor drafts and unknown metadata fields.
- Journal same-folder renames and associated audio-manifest updates so interrupted operations can be recovered before library scanning and background services start. Reserve source and destination paths against uploads, exports and active recordings.
- Save active and parked editor drafts before renaming their media; keep edits and the original file available when saving or closing fails.
- Define a versioned editor-project contract with multiple media references and a lossless adapter for existing drafts. The expanded editor and remaining library features remain planned work.
- Keep ordinary metadata reads on a validated in-memory schema snapshot while retaining file-identity checks and fresh database validation for writes.
- Restore library scroll updates after initial scanning and attach them only once when the grid enters its scroll area.
- Explain the system-mix fallback for an empty playback-device selection; distinguish missing devices from an unchecked or failed scan and allow retrying in multi-device mode.
- Mark the manual recording timer as approximate and report measured video duration after the closed file is validated. Missing timing information does not prevent saving a valid recording.
- Keep clip metadata in memory unchanged when its database transaction fails, including editor-draft pruning; reject metadata rename collisions instead of overwriting the destination.
- Bind new editor drafts to the source file's device/file identity as well as size and modification time, while retaining existing draft compatibility.
- Upload the current editor result after a separate validated export, including cuts, crop, effects, speed and audio mix. Preserve the original file and refuse uploads when export or media inspection fails.
- Keep upload exports running across editor navigation/close, prevent repeated export jobs for the same source and reuse unchanged completed output through the existing uploader's duplicate checks.
- Hardware qualification and release-package acceptance remain open. This later source change is not included in the previously built installer.

## 19 October 2026 — vxnsin pull request integration

- PR #87 is already covered by the adapted AMF single-texture pool; retain the current AMD stabilization.
- Integrate PR #88: real CANCEL/CONTINUE installer navigation while applications use files, plus distinct finished/pending step artwork.
- Integrate PR #89: Windows replay clips can record one playback device, per-program tracks or up to four named playback-device tracks. Preserve legacy settings and route the active generation through finalization without merging the new separate tracks away. Manual continuous recordings retain the existing system-mix path.
- Integrate PR #90: window size changes request a restart of the same selected HWND. Keep existing identity, generation and retry-budget guards; show terminal errors when recovery is unavailable.
- The earlier audio-sync fixes remain included. Parser/UI/build checks do not replace multi-endpoint or supported-Windows per-program hardware qualification.

## 19 October 2026 — Windows audio/video synchronization

- Correct the reproduced Windows MP4 video-edit and AAC-priming interpretation with a lossless, timing-neutral container workaround. Apply it before new Windows MP4 saves/exports are published and to eligible older clips through an asynchronous playback copy. Original library files remain intact.
- Preserve the complete video tail when the combined AAC track ends earlier, without changing relative audio start times.
- Use the shared overflow-safe QPC conversion in the optional process-loopback fallback.
- The local Warzone decoder comparison and generated WGC/system-loopback marker test pass. Physical microphone/speaker latency, long gameplay and Radeon qualification remain pending.

## 18 October 2026 — AMD source stabilization and verification

- Keep AMF production input on the bounded pool of separate BGRA textures;
  matching geometry uses a direct GPU copy. GPU NV12 remains an explicit
  diagnostic comparison, with separate production and comparison results.
- Bound compressed replay video and live save snapshots by a shared byte
  budget. Reject settings or saves that cannot fit instead of silently
  shortening configured history.
- Bind window capture to its HWND/process/thread identity and replace
  blocking diagnostic readbacks with bounded asynchronous sampling.
- Qualify Windows encoders through the actual native GPU path and decode all
  48 changing testframes. Cache identity includes engine/runtime content and
  current monitor, adapter, driver and encoding parameters.
- Correct the WinAPI primary-monitor flag used for first-run monitor selection.
- Add a repeatable video-only WGC window lifecycle qualification helper and
  compile it in Windows CI alongside the AMD helper.
- The full local MSVC Release/x64 build and NVIDIA GPU-input regressions now
  pass in the historical development environment. Physical Radeon qualification
  remains pending; these results do not qualify the complete release matrix.

## 17 October 2026 — partial replay saves

- Replay saves during startup or after capture restarts now save the available
  decodable history once at least 0.5 seconds is buffered, even when the selected
  clip length is longer. Timeline, freshness, cadence and generation checks
  remain enforced. The output duration reflects the actual available footage.
- Added a Desktop/Start menu shortcut helper for the latest local packaged build:
  `tools/update_latest_build_shortcut.ps1`.

## 17 October 2026 — qualification status

- Local RC01 qualification on Windows 10 with an RTX 4060 Ti covered
  WGC_MONITOR/H.264 NVENC at 1920×1080/60 FPS. Moving and paused-reference F9
  saves succeeded; both native recordings and the production-finalizer outputs
  created from byte-identical input copies passed strict FFmpeg full-decode validation
  (exit 0). This is local evidence, not public-release approval.
- In the moving run, ten uniquely matched markers measured native system-audio
  deltas from -25.00 to +25.33 ms and combined-audio deltas from -20.00 to
  +30.33 ms; the finalizer changed the measured value by +5 ms. Physical
  uncertainty remains at least one 60 Hz frame plus an audio hop (about ±18
  ms), and microphone synchronization is **UNVERIFIED**.
- A new Qt Windows backend probe reports an approximately 300 ms
  content-versus-PTS mismatch in the combined physical file. A third F9 save
  re-recorded Windows Media Player playback of that exact file: seven common
  markers show about 304 ms additional audio lead (conservative ±36 ms
  difference uncertainty), and the new recording fully decodes. This narrows
  the playback-output/second-capture boundary; it is not a production fix. The historical
  1.3 s Warzone playback issue therefore remains open; this single run did not
  prove a universal 1–2 s native offset or a complete playback fix. DXGI,
  120/144 FPS, recovery/soak, AMD physical validation, and Kaspersky/unsigned
  artifact qualification remain open; external reports remain **FIELD
  VALIDATION PENDING**. Local candidate bundles were frozen before this documentation
  update; no sealed bundle was modified.
- Stable game-folder grouping applies to future saves only; it does not rewrite
  existing library history.
- AMD/NVIDIA core parity remains a **P0 release requirement**. AMD validation is
  **BLOCKED — AMD HARDWARE REQUIRED**, not optional or best-effort. The local
  NVIDIA path above is qualified only for its stated configuration; the full
  Windows matrix, Intel and hybrid-GPU paths remain unverified. External
  reports remain **FIELD VALIDATION PENDING**.
- Local Windows artifacts are unsigned and are not publishable under the
  current release gate. The Linux/AppImage qualification baseline is unchanged
  and remains out of scope for this Windows candidate.

## 16 October 2026

- Add hardware-accelerated clip exports with Very fast, High quality and Lossless choices for export and sharing.
- Keep exports running while browsing clips, with optional simultaneous exports and a progress window.
- Fix the Discord share window opening behind the editor.
- Match export window controls to the app and correct the editor's maximize button.
- Simplify the interface performance descriptions.

## 13 October 2026

- Add interface performance presets and individual controls with estimated impact labels, including a lightweight clip viewer.
- Support trusted SSH known_hosts entries for SFTP when the fingerprint field is blank, and remember keys after successful fingerprint verification.
- Rebuild the Upload Extension as version 1.3.1 with the updated SFTP verification.

This alpha release includes the current editor performance and SFTP updates.

## What is in here

- Replay capture and transactional clip saving
- Windows WGC/DXGI capture with H.264, HEVC and AV1 paths
- Manual recording that keeps the active stream and writes a stable MP4 output
- System audio and microphone tracks with in-app playback
- Monitor screenshots, crop editing and background operation
- Persistent autostart and settings
- Overlay previews, webcam and click burn-ins
- Installer and Linux AppImage build definitions with release checks in place

## Current status

This is a release-candidate qualification build for controlled testing only;
it is not approved for public distribution.

- NVIDIA H.264/HEVC/AV1 stall fixes are integrated and covered by automated
  lifecycle tests. A complete physical Windows qualification after those fixes
  has **not** run yet.
- HEVC editor playback and audible microphone content also require a new
  physical run; earlier failures are not evidence that the current source is
  fixed or still broken.
- AMD and Intel paths are automated-tested but hardware-unverified.
- Local Windows artifacts remain unsigned.
- The Linux engine builds and all native CTests pass in WSL2. The AppImage also
  passes construction, licence, and extraction checks there, but native
  Wayland/X11 capture, audio, hotkeys, and multi-monitor behaviour remain
  physically unverified.
