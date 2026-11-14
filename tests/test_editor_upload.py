"""Focused tests for edited-clip upload coordination."""

from pathlib import Path
import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from core.editor_upload import EditorUploadCoordinator, EditorUploadRequest


class _FakeTask:
    def __init__(self, output):
        self.output = str(output)
        self.status = 'Queued'
        self.detail = ''
        self.cancel = threading.Event()
        self.done = False


def _viewer_state(segments=((0.0, 1.0),)):
    from ui.clip_viewer import ClipViewer

    viewer = ClipViewer.__new__(ClipViewer)
    viewer._audio_preparation_ready = True
    viewer._audio_inspection_error = False
    viewer._metadata_loaded = True
    viewer.duration_ms = 1_000
    viewer._crop_rect = None
    viewer._stretch_ratio = 1.0
    viewer._speed_rate = 1.0
    viewer._effects = {
        'exposure': 0, 'contrast': 0, 'saturation': 0,
        'temperature': 0, 'sharpness': 0, 'vignette': 0,
    }
    viewer.sm = {'watermark_enabled': False}
    viewer._master_volume = 100
    viewer._source_volumes = {}
    viewer._source_mutes = {}
    viewer._undo_stack = []
    return viewer, list(segments)


def _request(source, directory, *, marker='one', audio_ready=True):
    def build(output):
        return ['fake-ffmpeg', '-vf', f'marker={marker}', output]

    return EditorUploadRequest(
        source=str(source), directory=directory, quality='quality', duration=1.0,
        build_command=build, validate=lambda *_: None,
        audio_ready=audio_ready, edited=True, segment_count=1,
        kept_duration_seconds=1.0)


def test_audio_not_ready_never_uploads_original(monkeypatch, tmp_path):
    from core import export_queue
    calls = []
    monkeypatch.setattr(export_queue, 'submit', lambda *args, **kwargs: calls.append(args))
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads, errors = [], []
    coordinator.upload_requested.connect(uploads.append)
    coordinator.export_error.connect(lambda title, detail, level: errors.append(title))

    assert not coordinator.submit(_request(source, tmp_path, audio_ready=False))
    assert not calls and not uploads
    assert errors == ['UPLOAD NOT READY']


def test_cold_viewer_state_cannot_export_estimated_duration():
    from ui.clip_viewer import ClipViewer

    viewer = ClipViewer.__new__(ClipViewer)
    viewer._metadata_loaded = False
    viewer.duration_ms = 30_000
    viewer._kept_segments_seconds = lambda: [(0.0, 30.0)]
    with pytest.raises(RuntimeError, match='metadata is still loading'):
        viewer._build_upload_request()


@pytest.mark.parametrize('terminal', ['Failed', 'Cancelled', 'Timed_Out'])
def test_terminal_failure_is_visible_and_retryable(monkeypatch, tmp_path, terminal):
    from core import export_queue
    tasks = []

    def submit(*args, **kwargs):
        task = _FakeTask(tmp_path / f'out-{len(tasks)}.mp4')
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads, errors = [], []
    coordinator.upload_requested.connect(uploads.append)
    coordinator.export_error.connect(lambda title, detail, level: errors.append((title, detail)))
    request = _request(source, tmp_path)
    assert coordinator.submit(request)
    tasks[0].status, tasks[0].detail, tasks[0].done = terminal, 'terminal failure', True
    coordinator._poll()
    assert not uploads and errors[-1] == ('UPLOAD EXPORT FAILED', 'terminal failure')
    assert coordinator.submit(request)
    assert len(tasks) == 2


def test_validator_failure_detail_is_visible_and_retry_uses_new_task(monkeypatch, tmp_path):
    from core import export_queue
    tasks = []

    def submit(source, directory, quality, build_command, duration, validate, **kwargs):
        task = _FakeTask(tmp_path / f'out-{len(tasks)}.mp4')
        try:
            validate(tmp_path / 'invalid.mp4', threading.Event())
        except RuntimeError as error:
            task.status, task.detail, task.done = 'Failed', str(error), True
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    errors = []
    coordinator.export_error.connect(lambda title, detail, level: errors.append(detail))
    def validator(*_):
        raise RuntimeError('validator rejected')

    request = replace(_request(source, tmp_path), validate=validator)
    assert coordinator.submit(request)
    coordinator._poll()
    assert errors[-1] == 'validator rejected'
    assert coordinator.submit(request)
    assert len(tasks) == 2


