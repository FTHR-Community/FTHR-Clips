"""Capture-only presentation for Ultra Performance Mode."""

import subprocess
import sys
import threading

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from core.capture_settings import (
    AUDIO_CAPTURE_MODE_COMBINED, AUDIO_CAPTURE_MODE_SEPARATED, AUDIO_SOURCE_COMBINED,
    AUDIO_SOURCE_DEVICE, AUDIO_SOURCE_DEVICES, AUDIO_SOURCE_PROGRAMS, effective_audio_source_mode,
)
from core.windows_microphone_devices import (
    MicrophoneDiscoveryCancelled, MicrophoneDiscoveryError, discover_microphones,
    parse_microphone_inventory, playback_endpoint_label,
)
from ui.dialogs import FthrDialog
from ui.style import (
    Colors, Fonts, WheelSafeComboBox, button_outline_qss, button_primary_qss,
    checkbox_qss, combo_qss,
    set_theme_style,
)


class UltraPerformancePage(QWidget):
    """Keep the capture controls accessible without an active library surface."""

    capture_requested = Signal()
    source_requested = Signal()
    hotkeys_requested = Signal()
    audio_changed = Signal(bool)
    audio_devices_requested = Signal()
    save_requested = Signal()
    open_folder_requested = Signal()
    exit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ultraPerformancePage')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.addStretch(1)
        panel = QFrame(self)
        panel.setMaximumWidth(650)
        panel.setMinimumWidth(510)
        content = QVBoxLayout(panel)
        content.setSpacing(14)

        title = QLabel('Ultra Performance Mode')
        set_theme_style(title, lambda: (
            f'color: {Colors.TEXT}; font-family: {Fonts.BRAND}; '
            'font-size: 28px; font-weight: 700;'))
        content.addWidget(title)
        self.status = QLabel('Starting capture')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(self.status)
        explanation = QLabel(
            'Library activity, uploads, and clip effects are paused.')
        explanation.setWordWrap(True)
        set_theme_style(explanation, lambda: f'color: {Colors.TEXT_MUTED};')
        content.addWidget(explanation)

        controls = QGridLayout()
        controls.setHorizontalSpacing(16)
        controls.setVerticalSpacing(9)
        self.source = self._button('Desktop', self.source_requested)
        self.capture = self._button('Capture settings', self.capture_requested)
        self.hotkeys = self._button('Hotkeys', self.hotkeys_requested)
        self.audio = QCheckBox('Capture audio')
        self.audio.toggled.connect(self.audio_changed)
        set_theme_style(self.audio, checkbox_qss)
        audio_controls = QWidget()
        audio_layout = QHBoxLayout(audio_controls)
        audio_layout.setContentsMargins(0, 0, 0, 0)
        audio_layout.addWidget(self.audio)
        audio_layout.addWidget(self._button('Sources and devices', self.audio_devices_requested))
        audio_layout.addStretch(1)
        for row, (name, control) in enumerate((
                ('Source / monitor', self.source),
                ('Clip length / FPS / quality', self.capture),
                ('Save clip shortcut', self.hotkeys),
                ('Audio', audio_controls))):
            label = QLabel(name)
            label.setBuddy(self.audio if control is audio_controls else control)
            controls.addWidget(label, row, 0)
            controls.addWidget(control, row, 1)
        controls.setColumnStretch(1, 1)
        content.addLayout(controls)

        actions = QGridLayout()
        self.save = self._button('Save clip', self.save_requested)
        set_theme_style(self.save, button_primary_qss)
        actions.addWidget(self.save, 0, 0)
        actions.addWidget(self._button('Open Clips folder', self.open_folder_requested), 0, 1)
        actions.addWidget(self._button('Return to full app', self.exit_requested), 0, 2)
        content.addLayout(actions)
        outer.addWidget(panel, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

    @staticmethod
    def _button(text, signal):
        button = QPushButton(text)
        button.setMinimumHeight(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_theme_style(button, button_outline_qss)
        button.clicked.connect(signal)
        return button

    def set_capture_details(self, source, capture, hotkeys, audio_enabled):
        for button, value in (
                (self.source, source), (self.capture, capture), (self.hotkeys, hotkeys)):
            button.setToolTip(value)
            label = button.fontMetrics().elidedText(
                value, Qt.TextElideMode.ElideRight, 340)
            button.setText(label.replace('&', '&&'))
        blocked = self.audio.blockSignals(True)
        self.audio.setChecked(audio_enabled)
        self.audio.blockSignals(blocked)

class UltraAudioDialog(FthrDialog):
    """Edit capture sources with one bounded inventory request and no preview."""

    inventory_ready = Signal(object, object, str)
    settings_changed = Signal(object)

    def __init__(self, settings, engine_path, parent=None):
        super().__init__('Audio sources and devices', parent, width=490)
        self._settings = settings
        self._cancel = threading.Event()
        self._windows = sys.platform == 'win32'
        self._source_mode = effective_audio_source_mode(
            settings.get('audio_source_mode'),
            settings.get('audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED),
            settings.get('audio_device_tracks', []),
            settings.get('per_app_audio_tracks'))
        form = QFormLayout()
        self.source = self._combo()
        self.microphone = self._combo()
        self.playback = self._combo()
        if self._windows:
            for label, mode in (
                    ('Mix system audio and microphone', AUDIO_SOURCE_COMBINED),
                    ('System audio and microphone on separate tracks', AUDIO_SOURCE_DEVICE),
                    ('Separate program tracks', AUDIO_SOURCE_PROGRAMS),
                    ('Saved playback device tracks', AUDIO_SOURCE_DEVICES)):
                self.source.addItem(label, mode)
            self.source.setCurrentIndex(self.source.findData(self._source_mode))
            form.addRow('Audio source', self.source)
            form.addRow('Playback device', self.playback)
            self.source.currentIndexChanged.connect(self._sync_playback)
        form.addRow('Microphone', self.microphone)
        self.body_layout.addLayout(form)
        self._populate_devices(self.microphone, (), 'mic')
        self._populate_devices(self.playback, (), 'playback')
        self._sync_playback()
        self._initial_microphone = self._device_key(self.microphone)
        self._initial_playback = self._device_key(self.playback)

        self.status = QLabel('Looking for audio devices...')
        self.status.setWordWrap(True)
        self.body_layout.addWidget(self.status)
        close = QPushButton('Close')
        set_theme_style(close, button_outline_qss)
        close.clicked.connect(self.accept)
        self.action_layout.addStretch()
        self.action_layout.addWidget(close)
        for combo in (self.source, self.microphone, self.playback):
            combo.currentIndexChanged.connect(self._emit_changes)
        self.inventory_ready.connect(self._apply_inventory)
        threading.Thread(
            target=self._discover, args=(engine_path,),
            daemon=True, name='fthr-ultra-audio-inventory').start()

    @staticmethod
    def _combo():
        combo = WheelSafeComboBox()
        set_theme_style(combo, combo_qss)
        return combo

    def _sync_playback(self):
        self.playback.setEnabled(self.source.currentData() in (
            AUDIO_SOURCE_COMBINED, AUDIO_SOURCE_DEVICE))

    def _populate_devices(self, combo, devices, key):
        old = combo.currentData()
        saved_id = self._settings.get(f'{key}_device_id') or ''
        saved_name = self._settings.get(f'{key}_device_name') or ''
        selected = old if isinstance(old, tuple) else (saved_id, saved_name)
        combo.clear()
        combo.addItem('System default', ('', ''))
        for endpoint_id, label in devices:
            combo.addItem(label, (endpoint_id, label))
        for index in range(combo.count()):
            endpoint_id, name = combo.itemData(index)
            if (endpoint_id == selected[0]
                    and (endpoint_id or name == selected[1])):
                combo.setCurrentIndex(index)
                return
        combo.addItem(f'{selected[1] or "Selected device"} (unavailable)', selected)
        combo.setCurrentIndex(combo.count() - 1)

    def _discover(self, engine_path):
        microphones, playback, warning = [], [], ''
        try:
            query = None
            if not self._windows:
                try:
                    from sounddevice import query_devices
                    query = query_devices
                except ImportError:
                    pass
            result = discover_microphones(
                engine_path if self._windows else None, legacy_query=query,
                cancel_event=self._cancel)
            microphones = [
                (endpoint.endpoint_id, endpoint.display_name)
                for endpoint in result.native_endpoints if endpoint.is_active]
            microphones.extend(('', name) for name in result.legacy_indices)
            if result.native_error or result.legacy_error:
                warning = 'Some microphone devices could not be listed.'
            if self._windows and engine_path and not self._cancel.is_set():
                completed = subprocess.run(
                    [str(engine_path), '--list-playback-devices'], check=True,
                    capture_output=True, encoding='utf-8', text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                playback = [
                    (endpoint.endpoint_id, playback_endpoint_label(endpoint))
                    for endpoint in parse_microphone_inventory(completed.stdout, strict=True)
                    if endpoint.is_active]
        except MicrophoneDiscoveryCancelled:
            return
        except (OSError, ValueError, subprocess.SubprocessError, MicrophoneDiscoveryError):
            warning = 'Some audio devices could not be listed. Saved selections are available.'
        if not self._cancel.is_set():
            try:
                self.inventory_ready.emit(microphones, playback, warning)
            except RuntimeError:
                pass  # Dialog was closed as discovery finished.

    def _apply_inventory(self, microphones, playback, warning):
        with QSignalBlocker(self.microphone), QSignalBlocker(self.playback):
            self._populate_devices(self.microphone, microphones, 'mic')
            self._populate_devices(self.playback, playback, 'playback')
        self.status.setText(warning or 'Device changes restart capture.')

    def _emit_changes(self):
        values = self.values()
        if values:
            self.settings_changed.emit(values)

    def mark_saved(self):
        self._initial_microphone = self._device_key(self.microphone)
        self._initial_playback = self._device_key(self.playback)
        if self._windows:
            self._source_mode = self.source.currentData()

    @staticmethod
    def _device_key(combo):
        endpoint_id, name = combo.currentData()
        return (endpoint_id, '' if endpoint_id else name)

    def values(self):
        microphone, mic_name = self.microphone.currentData()
        values = {}
        if self._device_key(self.microphone) != self._initial_microphone:
            values.update({
                'mic_device_id': microphone or None,
                'mic_device_name': mic_name or None,
            })
        if self._windows:
            playback, playback_name = self.playback.currentData()
            mode = self.source.currentData()
            if mode != self._source_mode:
                values.update({
                    'audio_source_mode': mode,
                    'audio_capture_mode': (AUDIO_CAPTURE_MODE_COMBINED
                                           if mode == AUDIO_SOURCE_COMBINED
                                           else AUDIO_CAPTURE_MODE_SEPARATED),
                    'per_app_audio_tracks': mode == AUDIO_SOURCE_PROGRAMS,
                })
            if self._device_key(self.playback) != self._initial_playback:
                values.update({
                    'playback_device_id': playback,
                    'playback_device_name': playback_name,
                })
        return values

    def done(self, result):
        self._cancel.set()
        super().done(result)
