"""GPU composition of full-resolution transition snapshots, with a raster fallback."""

import os
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal, Slot
from PySide6.QtGui import QImage, QColor
from PySide6.QtQml import QQmlEngine
from PySide6.QtQuick import QQuickImageProvider, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication


class _Snapshots(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.images = {}
        self.serial = 0

    def requestImage(self, image_id, size, requested_size):
        image = self.images.get(image_id, QImage())
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image


def create_snapshot_surface(parent):
    if (QApplication.platformName() in ('offscreen', 'minimal')
            or os.environ.get('QT_QUICK_BACKEND', '').lower() == 'software'):
        return None
    try:
        return SnapshotSurface(parent)
    except RuntimeError:
        return None


class SnapshotSurface(QQuickWidget):
    failed = Signal()

    def __init__(self, parent):
        # One QML engine per application, rather than recompiling the tiny
        # transition scene and reinitializing an engine for every arrow press.
        app = QApplication.instance()
        if not hasattr(app, '_snapshot_engine'):
            app._snapshot_engine = QQmlEngine(app)
            app._snapshot_provider = _Snapshots()
            app._snapshot_engine.addImageProvider('snapshots', app._snapshot_provider)
        super().__init__(app._snapshot_engine, parent)
        self.quickWindow().setPersistentSceneGraph(False)
        self._provider = app._snapshot_provider
        self._keys = []
        # The application owns the provider. A surface destroyed before its
        # transition is shown must still remove its images from that provider.
        provider, keys = self._provider, self._keys
        def discard_images(*_):
            for key in keys:
                provider.images.pop(key, None)
            keys.clear()
        self.destroyed.connect(discard_images)
        self._failed = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setClearColor(QColor('transparent'))
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name('qml') / 'Snapshot.qml')))
        if self.status() == QQuickWidget.Status.Error:
            self.deleteLater()
            raise RuntimeError('Snapshot scene unavailable')
        self.statusChanged.connect(self._check_status)
        self.quickWindow().sceneGraphError.connect(self._scene_graph_error)
        self.quickWindow().sceneGraphInitialized.connect(
            self._check_backend, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _check_backend(self):
        if self.quickWindow().rendererInterface().graphicsApi() == QSGRendererInterface.GraphicsApi.Software:
            self._fail()

    def _check_status(self, status):
        if status == QQuickWidget.Status.Error:
            self._fail()

    def _scene_graph_error(self, *_):
        self._fail()

    def _fail(self):
        if not self._failed:
            self._failed = True
            self.failed.emit()

    def set_images(self, outgoing, incoming=None):
        self.release_images()
        for prop, pixmap in (('outgoing', outgoing), ('incoming', incoming)):
            if pixmap is None or pixmap.isNull():
                continue
            self._provider.serial += 1
            key = str(self._provider.serial)
            self._keys.append(key)
            self._provider.images[key] = pixmap.toImage()
            self.rootObject().setProperty(prop, QUrl(f'image://snapshots/{key}'))

    def release_images(self):
        root = self.rootObject()
        if root is not None:
            root.setProperty('outgoing', QUrl())
            root.setProperty('incoming', QUrl())
        for key in self._keys:
            self._provider.images.pop(key, None)
        self._keys.clear()

    def set_slide(self, progress, horizontal, direction):
        root = self.rootObject()
        root.setProperty('flight', False)
        root.setProperty('progress', progress)
        root.setProperty('horizontal', horizontal)
        root.setProperty('direction', direction)

    def set_flight(self, rect, opacity):
        root = self.rootObject()
        root.setProperty('flight', True)
        root.setProperty('frameX', float(rect.x()))
        root.setProperty('frameY', float(rect.y()))
        root.setProperty('frameWidth', float(rect.width()))
        root.setProperty('frameHeight', float(rect.height()))
        root.setProperty('frameOpacity', opacity)
