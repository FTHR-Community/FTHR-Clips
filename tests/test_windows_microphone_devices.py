from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.windows_microphone_devices import (
    MicrophoneDiscoveryCancelled,
    MicrophoneDiscoveryJob,
    MicrophoneDiscoveryTimeout,
    MicrophoneDiscoveryResult,
    WindowsMicrophoneEndpoint,
    discovery_diagnostic_code,
    discovery_result_event_fields,
    discover_microphones,
    list_native_microphones,
    migrate_legacy_microphone_name,
    parse_microphone_inventory,
)


def _inventory() -> str:
    return '''{"schema_version":1,"microphones":[
        {"endpoint_id":"default-id","display_name":"FIFINE","device_state":1,"is_default":true},
        {"endpoint_id":"disabled-id","display_name":"FIFINE","device_state":2,"is_default":false},
        {"endpoint_id":"camo-id","display_name":"Camo","device_state":1,"is_default":false}
    ]}'''


def test_native_inventory_preserves_stable_id_and_state():
    endpoints = parse_microphone_inventory(_inventory())

    assert [(item.endpoint_id, item.is_active, item.is_default) for item in endpoints] == [
        ('default-id', True, True),
        ('disabled-id', False, False),
        ('camo-id', True, False),
    ]


def test_legacy_name_migrates_only_when_one_active_native_endpoint_matches():
    endpoints = parse_microphone_inventory(_inventory())

    assert migrate_legacy_microphone_name('Camo', endpoints) == 'camo-id'
    assert migrate_legacy_microphone_name('FIFINE', endpoints) == 'default-id'
    assert migrate_legacy_microphone_name('Missing', endpoints) is None


def test_duplicate_active_legacy_names_do_not_bind_to_a_random_device():
    endpoints = parse_microphone_inventory(
        '''{"schema_version":1,"microphones":[
            {"endpoint_id":"one","display_name":"Same","device_state":1,"is_default":false},
            {"endpoint_id":"two","display_name":"Same","device_state":1,"is_default":false}
        ]}''')

    assert migrate_legacy_microphone_name('Same', endpoints) is None


def test_engine_inventory_is_invoked_with_a_bounded_helper_mode(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=_inventory(), stderr='')

    endpoints = list_native_microphones(tmp_path / 'FTHRclips.exe', runner=runner)

    assert endpoints[0].endpoint_id == 'default-id'
    assert calls[0][0][-1] == '--list-microphones'
    assert calls[0][1]['timeout'] == 5


def test_invalid_native_inventory_is_rejected():
    with pytest.raises(ValueError, match='valid JSON'):
        parse_microphone_inventory('not json')


@pytest.mark.parametrize('payload', [
    '{"schema_version":1,"microphones":[42]}',
    '{"schema_version":1,"microphones":[{"endpoint_id":"a","display_name":"A","device_state":1},{"endpoint_id":"a","display_name":"Again","device_state":1}]}',
    '{"schema_version":1,"microphones":[{"endpoint_id":"a","display_name":"A","device_state":true}]}',
    '{"schema_version":true,"microphones":[]}',
    '{"schema_version":1.0,"microphones":[]}',
    '{"schema_version":1,"microphones":[{"endpoint_id":"a","display_name":"A","device_state":1,"is_default":"yes"}]}',
])
def test_strict_inventory_rejects_incomplete_or_ill_typed_rows(payload):
    with pytest.raises(ValueError):
        parse_microphone_inventory(payload, strict=True)


def test_strict_inventory_accepts_a_successful_empty_endpoint_list():
    assert parse_microphone_inventory(
        '{"schema_version":1,"microphones":[]}', strict=True) == []


def test_default_microphone_inventory_parser_keeps_legacy_lenient_behavior():
    payload = '{"schema_version":true,"microphones":[' \
        '{"endpoint_id":"legacy","display_name":"Mic",' \
        '"device_state":true,"is_default":"yes"},42]}'
    endpoints = parse_microphone_inventory(payload)

    assert [(item.endpoint_id, item.device_state, item.is_default)
            for item in endpoints] == [('legacy', True, True)]


class _HangingProcess:
    def __init__(self, *_args, **_kwargs):
        self.returncode = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.killed:
            self.waited = True
            self.returncode = -9
            return '', ''
        raise subprocess.TimeoutExpired('engine', timeout)

    def kill(self):
        self.killed = True


