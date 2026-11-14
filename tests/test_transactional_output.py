from __future__ import annotations

from pathlib import Path
import threading

from core.transactional_output import (
    commit_staged_output,
    create_staged_output_path,
    discard_staged_output,
)
from ui.clip_viewer import ClipViewer, VolumePopup


def test_staged_export_is_same_directory_and_not_final_looking(tmp_path):
    final = tmp_path / 'export.mp4'

    staged = create_staged_output_path(final)

    assert staged.parent == final.parent
    assert staged != final
    assert staged.suffix == '.mp4'
    assert '.partial.' in staged.name


def test_failed_export_cleanup_leaves_no_final_file(tmp_path):
    final = tmp_path / 'export.mp4'
    staged = create_staged_output_path(final)
    staged.write_bytes(b'corrupt partial output')

    discard_staged_output(staged)

    assert not staged.exists()
    assert not final.exists()


def test_success_atomically_promotes_staged_output(tmp_path):
    final = tmp_path / 'export.mp4'
    staged = create_staged_output_path(final)
    staged.write_bytes(b'complete output')

    commit_staged_output(staged, final)

    assert final.read_bytes() == b'complete output'
    assert not staged.exists()


def test_editor_failure_removes_staged_output_and_never_publishes_final(
        tmp_path, monkeypatch):
    from types import SimpleNamespace

    final = tmp_path / 'export.mp4'
    emitted = []
    fake = SimpleNamespace(
        _build_export_cmd=lambda **kwargs: ['ffmpeg', kwargs['out_path']],
        _export_done=SimpleNamespace(emit=lambda *args: emitted.append(args)),
        export_error=SimpleNamespace(emit=lambda *_args: None),
    )
    monkeypatch.setattr('ui.clip_viewer.get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr('ui.clip_viewer.software_video_args', lambda: [])
    monkeypatch.setattr('ui.clip_viewer.maximum_quality_video_args', lambda _ff: [])

    def _failed_popen(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b'partial')
        raise OSError('simulated FFmpeg launch failure')

    monkeypatch.setattr('ui.clip_viewer.subprocess.Popen', _failed_popen)

    fake._export_cancel = threading.Event()
    fake._export_staged_path = None
    ClipViewer._export_worker_impl(fake, 0.0, 1.0, str(final), None)

    assert not final.exists()
    assert list(tmp_path.glob('*.partial.*')) == []
    assert emitted and emitted[-1][0] is False


def test_alpha_volume_popup_exposes_master_only():
    assert VolumePopup._SOURCES == [('master', 'Master')]
