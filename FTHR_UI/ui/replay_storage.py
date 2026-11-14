"""Settings controls for the rolling replay buffer."""

from pathlib import Path
from threading import Thread
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit
from PySide6.QtWidgets import QCheckBox, QPushButton, QSlider, QVBoxLayout, QWidget

from core.capture_settings import audio_plan_from_settings, compute_buffer_seconds
from core.replay_storage import (
    MIB, MIN_BUDGET_MIB, REPLAY_STORAGE_KEY,
    ReplayStorageSettings, estimate_replay_disk_bytes, inspect_replay_folder,
)
from ui.style import (
    Colors, Fonts, Sizes, button_outline_qss, combo_qss,
    checkbox_qss, label_body, label_uppercase, lineedit_qss, set_theme_style, slider_qss,
)


_MB = 10 ** 6
_GB = 10 ** 9
_SMALL_STOPS_MB = (100, 250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000)
_RAM_STOPS_MB = (*_SMALL_STOPS_MB, 5000, 6000, 8000, 10_000, 12_000, 14_000, 16_000)
_DISK_STOPS_MB = (*_SMALL_STOPS_MB, 5000, 7500, 10_000, 15_000, 20_000, 30_000,
                  50_000, 75_000, 100_000, 150_000, 250_000, 500_000, 750_000, 1_000_000)


def _space_text(size):
    if size >= _GB:
        return f'{size / _GB:,.2f}'.rstrip('0').rstrip('.') + ' GB'
    return f'{size / _MB:,.0f} MB'


