from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess

import pytest

from core.combined_audio_policy import (
    AudioStreamDescriptor,
    build_combined_audio_filter,
    descriptors_from_ffprobe_streams,
    select_combined_audio_streams,
)


def _stream(index: int, audio_index: int, title: str | None = None):
    return AudioStreamDescriptor(
        container_index=index, audio_index=audio_index, title=title)


def test_verified_manifest_selects_only_system_and_microphone_streams():
    manifest = {'sources': [
        {'stream_index': 1, 'source_type': 'system', 'display_name': 'Default Mix'},
        {'stream_index': 2, 'source_type': 'application', 'display_name': 'Game'},
        {'stream_index': 3, 'source_type': 'microphone', 'display_name': 'Microphone'},
    ]}

    selected = select_combined_audio_streams(
        manifest,
        (_stream(1, 0), _stream(2, 1), _stream(3, 2)),
    )

    assert selected == (0, 2)


def test_verified_manifest_with_no_base_sources_does_not_mix_application_stems():
    manifest = {'sources': [
        {'stream_index': 1, 'source_type': 'application', 'display_name': 'Game'},
        {'stream_index': 2, 'source_type': 'application', 'display_name': 'Chat'},
    ]}

    assert select_combined_audio_streams(
        manifest, (_stream(1, 0), _stream(2, 1))) == ()


def test_legacy_clip_uses_explicit_system_and_microphone_labels():
    streams = (_stream(1, 0, 'System Audio'),
               _stream(2, 1, 'Microphone'),
               _stream(3, 2, 'Game'))

    assert select_combined_audio_streams(None, streams) == (0, 1)


def test_legacy_clip_without_labels_keeps_all_audio_as_compatibility_fallback():
    streams = (_stream(1, 0), _stream(2, 1))

    assert select_combined_audio_streams(None, streams) == (0, 1)


def test_ffprobe_audio_ordinals_are_derived_from_absolute_container_indexes():
    streams = descriptors_from_ffprobe_streams([
        {'index': 0, 'codec_type': 'video'},
        {'index': 3, 'codec_type': 'audio',
         'tags': {'handler_name': 'System Audio'}},
        {'index': 7, 'codec_type': 'audio',
         'tags': {'title': 'Microphone'}},
    ])

    assert [(stream.container_index, stream.audio_index, stream.handler_name)
            for stream in streams] == [
                (3, 0, 'System Audio'), (7, 1, None)]


def test_combined_filter_preserves_shared_timestamps_before_amix():
    audio_filter = build_combined_audio_filter((0, 2))

    assert audio_filter.count('aresample=first_pts=0:async=0') == 2
    assert '[audio_input_0][audio_input_1]amix=inputs=2:' in audio_filter
    assert 'normalize=0:duration=longest:dropout_transition=0' in audio_filter
    assert 'alimiter=limit=0.98:level=disabled[aout]' in audio_filter


def test_single_combined_stream_keeps_anull_compatibility_path():
    assert build_combined_audio_filter((3,)) == '[0:a:3]anull[aout]'


def test_padding_mode_appends_apad_after_existing_limiter():
    audio_filter = build_combined_audio_filter((0, 2), pad_to_video_end=True)

    assert ('alimiter=limit=0.98:level=disabled[limited];'
            '[limited]apad[aout]') in audio_filter
    assert 'aresample=first_pts=0:async=0' in audio_filter
    assert 'amix=inputs=2:normalize=0:duration=longest:dropout_transition=0' in audio_filter


def test_single_stream_padding_mode_keeps_anull_then_apad():
    assert build_combined_audio_filter((3,), pad_to_video_end=True) == (
        '[0:a:3]anull,apad[aout]')


def test_padding_mode_extends_short_audio_to_video_end_with_ffmpeg(tmp_path):
    """The production graph must keep the complete video when AAC is shorter."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    try:
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
    except Exception as error:
        pytest.skip(f'FFmpeg unavailable: {error}')

    native = tmp_path / 'short-audio.mp4'
    combined = tmp_path / 'short-audio-combined.mp4'
    def run(command):
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0, result.stderr

    run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=30:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:d=1',
        '-map', '0:v', '-map', '1:a', '-t', '2',
        '-c:v', 'mpeg4', '-q:v', '5', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-b:a', '128k', str(native),
    ])
    run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(native),
        '-filter_complex', build_combined_audio_filter(
            (0,), pad_to_video_end=True),
        '-map', '0:v:0', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac',
        '-shortest', str(combined),
    ])
    probe = subprocess.run(
        [ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(combined)],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
    streams = json.loads(probe.stdout)['streams']
    video = next(stream for stream in streams if stream['codec_type'] == 'video')
    audio = next(stream for stream in streams if stream['codec_type'] == 'audio')
    assert float(video['duration']) >= 1.95
    assert float(audio['duration']) >= 1.90
    assert float(audio['duration']) <= float(video['duration']) + 0.05


def test_native_combine_worker_calls_shared_filter_builder():
    source = (Path(__file__).parents[1] / 'FTHR_UI' / 'main.py').read_text(
        encoding='utf-8')
    module = ast.parse(source)
    worker = next(node for node in ast.walk(module)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == '_combine_native_audio_worker')
    calls = [node for node in ast.walk(worker)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == 'build_combined_audio_filter']

    assert len(calls) == 1
    assert any(
        keyword.arg == 'pad_to_video_end'
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in calls[0].keywords)

    timeline_events = [node for node in ast.walk(worker)
                       if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name)
                       and node.func.id == 'emit_event']
    assert timeline_events
    for call in timeline_events:
        assert any(keyword.arg == 'clip_path' for keyword in call.keywords)
