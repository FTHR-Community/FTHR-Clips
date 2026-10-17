from __future__ import annotations

import sys
import types

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Signal
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QWidget
from PySide6.QtTest import QTest

from ui.window_preview import WindowPreview


class FakePreviewBackend(QObject):
    image_ready = Signal(QImage)
    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = []
        self.geometry = []
        self.stopped = 0
        self.disposed = 0

    def start(self, window, destination_hwnd, destination_rect):
        self.started.append((window, destination_hwnd, destination_rect))

    def update_geometry(self, destination_hwnd, destination_rect):
        self.geometry.append((destination_hwnd, destination_rect))

    def stop(self):
        self.stopped += 1

    def dispose(self):
        self.disposed += 1
        self.stop()


class SettingsStub:
    def __init__(self):
        self.values = {'capture_mode': 'desktop', 'target_hwnd': 0}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        return True


def install_fake_factory(monkeypatch):
    module = types.ModuleType('core.window_preview')
    module.backends = []

    def factory(parent=None):
        backend = FakePreviewBackend(parent)
        module.backends.append(backend)
        return backend

    module.create_window_preview_backend = factory
    monkeypatch.setitem(sys.modules, 'core.window_preview', module)
    return module


def test_preview_is_lazy_tracks_exact_selected_window_and_clears_stale_frame(
        qapp, monkeypatch):
    fake = install_fake_factory(monkeypatch)
    host = QWidget()
    preview = WindowPreview(host)
    preview.set_window({'hwnd': 101, 'pid': 10, 'title': '<Firefox A>'})
    assert fake.backends == []

    host.show()
    qapp.processEvents()
    assert len(fake.backends) == 1
    first = fake.backends[0]
    assert first.started[0][0]['hwnd'] == 101
    assert first.started[0][1] == int(host.winId())
    assert preview.caption.text() == '<Firefox A>'
    assert preview.caption.toolTip() == '<Firefox A>'

    long_title = '<Firefox with a title long enough that it cannot fit in the narrow preview caption>'
    preview.set_window({'hwnd': 101, 'pid': 10, 'title': long_title})
    qapp.processEvents()
    assert preview.caption.text() != long_title
    assert preview.caption.toolTip() == long_title

    old_image = QImage(16, 9, QImage.Format.Format_RGB32)
    old_image.fill(QColor('red'))
    old_slot = preview._image_slot
    first.image_ready.emit(old_image)
    qapp.processEvents()
    assert not preview.image_label.pixmap().isNull()

    preview.set_window({'hwnd': 202, 'pid': 20, 'title': 'Firefox B'})
    assert preview.image_label.pixmap().isNull()
    assert first.stopped >= 1
    qapp.processEvents()
    assert len(fake.backends) == 1
    second = fake.backends[0]
    assert second.started[-1][0]['hwnd'] == 202
    old_slot(old_image)
    assert preview.image_label.pixmap().isNull()
    second.status_changed.emit('Live window previews require a native X11 session.')
    assert preview.image_label.text() == (
        'Live window previews require a native X11 session.')
    second.status_changed.emit('The selected window is minimized.')
    assert preview.image_label.text() == 'The selected window is minimized.'
    second.status_changed.emit('Live window preview')
    assert preview.image_label.text() == ''
    preview.set_window({'hwnd': 202, 'pid': 20, 'title': 'Firefox B refreshed'},
                       force_refresh=True)
    assert len(fake.backends) == 1
    assert second.started[-1][0]['title'] == 'Firefox B refreshed'

    preview.set_window(None)
    assert preview.caption.text() == ''
    assert preview.image_label.text() == 'Select a window to preview'
    assert second.stopped >= 1
    host.close()