def test_neutral_and_editor_state_route_to_expected_path(monkeypatch, tmp_path):
    from core import export_queue
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    tasks = []

    def submit(*args, **kwargs):
        task = _FakeTask(tmp_path / f'edited-{len(tasks)}.mp4')
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads = []
    coordinator.upload_requested.connect(uploads.append)
    viewer, segments = _viewer_state()
    assert viewer._upload_is_neutral(segments)
    assert not viewer._upload_is_neutral([(0.001, 1.0)])
    assert not viewer._upload_is_neutral([(0.0, 0.999)])
    neutral = EditorUploadRequest(
        source=str(source), directory=tmp_path, quality='quality', duration=1,
        build_command=lambda output: ['unused', output], validate=lambda *_: None,
        edited=False, audio_ready=True)
    assert not coordinator.submit(neutral)
    assert uploads == [str(source)] and not tasks

    edited_cases = [
        [(0.0, 0.5)], [(0.0, 0.25), (0.75, 1.0)],
    ]
    for edited_segments in edited_cases:
        assert not viewer._upload_is_neutral(edited_segments)
    viewer._crop_rect = (0, 0, 100, 100)
    assert not viewer._upload_is_neutral(segments)
    viewer._crop_rect = None
    viewer._effects['contrast'] = 20
    assert not viewer._upload_is_neutral(segments)
    viewer._effects['contrast'] = 0
    viewer._stretch_ratio = 1.2
    assert not viewer._upload_is_neutral(segments)
    viewer._stretch_ratio = 1.0
    viewer._stretch_size = (1280, 720)
    assert not viewer._upload_is_neutral(segments)
    viewer._stretch_size = None
    viewer._speed_rate = 1.25
    assert not viewer._upload_is_neutral(segments)
    viewer._speed_rate = 1.0
    viewer._master_volume = 80
    assert not viewer._upload_is_neutral(segments)
    viewer._master_volume = 100
    viewer._source_mutes['system'] = True
    assert not viewer._upload_is_neutral(segments)
    # A restored draft is evaluated from current state, never from undo history.
    viewer._source_mutes.clear()
    viewer._undo_stack = [SimpleNamespace(trim_start=0.0, trim_end=1.0)]
    assert viewer._upload_is_neutral([(0.0, 0.5)]) is False
    edited = _request(source, tmp_path, marker='crop')
    assert coordinator.submit(edited)
    assert len(tasks) == 1 and uploads == [str(source)]


def test_neutral_request_does_not_probe_ffmpeg(monkeypatch, tmp_path):
    from ui import clip_viewer

    viewer, segments = _viewer_state()
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    viewer.clip_path = str(source)
    viewer._kept_segments_seconds = lambda: list(segments)
    viewer.export_quality_combo = SimpleNamespace(currentData=lambda: 'quality')
    monkeypatch.setattr(
        clip_viewer, 'get_ffmpeg_exe',
        lambda: (_ for _ in ()).throw(AssertionError('neutral upload probed ffmpeg')))
    request = viewer._build_upload_request()
    assert request.edited is False
    assert request.directory == source.parent


def test_upload_command_snapshot_ignores_later_viewer_mutations(tmp_path):
    from ui.clip_viewer import ClipViewer

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    segments_state = [(0.0, 0.8)]
    viewer = ClipViewer.__new__(ClipViewer)
    viewer._metadata_loaded = True
    viewer.duration_ms = 1_000
    viewer._kept_segments_seconds = lambda: list(segments_state)
    viewer.clip_path = str(source)
    viewer.export_quality_combo = SimpleNamespace(currentData=lambda: 'quality')
    viewer._crop_rect = (0, 0, 100, 100)
    viewer._effects = {'contrast': 10, 'exposure': 0, 'saturation': 0,
                       'temperature': 0, 'sharpness': 0, 'vignette': 0}
    viewer._stretch_ratio = 1.2
    viewer._stretch_size = (84, 132)
    viewer._speed_rate = 1.25
    viewer._preserve_pitch = True
    viewer._playback_sources = ()
    viewer._audio_tracks = ()
    viewer._source_volumes = {}
    viewer._source_mutes = {}
    viewer._master_volume = 50
    viewer._audio_preparation_ready = True
    viewer._audio_inspection_error = False
    viewer.sm = {'clips_directory': str(tmp_path / 'clips'), 'watermark_enabled': False}
    request = viewer._build_upload_request()
    before = tuple(request.build_command('<OUTPUT>'))
    assert any('scale=84:132,setsar=1' in argument for argument in before)
    segments_state[:] = [(0.0, 0.2)]
    viewer._crop_rect = None
    viewer._effects['contrast'] = 0
    viewer._stretch_ratio = 1.0
    viewer._stretch_size = None
    viewer._speed_rate = 1.0
    viewer._master_volume = 100
    after = tuple(request.build_command('<OUTPUT>'))
    assert before == after


def test_repeated_coordinator_binding_has_one_status_receiver():
    from ui.clip_viewer import ClipViewer

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def disconnect(self, callback):
            self.callbacks.remove(callback)

        def emit(self, *args):
            for callback in tuple(self.callbacks):
                callback(*args)

    signal = FakeSignal()
    viewer = ClipViewer.__new__(ClipViewer)
    viewer.clip_path = 'source.mp4'
    viewer._upload_enabled = True
    viewer.upload_btn = SimpleNamespace(setText=lambda *_: None,
                                         setEnabled=lambda *_: None)
    viewer._later = lambda *_: None
    viewer._upload_coordinator = SimpleNamespace(status_changed=signal)
    viewer._upload_status_coordinator = None
    viewer._bind_upload_coordinator()
    viewer._bind_upload_coordinator()
    signal.emit('source.mp4', 'exporting')
    assert len(signal.callbacks) == 1


