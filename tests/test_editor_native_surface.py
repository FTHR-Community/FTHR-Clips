import ctypes
import sys

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from ui.clip_editor_window import ClipEditorWindow
from ui.quick_video_preview import QuickVideoPreview, quick_preview_supported


class _WindowEvents(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.events = []
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.WinIdChange, QEvent.Type.Hide, QEvent.Type.Show):
            self.events.append(event.type())
        return super().eventFilter(watched, event)


def test_windows_editor_keeps_native_window_during_deferred_page_handoff(
        qtbot, monkeypatch, tmp_path):
    if sys.platform != 'win32' or QApplication.platformName() != 'windows':
        pytest.skip('Requires the native Qt Windows platform')
    if not quick_preview_supported():
        pytest.skip('Requires the Qt Quick editor renderer')

    from ui.clip_viewer import ClipViewer

    monkeypatch.setattr(ClipViewer, '_prepare_initial_media_source', lambda _self: None)
    monkeypatch.setattr(ClipViewer, '_discover_audio_sources_async', lambda _self: None)
    path = tmp_path / 'editor.mp4'
    path.touch()
    window = ClipEditorWindow(str(path), None, defer_initial_clip=True, autoplay=False)
    qtbot.addWidget(window)
    # Exercise the real Win32 backing surface without covering the user's apps.
    window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.move(window.screen().virtualGeometry().topLeft() - QPoint(2000, 2000))

    reserved_preview = window.findChild(QuickVideoPreview)
    assert reserved_preview is not None
    assert reserved_preview.rootObject() is None
    skeleton = window._content.currentWidget()
    with qtbot.waitSignal(window.clip_page_ready, timeout=5000):
        window.show()
        hwnd = int(window.winId())
        assert ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd))
        changes = _WindowEvents(window)
        # Windows can omit exposure events for windows outside the desktop.
        skeleton.render(QPixmap(skeleton.size()))

    assert window.clip_page._native_video_widget is reserved_preview
    assert reserved_preview.rootObject() is not None
    reserved_preview.show()
    qtbot.wait(50)
    assert int(window.winId()) == hwnd
    assert ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd))
    assert changes.events == []
