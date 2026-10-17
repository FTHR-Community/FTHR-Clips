from __future__ import annotations

from io import StringIO
from pathlib import Path
import subprocess
import threading
import time

from core.export_lifecycle import ExportJob, ExportState
from ui.clip_viewer import ClipViewer


class _FakeProcess:
    def __init__(self, *, code=0, output='out_time_ms=100000\nprogress=end\n',
                 stderr=''):
        self.stdout = StringIO(output)
        self.stderr = StringIO(stderr)
        self.returncode = None
        self._code = code
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.returncode is None and not self.terminated and not self.killed:
            self.returncode = self._code
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = self._code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class _SlowStream:
    def __init__(self, lines, delay=0.01):
        self._lines = iter(lines)
        self._delay = delay

    def readline(self):
        try:
            line = next(self._lines)
        except StopIteration:
            return ''
        time.sleep(self._delay)
        return line


class _StreamingProcess:
    def __init__(self, duration=0.45):
        self.stdout = _SlowStream(
            [f'out_time_ms={index * 10000}\n' for index in range(1, 101)],
            delay=0.005)
        self.stderr = StringIO(''.join(f'stderr-{index}\n' for index in range(1000)))
        self.returncode = None
        self._deadline = time.monotonic() + duration
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.returncode is None and time.monotonic() >= self._deadline:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        self.returncode = 0 if not self.terminated and not self.killed else self.returncode
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def _factory(process):
    def make(*_args, **_kwargs):
        return process
    return make


def test_export_job_requires_terminal_success_after_validation(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    states = []
    process = _FakeProcess()

    result = ExportJob(
        ['ffmpeg', '-i', 'source.mp4', str(staged)], staged, final,
        popen_factory=_factory(process), validate_output=lambda path: path.stat(),
        commit_output=lambda source, target: Path(target).write_bytes(Path(source).read_bytes()),
        state_callback=states.append,
    ).run(duration=1.0)

    assert result.succeeded
    assert result.state is ExportState.COMPLETED
    assert states == [ExportState.REQUESTED, ExportState.PREPARING,
                      ExportState.EXPORTING, ExportState.FINALIZING,
                      ExportState.COMPLETED]
    assert final.read_bytes() == b'valid'
    assert not staged.exists()


def test_export_job_rejects_zero_byte_output_and_cleans_staged(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'')
    process = _FakeProcess()

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process),
    ).run()

    assert result.state is ExportState.FAILED
    assert not staged.exists()
    assert not final.exists()


def test_export_job_preserves_bounded_stderr_on_nonzero_exit(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    process = _FakeProcess(code=7, stderr='x' * 20_000)

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), stderr_limit=256,
    ).run()

    assert result.state is ExportState.FAILED
    assert result.returncode == 7
    assert len(result.stderr_tail) <= 256


def test_export_job_cancel_before_launch_is_terminal(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    called = []

    job = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=lambda *_args, **_kwargs: called.append(True),
    )
    job.cancel()
    result = job.run()

    assert result.state is ExportState.CANCELLED
    assert called == []


def test_cancel_does_not_set_event_after_terminal_completion(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    job = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(_FakeProcess()),
        validate_output=lambda path: None,
    )

    result = job.run()
    assert result.state is ExportState.COMPLETED
    job.cancel()

    assert not job._cancel_event.is_set()


def test_stderr_flood_keeps_newest_tail_and_progress_alive(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    process = _StreamingProcess()

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), stderr_limit=256,
        inactivity_timeout=0.08,
        validate_output=lambda path: None,
    ).run(duration=1.0)

    assert result.state is ExportState.COMPLETED
    assert 'stderr-999' in result.stderr_tail
    assert 'stderr-0' not in result.stderr_tail
    assert len(result.stderr_tail) <= 256


def test_export_job_cancel_during_execution_reaps_process(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    process = _FakeProcess(output='')
    process.poll = lambda: None if not process.terminated else process.returncode
    job = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), inactivity_timeout=5,
    )
    result_holder = []
    thread = threading.Thread(target=lambda: result_holder.append(job.run()))
    thread.start()
    for _ in range(100):
        if job.process is not None:
            break
        time.sleep(0.005)
    job.cancel()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result_holder and result_holder[0].state is ExportState.CANCELLED
    assert process.terminated or process.killed
    assert not any(
        thread.name.startswith('fthr-export-') and thread.is_alive()
        for thread in threading.enumerate())


