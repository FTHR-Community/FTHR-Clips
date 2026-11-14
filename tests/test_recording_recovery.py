import json
import struct
import threading
from pathlib import Path

import pytest

from core import recording_recovery as recovery


def _box(kind, payload, *, extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def _validation(*, duration='3.25', frames=None, errors=b''):
    return (0, json.dumps({
        'frames': [{'key_frame': 1}] if frames is None else frames,
        'streams': [{'duration': duration}],
    }).encode(), errors)


@pytest.fixture
def fragmented_recording(tmp_path):
    source = tmp_path / 'Recording.mp4'
    header = _box(b'ftyp', b'isom') + _box(b'moov', b'init')
    fragment = _box(b'moof', b'fragment') + _box(b'mdat', b'frames', extended=True)
    torn_fragment = _box(b'moof', b'next') + _box(b'mdat', b'half written')[:-6]
    source.write_bytes(header + fragment + torn_fragment)
    return source, len(header + fragment)


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(recovery, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(recovery, 'get_ffprobe_exe', lambda: 'ffprobe')


def test_requires_writer_close_proof(fragmented_recording, monkeypatch):
    source, _ = fragmented_recording

    def unexpected_read(_path):
        pytest.fail('An active or uncertain recording must never be opened.')

    monkeypatch.setattr(recovery, 'media_identity', unexpected_read)
    assert recovery.recover_manual_recording(source, writer_closed=False) is None


def test_no_complete_fragment_does_not_launch_ffmpeg(tmp_path, monkeypatch):
    source = tmp_path / 'Incomplete.mp4'
    data = _box(b'moov', b'init') + _box(b'moof', b'fragment')
    source.write_bytes(data)

    def unexpected_process(*_args, **_kwargs):
        pytest.fail('A header without committed media is not recoverable.')

    monkeypatch.setattr(recovery, 'run_media_process', unexpected_process)
    assert recovery.recover_manual_recording(source, writer_closed=True) is None
    assert source.read_bytes() == data


def test_refuses_replacement_at_queued_source_path(fragmented_recording, monkeypatch):
    source, _ = fragmented_recording
    expected = recovery.media_identity(source)
    source.write_bytes(source.read_bytes() + b'replacement bytes')

    def unexpected_scan(*_args, **_kwargs):
        pytest.fail('A queued attempt must stay bound to its original source.')

    monkeypatch.setattr(recovery, '_complete_fragment_end', unexpected_scan)
    assert recovery.recover_manual_recording(
        source, writer_closed=True, expected_identity=expected) is None


def test_recovers_only_complete_fragments_without_overwriting(
        fragmented_recording, monkeypatch, tools):
    source, complete_end = fragmented_recording
    source_bytes = source.read_bytes()
    existing = source.with_name('Recording (recovered).mp4')
    existing.write_bytes(b'existing recording')
    sidecar = source.with_suffix('.json')
    sidecar.write_bytes(b'original metadata')
    commands = []

    def run(command, cancel, *, timeout_seconds):
        assert not cancel.is_set()
        assert 0 < timeout_seconds <= 90
        commands.append(command)
        staged = Path(command[-1])
        assert staged.parent == source.parent and staged.suffix == '.tmp'
        if command[0] == 'ffmpeg':
            assert command[command.index('-i') + 1] == (
                f'subfile,,start,0,end,{complete_end},,:file:{source}')
            assert command[command.index('-c') + 1] == 'copy'
            assert '0:a?' in command
            staged.write_bytes(b'copied video and audio')
            return 0, b'', b''
        assert timeout_seconds <= 10
        assert '%+#120' in command
        assert staged.read_bytes() == b'copied video and audio'
        return _validation()

    monkeypatch.setattr(recovery, 'run_media_process', run)
    result = recovery.recover_manual_recording(source, writer_closed=True)

    assert result == recovery.RecoveredRecording(
        source.with_name('Recording (recovered)_2.mp4'), 3.25)
    assert result.path.read_bytes() == b'copied video and audio'
    assert source.read_bytes() == source_bytes
    assert existing.read_bytes() == b'existing recording'
    assert sidecar.read_bytes() == b'original metadata'
    assert [command[0] for command in commands] == ['ffmpeg', 'ffprobe']
    assert not list(source.parent.glob('.fr-*'))


def test_publication_fits_path_budget_and_retries_sharing_violation(tmp_path, monkeypatch):
    from core.file_recovery import WINDOWS_CAPTURE_PATH_UNITS, windows_path_units

    stem_length = WINDOWS_CAPTURE_PATH_UNITS - windows_path_units(tmp_path / 'x.mp4')
    source = tmp_path / ('N' * stem_length + '.mp4')
    staged = tmp_path / '.fr-staged.tmp'
    staged.write_bytes(b'valid staged bytes')
    retry = recovery.retry_file_operation
    attempts = []

    def transient_once(operation, *args):
        def replace(*operation_args):
            attempts.append(operation_args)
            if len(attempts) == 1:
                import errno
                raise OSError(errno.EBUSY, 'Busy file')
            return operation(*operation_args)
        return retry(replace, *args)

    monkeypatch.setattr(recovery, 'retry_file_operation', transient_once)
    final = recovery._publish_recovery(staged, source)

    assert len(attempts) == 2
    assert windows_path_units(final) <= WINDOWS_CAPTURE_PATH_UNITS
    assert final.name.endswith(' (recovered).mp4')
    assert final.read_bytes() == b'valid staged bytes'


@pytest.mark.parametrize('failure', [
    'remux-error', 'timeout', 'empty-output', 'invalid-json', 'no-frame',
    'non-keyframe', 'invalid-duration', 'decoder-error', 'source-changed', 'cancelled',
])
def test_failed_or_invalid_recovery_does_not_publish(
        fragmented_recording, monkeypatch, tools, failure):
    source, _ = fragmented_recording
    source_bytes = source.read_bytes()
    cancel = threading.Event()

    def run(command, event, **_kwargs):
        assert event is cancel
        if command[0] == 'ffmpeg':
            if failure != 'empty-output':
                Path(command[-1]).write_bytes(b'partially recovered')
            if failure == 'remux-error':
                return 1, b'', b'disk full'
            if failure == 'timeout':
                return None
            return 0, b'', b''
        assert failure not in ('remux-error', 'timeout', 'empty-output')
        if failure == 'invalid-json':
            return 0, b'invalid', b''
        if failure == 'no-frame':
            return _validation(frames=[])
        if failure == 'non-keyframe':
            return _validation(frames=[{'key_frame': 0}])
        if failure == 'invalid-duration':
            return _validation(duration='inf')
        if failure == 'decoder-error':
            return _validation(errors=b'Invalid video bitstream')
        if failure == 'source-changed':
            source.write_bytes(source_bytes + b'unrelated mutation')
        if failure == 'cancelled':
            cancel.set()
        return _validation()

    monkeypatch.setattr(recovery, 'run_media_process', run)
    assert recovery.recover_manual_recording(
        source, writer_closed=True, cancel_event=cancel) is None
    assert list(source.parent.iterdir()) == [source]
    if failure != 'source-changed':
        assert source.read_bytes() == source_bytes
