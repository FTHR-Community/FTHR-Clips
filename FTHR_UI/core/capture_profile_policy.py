"""Pure policy helpers for resolving a Windows capture profile request.

The policy accepts detached values supplied by MainWindow.  It deliberately
does not know about Qt, SettingsManager, devices, processes, or the camera
singleton, so an invalid profile can be rejected before any owned engine is
stopped.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.capture_settings import (
    AUDIO_CAPTURE_MODE_COMBINED,
    CaptureConfig,
    audio_plan_from_settings,
    compute_buffer_seconds,
    configured_post_roll_seconds,
    encode_audio_device_tracks,
    normalize_audio_capture_mode,
    validate_fps,
    validate_normal_clip_length,
)
from core.game_profiles import resolve_profile
from core.running_apps import encode_application_audio_exclusions


def resolve_profile_values(catalog, exe_path: object, global_values: Mapping):
    """Return the exact profile and immutable effective leaves for one EXE."""
    return resolve_profile(catalog, exe_path, global_values)


def _quality_values(values: Mapping, purpose: str, bitrate_presets: Mapping,
                    resolution_to_dims) -> tuple[int, int, int, int, int]:
    if purpose == 'recording':
        fps_key = 'recording_framerate'
        resolution_key = 'recording_resolution'
        quality_key = 'recording_bitrate_level'
        custom_key = 'recording_custom_bitrate_kbps'
    else:
        fps_key = 'framerate'
        resolution_key = 'resolution'
        quality_key = 'bitrate_level'
        custom_key = 'custom_bitrate_kbps'
    fps = validate_fps(int(values[fps_key]))
    resolution = str(values[resolution_key]).lower()
    if resolution not in bitrate_presets:
        raise ValueError(f'unsupported capture resolution: {resolution}')
    quality = str(values[quality_key]).lower()
    if quality == 'custom':
        bitrate = int(values[custom_key])
        if not 500 <= bitrate <= 200_000:
            raise ValueError('custom bitrate is outside the encoder bound')
    else:
        bitrate = int(bitrate_presets[resolution].get(
            quality, bitrate_presets[resolution]['high']))
    width, height = resolution_to_dims(resolution)
    normal_seconds = validate_normal_clip_length(int(values['clip_length']))
    return fps, width, height, bitrate, normal_seconds


def build_capture_config(*, values: Mapping, global_values: Mapping,
                          purpose: str, bitrate_presets: Mapping,
                          resolution_to_dims, monitor: str, codec: str,
                          encoder: str, preset: int, scaling: str,
                          crop: Mapping | None = None,
                          custom_clip_lengths=()) -> CaptureConfig:
    """Build the unchanged native CaptureConfig from explicit inputs.

    ``values`` contains only the effective profile leaves.  Global-only
    native fields are supplied separately and are never inferred by copying a
    preference document.
    """
    if purpose not in {'clips', 'recording'}:
        raise ValueError('capture purpose must be clips or recording')
    fps, width, height, bitrate, normal_seconds = _quality_values(
        values, purpose, bitrate_presets, resolution_to_dims)
    separate_audio = False
    application_audio = False
    tracks = []
    if global_values.get('platform') == 'win32':
        separate_audio, application_audio, tracks = audio_plan_from_settings(values)
    else:
        separate_audio = (
            normalize_audio_capture_mode(values.get(
                'audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED))
            == 'separated')
    crop = crop if isinstance(crop, Mapping) else None
    crop_enabled = bool(crop and crop.get('enabled', True))
    if crop_enabled:
        crop_x = float(crop.get('x', 0.0))
        crop_y = float(crop.get('y', 0.0))
        crop_w = float(crop.get('w', 1.0))
        crop_h = float(crop.get('h', 1.0))
    else:
        crop_x = crop_y = 0.0
        crop_w = crop_h = 1.0
    from core.replay_storage import REPLAY_STORAGE_KEY, ReplayStorageSettings

    post_roll_seconds = configured_post_roll_seconds(global_values)
    return CaptureConfig(
        fps=fps,
        buffer_seconds=compute_buffer_seconds(
            normal_seconds, custom_clip_lengths, post_roll_seconds),
        width=width,
        height=height,
        bitrate_kbps=bitrate,
        codec=str(codec),
        preset=int(preset),
        monitor=str(monitor or ''),
        scaling=str(scaling),
        audio_enabled=bool(values.get('audio_capture_enabled', True)),
        encoder=str(encoder),
        multiband_enabled=False,
        separate_audio_enabled=separate_audio,
        microphone_endpoint_id=str(values.get('mic_device_id') or ''),
        playback_endpoint_id=(
            str(values.get('playback_device_id') or '')
            if global_values.get('platform') == 'win32' else ''),
        microphone_mono=(
            bool(values.get('mic_downmix_mono', False))
            if global_values.get('platform') == 'win32' else False),
        audio_device_tracks=(
            encode_audio_device_tracks(tracks)
            if global_values.get('platform') == 'win32' else ''),
        application_audio_tracks_enabled=application_audio,
        keep_system_audio_backup=global_values.get('keep_system_audio_backup', True) is not False,
        application_audio_exclusions=(
            encode_application_audio_exclusions(
                global_values.get('application_audio_exclusions', []))
            if global_values.get('platform') == 'win32' else ''),
        normal_clip_seconds=normal_seconds,
        post_roll_seconds=post_roll_seconds,
        crop_enabled=crop_enabled,
        crop_x=crop_x,
        crop_y=crop_y,
        crop_w=crop_w,
        crop_h=crop_h,
        replay_storage=ReplayStorageSettings.from_value(
            global_values.get(REPLAY_STORAGE_KEY)),
        allow_replay_gaps=global_values.get('allow_replay_gaps', True) is True,
        allow_low_fps_clips=global_values.get('allow_low_fps_clips', True) is True,
        capture_cursor=global_values.get('capture_cursor', True) is not False,
    )
