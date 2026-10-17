# Windows A/V investigation — 2026-10-17

Status: **PARTIAL — one replay-boundary defect fixed and regression tested;
historical Windows playback compatibility defect isolated, not fixed.**

Repository: `C:\Users\Tom\Documents\FTHR-Clips-master`, branch
`integrate/latest-project-source`, HEAD
`d990630f5befd0d19ddf371dddf1e39dd5f87c02`. No staging, commit or push.
The pre-existing dirty source state was backed up before this task; comparisons
use that backup, not a reset to the older HEAD.

Evidence directory:
`C:\Users\Tom\Documents\FTHR-Warzone-AV-Investigation-20261017-01`.
See its `baseline.json`, `task-review.json`, `task-only.diff` and `REGRESSION.md`.

## Exact historical media and user evidence

- Original: `C:\Users\Tom\FTHR_Clips\cod\cod_clip_from_09Oct2026_21-15-43.mp4`.
- SHA-256: `E6828F5324D4EBE18ED9CADC83FEEDD16674590D5B6790CC61367E6380F5795D`.
- 224,174,525 bytes; 1920x1080 H.264; approximately 30 seconds.
- One stereo 48 kHz AAC track titled `Combined Audio`; no native sidecar.
- Tom hears shots near 2–3 seconds and sees firing near 4–5 seconds.
- Tom confirms the other affected application is **Windows Media Player**,
  not VLC. Reports from external users remain FIELD VALIDATION PENDING.

Original media is unchanged. No recording session was stopped or started.
This old combined file is not a new native test recording, does not retain
separate source identities and cannot validate microphone synchronization.

## 1. Historical clip: Windows playback boundary

Fresh strict full FFmpeg decode of video and audio: exit 0. Decoded video has
1,792 visible frames, no PTS regressions, largest PTS gap 66.666 ms; decoded
audio begins at PTS 0 with continuous timestamps. AAC skip/priming metadata was
handled once by the decoder, not subtracted again.

The first visible ammunition decrement is at decoded video PTS 2.916667 s.
An audio attack candidate occurs at 2.888 s, but the combined stream does not
identify whose shot/source it is. These are not unambiguous physical sync
markers; equal stream origins or near-matching attacks do not prove A/V sync.

Silent Qt 6.11.1 image-content probes matched actual decoded images against
the pinned FFmpeg presentation timestamps (not frame-index/FPS estimates):

| Qt backend | Reported video PTS | Matched content PTS | Content minus reported PTS |
| --- | ---: | ---: | ---: |
| Windows | 2.016666 s | 0.716667 s | approximately -1300 ms |
| Windows | 3.016666 s | 1.716667 s | approximately -1300 ms |
| Windows | 4.016666 s | 2.716667 s | approximately -1300 ms |
| Windows | 5.016666 s | 3.716667 s | approximately -1300 ms |
| Windows | 6.016666 s | 4.716667 s | approximately -1300 ms |
| FFmpeg | all sampled PTS | matching content PTS | approximately 0 ms |

Image matching uncertainty is approximately one 60 Hz frame (16.7 ms).
Both pixel and perceptual metrics agreed at the stable points. The first
Windows snapshot had no reliable match and was excluded.

An independent silent Media Foundation Source Reader audio decode matched
five 500 ms content windows against FFmpeg with an approximately 42.333 ms
decoder/priming-boundary difference, not 1.3 seconds. Search resolution was
1 ms. It used no speaker endpoint. Its independent RGB32 video request failed
with `MF_E_INVALIDMEDIATYPE (0xC00D36B4)`; that helper produced no video proof.

The original MP4 has video edit-list `media_time=58500/90000 = 650 ms` and
audio edit-list `media_time=1024/48000 = 21.333 ms`. A stream-copy diagnostic
variant ignoring input edits removed the Windows video-content offset when
compared against the variant's own decoded timeline.

**That variant is not a sync fix:** independently ignoring unequal audio/video
edits changes their relative content timing and exposes decoder pre-roll.
Its duration is 30.650 s instead of 30 s. Do not replace the original with it,
publish it as repaired, or apply `-ignore_editlist` in production.

Conclusion: strong, repeatable evidence isolates an MP4-edit-list/Windows
playback compatibility boundary matching the reported symptom. It does not
prove the historical capture clock or finalizer was wrong. Direct screen and
speaker timing, and Windows Media Player's rendered scheduling, were NOT RUN.

FTHR's default Qt Windows backend remains unchanged. The source records a
previous Qt FFmpeg source-cycle handle-leak concern; blindly switching the
whole application or adding a fixed audio delay is not an approved fix.
The custom FFmpeg/QAudioSink mixer is not used for this direct combined track.

A fresh bounded resource regression confirms why a global FFmpeg backend
switch is unsafe here. All 20 repeated source cycles produced a valid frame,
with a muted audio output attached. Residual handles after clearing the source
grew from 543 to 564; residual Private Bytes grew from 166.5 to 310.6 MB.
This is a bounded observed growth trend, not a claim to have identified the
exact leaking Qt owner. Recreate mode was not run after the stop criterion was
met. The earlier 350 ms probe produced zero valid frames and is explicitly
not playback-stability evidence. See `qt-valid-lifecycle-report.md`.

