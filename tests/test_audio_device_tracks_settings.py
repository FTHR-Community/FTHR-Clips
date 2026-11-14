"""Settings page: Windows endpoint tracks and source-mode persistence."""

from types import SimpleNamespace

import pytest

from core.settings_manager import SettingsManager
from core.capture_settings import CaptureConfig, encode_audio_device_tracks, resolve_audio_plan
from core.capture_session import (
    CameraSnapshot,
    SourceSnapshot,
    bind_capture_session,
    build_resolved_capture_request,
)
from main import _SettingsPage


@pytest.fixture
def audio_page(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for name in ('_populate_mic_devices', '_populate_playback_devices',
                 '_refresh_keyboard_windows', '_start_encoder_probe'):
        monkeypatch.setattr(_SettingsPage, name,
                            lambda *args, **kwargs: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(2)
    page._apply_playback_devices([
        SimpleNamespace(endpoint_id='{chat}', display_name='Chat (RODECaster)'),
        SimpleNamespace(endpoint_id='{game}', display_name='Game (RODECaster)'),
    ])
    yield page


def test_added_devices_become_named_tracks_and_leave_picker(audio_page):
    page = audio_page
    emitted = []
    page.audio_capture_changed.connect(emitted.append)

    page.extra_device_combo.setCurrentIndex(
        page.extra_device_combo.findData('{chat}'))
    page.extra_device_name.setText('Chat')
    page._on_add_extra_audio_device()

    assert page.sm.get('audio_device_tracks') == [
        {'id': '{chat}', 'name': 'Chat'}]
    assert page.extra_device_combo.findData('{chat}') < 0
    assert page.extra_device_combo.findData('{game}') >= 0
    assert emitted == [True]


def test_blank_name_falls_back_to_device_name_and_remove_restores_picker(audio_page):
    page = audio_page
    page.extra_device_combo.setCurrentIndex(
        page.extra_device_combo.findData('{game}'))
    page._on_add_extra_audio_device()

    assert page.sm.get('audio_device_tracks') == [
        {'id': '{game}', 'name': 'Game (RODECaster)'}]
    page._on_remove_extra_audio_device('{game}')
    assert page.sm.get('audio_device_tracks') == []
    assert page.extra_device_combo.findData('{game}') >= 0


def test_picker_locks_once_track_limit_is_reached(audio_page):
    page = audio_page
    page._apply_playback_devices([
        SimpleNamespace(endpoint_id=f'{{d{i}}}', display_name=f'Device {i}')
        for i in range(6)])
    for _ in range(4):
        page._on_add_extra_audio_device()

    assert len(page.sm.get('audio_device_tracks')) == 4
    assert not page.extra_device_add.isEnabled()


def test_record_mode_shows_device_list_only_for_several_devices(audio_page):
    page = audio_page
    page.show()

    assert page.audio_source_combo.currentData() == 'combined'
    assert page.extra_audio_group.isHidden()

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('devices'))
    assert not page.extra_audio_group.isHidden()
    assert page.playback_combo.isHidden()
    assert page.sm.get('audio_source_mode') == 'devices'

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('programs'))
    assert page.extra_audio_group.isHidden()
    assert page.sm.get('per_app_audio_tracks') is True
    assert page.playback_combo.isHidden()

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('device'))
    assert not page.playback_combo.isHidden()


def test_program_mode_reaches_active_config_and_separate_post_route(monkeypatch):
    import main
    from core.windows_monitor import MonitorChoice

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monitor = MonitorChoice(
        gdi_name=r'\\.\DISPLAY1', friendly_name='Primary',
        device_path=r'\\?\DISPLAY#PRIMARY\1', primary=True)
    monkeypatch.setattr(main, 'enumerate_windows_monitors',
                        lambda: [monitor])
    values = {
        'capture_monitor': monitor.device_path,
        'audio_capture_enabled': True,
        'audio_capture_mode': 'combined',
        'audio_source_mode': 'programs',
        'audio_device_tracks': [],
        'per_app_audio_tracks': True,
    }
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        set=lambda key, value: values.__setitem__(key, value),
        save_settings=lambda: True,
    )
    host = SimpleNamespace(
        settings_manager=settings, capture_fps=60, clip_duration=30,
        capture_width=1920, capture_height=1080, capture_bitrate=25_000,
        _active_game_window=None)

    config = main.MainWindow._requested_capture_config(host)

    assert config.separate_audio_enabled is True
    assert config.application_audio_tracks_enabled is True
    assert config.audio_device_tracks == ''
    assert main.MainWindow._diagnostic_capture_config(config)[
        'separate_audio_enabled'] is True


