# Application audio exclusions

Windows **Programs** audio capture now supports a persistent list of excluded
executables. Audio settings provide the local running-app picker, executable
icons, and saved rules. Editing the list requests the
existing capture restart path; an accepted capture session retains its own
immutable exclusion configuration.

## Behavior and boundaries

- The global `application_audio_exclusions` setting stores at most 64 normalized,
  path-free executable identities, such as `discord` or `spotify`. Matching does
  not depend on a PID, installation path, or a currently active audio session.
  Apps discovered later and restarted apps pass through the same rule.
- Rules apply only in Programs mode. Device capture still contains all sound
  routed to the chosen device. Existing clips retain their recorded sources.
- With any Programs exclusion, the native engine does not start the full-device
  loopback capture or retain a system-mix backup. Unavailable application
  capture stays silent rather than falling back to an unfiltered device mix.
  Available microphone capture remains independent.
- Audio settings also expose **Keep System Audio Backup**, backed by the global
  `keep_system_audio_backup` boolean (default `true`). Turning it off omits the
  full-device loopback from future Programs clips and recordings. Device modes
  keep their audio behavior. Exclusions always override the saved backup
  preference; the checkbox is disabled while exclusions apply, and the saved
  preference returns after they are removed. This control is absent from the
  clip editor.
- Filtering occurs before application tree grouping and provider creation.
  Excluded executable trees and broad ancestor providers that would include
  their audio are omitted. An excluded running parent does not need its own
  audio session for its render helpers to be excluded. Other disjoint programs
  remain eligible. Windows process-tree grouping can omit additional containing
  groups; discovery follows session notifications and the existing two-second
  topology safety poll.
- Executable basenames define rules: same-named executables in different
  locations share a rule. Process paths are used locally for icon resolution
  and are never added to clip manifests. Unreadable process metadata may keep
  an app out of the running-app picker. A generic icon is the fallback.

## Native compatibility

The startup contract carries the normalized list in the named
`--audio-exclusions` argument. The immutable Python `CaptureConfig` and request
serialization include the encoded list, while profile resolution takes the
list explicitly from global values rather than allowing per-game overrides.

Before an audio-enabled Programs capture with exclusions, Python requires a
qualified native engine. It reads the executable first and requires both the
existing encoder-probe marker and the new capability marker. Only then can it
run the safe nested command:

```text
FTHRclips.exe --probe-encoder --audio-exclusions-capability
```

Success requires exit zero, exactly `FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1` on
stdout, and unchanged executable bytes. Positive qualification is cached by
resolved path and SHA-256. Old, missing, changed, timed-out, or malformed engines
cannot start the excluded Programs capture. The UI reports that an updated
capture engine is required. The native probe returns before capture, COM,
monitor, or encoder initialization.

Disabling the Programs backup requires a separate qualified native capability:
`--probe-encoder --audio-backup-capability` must return exactly
`FTHR_AUDIO_BACKUP_CAPABILITY_V1`. Exclusion support alone cannot qualify backup
control. The named `--keep-system-audio-backup 0|1` argument carries the immutable
preference to the native engine. Normal, per-game profile, and manual recording
requests take this preference from explicit global values.

## Reusable UI API

- `core.running_apps.list_running_apps() -> list[RunningApp]`, with immutable
  `identity`, `name`, `executable_path`, and `pid` fields. Inventory uses Win32
  Toolhelp and limited process queries without a new Python dependency.
- `core.running_apps.application_icon(identity, size=20) -> QIcon`. Called on
  the Qt GUI thread; it resolves local executable icons with `ExtractIconExW`,
  then Qt's file icon provider and a generic fallback.
- `ui.running_app_picker.RunningAppPickerDialog(parent=None, *, title=...,
  excluded=...)`, with search, refresh, icons, and `selected_app()`.
- `ui.audio_exclusions.AudioExclusionsWidget(settings_manager, parent=None)`
  persists add/remove operations, rolls back on a failed settings save, and
  emits `changed` only after success. `refresh()` and refresh-on-show synchronize
  multiple views. Labels elide in narrow panels while retaining full tooltips
  and accessible names.

## Validation

- 205 Python tests passed across application exclusions, the backup preference,
  launch argument and old-engine guards, device-track settings,
  immutable capture/session boundaries, capture profile policy, admission
  integration/bridge, and combined/separated audio policy.
- 172 additional cursor, admission revocation/camera, capture health/settings,
  and settings-manager regression tests passed.
- Ruff passed the four new runtime/UI modules and the new focused test file.
- Isolated Release x64 builds succeeded for the complete native engine and
  native tests. The new engine's safe capability probe qualified successfully.
- `--audio-exclusions-only`: 27 registry/policy checks passed, including backup
  on/off and Device-mode isolation, app
  restarts, nested excluded renderers, an excluded parent without its own
  session, disjoint sibling preservation, and disabling the device backup.
- `--capture-recovery-only`: 93 checks passed. The existing WARP pixel check
  was skipped because `ID3D11VideoDevice` was unavailable; 51 WGC lifecycle
  checks ran.
- `--capture-backend-policy-only`: 17 backend-policy and 110 DXGI-cursor checks
  passed.
- The full native suite still encounters the pre-existing assertion
  `first meaningful block is held by activity hysteresis`. Its expectation was
  preserved; no activity-admission implementation or unrelated test change is
  part of this work.

Latest build outputs are isolated under `build/system-audio-backup/engine/FTHRclips.exe`
and `build/system-audio-backup/tests/FTHRclips_tests.exe`. Logs are in the same
parent directory: `engine-build.log`, `tests-build.log`,
`audio-exclusions-only.log`, `capture-recovery-only.log`,
and `capture-backend-policy-only.log`. The baseline full-suite failure is
recorded separately in `build/audio-exclusions/native-tests.log`. These builds did not
replace the live or distributed binaries.
