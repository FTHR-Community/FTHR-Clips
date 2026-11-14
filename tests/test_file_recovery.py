import errno
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from core import file_recovery
from core.clip_files import is_fthr_owned_partial_path
from core.file_recovery import (
    OutputPathTooLongError,
    next_available_output_path,
    retry_file_operation,
    windows_path_units,
)


def test_ordinary_name_and_collision_sequence_are_unchanged(tmp_path):
    original = tmp_path / 'FTHR_recording_2026-11-09_16-30-00.mp4'
    assert next_available_output_path(tmp_path, original.stem, '.mp4') == original

    original.write_bytes(b'existing')
    output = next_available_output_path(tmp_path, original.stem, '.mp4')
    assert output.name == f'{original.stem}_2.mp4'
    assert original.read_bytes() == b'existing'


def test_shortening_counts_utf16_preserves_marker_and_avoids_collisions(tmp_path):
    folder = tmp_path / 'Game'
    folder.mkdir()
    stem = '\U0001f3ae' * 24
    suffix = '_clip_from_09Nov2026_16-30-00'
    desired = folder / f'{stem}{suffix}.mp4'
    budget = windows_path_units(folder / f'{"x" * 16}{suffix}.mp4')
    assert len(str(desired)) < windows_path_units(desired)

    args = (tmp_path, stem, '.mp4')
    options = dict(generated_subdir='Game', stem_suffix=suffix, max_units=budget)
    first = next_available_output_path(*args, **options)
    assert first.parent == folder
    assert windows_path_units(first) <= budget
    assert is_fthr_owned_partial_path(first.with_suffix('.mp4.partial'))
    assert first.name.endswith(suffix + '.mp4')
    assert next_available_output_path(*args, **options) == first

    first.write_bytes(b'existing')
    second = next_available_output_path(*args, **options)
    assert second.parent == first.parent
    assert second != first
    assert windows_path_units(second) <= budget
    assert second.name.endswith(suffix + '.mp4')
    assert first.read_bytes() == b'existing'


def test_shortens_only_explicit_generated_child_and_preserves_distinct_groups(tmp_path):
    child = ('Long game name ' * 3).rstrip()
    budget = windows_path_units(tmp_path / ('x' * 20) / ('x' * 15 + '.mp4'))
    first = next_available_output_path(
        tmp_path, 'Generated clip filename', '.mp4', generated_subdir=child,
        max_units=budget,
    )
    second = next_available_output_path(
        tmp_path, 'Generated clip filename', '.mp4', generated_subdir=child + 'variant',
        max_units=budget,
    )
    assert first.parent.parent == second.parent.parent == tmp_path
    assert first.parent != tmp_path / child
    assert first.parent != second.parent
    assert windows_path_units(first) <= budget

    with pytest.raises(OutputPathTooLongError):
        next_available_output_path(
            tmp_path / child, 'Generated clip filename', '.mp4', max_units=budget,
        )


def test_default_limit_is_255_utf16_units():
    root = Path.cwd().anchor
    budget = 255 - windows_path_units(Path(root) / 'x') + 1
    fitting = 'x' * (budget - 4)
    assert next_available_output_path(root, fitting, '.mp4').stem == fitting
    shortened = next_available_output_path(root, fitting + '\U0001f3ae', '.mp4')
    assert windows_path_units(shortened) <= 255


def test_compact_name_uses_remaining_space_and_handles_exhausted_names(tmp_path):
    spare = tmp_path / 'f.mp4'
    budget = windows_path_units(spare)
    for token in '0123456789abcde':
        (tmp_path / f'{token}.mp4').write_bytes(b'existing')

    assert next_available_output_path(tmp_path, 'Recording', '.mp4', max_units=budget) == spare
    spare.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        next_available_output_path(tmp_path, 'Recording', '.mp4', max_units=budget)
    with pytest.raises(OutputPathTooLongError):
        next_available_output_path(tmp_path, 'Recording', '.mp4', max_units=budget - 1)


@pytest.mark.parametrize('name', ['../outside', '..\\outside', 'C:outside', '/outside', '..'])
def test_generated_child_cannot_escape_configured_root(tmp_path, name):
    with pytest.raises(ValueError, match='single path components'):
        next_available_output_path(tmp_path, 'recording', '.mp4', generated_subdir=name)


def _native_error(code, posix_code=errno.EACCES):
    error = OSError(posix_code, 'simulated native failure')
    error.winerror = code
    return error


def _wrapped_native_error(code):
    error = ValueError('could not inspect media identity')
    error.__cause__ = _native_error(code)
    return error


@pytest.fixture
def pause(monkeypatch):
    sleeper = Mock()
    monkeypatch.setattr(file_recovery.time, 'sleep', sleeper)
    return sleeper


@pytest.mark.parametrize(
    'failure',
    [_native_error(32), _native_error(33), OSError(errno.EBUSY, 'busy'),
     OSError(errno.ETXTBSY, 'text busy'), _wrapped_native_error(32)],
)
def test_retries_transient_lock_and_returns_original_result(failure, pause):
    operation = Mock(side_effect=[failure, failure, 'published'])
    assert retry_file_operation(operation, 'staged', 'final', replace=False) == 'published'
    assert operation.call_args_list == [call('staged', 'final', replace=False)] * 3
    assert pause.call_args_list == [call(0.02), call(0.04)]


@pytest.mark.parametrize(
    'failure',
    [OSError(errno.EACCES, 'denied'), OSError(errno.EPERM, 'forbidden'),
     OSError(errno.ENOSPC, 'full'), OSError(errno.ENOENT, 'missing'),
     OSError(errno.EEXIST, 'collision'), _native_error(5, errno.EBUSY),
     _native_error(112), _native_error(206), _wrapped_native_error(5),
     ValueError('media identity changed')],
)
def test_immediately_propagates_permanent_failure(failure, pause):
    operation = Mock(side_effect=failure)
    with pytest.raises(type(failure)) as raised:
        retry_file_operation(operation)
    assert raised.value is failure
    assert operation.call_count == 1
    pause.assert_not_called()


def test_propagates_latest_lock_error_after_bounded_retries(pause):
    last_failure = _native_error(33)
    operation = Mock(side_effect=[_native_error(32)] * 3 + [last_failure])
    with pytest.raises(OSError) as raised:
        retry_file_operation(operation)
    assert raised.value is last_failure
    assert operation.call_count == 4
    assert sum(attempt.args[0] for attempt in pause.call_args_list) == pytest.approx(0.12)