def test_preview_lifecycle_geometry_and_letterboxing(qapp, monkeypatch):
    fake = install_fake_factory(monkeypatch)
    host = QWidget()
    host.resize(500, 400)
    preview = WindowPreview(host, eyebrow=False)
    preview.set_window({'hwnd': 55, 'title': 'Portrait Game'})
    preview.setGeometry(30, 40, 260, 150)
    host.show()
    qapp.processEvents()

    backend = fake.backends[0]
    _window, destination_hwnd, rect = backend.started[0]
    assert destination_hwnd == int(host.winId())
    assert rect[0] >= 0 and rect[1] >= 0
    assert rect[2] > rect[0] and rect[3] > rect[1]

    portrait = QImage(9, 16, QImage.Format.Format_RGB32)
    portrait.fill(QColor('blue'))
    backend.image_ready.emit(portrait)
    qapp.processEvents()
    pixmap = preview.image_label.pixmap()
    assert not pixmap.isNull()
    assert pixmap.height() > pixmap.width()

    preview.resize(280, 160)
    QTest.qWait(100)
    assert backend.geometry
    host.hide()
    qapp.processEvents()
    assert backend.stopped >= 1
    assert preview._backend is backend
    assert not preview._backend_running
    preview.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert backend.disposed == 1


def test_preview_theme_and_minimized_mode_do_not_initialize_backend(qapp, monkeypatch):
    fake = install_fake_factory(monkeypatch)
    from ui import style

    monkeypatch.setattr(style.Colors, 'SURFACE_1', '#123456')
    host = QWidget()
    preview = WindowPreview(host)
    preview.set_window({'hwnd': 8, 'title': 'The selected target'})
    preview.hide()
    preview.refresh_theme()
    assert fake.backends == []

    preview.set_window(None)
    assert fake.backends == []
    assert preview.eyebrow.text() == 'SELECTED WINDOW PREVIEW'
    assert '#123456' in preview.styleSheet()


def test_source_popup_syncs_selection_refresh_and_desktop_hide(qtbot, monkeypatch):
    import main

    fake = install_fake_factory(monkeypatch)
    window = {'hwnd': 91, 'pid': 14, 'title': 'Selected Firefox',
              'display_name': 'Firefox', 'is_game': False}
    windows = [window]
    monkeypatch.setattr(main, '_enumerate_capturable_windows', lambda: list(windows))
    popup = main.SourcePopup(SettingsStub())
    qtbot.addWidget(popup)

    assert not popup.window_preview.isVisibleTo(popup)
    assert popup.set_capture_window(window)
    assert popup.window_preview._entry['hwnd'] == 91
    assert popup.window_combo.itemData(0, 3) == 'Selected Firefox'
    assert popup.window_preview.isVisibleTo(popup)
    popup.show()
    qtbot.wait(20)
    assert len(fake.backends) == 1
    assert 100 <= popup.window_preview.image_label.height() <= 146

    backend = fake.backends[0]
    backend.status_changed.emit('The selected window is minimized.')
    refreshed = dict(window, title='Selected Firefox refreshed')
    windows[:] = [refreshed]
    popup._refresh_windows()
    assert popup.window_preview._entry['title'] == 'Selected Firefox refreshed'
    assert len(backend.started) == 2

    windows.clear()
    popup._refresh_windows()
    assert popup.window_preview._entry is None

    popup.set_capture_window(window)
    popup.show()
    qtbot.wait(20)
    assert popup.window_preview._backend_running
    popup.set_capture_desktop()
    assert popup.window_preview._entry is None
    assert not popup.window_preview.isVisibleTo(popup)
    assert backend.stopped >= 2  # refresh-list clear, then desktop transition

    popup.set_capture_window(window)
    popup.show()
    qtbot.wait(20)
    assert popup.window_preview._backend_running
    popup.hide()
    qtbot.wait(20)
    assert not popup.window_preview._backend_running


def test_source_popup_natural_height_is_not_a_small_screen_minimum(qtbot, monkeypatch):
    import main

    install_fake_factory(monkeypatch)
    monkeypatch.setattr(main, '_enumerate_capturable_windows', lambda: [])
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])
    settings = SettingsStub()
    settings.values['capture_mode'] = 'window'
    popup = main.SourcePopup(settings)
    qtbot.addWidget(popup)

    class SmallScreen:
        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 420, 270)

    screen = SmallScreen()
    monkeypatch.setattr(main.QApplication, 'screenAt', lambda *_args: screen)
    monkeypatch.setattr(main.QApplication, 'primaryScreen', lambda: screen)
    anchor = QWidget()
    anchor.resize(70, 24)
    anchor.show()
    popup.show_below(anchor)
    assert popup.height() <= 254
    assert popup.layout().sizeConstraint() == popup.layout().SizeConstraint.SetMinimumSize
