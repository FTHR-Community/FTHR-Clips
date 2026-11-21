"""Small clip player independent of the clip editor."""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ui.clip_worker import CompatibilitySourceWorker
from ui.sliders import ClickToSeekSlider


class BasicClipViewer(QDialog):
    def __init__(self, clip_path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f'{Path(clip_path).name} — Clip viewer')
        self.resize(960, 600)
        layout = QVBoxLayout(self)
        self.note = QLabel('Editing is disabled in Settings → Performance')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.video = QVideoWidget(self)
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.seek = ClickToSeekSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName('Playback position')
        self.seek.setRange(0, 0)
        self.seek.valueChanged.connect(self.player.setPosition)
        self.player.durationChanged.connect(lambda duration: self.seek.setRange(0, duration))
        self.player.positionChanged.connect(self._position_changed)
        layout.addWidget(self.seek)
        controls = QHBoxLayout()
        self.play = QPushButton('Play')
        self.play.clicked.connect(self._toggle_playback)
        self.player.playbackStateChanged.connect(lambda state: self.play.setText(
            'Pause' if state == QMediaPlayer.PlaybackState.PlayingState else 'Play'))
        controls.addWidget(self.play)
        controls.addStretch()
        controls.addWidget(QLabel('Volume'))
        volume = ClickToSeekSlider(Qt.Orientation.Horizontal)
        volume.setAccessibleName('Volume')
        volume.setRange(0, 100)
        volume.setValue(80)
        volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        controls.addWidget(volume)
        layout.addLayout(controls)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.player.errorOccurred.connect(lambda *_: self.error.setText(self.player.errorString()))
        self._prepare_cancel = threading.Event()
        cancelled = self._prepare_cancel
        self.destroyed.connect(lambda *_: cancelled.set())
        self._clip_path = str(Path(clip_path).resolve())
        self.error.setText('Preparing clip…')
        self.play.setEnabled(False)
        worker = CompatibilitySourceWorker(self._clip_path, self._prepare_cancel)
        worker.signals.ready.connect(self._set_source, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _set_source(self, path):
        if not self._prepare_cancel.is_set():
            from core.playback_proxy import is_sdr_preview_path

            self.error.clear()
            if is_sdr_preview_path(self._clip_path, path):
                self.note.setText('HDR source · SDR preview')
            self.player.setSource(QUrl.fromLocalFile(path))
            self.play.setEnabled(True)

    def _position_changed(self, position):
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(position)
            self.seek.blockSignals(False)

    def _toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def done(self, result):
        self._release_source()
        super().done(result)

    def closeEvent(self, event):
        self._release_source()
        super().closeEvent(event)

    def _release_source(self):
        self._prepare_cancel.set()
        self.player.stop()
        self.player.setSource(QUrl())
