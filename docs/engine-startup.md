# Native engine startup arguments

`FTHR_UI/main.py` launches the native engine with positional arguments. Keep
the UI's argument order and both native parsers aligned. Don't reuse retired
slots: older callers may still supply them.

The parsers are in `FTHRcapture/FTHRclips/src/main.cpp` and
`FTHRcapture_linux/src/main.cpp`. Positions below are `argv` indices; index 0
is the executable. Defaults apply when an argument is omitted.

| Position | Value | Default / meaning |
|---|---|---|
| 1 | Capture FPS | 60; supported range 1–360 |
| 2 | Replay duration | 30 seconds; supported range 1–300 |
| 3–4 | Output width, height | 0, 0 for native size |
| 5 | Video bitrate | 16,000 kbps; supported range 500–200,000 |
| 6 | Legacy raw-buffer budget | Windows: 2,048 MB; unused for compressed replay. Linux ignores it. |
| 7 | Capture mode | Windows: 0 desktop, 1 window. Linux ignores it. |
| 8 | Target window | Windows: decimal HWND, or 0. Linux ignores it. |
| 9 | Scaling mode | 0 stretch, 1 fit with bars |
| 10 | Capture output | Windows: stable monitor device path. Wayland: output name. X11: UI-resolved `@x11:x,y,width,height`. |
| 11 | Codec preference | 0 auto, 1 H.264, 2 HEVC, 3 AV1. Windows auto resolves to H.264. |
| 12 | Encoder preset | 1–7, default 4; NVENC uses P1–P7 |
| 13 | Retired multiband slot | Ignored; the UI sends 0 |
| 14 | System audio | 1 enabled, 0 disabled |
| 15 | Microphone endpoint | Windows: stable native endpoint ID; empty selects the default. Linux ignores it. |
| 16 | Microphone input gain | Windows: 0–200 percent, default 100. Linux ignores it. |
| 17 | Encoder preference | 0 auto, 1 NVIDIA, 2 AMD, 3 Intel, 4 software |
| 18 | Crop enabled | Windows: 1 enables positions 19–22, default 0. Linux ignores it. |
| 19–22 | Crop x, y, width, height | Windows: normalized source coordinates, default 0, 0, 1, 1. Linux ignores them. |
| 23 | Audio layout | 0 combined, 1 separate tracks |

Windows resets out-of-range FPS, duration, and bitrate to their defaults;
Linux clamps them to the supported range. Both constrain the preset to 1–7.
Windows also validates output dimensions and crop geometry.

An encoder preference doesn't guarantee availability. Windows public-alpha
startup requires a supported hardware backend on the capture adapter. It
doesn't silently switch to raw replay or another adapter.

Microphone capture is native on Windows and handled by Python on Linux.
Completed clips pass through UI finalization to produce the chosen audio layout.

## Cursor capture

Both engines accept `--capture-cursor 1|0` after the positional arguments,
including after the replay-storage options. Omission defaults to enabled.
The cursor arguments are removed before parsing the replay option tail.
Settings > Capture > Video encoding exposes **Capture cursor**; changing it
restarts capture and its replay buffer.

On Windows, enabled desktop capture retains DXGI cursor composition. Disabled
cursor capture selects WGC in Auto mode, because DXGI can embed the pointer in
its desktop pixels. Forced DXGI with cursor capture disabled is rejected rather
than silently recording the cursor. WGC window and monitor sessions explicitly
apply the cursor setting; exclusion requires the cursor-control session API.

On Linux, the preference controls X11 `draw_mouse`, wlr-screencopy overlay,
ext-image-copy-capture paint-cursors, and portal HIDDEN/EMBEDDED cursor mode.
Game-rendered cursors and crosshairs are part of the game image and are not
removed by this setting. A cursor deliberately hidden by a game is not invented.
