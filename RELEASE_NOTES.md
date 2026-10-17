# FTHR Clips 2.0.0-alpha

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
