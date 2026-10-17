# Selected-window preview

The source selector shows a small preview directly below the window dropdown,
with the selected window's title. Selection uses the window ID, not the app
name, so two Firefox windows can be distinguished. This is a preview of the
pending selection; applying a capture-source change still uses the existing
Apply flow.

## Platform support

- Windows: DWM composites a live thumbnail from the exact selected HWND into
  FTHR's popup. No second capture encoder or full-resolution Python image copy
  is involved. Closed, minimized or unavailable windows show a status instead
  of retaining another window's picture.
- Linux/X11: the existing FFmpeg executable samples the exact selected XID at
  2 FPS and delivers a letterboxed 320 × 180 RGB preview. The process runs only
  while the preview is visible. It has a frame watchdog, bounded image/error
  buffers and asynchronous termination. An unavailable window does not trigger
  a desktop-crop fallback. Visibility/protected-surface behavior depends on the
  X server and application.
- Linux/Wayland: no automatic exact-window preview is claimed. The compositor
  addresses currently in FTHR's list are not interchangeable with X11 window
  IDs or portal-selected stream identities. The UI explains the limitation;
  it does not start an unrelated portal picker or show a guessed desktop crop.

**Existing Linux limitation:** the native Linux recording engine currently
ignores the UI's window target and records a desktop output. The preview does
not implement window-only recording. The Linux UI explicitly states this.
Implementing compositor-native window capture/preview needs a separate design
for identity, permissions and stream ownership.

## Isolation and privacy

The preview never activates or sends input to the selected window. Hiding the
selector stops preview work; changing selection clears the previous picture.
Preview pixels are transient and are not saved or added to diagnostic reports.
The capture engine, audio, replay guards, shared-memory ABI and existing
diagnostic-export contract are unchanged.

## Validation boundaries

Deterministic tests cover exact window selection, same-app/different-window
identities, selection changes, lifecycle cleanup, bounded process buffers,
native failures, geometry/aspect ratio and unsupported display modes.

A controlled WSLg/X11 test sampled two self-owned Qt windows. Covering the
selected window with the other window did not substitute the cover's content;
changing the covered source's color changed its preview. This qualifies that
WSLg fixture only, not all native Xorg setups, protected/GL surfaces or native
Wayland desktops. Windows DWM API/lifecycle checks are distinct from a visual
check of the final packaged application.

The real Linux preview backend was also exercised with QProcess and FFmpeg,
not just a reconstructed command. The two self-owned XCB windows produced the
expected red and green 320 × 180 images: the first frame arrived in 0.93 s,
switching windows took 0.86 s, and restarting after stop took 0.86 s. Stop
reaped the process in 0.26 s; disposal killed/reaped it and destroyed the
backend in 0.03 s. The fixture injected an X11 environment into WSLg; the
production factory still refuses native Wayland. These are fixture timings,
not a recording-performance benchmark.

The Windows check registered, resized, switched and released DWM thumbnails
for two self-owned native Qt windows. It also verified minimized-window
cleanup. This confirms the native API/lifecycle boundary, not the visual
result in the packaged FTHR application. No packaged build was replaced or
restarted for this feature.

Validation on 2026-10-12:

- Windows full Python suite: 1,332 passed, 45 skipped; two existing Qt
  deprecation warnings. An isolated test environment supplied pytest 9.1.1.
- WSL focused preview, source-popup and capture-settings tests: 27 passed.
- Ruff on the six feature/integration/test Python files: passed.
- Shared-memory and exception-handling contracts: passed, without lowering
  existing gates. The mapping remains `FTHR_SharedMemory_v4`.
- Own offscreen UI fixtures: preview layout and Linux warning checked with
  the bundled font. These use synthetic images, not native preview evidence.
- Final packaged-app visual check and physical native Linux qualification:
  NOT RUN. No recording-engine rebuild, Git commit or push was performed.

## References

- [Microsoft DWM thumbnail registration](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/nf-dwmapi-dwmregisterthumbnail)
- [FFmpeg x11grab options](https://ffmpeg.org/ffmpeg-devices.html#x11grab)
- [Pinned FFmpeg XCB window drawable implementation](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/libavdevice/xcbgrab.c)
- [Qt screen-grab visibility limitations](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QScreen.html#PySide6.QtGui.QScreen.grabWindow)
