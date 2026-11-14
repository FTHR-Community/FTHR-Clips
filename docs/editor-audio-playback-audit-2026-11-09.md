# Editor audio playback audit — 2026-11-09

## Behavior

An eligible clip with one embedded, unprocessed audio source now uses QMediaPlayer for both video and audio. No custom audio decoder, PCM queue, QAudioSink, or editor drift correction is created. Eligibility comes from the clip's actual tracks, not the current capture-device settings. Source/master volume, mute, removal, and undo remain supported.

Multiple tracks, imported audio, source processing, ambiguous backup tracks, and pitch-preserving speed changes retain the custom mixer. Returning an eligible clip to normal speed returns it to direct playback. The dry direct preview plays container samples without the mix limiter; the existing limiter still applies to mixed previews and exports.

## Fixed synchronization and resource issues

- Device-buffer latency was subtracted in wall-clock milliseconds from a source-time position. It now accounts for playback speed.
- Natural audio EOF incorrectly stopped transport state, preventing audio from restarting after a backward seek while video kept playing. EOF and transport state are now separate.
- Short audio could trigger repeated drift corrections after it ended. Natural EOF now suppresses those corrections.
- Pausing left the 20 ms audio maintenance timer running. Pause now stops it.
- Every native seek initialized source processing twice. It now initializes once.
- Native streams marked finished could receive more packets. Finished decoders are now excluded from packet dispatch.

## Validation

95 editor integration tests passed across direct playback, playback performance, editor switching, playback proxies, audio export, and project-state contracts. The implementation worker also checked speed, lifecycle, compatibility, and timeline tests.

An isolated Release x64 native mixer build passed 62 audio quality, mix, and timestamp tests with the new DLL explicitly selected. Two tests requiring separate capture-tool executables were skipped. Checks covered late audio, unequal timestamps, negative AAC preroll, seeking, and short audio followed by backward seeks. This work did not rebuild the installer or replace packaged native binaries.

A real Windows Qt playback smoke used a generated 20-second 1280×720/30 fps H.264/AAC MP4. Direct playback became ready in 0.506 seconds, delivered about 29.96 fps, and reported no media errors or timestamp regressions during continuous playback. Three pause/seek/resume cycles passed; the next moving frame arrived about 30–48 ms after resume. These are one-machine synthetic measurements, not guarantees for recorded clips.

The six-second process-CPU samples were 17.96% of one core before the direct path and 20.82% after it. This short check does not establish a CPU saving. It does verify removal of the separate audio pipeline. Reports and the fixture are under `build/editor-audio-check/`.

## Remaining limits

The custom mixer estimates audible position from device-buffer occupancy. It corrects drift above 120 ms following a 750 ms seek-recovery guard; this is not sample-accurate synchronization. Hardware-output latency and long-session behavior still need listening/loopback measurements on representative devices. Direct playback relies on the media backend's container synchronization and cannot repair timestamps already recorded incorrectly.

Very long internal timestamp gaps in imported or untagged audio still depend on native packet read-ahead limits; this audit did not redesign buffering for pathological imported files.
