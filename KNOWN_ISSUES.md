## Current issues

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
- manual recording a length sometimes inaccurately displayed
- Linux ScreenCast portal capture can temporarily appear as "CAPTURE STALLED"
  while the desktop's first-run screen picker is open. The picker controls the
  selected screen; the separate screenshot path still depends on `grim` and is
  not proven by working portal video capture.