def test_native_timeout_kills_and_reaps_helper_process():
    processes = []

    def popen(*args, **kwargs):
        process = _HangingProcess(*args, **kwargs)
        processes.append(process)
        return process

    with pytest.raises(MicrophoneDiscoveryTimeout, match='timed out'):
        list_native_microphones(
            'FTHRclips.exe', popen_factory=popen, timeout=0.01)

    assert processes[0].killed is True
    assert processes[0].waited is True


def test_native_cancellation_kills_and_reaps_helper_process():
    cancel = threading.Event()
    processes = []

    class _CancellingProcess(_HangingProcess):
        def communicate(self, timeout=None):
            if not self.killed:
                cancel.set()
            return super().communicate(timeout)

    def popen(*args, **kwargs):
        process = _CancellingProcess(*args, **kwargs)
        processes.append(process)
        return process

    with pytest.raises(MicrophoneDiscoveryCancelled):
        list_native_microphones(
            'FTHRclips.exe', popen_factory=popen,
            timeout=1, cancel_event=cancel)

    assert processes[0].killed is True
    assert processes[0].waited is True


def test_discovery_cancellation_stops_before_legacy_query():
    cancel = threading.Event()
    cancel.set()
    legacy_called = False

    def legacy_query():
        nonlocal legacy_called
        legacy_called = True
        return []

    with pytest.raises(MicrophoneDiscoveryCancelled):
        discover_microphones(
            None, legacy_query=legacy_query, cancel_event=cancel)

    assert legacy_called is False


def test_legacy_inventory_timeout_is_bounded_without_blocking_caller():
    started = threading.Event()

    def legacy_query():
        started.set()
        time.sleep(0.2)
        return []

    start = time.monotonic()
    result = discover_microphones(
        None, legacy_query=legacy_query, timeout=0.01)
    elapsed = time.monotonic() - start

    assert started.is_set()
    assert elapsed < 0.15
    assert result.legacy_error is not None
    assert 'timed out' in result.legacy_error


def test_discovery_job_reports_cancelled_generation_without_publishing_result():
    finished = []
    started = threading.Event()
    release = threading.Event()

    def worker(_cancel_event):
        started.set()
        release.wait(1)
        raise MicrophoneDiscoveryCancelled('superseded')

    job = MicrophoneDiscoveryJob(None, worker=worker)
    job.start(lambda result, error: finished.append((result, error)))
    assert started.wait(1)
    job.cancel()
    release.set()
    assert job.wait(1)
    assert isinstance(finished[0][1], MicrophoneDiscoveryCancelled)
    assert job.result is None


def test_discovery_result_is_immutable_for_stale_ui_consumers():
    result = MicrophoneDiscoveryResult(
        native_endpoints=(), legacy_indices={'Mic': (1,)},
        native_error=None, legacy_error=None, generation=4)
    assert result.generation == 4
    with pytest.raises(TypeError):
        result.legacy_indices['Mic'] = (2,)


def test_discovery_error_mapping_is_stable_and_suppresses_cancellation():
    assert discovery_diagnostic_code(
        MicrophoneDiscoveryTimeout('native timed out')) == (
            'AUDIO_ENDPOINT_SCAN_TIMEOUT')
    assert discovery_diagnostic_code(
        MicrophoneDiscoveryCancelled('superseded')) is None
    assert discovery_diagnostic_code(
        RuntimeError('helper failed')) == 'AUDIO_ENDPOINT_NOT_FOUND'


def test_discovery_event_fields_are_bounded_and_do_not_contain_endpoint_ids():
    fields = discovery_result_event_fields(MicrophoneDiscoveryResult(
        native_endpoints=(
            WindowsMicrophoneEndpoint('secret-endpoint-id', 'USB Mic', 1, True),
            WindowsMicrophoneEndpoint('disabled-id', 'Old Mic', 2, False),
        ),
        legacy_indices={'USB Mic': (3,), 'Other': (4, 5)},
        native_error='ignored raw error', legacy_error=None, generation=7))

    assert fields == {
        'generation': 7,
        'native_endpoint_count': 2,
        'active_native_endpoint_count': 1,
        'legacy_endpoint_count': 2,
        'native_scan_ok': False,
        'legacy_scan_ok': True,
    }
    assert 'secret-endpoint-id' not in repr(fields)


def test_settings_scan_does_not_call_device_apis_synchronously():
    source = (Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'main.py'
              ).read_text(encoding='utf-8')
    start = source.index('    def _populate_mic_devices(self):')
    end = source.index('    def _cancel_mic_discovery', start)
    body = source[start:end]

    assert 'MicrophoneDiscoveryJob(' in body
    assert 'query_devices()' not in body
    assert 'list_native_microphones(' not in body
    assert 'sys.platform != \'win32\'' in body
