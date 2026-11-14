"""Detached A/B session boundary checks for F12B finalization handoff."""

from core.capture_session import (
    CameraSnapshot,
    SourceSnapshot,
    bind_capture_session,
    build_resolved_capture_request,
)
from core.capture_settings import CaptureConfig


def _request(token, profile_name, *, enabled=False, device_index=0):
    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='auto', preset=4, monitor='',
        scaling='stretch', audio_enabled=True, encoder='auto',
        multiband_enabled=False, separate_audio_enabled=False,
        microphone_endpoint_id='', playback_endpoint_id='',
        microphone_mono=False, audio_device_tracks='',
        application_audio_tracks_enabled=False, normal_clip_seconds=30,
    )
    return build_resolved_capture_request(
        request_token=token, purpose='clips',
        source=SourceSnapshot(source_mode='desktop'),
        capture_config=config,
        camera=CameraSnapshot(enabled=enabled, device_index=device_index),
        effects={'capture_card_enabled': True},
        profile_name=profile_name,
        profile_id='12345678-1234-4234-8234-1234567890ab',
        profile_revision=1,
        profile_document_revision=1,
    )


def test_accepted_session_retains_a_when_latest_request_changes():
    request_a = _request(1, 'A', enabled=True, device_index=2)
    request_b = _request(2, 'B', enabled=False)
    session_a = bind_capture_session(
        request_a, process_generation=7, native_capture_generation=11,
        accepted_config_token=3)

    latest = request_b

    assert session_a.request is request_a
    assert session_a.request.profile_name == 'A'
    assert session_a.request.camera.device_index == 2
    assert latest.profile_name == 'B'
    assert latest.camera.enabled is False
