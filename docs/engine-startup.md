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
| 2 | Replay duration | 30 seconds; supported range 1–1800 |
| 3–4 | Output width, height | 0, 0 for native size |
| 5 | Video bitrate | 16,000 kbps; supported range 500–60,000 |
| 6 | Memory budget | Windows: legacy raw-buffer budget 2,048 MB. Linux: in-memory replay buffer budget (default 2,048 MB, range 64–4,096 MB); configurations exceeding budget are rejected. |
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
