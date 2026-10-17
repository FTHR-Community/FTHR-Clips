import io
import json
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from core.field_diagnostics import (
    DiagnosticError,
    DiagnosticSession,
    EngineLogCapture,
    build_adapter_chain,
    classify_capture_stall,
    classify_encoder_stall,
    collect_gpu_adapters,
    qt_display_snapshot,
)


def _events(session: DiagnosticSession) -> list[dict]:
    assert session.flush()
    return [
        json.loads(line)
        for line in session.events_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]


def test_native_encoder_taxonomy_uses_real_backend_boundaries():
    source = (
        Path(__file__).resolve().parent.parent
        / 'FTHRcapture' / 'FTHRclips' / 'src' / 'capture_engine.cpp'
    ).read_text(encoding='utf-8')

    assert 'startup_encoder_backend_ == "native-nvenc"' in source
    assert 'diagnostic_error = "ENCODER_NVENC_INIT_FAILED"' in source
    assert 'startup_encoder_backend_ == "ffmpeg-amf"' in source
    assert 'diagnostic_error = "ENCODER_AMF_INIT_FAILED"' in source
    assert 'startup_encoder_backend_ == "ffmpeg-qsv"' in source
    assert 'diagnostic_error = "ENCODER_QSV_INIT_FAILED"' in source


@pytest.mark.parametrize(
    ('vendor', 'encoder_backend', 'codec'),
    [
        ('NVIDIA', 'native-nvenc', 'h264'),
        ('AMD', 'ffmpeg-amf', 'hevc'),
        ('Intel', 'ffmpeg-qsv', 'av1'),
    ],
)
def test_single_adapter_topologies_are_complete(vendor, encoder_backend, codec):
    luid = {'high': 17, 'low': 42}
    chain = build_adapter_chain({
        'monitor_id': 'monitor-session-token',
        'windows_display': r'\\.\DISPLAY1',
        'dxgi_output': {'index': 0},
        'monitor_adapter': vendor,
        'monitor_adapter_luid': luid,
        'capture_d3d11_device': 'selected-monitor-adapter',
        'capture_device_luid': luid,
        'encoder_adapter': vendor,
        'encoder_adapter_luid': luid,
        'capture_backend': 'WGC',
        'encoder_backend': encoder_backend,
        'codec': codec,
    })

    assert set(chain) == {
        'monitor_id', 'windows_display', 'dxgi_output', 'monitor_adapter',
        'monitor_adapter_luid', 'capture_d3d11_device', 'capture_device_luid',
        'encoder_adapter', 'encoder_adapter_luid', 'capture_backend',
        'encoder_backend', 'codec',
    }
    assert chain['monitor_adapter_luid'] == chain['capture_device_luid']
    assert chain['capture_device_luid'] == chain['encoder_adapter_luid']


def test_hybrid_adapter_mismatch_remains_visible():
    chain = build_adapter_chain({
        'monitor_id': 'monitor-a',
        'windows_display': r'\\.\DISPLAY2',
        'dxgi_output': {'index': 1},
        'monitor_adapter': 'AMD',
        'monitor_adapter_luid': {'high': 1, 'low': 2},
        'capture_d3d11_device': 'selected-monitor-adapter',
        'capture_device_luid': {'high': 1, 'low': 2},
        'encoder_adapter': 'NVIDIA',
        'encoder_adapter_luid': {'high': 9, 'low': 8},
        'capture_backend': 'DXGI',
        'encoder_backend': 'native-nvenc',
        'codec': 'h264',
    })

    assert chain['capture_device_luid'] != chain['encoder_adapter_luid']
    assert chain['monitor_adapter'] == 'AMD'
    assert chain['encoder_adapter'] == 'NVIDIA'


def test_early_engine_failure_explains_every_unavailable_topology_step():
    chain = build_adapter_chain(
        None, unavailable_reason='engine_process_not_started')

    assert all(
        value == 'unavailable:engine_process_not_started'
        for value in chain.values()
    )


class _Geometry:
    def __init__(self, x, y, width, height):
        self._values = x, y, width, height

    def x(self):
        return self._values[0]

    def y(self):
        return self._values[1]

    def width(self):
        return self._values[2]

    def height(self):
        return self._values[3]


class _Screen:
    def __init__(self, name, geometry, refresh, ratio, dpi):
        self._name = name
        self._geometry = geometry
        self._refresh = refresh
        self._ratio = ratio
        self._dpi = dpi

    def name(self):
        return self._name

    def geometry(self):
        return self._geometry

    def refreshRate(self):
        return self._refresh

    def devicePixelRatio(self):
        return self._ratio

    def logicalDotsPerInch(self):
        return self._dpi


