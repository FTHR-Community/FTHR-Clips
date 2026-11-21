"""Bounded parsing for structured native-engine startup failures."""

from dataclasses import dataclass
import ctypes
import json
import re


_STARTUP_FAILURE = re.compile(
    r'^FTHR_STARTUP_ERROR:\s*([A-Z0-9_]+):\s*(.+)$',
    re.MULTILINE,
)
_STARTUP_WARNING = re.compile(
    r'^FTHR_STARTUP_WARNING:\s*([A-Z0-9_]+):\s*(.+)$',
    re.MULTILINE,
)
_MAX_DETAIL_LENGTH = 1000


@dataclass(frozen=True)
class EngineStartupFailure:
    code: str
    title: str
    detail: str


@dataclass(frozen=True)
class EngineStartupWarning:
    code: str
    title: str
    detail: str


@dataclass(frozen=True)
class EngineLaunchContext:
    selected_monitor_id: str
    requested_capture_mode: str
    requested_encoder: str
    codec: str
    dxgi_output: str = 'unavailable:engine_process_not_started'
    owning_adapter_luid: str = 'unavailable:engine_process_not_started'
    capture_device_adapter_luid: str = 'unavailable:engine_process_not_started'
    encoder_adapter_luid: str = 'unavailable:engine_process_not_started'
    capture_backend: str = 'unavailable:engine_process_not_started'
    encoder_backend: str = 'unavailable:engine_process_not_started'


def _windows_symbolic_error_name(error: int) -> str | None:
    # Confirmed by the pinned Windows SDK winerror.h. This maps the constant;
    # it does not infer which API failed. The call site is captured separately.
    if error == 225:
        return 'ERROR_VIRUS_INFECTED'
    if error == 226:
        return 'ERROR_VIRUS_DELETED'
    if error == 4551:
        return 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION'
    return None


def _windows_system_message(error: int, exception: BaseException) -> str:
    try:
        message = ctypes.FormatError(error).strip()
        if message:
            return message
    except (AttributeError, OSError, ValueError):
        pass
    return str(exception)


def format_engine_launch_failure(
        exception: BaseException,
        context: EngineLaunchContext,
        *,
        api_call: str = 'CreateProcessW') -> EngineStartupFailure:
    """Preserve a process-launch native error without relabeling its call site."""
    native_error = getattr(exception, 'winerror', None)
    error_domain = 'win32'
    if native_error is None:
        native_error = getattr(exception, 'errno', None)
        error_domain = 'os_error'

    if native_error is None:
        native_fields = {
            'api_call': api_call,
            'error_domain': error_domain,
            'native_error_signed': None,
            'native_error_unsigned': None,
            'native_error_hex': None,
            'win32_error_decimal': None,
            'symbolic_error': None,
            'system_message': str(exception),
        }
    else:
        signed_error = int(native_error)
        unsigned_error = signed_error & 0xFFFFFFFF
        win32_error = signed_error if error_domain == 'win32' else None
        native_fields = {
            'api_call': api_call,
            'error_domain': error_domain,
            'native_error_signed': signed_error,
            'native_error_unsigned': unsigned_error,
            'native_error_hex': f'0x{unsigned_error:08X}',
            'win32_error_decimal': win32_error,
            'symbolic_error': (
                _windows_symbolic_error_name(win32_error)
                if win32_error is not None else None),
            'system_message': (
                _windows_system_message(win32_error, exception)
                if win32_error is not None else str(exception)),
        }

    diagnostic = {
        'native_failure': native_fields,
        'startup_context': {
            'selected_monitor_id': context.selected_monitor_id,
            'dxgi_output': context.dxgi_output,
            'owning_adapter_luid': context.owning_adapter_luid,
            'capture_device_adapter_luid': context.capture_device_adapter_luid,
            'encoder_adapter_luid': context.encoder_adapter_luid,
            'capture_backend': context.capture_backend,
            'encoder_backend': context.encoder_backend,
            'codec': context.codec,
            'requested_capture_mode': context.requested_capture_mode,
            'requested_encoder': context.requested_encoder,
        },
    }
    detail = 'The operating system could not start the capture engine executable.'
    if native_fields['win32_error_decimal'] in (225, 226):
        detail = ('Windows reported an antivirus detection when starting the capture engine. '
                  'Check your antivirus protection history and send the detection name '
                  'and app version to FTHR support.')
    return EngineStartupFailure(
        code='ENGINE_PROCESS_LAUNCH_FAILED',
        title='CAPTURE ENGINE LAUNCH FAILED',
        detail=(
            detail + ' diagnostic='
            + json.dumps(diagnostic, ensure_ascii=False, separators=(',', ':'))
        ),
    )


def extract_startup_warnings(output: str) -> tuple[EngineStartupWarning, ...]:
    return tuple(
        EngineStartupWarning(
            code=match.group(1),
            title=match.group(1).replace('_', ' '),
            detail=match.group(2).strip()[:_MAX_DETAIL_LENGTH],
        )
        for match in _STARTUP_WARNING.finditer(output or '')
    )


def extract_startup_failure(output: str) -> EngineStartupFailure:
    """Return the last structured failure emitted by the native engine."""
    matches = list(_STARTUP_FAILURE.finditer(output or ''))
    if not matches:
        return EngineStartupFailure(
            code='ENGINE_START_FAILED',
            title='ENGINE COULD NOT START',
            detail='The capture engine stopped before connecting. Check the selected '
                   'monitor and hardware encoder, then restart capture.',
        )
    match = matches[-1]
    code = match.group(1)
    detail = match.group(2).strip()[:_MAX_DETAIL_LENGTH]
    return EngineStartupFailure(
        code=code,
        title=code.replace('_', ' '),
        detail=detail,
    )
