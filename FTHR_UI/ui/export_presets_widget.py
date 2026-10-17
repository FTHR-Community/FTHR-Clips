"""Small, fully wired editor for persistent export presets."""

from __future__ import annotations

from ui.icons import IconButton

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.export_profiles import ExportPreset, ExportPresetManager
from ui.style import set_theme_style
from ui.style import (
    Colors, Fonts, Sizes, button_outline_qss, button_primary_qss, checkbox_qss,
    combo_qss, label_body, WheelSafeComboBox,
)
from ui.dialogs import FthrDialog, FthrMessageDialog


class _PresetStepperMixin:
    """Give preset fields the same square −/+ controls as capture bitrate."""

    def _setup_stepper(self):
        self.setObjectName('presetStepper')
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setFixedHeight(34)
        self.lineEdit().setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._minus = IconButton('decrease.png', self, label='Decrease')
        self._minus.setObjectName('presetStepDown')
        self._minus.setToolTip('Decrease')
        self._minus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._minus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._minus.clicked.connect(self.stepDown)

        self._plus = IconButton('increase.png', self, label='Increase')
        self._plus.setObjectName('presetStepUp')
        self._plus.setToolTip('Increase')
        self._plus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._plus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._plus.clicked.connect(self.stepUp)

        self.valueChanged.connect(self._sync_stepper)
        self._sync_stepper()
        set_theme_style(self, lambda: (f'''
            QSpinBox#presetStepper,
            QDoubleSpinBox#presetStepper {{
                background-color: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_BODY}px;
                font-weight: 600;
                padding: 6px 64px 6px 10px;
                selection-background-color: {Colors.ACCENT};
                selection-color: {Colors.BG};
            }}
            QSpinBox#presetStepper:hover,
            QSpinBox#presetStepper:focus,
            QDoubleSpinBox#presetStepper:hover,
            QDoubleSpinBox#presetStepper:focus {{
                border-color: {Colors.ACCENT};
            }}
            QPushButton#presetStepDown,
            QPushButton#presetStepUp {{
                background-color: {Colors.SURFACE_2};
                border: none;
                border-left: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.ACCENT};
                font-family: {Fonts.DISPLAY};
                font-size: 14px;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton#presetStepDown:hover,
            QPushButton#presetStepUp:hover {{
                background-color: {Colors.ACCENT};
                color: {Colors.BG};
            }}
            QPushButton#presetStepDown:pressed,
            QPushButton#presetStepUp:pressed {{
                background-color: {Colors.TEXT};
                color: {Colors.BG};
            }}
            QPushButton#presetStepDown:disabled,
            QPushButton#presetStepUp:disabled {{
                color: {Colors.TEXT_MUTED};
                background-color: {Colors.SURFACE_2};
            }}
        '''))

    def resizeEvent(self, event):  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        button_w = 28
        button_h = max(0, self.height() - 2)
        self._minus.setGeometry(
            self.width() - button_w * 2 - 1, 1, button_w, button_h)
        self._plus.setGeometry(
            self.width() - button_w - 1, 1, button_w, button_h)

    def _sync_stepper(self, *_args):
        enabled = self.isEnabled()
        self._minus.setEnabled(enabled and self.value() > self.minimum())
        self._plus.setEnabled(enabled and self.value() < self.maximum())


