"""Small original-media player: deliberately independent of the clip editor."""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout


class BasicClipViewer(QDialog):
    def __init__(self, clip_path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f'{Path(clip_path).name} — Clip viewer')
        self.resize(960, 600)
        layout = QVBoxLayout(self)
        note = QLabel('Original clip • Editing is disabled in Settings → Performance')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.video = QVideoWidget(self)
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.seek = QSlider(Qt.Orientation.Horizontal)
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
        volume = QSlider(Qt.Orientation.Horizontal)
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
        self.player.setSource(QUrl.fromLocalFile(str(Path(clip_path).resolve())))

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
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)

    def closeEvent(self, event):
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)
