# Windows capture recovery verification

The reported WGC monitor session paused when its game lost focus and failed when focus returned while the source switched between 1600 × 900 and 1920 × 1080. The monitor path previously stopped on the first mismatched frame or item-size sample. The UI heartbeat could then reapply the already accepted request outside the existing recovery timer and retry budget.

WGC frame surfaces retain their pool allocation size. Frame content can be smaller or clipped during a size change. Microsoft documents recreating the pool when its geometry or device must change. FTHR's monitor path also binds the capture textures and encoder to the source geometry, so a stable change rebuilds the selected source through the existing engine restart. The ordinary WGC window path keeps its existing in-place pool recreation and canvas handling. [Microsoft Learn Screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture#react-to-capture-item-resizing-or-device-lost)

- A monitor geometry mismatch gates frame submission. A fresh frame that agrees with the original size resumes capture on the existing encoder. A valid new size must persist for at least 400 ms. An unsettled or zero-sized source requests reconfiguration after at most two seconds of active observation.
- WGC device removal, reset, and access loss request a fresh generation. Other consecutive frame failures retain their failure diagnostics.
- The UI schedules one recovery callback per failed generation and waits 750 ms, 1.5 s, then 3 s across replacement engines. Thirty seconds of sustained healthy frame progress restores the budget. Pending or active saves, manual recording, shutdown, admission changes, and newer settings/source intent take priority. The timer rechecks both the native generation and the current process owner before requesting a restart. The failure remains visible when no automatic retry is pending.
- When startup reduces replay history to fit memory, the persistent warning shows the requested and effective durations and opens Storage settings. A tray-started session retains the warning until the main window is built. A successful startup with full history clears the notice.

## Validation

The isolated Windows x64 production build succeeded, and 31,077 native checks passed. Policy cases exercise transient focus geometry, stable resize, zero-sized content, and alternating source sizes. The ordinary WGC window lifecycle smoke passed on Intel and NVIDIA.

187 focused Python tests passed with PySide6 6.11.1 on Windows. The changed Python files passed Ruff.

```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts= tests/test_automatic_engine_recovery.py tests/test_engine_error_recovery.py tests/test_pr90_recovery_ui.py tests/test_source_authority_and_save_reporting.py tests/test_game_profile_integration.py tests/test_capture_shutdown_recovery.py tests/test_capture_health.py
.\.venv\Scripts\python.exe -m ruff check FTHR_UI/main.py tests/test_automatic_engine_recovery.py tests/test_game_profile_integration.py
```

The physical lifecycle harness exercises ordinary WGC window capture. The protected Fortnite monitor path still needs a dedicated hardware run on the affected AMD setup. Reproduce the 1600 × 900 to 1920 × 1080 mode changes with repeated focus loss and restore, and verify that capture either resumes on matching geometry or rebuilds the same selected target. Include a clip save across the transition and manual-recording finalization when a replacement generation is required.
