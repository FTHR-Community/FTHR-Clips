from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
from array import array
from types import SimpleNamespace

import pytest

from core.av_sync_diagnostics import (
    TimelineProbeError,
    parse_ffprobe_timeline,
    probe_clip_av_timeline,
)
from core.audio_manifest import (
    AudioManifestError,
    AudioSourceManifestEntry,
    build_manifest,
    manifest_path_for,
    media_sha256,
    write_manifest_atomic,
)
from core.combined_audio_policy import build_combined_audio_filter
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe


def _payload(video_start: float, video_duration: float,
             *audio: tuple[float, float]) -> dict:
    streams = [{
        'index': 0,
        'codec_type': 'video',
        'start_time': str(video_start),
        'duration': str(video_duration),
    }]
    streams.extend({
        'index': index + 1,
        'codec_type': 'audio',
        'start_time': str(start),
        'duration': str(duration),
    } for index, (start, duration) in enumerate(audio))
    return {'streams': streams}


@pytest.mark.parametrize(('payload', 'start_delta_ms', 'end_delta_ms'), [
    (_payload(0, 30, (0, 30)), 0, 0),
    (_payload(0, 30, (0.5, 29.5)), 500, 0),
    (_payload(0, 30, (-0.5, 30.5)), -500, 0),
    (_payload(0, 30, (0, 30.5)), 0, 500),
    (_payload(0, 30, (0, 29.5)), 0, -500),
    (_payload(0, 30, (0, 29)), 0, -1000),
    (_payload(0, 29, (0, 30)), 0, 1000),
])
def test_probe_summary_preserves_synthetic_timeline_offsets(
        payload: dict, start_delta_ms: float, end_delta_ms: float) -> None:
    timeline = parse_ffprobe_timeline(payload)
    assert timeline.av_start_delta_ms == pytest.approx(start_delta_ms)
    assert timeline.av_end_delta_ms == pytest.approx(end_delta_ms)


def test_multiple_audio_streams_use_combined_timeline_envelope() -> None:
    timeline = parse_ffprobe_timeline(_payload(
        0, 30, (0, 30), (0.25, 29.5)))
    assert timeline.audio_stream_count == 2
    assert timeline.audio.start_time == pytest.approx(0)
    assert timeline.audio.end_time == pytest.approx(30)
    fields = timeline.diagnostic_fields()
    assert fields['timeline_time_unit'] == 'seconds'
    assert fields['audio_stream_timelines'] == [
        {'stream_index': 1, 'start_time': 0.0,
         'duration': 30.0, 'end_time': 30.0},
        {'stream_index': 2, 'start_time': 0.25,
         'duration': 29.5, 'end_time': 29.75},
    ]
    assert fields['audio_stream_timelines_truncated'] == 0


def test_per_stream_timeline_diagnostics_are_bounded() -> None:
    timeline = parse_ffprobe_timeline(_payload(
        0, 30, *((index / 100, 30) for index in range(20))))
    fields = timeline.diagnostic_fields()

    assert len(fields['audio_stream_timelines']) == 16
    assert fields['audio_stream_timelines_truncated'] == 4


def test_incomplete_probe_evidence_is_rejected() -> None:
    with pytest.raises(TimelineProbeError):
        parse_ffprobe_timeline({'streams': [{'codec_type': 'video'}]})


def _tools() -> tuple[str, str]:
    try:
        return get_ffmpeg_exe(), get_ffprobe_exe()
    except FFmpegUnavailable as error:
        pytest.skip(str(error))


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode != 0:
        detail = result.stderr.decode(errors='replace')
        if 'Unknown encoder' in detail or 'No such filter' in detail:
            pytest.skip(detail.splitlines()[-1])
        pytest.fail(detail[-2000:])
    return result


def _packet_times(ffprobe: str, media: Path, selector: str) -> list[float]:
    result = _run([
        ffprobe, '-v', 'error', '-select_streams', selector,
        '-show_packets', '-show_entries', 'packet=pts_time', '-of', 'json',
        str(media),
    ])
    payload = json.loads(result.stdout)
    return [float(packet['pts_time']) for packet in payload['packets']]


def _frame_times(ffprobe: str, media: Path, selector: str) -> list[float]:
    result = _run([
        ffprobe, '-v', 'error', '-select_streams', selector,
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
        '-of', 'json', str(media),
    ])
    payload = json.loads(result.stdout)
    return [float(frame['best_effort_timestamp_time'])
            for frame in payload['frames']]


