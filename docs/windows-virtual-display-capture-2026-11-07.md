# Virtual display capture startup failure

## Evidence

The supplied 2026-11-07 diagnostics show three capture startup attempts, selecting both the physical ASUS display and a default-monitor device path. All fail with `CCD and DXGI disagree about the live monitor adapter`, before capture or encoding begins. The DXGI-to-WGC fallback fails at the same topology resolution step. NVIDIA hardware encoding detection succeeds.

The machine reports Meta Virtual Monitor, Parsec Virtual Display Adapter, LuminonCore IDDCX Adapter, spacedesk Graphics Adapter, and an RTX 4070 Ti. The diagnostics do not identify which driver supplies the mismatched mapping, and do not contain the conflicting LUID values.

## Existing behavior

The UI enumerates live Windows desktop monitors and passes a persistent monitor device path, or a handle-qualified identity when PnP metadata is unavailable. Virtual displays use this same path; they are not explicitly excluded. Native capture correlates live HMONITOR handles and GDI display names with CCD metadata and DXGI outputs. Auto capture tries Desktop Duplication, then WGC on the selected monitor.

Previously, any CCD/DXGI adapter mismatch aborted the entire topology query. An issue on a virtual display could therefore prevent capture of an otherwise usable physical monitor too. WGC shared this resolver and could not bypass the failure.

## Correction

When DXGI identifies an output by both the exact live HMONITOR and GDI display name, use that output's adapter for capture. Preserve the selected monitor identity and the separate CCD target adapter metadata used for SDR white-level queries. Continue rejecting ambiguous source mappings and ambiguous DXGI adapter matches; do not substitute another display or output zero.

This addresses the observed startup rejection. It does not add a generic rendering-device fallback for virtual drivers that expose no usable DXGI adapter, nor establish that every virtual-display driver supports capture. A retest on the affected machine is still required to verify recording and saving end to end.

Microsoft's [indirect display driver overview](https://learn.microsoft.com/en-us/windows-hardware/drivers/display/indirect-display-driver-model-overview) describes virtual/remote display adapters. The fix relies on the existing exact output correlation, rather than a driver-name allowlist.

## Validation

- Release x64 solution build succeeded in `../monitor-resolver-build/`. Separate output avoids the running app's locked engine and playback DLL. No installer was rebuilt.
- `FTHRclips_tests.exe --monitor-resolver-only`: 51 checks passed, including the live local desktop lookup, conflicting CCD/DXGI mapping, independent physical/virtual selection, exact identity requirements, ambiguous outputs, and CCD target metadata preservation/invalidation.
- `python -m pytest tests/test_windows_monitor.py`: 34 passed.
- The default native suite passed the monitor checks but later failed the audio assertion `first meaningful block is held by activity hysteresis` in `windows_process_loopback_audio_provider_test.cpp:83`. That code was not changed. The complete native suite is therefore not green.

Build and test logs are in the parent workspace: `monitor-resolver-isolated-build.log`, `monitor-resolver-only-tests.log`, and `monitor-resolver-native-tests.log`.
