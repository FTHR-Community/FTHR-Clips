from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_child(source: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'FTHR_UI')
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    return subprocess.run(
        [sys.executable, '-B', '-c', textwrap.dedent(source), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize('failure', ['importerror', 'oserror'])
def test_optional_multimedia_import_failure_keeps_sound_playback_unavailable(
        failure):
    result = _run_child(r'''
        import importlib.abc
        import logging
        import sys

        class FailMultimedia(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'PySide6.QtMultimedia':
                    if sys.argv[1] == 'oserror':
                        raise OSError('simulated multimedia shared library failure')
                    raise ImportError('simulated optional multimedia missing')

        sys.meta_path.insert(0, FailMultimedia())
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        from ui import sound_playback

        assert sound_playback.QAudioOutput is None
        assert sound_playback.QMediaPlayer is None
        assert sound_playback.QSoundEffect is None
        assert sound_playback._MULTIMEDIA_ERROR
        player = sound_playback.SoundPlayback()
        assert player.prepare('/not/a/real/sound.wav') is False
        assert player.play('/not/a/real/sound.wav') is False
        player.stop()
        player.set_volume(50)
        assert player._entries == {}
        assert player._active is None
    ''', failure)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.count('Optional Qt multimedia unavailable:') == 1
    assert len(result.stderr) < 2000


def test_healthy_multimedia_import_remains_available():
    result = _run_child(r'''
        from ui import sound_playback

        assert sound_playback._MULTIMEDIA_ERROR is None
        assert sound_playback.QAudioOutput is not None
        assert sound_playback.QMediaPlayer is not None
        assert sound_playback.QSoundEffect is not None
    ''')

    assert result.returncode == 0, result.stdout + result.stderr


def test_required_qtcore_import_failure_is_not_swallowed():
    result = _run_child(r'''
        import importlib.abc
        import sys

        class FailCore(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'PySide6.QtCore':
                    raise ImportError('simulated required QtCore failure')

        sys.meta_path.insert(0, FailCore())
        try:
            from ui import sound_playback
        except ImportError as error:
            assert 'simulated required QtCore failure' in str(error)
        else:
            raise AssertionError('required QtCore failure was swallowed')
    ''')

    assert result.returncode == 0, result.stdout + result.stderr
