# Capture startup and optional audio arguments

The failing session was the source Python UI. Its log selected
`FTHRcapture/x64/Release/FTHRclips.exe`, an older native build, then reported
`FTHR_STARTUP_ERROR: Invalid replay storage option --audio-exclusions` on every
capture attempt. Audio was in Programs mode with no excluded apps and the
default system backup enabled.

The UI had emitted both new optional audio-policy arguments even when they
requested the native defaults. That older engine's strict replay-option parser
rejected the unknown argument before initializing capture. Source discovery
also omitted the isolated current native builds.

`native_audio_policy_arguments` now emits exclusion arguments only for a
nonempty, active Programs exclusion list, and the backup argument only when
the active Programs backup is disabled. Device and audio-off capture emit
neither inactive policy. Non-default policies still require the existing
fail-closed native capability qualification before process launch.

Source engine discovery now includes `build/*/engine/FTHRclips.exe` candidates
in the existing newest-build selection. It does not search packaged/test
binaries. Frozen applications continue using their bundled engine only.

Audio settings also move routine explanations to control tooltips. Actual
states remain visible: audio off, missing/unconfirmed devices, backup disabled
by exclusions, microphone gain warnings, and failed settings saves. Programs
backup controls and the exclusions picker remain in Audio settings.

## Actual startup verification

A bounded CLI smoke used the user's selected monitor, NVENC H.264, 60 fps,
62-second buffer, 27,500 kbps, Programs audio, microphone endpoint/gain, and
playback endpoint. Only the replay spool location was redirected to a new
temporary evidence directory. No clips or manual recordings were saved.

| Native executable | Audio policy | Frames over three seconds | Result |
| --- | --- | --- | --- |
| `build/system-audio-backup/engine/FTHRclips.exe` | Default backup, no optional flags | 1 → 167 | Initialized, exit 0 |
| `FTHRcapture/x64/Release/FTHRclips.exe` | Default backup, no optional flags | 1 → 153 | Initialized, exit 0 |
| `build/system-audio-backup/engine/FTHRclips.exe` | Backup disabled | 2 → 161 | Initialized, exit 0; no system loopback initialized |

Every run reached the real capture-engine RUNNING banner, progressed native
frames, and exited gracefully. No run reported an invalid replay-storage
option. Each smoke checked that no pre-existing native engine was running,
owned the exact spawned native process, and stopped only that owned process.
The existing Python UI was left alone. The settings file's SHA-256 was
unchanged before and after the smoke.

Evidence is in
`C:/Users/nombo/AppData/Local/Temp/fthr-audio-startup-6nz9roes/result.json`, beside
`selected-default.log`, `legacy-default.log`, and `selected-backup-off.log`.
The scratch runner is `build/capture-startup-fix/startup_smoke.py`.

The running source app must be exited and reopened to load the corrected Python
code and discover the current engine. No installer or native rebuild is needed
for this fix.
