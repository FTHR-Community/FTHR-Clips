# Windows AMD field test — 2026-10-16

Field-test package:

`C:\Users\Tom\Documents\FTHR-Windows-AMD-Gate-20261016-01\package\FTHRClips-2.0.0-alpha-AMD-20261016-01`

Use only fresh evidence directories and output filenames. Never overwrite a
previous clip, log, or report.

## Candidate field checklist

1. Select the intended monitor by its persistent device path and confirm that
   the selected monitor and AMF encoder are on the same AMD adapter.
2. Run the candidate at H.264/AMF, 1920×1080, 60 FPS, with a native 30-second
   replay buffer. Confirm startup diagnostics report the requested WGC monitor,
   `ffmpeg-amf`, AMD, and same-adapter identity.
3. Use visibly moving, coloured content. Warm up for at least 30 seconds.
4. Press F9 several times, using a new output filename for every save. Retain
   every save request, result, output path, and diagnostic log.
5. For every clip, verify video and audio playback, full decode, dimensions/FPS,
   monotonic timestamps, export success, and engine diagnostics. Record dropped
   frames, backend failures, queue errors, and SaveClip evidence.
6. Repeat the same checklist with the original failure configuration, changing
   only the documented configuration under test. Keep those results separate
   from candidate evidence.

No physical AMD/Radeon result was obtained on the development machine. The
isolation probe returned exit code 20 because no AMD adapter was enumerated;
this is `AMD_HARDWARE_REQUIRED`, not a pass and not a simulated AMD result.

## Opt-in isolation modes and evidence limits

- **A** generates deterministic moving BGRA D3D11 frames and sends them
  directly to pinned FFmpeg AMF hardware-frame encoding.
- **B** sends the same source through production
  `FfmpegAmfReplayEncoder` conversion/scaling and its MP4 writer.
- **C** uses an explicitly selected real WGC monitor through `CaptureEngine`
  and `SaveClip`, without shared-memory mapping or global settings.

The native helper is a capture/mux exercise; it does **not** perform the full
post-capture decode/PTS/dimension/content qualification for A or B. Use the
Python runner from the complete pinned source tree for that validation. Native
C output named `transport_decode_pass_content_pending` is only transport/save
evidence; it is not a full-decode result until the runner validates the clip.
C does not claim controlled content and disables audio in its isolated config.
The normal field checklist above must separately validate application video and
audio/export diagnostics.

Exit code 20 means no suitable AMD adapter was enumerated (or the requested
AMD adapter index was unavailable); A/B/C capture was not attempted.

## Packaged helper commands

The prebuilt helper and pinned FFmpeg tools are:

```powershell
$pkg = "C:\Users\Tom\Documents\FTHR-Windows-AMD-Gate-20261016-01\package\FTHRClips-2.0.0-alpha-AMD-20261016-01"
$exe = Join-Path $pkg "FTHRClips\_internal\engine\amd-isolation-smoke.exe"
$ffmpeg = Join-Path $pkg "FTHRClips\_internal\engine\ffmpeg.exe"
$ffprobe = Join-Path $pkg "FTHRClips\_internal\engine\ffprobe.exe"
$evidence = "C:\Users\Tom\Documents\FTHR-Windows-AMD-Gate-20261016-01\amd-field-evidence-20261016-01"
New-Item -ItemType Directory -Path $evidence | Out-Null
```

First probe; proceed only if it reports a real AMD adapter:

```powershell
& $exe --mode probe --output (Join-Path $evidence "probe.mp4") --adapter-index 0 --width 640 --height 360 --output-width 960 --output-height 540 --fps 60 --frames 120 --warmup-seconds 8 --duration-seconds 3 *> (Join-Path $evidence "probe.log")
```

Then A/B with fresh output filenames:

```powershell
& $exe --mode a --output (Join-Path $evidence "a.mp4") --adapter-index 0 --width 640 --height 360 --output-width 960 --output-height 540 --fps 60 --frames 120 --warmup-seconds 8 --duration-seconds 3 *> (Join-Path $evidence "a.log")
& $exe --mode b --output (Join-Path $evidence "b.mp4") --adapter-index 0 --width 640 --height 360 --output-width 960 --output-height 540 --fps 60 --frames 120 --warmup-seconds 8 --duration-seconds 3 *> (Join-Path $evidence "b.log")
```

C requires the exact persistent monitor device path:

```powershell
& $exe --mode c --monitor-path "<explicit-monitor-device-path>" --output (Join-Path $evidence "c.mp4") --adapter-index 0 --width 640 --height 360 --output-width 960 --output-height 540 --fps 60 --frames 120 --warmup-seconds 8 --duration-seconds 3 *> (Join-Path $evidence "c.log")
```

Manual full decode for each produced clip uses the packaged FFmpeg, for
example:

```powershell
& $ffmpeg -v error -xerror -err_detect explode -i (Join-Path $evidence "a.mp4") -map 0:v:0 -f null NUL
& $ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,avg_frame_rate -of json (Join-Path $evidence "a.mp4")
```

Repeat those two commands for `b.mp4` and `c.mp4`; then retain their logs with
the helper logs. The Python runner additionally validates complete decoded PTS,
dimensions, frame coverage, and controlled A/B colour/motion samples.

The packaged `amd-qualification` source folder is an audit copy and is not
auto-executed. Running the Python source runner requires the complete pinned
source tree, Python dependencies, MSVC/MSBuild, FFmpeg headers/libraries, and
runtime dependencies; the copied subset is not a standalone source runner.