def test_running_source_deduplicates_changed_snapshot(monkeypatch, tmp_path):
    from core import export_queue
    tasks = []

    def submit(*args, **kwargs):
        task = _FakeTask(tmp_path / f'out-{len(tasks)}.mp4')
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    assert coordinator.submit(_request(source, tmp_path, marker='one'))
    assert not coordinator.submit(_request(source, tmp_path, marker='two'))
    assert len(tasks) == 1


def test_neutral_fastpath_waits_for_running_edited_export(monkeypatch, tmp_path):
    from core import export_queue
    task = _FakeTask(tmp_path / 'out.mp4')
    calls = []
    monkeypatch.setattr(export_queue, 'submit', lambda *args, **kwargs: calls.append(args) or task)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads = []
    coordinator.upload_requested.connect(uploads.append)
    assert coordinator.submit(_request(source, tmp_path, marker='edited'))
    neutral = EditorUploadRequest(
        source=str(source), directory=tmp_path, quality='quality', duration=1.0,
        build_command=lambda output: ['unused', output], validate=lambda *_: None,
        edited=False, audio_ready=True)
    assert not coordinator.submit(neutral)
    assert len(calls) == 1 and not uploads


def test_done_before_first_poll_is_published_once(monkeypatch, tmp_path):
    from core import export_queue
    tasks = []

    def submit(*args, **kwargs):
        task = _FakeTask(tmp_path / 'out.mp4')
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads = []
    coordinator.upload_requested.connect(uploads.append)
    request = _request(source, tmp_path)
    assert coordinator.submit(request)
    tasks[0].status = 'Completed'
    Path(tasks[0].output).write_bytes(b'export')
    tasks[0].done = True
    # A second click sees the completed task and lets the coordinator publish
    # it; it must not start another queue task.
    assert not coordinator.submit(request)
    assert len(tasks) == 1
    assert uploads == [tasks[0].output]


def test_changed_output_invalidates_finished_cache(monkeypatch, tmp_path):
    from core import export_queue
    tasks = []

    def submit(*args, **kwargs):
        task = _FakeTask(tmp_path / f'out-{len(tasks)}.mp4')
        tasks.append(task)
        return task

    monkeypatch.setattr(export_queue, 'submit', submit)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    request = _request(source, tmp_path)
    assert coordinator.submit(request)
    task = tasks[0]
    Path(task.output).write_bytes(b'export')
    task.status, task.done = 'Completed', True
    coordinator._poll()
    assert len(tasks) == 1
    Path(task.output).write_bytes(b'tampered')
    assert coordinator.submit(request)
    assert len(tasks) == 2


def test_source_replacement_after_export_never_uploads(monkeypatch, tmp_path):
    from core import export_queue
    task = _FakeTask(tmp_path / 'out.mp4')
    monkeypatch.setattr(export_queue, 'submit', lambda *args, **kwargs: task)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'old')
    coordinator = EditorUploadCoordinator(poll_ms=10)
    uploads, errors = [], []
    coordinator.upload_requested.connect(uploads.append)
    coordinator.export_error.connect(lambda title, detail, level: errors.append(title))
    assert coordinator.submit(_request(source, tmp_path))
    Path(task.output).write_bytes(b'export')
    source.write_bytes(b'new source')
    task.status, task.done = 'Completed', True
    coordinator._poll()
    assert not uploads
    assert errors == ['UPLOAD EXPORT FAILED']


def test_real_cropped_trim_export_emits_committed_output(tmp_path, monkeypatch, qtbot):
    from core import export_encoding, export_queue
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'source.mkv'
    subprocess.run([
        ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x240:rate=12:duration=1', '-c:v', 'ffv1', str(source)],
        check=True)
    monkeypatch.setattr(
        export_queue, 'choose_video_args',
        lambda ff, mode, bitrate, **kwargs: export_encoding.software_args(mode, ff, bitrate))
    coordinator = EditorUploadCoordinator(poll_ms=10)
    outputs = []
    coordinator.upload_requested.connect(outputs.append)

    def build(output):
        return [ffmpeg, '-y', '-i', str(source), '-t', '0.5', '-vf',
                'crop=160:120:20:20', *export_encoding.VIDEO_PLACEHOLDER, output]

    def validate(path, cancel):
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, check=True)
        assert result.stdout.strip() == '160,120'

    request = EditorUploadRequest(
        source=str(source), directory=tmp_path / 'Exported', quality='quality',
        duration=.5, build_command=build, validate=validate, edited=True,
        audio_ready=True, segment_count=1, kept_duration_seconds=.5)
    assert coordinator.submit(request)
    qtbot.waitUntil(lambda: bool(outputs), timeout=20_000)
    output = Path(outputs[0])
    assert output.is_file()
    assert output.parent.name == 'Exported'
    assert not Path(str(output) + '.partial').exists()
    assert QCoreApplication.instance() is not None
