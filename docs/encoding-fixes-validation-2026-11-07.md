# Encoding and replay policy fixes — 2026-11-07

Implemented independent low-FPS clip acceptance (enabled by default), bounded
NVENC raw buffer allocation, and QSV shared-device multithread protection.
The setting survives persistence, accepted capture snapshots, and process launch;
both policy switches remain independent in native replay validation.

## Verification

- Release x64 engine and test solution build passed.
- 174 focused Python tests passed; a further 101-test integration selection
  passed (overlapping suites, not additive coverage).
- Native QSV: 43 checks; replay policy: 546; replay memory: 1,920 checks passed.
- Isolated NVENC lifecycle/buffer policy harness: 6,016 assertions passed.
- Real NVIDIA smoke: eight CPU/GPU scaling scenarios passed, each submitting
  and draining 180 frames with the four-slot pool and fully decoding output.
  Evidence: %TEMP%/fthr-nvenc-pool-live-20261107/results.json.
- Ruff on changed Python sources/tests passed.
- Shared-memory and response publication contract checks passed.
- Settings control rendered and inspected, with persistent defaults and save
  failure rollback covered by Qt tests.
- Real QSV desktop H.264 storage replay and HEVC memory replay both saved fully
  decodable two-second clips. See qsv-startup-validation-2026-11-07.md.

The complete native suite stops at the previously documented unrelated
process-loopback activity-hysteresis assertion. Exact before/after application
RAM reduction is not measured; four raw buffers versus 32 is the verified
allocation change. Storage replay still uses a bounded write queue and metadata.

## Evidence

Workspace parent logs: encoding-fixes-build.log, encoding-fixes-native-tests.log,
encoding-fixes-pytest.log, encoding-fixes-integration.log, encoding-fixes-installer.log.
Settings render: encoding-fixes-settings-2026-11-07.png.
Focused NVENC evidence: %TEMP%/fthr-nvenc-pool-policy-20261107/.

Installer build and final lifecycle/license gates passed. The unsigned local installer is Output/FTHRClips-Setup-2.0.0-alpha-x64.exe. SHA-256: 67457c5f441df2a384444b5755282d0b2f911a057669065adbdd7864558cfbbf. The packaged engine hash matches the live-tested development engine.