def test_export_job_times_out_when_progress_stalls(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    process = _FakeProcess(output='out_time_ms=100000\n')
    process.poll = lambda: None
    process.wait = lambda timeout=None: (_ for _ in ()).throw(
        subprocess.TimeoutExpired('ffmpeg', timeout))

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), overall_timeout=0.2,
        inactivity_timeout=0.05,
    ).run(duration=1.0)

    assert result.state is ExportState.TIMED_OUT
    assert process.terminated or process.killed
    assert not staged.exists()
    assert not any(
        thread.name.startswith('fthr-export-') and thread.is_alive()
        for thread in threading.enumerate())


def test_export_job_only_resets_stall_deadline_for_forward_progress():
    job = ExportJob(['ffmpeg', 'staged.mp4'], 'staged.mp4', 'final.mp4')

    assert job._handle_progress('diagnostic=no-op', 1.0) is False
    assert job._handle_progress('out_time_ms=100000', 1.0) is True
    assert job._handle_progress('out_time_ms=100000', 1.0) is False
    assert job._handle_progress('out_time_ms=99000', 1.0) is False
    assert job._handle_progress('malformed', 1.0) is False


def test_export_job_cancel_wins_if_requested_during_commit(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    process = _FakeProcess()
    holder = {}

    def commit(source, target):
        Path(target).write_bytes(Path(source).read_bytes())
        holder['job'].cancel()

    holder['job'] = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), validate_output=lambda path: path.stat(),
        commit_output=commit,
    )
    result = holder['job'].run()

    assert result.state is ExportState.CANCELLED
    assert not final.exists()
    assert not staged.exists()


def test_editor_export_button_submits_another_independent_job():
    calls = []
    viewer = type('Viewer', (), {'_export_clip': lambda self: calls.append('queued')})()
    ClipViewer._on_export_button(viewer)
    ClipViewer._on_export_button(viewer)
    assert calls == ['queued', 'queued']


def test_export_job_launch_failure_is_terminal(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'

    def launch_failure(*_args, **_kwargs):
        raise OSError('ffmpeg missing')

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=launch_failure,
    ).run()

    assert result.state is ExportState.FAILED
    assert 'missing' in result.detail
    assert not final.exists()


def test_export_job_missing_output_after_zero_exit_is_failed(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(_FakeProcess()),
    ).run()

    assert result.state is ExportState.FAILED
    assert 'usable output' in result.detail
    assert not final.exists()


def test_export_job_rename_failure_is_terminal_and_clean(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    process = _FakeProcess()

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), validate_output=lambda path: None,
        commit_output=lambda *_: (_ for _ in ()).throw(OSError('rename denied')),
    ).run()

    assert result.state is ExportState.FAILED
    assert 'rename denied' in result.detail
    assert not staged.exists()
    assert not final.exists()


def test_export_job_validation_cancellation_cleans_staged(tmp_path):
    staged = tmp_path / 'job.partial.mp4'
    final = tmp_path / 'job.mp4'
    staged.write_bytes(b'valid')
    process = _FakeProcess()
    cancel = threading.Event()

    def validate(_path, cancel_event):
        cancel_event.set()
        raise RuntimeError('validation cancelled')

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process), validate_output=validate,
        cancel_event=cancel,
    ).run()

    assert result.state is ExportState.CANCELLED
    assert result.detail == 'Export cancelled'
    assert not staged.exists()
    assert not final.exists()


def test_editor_worker_preparation_exception_is_terminal(monkeypatch):
    emitted = []
    viewer = type('Viewer', (), {
        '_closing': False,
        '_export_cancel': threading.Event(),
        '_export_job': None,
        '_export_staged_path': None,
        '_export_done': type('Signal', (), {
            'emit': lambda self, *args: emitted.append(args),
        })(),
    })()
    viewer._export_worker_impl = ClipViewer._export_worker_impl.__get__(viewer)

    def fail_prepare():
        raise RuntimeError('command preparation failed')

    monkeypatch.setattr('ui.clip_viewer.get_ffmpeg_exe', fail_prepare)
    ClipViewer._export_worker(viewer, 0.0, 1.0, 'clip.mp4', None)

    assert emitted == [(False, 'RuntimeError: command preparation failed')]


def test_export_job_supports_sequential_jobs(tmp_path):
    final = tmp_path / 'job.mp4'
    for index in range(2):
        staged = tmp_path / f'job-{index}.partial.mp4'
        staged.write_bytes(f'valid-{index}'.encode())
        result = ExportJob(
            ['ffmpeg', str(staged)], staged, final,
            popen_factory=_factory(_FakeProcess()),
            validate_output=lambda path: None,
        ).run()
        assert result.state is ExportState.COMPLETED
        assert final.read_bytes() == f'valid-{index}'.encode()
