from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest

from core import export_encoding as encoding, export_queue as queue
from core.export_lifecycle import ExportResult, ExportState
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
from core.library_index import LibraryRecord, canonical_media_path


@pytest.fixture
def isolated_admission(monkeypatch):
    # New tests own these module globals and join their own worker threads.
    for name, value in {
            '_path_claims': {}, '_local_mutations': set(), '_mutation_tokens': {}, '_records': [],
            '_waiting': [], '_active': 0, '_limit': 1, '_initializing': False}.items():
        monkeypatch.setattr(queue, name, value)
    monkeypatch.setattr(queue, 'emit_event', lambda *args, **kwargs: None)


def _original_record(path):
    info = path.stat()
    return LibraryRecord(str(path), canonical_media_path(path), (), False, 'video',
                         info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)


@pytest.mark.parametrize('backend', ['export', 'upload'])
def test_owned_reservation_release_keeps_foreign_target_after_resolver_changes(
        isolated_admission, qapp, tmp_path, monkeypatch, backend):
    from types import SimpleNamespace
    from core import upload_manager

    source, target = tmp_path / 'source.mp4', tmp_path / 'foreign.mp4'
    source.write_bytes(b'original source')
    target.write_bytes(b'foreign target')
    (tmp_path / 'alias').mkdir()
    stable_source_alias = str(tmp_path / 'alias' / '..' / source.name)
    manager = None
    if backend == 'export':
        module, acquire, release, legacy_end = (
            queue, queue.acquire_local_mutations, queue.release_local_mutations,
            queue.end_local_mutations)
    else:
        monkeypatch.setattr(upload_manager.UploadManager, '_load_settings', lambda self: {})
        manager = upload_manager.UploadManager(SimpleNamespace(get=lambda key, default=None: default))
        monkeypatch.setattr(manager, 'is_uploaded', lambda path: False)
        module, acquire, release, legacy_end = (
            upload_manager, manager.acquire_local_mutations, manager.release_local_mutations,
            manager.end_local_mutations)
    source_token, target_token = acquire([str(source)]), acquire([str(target)])
    assert source_token is not None and target_token is not None
    assert source_token.keys == frozenset({canonical_media_path(source)})
    original, resolved = module.canonical_media_path, []

    def redirected(path):
        resolved.append(str(path))
        # Deterministic reparse/alias resolution fault without requiring junction
        # privileges or changing real files: source now resolves to foreign target.
        return original(target if str(path) == str(source) else path)

    try:
        monkeypatch.setattr(module, 'canonical_media_path', redirected)
        release(source_token)
        release(source_token)
        release(queue.LocalMutationReservation(target_token.keys))
        assert resolved == [], 'release must use captured ownership keys without realpath'
        legacy_end([str(source), str(target)])
        assert acquire([str(target)]) is None
        assert acquire([str(source)]) is None
        available = acquire([stable_source_alias])
        assert available is not None and available.keys == source_token.keys
        release(available)
        if backend == 'export':
            with pytest.raises(RuntimeError):
                queue._claim_paths([str(target)])
        else:
            manager._enqueue(str(target), threading.Event())
            assert manager._queue.empty() and not manager._in_flight
        assert source.read_bytes() == b'original source'
        assert target.read_bytes() == b'foreign target'
    finally:
        release(source_token)
        release(target_token)
        if manager is not None:
            manager.stop()


