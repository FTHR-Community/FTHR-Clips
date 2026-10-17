# Provider removal and settings responsiveness — 15 October 2026

Removed the retired upload provider's API client, account controls, consent
dialog, donation link, runtime routing, and upload-size preset. Removed its
hardware identity service, IPC client, legal documents, dormant package, and
build/install bindings. Updated the uploader policies, website demo and privacy
copy, README, and build instructions.

Persisted unsupported providers load with uploads disabled and Catbox selected.
Obsolete provider settings are dropped from memory and subsequent settings
saves. The runtime rejects retired provider names and account actions.

Camera enumeration previously ran synchronously while constructing Visuals.
It launches FFmpeg with a 12-second timeout and can then probe eight camera
indices. Discovery now runs on a daemon worker, with a queued Qt signal updating
the device list. The list shows a scanning state, preserves the saved device,
blocks selection signals during population, prevents overlapping scans, and
allows refreshing after failure. Navigation uses a zero-delay queued callback
instead of adding a 60 ms wait to each category change.

Renamed the timeline thumbnail option to **Seek bar preview**, in both Interface
Performance and Clip Editor. Its description reads: “Off: the app will no longer
generate preview images in the timeline.”

## Validation

- 47 upload, packaging, installer-contract, export-profile, and camera-discovery
  regressions passed: `Output/provider-settings-regressions-2026-10-15.xml`.
- 70 settings/navigation, camera failures, performance, consistency, and settings
  persistence regressions passed: `Output/settings-navigation-regressions-2026-10-15.xml`.
- 40 SFTP regressions passed: `Output/sftp-regressions-2026-10-15.xml`.
- The blocked-discovery test verifies first Visuals construction, Qt timer
  delivery, navigation to another category, device selection preservation, and
  single-flight scanning while the camera worker waits.
- Ruff and Python compilation passed for changed production Python files.
- Rebuilt and sealed the uploader as version 1.4.0 with policy version
  `2026-10-15-v3`; the package integrity and local executable activation tests
  passed. Build log: `Output/uploader-build-2026-10-15.log`.

The desktop executable/installer was not rebuilt and the website was not
published. Restart the source app to load UI changes. No native first-load
latency benchmark or physical-camera compatibility claim is made.
