"""Contract tests for immutable capture requests and sessions."""

from __future__ import annotations

import pytest

from core.capture_session import CameraSnapshot
from core.capture_session import CaptureSessionSnapshot
from core.capture_session import CaptureSnapshotError
from core.capture_session import ProfileReference
from core.capture_session import ResolvedCaptureRequest
from core.capture_session import SourceSnapshot
from core.capture_session import bind_capture_session
from core.capture_settings import CaptureConfig
from core.capture_settings import audio_plan_from_settings
from core.source_authority import identity_matches


def _config(**overrides):
    values = {
        'fps': 60,
        'buffer_seconds': 32,
        'width': 1920,
        'height': 1080,
        'bitrate_kbps': 25_000,
        'codec': 'auto',
        'preset': 4,
        'monitor': 'DISPLAY1',
        'scaling': 'stretch',
        'audio_enabled': True,
    }
    values.update(overrides)
    return CaptureConfig(**values)


def _request(**overrides):
    values = {
        'request_token': 1,
        'purpose': 'clips',
        'source': SourceSnapshot(
            'window', r'C:\Games\Game.exe',
            {'hwnd': 2, 'pid': 3, 'exe_path': r'C:\Games\Game.exe',
             'class_name': 'GameWindow', 'process_created': 4},
            display_name='Game', clip_group='g1', monitor_identity='DISPLAY1'),
        'capture_config': _config(),
        'mic_gain_percent': 0,
        'camera': CameraSnapshot(
            enabled=False, device_index=0,
            overlay_rect={'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}),
        'effects': {'watermark_enabled': False},
    }
    values.update(overrides)
    return ResolvedCaptureRequest(**values)


def test_request_and_session_are_deeply_detached():
    source = {'hwnd': 2, 'pid': 3, 'exe_path': r'C:\Games\Game.exe',
              'class_name': 'GameWindow', 'process_created': 4}
    effects = {'image_overlays': [{'path': 'a.png', 'enabled': True}]}
    request = _request(source=SourceSnapshot('window', r'C:\Games\Game.exe', source),
                       effects=effects)
    session = bind_capture_session(
        request, process_generation=4, native_capture_generation=8,
        accepted_config_token=11)
    source['pid'] = 99
    effects['image_overlays'][0]['enabled'] = False
    detached = session.to_dict()
    assert detached['request']['source']['live_identity']['pid'] == 3
    assert detached['request']['effects']['image_overlays'][0]['enabled'] is True
    assert session.request == request
    assert identity_matches(
        detached['request']['source']['live_identity'],
        detached['request']['source']['live_identity'])


def test_profile_and_generations_remain_bound_to_accepted_request():
    request = _request(
        profile_id='12345678-1234-5678-1234-567812345678',
        profile_revision=2, profile_name='Game',
        profile_document_revision=5, source_intent_revision=7)
    session = CaptureSessionSnapshot(
        request, 11, 13, accepted_config_token=7,
        manual_consent_nonce='consent-1', allowlist_revision=9)
    assert session.request.profile_revision == 2
    assert session.process_generation == 11
    assert session.native_capture_generation == 13
    assert session.accepted_config_token == 7
    assert session.manual_consent_nonce == 'consent-1'
    assert session.allowlist_revision == 9


def test_absent_profile_reference_requires_strict_zero_revision():
    with pytest.raises(CaptureSnapshotError):
        ProfileReference(profile_id=None, profile_revision=False)


def test_capture_only_effect_whitelist_rejects_credentials_and_preferences():
    with pytest.raises(CaptureSnapshotError):
        _request(effects={'password': 'secret'})
    with pytest.raises(CaptureSnapshotError):
        _request(effects={'upload_account': 'user@example.test'})


@pytest.mark.parametrize('field,value', [
    ('fps', True),
    ('bitrate_kbps', 100),
    ('preset', 0),
    ('normal_clip_seconds', 901),
    ('allow_replay_gaps', 1),
    ('allow_replay_gaps', 'true'),
    ('allow_low_fps_clips', 1),
    ('allow_low_fps_clips', 'true'),
    ('capture_cursor', 1),
    ('capture_cursor', 'false'),
])
def test_capture_config_boundaries_are_revalidated(field, value):
    with pytest.raises(CaptureSnapshotError):
        _request(capture_config=_config(**{field: value}))


def test_replay_gap_policy_is_bound_to_the_accepted_capture_request():
    request = _request(capture_config=_config(allow_replay_gaps=True))
    session = bind_capture_session(
        request, process_generation=4, native_capture_generation=8,
        accepted_config_token=11)
    assert session.request.capture_config.allow_replay_gaps is True
    assert session.to_dict()['request']['capture_config']['allow_replay_gaps'] is True


@pytest.mark.parametrize('field,valid,invalid', [
    ('fps', (15, 240), (14, 241, True)),
    ('bitrate_kbps', (500, 200000), (499, 200001, True)),
    ('normal_clip_seconds', (5, 900), (4, 901, True)),
])
def test_capture_numeric_boundaries_accept_only_contract_values(
        field, valid, invalid):
    for value in valid:
        _request(capture_config=_config(**{field: value}))
    for value in invalid:
        with pytest.raises(CaptureSnapshotError):
            _request(capture_config=_config(**{field: value}))


@pytest.mark.parametrize('gain', [0, 200])
def test_microphone_gain_accepts_both_contract_boundaries(gain):
    _request(mic_gain_percent=gain)


@pytest.mark.parametrize('gain', [-1, 201, True])
def test_microphone_gain_rejects_outside_contract(gain):
    with pytest.raises(CaptureSnapshotError):
        _request(mic_gain_percent=gain)


@pytest.mark.parametrize('crop_w,crop_h', [(0, 1), (1, 0)])
def test_enabled_crop_requires_positive_width_and_height(crop_w, crop_h):
    with pytest.raises(CaptureSnapshotError):
        _request(capture_config=_config(
            crop_enabled=True, crop_w=crop_w, crop_h=crop_h))


@pytest.mark.parametrize('purpose', [[], 'desktop'])
def test_request_purpose_enum_is_strict(purpose):
    with pytest.raises(CaptureSnapshotError):
        _request(purpose=purpose)


def test_source_identity_and_camera_rect_are_strict():
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot('window', r'C:\Games\Game.exe', {'hwnd': True})
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot('window', r'C:\Games\Game.exe', ('legacy',))
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot('game', r'C:\Games\Game.exe', {})
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot('window', r'C:\Games\Game.exe',
                       {'process_created': True})
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot(['window'], r'C:\Games\Game.exe', {})
    with pytest.raises(CaptureSnapshotError):
        SourceSnapshot('window', r'C:\Games\Game.exe', {1: 'bad'})
    with pytest.raises(CaptureSnapshotError):
        CameraSnapshot(overlay_rect={'x': 0.1, 'y': 0.2, 'w': 0.3})
    with pytest.raises(CaptureSnapshotError):
        CameraSnapshot(overlay_rect={'x': float('nan'), 'y': 0.2,
                                     'w': 0.3, 'h': 0.4})
    for device_index in (0, 255):
        CameraSnapshot(device_index=device_index)
    for device_index in (-1, 256, True):
        with pytest.raises(CaptureSnapshotError):
            CameraSnapshot(device_index=device_index)


def test_source_sentinel_dimensions_and_encoded_tracks_are_strict():
    source_config = _config(width=0, height=0)
    assert _request(capture_config=source_config).capture_config.width == 0
    for width, height in ((0, 1080), (1920, 0), (-1, 1080), (1920, -1)):
        with pytest.raises(CaptureSnapshotError):
            _request(capture_config=_config(width=width, height=height))
    valid = _request(capture_config=_config(
        audio_device_tracks='Mic;mic-1|Game Audio;game-1'))
    assert valid.capture_config.audio_device_tracks.startswith('Mic;')
    for tracks in ('bad', 'Mic;one|Mic;one', 'Mic;one|Bad|x',
                    'Bad/name;one', 'Mic;one|A;two|B;three|C;four|D;five'):
        with pytest.raises(CaptureSnapshotError):
            _request(capture_config=_config(audio_device_tracks=tracks))
    plan = audio_plan_from_settings({
        'audio_source_mode': 'devices',
        'audio_capture_mode': 'combined',
        'audio_device_tracks': [{'id': 'mic-1', 'name': 'Mic'}],
        'per_app_audio_tracks': None,
    })
    assert plan == (True, False, [{'id': 'mic-1', 'name': 'Mic'}])


def test_effects_use_strict_nested_schemas_and_detached_defaults():
    request = _request(effects={
        'image_overlays': [{'path': 'a.png'}],
        'third_party_keyboard': {'enabled': True, 'color': '#123456'},
    })
    exported = request.to_dict()['effects']
    assert exported['image_overlays'][0]['opacity'] == 100
    assert exported['third_party_keyboard']['intensity'] == 58
    exported['image_overlays'][0]['path'] = 'changed'
    assert request.effects['image_overlays'][0]['path'] == 'a.png'
    for effects in (
            {'image_overlays': [{'path': 'a.png', 'credentials': 'secret'}]},
            {'third_party_keyboard': {'password': 'secret'}},
            {'image_overlay_opacity': True},
            {'third_party_keyboard': {'color': 'green'}},
    ):
        with pytest.raises(CaptureSnapshotError):
            _request(effects=effects)


@pytest.mark.parametrize('effects', [
    {'image_overlay_fit': []},
    {'image_overlay_fit': {}},
    {'image_overlays': [{'path': 'a.png', 'fit': []}]},
    {'image_overlays': [{'path': 'a.png', 'fit': {}}]},
])
def test_effect_fit_values_are_typed_before_membership_check(effects):
    with pytest.raises(CaptureSnapshotError):
        _request(effects=effects)


def test_direct_constructor_default_factories_are_independent():
    base = _request()
    first = ResolvedCaptureRequest(
        request_token=1, purpose='clips', source=base.source,
        capture_config=base.capture_config)
    second = ResolvedCaptureRequest(
        request_token=2, purpose='clips', source=base.source,
        capture_config=base.capture_config)
    assert first.camera is not second.camera
    assert first.effects is not second.effects
    detached = first.to_dict()
    detached['camera']['overlay_rect']['x'] = 0.01
    assert first.camera.overlay_rect['x'] != 0.01
    detached['effects']['new'] = True
    assert 'new' not in second.effects


@pytest.mark.parametrize('field,value', [
    ('microphone_endpoint_id', None),
    ('microphone_endpoint_id', ' mic'),
    ('microphone_endpoint_id', 'mic '),
    ('microphone_endpoint_id', 'mic;bad'),
    ('microphone_endpoint_id', 'mic|bad'),
    ('microphone_endpoint_id', 'mic\n'),
    ('microphone_endpoint_id', 'm' * 2049),
])
def test_capture_endpoint_boundaries_are_strict(field, value):
    if value is None:
        with pytest.raises(CaptureSnapshotError):
            _request(capture_config=_config(**{field: value}))
        return
    with pytest.raises(CaptureSnapshotError):
        _request(capture_config=_config(**{field: value}))


@pytest.mark.parametrize('token', [True, 0, -1, 1.5])
def test_accepted_config_token_is_separate_and_strict(token):
    request = _request()
    with pytest.raises(CaptureSnapshotError):
        CaptureSessionSnapshot(
            request, 1, 2, accepted_config_token=token)



@pytest.mark.parametrize('enabled', [False, True])
def test_low_fps_policy_is_bound_to_accepted_capture_request(enabled):
    request = _request(capture_config=_config(allow_low_fps_clips=enabled))
    session = bind_capture_session(
        request, process_generation=4, native_capture_generation=8,
        accepted_config_token=11)
    assert session.request.capture_config.allow_low_fps_clips is enabled
    assert session.to_dict()['request']['capture_config']['allow_low_fps_clips'] is enabled