def test_upload_and_export_tokens_cannot_release_each_others_claims(
        isolated_admission, qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core import upload_manager

    source = tmp_path / 'shared.mp4'
    source.write_bytes(b'held by independent owners')
    monkeypatch.setattr(upload_manager.UploadManager, '_load_settings', lambda self: {})
    manager = upload_manager.UploadManager(SimpleNamespace(get=lambda key, default=None: default))
    monkeypatch.setattr(manager, 'is_uploaded', lambda path: False)
    export = queue.acquire_local_mutations([source])
    upload = manager.acquire_local_mutations([source])
    assert export is not None and upload is not None and export is not upload
    try:
        queue.release_local_mutations(upload)
        manager.release_local_mutations(export)
        queue.end_local_mutations([source])
        manager.end_local_mutations([source])
        assert queue.acquire_local_mutations([source]) is None
        assert manager.acquire_local_mutations([source]) is None
        with pytest.raises(RuntimeError):
            queue._claim_paths([source])
        manager._enqueue(str(source), threading.Event())
        assert manager._queue.empty()
    finally:
        queue.release_local_mutations(export)
        manager.release_local_mutations(upload)
        manager.stop()


def test_original_batch_waits_in_shared_fifo_and_claims_primary_and_possible_sidecar(
        isolated_admission, monkeypatch, tmp_path):
    from core import original_export
    from core.audio_manifest import manifest_path_for

    source = tmp_path / 'original.mp4'
    source.write_bytes(b'original bytes')
    encoded_source = tmp_path / 'editor.mp4'
    encoded_source.write_bytes(b'editor bytes')
    output = tmp_path / 'output'
    output.mkdir()
    prepared = threading.Event()
    order = []
    real_prepare, real_export = original_export.prepare_originals, original_export.export_originals

    def prepare(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        prepared.set()
        return result

    def export(*args, **kwargs):
        assert queue._active == 1
        order.append('originals')
        return real_export(*args, **kwargs)

    class EncodedJob:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            assert queue._active == 1
            order.append('encoded')
            return ExportResult(ExportState.COMPLETED)

    monkeypatch.setattr(original_export, 'prepare_originals', prepare)
    monkeypatch.setattr(original_export, 'export_originals', export)
    # Preserve run_export's real shared-slot admission; only the subprocess
    # job is synthetic, so no network, media encoder or hardware is launched.
    monkeypatch.setattr(queue, 'ExportJob', EncodedJob)
    tasks = []
    try:
        with queue.export_slot(threading.Event()) as admitted:
            assert admitted
            task = queue.submit_originals((_original_record(source),), output)
            tasks.append(task)
            assert prepared.wait(3)
            deadline = time.monotonic() + 3
            while len(queue._waiting) < 1 and time.monotonic() < deadline:
                time.sleep(.005)
            assert len(queue._waiting) == 1
            assert not queue.begin_local_mutations([source])
            assert not queue.begin_local_mutations([manifest_path_for(source)])
            encoded = queue.submit(encoded_source, output, 'quality',
                                   lambda path: ['synthetic-ffmpeg', path], 1, None)
            tasks.append(encoded)
            assert order == []
        for task in tasks:
            task.thread.join(5)
            assert not task.thread.is_alive() and task.done
        assert order == ['originals', 'encoded']
        assert tasks[0].label == '1 originals'
        assert tasks[0].result.successes == (str(source),)
        assert not queue._path_claims and queue._active == 0
    finally:
        for task in tasks:
            task.cancel.set()
            task.thread.join(5)


def test_queued_original_replacement_is_not_copied_and_all_claims_are_released(
        isolated_admission, monkeypatch, tmp_path):
    from core import original_export

    source = tmp_path / 'queued.mp4'
    source.write_bytes(b'original queued bytes')
    record = _original_record(source)
    output = tmp_path / 'output'
    output.mkdir()
    prepared = threading.Event()
    prepare = original_export.prepare_originals

    def freeze(*args, **kwargs):
        result = prepare(*args, **kwargs)
        prepared.set()
        return result

    monkeypatch.setattr(original_export, 'prepare_originals', freeze)
    task = None
    try:
        with queue.export_slot(threading.Event()):
            task = queue.submit_originals((record,), output)
            assert prepared.wait(3)
            source.rename(tmp_path / 'retained-queued-original.mp4')
            source.write_bytes(b'replacement bytes')
        task.thread.join(5)
        assert not task.thread.is_alive()
        assert task.status == 'Failed' and task.result.output is None
        assert [path for path, error in task.result.failures] == [str(source)]
        assert not queue._path_claims
        assert queue.begin_local_mutations([source])
        queue.end_local_mutations([source])
    finally:
        if task is not None:
            task.cancel.set()
            task.thread.join(5)


def test_original_export_thread_start_failure_is_terminal_and_shutdown_safe(
        isolated_admission, monkeypatch, tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')

    def fail_start(*args, **kwargs):
        raise OSError('synthetic originals thread start failure')

    monkeypatch.setattr(queue.threading.Thread, 'start', fail_start)
    with pytest.raises(OSError):
        queue.submit_originals((_original_record(source),), tmp_path)
    task = queue.tasks()[-1]
    assert task.done and task.status == 'Failed' and task.thread is None
    assert 'start failure' in task.detail
    assert not queue._path_claims
    queue.shutdown(timeout=.1)


@pytest.mark.parametrize('ready', [False, 'true', 1])
def test_original_export_rejects_non_ready_snapshot_without_losing_reader_claims(
        isolated_admission, tmp_path, ready):
    source = tmp_path / 'not-ready.mp4'
    source.write_bytes(b'not ready')
    task = queue.submit_originals((_original_record(source),), tmp_path,
                                  readiness={str(source): ready})
    task.thread.join(5)
    assert task.done and not task.thread.is_alive()
    assert task.status == 'Failed' and task.result.successes == ()
    assert [path for path, error in task.result.failures] == [str(source)]
    assert not queue._path_claims and queue._active == 0


def test_shutdown_cancels_running_original_copy_and_releases_every_claim(
        isolated_admission, monkeypatch, tmp_path):
    from core import original_export

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    entered = threading.Event()
    export = original_export.export_originals

    def wait_for_shutdown(*args, **kwargs):
        entered.set()
        assert kwargs['cancel'].wait(3)
        return export(*args, **kwargs)

    monkeypatch.setattr(original_export, 'export_originals', wait_for_shutdown)
    task = queue.submit_originals((_original_record(source),), tmp_path)
    assert entered.wait(3)
    queue.shutdown(timeout=5)
    assert task.done and not task.thread.is_alive() and task.status == 'Cancelled'
    assert task.result.cancelled and task.result.output is None
    assert not queue._path_claims and queue._active == 0


def test_original_export_preserves_all_named_failures_but_bounds_task_detail(
        isolated_admission, tmp_path):
    from dataclasses import replace

    source = tmp_path / 'missing-evidence.mp4'
    source.write_bytes(b'original')
    original = _original_record(source)
    records = tuple(replace(original, path=str(tmp_path / f'clip-{index:02}.mp4'),
                            identity=str(index), st_ino=None) for index in range(25))
    task = queue.submit_originals(records, tmp_path)
    task.thread.join(5)
    assert task.done and not task.thread.is_alive()
    assert len(task.result.failures) == 25
    assert {path for path, error in task.result.failures} == {record.path for record in records}
    assert len(task.detail.splitlines()) <= 12
    assert 'clip-00.mp4' in task.detail and 'clip-09.mp4' in task.detail
    assert 'clip-24.mp4' not in task.detail and '15' in task.detail
    assert not queue._path_claims


def test_hardware_probe_failure_uses_software(monkeypatch):
    encoding._working.clear()
    monkeypatch.setattr(encoding.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 1})())
    monkeypatch.setattr(encoding, 'software_video_args', lambda *a: ['-c:v', 'libopenh264'])
    assert encoding.choose_video_args('fake', 'fast')[1] == 'libopenh264'
    encoding._working.clear()


def test_failed_hardware_job_retries_and_cancel_never_retries(monkeypatch, tmp_path):
    commands = []
    cancel = threading.Event()
    monkeypatch.setattr(queue, 'choose_video_args', lambda *a, **k: ['-c:v', 'h264_nvenc'])
    monkeypatch.setattr(queue, 'software_args', lambda *a: ['-c:v', 'libopenh264'])
    class Job:
        def __init__(self, cmd, *a, **k):
            commands.append(cmd)
        def run(self, **kwargs):
            return ExportResult(ExportState.FAILED if len(commands) == 1 else ExportState.COMPLETED)
    monkeypatch.setattr(queue, 'ExportJob', Job)
    args = (['fake', *encoding.VIDEO_PLACEHOLDER, str(tmp_path / 'partial.mp4')],
            tmp_path / 'partial.mp4', tmp_path / 'final.mp4')
    result = queue.run_export(*args, quality='quality', duration=1, cancel=cancel, validate=None)
    assert result.succeeded and len(commands) == 2
    assert 'h264_nvenc' in commands[0] and 'libopenh264' in commands[1]
    cancel.set()
    result = queue.run_export(*args, quality='quality', duration=1, cancel=cancel, validate=None)
    assert result.state == ExportState.CANCELLED and len(commands) == 2


def test_parallel_limit_and_live_disable(monkeypatch):
    queue.configure({'parallel_exports': True, 'parallel_export_limit': 2})
    release = threading.Event()
    entered = []
    cancels = [threading.Event() for _ in range(3)]
    def work(i):
        with queue.export_slot(cancels[i]) as admitted:
            if admitted:
                entered.append(i)
                release.wait(3)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while len(entered) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(entered) == 2
    queue.configure({'parallel_exports': False})
    # Cancelling a queued task must not consume a slot or launch a process.
    waiting = next(i for i in range(3) if i not in entered)
    cancels[waiting].set()
    threads[waiting].join(2)
    assert not threads[waiting].is_alive()
    release.set()
    for thread in threads:
        thread.join(3)
    assert len(entered) == 2 and queue._active == 0


def test_submission_freezes_command_and_unique_paths(monkeypatch, tmp_path):
    captured = []
    release = threading.Event()
    def run(template, *args, **kwargs):
        release.wait(2)
        captured.append(template)
        return ExportResult(ExportState.COMPLETED)
    monkeypatch.setattr(queue, 'run_export', run)
    state = {'crop': 'crop=100:100:0:0'}
    def build(path):
        return ['fake', '-vf', state['crop'], path]
    first = queue.submit('clip.mp4', tmp_path, 'quality', build, 1, None)
    state['crop'] = 'crop=50:50:4:4'
    second = queue.submit('clip.mp4', tmp_path, 'quality', build, 1, None)
    release.set()
    first.thread.join(3)
    second.thread.join(3)
    assert first.output != second.output
    assert {cmd[2] for cmd in captured} == {'crop=100:100:0:0', 'crop=50:50:4:4'}


@pytest.mark.parametrize('failure', ['builder', 'diagnostics', 'thread-start'])
def test_export_reserves_before_builder_and_releases_after_preparation_failure(
        isolated_admission, monkeypatch, tmp_path, failure):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    outputs = []
    stage = queue.create_staged_output_path

    def create_stage(output):
        outputs.append(output)
        return stage(output)

    def build(staged):
        assert not queue.begin_local_mutations([source])
        assert not queue.begin_local_mutations(outputs)
        if failure == 'builder':
            raise OSError('builder failed')
        return ['synthetic-export', staged]

    def fail(*args, **kwargs):
        raise OSError('preparation failed')

    monkeypatch.setattr(queue, 'create_staged_output_path', create_stage)
    if failure == 'diagnostics':
        monkeypatch.setattr(queue, 'emit_event', fail)
    if failure == 'thread-start':
        monkeypatch.setattr(queue.threading.Thread, 'start', fail)
    with pytest.raises(OSError):
        queue.submit(source, tmp_path, 'quality', build, 1, None)
    assert queue.begin_local_mutations([source, *outputs])
    queue.end_local_mutations([source, *outputs])
    # A never-started thread must not make application shutdown raise join errors.
    queue.shutdown(timeout=0.1)


def test_export_thread_start_keyboard_interrupt_releases_reservations(
        isolated_admission, monkeypatch, tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(queue.threading.Thread, 'start', interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.submit_work(
            [source], tmp_path / 'merged.mp4', 'merge',
            lambda task: ExportResult(ExportState.COMPLETED))

    assert not queue._path_claims
    assert queue._active == 0
    assert queue.begin_local_mutations([source])
    queue.end_local_mutations([source])
    queue.shutdown(timeout=0.1)


def test_shared_source_remains_reserved_while_another_export_is_queued_or_running(
        isolated_admission, monkeypatch, tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    entered = [threading.Event(), threading.Event()]
    second_requested = threading.Event()
    released = [threading.Event(), threading.Event()]
    tasks = []
    counter = iter(range(2))

    def run(*args, cancel, **kwargs):
        index = next(counter)
        if index == 1:
            second_requested.set()
        with queue.export_slot(cancel) as admitted:
            assert admitted
            entered[index].set()
            assert released[index].wait(3)
        return ExportResult(ExportState.COMPLETED)

    monkeypatch.setattr(queue, 'run_export', run)
    try:
        tasks.append(queue.submit(source, tmp_path, 'quality', lambda path: ['fake', path], 1, None))
        assert entered[0].wait(2)
        tasks.append(queue.submit(source, tmp_path, 'quality', lambda path: ['fake', path], 1, None))
        assert second_requested.wait(2)
        assert not entered[1].is_set()
        assert not queue.begin_local_mutations([source])
        assert all(not queue.begin_local_mutations([task.output]) for task in tasks)
        released[0].set()
        tasks[0].thread.join(2)
        assert tasks[0].done and entered[1].wait(2)
        assert not queue.begin_local_mutations([source])
        released[1].set()
        tasks[1].thread.join(2)
        assert tasks[1].done
        assert queue.begin_local_mutations([source, *(task.output for task in tasks)])
        queue.end_local_mutations([source, *(task.output for task in tasks)])
    finally:
        for event in released:
            event.set()
        for task in tasks:
            task.cancel.set()
            task.thread.join(3)


def test_export_mutation_claims_are_canonical_atomic_and_block_startup(
        isolated_admission, tmp_path):
    source, other = tmp_path / 'source.mp4', tmp_path / 'other.mp4'
    (tmp_path / 'sub').mkdir()
    alias = tmp_path / 'sub' / '..' / source.name
    assert queue.begin_local_mutations([source, alias])
    assert not queue.begin_local_mutations([alias, other])
    assert queue.begin_local_mutations([other])
    with pytest.raises(RuntimeError):
        queue.submit(alias, tmp_path, 'quality', lambda _: pytest.fail('blocked builder ran'), 1, None)
    queue.end_local_mutations([alias, other])
    queue.set_initializing(True)
    assert not queue.begin_local_mutations([source])
    with pytest.raises(RuntimeError):
        queue.submit(source, tmp_path, 'quality', lambda _: pytest.fail('startup builder ran'), 1, None)
    queue.set_initializing(False)
    assert queue.begin_local_mutations([source])
    queue.end_local_mutations([source])


def test_startup_gate_holds_previously_admitted_export_before_process_launch(
        isolated_admission, monkeypatch, tmp_path):
    launched = threading.Event()

    class Job:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            launched.set()
            return ExportResult(ExportState.COMPLETED)

    def build(path):
        # Admission has already reserved the source/output; recovery starts
        # before its worker can launch the process.
        queue.set_initializing(True)
        return ['synthetic-export', path]

    monkeypatch.setattr(queue, 'ExportJob', Job)
    task = queue.submit(tmp_path / 'source.mp4', tmp_path, 'quality', build, 1, None)
    try:
        assert not launched.wait(0.15)
        assert not task.done
        queue.set_initializing(False)
        assert launched.wait(2)
        task.thread.join(2)
        assert task.done
    finally:
        queue.set_initializing(False)
        task.cancel.set()
        task.thread.join(3)


def test_quick_finish_survives_editor_close_and_reports_output_identity(
        monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(queue, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(
        queue, 'run_export',
        lambda *args, **kwargs: ExportResult(ExportState.COMPLETED,
                                              elapsed_seconds=0.001))

    task = queue.submit(
        'clip.mp4', tmp_path, 'quality',
        lambda staged: ['fake', staged], 1, None)
    # The reusable editor may close immediately after dispatch.  Terminal
    # diagnostics belong to the queue task, not to an editor callback.
    task.thread.join(2)

    assert task.done
    terminal = next(fields for args, fields in reversed(events)
                    if args == ('export', 'terminal'))
    assert terminal['output'] == task.output
    assert terminal['output_exists'] is False


@pytest.mark.parametrize('failed_step', ['validation', 'commit'])
def test_export_queue_reports_failure_after_successful_encode(
        isolated_admission, monkeypatch, tmp_path, failed_step):
    events = []
    monkeypatch.setattr(queue, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(queue, 'encode_command', lambda template, *_args: template)
    reason = f'Output {failed_step} failed'

    def fail_step(*_args):
        raise OSError(reason)

    if failed_step == 'commit':
        monkeypatch.setattr(queue, 'commit_staged_output', fail_step)

    def encode(staged):
        # The extra argument prevents ExportJob from adding FFmpeg's progress flags.
        return [
            sys.executable, '-c',
            'import sys; from pathlib import Path; '
            'print("Encoded one frame", file=sys.stderr); '
            'Path(sys.argv[-1]).write_bytes(b"encoded")',
            '-progress', staged,
        ]

    task = queue.submit(
        tmp_path / 'clip.mp4', tmp_path, 'quality', encode, 1,
        fail_step if failed_step == 'validation' else None)
    task.thread.join(5)

    assert task.done
    assert task.status == 'Failed'
    assert task.detail.splitlines() == [task.result.failure_message(), reason]
    assert Path(task.output).name in task.result.failure_message()
    assert not Path(task.output).exists()
    terminal = next(fields for args, fields in events if args == ('export', 'terminal'))
    assert terminal['state'] == 'FAILED'
    assert terminal['exit_code'] == 0
    assert terminal['detail'] == reason
    assert terminal['stderr_tail'] == 'Encoded one frame'


def test_terminal_stat_failure_cannot_strand_queue_task(monkeypatch, tmp_path):
    events = []
    ready = threading.Event()
    monkeypatch.setattr(queue, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    def complete_after_output_is_known(*args, **kwargs):
        assert ready.wait(2)
        return ExportResult(ExportState.COMPLETED)
    monkeypatch.setattr(queue, 'run_export', complete_after_output_is_known)
    original_is_file = Path.is_file
    original_stat = Path.stat
    output_path = tmp_path / 'clip_export.mp4'

    def is_file(path):
        return True if path == output_path else original_is_file(path)

    def stat(path, *args, **kwargs):
        if path == output_path:
            raise OSError('output disappeared during diagnostics')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'is_file', is_file)
    monkeypatch.setattr(Path, 'stat', stat)
    task = queue.submit(
        'clip.mp4', tmp_path, 'quality',
        lambda staged: ['fake', staged], 1, None)
    # Use the task's generated output as the path whose metadata read races;
    # this avoids relying on a fixed UUID-derived filename.
    output_path = Path(task.output)
    ready.set()
    task.thread.join(2)

    assert task.done
    assert any(args == ('export', 'terminal') for args, _ in events)


@pytest.mark.parametrize('quality', ['fast', 'quality', 'lossless'])
def test_real_cropped_encode_and_lossless_pixels(tmp_path, monkeypatch, quality):
    ffmpeg = get_ffmpeg_exe()
    source = tmp_path / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x240:rate=12:duration=1', '-c:v', 'ffv1', str(source)], check=True)
    extension = '.mkv' if quality == 'lossless' else '.mp4'
    staged, output = tmp_path / ('partial' + extension), tmp_path / ('output' + extension)
    monkeypatch.setattr(queue, 'choose_video_args',
                        lambda ff, mode, bitrate, **kwargs: encoding.software_args(mode, ff, bitrate))
    template = [ffmpeg, '-y', '-i', str(source), '-vf', 'crop=160:120:20:20',
                *encoding.VIDEO_PLACEHOLDER, str(staged)]
    result = queue.run_export(template, staged, output, quality=quality, duration=1,
                              cancel=threading.Event(), validate=None)
    assert result.succeeded, result.stderr_tail
    assert output.is_file() and not staged.exists()
    def pixels(path, crop=False):
        return subprocess.run([ffmpeg, '-v', 'error', '-i', str(path),
                               *(['-vf', 'crop=160:120:20:20'] if crop else []),
                               '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'],
                              capture_output=True, check=True).stdout
    actual = pixels(output)
    assert len(actual) == 160 * 120 * 3 // 2 * 12
    if quality == 'lossless':
        assert actual == pixels(source, crop=True)


def test_lossless_audio_mix_uses_flac_without_bitrate():
    cmd = encoding.encode_command(
        ['ffmpeg', '-i', 'source', *encoding.VIDEO_PLACEHOLDER,
         '-c:a', 'aac', '-b:a', '320k', 'output.mkv'],
        encoding.video_args('lossless', 'ffv1'), 'lossless')
    assert 'flac' in cmd and 'aac' not in cmd and '-b:a' not in cmd
    assert 'h264_metadata' not in ' '.join(cmd)


def test_queued_export_preserves_unequal_audio_track_origins(tmp_path, monkeypatch):
    """The queued editor path must preserve unequal source origins."""
    from test_clip_viewer_audio_export import (
        _decode_marker_centers,
        _export_viewer,
        _first_decoded_audio_timestamp,
        _make_late_source,
    )
    from ui.clip_viewer import ClipViewer

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'unequal-origins-source.mp4'
    _make_late_source(ffmpeg, source)

    staged = tmp_path / 'queued.partial.mp4'
    output = tmp_path / 'queued.mp4'
    monkeypatch.setattr(queue, 'choose_video_args',
                        lambda ff, mode, bitrate, **kwargs:
                        encoding.software_args(mode, ff, bitrate))
    viewer = _export_viewer(source)
    template = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 4.0, str(staged), None,
        encoding.VIDEO_PLACEHOLDER)
    result = queue.run_export(
        template, staged, output, quality='quality', duration=3,
        cancel=threading.Event(), validate=None)
    assert result.succeeded, result.stderr_tail or result.detail
    assert output.is_file() and not staged.exists()

    native_system = _decode_marker_centers(
        ffmpeg, source, 0, tmp_path / 'system-native.f32', 440.0)
    native_mic = _decode_marker_centers(
        ffmpeg, source, 1, tmp_path / 'mic-native.f32', 880.0)
    system_origin = _first_decoded_audio_timestamp(ffprobe, source, 0)
    mic_origin = _first_decoded_audio_timestamp(ffprobe, source, 1)
    combined_system = _decode_marker_centers(
        ffmpeg, output, 0, tmp_path / 'combined-system.f32', 440.0)
    combined_mic = _decode_marker_centers(
        ffmpeg, output, 0, tmp_path / 'combined-mic.f32', 880.0)
    assert combined_system == pytest.approx(
        [marker + system_origin for marker in native_system], abs=0.08)
    assert combined_mic == pytest.approx(
        [marker + mic_origin for marker in native_mic], abs=0.08)


def test_export_jobs_cancel_ui(qtbot):
    from ui.export_jobs import ExportJobsDialog
    task = queue.ExportTask('sample.mp4', 'sample-export.mp4', 'quality')
    with queue._condition:
        queue._records.append(task)
    window = ExportJobsDialog()
    qtbot.addWidget(window)
    window.refresh()
    window.list.setCurrentRow(len(window.rows) - 1)
    window.cancel_button.click()
    assert task.cancel.is_set()
    task.done = True


def test_jobs_custom_close_and_live_progress(qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    from ui import export_jobs
    task = queue.ExportTask('clip.mp4', 'export.mp4', 'quality', progress=42)
    monkeypatch.setattr(export_jobs, 'tasks', lambda: (task,))
    window = export_jobs.ExportJobsDialog()
    qtbot.addWidget(window)
    window.show()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.isModal()
    row = window._row_widgets[id(task)]
    assert row.percent.text() == '42%' and row.progress.value() == 42
    task.progress = 78
    window.refresh()
    assert window._row_widgets[id(task)] is row
    assert row.percent.text() == '78%' and row.progress.value() == 78
    close = window.findChild(QPushButton, 'fthrDialogClose')
    close.click()
    assert not window.isVisible() and not window.timer.isActive()
    assert not task.cancel.is_set()


@pytest.mark.parametrize('quality,preset,stretch_size', [
    ('fast', False, None), ('quality', False, None), ('lossless', False, None),
    ('quality', True, None), ('quality', False, (128, 192)), ('quality', True, (128, 192)),
])
def test_share_uses_selected_quality_and_preserves_frame_edits(
        qtbot, monkeypatch, tmp_path, quality, preset, stretch_size):
    from core.export_profiles import probe_media
    from ui.clip_viewer import ShareWindow
    ffmpeg = get_ffmpeg_exe()
    source = tmp_path / 'share-source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x240:rate=12:duration=1', '-c:v', 'ffv1', str(source)], check=True)
    monkeypatch.setattr(ShareWindow, '_make_thumbnail', lambda self: None)
    monkeypatch.setattr(queue, 'choose_video_args',
                        lambda ff, mode, bitrate, **kwargs: encoding.software_args(mode, ff, bitrate))
    dialog = ShareWindow(str(source), 0, 1, (20, 20, 160, 120),
                         {'clips_directory': str(tmp_path), 'export_quality': quality},
                         discord_mode=preset, stretch_size=stretch_size)
    qtbot.addWidget(dialog)
    results = []
    dialog._export_sig.connect(lambda ok, path: results.append((ok, path)))
    dialog._export_worker_impl()
    assert results and results[-1][0], results
    output = Path(results[-1][1])
    assert output.suffix == ('.mkv' if quality == 'lossless' else '.mp4')
    media = probe_media(str(output))
    assert (media.width, media.height) == (stretch_size or (160, 120))
    assert media.duration_s == pytest.approx(1.0)




@pytest.mark.parametrize('quality', ['fast', 'quality'])
@pytest.mark.parametrize('oversized_attempts', [1, 2])
def test_discord_share_retries_measured_oversize_once(
        qtbot, monkeypatch, tmp_path, oversized_attempts, quality):
    from ui import clip_viewer
    ffmpeg = get_ffmpeg_exe()
    source = tmp_path / 'share.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x240:rate=30:duration=2',
                    '-c:v', 'ffv1', str(source)], check=True)
    monkeypatch.setattr(clip_viewer.ShareWindow, '_make_thumbnail', lambda self: None)
    settings = {'clips_directory': str(tmp_path), 'export_quality': quality}
    dialog = clip_viewer.ShareWindow(str(source), 0, 2, None, settings, discord_mode=True)
    qtbot.addWidget(dialog)
    limit = int(dialog._export_preset.target_size_mb * 1024**2)
    rates = []

    def encoder(ffmpeg, mode, bitrate, **kwargs):
        rates.append(bitrate)
        return encoding.software_args(mode, ffmpeg, bitrate)

    monkeypatch.setattr(queue, 'choose_video_args', encoder)
    validate = clip_viewer._validate_export_output

    def simulate_encoder_overshoot(path, ffmpeg, cancel):
        validate(path, ffmpeg, cancel)
        if len(rates) <= oversized_attempts:
            # Simulate a variable-rate overshoot after a real, decodable encode.
            extra = int(limit * 1.155) - path.stat().st_size
            with path.open('ab') as stream:
                stream.write(extra.to_bytes(4, 'big') + b'free')
                stream.truncate(stream.tell() + extra - 8)

    monkeypatch.setattr(clip_viewer, '_validate_export_output', simulate_encoder_overshoot)
    results = []
    dialog._export_sig.connect(lambda ok, detail: results.append((ok, detail)))
    dialog._export_worker_impl()
    assert len(rates) == 2 and 0 < rates[1] < rates[0], results
    ok, detail = results.pop()
    if oversized_attempts == 1:
        assert ok and Path(detail).stat().st_size <= limit
    else:
        assert not ok and 'above the' in detail and 'target' in detail
        assert not list((tmp_path / 'Shared').iterdir())
