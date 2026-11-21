import json

import pytest

from core.engine_startup_diagnostics import (
    EngineLaunchContext,
    extract_startup_failure,
    extract_startup_warnings,
    format_engine_launch_failure,
)


def test_extracts_last_structured_engine_failure():
    output = """engine setup\nFTHR_STARTUP_ERROR: ENCODER_INIT_FAILED: old\nnoise\nFTHR_STARTUP_ERROR: REQUESTED_CODEC_UNSUPPORTED: AV1 is not supported\n"""

    failure = extract_startup_failure(output)

    assert failure.code == 'REQUESTED_CODEC_UNSUPPORTED'
    assert failure.detail == 'AV1 is not supported'
    assert failure.title == 'REQUESTED CODEC UNSUPPORTED'


def test_missing_structured_failure_has_actionable_generic_result():
    failure = extract_startup_failure('unexpected native process exit')

    assert failure.code == 'ENGINE_START_FAILED'
    assert 'before connecting' in failure.detail


def test_detail_is_bounded_for_ui_display():
    output = 'FTHR_STARTUP_ERROR: ENCODER_INIT_FAILED: ' + ('x' * 4000)

    failure = extract_startup_failure(output)

    assert len(failure.detail) == 1000


def test_extracts_linux_video_only_audio_warning():
    output = (
        'FTHR_STARTUP_WARNING: DESKTOP_AUDIO_UNAVAILABLE: '
        'Default output monitor could not be opened; capture continues video-only.\n')

    warnings = extract_startup_warnings(output)

    assert len(warnings) == 1
    assert warnings[0].code == 'DESKTOP_AUDIO_UNAVAILABLE'
    assert 'video-only' in warnings[0].detail


def test_winerror_4551_launch_failure_preserves_native_call_and_context():
    error = OSError(4551, 'blocked by Windows policy')
    error.winerror = 4551

    failure = format_engine_launch_failure(
        error,
        EngineLaunchContext(
            selected_monitor_id=r'\\?\display#monitor-3090',
            requested_capture_mode='desktop',
            requested_encoder='nvenc',
            codec='hevc',
        ),
    )
    diagnostic = json.loads(failure.detail.split('diagnostic=', 1)[1])
    native = diagnostic['native_failure']
    context = diagnostic['startup_context']

    assert failure.code == 'ENGINE_PROCESS_LAUNCH_FAILED'
    assert native['api_call'] == 'CreateProcessW'
    assert native['error_domain'] == 'win32'
    assert native['native_error_signed'] == 4551
    assert native['native_error_unsigned'] == 4551
    assert native['native_error_hex'] == '0x000011C7'
    assert native['win32_error_decimal'] == 4551
    assert native['symbolic_error'] == 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION'
    assert native['system_message']
    assert context == {
        'selected_monitor_id': r'\\?\display#monitor-3090',
        'dxgi_output': 'unavailable:engine_process_not_started',
        'owning_adapter_luid': 'unavailable:engine_process_not_started',
        'capture_device_adapter_luid': 'unavailable:engine_process_not_started',
        'encoder_adapter_luid': 'unavailable:engine_process_not_started',
        'capture_backend': 'unavailable:engine_process_not_started',
        'encoder_backend': 'unavailable:engine_process_not_started',
        'codec': 'hevc',
        'requested_capture_mode': 'desktop',
        'requested_encoder': 'nvenc',
    }


@pytest.mark.parametrize(('code', 'symbol'), [
    (225, 'ERROR_VIRUS_INFECTED'),
    (226, 'ERROR_VIRUS_DELETED'),
])
def test_antivirus_block_identifies_detection_and_preserves_diagnostics(code, symbol):
    error = OSError('launch blocked')
    error.winerror = code
    failure = format_engine_launch_failure(error, EngineLaunchContext('', 'desktop', 'auto', 'h264'))
    message, diagnostic = failure.detail.split('diagnostic=', 1)
    assert 'antivirus protection history' in message
    assert json.loads(diagnostic)['native_failure']['symbolic_error'] == symbol


def test_non_win32_launch_error_is_not_mislabeled_as_winerror():
    error = OSError(13, 'permission denied')
    failure = format_engine_launch_failure(
        error,
        EngineLaunchContext('', 'desktop', 'auto', 'h264'),
        api_call='subprocess.Popen',
    )
    diagnostic = json.loads(failure.detail.split('diagnostic=', 1)[1])

    assert diagnostic['native_failure']['error_domain'] == 'os_error'
    assert diagnostic['native_failure']['win32_error_decimal'] is None
    assert diagnostic['native_failure']['symbolic_error'] is None


def test_low_resource_warning_explains_attempt_without_reporting_failure():
    from core.engine_startup_diagnostics import extract_startup_warnings
    warnings = extract_startup_warnings(
        'FTHR_STARTUP_WARNING: LOW_REPLAY_RAM: Replay RAM estimate: 806 MiB; '
        'available budget: 128 MiB. Trying anyway; capture or saving may fail.\n')
    assert len(warnings) == 1
    assert warnings[0].code == 'LOW_REPLAY_RAM'
    assert 'Trying anyway' in warnings[0].detail
