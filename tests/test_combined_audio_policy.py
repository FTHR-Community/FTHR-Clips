from __future__ import annotations

import ast
from pathlib import Path

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

    timeline_events = [node for node in ast.walk(worker)
                       if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name)
                       and node.func.id == 'emit_event']
    assert timeline_events
    for call in timeline_events:
        assert any(keyword.arg == 'clip_path' for keyword in call.keywords)
