"""Focused F12B policy checks using detached boundary values only.

These tests intentionally stop at the pure resolver/config boundary.  They do
not instantiate Qt, MainWindow, FFmpeg, a native engine, or a device provider.
"""

from core.capture_profile_policy import build_capture_config


_PRESETS = {
    'source': {'low': 10_000, 'medium': 25_000, 'high': 50_000},
    '1080p': {'low': 10_000, 'medium': 25_000, 'high': 50_000},
}


def _dims(name):
    return {'source': (0, 0), '1080p': (1920, 1080)}[name]


def _values():
    return {
        'clip_length': 30,
        'framerate': 60,
        'resolution': '1080p',
        'bitrate_level': 'high',
        'custom_bitrate_kbps': 25_000,
        'recording_framerate': 120,
        'recording_resolution': 'source',
        'recording_bitrate_level': 'medium',
        'recording_custom_bitrate_kbps': 30_000,
        'audio_capture_enabled': True,
        'audio_capture_mode': 'combined',
        'audio_source_mode': 'devices',
        'audio_device_tracks': [{'id': 'endpoint-a', 'name': 'Game'}],
        'per_app_audio_tracks': False,
        'mic_device_id': 'mic-a',
        'playback_device_id': 'playback-a',
        'mic_downmix_mono': True,
    }


def test_policy_keeps_explicit_clip_values_and_native_audio_plan():
    values = _values()
    config = build_capture_config(
        values=values,
        global_values={'platform': 'win32'},
        purpose='clips',
        bitrate_presets=_PRESETS,
        resolution_to_dims=_dims,
        monitor='\\\\?\\display#one',
        codec='h264',
        encoder='nvenc',
        preset=4,
        scaling='fit',
        custom_clip_lengths=[60],
    )

    assert config.fps == 60
    assert config.width == 1920 and config.height == 1080
    assert config.buffer_seconds == 62
    assert config.audio_device_tracks == 'Game;endpoint-a'
    assert config.microphone_endpoint_id == 'mic-a'
    assert config.playback_endpoint_id == 'playback-a'
    assert config.microphone_mono is True


def test_policy_uses_recording_profile_leaves_without_copying_clip_leaves():
    values = _values()
    config = build_capture_config(
        values=values,
        global_values={'platform': 'linux'},
        purpose='recording',
        bitrate_presets=_PRESETS,
        resolution_to_dims=_dims,
        monitor='',
        codec='auto',
        encoder='software',
        preset=4,
        scaling='stretch',
        custom_clip_lengths=(),
    )

    assert config.fps == 120
    assert (config.width, config.height) == (0, 0)
    assert config.audio_device_tracks == ''
    assert config.playback_endpoint_id == ''