@pytest.mark.parametrize(
    ('source_mode', 'active_separate', 'saved_mode', 'expected_route'),
    [('programs', True, 'combined', 'finalize'),
     ('devices', True, 'combined', 'finalize'),
     ('device', False, 'separated', 'mic')],
)
def test_clip_written_dispatch_uses_active_audio_generation(
        tmp_path, monkeypatch, source_mode, active_separate, saved_mode,
        expected_route):
    """A persisted-but-not-applied setting cannot reroute a committed clip."""
    import main

    tracks = ([{'id': '{chat}', 'name': 'Chat'}]
              if source_mode == 'devices' else [])
    separate, application, devices = resolve_audio_plan(
        source_mode, 'combined', tracks, None)
    assert separate is active_separate
    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor='', scaling='stretch', audio_enabled=True,
        separate_audio_enabled=separate,
        application_audio_tracks_enabled=application,
        audio_device_tracks=encode_audio_device_tracks(devices),
    )
    clip = tmp_path / 'saved.mp4'
    clip.write_bytes(b'clip')
    routes = []
    events = []
    capture_jobs = []

    class _Upload:
        @staticmethod
        def notify_clip_saved(_path, *, has_mic_mux):
            return object()

    settings = SimpleNamespace(get=lambda key, default=None: {
        'audio_capture_mode': saved_mode,
        'audio_capture_enabled': True,
        'watermark_enabled': False,
        'camera_enabled': False,
        'image_overlay_enabled': False,
        'third_party_keyboard': {'enabled': False},
    }.get(key, default))
    host = SimpleNamespace(
        _capture_config=SimpleNamespace(active=config),
        settings_manager=settings,
        upload_manager=_Upload(),
        capture_card=SimpleNamespace(show_error=lambda: None),
        _set_status=lambda *_args: None,
    )

    def mux_mic(*args, audio_mode, capture_job):
        capture_jobs.append(capture_job)
        routes.append(('mic', audio_mode))

    def finalize(*args, capture_job):
        capture_jobs.append(capture_job)
        routes.append(('finalize', None))

    host._mux_mic_into_clip = mux_mic
    host._finalize_clip = finalize
    accepted_request = build_resolved_capture_request(
        request_token=1, purpose='clips',
        source=SourceSnapshot(source_mode='desktop'),
        capture_config=config, mic_gain_percent=100,
        camera=CameraSnapshot(enabled=False),
        effects={'third_party_keyboard': {'enabled': False}},
    )
    op = SimpleNamespace(
        output_path=str(clip), op_id='dispatch', duration_seconds=30,
        context={
            'session_snapshot': bind_capture_session(
                accepted_request, process_generation=1,
                native_capture_generation=1, accepted_config_token=1),
        }, requested_at=0.0)
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main.MainWindow, '_register_saved_clip_duration_report',
                        lambda *_args: None)
    monkeypatch.setattr(main, 'third_party_keyboard_settings',
                        lambda _settings: {'enabled': False})
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))

    main.MainWindow._on_clip_written(host, op)

    assert routes == [(expected_route,
                       ('separated' if active_separate else 'combined')
                       if expected_route == 'mic' else None)]
    planned = next(fields for args, fields in events
                   if args[:2] == ('clip_save', 'post_processing_planned'))
    assert planned['audio_mode'] == (
        'separated' if active_separate else 'combined')
    assert len(capture_jobs) == 1
    capture_job = capture_jobs[0]
    expected_identity = {
        'st_dev': int(clip.stat().st_dev),
        'st_ino': int(clip.stat().st_ino),
        'size': int(clip.stat().st_size),
        'mtime_ns': int(clip.stat().st_mtime_ns),
    }
    assert capture_job['operation_id'] == 'dispatch'
    assert capture_job['initial_identity'] == expected_identity
    assert capture_job['owned_identity'] == expected_identity
    assert capture_job['expected_clip_id'] is None


def test_combined_selection_ignores_stale_devices_and_survives_reload(audio_page):
    page = audio_page
    page.sm.set('audio_device_tracks', [{'id': '{chat}', 'name': 'Chat'}])
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('programs'))
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('combined'))
    assert page.sm.get('audio_capture_mode') == 'combined'
    assert page.sm.get('per_app_audio_tracks') is False
    assert not page.playback_combo.isHidden()
    assert page.extra_audio_group.isHidden()
    assert 'microphone' in page.audio_source_hint.text().lower()
    from core.capture_settings import audio_plan_from_settings
    assert audio_plan_from_settings(page.sm) == (False, False, [])
    reloaded = SettingsManager()
    assert reloaded.get('audio_source_mode') == 'combined'
    assert audio_plan_from_settings(reloaded) == (False, False, [])


def test_system_and_microphone_selection_enables_separate_tracks(audio_page):
    page = audio_page
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('device'))
    assert page.audio_source_combo.currentText() == 'System Audio plus Microphone'
    assert page.sm.get('audio_capture_mode') == 'separated'
    from core.capture_settings import audio_plan_from_settings
    assert audio_plan_from_settings(page.sm) == (True, False, [])