class _PresetSpinBox(_PresetStepperMixin, QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_stepper()


class _PresetDoubleSpinBox(_PresetStepperMixin, QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_stepper()


class ExportPresetDialog(FthrDialog):
    def __init__(self, preset: ExportPreset | None = None, parent=None):
        super().__init__(
            'Edit export preset' if preset else 'New export preset',
            parent,
            width=420,
        )
        self._original = preset
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.name = QLineEdit(preset.name if preset else '')
        set_theme_style(self.name, lambda: (f'''
            QLineEdit {{
                background: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                padding: 7px 9px;
            }}
            QLineEdit:focus {{ border-color: {Colors.ACCENT}; }}
        '''))
        form.addRow('Name', self.name)
        self.size = _PresetDoubleSpinBox()
        self.size.setRange(1, 10_000)
        self.size.setDecimals(1)
        self.size.setSuffix(' MB')
        self.size.setValue(preset.target_size_mb if preset else 25.0)
        form.addRow('Target file size', self.size)

        self.limit_resolution = QCheckBox('Limit resolution')
        set_theme_style(self.limit_resolution, checkbox_qss)
        self.limit_resolution.setChecked(bool(preset and preset.target_width))
        form.addRow('', self.limit_resolution)
        dimensions = QHBoxLayout()
        self.width = _PresetSpinBox()
        self.width.setRange(16, 8192)
        self.width.setSingleStep(2)
        self.width.setValue(preset.target_width if preset and preset.target_width else 1920)
        self.height = _PresetSpinBox()
        self.height.setRange(16, 8192)
        self.height.setSingleStep(2)
        self.height.setValue(preset.target_height if preset and preset.target_height else 1080)
        dimensions.addWidget(self.width)
        dimensions.addWidget(QLabel('×'))
        dimensions.addWidget(self.height)
        form.addRow('Resolution', dimensions)

        self.limit_fps = QCheckBox('Limit frame rate')
        set_theme_style(self.limit_fps, checkbox_qss)
        self.limit_fps.setChecked(bool(preset and preset.target_fps))
        form.addRow('', self.limit_fps)
        self.fps = _PresetDoubleSpinBox()
        self.fps.setRange(1, 240)
        self.fps.setDecimals(2)
        self.fps.setValue(preset.target_fps if preset and preset.target_fps else 60)
        self.fps.setSuffix(' FPS')
        form.addRow('Frame rate', self.fps)

        self.codec = WheelSafeComboBox()
        self.codec.addItem('H.264 (reviewed runtime)', 'h264')
        set_theme_style(self.codec, combo_qss)
        form.addRow('Video codec', self.codec)
        self.audio = _PresetSpinBox()
        self.audio.setRange(32, 512)
        self.audio.setValue(preset.audio_bitrate_kbps if preset else 128)
        self.audio.setSuffix(' kbps AAC')
        form.addRow('Audio', self.audio)
        self.body_layout.addLayout(form)

        cancel = QPushButton('Cancel')
        set_theme_style(cancel, button_outline_qss)
        cancel.clicked.connect(self.reject)
        save = QPushButton('Save preset')
        set_theme_style(save, button_primary_qss)
        save.clicked.connect(self._validate_and_accept)
        self.action_layout.addStretch()
        self.action_layout.addWidget(cancel)
        self.action_layout.addWidget(save)
        self.limit_resolution.toggled.connect(self._sync)
        self.limit_fps.toggled.connect(self._sync)
        self._sync()

    def _sync(self):
        self.width.setEnabled(self.limit_resolution.isChecked())
        self.height.setEnabled(self.limit_resolution.isChecked())
        self.fps.setEnabled(self.limit_fps.isChecked())

    def preset(self) -> ExportPreset:
        preset_id = self._original.preset_id if self._original else ''
        return ExportPreset(
            preset_id=preset_id,
            name=self.name.text().strip(),
            target_size_mb=self.size.value(),
            target_width=self.width.value() if self.limit_resolution.isChecked() else None,
            target_height=self.height.value() if self.limit_resolution.isChecked() else None,
            target_fps=self.fps.value() if self.limit_fps.isChecked() else None,
            video_codec=str(self.codec.currentData() or 'h264'),
            audio_bitrate_kbps=self.audio.value(),
        ).validated()

    def _validate_and_accept(self):
        try:
            self.preset()
        except ValueError as exc:
            FthrMessageDialog.warning(self, 'Preset is not valid', str(exc))
            return
        self.accept()


class ExportPresetsWidget(QWidget):
    presets_changed = Signal()

    def __init__(self, manager: ExportPresetManager | None = None, parent=None):
        super().__init__(parent)
        self.manager = manager or ExportPresetManager()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        row = QHBoxLayout()
        self.combo = WheelSafeComboBox()
        set_theme_style(self.combo, combo_qss)
        row.addWidget(self.combo, 1)
        self.new_btn = QPushButton('New')
        set_theme_style(self.new_btn, button_primary_qss)
        self.new_btn.clicked.connect(self._new)
        row.addWidget(self.new_btn)
        self.edit_btn = QPushButton('Edit')
        set_theme_style(self.edit_btn, button_outline_qss)
        self.edit_btn.clicked.connect(self._edit)
        row.addWidget(self.edit_btn)
        self.delete_btn = QPushButton('Delete')
        set_theme_style(self.delete_btn, button_outline_qss)
        self.delete_btn.clicked.connect(self._delete)
        row.addWidget(self.delete_btn)
        root.addLayout(row)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        set_theme_style(self.detail, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        root.addWidget(self.detail)
        self.combo.currentIndexChanged.connect(self._sync)
        self.refresh()

    def selected(self) -> ExportPreset | None:
        return self.manager.get(str(self.combo.currentData() or ''))

    def refresh(self, select_id: str = ''):
        current = select_id or str(self.combo.currentData() or '')
        self.combo.blockSignals(True)
        self.combo.clear()
        for preset in self.manager.all():
            suffix = ' · Built-in' if preset.built_in else ''
            self.combo.addItem(preset.name + suffix, preset.preset_id)
        index = self.combo.findData(current)
        self.combo.setCurrentIndex(index if index >= 0 else 0)
        self.combo.blockSignals(False)
        self._sync()

    def _sync(self):
        preset = self.selected()
        built_in = not preset or preset.built_in
        self.edit_btn.setEnabled(not built_in)
        self.delete_btn.setEnabled(not built_in)
        if not preset:
            self.detail.clear()
            return
        constraints = [f'{preset.target_size_mb:g} MB target']
        if preset.target_width:
            constraints.append(f'{preset.target_width}×{preset.target_height} max')
        if preset.target_fps:
            constraints.append(f'{preset.target_fps:g} FPS max')
        constraints.append(f'AAC {preset.audio_bitrate_kbps} kbps')
        self.detail.setText(' · '.join(constraints))

    def _new(self):
        dialog = ExportPresetDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        preset = self.manager.save(dialog.preset())
        self.refresh(preset.preset_id)
        self.presets_changed.emit()

    def _edit(self):
        preset = self.selected()
        if not preset or preset.built_in:
            return
        dialog = ExportPresetDialog(preset, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        saved = self.manager.save(dialog.preset())
        self.refresh(saved.preset_id)
        self.presets_changed.emit()

    def _delete(self):
        preset = self.selected()
        if not preset or preset.built_in:
            return
        answer = FthrMessageDialog.question(
            self, 'Delete export preset?', f'Delete “{preset.name}”?',
        )
        if not answer:
            return
        self.manager.delete(preset.preset_id)
        self.refresh()
        self.presets_changed.emit()


__all__ = ['ExportPresetDialog', 'ExportPresetsWidget']
