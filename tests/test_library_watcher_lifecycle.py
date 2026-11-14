"""Actual Qt ownership regression; no timing/native-dispatcher qualification."""
import threading
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtWidgets import QWidget

from ui import clip_grid


@pytest.fixture
def library(qapp, qtbot, monkeypatch, tmp_path):
    assert QThread.currentThread() == qapp.thread()
    monkeypatch.setattr(clip_grid.ClipGrid, '_load_clips', lambda *_: None)
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    # qtbot tracks a surviving host, not a grid deliberately deleted while
    # its Python wrapper remains held by the owner-destruction test.
    host = QWidget()
    qtbot.addWidget(host)
    grid = clip_grid.ClipGrid(settings, parent=host, autostart=False)
    watcher = grid._watcher
    try:
        yield grid, watcher, tmp_path
    finally:
        assert QThread.currentThread() == qapp.thread()
        try:
            if shiboken6.isValid(grid):
                try:
                    grid.shutdown()
                finally:
                    grid.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        finally:
            if shiboken6.isValid(watcher):
                # RED leaves the unparented watcher alive. Only clean up AFTER
                # the observation/assertion, on the GUI thread, never as its fix.
                shiboken6.delete(watcher)


def test_watcher_is_owned_by_grid_on_gui_thread(library, qapp):
    grid, watcher, _directory = library
    assert shiboken6.isValid(grid) and shiboken6.isValid(watcher)
    assert watcher.parent() is grid
    assert watcher.thread() == grid.thread() == qapp.thread()


def test_cpp_grid_deletion_destroys_retained_watcher_on_gui_thread(library, qapp):
    grid, watcher, _directory = library
    gui_identity = (threading.get_native_id(), threading.get_ident())
    assert QThread.currentThread() == qapp.thread()
    destroyed = []

    def observed_destroyed():
        # Do not inspect Qt objects or adopt a QThread inside this callback.
        destroyed.append((threading.get_native_id(), threading.get_ident()))

    watcher.destroyed.connect(observed_destroyed, Qt.ConnectionType.DirectConnection)
    grid.shutdown()
    grid.shutdown()
    assert shiboken6.isValid(grid) and shiboken6.isValid(watcher)
    assert destroyed == []
    grid.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    observed_before_cleanup = tuple(destroyed)
    assert not shiboken6.isValid(grid)
    assert not shiboken6.isValid(watcher)
    assert observed_before_cleanup == (gui_identity,)
    # grid/watcher Python wrappers remain strongly referenced throughout;
    # the expected destruction comes from the C++ parent, not Python GC.


def test_shutdown_is_idempotent_removes_paths_without_early_watcher_destruction(library):
    grid, watcher, directory = library
    assert watcher.addPath(str(directory))
    assert str(directory) in watcher.directories()
    grid.shutdown()
    assert grid._shutdown_started
    assert shiboken6.isValid(grid) and shiboken6.isValid(watcher)
    assert watcher.directories() == []
    grid.shutdown()
    assert shiboken6.isValid(grid) and shiboken6.isValid(watcher)
    assert watcher.directories() == []