class _Application:
    def __init__(self, screens):
        self._screens = screens

    def screens(self):
        return self._screens

    def primaryScreen(self):
        return self._screens[0]


def test_multi_monitor_snapshot_preserves_negative_coordinates_and_mixed_dpi():
    primary = _Screen('Primary', _Geometry(0, 0, 2560, 1440), 200.0, 1.25, 120.0)
    left = _Screen('Left', _Geometry(-1920, 180, 1920, 1080), 60.0, 1.0, 96.0)

    displays = qt_display_snapshot(_Application([primary, left]))

    assert displays[0]['primary'] is True
    assert displays[0]['device_pixel_ratio'] == 1.25
    assert displays[1]['desktop_coordinates'] == {'x': -1920, 'y': 180}
    assert displays[1]['refresh_hz'] == 60.0
    assert displays[1]['logical_dpi'] == 96.0


def test_gpu_inventory_supports_nvidia_amd_intel_without_collecting_serials():
    payload = json.dumps([
        {'Name': 'NVIDIA GeForce RTX', 'AdapterRAM': 8_000_000_000,
         'DriverVersion': '1.2.3'},
        {'Name': 'AMD Radeon Graphics', 'AdapterRAM': 1_000_000_000,
         'DriverVersion': '4.5.6'},
        {'Name': 'Intel Arc', 'AdapterRAM': 4_000_000_000,
         'DriverVersion': '7.8.9'},
    ])

    adapters = collect_gpu_adapters(
        lambda _command, _timeout: payload, platform_name='win32')

    assert [adapter['vendor'] for adapter in adapters] == ['NVIDIA', 'AMD', 'Intel']
    assert all('serial' not in key.lower()
               for adapter in adapters for key in adapter)
    assert all(adapter['adapter_luid'] == 'unavailable:engine_mapping_required'
               for adapter in adapters)


def test_capture_and_encoder_stalls_classify_distinct_pipeline_boundaries():
    no_frames = classify_capture_stall(
        active=True, frames_acquired=0, last_acquired_ms=1000, now_ms=7000)
    frozen = classify_capture_stall(
        active=True, frames_acquired=30, last_acquired_ms=1000, now_ms=7000)
    encoder = classify_encoder_stall(
        active=True, submissions=30, last_submission_ms=6500,
        last_output_ms=1000, now_ms=7000)

    assert no_frames.error is DiagnosticError.CAPTURE_NO_FRAMES
    assert frozen.error is DiagnosticError.CAPTURE_FRAME_STALLED
    assert encoder.error is DiagnosticError.ENCODER_OUTPUT_STALLED


def test_winerror_4551_event_preserves_native_evidence_and_high_level_boundary(tmp_path):
    session = DiagnosticSession('test', root=tmp_path).start()
    try:
        session.emit(
            'engine', 'start_failed', state='FAILED',
            error=DiagnosticError.ENGINE_START_FAILED,
            compatibility_error='Error 001',
            native_failure={
                'api_call': 'CreateProcessW',
                'native_error_signed': 4551,
                'native_error_unsigned': 4551,
                'native_error_hex': '0x000011C7',
                'win32_error_decimal': 4551,
                'symbolic_error': 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION',
            },
            startup_context=build_adapter_chain(
                None, unavailable_reason='engine_process_not_started'),
        )
        record = _events(session)[-1]
    finally:
        session.close()

    assert record['error_code'] == 'ENGINE_START_FAILED'
    assert record['fields']['compatibility_error'] == 'Error 001'
    assert record['fields']['native_failure'] == {
        'api_call': 'CreateProcessW',
        'native_error_signed': 4551,
        'native_error_unsigned': 4551,
        'native_error_hex': '0x000011C7',
        'win32_error_decimal': 4551,
        'symbolic_error': 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION',
    }
    assert all(
        value == 'unavailable:engine_process_not_started'
        for value in record['fields']['startup_context'].values()
    )


