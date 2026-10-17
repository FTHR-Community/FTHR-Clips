# Linux catch-up to FTHR Clips 2.0.0-alpha

Status: in progress (2026-10-17). Owner: Linux maintainer.

## Inputs

- `upstream/2.0.0-alpha`: the main maintainer's 2.0.0-alpha source snapshot
  (2026-10-16), imported as one commit on `4f3f08a`, the last commit shared by
  every branch. It is Windows-only: upstream removed the Linux UI modules,
  `build_linux.sh`, `FTHR_linux.spec` and every Linux path in the shared release
  tools, and the snapshot's packager does not include `FTHRcapture_linux/`.
- `linux` at the time of the merge: 1.1.3-alpha plus the post-release fixes
  (#79, #80, #81).

## Strategy

`linux-2.0` is cut from `linux` and merges `upstream/2.0.0-alpha`, so git keeps
a real merge base and later upstream snapshots can be merged the same way. The
2.0 UI becomes the Linux UI. The Linux layer is put back on top of it in
reviewed pull requests into `linux-2.0`; when the branch is qualified on real
Linux desktops it is merged into `linux` and released as 2.0.0-alpha (Linux).

### Merge resolution rules (the integration commit)

1. Shared code: conflicting hunks take the 2.0 side; non-conflicting Linux
   hunks are kept as git merged them. Upstream deleted Linux branches in lines
   `linux` never touched, so most Linux hooks in shared files are gone after the
   merge and are restored in Phase B.
2. Linux-only files that upstream deleted (`core/compositor.py`,
   `linux_runtime.py`, `linux_tools.py`, `x11_monitor.py`, Linux tests and
   tools, `build_linux.sh`, `FTHR_linux.spec`, Wayland notices) are kept.
3. Linux-hardened shared files keep the `linux` version and receive 2.0's
   non-Linux changes later: `FTHRcapture_common/transactional_save.h` (fsync
   before publish; 2.0's copy is the old base), the release gates
   (`tools/fetch_third_party.py`, `verify_release_licenses.py`,
   `verify_version_consistency.py`, `verify_shared_memory_contract.py`,
   `verify_engine_response_contract.py`, `generate_release_assets.py`,
   `qt_runtime_manifest.json`, `release_asset_manifest.json`) and
   `.github/workflows/ci.yml`.
4. Upstream's uploader restructuring wins (`FTHR_Uploader/fthr_upload_api.py`
   and `hardware_identity_client.py` stay deleted).

CI on `linux-2.0` is expected to be red on Linux UI tests until Phase B lands.

## Phase B: Linux UI layer on the 2.0 UI

- `core/capture_bridge.py`: Linux `c_char` layout and `/dev/shm` mmap. 2.0's
  bridge calls `ctypes.windll` unconditionally. Do not change the engine
  struct: on Linux `c_wchar` is 4 bytes, so a naive restore would line up by
  accident and send UTF-32 to an engine that reads UTF-8.
- Engine launch: Linux engine path, xrandr/output-name monitor argument (2.0's
  `validate_engine_monitor_argument` rejects anything but a Windows device
  path), child environment restore, no `CREATE_NO_WINDOW` on Linux.
- Startup: crash logging (#79), Qt platform configuration, AppRun fallback,
  single instance, desktop entry before `QApplication` (#80).
- Hotkeys: GlobalShortcuts portal and Hyprland socket binds; 2.0's application
  shortcuts (Tab+S and friends) are Qt-local and should work as is; the
  `keyboard` package is Windows-only (environment marker).
- Screenshots (grim, portal worker), monitor selection, game detection,
  camera/microphone selection, frameless window, tray, open folder.
- Manual recording: 2.0 replaced the Linux skip with `pass`, so the Linux
  engine would acknowledge a recording it never writes. Keep the UI skip until
  the engine has a recorder.
- Software encoder acceptance: 2.0 accepts software only when the preference is
  Software and `nvenc_active` is set (see Phase C).
- Uploader: SFTP saving is Windows-only in 2.0 (WinVaultKeyring); add a Secret
  Service keyring path on Linux or keep saving disabled with a message.

## Phase C: engine contract

The shared-memory layout and both enums are unchanged; one new health bit
(`RECONFIGURE_REQUIRED`) is Windows-only. Work list:

1. Exit early on any `--` argument; implement `--list-playback-devices`
   (PulseAudio sinks, schema 1) and `--list-microphones` (empty list).
2. argv 24 (playback endpoint id → PulseAudio sink; capture `<sink>.monitor`,
   default when empty or missing) and argv 25 (microphone mono, ignored until
   the engine captures a microphone).
3. Limits: buffer seconds up to 900, bitrate up to 200000; adopt `ParseU32`.
4. Encoder policy as on Windows 2.0: Auto is hardware only, software only when
   the preference is Software; `nvenc_active` means "a compressed replay
   encoder is active". Correct `docs/linux.md`.
5. A byte budget for the video and audio rings (argv 6) now that buffers can be
   15 minutes long.
6. START_RECORDING with a path: reply MANUAL_RECORDING_ERROR until a recorder
   exists.

Keep the Linux `transactional_save.h`; 2.0's `replay_interval.h` is an additive
drop-in.

## Phase D: packaging and CI

- `FTHR_linux.spec`: stop excluding QtQuick/QtQml (the 2.0 editor uses
  `QQuickWidget` and QML), add `ui/qml`, the precompiled shader, `gatze.png`,
  `ui/capture_card_process.py` and the new hidden imports; keep the QML import
  set minimal; smoke-test the editor under XCB and Wayland.
- Release tools: `linux` versions plus 2.0's Qt Quick modules and assets in the
  Qt and asset manifests.
- Uploader: 2.0's `_write_core_manifest` must keep the Linux bundle hash;
  `build_linux_uploader.py` moves to the 1.4.0/v3 constants, installs
  `requirements-uploader.txt`, embeds licences and covers the plugin payload in
  the glibc gate. Pin `SecretStorage` and `jeepney` for Linux.
- `ci.yml`: the union of both (Linux jobs plus 2.0's Windows jobs, with the
  PyInstaller exit-code check restored).
- Notices and licences: union, plus paramiko and the uploader dependencies.

## Phase E: qualification and release

Physical checks on Hyprland (wlr-screencopy, VA-API) and, through community
testers, KDE Plasma and GNOME (portal path), then merge `linux-2.0` into
`linux` and publish 2.0.0-alpha (Linux).