def _marker_centers(pcm: Path, frequencies: tuple[float, ...],
                    *, sample_rate: int = 48000,
                    threshold: float = 0.05) -> dict[float, list[float]]:
    samples = array('f')
    with pcm.open('rb') as stream:
        samples.fromfile(stream, pcm.stat().st_size // samples.itemsize)
    block_size = sample_rate // 100  # 10 ms; tolerant of AAC framing.
    kernels = {
        frequency: tuple(
            (math.cos(2.0 * math.pi * frequency * sample_index / sample_rate),
             -math.sin(2.0 * math.pi * frequency * sample_index / sample_rate))
            for sample_index in range(block_size))
        for frequency in frequencies
    }
    centers: dict[float, list[float]] = {}
    for frequency in frequencies:
        values: list[float] = []
        kernel = kernels[frequency]
        for offset in range(0, len(samples) - block_size + 1, block_size):
            block = samples[offset:offset + block_size]
            real = sum(sample * cosine for sample, (cosine, _sine)
                       in zip(block, kernel, strict=True))
            imag = sum(sample * sine for sample, (_cosine, sine)
                       in zip(block, kernel, strict=True))
            values.append(math.hypot(real, imag) / block_size)
        active = [index for index, value in enumerate(values)
                  if value > threshold]
        grouped: list[list[int]] = []
        for index in active:
            if not grouped or index != grouped[-1][-1] + 1:
                grouped.append([index])
            else:
                grouped[-1].append(index)
        centers[frequency] = [
            (group[0] + group[-1] + 1) * block_size / (2 * sample_rate)
            for group in grouped]
    return centers


def _decode_audio_pcm(ffmpeg: str, media: Path, audio_ordinal: int,
                      output: Path, *, sample_rate: int = 48000) -> None:
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(media),
        '-map', f'0:a:{audio_ordinal}', '-f', 'f32le', '-ac', '1',
        '-ar', str(sample_rate), str(output),
    ])


def _audio_marker_expression(frequency: float, markers: tuple[float, ...],
                             sample_rate: int, duration: float) -> str:
    windows = '+'.join(
        f'between(t\\,{marker}\\,{marker + 0.05})'
        for marker in markers)
    return (
        f'aevalsrc=0.7*sin(2*PI*{frequency}*t)*if({windows}\\,1\\,0):'
        f's={sample_rate}:d={duration}')


def _video_marker_expression(frame_rate: int, markers: tuple[float, ...],
                             duration: float) -> str:
    windows = '+'.join(
        f'between(t\\,{marker}\\,{marker + 0.05})'
        for marker in markers)
    return (
        f'color=c=black:s=64x48:r={frame_rate}:d={duration},'
        'drawbox=x=0:y=0:w=64:h=48:color=white:t=fill:'
        f'enable={windows}')


def _render_source_pcm(ffmpeg: str, expression: str, output: Path) -> None:
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', expression, '-f', 'f32le', '-ac', '1',
        '-ar', '48000', str(output),
    ])


def _format_start_time(ffprobe: str, media: Path) -> float:
    payload = json.loads(_run([
        ffprobe, '-v', 'error', '-show_entries', 'format=start_time',
        '-of', 'json', str(media),
    ]).stdout)
    value = payload.get('format', {}).get('start_time')
    return 0.0 if value in (None, 'N/A') else float(value)


def _video_flash_centers(ffmpeg: str, ffprobe: str, media: Path,
                         *, threshold: int = 160) -> list[float]:
    metadata = json.loads(_run([
        ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json',
        str(media),
    ]).stdout)
    timestamps = [float(frame['best_effort_timestamp_time'])
                  for frame in metadata['frames']]
    decoded = _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(media),
        '-map', '0:v:0', '-vf', 'scale=1:1,format=gray',
        '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'gray',
        'pipe:1',
    ]).stdout
    assert len(decoded) == len(timestamps)
    active = [index for index, value in enumerate(decoded)
              if value >= threshold]
    groups: list[list[int]] = []
    for index in active:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    return [
        sum(timestamps[index] for index in group) / len(group)
        for group in groups
    ]