def test_dxgi_access_lost_preserves_runtime_api_hresult_and_adapter(tmp_path):
    session = DiagnosticSession('test', root=tmp_path).start()
    try:
        session.emit_native({
            'subsystem': 'capture',
            'event': 'runtime_failed',
            'state': 'FAILED',
            'error_code': 'CAPTURE_DXGI_RUNTIME_FAILED',
            'native_failure': {
                'api_call': 'IDXGIOutputDuplication::AcquireNextFrame',
                'error_domain': 'hresult',
                'native_error_signed': -2005270490,
                'native_error_unsigned': 2289696806,
                'native_error_hex': '0x887A0026',
                'win32_error_decimal': None,
                'symbolic_error': None,
                'system_message': 'unavailable',
            },
            'capture_device_luid': {'high_part': 0, 'low_part': 97705},
        })
        record = _events(session)[-1]
    finally:
        session.close()

    assert record['error_code'] == 'CAPTURE_DXGI_RUNTIME_FAILED'
    native = record['fields']['native_failure']
    assert native['api_call'] == 'IDXGIOutputDuplication::AcquireNextFrame'
    assert native['native_error_hex'] == '0x887A0026'
    assert record['fields']['capture_device_luid'] == {
        'high_part': 0, 'low_part': 97705,
    }


@pytest.mark.parametrize(
    ('subsystem', 'event', 'error'),
    [
        ('audio', 'initialization_failed', DiagnosticError.AUDIO_OUTPUT_INIT_FAILED),
        ('playback', 'decoder_failed', DiagnosticError.PLAYBACK_DECODER_FAILED),
        ('export', 'process_failed', DiagnosticError.EXPORT_PROCESS_FAILED),
    ],
)
def test_failure_events_keep_subsystem_classification_and_detail(
        tmp_path, subsystem, event, error):
    session = DiagnosticSession('test', root=tmp_path).start()
    try:
        session.emit(subsystem, event, state='FAILED', error=error,
                     detail='injected deterministic failure')
        record = _events(session)[-1]
    finally:
        session.close()

    assert record['subsystem'] == subsystem
    assert record['error_code'] == error.value
    assert record['fields']['detail'] == 'injected deterministic failure'


def test_stage6_audio_error_taxonomy_is_stable():
    expected = {
        'SYSTEM_AUDIO_INIT_FAILED',
        'SYSTEM_AUDIO_NO_PACKETS',
        'MIC_INIT_FAILED',
        'MIC_NO_PACKETS',
        'AUDIO_ENDPOINT_NOT_FOUND',
        'AUDIO_ENDPOINT_SCAN_TIMEOUT',
        'AUDIO_DEVICE_INVALIDATED',
        'AUDIO_FORMAT_UNSUPPORTED',
        'AUDIO_RESAMPLE_FAILED',
        'AUDIO_ENCODER_FAILED',
        'AUDIO_PACKET_DISCONTINUITY',
        'PROCESS_AUDIO_INIT_FAILED',
        'PROCESS_AUDIO_SOURCE_LIMIT',
    }
    assert {DiagnosticError[name].value for name in expected} == expected


def test_native_event_updates_actual_configuration_and_adapter_chain(tmp_path):
    session = DiagnosticSession('test', root=tmp_path).start()
    try:
        session.emit_native({
            'subsystem': 'capture',
            'event': 'adapter_topology_resolved',
            'monitor_id': 'monitor-a',
            'windows_display': r'\\.\DISPLAY1',
            'dxgi_output': {'index': 0},
            'monitor_adapter': 'NVIDIA',
            'monitor_adapter_luid': {'high': 1, 'low': 2},
            'capture_d3d11_device': 'selected-monitor-adapter',
            'capture_device_luid': {'high': 1, 'low': 2},
            'encoder_adapter': 'NVIDIA',
            'encoder_adapter_luid': {'high': 1, 'low': 2},
            'capture_backend': 'WGC',
            'encoder_backend': 'native-nvenc',
            'codec': 'hevc',
            'capture_width': 2560,
            'capture_height': 1440,
            'encoder_width': 1920,
            'encoder_height': 1080,
        })
        summary = session.summary()
    finally:
        session.close()

    assert summary['adapter_topology']['capture_device_luid'] == {'high': 1, 'low': 2}
    assert summary['actual_configuration']['capture_dimensions'] == {
        'width': 2560, 'height': 1440,
    }
    assert summary['actual_configuration']['encoder_dimensions'] == {
        'width': 1920, 'height': 1080,
    }
    assert summary['actual_configuration']['actual_encoder'] == 'native-nvenc'


