# Native engine startup arguments

`FTHR_UI/main.py` launches the native engine with positional arguments. Keep
the UI's argument order and both native parsers aligned. Don't reuse retired
slots: older callers may still supply them.

The parsers are in `FTHRcapture/FTHRclips/src/main.cpp` and
`FTHRcapture_linux/src/engine_args.cpp` (tested by `linux_engine_args`).
Positions below are `argv` indices; index 0 is the executable. Defaults apply
when an argument is omitted; on Linux an empty argument also means the
default.

| Position | Value | Default / meaning |
|---|---|---|
| 1 | Capture FPS | 60; supported range 1–360 |
| 2 | Replay duration | 30 seconds; supported range 1–900 (the UI sends the longest clip length plus 2, at most 900) |
| 3–4 | Output width, height | 0, 0 for native size |
| 5 | Video bitrate | 16,000 kbps; supported range 500–200,000 |
| 6 | Buffer budget (MB) | Windows: legacy raw-buffer budget, 2,048 MB default, diagnostic only. Linux: byte budget for the encoded video ring, clamped to 64–16,384; 0 or omitted means no budget. Over budget the oldest GOPs are dropped and the engine logs it. |
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
| 24 | Playback device | Windows: render endpoint ID. Linux: PulseAudio sink name; the engine records that sink's monitor. Empty selects the default output (and follows it when it changes); a sink that does not exist is logged and falls back to the default. |
| 25 | Microphone mono | Windows: 1 downmixes the microphone to mono, default 0. Linux ignores it. |

Windows resets out-of-range FPS, duration, and bitrate to their defaults;
Linux clamps them to the supported range (0 FPS or duration becomes the
default) and logs a `[Config]` line for every value it changes. Both parse
numbers with `FTHRcapture_common/capture_config_limits.h` `ParseU32`, map an
unknown codec or encoder preference to auto, constrain the preset to 1–7 and
fall back to native size for a half-specified or larger-than-8K resolution.
Windows also validates crop geometry.

## Listing modes

`--list-playback-devices` and `--list-microphones` print one JSON line and
exit without creating shared memory or starting capture:

```json
{"schema_version":1,"microphones":[{"endpoint_id":"...","display_name":"...","device_state":1,"is_default":true}]}
```

Both modes use the `microphones` key. On Linux, playback devices are the
PulseAudio (or PipeWire-Pulse) sinks: `endpoint_id` is the sink name and
`display_name` its description. The Linux microphone list is empty because the
engine does not capture a microphone. If the audio server cannot be reached
the engine prints `{"schema_version":1,"error":"..."}` on stderr and exits 1.

On Linux any other argument starting with `--`, or a listing flag combined
with other arguments, exits with status 2 and a usage message; it is never read
as a positional value.

An encoder preference doesn't guarantee availability. Windows public-alpha
startup requires a supported hardware backend on the capture adapter. It
doesn't silently switch to raw replay or another adapter. Linux follows the
same policy: auto tries NVENC, VA-API/AMF and QSV only, and a software encoder
is used only with preference 4. Shared memory `nvenc_active` means "a
compressed replay encoder is active" on both platforms, software included; the
UI reads `active_codec` to tell them apart.

## Recording commands

On Linux `START_RECORDING` with an empty `ui_string` resumes capture after
`STOP_RECORDING` paused it (focus pause). With a path it is a manual recording
request, which the Linux engine cannot serve yet: it answers
`MANUAL_RECORDING_ERROR` with a message in `engine_string`. The engine clears
`ui_string` after reading a `SAVE_CLIP` path so that a later resume, which does
not write `ui_string`, is not mistaken for a recording request.

Microphone capture is native on Windows and handled by Python on Linux.
Completed clips pass through UI finalization to produce the chosen audio layout.