def test_production_combined_filter_preserves_offset_marker_positions(
        tmp_path: Path) -> None:
    """Decoded markers catch amix's otherwise invisible input-offset loss."""
    ffmpeg, ffprobe = _tools()
    native = tmp_path / 'native-markers.mp4'
    combined = tmp_path / 'combined-markers.mp4'
    legacy_combined = tmp_path / 'legacy-combined-markers.mp4'
    legacy_pcm = tmp_path / 'legacy-combined-markers.f32'
    pcm = tmp_path / 'combined-markers.f32'
    system = (
        'aevalsrc=0.7*sin(2*PI*440*t)*'
        'if(between(t\\,0.4\\,0.45)+between(t\\,1.8\\,1.85)+'
        'between(t\\,3.0\\,3.05)\\,1\\,0):s=48000:d=4')
    microphone = (
        'aevalsrc=0.7*sin(2*PI*880*t)*'
        'if(between(t\\,0.8\\,0.85)+between(t\\,1.8\\,1.85)+'
        'between(t\\,2.8\\,2.85)\\,1\\,0):s=48000:d=3.2')
    video = (
        'color=c=black:s=64x48:r=60:d=4,drawbox=x=0:y=0:w=64:h=48:'
        'color=white:t=fill:enable=between(t\\,0.4\\,0.45)+'
        'between(t\\,1.8\\,1.85)+between(t\\,3.0\\,3.05)')
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', video,
        '-f', 'lavfi', '-i', system,
        '-itsoffset', '0.5', '-f', 'lavfi', '-i', microphone,
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'mpeg4', '-q:v', '5', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-shortest', str(native),
    ])
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(native),
        '-filter_complex', build_combined_audio_filter((0, 1)),
        '-map', '0:v?', '-map', '[aout]', '-map_metadata', '0',
        '-map_chapters', '0', '-movflags', 'use_metadata_tags',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest',
        str(combined),
    ])
    # Production-equivalent red control for the old graph.  It deliberately
    # omits per-input timestamp normalization, so the late microphone marker
    # is pulled to decoded time zero while the corrected helper preserves it.
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(native),
        '-filter_complex',
        '[0:a:0][0:a:1]amix=inputs=2:normalize=0:duration=longest:'
        'dropout_transition=0,alimiter=limit=0.98:level=disabled[aout]',
        '-map', '0:v?', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac',
        '-b:a', '192k', '-shortest', str(legacy_combined),
    ])
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(combined),
        '-map', '0:a:0', '-f', 'f32le', '-ac', '1', '-ar', '48000', str(pcm),
    ])
    _decode_audio_pcm(ffmpeg, legacy_combined, 0, legacy_pcm)

    # Independently measure the native streams before final mixing.  FFmpeg's
    # raw decode starts each selected stream at zero, so add the stream's
    # probed origin back to the locally measured marker center.
    before = probe_clip_av_timeline(ffprobe, str(native))
    native_system_pcm = tmp_path / 'native-system.f32'
    native_mic_pcm = tmp_path / 'native-mic.f32'
    _decode_audio_pcm(ffmpeg, native, 0, native_system_pcm)
    _decode_audio_pcm(ffmpeg, native, 1, native_mic_pcm)
    native_system = _marker_centers(native_system_pcm, (440.0,))[440.0]
    native_mic = _marker_centers(native_mic_pcm, (880.0,))[880.0]
    assert native_system == pytest.approx([0.425, 1.825, 3.025], abs=0.02)
    mic_origin = before.audio_streams[1].start_time
    assert [center + mic_origin for center in native_mic] == pytest.approx(
        [1.325, 2.325, 3.325], abs=0.02)

    legacy_markers = _marker_centers(legacy_pcm, (440.0, 880.0))
    assert legacy_markers[880.0][0] < native_mic[0] + mic_origin - 0.2

    markers = _marker_centers(pcm, (440.0, 880.0))
    assert markers[440.0] == pytest.approx([0.425, 1.825, 3.025], abs=0.02)
    # The microphone source starts 0.5 s later; this is the regression that
    # literal [a0][a1]amix loses by starting both inputs at decoded zero.
    assert markers[880.0] == pytest.approx([1.325, 2.325, 3.325], abs=0.02)
    assert _video_flash_centers(ffmpeg, ffprobe, native) == pytest.approx(
        [0.425, 1.825, 3.025], abs=1 / 60 + 0.002)


