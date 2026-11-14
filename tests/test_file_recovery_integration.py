import errno
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import file_recovery
from core.capture_highlights import media_identity


def _sharing_violation():
    error = OSError(errno.EACCES, 'simulated Windows sharing violation')
    error.winerror = 32
    return error


@pytest.fixture
def owned_output(tmp_path, monkeypatch):
    import main

    source = tmp_path / 'staged.mp4'
    destination = tmp_path / 'clip.mp4'
    source.write_bytes(b'processed clip')
    destination.write_bytes(b'original clip')
    job = {'owned_identity': media_identity(destination)}
    monkeypatch.setattr(main._FINALIZATION_CONTEXT, 'capture_job', job, raising=False)
    replace = main.MainWindow._replace_finalization_output.__get__(SimpleNamespace())
    return replace, source, destination, job


def test_finalization_retries_sharing_violation_and_updates_owned_identity(
        owned_output, monkeypatch):
    replace, source, destination, job = owned_output
    staged_identity = media_identity(source)
    commit = Mock(side_effect=[_sharing_violation(), None])
    real_replace = os.replace
    pause = Mock()

    def replace_after_lock(src, dst):
        commit(src, dst)
        real_replace(src, dst)

    monkeypatch.setattr(os, 'replace', replace_after_lock)
    monkeypatch.setattr(file_recovery.time, 'sleep', pause)

    assert replace(str(source), str(destination)) == staged_identity
    assert commit.call_count == 2
    pause.assert_called_once()
    assert destination.read_bytes() == b'processed clip'
    assert not source.exists()
    assert job['owned_identity'] == job['final_identity'] == media_identity(destination)


@pytest.mark.parametrize('changed_member', ['staged', 'destination'])
def test_finalization_rechecks_both_file_identities_before_retry(
        owned_output, tmp_path, monkeypatch, changed_member):
    replace, source, destination, job = owned_output
    owned_identity = job['owned_identity']
    changed_file = source if changed_member == 'staged' else destination
    foreign_file = tmp_path / 'unowned.mp4'
    foreign_file.write_bytes(b'independently installed clip')
    real_replace = os.replace
    commit = Mock(side_effect=_sharing_violation())
    monkeypatch.setattr(os, 'replace', commit)
    monkeypatch.setattr(
        file_recovery.time, 'sleep', lambda _delay: real_replace(foreign_file, changed_file),
    )

    with pytest.raises(OSError, match='ownership changed'):
        replace(str(source), str(destination))

    commit.assert_called_once_with(str(source), str(destination))
    assert changed_file.read_bytes() == b'independently installed clip'
    if changed_member == 'staged':
        assert destination.read_bytes() == b'original clip'
    else:
        assert source.read_bytes() == b'processed clip'
    assert job['ownership_invalid'] is True
    assert job['owned_identity'] == owned_identity
    assert 'final_identity' not in job


def test_manual_recording_hands_shortened_path_to_start(tmp_path, monkeypatch):
    import main

    child_units = 235 - file_recovery.windows_path_units(tmp_path / 'x') + 1
    root = tmp_path / ('x' * child_units) if child_units > 0 else tmp_path
    monkeypatch.setattr(main, 'recording_directory_from', lambda _settings: root)
    monkeypatch.setattr(main.shutil, 'disk_usage', lambda _root: SimpleNamespace(free=2**30))
    request = SimpleNamespace(
        source=SimpleNamespace(
            source_mode='desktop', exe_path='', live_identity=None,
            authority='manual', monitor_identity=None,
        ),
        camera=SimpleNamespace(enabled=False, device_index=None),
        capture_config=object(),
        mic_gain_percent=100,
    )
    host = SimpleNamespace(
        settings_manager=object(),
        _admission_transition_active=lambda: False,
        _admission_guard_current_session=lambda: True,
        _save_state=SimpleNamespace(is_busy=lambda: False),
        _capture_resources_busy=lambda: False,
        bridge=SimpleNamespace(is_connected=lambda: True, get_status=lambda: {}),
        _accepted_session_owner_is_current=lambda: True,
        _accepted_capture_session=SimpleNamespace(request=request),
        _storage_admission=lambda _path: (True, ''),
        _prepare_next_capture_request=lambda _purpose: request,
        _rebind_equivalent_capture_request=lambda *_args: True,
        push_error=Mock(),
    )
    submitted = []

    def submit():
        submitted.append(host._pending_manual_record_path)
        host._manual_record_state = 'starting'

    host._submit_manual_recording_start = submit

    main.MainWindow._start_manual_recording(host)

    host.push_error.assert_not_called()
    assert len(submitted) == 1
    output = submitted[0]
    assert output.parent == root
    assert root.is_dir()
    assert output.suffix == '.mp4'
    assert file_recovery.windows_path_units(output) <= file_recovery.WINDOWS_CAPTURE_PATH_UNITS
