## Current issues

### Next update — trash and restore, 20 October 2026

The working source moves owned clips to recoverable Trash from the library, editor and selected bulk actions. Metadata and saved drafts retain their clip identity. Restore chooses a free name on collision; permanent deletion requires a separate confirmation. Interrupted permanent deletions remain visible for retry until cleanup is complete. Protection and upload/export ownership are checked before mutations; external originals and unsupported media associations are retained.

Trash and restore currently require the same volume. Cross-volume operations, live power-loss recovery and current-build/interactive upgrade acceptance remain open. Startup explicitly requests the backed-up schema-3 migration. No production profile was migrated during source validation. Earlier binaries do not contain these changes.

### Next update — library multi-selection, 20 October 2026

The working source supports Ctrl/Shift selection, all filtered results and bulk favorite/protection changes. Original-file export copies complete selected sources and valid audio-label manifests through the existing export queue; saved editor changes are not applied. Registered additional media that cannot be preserved by this export are reported as unsupported. Busy, unfinished or replaced sources are reported individually. Bulk deletion now uses recoverable Trash with per-file protection and identity checks. Earlier binaries do not contain these source changes; current-build and hardware qualification remain open.

### Next update — favorites and protected clips, 19 October 2026

The working source includes favorite controls, a favorites filter and independent deletion protection. New favorites start protected; explicitly removing protection keeps the favorite. Current library, editor, upload and orphan audio-manifest deletion paths recheck protection. The later trash/restore implementation described above replaces the ordinary permanent-delete workflow for owned clips. Earlier application binaries do not contain these source changes. Hardware qualification and current-build/upgrade acceptance remain open.

Removing a favorite now releases protection added automatically by the current source. Separately selected protection remains active. Older saved protection has no origin marker and is retained conservatively; use “Unprotect clip” explicitly for those clips.

Offscreen integration runs have aborted natively during Qt event processing, including the combined F06 source-suite run. The source suite is qualified in four fixed isolated test processes with the existing hardware/platform skips retained. The native cause remains unconfirmed; single-process Qt stability and current-build acceptance remain open. No native-crash fix is claimed.

### Next update — library foundations, 19 October 2026

The working source adds backed-up metadata migration, stable clip identities and journaled same-folder renames. Recovery coverage simulates process interruptions; power-loss durability, live recovery and interactive upgrade acceptance remain open. Trash/restore, cross-volume moves and the expanded editor are not implemented by this foundation. The previous portable build and earlier installer do not contain these source changes.

### Next update — editor upload, 19 October 2026

The local editor now exports and validates the current edit before handing its output to the existing optional uploader. Export failures do not fall back to uploading the original. The previous packaged PR candidate does not contain this later change. Physical audio/AMD qualification and release-package acceptance remain open.

### vxnsin PR integration — 19 October 2026

PRs #88–#90 are integrated into this local source; PR #87 was already adapted. Multiple named playback-device tracks apply to Windows replay clips; manual continuous recordings retain the existing system mix. Full multi-endpoint hardware-mixer and supported-Windows per-program qualification remain separate from parser/UI/build validation. Window resizing restarts the existing selected source with a fresh replay generation and bounded retries; terminal failures remain visible.

### Audio/video synchronization update — 19 October 2026

The reproduced Windows MP4 Edit List video offset and AAC priming offset are corrected in the tested decoder paths. New Windows MP4 outputs and eligible old-clip playback copies use the timing-neutral workaround. A new generated system-loopback capture and production-finalized copy retain all six matched markers within one 60-Hz frame and preserve the full video tail. Historical entries below describe their original test state; the reproduced container cases are superseded by these corrections and controlled decoder checks.

Physical microphone/speaker/display latency, long gameplay, device changes and other Windows/hardware configurations remain unqualified. This is not a universal repair for offsets already present in recorded source content.

### AMD source review — 18 October 2026

The full Windows Release/x64 build has now been run on the development PC.
Native contract tests and the production GPU-input probe pass on its RTX 4060 Ti.
These results do not qualify Radeon hardware, gameplay, physical microphone
sync or the complete GUI/release matrix. The existing Windows playback-offset
issue remained open at this historical review stage. Physical Radeon follow-up
is still required; the later container corrections above do not qualify Radeon hardware.

### Release qualification status — 17 October 2026

- AMD/NVIDIA core feature parity is a P0 requirement. AMD physical validation
  is **BLOCKED — AMD HARDWARE REQUIRED**; it is not optional or best-effort.
- The local RC01 path has been physically exercised on Windows 10/RTX 4060 Ti
  using WGC_MONITOR/H.264 NVENC at 1920×1080/60 FPS. Moving and paused-reference
  F9 saves succeeded, and two native recordings plus two production-finalized
  copies passed strict FFmpeg full-decode validation (exit 0). This does not
  qualify the complete Windows matrix or public release.
- The moving run's ten uniquely matched markers measured native system-audio
  deltas of -25.00…+25.33 ms and combined deltas of -20.00…+30.33 ms; the
  finalizer difference was +5 ms. Measurement uncertainty is at least one 60 Hz
  frame plus an audio hop (about ±18 ms), and microphone synchronization remains
  **UNVERIFIED**. A new Qt Windows backend probe reports an approximately
  300 ms content-versus-PTS mismatch in the combined file; the historical 1.3 s
  Warzone playback issue remains open, and this run is not proof of a universal
  1–2 s native-offset fix.
- Re-recording Windows Media Player playback of that exact combined file
  produced about 304 ms additional audio lead across seven common markers
  (conservative difference uncertainty ±36 ms). This third F9 save fully
  decodes but demonstrates an open playback-output/second-capture boundary,
  not a correction. No global audio offset was added.
- DXGI qualification, 120/144 FPS, recovery/long-soak coverage, Intel and
  hybrid-GPU configurations, and AMD physical validation remain open. AMD
  validation is **BLOCKED — AMD HARDWARE REQUIRED**; unsigned/Kaspersky exact
  detection is untriaged, and external reports remain **FIELD VALIDATION
  PENDING**. Local candidate bundles were frozen before this documentation update.
- Local Windows artifacts are unsigned and are not publishable under the
  current release gate.
- Linux/AppImage launch, capture, audio and multi-monitor qualification remain
  open; the Linux RPATH baseline is unchanged and this Windows candidate does
  not change it.

this is just a unordered list of things that we need to address as we move on, please note that this list was written with only accounts of our own self-discovered issues and doesn't list community reports:
- slight audio delay is sometimes possible at higher frame rate recordings
- app drops frames of anticheat protected games after tabbing out
- capture card doesn't always display correct amount of seconds of the actual clip but rather follows the selected length even if it wasn't reached
- The manual recording timer shows approximate elapsed time; the saved notification reports measured video duration when available. Physical start/stop timing and long recordings remain part of hardware qualification.
- Linux ScreenCast portal capture can temporarily appear as "CAPTURE STALLED"
  while the desktop's first-run screen picker is open. The picker controls the
  selected screen; the separate screenshot path still depends on `grim` and is
  not proven by working portal video capture.