@pytest.mark.parametrize(
    'case',
    [
        {'name': 'aligned-60-48', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.0, 'mic_offset': 0.0, 'duration': 1.0,
         'video_offset': 0.0, 'common_origin': 0.0, 'flash':
         (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
         'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'system-late-60-48', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.2, 'mic_offset': 0.0, 'duration': 1.0,
         'video_offset': 0.0, 'common_origin': 0.0, 'flash':
         (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
         'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'mic-late-60-48', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.0, 'mic_offset': 0.2, 'duration': 1.0,
         'video_offset': 0.0, 'common_origin': 0.0, 'flash':
         (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
         'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'both-late-120-48', 'video_rate': 120, 'audio_rate': 48000,
         'system_offset': 0.2, 'mic_offset': 0.2, 'duration': 1.0,
         'video_offset': 0.0, 'common_origin': 0.0, 'flash':
         (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
         'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'audio-before-video-144-48', 'video_rate': 144,
         'audio_rate': 48000, 'system_offset': 0.0, 'mic_offset': -0.2,
          'duration': 1.0, 'video_offset': 0.0, 'common_origin': 0.0,
          'flash': (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
          'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'common-origin-10s-60-44', 'video_rate': 60,
          'audio_rate': 44100, 'system_offset': 0.0, 'mic_offset': 0.2,
          'duration': 1.0, 'video_offset': 0.0, 'common_origin': 10.0,
              'copyts': True, 'flash': (10.225, 10.475, 10.725),
          'system_markers': (0.2, 0.45, 0.7), 'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'cross-rate-60-44', 'video_rate': 60, 'audio_rate': 44100,
         'system_offset': 0.0, 'mic_offset': 0.2, 'duration': 1.0,
          'video_offset': 0.0, 'common_origin': 0.0,
          'flash': (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
          'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'cross-rate-144-96', 'video_rate': 144,
         'audio_rate': 96000, 'system_offset': 0.0, 'mic_offset': 0.2,
         'duration': 1.0, 'video_offset': 0.0, 'common_origin': 0.0,
                 'flash': (0.225, 0.475, 0.725), 'system_markers': (0.2, 0.45, 0.7),
         'mic_markers': (0.3, 0.55, 0.75)},
        {'name': 'negative-preroll-bframes-120', 'video_rate': 120,
         'audio_rate': 48000, 'system_offset': 0.0, 'mic_offset': 0.2,
          'duration': 1.0, 'video_offset': -0.1, 'common_origin': 0.0,
                  'copyts': True, 'flash': (0.125, 0.375, 0.625),
              'system_markers': (0.2, 0.45, 0.7), 'mic_markers': (0.25, 0.45, 0.6)},
        {'name': 'duration-15-60', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.0, 'mic_offset': 0.2, 'duration': 15.0,
         'video_offset': 0.0, 'common_origin': 0.0,
         'flash': (0.225, 7.525, 14.225), 'system_markers': (0.2, 7.5, 14.2),
         'mic_markers': (0.35, 7.6, 14.4)},
        {'name': 'duration-30-60', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.0, 'mic_offset': 0.2, 'duration': 30.0,
         'video_offset': 0.0, 'common_origin': 0.0,
         'flash': (0.225, 15.025, 29.225), 'system_markers': (0.2, 15.0, 29.2),
         'mic_markers': (0.35, 15.1, 29.4)},
        {'name': 'duration-60-60', 'video_rate': 60, 'audio_rate': 48000,
         'system_offset': 0.0, 'mic_offset': 0.2, 'duration': 60.0,
         'video_offset': 0.0, 'common_origin': 0.0,
         'flash': (0.225, 30.025, 59.225), 'system_markers': (0.2, 30.0, 59.2),
         'mic_markers': (0.35, 30.1, 59.4)},
    ],
    ids=lambda case: case['name'],
)
def test_combined_filter_timing_matrix_preserves_video_frame_reference(
        tmp_path: Path, case: dict) -> None:
    """Exercise representative rates/origins without a full-size media run."""
    ffmpeg, ffprobe = _tools()
    native = tmp_path / f"{case['name']}-native.mp4"
    combined = tmp_path / f"{case['name']}-combined.mp4"
    duration = case['duration']
    video_markers = case['flash']
    video = _video_marker_expression(
        case['video_rate'],
        tuple(marker - case['common_origin'] - case['video_offset'] - 0.025
              for marker in video_markers),
        duration)
    system_markers = case['system_markers']
    mic_markers = case['mic_markers']
    system = _audio_marker_expression(
        440.0, system_markers, case['audio_rate'], duration)
    microphone = _audio_marker_expression(
        880.0, mic_markers, case['audio_rate'], duration)
    common_origin = case['common_origin']
    video_offset = common_origin + case['video_offset']
    system_offset = common_origin + case['system_offset']
    mic_offset = common_origin + case['mic_offset']
    video_input = []
    if video_offset:
        video_input.extend(['-itsoffset', str(video_offset)])
    video_input.extend(['-f', 'lavfi', '-i', video])
    system_input = []
    if system_offset:
        system_input.extend(['-itsoffset', str(system_offset)])
    system_input.extend(['-f', 'lavfi', '-i', system])
    mic_input = []
    if mic_offset:
        mic_input.extend(['-itsoffset', str(mic_offset)])
    mic_input.extend(['-f', 'lavfi', '-i', microphone])
    input_flags = ['-copyts'] if case.get('copyts', False) else []
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        *input_flags,
        *video_input, *system_input, *mic_input,
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-fps_mode', 'passthrough', '-c:v', 'mpeg4', '-bf', '2',
        '-q:v', '10', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
        '-shortest', '-avoid_negative_ts', 'disabled', str(native),
    ])
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(native),
        '-filter_complex', build_combined_audio_filter((0, 1)),
        '-map', '0:v?', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac',
        '-b:a', '96k', '-shortest', str(combined),
    ])

    before = probe_clip_av_timeline(ffprobe, str(native))
    after = probe_clip_av_timeline(ffprobe, str(combined))
    format_start = _format_start_time(ffprobe, native)
    assert math.isfinite(format_start)

    # Independently render source markers and compare their measured centers to
    # each native decoded stream.  Stream origins are added back explicitly;
    # this avoids double-counting packet skips when a decoder has preroll.
    source_system_pcm = tmp_path / f"{case['name']}-source-system.f32"
    source_mic_pcm = tmp_path / f"{case['name']}-source-mic.f32"
    native_system_pcm = tmp_path / f"{case['name']}-native-system.f32"
    native_mic_pcm = tmp_path / f"{case['name']}-native-mic.f32"
    combined_pcm = tmp_path / f"{case['name']}-combined.f32"
    _render_source_pcm(ffmpeg, system, source_system_pcm)
    _render_source_pcm(ffmpeg, microphone, source_mic_pcm)
    _decode_audio_pcm(ffmpeg, native, 0, native_system_pcm)
    _decode_audio_pcm(ffmpeg, native, 1, native_mic_pcm)
    _decode_audio_pcm(ffmpeg, combined, 0, combined_pcm)
    source_system = _marker_centers(source_system_pcm, (440.0,))[440.0]
    source_mic = _marker_centers(source_mic_pcm, (880.0,))[880.0]
    native_system = _marker_centers(native_system_pcm, (440.0,))[440.0]
    native_mic = _marker_centers(native_mic_pcm, (880.0,))[880.0]
    native_system_absolute = [
        center + before.audio_streams[0].start_time for center in native_system]
    native_mic_absolute = [
        center + before.audio_streams[1].start_time for center in native_mic]
    expected_system_absolute = [
        center + system_offset for center in source_system]
    expected_mic_absolute = [center + mic_offset for center in source_mic]
    assert native_system_absolute == pytest.approx(
        expected_system_absolute, abs=0.03)
    assert native_mic_absolute == pytest.approx(expected_mic_absolute, abs=0.03)
    # For negative video/B-frame preroll, compare against the container's
    # demux origin rather than packet zero.  The same mapping must hold for
    # PCM marker PTS; otherwise packet skipping would be counted twice.
    assert [timestamp - format_start for timestamp in native_system_absolute] == pytest.approx(
        [timestamp - format_start for timestamp in expected_system_absolute],
        abs=0.03)
    assert [timestamp - format_start for timestamp in native_mic_absolute] == pytest.approx(
        [timestamp - format_start for timestamp in expected_mic_absolute],
        abs=0.03)

    # This reference is measured from decoded RGB brightness and frame PTS,
    # never inferred from stream metadata.  One video frame plus two ms is a
    # tighter bound than the AAC marker tolerance used by the audio checks.
    native_flash = _video_flash_centers(ffmpeg, ffprobe, native)
    combined_flash = _video_flash_centers(ffmpeg, ffprobe, combined)
    assert native_flash == pytest.approx(
        list(video_markers), abs=1 / case['video_rate'] + 0.002)
    expected_combined_flash = [
        timestamp - before.video.start_time + after.video.start_time
        for timestamp in native_flash]
    assert combined_flash == pytest.approx(
        expected_combined_flash, abs=1 / case['video_rate'] + 0.002)
    combined_markers = _marker_centers(combined_pcm, (440.0, 880.0))
    expected_final_system = [
        absolute - before.video.start_time + after.audio.start_time
        for absolute in native_system_absolute]
    expected_final_mic = [
        absolute - before.video.start_time + after.audio.start_time
        for absolute in native_mic_absolute]
    assert combined_markers[440.0] == pytest.approx(
        expected_final_system, abs=0.03)
    assert combined_markers[880.0] == pytest.approx(
        expected_final_mic, abs=0.03)


def test_native_worker_publishes_combined_clip_and_logs_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Exercise the actual finalizer through its publish/log boundary."""
    ffmpeg, ffprobe = _tools()
    native = tmp_path / 'worker-native.mp4'
    system = _audio_marker_expression(440.0, (0.2, 0.5), 48000, 1.0)
    microphone = _audio_marker_expression(880.0, (0.4, 0.7), 48000, 0.8)
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=64x48:r=60:d=1',
        '-f', 'lavfi', '-i', system,
        '-itsoffset', '0.2', '-f', 'lavfi', '-i',
        microphone,
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'mpeg4', '-bf', '2', '-q:v', '10', '-c:a', 'aac',
        '-shortest', str(native),
    ])
    warnings: list[tuple[str, str]] = []
    host = SimpleNamespace(
        _record_finalization_warning=lambda path, message:
            warnings.append((path, message)),
        _apply_crop=lambda *_args: None,
        _apply_image_overlay=lambda *_args: None,
        _apply_keyboard_overlay=lambda *_args: None,
        _apply_camera_overlay=lambda *_args: None,
    )
    from main import MainWindow

    MainWindow._combine_native_audio_worker(
        host, str(native), 1, 0.0, None, None)

    assert warnings == []
    assert native.is_file()
    assert probe_clip_av_timeline(ffprobe, str(native)).audio_stream_count == 1
    assert 'Combined 2 native stream(s)' in capsys.readouterr().out
    combined_pcm = tmp_path / 'worker-combined.f32'
    _decode_audio_pcm(ffmpeg, native, 0, combined_pcm)
    markers = _marker_centers(combined_pcm, (440.0, 880.0))
    assert markers[440.0] == pytest.approx([0.225, 0.525], abs=0.03)
    assert markers[880.0] == pytest.approx([0.625, 0.925], abs=0.03)


def _make_retest_native_clip(path: Path, ffmpeg: str) -> None:
    system = _audio_marker_expression(440.0, (0.4, 1.8, 3.0), 48000, 4.0)
    microphone = _audio_marker_expression(
        880.0, (0.8, 1.8, 2.8), 48000, 3.2)
    video = _video_marker_expression(60, (0.4, 1.8, 3.0), 4.0)
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', video,
        '-f', 'lavfi', '-i', system,
        '-itsoffset', '0.5', '-f', 'lavfi', '-i', microphone,
        '-f', 'lavfi', '-i', 'sine=frequency=1320:sample_rate=48000:duration=4',
        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:a',
        '-metadata:s:a:0', 'title=System Audio',
        '-metadata:s:a:1', 'title=Microphone',
        '-metadata:s:a:2', 'title=Auxiliary',
        '-c:v', 'mpeg4', '-bf', '2', '-q:v', '10', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-shortest', str(path),
    ])


def _write_retest_manifest(native: Path, ffprobe: str) -> dict:
    streams = json.loads(_run([
        ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(native),
    ]).stdout)['streams']
    audio_indexes = [stream['index'] for stream in streams
                     if stream.get('codec_type') == 'audio']
    sources = [
        AudioSourceManifestEntry(
            source_uuid='12345678-1234-5678-1234-567812345678',
            stream_index=audio_indexes[0], source_type='system',
            display_name='System Audio', persistent_identity='test-system',
            icon_reference=None, first_active_100ns=0,
            last_active_100ns=40_000_000, sample_rate=48000, channels=1,
            sample_format='fltp'),
        AudioSourceManifestEntry(
            source_uuid='87654321-4321-8765-4321-876543218765',
            stream_index=audio_indexes[1], source_type='microphone',
            display_name='Microphone', persistent_identity='test-microphone',
            icon_reference=None, first_active_100ns=5_000_000,
            last_active_100ns=37_000_000, sample_rate=48000, channels=1,
            sample_format='fltp'),
    ]
    manifest = build_manifest(
        media_path=native, transaction_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        sources=sources)
    write_manifest_atomic(manifest, native)
    return manifest


def test_retest_runner_finalizes_copy_and_preserves_source_track_markers(
        tmp_path: Path) -> None:
    """Retest evidence comes from production finalization of the same native file."""
    ffmpeg, ffprobe = _tools()
    native = tmp_path / 'native-retest.mp4'
    output_dir = tmp_path / 'retest-output'
    _make_retest_native_clip(native, ffmpeg)
    original_manifest = _write_retest_manifest(native, ffprobe)
    original_sha = media_sha256(native)
    sidecar = manifest_path_for(native)
    original_sidecar_bytes = sidecar.read_bytes()
    original_streams = json.loads(_run([
        ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(native),
    ]).stdout)['streams']
    before = probe_clip_av_timeline(ffprobe, str(native))
    native_system_pcm = tmp_path / 'retest-native-system.f32'
    native_mic_pcm = tmp_path / 'retest-native-mic.f32'
    _decode_audio_pcm(ffmpeg, native, 0, native_system_pcm)
    _decode_audio_pcm(ffmpeg, native, 1, native_mic_pcm)
    native_system = _marker_centers(native_system_pcm, (440.0,))[440.0]
    native_mic = _marker_centers(native_mic_pcm, (880.0,))[880.0]
    system_absolute = [
        center + before.audio_streams[0].start_time for center in native_system]
    mic_absolute = [
        center + before.audio_streams[1].start_time for center in native_mic]
    assert system_absolute == pytest.approx([0.425, 1.825, 3.025], abs=0.03)
    assert mic_absolute == pytest.approx([1.325, 2.325, 3.325], abs=0.03)
    native_video = _video_flash_centers(ffmpeg, ffprobe, native)

    from finalize_native_audio_retest import run_retest

    evidence_path = run_retest(native, output_dir)

    combined = output_dir / native.name
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    after = probe_clip_av_timeline(ffprobe, str(combined))
    assert evidence['status'] == 'FINALIZED'
    assert evidence['finalizer_execution_status'] == 'PASS'
    assert evidence['physical_validation'] == 'NOT RUN'
    assert evidence['content_sync_validation'] == 'NOT RUN'
    assert evidence['source_manifest'] == original_manifest
    assert evidence['source_streams'] == original_streams
    assert evidence['source_sha256'] == original_sha
    assert evidence['native_copy_sha256_before_finalizer'] == original_sha
    assert evidence['native_copy_sidecar_sha256_before_finalizer'] == media_sha256(sidecar)
    assert media_sha256(native) == original_sha
    assert sidecar.read_bytes() == original_sidecar_bytes
    assert combined.is_file()
    assert not manifest_path_for(combined).exists()
    assert after.audio_stream_count == 1
    final_audio_stream = next(
        stream for stream in evidence['final_streams']
        if stream.get('codec_type') == 'audio')
    assert any(final_audio_stream.get('tags', {}).get(field) == 'Combined Audio'
               for field in ('title', 'name', 'handler_name'))
    assert evidence['warnings'] == []
    assert evidence['overlay_hooks']['disabled'] is True
    assert set(evidence['production_source_sha256']) >= {
        'FTHR_UI/main.py', 'FTHR_UI/core/combined_audio_policy.py',
        'FTHR_UI/core/audio_manifest.py',
    }

    tracks = evidence['source_audio_tracks']
    assert [(track['audio_ordinal'], track['source_type'], track['provenance'])
            for track in tracks] == [
        (0, 'system', 'verified_manifest'),
        (1, 'microphone', 'verified_manifest'),
        (2, 'unknown', 'unknown'),
    ]
    assert [track['container_index'] for track in tracks] == [1, 2, 3]
    assert [track['selected_for_finalization'] for track in tracks] == [
        True, True, False]

    combined_pcm = tmp_path / 'retest-combined.f32'
    _decode_audio_pcm(ffmpeg, combined, 0, combined_pcm)
    combined_markers = _marker_centers(
        combined_pcm, (440.0, 880.0, 1320.0))
    expected_audio = [
        absolute - before.video.start_time + after.audio.start_time
        for absolute in system_absolute]
    expected_mic = [
        absolute - before.video.start_time + after.audio.start_time
        for absolute in mic_absolute]
    assert combined_markers[440.0] == pytest.approx(expected_audio, abs=0.03)
    assert combined_markers[880.0] == pytest.approx(expected_mic, abs=0.03)
    assert combined_markers[1320.0] == []
    combined_video = _video_flash_centers(ffmpeg, ffprobe, combined)
    expected_video = [
        timestamp - before.video.start_time + after.video.start_time
        for timestamp in native_video]
    assert combined_video == pytest.approx(expected_video, abs=1 / 60 + 0.002)


def test_retest_runner_keeps_unlabeled_source_unknown() -> None:
    from finalize_native_audio_retest import identify_audio_sources

    tracks = identify_audio_sources([
        {'index': 4, 'codec_type': 'audio',
         'tags': {'title': 'System Audio'}},
        {'index': 9, 'codec_type': 'audio', 'tags': {'title': 'Mystery Track'}},
    ], None)

    assert [(track['container_index'], track['audio_ordinal'],
             track['source_type'], track['provenance']) for track in tracks] == [
        (4, 0, 'system', 'explicit_label'),
        (9, 1, 'unknown', 'unknown'),
    ]


def test_retest_runner_refuses_destination_that_resolves_over_source(
        tmp_path: Path) -> None:
    ffmpeg, _ffprobe = _tools()
    native = tmp_path / 'native.mp4'
    _make_retest_native_clip(native, ffmpeg)
    aliased_source_dir = tmp_path / 'missing' / '..'

    from finalize_native_audio_retest import run_retest

    with pytest.raises(ValueError, match='contains the input'):
        run_retest(native.parent / '.' / native.name, aliased_source_dir)


def test_retest_runner_refuses_existing_destination_without_modifying_it(
        tmp_path: Path) -> None:
    ffmpeg, _ffprobe = _tools()
    native = tmp_path / 'native.mp4'
    _make_retest_native_clip(native, ffmpeg)
    output_dir = tmp_path / 'existing-output'
    output_dir.mkdir()
    sentinel = output_dir / 'keep.txt'
    sentinel.write_text('user data', encoding='utf-8')

    from finalize_native_audio_retest import run_retest

    with pytest.raises(FileExistsError, match='must not exist'):
        run_retest(native, output_dir)
    assert sentinel.read_text(encoding='utf-8') == 'user data'
    assert not (output_dir / native.name).exists()


def test_retest_runner_rejects_invalid_present_manifest_before_output_creation(
        tmp_path: Path) -> None:
    native = tmp_path / 'not-a-valid-media.mp4'
    native.write_bytes(b'not an mp4')
    sidecar = manifest_path_for(native)
    sidecar.write_text('{broken json', encoding='utf-8')
    output_dir = tmp_path / 'should-not-be-created'

    from finalize_native_audio_retest import run_retest

    with pytest.raises(AudioManifestError, match='invalid present audio manifest'):
        run_retest(native, output_dir)
    assert not output_dir.exists()
    assert native.read_bytes() == b'not an mp4'
    assert sidecar.read_text(encoding='utf-8') == '{broken json'


def test_retest_runner_rejects_hash_valid_manifest_with_missing_stream(
        tmp_path: Path) -> None:
    ffmpeg, _ffprobe = _tools()
    native = tmp_path / 'native.mp4'
    _make_retest_native_clip(native, ffmpeg)
    manifest = build_manifest(
        media_path=native,
        transaction_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        sources=[AudioSourceManifestEntry(
            source_uuid='cccccccc-cccc-4ccc-8ccc-cccccccccccc',
            stream_index=77, source_type='system', display_name='System Audio',
            persistent_identity='test-system', icon_reference=None,
            first_active_100ns=0, last_active_100ns=40_000_000,
            sample_rate=48000, channels=1, sample_format='fltp')])
    write_manifest_atomic(manifest, native)
    output_dir = tmp_path / 'should-not-be-created'
    sidecar = manifest_path_for(native)
    sidecar_sha = media_sha256(sidecar)

    from finalize_native_audio_retest import run_retest

    with pytest.raises(AudioManifestError, match='non-audio stream'):
        run_retest(native, output_dir)
    assert not output_dir.exists()
    assert media_sha256(native) == manifest['media_sha256']
    assert media_sha256(sidecar) == sidecar_sha


def test_retest_runner_records_warning_as_failure_and_preserves_originals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ffmpeg, ffprobe = _tools()
    native = tmp_path / 'native.mp4'
    _make_retest_native_clip(native, ffmpeg)
    _write_retest_manifest(native, ffprobe)
    source_sha = media_sha256(native)
    sidecar = manifest_path_for(native)
    sidecar_sha = media_sha256(sidecar)
    output_dir = tmp_path / 'warning-output'

    from main import MainWindow
    from finalize_native_audio_retest import run_retest

    def warn(host, clip_path, *_args):
        host._record_finalization_warning(clip_path, 'test finalizer warning')

    monkeypatch.setattr(MainWindow, '_combine_native_audio_worker', warn)
    with pytest.raises(RuntimeError, match='reported a warning'):
        run_retest(native, output_dir)

    evidence = json.loads(
        (output_dir / 'native_audio_retest_evidence.json').read_text(
            encoding='utf-8'))
    assert evidence['status'] == 'FINALIZER_FAILED'
    assert evidence['finalizer_execution_status'] == 'FAIL'
    assert evidence['warnings'] == ['test finalizer warning']
    assert evidence['originals_unchanged'] is True
    assert media_sha256(native) == source_sha
    assert media_sha256(sidecar) == sidecar_sha


def test_aac_mux_and_final_two_track_mix_preserve_av_timing(
        tmp_path: Path) -> None:
    ffmpeg, ffprobe = _tools()
    native = tmp_path / 'native.mp4'
    combined = tmp_path / 'combined.mp4'
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=60:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3',
        '-itsoffset', '0.5', '-f', 'lavfi', '-i',
        'sine=frequency=880:sample_rate=48000:duration=2.5',
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'mpeg4', '-q:v', '5', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', str(native),
    ])
    before = probe_clip_av_timeline(ffprobe, str(native))
    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(native),
        '-filter_complex', build_combined_audio_filter((0, 1)),
        '-map', '0:v?', '-map', '[aout]', '-map_metadata', '0',
        '-map_chapters', '0', '-movflags', 'use_metadata_tags',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest',
        str(combined),
    ])
    after = probe_clip_av_timeline(ffprobe, str(combined))

    # Stream metadata and packet/frame timestamps are checked independently.
    assert before.audio_stream_count == 2
    assert after.audio_stream_count == 1
    assert after.av_start_delta_ms == pytest.approx(
        before.av_start_delta_ms, abs=1.0)
    assert after.av_end_delta_ms == pytest.approx(
        before.av_end_delta_ms, abs=25.0)
    audio_packets = _packet_times(ffprobe, combined, 'a:0')
    video_packets = _packet_times(ffprobe, combined, 'v:0')
    audio_frames = _frame_times(ffprobe, combined, 'a:0')
    video_frames = _frame_times(ffprobe, combined, 'v:0')
    assert audio_packets[0] < 0  # AAC encoder priming packet.
    assert video_packets[0] == pytest.approx(0, abs=1e-6)
    assert audio_frames[0] == pytest.approx(video_frames[0], abs=1e-6)
    assert audio_frames[-1] - video_frames[-1] == pytest.approx(0, abs=0.025)
