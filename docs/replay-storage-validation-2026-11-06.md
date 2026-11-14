# Replay storage validation — 2026-11-06

## Result

Implemented selectable Memory / Storage replay for video and audio, bounded
packet streaming during saves, temporary-file cleanup, RAM/disk admission,
folder checks, and an HDD warning under Clip → Library & capture.

The tested Windows development engine is installed at
`FTHRcapture/x64/Release/FTHRclips.exe`. Its SHA-256 is
`94F665ED36430BD58B4693C0595342A702E4035AEC202ECF8146A49E18D9042C`.
The preceding executable was renamed to
`FTHRclips.pre-replay-storage-20261106.exe` in that directory, preserving the
already-running process. Restart the development app to load the new UI and
engine together. No installer was packaged.

## Passed checks

- Windows Release build and 1,897 native replay assertions: memory/disk packet
  identity, audio samples and silence, timestamps, retained snapshots,
  wrap/reset, disk accounting, in-flight queue limits, write failures, and
  Unicode arguments.
- Final live Windows QSV qualification in both memory and disk mode. Each run
  saved and fully decoded three 2.0-second clips through initial capture,
  resize, and restore. Minimize and closed-window checks passed. Temporary
  replay files were absent after shutdown.
- Linux Debug build under Ubuntu 24.04 / WSL, with portal capture disabled
  because its optional development headers were unavailable. Sixteen CTest
  tests passed, including real MP4/AAC saves in both storage modes.
- Focused Python settings, snapshot, profile, restart, search, and capture
  regressions passed. A broader selected integration run passed 182 tests;
  the final replay-settings/Linux-policy/smoke-runner run passed 29 tests.
- Ruff on changed Python files; shared-memory ABI, engine response publication,
  and version consistency checks.
- Qt settings renders at 1000×780 and 680×650, including memory, HDD/low-space,
  long folder paths, and invalid-folder states. The actual local drive probe
  reported SSD. HDD and failure-state UI checks used simulated results.

## Existing or environmental limitations

The entire repository test suite is not green. The broad Python run exposed
missing workflow files/assets and an optional uploader bundle, child processes
without PySide6, tests requiring an E: temporary directory, and existing UI
assertions. The six unrelated UI failures and seven camera fixture errors were
reproduced against the pre-change source snapshot. The exception-handler gate
reports 102 findings before and after this change, above its baseline of 92.
Two obsolete Linux source-contract assertions were updated and their seven-test
suite passed afterward.

The full Windows native executable stops at the process-loopback activity
hysteresis assertion after the replay tests; the same failure was reproduced
from the pre-change snapshot. Linux skipped the VAAPI hardware test and failed
the existing timestamp test because the distro FFmpeg lacks `libopenh264`.

The first NVIDIA window-capture attempt was rejected by the existing
cross-adapter FIT policy because this display belongs to Intel. The completed
live checks used the matching Intel QSV encoder. These runs are video-only;
audio storage/muxing was exercised by native tests. Sustained HDD throughput,
physical Linux capture, and game-performance measurements were not performed.

## Local evidence

- `%TEMP%/fthr-replay-final-disk-20261106/result.json`
- `%TEMP%/fthr-replay-final-memory-20261106/result.json`
- `%TEMP%/fthr-replay-win-tests.log`
- `%TEMP%/fthr-replay-win-baseline-tests.log`
- `%TEMP%/fthr-replay-linux-tests.log`
- `%TEMP%/fthr-replay-ui-baseline-pytest.log`
- Workspace parent: `replay-storage-pytest.log`

See [replay-storage.md](replay-storage.md) for the user-facing behavior and
native argument contract.
