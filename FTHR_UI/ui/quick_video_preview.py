"""Qt Quick video presentation; decoded frames never pass through QImage here."""

import os
from pathlib import Path

from PySide6.QtCore import QUrl, Signal, Slot, Qt
from PySide6.QtGui import QColor, QVector4D
from PySide6.QtQuick import QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication


class QuickVideoPreview(QQuickWidget):
    gpu_failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # QQuickWidget honors this at hide time. Changing it only after the
        # widget is already hidden leaves its scene graph allocated on Windows.
        self.quickWindow().setPersistentSceneGraph(False)
        self.gpu_ready = True
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setSource(QUrl.fromLocalFile(str(
            Path(__file__).with_name('qml') / 'VideoPreview.qml')))
        if self.status() == QQuickWidget.Status.Error:
            detail = '; '.join(error.toString() for error in self.errors())
            self.deleteLater()
            raise RuntimeError(detail)
        self.rootObject().shaderFailed.connect(self._shader_failed)
        self.statusChanged.connect(self._check_status)
        self.quickWindow().sceneGraphError.connect(self._scene_graph_error)
        self.quickWindow().sceneGraphInitialized.connect(
            self._check_backend, Qt.ConnectionType.QueuedConnection)

    def release_idle_resources(self):
        if not self.isVisible():
            self.quickWindow().setPersistentSceneGraph(False)
            self.quickWindow().releaseResources()

    def resume_resources(self):
        # Qt reconstructs nodes on show while preserving QML state and sink.
        self.quickWindow().setPersistentSceneGraph(False)

    def videoSink(self):
        return self.rootObject().property('videoSink')

    @Slot()
    def _check_backend(self):
        if (self.quickWindow().rendererInterface().graphicsApi()
                == QSGRendererInterface.GraphicsApi.Software):
            self._shader_failed()

    def _check_status(self, status):
        if status == QQuickWidget.Status.Error:
            self._shader_failed()

    def _scene_graph_error(self, _error, _message):
        self._shader_failed()

    @Slot()
    def _shader_failed(self):
        if self.gpu_ready:
            self.gpu_ready = False
            self.gpu_failed.emit()

    def set_edit_state(self, crop_rect, effects, stretch_ratio, src_w, src_h,
                       background, accent):
        root = self.rootObject()
        values = effects or {}
        root.setProperty('backgroundColor', QColor(background))
        root.setProperty('accentColor', QColor(accent))
        root.setProperty('sourceAspect', max(1, src_w) / max(1, src_h))
        root.setProperty('stretchRatio', stretch_ratio)
        root.setProperty('exposure', values.get('exposure', 0) * 0.003)
        root.setProperty('contrast', 1.0 + values.get('contrast', 0) * 0.006)
        root.setProperty('saturation', 1.0 + values.get('saturation', 0) * 0.01)
        root.setProperty('temperature', values.get('temperature', 0) * 0.003)
        root.setProperty('sharpness', values.get('sharpness', 0) * 0.015)
        root.setProperty('vignette', values.get('vignette', 0) * 1.8 / 255.0)
        root.setProperty('effectsActive', any(values.values()) and self.gpu_ready)
        crop = crop_rect or (0, 0, src_w, src_h)
        root.setProperty('cropArea', QVector4D(
            crop[0] / max(1, src_w), crop[1] / max(1, src_h),
            crop[2] / max(1, src_w), crop[3] / max(1, src_h)))
        root.setProperty('cropActive', bool(crop_rect))
        root.setProperty('cropLabel', f'CROP  ·  {crop[2]} × {crop[3]}')


def quick_preview_supported():
    # Offscreen test runners cannot create a hardware scene graph.
    return (QApplication.platformName() not in ('offscreen', 'minimal')
            and os.environ.get('QT_QUICK_BACKEND', '').lower() != 'software'
            and (Path(__file__).with_name('qml') / 'color.frag.qsb').is_file())
