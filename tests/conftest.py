"""Shared pytest setup for the uninstalled FTHR_UI source tree.

Add the UI directory to sys.path centrally so test imports do not depend
on collection order.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Widget tests construct real QWidgets. On a headless CI runner Qt aborts
# without a platform plugin; offscreen is the supported answer and is harmless
# on a desktop. setdefault so a developer can still force xcb/windows to watch
# a test render.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

for _p in (ROOT / 'FTHR_UI', ROOT / 'tools'):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture(autouse=True)
def _isolate_windows_videos_storage(monkeypatch, tmp_path):
    """Keep Windows known-folder tests away from a developer's real Videos."""
    from core import settings_manager

    monkeypatch.setattr(
        settings_manager, '_windows_videos_directory',
        lambda: tmp_path / 'Videos', raising=False)


@pytest.fixture(autouse=True)
def _reset_ui_runtime_state():
    """Do not leak SettingsPage animation or application-QSS state between tests."""
    yield
    from PySide6.QtWidgets import QApplication
    from ui.motion import set_reduce_motion

    app = QApplication.instance()
    if app is None:
        return
    set_reduce_motion(False)
    app.setProperty('fthrReduceMotion', False)
    app.setStyleSheet('')
