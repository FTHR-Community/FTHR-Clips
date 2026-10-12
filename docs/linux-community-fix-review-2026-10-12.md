# Linux community bug review — 2026-10-12

## Baseline and scope

This change starts from the live upstream `linux` branch at
`eb17039ce85e3abf71b3190132a65a7f615efcda`. The local Windows source has
uncommitted work and is not a drop-in replacement for that branch. Its committed
ancestor is 83 commits behind this Linux baseline. Only reviewed, relevant
camera lifecycle and optional sound-cue changes are carried over; the remaining
Windows work stays separate.

The community's merged work is retained, including the AppImage glibc baseline
([#40](https://github.com/FTHR-Community/FTHR-Clips/pull/40)), engine connection
race ([#50](https://github.com/FTHR-Community/FTHR-Clips/pull/50)), frame format,
resize and audio recovery work
([#51](https://github.com/FTHR-Community/FTHR-Clips/pull/51)), and ELF alignment
repair ([#53](https://github.com/FTHR-Community/FTHR-Clips/pull/53)). Copying the
older local native Linux files would remove these fixes, so they are not copied.
No native capture/encoder, shared-memory ABI, audio clock, Windows packaging,
or existing asset/license decision is changed here.

## Issue disposition

| Report | Source finding and action | Qualification / remaining work |
| --- | --- | --- |
| [#52 KDE/GNOME Wayland screenshots](https://github.com/FTHR-Community/FTHR-Clips/issues/52) | `grim` requires compositor support; Qt fallback is not a reliable Wayland screenshot path. Keep exact-output `grim`, then offer an explicit Screenshot portal action with bounded async handling and local PNG validation. | The portal chooses content independently of the replay monitor. Protocol/regression tests are not native compositor qualification. KDE/GNOME physical tests remain pending. |
| [#56 non-default microphone](https://github.com/FTHR-Community/FTHR-Clips/issues/56) | The meter opened the new input before `MicRecorder` switched, reproducing an exclusive-device conflict. Reorder teardown/open/share and persist the Linux input name with unique-input resolution. | Real recorder/meter code is tested with an exclusive fake stream. The external Fedora device/driver root cause is not independently reproduced; field validation remains pending. |
| [#44 camera missing / refresh freezes](https://github.com/FTHR-Community/FTHR-Clips/issues/44) | Refresh re-opened the active exclusive camera and emitted intermediate combo-box changes. Preserve the known-running device, block refresh signals, release probe handles, and isolate capture generations after failed/stuck readers. | The remaining scan of other camera nodes is synchronous and can still block. Native camera testing and camera/audio sync remain pending; do not close the entire issue from these tests. |
| [#45 Gary steals focus](https://github.com/FTHR-Community/FTHR-Clips/issues/45) | Existing overlay already requests non-focusable, non-activating, input-transparent behavior. No missing-flag defect is demonstrated. | Keep open: collect compositor/window activation evidence on affected Hyprland before changing window flags or introducing timing workarounds. |
| [#28 GLIBC launch failure](https://github.com/FTHR-Community/FTHR-Clips/issues/28) | Addressed in merged #40 and the published 1.1.2-alpha build's ELF check; no duplicate patch here. | Affected user's current-release retest remains the relevant field confirmation. |
| [#6 qualified Linux runtime](https://github.com/FTHR-Community/FTHR-Clips/issues/6) | Build/runtime documentation and existing gates are preserved. | Native desktop/GPU qualification still matters. WSL compilation is not capture qualification. |

Independent open PRs stay separate for their maintainers to review:
[#42 VA-API symbol/RAM guard](https://github.com/FTHR-Community/FTHR-Clips/pull/42),
[#16 portal global shortcuts](https://github.com/FTHR-Community/FTHR-Clips/pull/16),
and [#19 desktop identity](https://github.com/FTHR-Community/FTHR-Clips/pull/19).
Windows-only reports and unrelated editor/upload enhancements are not included.

The optional notification-sound helper also now tolerates `ImportError`/`OSError`
from QtMultimedia with a bounded warning and disabled sound cues. This is a
small carried-over safety fix, not a substitute for the merged ELF repair or a
claim that every application import survives a broken multimedia runtime.

## Screenshot protocol and permissions

The [Screenshot portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)
does not expose FTHR's monitor ID as a source-selection option. The explicit
**CHOOSE SCREENSHOT** action therefore tells the user that the compositor picks
the content. This avoids silently saving a different monitor. The
[Request protocol](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html)
requires response subscription before the method reply; token/path matching,
early responses, cancellation, deadlines and late callbacks are tested.

No media or credentials are uploaded. Only bounded local PNG results are
accepted. Temporary imported images use the existing atomic screenshot save
path. QtDBus is added to the Linux frozen application's hidden imports; it is
already covered by the existing Linux Qt runtime license allowlist.

## Validation

Environment: Ubuntu 24.04 under WSL2, GCC 13.3, Python 3.12.3, pinned PySide6
6.11.1 and pytest 9.1.1. Dependencies were installed from
`requirements-alpha.txt` and `requirements-dev.txt` into a separate environment.
The project pytest minimum and existing CI exclusions were not weakened.

| Check actually executed | Result |
| --- | --- |
| Complete Python suite using the existing Linux CI command/exclusions | 1,044 passed, 4 skipped |
| Additional microphone/camera/sound focused run, including existing Windows microphone contract tests | 54 passed (overlaps the full suite) |
| Additional Linux AppImage report/runtime/tool tests excluded by the existing CI command | 36 passed |
| Real QtDBus Screenshot method/early Response on an isolated session bus | 1 passed; now also runs in Linux CI |
| Native Release build with pinned Linux LGPL FFmpeg | Engine and playback mixer built successfully |
| Native CTest | 17 passed, 1 skipped; VA-API encoder skipped without a render device |
| Ruff, byte compilation, product version gate | Passed |
| Shared-memory and engine response-publication contracts | Passed; `FTHR_SharedMemory_v4` unchanged |
| Exception-handler ratchet | Passed: 91 silent handlers vs existing baseline 92 |
| Source license/asset/FFmpeg/linkage gate | 124 checks, 0 failures, 1 warning for absent Windows FFmpeg binaries in this Linux checkout |
| Native desktop/compositor, real microphone and webcam qualification | NOT RUN |
| Full frozen AppImage on the Ubuntu 22.04 release baseline | NOT RUN locally; existing PR packaging CI remains enabled |

An additional unchanged live-capture integration probe was also attempted:
`tests/test_capture_backend_detection.py` had 3 passing checks and 1 failure at
`test_backend_auto_detects`. WSLg exposed a Wayland socket but neither supported
screencopy protocol nor a usable ScreenCast portal, so the engine correctly
reported `No capture backend available`. The native engine source and this
test are identical to the reviewed upstream baseline. This is not a passing
graphical-capture test; it is not worked around by changing production behavior
or weakening the test. This test was already excluded by upstream Linux CI.

Reproduce the private-bus protocol check independently of a real desktop:

```bash
dbus-run-session -- env QT_QPA_PLATFORM=offscreen FTHR_TEST_PRIVATE_DBUS=1 \
  python -m pytest tests/test_wayland_screenshot_portal.py \
  -k real_qtdbus_portal_response_before_method_reply
```

The new regressions cover the actual recorder/meter with a fake exclusive input,
active/inactive camera enumeration, signal suppression, provider exceptions and
generation ownership, optional multimedia import failures, portal cancellation,
late/early responses, invalid images/URIs, and shutdown during a controlled real
image worker. They do not use recorded audio or captured desktop content.
Native desktop screenshot, microphone, webcam and gaming-capture field tests
must not be inferred from these results. The license gate is technical
verification, not a new legal determination.

## Native desktop follow-up

1. KDE/GNOME: use F12 without a working `grim` backend, choose the screenshot
   explicitly, verify the selected content and saved PNG, then test cancel,
   repeated requests and shutdown with the permission dialog open.
2. A compositor with `grim`: verify F12 still saves only the selected output,
   including a second monitor; an unrelated portal dialog should not appear.
3. Fedora/PipeWire/ALSA: switch from System Default to an explicit input, check
   meter and recorded audio, reopen settings/restart the app and check the saved
   choice. Include device enumeration and backend error text if it still fails.
4. V4L2 camera: refresh while recording on a nonzero device, confirm the same
   feed stays selected, unplug/reconnect, and report any slow probe separately.
5. Keep #45 open until affected Hyprland testing identifies which surface
   actually receives focus. Do not treat the camera fixes as A/V sync evidence.