def test_system_and_microphone_health_remain_separate_in_summary(tmp_path):
    session = DiagnosticSession('test', root=tmp_path).start()
    try:
        session.emit_native({
            'subsystem': 'audio', 'event': 'audio_health_snapshot',
            'source': 'system_audio', 'packet_count': 100,
        })
        session.emit_native({
            'subsystem': 'audio', 'event': 'audio_health_snapshot',
            'source': 'microphone', 'packet_count': 90,
        })
        summary = session.summary()
    finally:
        session.close()

    assert summary['audio_health']['system_audio']['packet_count'] == 100
    assert summary['audio_health']['microphone']['packet_count'] == 90


def test_engine_log_capture_parses_native_events_and_rotates(tmp_path):
    session = DiagnosticSession('test', root=tmp_path / 'sessions').start()
    engine_log = tmp_path / 'engine.log'
    stream = io.StringIO(
        'legacy startup line\n'
        'FTHR_DIAGNOSTIC_EVENT '
        '{"subsystem":"capture","event":"capture_health_snapshot",'
        '"frames_acquired":10,"encoded_packets":9}\n')
    capture = EngineLogCapture(engine_log, session, max_bytes=90)
    try:
        capture.attach(stream)
        for _ in range(100):
            if 'capture_health_snapshot' in {
                    event['event'] for event in _events(session)}:
                break
            time.sleep(0.01)
        capture.close()
        summary = session.summary()
    finally:
        session.close()

    assert 'legacy startup line' in capture.startup_text()
    assert summary['native_capture_health']['frames_acquired'] == 10
    assert engine_log.stat().st_size <= 90


def test_diagnostic_zip_is_bounded_parseable_redacted_and_contains_no_media(tmp_path):
    root = tmp_path / 'sessions'
    ui_log = tmp_path / 'ui.log'
    ui_log.write_text(
        r'path=C:\Users\Alice\Videos\private.mp4 Authorization: Bearer secret-value',
        encoding='utf-8')
    session = DiagnosticSession(
        'test', root=root, max_event_bytes=800).start()
    session.engine_log_path.write_text(
        r'cache=C:\Users\Alice\AppData\Local\FTHR password=hunter2',
        encoding='utf-8')
    try:
        for index in range(100):
            session.emit('capture', 'bounded_snapshot', index=index, detail='x' * 80)
        destination = session.export_zip(
            tmp_path / 'FTHR-Clips-Diagnostics-test.zip',
            ui_log_path=ui_log,
            displays=[{'name': 'Display 1', 'desktop_coordinates': {'x': -10, 'y': 0}}],
        )
        assert session.events_path.stat().st_size <= 800
    finally:
        session.close()

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert names == {
            'diagnostic-summary.json', 'events.jsonl', 'ui.log',
            'engine.log', 'README.txt',
        }
        summary = json.loads(archive.read('diagnostic-summary.json'))
        for line in archive.read('events.jsonl').decode('utf-8').splitlines():
            json.loads(line)
        combined = '\n'.join(
            archive.read(name).decode('utf-8', errors='replace') for name in names)

    assert summary['privacy']['media_included'] is False
    assert summary['displays'][0]['desktop_coordinates']['x'] == -10
    assert 'C:\\Users\\Alice' not in combined
    assert 'secret-value' not in combined
    assert 'hunter2' not in combined
    assert '%USERPROFILE%' in combined
    assert not any(Path(name).suffix.lower() in {'.mp4', '.mkv', '.png', '.wav'}
                   for name in names)


def test_previous_unclean_session_is_reported_on_next_launch(tmp_path):
    first = DiagnosticSession('test', root=tmp_path).start()
    second = DiagnosticSession('test', root=tmp_path).start()
    try:
        events = _events(second)
        summary = second.summary()
    finally:
        first.close(clean=False)
        second.close(clean=True)

    assert any(event['event'] == 'previous_session_unclean' for event in events)
    assert summary['previous_session']['status'] == 'did_not_shut_down_cleanly'
    assert summary['previous_session']['session_id'] == first.session_id


def test_settings_troubleshooting_action_emits_export_request(
        qtbot, tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    requests = []
    page.diagnostic_export_requested.connect(lambda: requests.append(True))

    assert page.export_diagnostic_btn.text() == 'Export diagnostic report'
    page.export_diagnostic_btn.click()

    assert requests == [True]


def test_session_ids_are_random_valid_uuids(tmp_path):
    first = DiagnosticSession('test', root=tmp_path).start()
    second = DiagnosticSession('test', root=tmp_path).start()
    try:
        assert uuid.UUID(first.session_id).version == 4
        assert uuid.UUID(second.session_id).version == 4
        assert first.session_id != second.session_id
    finally:
        first.close(clean=False)
        second.close(clean=True)
