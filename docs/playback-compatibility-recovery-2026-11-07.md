# Playback compatibility recovery — 2026-11-07

The failed clip was `Desktop/desktop_clip_from_07Nov2026_15-26-26.mp4`, opened in diagnostic session `9767c880-ff30-41ed-8403-50bde86b5d43`. At 14:26:39 UTC the Windows Qt player reported `InvalidMedia` and `Unsupported media, a codec is missing.` Later Desktop and Minecraft clips in session `51760a92-86a7-4ba1-86b2-10da81b81199` opened without decoder errors.

## Evidence and cause

The original failed clip is intact: ffprobe succeeds and a full FFmpeg decode of its H.264 video and all three float PCM (`fpcm`) audio streams exits successfully without errors. Its video track reports 60 seconds and 2733 samples. Windows Media Foundation cannot open the original PCM-in-MP4 container; the editor normally prepares a disposable copy with copied video and AAC container audio while its audio mixer continues to use original lossless tracks.

That compatibility conversion succeeded, but FFmpeg rounded the video track duration from 60.000 to 59.917 seconds. The last video sample is at 59.900 seconds with duration 0.016667 seconds in both files. The old validator allowed only one nominal frame of tail edit rounding (16.667 ms at 60 FPS), rejected the 83 ms difference, removed the preview, and supplied the unsupported original to Qt.

## Change and bounds

The validator now accepts at most 250 ms (plus 2 ms muxing tolerance) of shorter trailing presentation padding when the positive video sample count is unchanged, video codec tag matches, and presentation start is unchanged within 2 ms. Longer output timelines, missing samples, changed starts and excessive drift are rejected. Irregular capture cadence means the final presentation hold can exceed one nominal frame. The video remains copied; no video transcode or original media mutation occurs.

## Validation

- Playback proxy and viewer compatibility suites: 34 tests passed, including rejection of sample loss, changed starts, increased duration and excessive tail drift, plus the exact 2733-sample field metadata regression.
- All 2733 video packet PTS, DTS, sample durations and SHA-256 payload hashes compare identically between the failed original and the fixed preview.
- An isolated Qt process using the application's Windows media backend reproduces the original failure: zero decoded frames, `InvalidMedia`, missing-codec error.
- The same process opens the fixed compatibility preview: `LoadedMedia`, three valid video frames, no errors.
- Full original FFmpeg decode succeeds; the original file was never modified.

Local investigation artifacts are in the sibling `playback-failure-evidence` directory: `prepare-trace.json`, `validation.json`, `qt-validation.json`, and the disposable preview. The isolated checks do not replace a complete interactive editor acceptance check.

Live application verification: after restart, the original 15:26:26 library entry opened successfully in the normal editor, displayed Ready, and advanced beyond 00:11 / 01:00 with visible video. The original recording was not modified.
