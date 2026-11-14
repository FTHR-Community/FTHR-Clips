# Guided Windows setup — 7 November 2026

Approved concept B is implemented in `installer_windows.iss`: neutral charcoal (#242629 main, #202326 sidebar/title, #2C2F33 fields), gray borders, mint #00FFAA actions, and a left Privacy / Folders / Install sidebar. Policy acknowledgement stays unchecked. Existing folder selection, previous clip settings, shortcuts, update identity, admin requirements, legacy opt-in cleanup, and uninstall safeguards remain.

The successful completion page uses Inno's real native finish lifecycle. A single `[Run]` entry exposes **Open FTHR Clips**, checked by default. `postinstall nowait skipifsilent runasoriginaluser` processes it on Finish only after successful installation, never silent setup or failed/cancelled setup. Inno suppresses postinstall choices when restart is required and retains its native restart radios. Closing the custom title-bar X clears the launch selection before finishing. As documented by Inno, a setup already launched elevated cannot recover a nonexistent pre-elevation token; normally launching the installer allows its original-user mechanism to work.

## Validation

- `python -m pytest tests/test_windows_installer_contract.py -q`: passed (2 tests).
- `python tools/verify_windows_installer_lifecycle.py`: passed, including checked-default, single-entry, original-user/silent flags, palette/sidebar, and retained lifecycle safeguards.
- Full native preview and full normal/admin installer compiled successfully against the existing `dist/FTHRClips` payload. **That payload was not rebuilt for current editor/native changes. These files are qualification artifacts, not a current complete release.**
- Final source also compiled with normal/admin settings and a tiny fixture payload for syntax verification.
- Impeccable mechanical detector returned no findings for the installer script.
- Native privacy and folder screens reviewed. Unchecked consent blocks Next, checking clears the error; Space/Enter keyboard consent/navigation verified. Existing clip path was retained. Cancellation exits the preview.
- Native progress and finish controls were also rendered in synthetic preview pages. The completion checkbox is visibly checked. No app installation or completion launch was performed; actual install/update/uninstall and restart-required behavior still need qualification in a disposable Windows VM.

## Preview and evidence

Run `python tools/build_windows_installer_preview.py` to build `build/guided-installer-preview/FTHRClips-Guided-Preview.exe`. This uses a tiny fixture payload, requires no elevation, and always blocks `PrepareToInstall`, even in silent mode. After Privacy/Folders, the preview offers synthetic progress and completion screens; they explicitly state nothing is installed/launched. Those preview-only pages are excluded from normal builds.

Evidence is in `build/guided-installer-evidence/`: `privacy-native.png`, `folders-native.png`, `installing-synthetic-native.png`, `finish-synthetic-native.png`. The latter two demonstrate native controls in a simulation, not a successful installation.

Compiled normal artifact: `build/guided-installer-normal/FTHRClips-Setup-2.1.0-alpha-x64.exe` (existing bundle; unsigned). Final source syntax artifact: `build/guided-installer-syntax/FTHRClips-Setup-2.1.0-alpha-x64.exe` (fixture payload, must not distribute).

Official lifecycle reference: https://jrsoftware.org/ishelp/topic_runsection.htm

## Updated local package

After the editor/audio changes completed, an isolated fresh PyInstaller bundle was built in `build/studio-package/dist/FTHRClips`, explicitly using `build/audio-exclusions/engine/FTHRclips.exe`. The bundled engine SHA-256 matches that tested native build and its safe audio-exclusion capability probe passes. The frozen archive contains the Studio editor, running-app picker, exclusion widget, and capability guard. Source artwork entries in `tools/release_asset_manifest.json` were updated for the reviewed generated installer assets.

The resulting current installer is `build/studio-package/installer/FTHRClips-Setup-2.1.0-alpha-x64.exe`, SHA-256 `4306cfb7f84f623db0a682f2b97a0976a9cbc1c2e5c288019a5cd3635781598f`. This supersedes the stale-payload qualification artifacts above for local testing. Source asset/license verification passed 245 checks (Linux FFmpeg not vendored warning); Windows bundle verification passed 121 checks without warnings. Final installer lifecycle verification passed with unsigned-artifact warnings. Build and verification logs are under `build/studio-package/`.

This is an unsigned local test build, not a published release. It was not installed over the running app. The full native suite retains the previously documented audio activity-hysteresis assertion failure; targeted audio exclusions, capture recovery, backend policy, and editor checks passed. Real install/update/uninstall and live recording qualification remain outside the preview checks.