class _BudgetSlider(QSlider):
    def __init__(self, stops_mb, saved_mib, readout, hint):
        super().__init__(Qt.Orientation.Horizontal)
        self._stops = tuple((mb * _MB + MIB - 1) // MIB for mb in stops_mb)
        self._labels = tuple(_space_text(mb * _MB) for mb in stops_mb)
        self._mib = saved_mib
        self._readout = readout
        self._hint = hint
        self.setMinimumHeight(28)
        self.setRange(0, len(self._stops) - 1)
        self.setPageStep(3)
        self.setTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setValue(min(range(len(self._stops)),
                          key=lambda index: abs(self._stops[index] - saved_mib)))
        set_theme_style(self, lambda: slider_qss() + (
            f'QSlider::handle:horizontal:focus {{ border: 2px solid {Colors.TEXT}; '
            'width: 8px; height: 8px; }'
        ))
        self._update_readout()
        self.valueChanged.connect(self._select_stop)

    def amount_mib(self):
        return self._mib

    def _select_stop(self, index):
        self._mib = self._stops[index]
        self._update_readout()

    def _update_readout(self):
        # An older budget can land between stops. Keep it until the slider moves.
        index = self.value()
        text = (self._labels[index] if self._mib == self._stops[index]
                else _space_text(self._mib * MIB))
        self._readout.setText(text)
        description = f'{text}. {self._hint}'
        self.setAccessibleDescription(description)
        self.setToolTip(description)


class ReplayStorageWidget(QWidget):
    applied = Signal()
    _folder_checked = Signal(int, object)

    def __init__(self, settings, bitrate_presets, combo_type, spin_type, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._bitrate_presets = bitrate_presets
        self._saved = ReplayStorageSettings.from_value(settings.get(REPLAY_STORAGE_KEY))
        self._folder = self._saved.folder
        self._probe_epoch = 0
        self._pending_probe_path = None
        self._folder_status = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(450)
        self._save_timer.timeout.connect(self._apply)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.mode_combo = combo_type()
        self.mode_combo.addItem('Memory (RAM)', 'memory')
        self.mode_combo.addItem('Storage (disk)', 'disk')
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(self._saved.mode))
        set_theme_style(self.mode_combo, combo_qss)
        self._add_row(layout, 'Storage mode', self.mode_combo)
        self.mode_detail = self._detail(layout, 'TEXT_DIM')

        self.budget_mode_combo = combo_type()
        self.budget_mode_combo.addItem('Automatic', 'auto')
        self.budget_mode_combo.addItem('Manual', 'manual')
        self.budget_mode_combo.setCurrentIndex(
            self.budget_mode_combo.findData(self._saved.budget_mode))
        set_theme_style(self.budget_mode_combo, combo_qss)
        self._add_row(layout, 'Budget mode', self.budget_mode_combo)
        self.budget_detail = self._detail(layout, 'TEXT_DIM')
        self.budget_detail.setText(
            'Sizes the buffer from your capture settings and available RAM or disk space.')

        self.memory_budget_row, self.memory_budget = self._add_budget(
            layout, 'RAM budget', _RAM_STOPS_MB, self._saved.memory_mib,
            'Maximum memory for replay data and saves.')

        self.disk_details = QWidget()
        disk_layout = QVBoxLayout(self.disk_details)
        disk_layout.setContentsMargins(0, 0, 0, 0)
        disk_layout.setSpacing(8)
        self.disk_budget_row, self.disk_budget = self._add_budget(
            disk_layout, 'Disk budget', _DISK_STOPS_MB, self._saved.disk_mib,
            'Maximum disk space for temporary replay data.')

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setMinimumWidth(0)
        set_theme_style(self.folder_edit, lineedit_qss)
        folder_row.addWidget(self.folder_edit, 1)
        self.browse_button = QPushButton('Browse')
        self.browse_button.setAccessibleName('Browse for a temporary replay folder')
        set_theme_style(self.browse_button, button_outline_qss)
        self.browse_button.clicked.connect(self._browse)
        folder_row.addWidget(self.browse_button)
        self._add_row(disk_layout, 'Replay folder', self.folder_edit, row=folder_row)

        self.space_label = self._detail(disk_layout, 'TEXT_DIM')
        self.capacity_warning = self._detail(disk_layout, 'WARNING')
        self.drive_advice = self._detail(disk_layout, 'WARNING')
        guidance = self._detail(disk_layout, 'TEXT_DIM')
        guidance.setText(
            'Use a dedicated hard drive (HDD) for continuous replay writes to reduce wear '
            'on an expensive SSD. If the drive cannot sustain your capture bitrate, use a '
            'faster drive or lower the bitrate.')
        self.folder_error = self._detail(disk_layout, 'ERROR')
        layout.addWidget(self.disk_details)

        self.allow_low_resources_check = QCheckBox('Attempt saves with low resources')
        self.allow_low_resources_check.setChecked(self._saved.allow_low_resources)
        set_theme_style(self.allow_low_resources_check, checkbox_qss)
        self.allow_low_resources_check.setToolTip(
            'Continue when estimated RAM or temporary storage needs exceed availability. '
            'Actual memory and write limits still apply.')
        layout.addWidget(self.allow_low_resources_check)
        self.low_resources_detail = self._detail(layout, 'WARNING')
        self.low_resources_detail.setText('Warn and try anyway. Capture may stop or a clip may fail to save.')

        hint = self._detail(layout, 'TEXT_DIM')
        hint.setText('Changes restart capture and clear the current replay history.')
        self.save_error = self._detail(layout, 'ERROR')
        self.save_error.hide()

        self.allow_low_resources_check.toggled.connect(self._schedule_apply)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.budget_mode_combo.currentIndexChanged.connect(self._schedule_apply)
        self.memory_budget.valueChanged.connect(self._schedule_apply)
        self.disk_budget.valueChanged.connect(self._schedule_apply)
        self._folder_checked.connect(self._on_folder_checked)
        self._refresh()

    def _add_budget(self, layout, name, stops, saved_mib, hint):
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        value = QLabel()
        value.setMinimumWidth(88)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_theme_style(value, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY))
        slider = _BudgetSlider(stops, saved_mib, value, hint)
        row.addWidget(slider, 1)
        row.addWidget(value)
        self._add_row(container_layout, name, slider, row=row)
        layout.addWidget(container)
        return container, slider

    @staticmethod
    def _detail(layout, color, stretch=0):
        detail = QLabel()
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setWordWrap(True)
        set_theme_style(detail, lambda: label_body(getattr(Colors, color), Fonts.SIZE_BODY))
        layout.addWidget(detail, stretch)
        return detail

    @staticmethod
    def _add_row(layout, text, field, *, row=None):
        fields = row if row is not None else QHBoxLayout()
        if row is None:
            fields.addWidget(field, 1)
        label = QLabel(text)
        label.setBuddy(field)
        field.setAccessibleName(text)
        label.setFixedWidth(140)
        set_theme_style(label, lambda: label_uppercase(
            Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL))
        fields.insertWidget(0, label)
        fields.setSpacing(Sizes.ROW_GAP)
        layout.addLayout(fields)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self.mode_combo.currentData() == 'disk':
            self._check_folder()
        self._refresh()

    def hideEvent(self, event):  # noqa: N802
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._apply()
        super().hideEvent(event)

    def _candidate(self):
        return ReplayStorageSettings(
            self.mode_combo.currentData(), self._folder,
            self.memory_budget.amount_mib(), self.disk_budget.amount_mib(),
            self.budget_mode_combo.currentData(),
            self.allow_low_resources_check.isChecked(),
        )

    def _mode_changed(self):
        if self.mode_combo.currentData() == 'disk':
            self._check_folder()
        self._schedule_apply()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, 'Select temporary replay folder', str(self._candidate().directory),
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self._folder = str(Path(folder))
            self._check_folder()
            self._schedule_apply()

    def _check_folder(self):
        directory = self._candidate().directory
        if directory == self._pending_probe_path:
            return
        self._probe_epoch += 1
        epoch = self._probe_epoch
        self._pending_probe_path = directory
        self._folder_status = None
        self._refresh()

        def check():
            status = inspect_replay_folder(directory)
            try:
                self._folder_checked.emit(epoch, status)
            except RuntimeError:
                # Settings can close while a removable drive is responding.
                pass

        Thread(target=check, name='fthr-replay-folder-probe', daemon=True).start()

    def _on_folder_checked(self, epoch, status):
        if epoch != self._probe_epoch:
            return
        self._pending_probe_path = None
        self._folder_status = status
        self._schedule_apply()

    def _estimated_bytes(self):
        settings = self._settings
        try:
            duration = compute_buffer_seconds(settings.get('clip_length', 30),
                                              settings.get('custom_clip_lengths', []))
        except ValueError:
            duration = compute_buffer_seconds(30, settings.get('custom_clip_lengths', []))
        resolution = str(settings.get('resolution', 'source'))
        presets = self._bitrate_presets.get(resolution, self._bitrate_presets['source'])
        quality = str(settings.get('bitrate_level', 'medium'))
        if quality == 'custom':
            try:
                bitrate = max(500, min(200_000, int(settings.get('custom_bitrate_kbps', 25_000))))
            except (TypeError, ValueError, OverflowError):
                bitrate = 25_000
        else:
            bitrate = presets.get(quality, presets['medium'])
        audio_tracks = 0
        if settings.get('audio_capture_enabled', True):
            audio_tracks = 1
            if sys.platform == 'win32':
                _, programs, devices = audio_plan_from_settings(settings)
                # Account for Windows system and microphone audio, selected
                # devices and the maximum of 32 retained program sources.
                audio_tracks = 2 + len(devices) + 32 * programs
        return estimate_replay_disk_bytes(duration, bitrate, audio_tracks)

    def _refresh(self):
        candidate = self._candidate()
        self.low_resources_detail.setVisible(candidate.allow_low_resources)
        disk = candidate.mode == 'disk'
        manual = candidate.budget_mode == 'manual'
        self.mode_detail.setText(
            'Continuously writes temporary replay data to disk. Capture and saves still use RAM.'
            if disk else 'Keeps replay video and audio in RAM.')
        self.disk_details.setVisible(disk)
        self.budget_detail.setVisible(not manual)
        self.memory_budget_row.setVisible(manual)
        self.disk_budget_row.setVisible(manual)
        if disk:
            directory = str(candidate.directory)
            self.folder_edit.setText(directory)
            self.folder_edit.setToolTip(directory)
            status = self._folder_status
            free = status.free_bytes if status else None
            error = status.error if status else ''
            estimate = self._estimated_bytes()
            required_free = estimate
            if not manual:
                # Native automatic storage rounds the quota up and reserves an extra chunk.
                quota_mib = max(MIN_BUDGET_MIB, (estimate + MIB - 1) // MIB)
                required_free = (quota_mib + 16) * MIB
            required_text = f'Estimated replay budget: {_space_text(required_free)}'
            if free is not None:
                free_text = f'{_space_text(free)} free on this drive.'
            else:
                free_text = 'Checking free space…' if not status else 'Free space unavailable.'
            self.space_label.setText(f'{required_text}. {free_text}')
            if free is not None and free < required_free:
                capacity = ('This drive has less free space than the replay estimate. '
                            'Free up space or choose another folder.')
            elif manual and candidate.disk_mib * MIB < estimate:
                capacity = ('The disk budget is below the replay estimate. Increase the budget '
                            'or reduce clip length or bitrate.')
            else:
                capacity = ''
            media = status.media if status and not error else None
            if media == 'hdd':
                advice = ('Hard drive (HDD) detected. Replay writes may compete with games '
                          'and saves on this drive.')
            elif media == 'unknown':
                advice = 'Drive type could not be identified.'
            else:
                advice = ''
            for label, value in ((self.capacity_warning, capacity), (self.folder_error, error),
                                 (self.drive_advice, advice)):
                label.setText(value)
                label.setVisible(bool(value))
            self.folder_edit.setAccessibleDescription(' '.join(
                value for value in (free_text, error, capacity, advice) if value))

    def _can_apply(self, candidate):
        return candidate != self._saved and (
            candidate.mode != 'disk'
            or (self._folder_status is not None and not self._folder_status.error)
        )

    def _schedule_apply(self):
        self._save_timer.stop()
        self.save_error.hide()
        self._refresh()
        if self._can_apply(self._candidate()):
            self._save_timer.start()

    def _apply(self):
        candidate = self._candidate()
        if not self._can_apply(candidate):
            return
        previous = self._settings.get(REPLAY_STORAGE_KEY)
        self._settings.set(REPLAY_STORAGE_KEY, candidate.to_dict())
        if self._settings.save_settings() is False:
            self._settings.set(REPLAY_STORAGE_KEY, previous)
            self.save_error.setText('Replay settings could not be saved. Change a setting to retry.')
            self.save_error.show()
            return
        self._saved = candidate
        self._refresh()
        self.applied.emit()