Observed host: Windows 10 Pro, build 19045, x64; Media Foundation `mf.dll`
version `10.0.19041.4522`; PySide6/Qt 6.11.1. Do not extrapolate to other
Windows builds, AMD/Intel configurations or external users' machines.

Detailed evidence: `mf-source-reader/content-comparison-report.md`,
`qt-*-content-correlation-fullpts.json`, and
`remux-editlist-ignored/editlist-counterexample-report.md` in the evidence directory.

## 2. Independently proven current replay interval defect

Before this task, video presentation PTS was anchored to selected encoded
frames, but audio QPC selection used the requested save end. A requested end
500 ms after the newest real encoded packet could still satisfy existing
admission guards while placing audio approximately 483 ms early.

`encoded_ring_buffer.cpp` now maps the audio presentation window to the same
unchanged video PTS interval using actual packet PTS/QPC, configured timebase
and the final one-tick duration. It preserves:

- selected packets, video PTS/cutoff and keyframe pre-roll;
- raw source timestamps, requested start/end and media-end evidence;
- Error-015/stale-end, coverage and capture-generation guards;
- static-held versus actual-source frame accounting;
- existing finalizer, gain, continuity policy, encoder/capture code and ABI v4.

Red regression failed before the fix. The reviewed native suite passes
**22,580 checks**. Tests cover 60/120/144 FPS, partial/full history, fractional
boundaries, PTS gaps, non-integer timebases and held/static observations.

An external fixture also calls the actual private production
`CaptureEngine::MuxEncodedClip`, with identical mux source in both binaries
and only the ring mapping differing. This is not a reconstructed FFmpeg mux
command. It does not start capture or open an audio endpoint.

| Known fixture marker | Video PTS, both builds | Baseline audio PTS | Fixed audio PTS |
| --- | ---: | ---: | ---: |
| first flash/tone | 1.000 s | 0.516687 s | 1.000021 s |
| later flash/tone | 4.000 s | 3.516687 s | 4.000021 s |

Thus the measured audio-minus-video difference changes from approximately
**-483.313 ms** to **+0.021 ms** at the detector threshold. Do not interpret
that last number as physical 21-microsecond precision: conservatively allow
one video frame plus an AAC packet (approximately 38 ms). Six generated
baseline/fixed MP4s, including no-stale and partial-history controls, fully
decode with exit 0. See `production-mux-repro/production-mux-repro-report.md`.

This proves this current save-boundary defect and its correction on controlled
encoded data, not that it caused the old Warzone symptom or is field validated.

Separate Release x64 engine:
`C:\Users\Tom\Documents\FTHR-Warzone-AV-Investigation-20261017-01\av-boundary-fix\engine-final\bin\FTHRclips.exe`

SHA-256: `CDD17FFA1C96E847F8ABA0AE99D32A6F68A6433EA3709B51B38BC85F7432BDD3`.
This is a native engine test build, not a newly packaged GUI/installer.

## 3. Stable future game folders

New saves use a stable application identity rather than mutable window-title
text. Minecraft Java version/multiplayer variants map to `Minecraft`; unrelated
Java apps and browser titles are not blindly merged. Manual/automatic/background
selection share the policy, and stale popup entries cannot override the active
window's identity. Correct 64-bit Win32 process-query signatures make executable
identity lookup reliable. No existing clips or directories were moved/renamed.

Grouping plus downstream selection/storage regressions: **113 passed**.

## Validation and open work

- Full reviewed Python suite: **1713 passed, 56 skipped, 3 failed**.
  The three failures remain the pre-existing optional uploader-package hash
  mismatch; no integrity gate/hash was weakened. This is not all-green release
  readiness. See `python-regression-reviewed.log` and XML.
- Native suite: **22,580 checks**, reviewer run exit 0.
- Changed Python Ruff checks: exit 0.
- Shared-memory and response-publication contracts: exit 0, ABI v4 unchanged.
- Source license/package-input gate: 219 checks, exit 0, 3 platform warnings.
  No new legal/asset clearance or Linux package/hardware validation is claimed.
- `git diff --check`: clean. No staging, commit or push.
- New real capture/repeated F9/physical system-loopback sync: **NOT RUN**.
- Physical microphone reference/synchronization: **UNVERIFIED / NOT RUN**.
- A native-vs-combined physical retest through the existing production
  finalizer remains **NOT RUN**; an old combined clip is not a valid substitute.

Next focused boundary is a stable Windows playback compatibility solution that
preserves edit-list timing, requested replay duration and high-FPS performance.
Do not solve it by shifting audio, discarding edits, reclassifying stale frames
as live, or silently re-encoding every saved replay.

**Overall user-visible A/V bug: still open.** The corrected replay-clock defect
and folder grouping are local source changes, not proof that the Windows
playback symptom has been fixed. No new end-user GUI package was published.
