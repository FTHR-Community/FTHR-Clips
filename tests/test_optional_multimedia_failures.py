"""Failure-injection coverage for optional Qt multimedia boundaries."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_child(tmp_path: Path, source: str, *args: str) -> subprocess.CompletedProcess:
    home = tmp_path / 'home'
    local = tmp_path / 'local'
    roaming = tmp_path / 'roaming'
    temporary = tmp_path / 'temp'
    for directory in (home, local, roaming, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        'HOME': str(home),
        'USERPROFILE': str(home),
        'APPDATA': str(roaming),
        'LOCALAPPDATA': str(local),
        'TEMP': str(temporary),
        'TMP': str(temporary),
        'QT_QPA_PLATFORM': 'offscreen',
        'PYTHONPATH': str(ROOT / 'FTHR_UI'),
    })
    return subprocess.run(
        [sys.executable, '-B', '-c', textwrap.dedent(source), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=45,
        check=False,
    )


@pytest.mark.parametrize('failure', ['importerror', 'dll'])
def test_main_import_survives_optional_multimedia_failure(
        tmp_path: Path, failure: str) -> None:
    result = _run_child(tmp_path, r'''
        import importlib.abc
        import logging
        import sys

        class FailOptionalMedia(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'PySide6.QtMultimedia':
                    if sys.argv[1] == 'dll':
                        raise OSError('simulated multimedia DLL unavailable')
                    raise ImportError('simulated optional multimedia unavailable')

        sys.meta_path.insert(0, FailOptionalMedia())
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        import main
        from ui import sound_playback

        assert main.MainWindow is not None
        assert sound_playback.QSoundEffect is None
        assert sound_playback.QMediaPlayer is None
        assert sound_playback.QAudioOutput is None
        player = sound_playback.SoundPlayback()
        for _ in range(25):
            assert player.prepare('missing.wav') is False
            assert player.play('missing.wav') is False
            player.stop()
            player.set_volume(40)
        assert player._entries == {}
        assert player._active is None
    ''', failure)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.count('Optional Qt multimedia unavailable:') == 1
    assert len(result.stderr) < 3000


def test_required_qtwidgets_import_failure_still_propagates(
        tmp_path: Path) -> None:
    result = _run_child(tmp_path, r'''
        import importlib.abc
        import sys

        class FailWidgets(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'PySide6.QtWidgets':
                    raise ImportError('simulated required QtWidgets failure')

        sys.meta_path.insert(0, FailWidgets())
        try:
            import main
        except ImportError as error:
            assert 'simulated required QtWidgets failure' in str(error)
        else:
            raise AssertionError('required QtWidgets failure was swallowed')
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('failure_type', [ImportError, OSError])
@pytest.mark.parametrize('notifications_enabled', [True, False])
@pytest.mark.parametrize(
    ('viewer_module', 'editor_enabled'),
    [('ui.clip_editor_window', True), ('ui.basic_clip_viewer', False)],
)
def test_clip_viewer_import_failure_preserves_ownership_and_records_detail(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        failure_type: type[Exception], notifications_enabled: bool,
        viewer_module: str, editor_enabled: bool) -> None:
    from core.field_diagnostics import DiagnosticError
    from main import MainWindow

    clip_path = tmp_path / 'clip.mp4'
    clip_path.touch()
    events = []
    notices = []

    class CameraTimer:
        stopped = False

        def stop(self):
            self.stopped = True

    class PreviousViewer:
        closed = False

        def close(self):
            self.closed = True

        def deleteLater(self):
            self.closed = True

    timer = CameraTimer()
    previous = PreviousViewer()
    window = SimpleNamespace(
        _clip_readiness=SimpleNamespace(can_access=lambda _path: True),
        _settings_page_widget=SimpleNamespace(_camera_preview_timer=timer),
        _active_clip_viewer=previous,
        _basic_clip_viewer=previous if not editor_enabled else None,
        _editor_sessions=[],
        _diagnostics=SimpleNamespace(emit=lambda *args, **kwargs: events.append((args, kwargs))),
        settings_manager=SimpleNamespace(get=lambda key, default=None: (
            notifications_enabled if key == 'error_notifications_enabled' else
            editor_enabled if key == 'clip_editor_enabled' else default)),
        error_bar=SimpleNamespace(push=lambda *args: notices.append(args)),
        _export_diagnostic_report=lambda: None,
        _show_playback_unavailable=lambda: MainWindow._show_playback_unavailable(window),
    )

    original_import = importlib.import_module

    def failing_import(name, package=None):
        if name == viewer_module:
            raise failure_type('simulated viewer native component failure')
        return original_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', failing_import)
    MainWindow._on_clip_opened(window, str(clip_path), None, None)

    assert window._active_clip_viewer is previous
    assert window._basic_clip_viewer is (None if editor_enabled else previous)
    assert previous.closed is False
    assert timer.stopped is False
    assert len(events) == 1
    args, fields = events[0]
    assert args[:2] == ('playback', 'viewer_import_failed')
    assert fields['error'] is DiagnosticError.PLAYBACK_INIT_FAILED
    assert fields['operation'] == 'import_viewer'
    assert fields['exception_type'] == failure_type.__name__
    assert 'simulated viewer native component failure' in fields['detail']
    assert len(fields['detail']) <= 240
    if notifications_enabled:
        assert len(notices) == 1
        assert notices[0][0] == 'PLAYBACK UNAVAILABLE'
        assert 'could not load' in notices[0][1].lower()
        assert notices[0][3][0][0] == 'EXPORT DIAGNOSTICS'
    else:
        assert notices == []
