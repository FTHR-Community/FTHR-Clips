from pathlib import Path
import subprocess
import threading
import time
import pytest

from core import export_encoding as encoding, export_queue as queue
from core.export_lifecycle import ExportResult, ExportState
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe


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


@pytest.mark.parametrize('quality,preset', [('fast', False), ('quality', False),
                                         ('lossless', False), ('quality', True)])
def test_share_uses_selected_quality_and_preserves_crop(qtbot, monkeypatch, tmp_path, quality, preset):
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
                         discord_mode=preset)
    qtbot.addWidget(dialog)
    results = []
    dialog._export_sig.connect(lambda ok, path: results.append((ok, path)))
    dialog._export_worker_impl()
    assert results and results[-1][0], results
    output = Path(results[-1][1])
    assert output.suffix == ('.mkv' if quality == 'lossless' else '.mp4')
    raw = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output),
                          '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'], capture_output=True, check=True).stdout
    assert len(raw) == 160 * 120 * 3 // 2 * 12


