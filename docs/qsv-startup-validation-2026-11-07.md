# Intel QSV startup regression — 2026-11-07

## Cause and fix

The installed app's desktop capture failed while deriving the QSV device from
the selected Intel D3D11 device. `%USERPROFILE%/.fthr/logs/fthr.log` reported
`Error setting child device handle: -16`, followed by
`ENCODER_QSV_INIT_FAILED`. The capability cache nevertheless listed QSV H.264
and HEVC because the isolated probe enabled D3D11 multithread protection before
opening the encoder. Production desktop and window capture did not.

The QSV codec session now enables and verifies protection on its shared
immediate context before passing the device to oneVPL. It leaves protection
enabled for the shared device's lifetime. The capability probe no longer
pre-enables protection for QSV, so qualification exercises the same requirement.
Missing and deferred contexts produce specific initialization errors.

Intel's [DirectX interoperability instructions](https://intel.github.io/libvpl/latest/programming_guide/VPL_prg_hw.html#work-with-microsoft-directx-applications)
require multithreading mode for the shared D3D11 device.

## Verification

- A direct experiment using the installed app's `avutil-60.dll` and the real
  Intel adapter reproduced the exact child-handle error with protection
  disabled. Enabling protection changed QSV device derivation from
  `-1313558101` to success. This held both with device flags zero and with
  `BGRA_SUPPORT | VIDEO_SUPPORT`; those flags alone did not resolve the error.
- Release x64 solution build succeeded. The native QSV tests passed 43 checks,
  including eight new checks on real WARP D3D11 contexts for initially disabled
  protection, repeated initialization, missing context, and deferred context.
  The full suite later stopped at the previously documented process-loopback
  activity-hysteresis assertion.
- Native H.264 and HEVC QSV probes each submitted and fully decoded 48 frames
  without pre-enabling context protection in the probe.
- Production desktop capture, explicit Intel H.264, disk replay: 311 frames
  captured, 1920×1080, 2.0-second clip saved and fully decoded with FFmpeg.
- Production desktop capture, explicit Intel HEVC, memory replay: 313 frames
  captured, 1920×1080, 2.0-second clip saved and fully decoded with FFmpeg.
- Both desktop runs exited gracefully with code zero and left no replay
  temporary files. A fullscreen animated test surface covered the desktop
  during capture. Audio was disabled in these focused video checks.

The tested development executable is `FTHRcapture/x64/Release/FTHRclips.exe`,
SHA-256 `7b130a13c93b2f7efd0aa63705084e831c0ce4e12615b89e9606ff2834de4941`.

## Evidence

- Workspace parent: `encoding-fixes-native-tests.log`
- `%TEMP%/fthr-qsv-desktop-fixed-20261107-083240/result.json`
- `%TEMP%/fthr-qsv-hevc-memory-20261107-083306/result.json`

The desktop evidence directories contain the production engine logs,
verification script, and saved MP4s.
