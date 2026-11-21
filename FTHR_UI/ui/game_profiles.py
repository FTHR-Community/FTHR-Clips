"""Passive editor for exact executable game capture profiles.

The profile page deliberately owns presentation and draft state only.  It does
not discover devices, start capture, or write ordinary application settings;
those responsibilities remain with SettingsPage/MainWindow.  A draft is
committed through the dedicated SettingsManager profile CAS API in one
operation, so a validation, disk, or compare-and-swap failure never discards
what the user has entered.
"""

from __future__ import annotations

import copy
import ntpath
import sys
from dataclasses import dataclass
from html import escape as _html_escape
from typing import Any, Callable
import uuid

from PySide6.QtCore import QSize, Qt, QSignalBlocker, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTabBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.game_profiles import (
    _GLOBAL_DEFAULTS,
    _validate_leaf,
    ProfileDataError,
    ProfileValidationError,
    canonical_exe_path,
    create_profile,
    delete_profile,
    update_profile,
)
from ui.dialogs import FthrDialog, FthrInputDialog, FthrMessageDialog
from ui.capture_admission import _detached_window_record
from ui.number_spinbox import NumberSpinBox, NumberDoubleSpinBox
from ui.style import (
    Colors,
    Fonts,
    Sizes,
    WheelSafeComboBox,
    button_outline_qss,
    button_primary_qss,
    checkbox_qss,
    combo_qss,
    context_menu_qss,
    label_body,
    set_theme_style,
)


_GROUP_TITLES = {
    'clips': 'Replay / Clips',
    'recording': 'Recording',
    'audio': 'Audio',
    'camera': 'Camera',
}

# This stable view mirrors the profile whitelist. Keeping it explicit makes the
# UI order deterministic while ensuring no ordinary preference can leak into a
# profile override by accident.
_LEAVES = (
    ('clips', 'clip_length', 'Clip length', 'int'),
    ('clips', 'framerate', 'Frame rate', 'fps'),
    ('clips', 'resolution', 'Resolution', 'resolution'),
    ('clips', 'bitrate_level', 'Bitrate quality', 'quality'),
    ('clips', 'custom_bitrate_kbps', 'Custom bitrate', 'bitrate'),
    ('recording', 'recording_framerate', 'Recording frame rate', 'fps'),
    ('recording', 'recording_resolution', 'Recording resolution', 'resolution'),
    ('recording', 'recording_bitrate_level', 'Recording bitrate quality', 'quality'),
    ('recording', 'recording_custom_bitrate_kbps', 'Recording custom bitrate', 'bitrate'),
    ('audio', 'audio_capture_enabled', 'Capture audio', 'bool'),
    ('audio', 'audio_capture_mode', 'Capture mode', 'audio_mode'),
    ('audio', 'audio_source_mode', 'Audio source', 'source_mode'),
    ('audio', 'audio_device_tracks', 'Audio device tracks', 'tracks'),
    ('audio', 'per_app_audio_tracks', 'Per-app audio', 'tri_bool'),
    ('audio', 'mic_device_id', 'Microphone', 'microphone'),
    ('audio', 'playback_device_id', 'Playback device', 'playback'),
    ('audio', 'mic_downmix_mono', 'Mono microphone', 'bool'),
    ('audio', 'mic_volume', 'Microphone volume', 'volume'),
    ('camera', 'camera_enabled', 'Camera enabled', 'bool'),
    ('camera', 'camera_device_index', 'Camera device', 'camera'),
    ('camera', 'camera_overlay_rect', 'Replay clips only: camera rectangle', 'rect'),
)

_ENUMS = {
    'resolution': (('Source', 'source'), ('480p', '480p'), ('720p', '720p'),
                   ('1080p', '1080p'), ('1440p', '1440p')),
    'quality': (('Low', 'low'), ('Medium', 'medium'), ('High', 'high'),
                ('Custom', 'custom')),
    'audio_mode': (('Combined', 'combined'), ('Separated', 'separated')),
    'source_mode': (('Automatic', None), ('All in one track', 'combined'),
                    ('System Audio plus Microphone', 'device'),
                    ('Programs', 'programs'), ('Selected devices', 'devices')),
    'tri_bool': (('Automatic', None), ('Disabled', False), ('Enabled', True)),
}


def _elide_path(path: str, width: int = 52) -> str:
    """Keep an executable path useful in a compact list without hiding it."""
    value = str(path or '')
    if len(value) <= width:
        return value
    tail = max(20, width - 12)
    return f'{value[:8]}…{value[-tail:]}'


def _tooltip_literal(value: Any) -> str:
    """Render dynamic text literally in Qt's auto-rich-text tooltips."""
    text = str(value or '')
    escaped = _html_escape(text, quote=True).replace('\r\n', '<br>').replace(
        '\r', '<br>').replace('\n', '<br>')
    return f'<qt>{escaped}</qt>'


def _friendly_record(record: Any, *, camera: bool = False) -> dict[str, Any]:
    """Detach one provider record without retaining a worker-owned object."""
    def read(name: str, default=None):
        if isinstance(record, dict):
            return record.get(name, default)
        return getattr(record, name, default)

    if camera:
        index = read('index', read('device_index', 0))
        try:
            index = int(index)
        except (TypeError, ValueError):
            return {}
        return {
            'index': index,
            'display_name': str(read('display_name', read('name', 'Camera')) or 'Camera'),
            'available': bool(read('available', True)),
            'is_default': bool(read('is_default', False)),
        }
    endpoint = read('endpoint_id', read('id', None))
    if endpoint is None:
        return {}
    return {
        'endpoint_id': str(endpoint),
        'display_name': str(read('display_name', read('name', endpoint)) or endpoint),
        'available': bool(read('available', True)),
        'is_default': bool(read('is_default', False)),
        'legacy_index': read('legacy_index', None),
    }


class _DraftDecisionDialog(FthrDialog):
    """Product-styled Save/Discard/Cancel prompt for draft navigation."""

    def __init__(self, parent=None):
        super().__init__('Unsaved profile changes', parent, width=430)
        message = QLabel('Save your profile changes before continuing?')
        message.setWordWrap(True)
        message.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(message, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.body_layout.addWidget(message)
        self.action_layout.addStretch(1)
        self.cancel = QPushButton('Cancel')
        self.discard = QPushButton('Discard')
        self.save = QPushButton('Save')
        for button in (self.cancel, self.discard, self.save):
            button.setMinimumHeight(Sizes.BUTTON_H)
        set_theme_style(self.cancel, button_outline_qss)
        set_theme_style(self.discard, button_outline_qss)
        set_theme_style(self.save, button_primary_qss)
        self.cancel.clicked.connect(self.reject)
        self.discard.clicked.connect(lambda: self.done(2))
        self.save.clicked.connect(lambda: self.done(1))
        self.action_layout.addWidget(self.cancel)
        self.action_layout.addWidget(self.discard)
        self.action_layout.addWidget(self.save)


@dataclass
class _Binding:
    group: str
    key: str
    kind: str
    toggle: QCheckBox
    editor: QWidget
    global_value: QLabel
    read: Callable[[], Any]
    write: Callable[[Any], None]


class _ResponsiveIdentity(QWidget):
    """Keep profile identity fields readable in both editor widths."""

    _COMPACT_BREAKPOINT = 480

    def __init__(self, name_edit: QLineEdit, exe_edit: QLineEdit, parent=None):
        super().__init__(parent)
        self._name_edit = name_edit
        self._exe_edit = exe_edit
        self._wide: bool | None = None
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(16)
        self._layout.setVerticalSpacing(6)
        self._labels = []
        for text in ('Profile name', 'App path'):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            self._labels.append(label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_layout(self.width() >= self._COMPACT_BREAKPOINT)

    def _apply_layout(self, wide: bool) -> None:
        if self._wide is wide:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        for column in range(2):
            self._layout.setColumnStretch(column, 0)
            self._layout.setColumnMinimumWidth(column, 0)
        for label in self._labels:
            label.setMinimumWidth(0)
        if wide:
            self._layout.addWidget(self._labels[0], 0, 0)
            self._layout.addWidget(self._name_edit, 0, 1)
            self._layout.addWidget(self._labels[1], 1, 0)
            self._layout.addWidget(self._exe_edit, 1, 1)
            self._layout.setColumnStretch(1, 1)
        else:
            self._layout.addWidget(self._labels[0], 0, 0)
            self._layout.addWidget(self._name_edit, 1, 0)
            self._layout.addWidget(self._labels[1], 2, 0)
            self._layout.addWidget(self._exe_edit, 3, 0)
            self._layout.setColumnStretch(0, 1)
        self._wide = wide
        self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802 - Qt API name
        self._apply_layout(self.width() >= self._COMPACT_BREAKPOINT)
        super().resizeEvent(event)


class _ResponsiveRectEditor(QWidget):
    """Four camera rectangle values with a compact 2x2 fallback."""

    _COMPACT_BREAKPOINT = 360

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact: bool | None = None
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)
        self._spins: dict[str, QDoubleSpinBox] = {}
        for name in ('x', 'y', 'w', 'h'):
            spin = NumberDoubleSpinBox()
            spin.setObjectName(f'cameraRect{name.upper()}')
            spin.setAccessibleName(f'Camera rectangle {name}')
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.01)
            spin.setDecimals(4)
            spin.setPrefix(f'{name}: ')
            spin.setMinimumWidth(0)
            spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._spins[name] = spin
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # A newly-created child can report a large default width before its
        # parent layout owns it.  Start compact so that this adapter never
        # advertises the wide four-column minimum during insertion.
        self._apply_layout(True)

    def _spin_minimum(self, name: str) -> tuple[int, int]:
        hint = self._spins[name].minimumSizeHint()
        return max(0, hint.width()), max(0, hint.height())

    def _wide_minimum_width(self) -> int:
        margins = self._layout.contentsMargins()
        return (
            sum(self._spin_minimum(name)[0] for name in ('x', 'y', 'w', 'h'))
            + self._layout.horizontalSpacing() * 3
            + margins.left() + margins.right())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API name
        left_width = max(self._spin_minimum('x')[0], self._spin_minimum('w')[0])
        right_width = max(self._spin_minimum('y')[0], self._spin_minimum('h')[0])
        margins = self._layout.contentsMargins()
        return QSize(
            max(0, left_width + right_width + self._layout.horizontalSpacing()
                + margins.left() + margins.right()),
            max(0, self._layout.minimumSize().height()),
        )

    def _apply_layout(self, compact: bool) -> None:
        if self._compact is compact:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        for column in range(4):
            self._layout.setColumnStretch(column, 0)
            self._layout.setColumnMinimumWidth(column, 0)
        if compact:
            self._layout.addWidget(self._spins['x'], 0, 0)
            self._layout.addWidget(self._spins['y'], 0, 1)
            self._layout.addWidget(self._spins['w'], 1, 0)
            self._layout.addWidget(self._spins['h'], 1, 1)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 1)
        else:
            for column, name in enumerate(('x', 'y', 'w', 'h')):
                self._layout.addWidget(self._spins[name], 0, column)
                self._layout.setColumnStretch(column, 1)
        self._compact = compact
        self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802 - Qt API name
        wide = (self.width() >= self._COMPACT_BREAKPOINT
                and self.width() >= self._wide_minimum_width())
        self._apply_layout(not wide)
        super().resizeEvent(event)


class _ResponsiveBindingRow(QFrame):
    """Adaptive binding row that never requires a horizontal editor scroll."""

    _COMPACT_BREAKPOINT = 620

    def __init__(self, title: QLabel, toggle: QCheckBox, editor: QWidget,
                 global_value: QLabel, parent=None):
        super().__init__(parent)
        self.setObjectName('profileLeafRow')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._wide: bool | None = None
        self._title = title
        self._toggle = toggle
        self._editor = editor
        self._global_value = global_value
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 6, 0, 6)
        self._layout.setHorizontalSpacing(12)
        self._layout.setVerticalSpacing(6)
        self._prepare_children()
        # See _ResponsiveRectEditor: the adapter must be shrinkable before
        # the parent can decide whether the wide layout actually fits.
        self._apply_layout(False)

    def _prepare_children(self) -> None:
        self._title.setMinimumWidth(0)
        self._title.setWordWrap(True)
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._toggle.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._editor.setMinimumWidth(0)
        self._editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._global_value.setMinimumWidth(0)
        self._global_value.setWordWrap(True)
        self._global_value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def _compact_minimum_width(self) -> int:
        margins = self._layout.contentsMargins()
        title_hint = self._title.minimumSizeHint()
        title_width = min(max(title_hint.width(), self._title.minimumWidth(), 80), 260)
        toggle_width = max(self._toggle.minimumSizeHint().width(), self._toggle.minimumWidth())
        header_width = title_width + toggle_width + self._layout.horizontalSpacing()
        editor_width = max(self._editor.minimumSizeHint().width(), self._editor.minimumWidth())
        global_hint = self._global_value.minimumSizeHint().width()
        global_width = max(180, min(320, global_hint))
        return max(header_width, editor_width, global_width) + margins.left() + margins.right()

    def _wide_minimum_width(self) -> int:
        margins = self._layout.contentsMargins()
        title_width = max(
            self._title.minimumSizeHint().width(),
            self._title.fontMetrics().horizontalAdvance(self._title.text()),
        )
        toggle_width = max(self._toggle.minimumSizeHint().width(), self._toggle.minimumWidth())
        editor_width = getattr(self._editor, '_wide_minimum_width', None)
        if callable(editor_width):
            editor_width = editor_width()
        else:
            editor_width = self._editor.minimumSizeHint().width()
        global_hint = self._global_value.minimumSizeHint().width()
        global_width = max(180, min(320, global_hint))
        return (title_width + toggle_width + max(0, int(editor_width)) + global_width
                + self._layout.horizontalSpacing() * 3
                + margins.left() + margins.right())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API name
        compact_width = self._compact_minimum_width()
        return QSize(
            max(0, compact_width),
            max(0, self._layout.minimumSize().height()),
        )

    def _apply_layout(self, wide: bool) -> None:
        if self._wide is wide:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        for column in range(4):
            self._layout.setColumnStretch(column, 0)
            self._layout.setColumnMinimumWidth(column, 0)
        if wide:
            self._title.setWordWrap(False)
            self._layout.setColumnMinimumWidth(0, 132)
            self._layout.setColumnMinimumWidth(1, 96)
            self._layout.addWidget(self._title, 0, 0)
            self._layout.addWidget(self._toggle, 0, 1)
            self._layout.addWidget(self._editor, 0, 2)
            self._layout.addWidget(self._global_value, 0, 3)
            self._layout.setColumnStretch(2, 1)
            self._layout.setColumnStretch(3, 1)
        else:
            self._title.setWordWrap(True)
            self._layout.addWidget(self._title, 0, 0)
            self._layout.addWidget(self._toggle, 0, 1)
            self._layout.addWidget(self._editor, 1, 0, 1, 2)
            self._layout.addWidget(self._global_value, 2, 0, 1, 2)
            self._layout.setColumnStretch(0, 1)
        self._wide = wide
        self.updateGeometry()

    @property
    def is_compact(self) -> bool:
        return self._wide is False

    def resizeEvent(self, event):  # noqa: N802 - Qt API name
        wide = (self.width() >= self._COMPACT_BREAKPOINT
                and self.width() >= self._wide_minimum_width())
        self._apply_layout(wide)
        super().resizeEvent(event)


class _TrackRows(QWidget):
    """Atomic, friendly editor for at most four persisted audio tracks."""

    changed = Signal()

    def __init__(self, owner: 'GameProfilesWidget', parent=None):
        super().__init__(parent)
        self.owner = owner
        self.rows: list[tuple[QLineEdit, WheelSafeComboBox]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for index in range(4):
            row = QHBoxLayout()
            row.setSpacing(6)
            name = QLineEdit()
            name.setPlaceholderText(f'Track {index + 1} name')
            name.setAccessibleName(f'Audio track {index + 1} name')
            name.setMinimumWidth(0)
            name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            set_theme_style(name, owner._line_edit_qss)
            combo = owner._make_combo()
            combo.setAccessibleName(f'Audio track {index + 1} device')
            row.addWidget(name, 1)
            row.addWidget(combo, 2)
            layout.addLayout(row)
            self.rows.append((name, combo))
            name.textChanged.connect(self.changed)
            combo.currentIndexChanged.connect(self.changed)

    def set_rows(self, value: Any):
        records = value if isinstance(value, (list, tuple)) else []
        for index, (name, combo) in enumerate(self.rows):
            record = records[index] if index < len(records) and isinstance(records[index], dict) else {}
            endpoint = str(record.get('id', '') or '')
            self.owner._fill_endpoint_combo(combo, self.owner._playback, endpoint,
                                            allow_default=False)
            with QSignalBlocker(name), QSignalBlocker(combo):
                name.setText(str(record.get('name', '') or ''))
                if endpoint:
                    found = combo.findData(endpoint)
                    if found >= 0:
                        if (combo.itemText(found) == 'Saved device (Unavailable)'
                                and record.get('name')):
                            label = f"{record['name']} (Unavailable)"
                            combo.setItemText(found, label)
                            combo.setItemData(
                                found, _tooltip_literal(label),
                                Qt.ItemDataRole.ToolTipRole)
                        combo.setCurrentIndex(found)
                    elif combo.findData(endpoint) < 0:
                        friendly = str(record.get('name', '') or 'Saved device')
                        label = f'{friendly} (Unavailable)'
                        combo.addItem(label, endpoint)
                        combo.setItemData(
                            combo.count() - 1, _tooltip_literal(label),
                            Qt.ItemDataRole.ToolTipRole)
                        combo.setCurrentIndex(combo.count() - 1)
                else:
                    combo.setCurrentIndex(-1)
                combo.setToolTip(_tooltip_literal(combo.currentText()))

    def value(self) -> list[dict[str, str]]:
        output = []
        for name, combo in self.rows:
            endpoint = combo.currentData()
            friendly = name.text().strip()
            # A completely empty row means absent.  Keep either half of a
            # partially entered row so Save can reject it without losing the
            # user's typing during an inventory refresh.
            if endpoint or friendly:
                output.append({'id': str(endpoint or ''), 'name': friendly})
        return output[:4]


class GameProfilesWidget(QWidget):
    """Passive exact-EXE profile editor with draft/CAS persistence."""

    catalog_changed = Signal(int)
    apply_requested = Signal()
    refresh_devices_requested = Signal()
    refresh_windows_requested = Signal()

    def __init__(self, settings_manager, parent=None, *, embedded=False):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._embedded = embedded
        self._catalog = None
        self._draft: dict[str, Any] | None = None
        self._canonical_draft: dict[str, Any] | None = None
        self._draft_is_new = False
        self._dirty = False
        self._loading = False
        self._future_catalog = False
        self._catalog_error = ''
        self._write_failed = False
        self._bindings: dict[str, _Binding] = {}
        self._microphones: list[dict[str, Any]] = []
        self._playback: list[dict[str, Any]] = []
        self._cameras: list[dict[str, Any]] = []
        self._capture_state: tuple[Any, Any, str] = (False, None, '')
        self._global_values: dict[str, Any] = {}
        self._global_errors: dict[str, str] = {}
        self._binding_rows: dict[str, _ResponsiveBindingRow] = {}

        self.setObjectName('gameProfilesWidget')
        self._build_ui()
        self._load_catalog()

    # ---------- presentation helpers ----------
    def _line_edit_qss(self) -> str:
        return f'''
            QLineEdit {{ background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px; color: {Colors.TEXT};
                font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px; padding: 6px 8px; }}
            QLineEdit:focus {{ border-color: {Colors.ACCENT}; }}
            QLineEdit:disabled {{ color: {Colors.TEXT_MUTED}; background: {Colors.SURFACE_1}; }}
        '''

    def _make_combo(self) -> WheelSafeComboBox:
        combo = WheelSafeComboBox()
        set_theme_style(combo, lambda: combo_qss(Colors.SURFACE_2))
        combo.setMinimumHeight(Sizes.INPUT_H)
        combo.setMinimumWidth(0)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(0)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        combo.currentIndexChanged.connect(
            lambda index, combo=combo: combo.setToolTip(
                _tooltip_literal(combo.itemText(index) if index >= 0 else '')))
        return combo

    def _button(self, text, *, primary=False):
        button = QPushButton(text)
        button.setMinimumHeight(Sizes.BUTTON_H)
        set_theme_style(button, button_primary_qss if primary else button_outline_qss)
        return button

    def _label(self, text='', *, heading=False):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        set_theme_style(label, lambda: label_body(
            Colors.TEXT if heading else Colors.TEXT_DIM,
            Fonts.SIZE_BODY_L if heading else Fonts.SIZE_BODY))
        return label

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_4)
        root.setSpacing(12)
        # Only errors interrupt the otherwise minimal profile list.
        status = QHBoxLayout()
        self._state_label = self._label()
        self._state_label.hide()
        status.addWidget(self._state_label, 1)
        self.reload_button = self._button('Reload profiles')
        self.reload_button.hide()
        self.reload_button.clicked.connect(self._reload_catalog)
        status.addWidget(self.reload_button)
        root.addLayout(status)
        self._flow = QStackedWidget()
        self._flow.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        root.addWidget(self._flow, 1)

        self._library_page = QWidget()
        library = QVBoxLayout(self._library_page)
        library.setContentsMargins(0, 0, 0, 0)
        library.setSpacing(12)
        toolbar = QHBoxLayout()
        self.new_button = self._button('Add Profile', primary=True)
        toolbar.addWidget(self.new_button)
        toolbar.addStretch()
        library.addLayout(toolbar)
        self.profile_list = QListWidget()
        self.profile_list.setAccessibleName('Game profiles. Select to edit; right-click to rename or delete.')
        self.profile_list.setMinimumWidth(0)
        self.profile_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.profile_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        set_theme_style(self.profile_list, lambda: f"""
            QListWidget {{ background: {Colors.SURFACE_1}; border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px; color: {Colors.TEXT};
                font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px; }}
            QListWidget::item {{ padding: 14px 16px; }}
            QListWidget::item:hover {{ background: {Colors.SURFACE_2}; }}
            QListWidget::item:selected {{ background: {Colors.ACCENT_SOFT}; color: {Colors.ACCENT}; }}
        """)
        library.addWidget(self.profile_list, 1)
        self._flow.addWidget(self._library_page)

        self._app_page = QWidget()
        app_layout = QVBoxLayout(self._app_page)
        app_layout.setContentsMargins(0, 0, 0, 0)
        app_layout.setSpacing(16)
        app_layout.addWidget(self._label('Choose an app', heading=True))
        self._source_tabs = QTabBar()
        self._style_tabs(self._source_tabs)
        self._source_tabs.addTab('Enter a path')
        self._source_tabs.addTab('Select a running app')
        self._source_tabs.setAccessibleName('How to choose the profile app')
        app_layout.addWidget(self._source_tabs)
        self._source_pages = QStackedWidget()
        manual = QWidget()
        manual_layout = QVBoxLayout(manual)
        manual_layout.setContentsMargins(0, 8, 0, 0)
        manual_layout.setSpacing(10)
        manual_label = self._label('Executable path')
        manual_layout.addWidget(manual_label)
        path_row = QHBoxLayout()
        self.manual_path_edit = QLineEdit()
        self.manual_path_edit.setAccessibleName('New profile executable path')
        self.manual_path_edit.setPlaceholderText(r'C:\Games\MyGame\Game.exe')
        self.manual_path_edit.setMinimumHeight(Sizes.INPUT_H)
        self.manual_path_edit.setMinimumWidth(0)
        manual_label.setBuddy(self.manual_path_edit)
        set_theme_style(self.manual_path_edit, self._line_edit_qss)
        path_row.addWidget(self.manual_path_edit, 1)
        self.browse_button = self._button('Browse…')
        path_row.addWidget(self.browse_button)
        manual_layout.addLayout(path_row)
        manual_layout.addWidget(self._label('Paste the full path to the app’s .exe file, or browse for it.'))
        manual_layout.addStretch()
        self._source_pages.addWidget(manual)
        running = QWidget()
        running_layout = QVBoxLayout(running)
        running_layout.setContentsMargins(0, 8, 0, 0)
        running_layout.setSpacing(10)
        running_layout.addWidget(self._label('Running app'))
        running_row = QHBoxLayout()
        self.running_windows_combo = self._make_combo()
        self.running_windows_combo.setAccessibleName('Running apps for game profiles')
        self.running_windows_combo.setPlaceholderText('No running apps found')
        self.running_windows_combo.setIconSize(QSize(20, 20))
        running_row.addWidget(self.running_windows_combo, 1)
        self.refresh_windows_button = self._button('Refresh')
        self.refresh_windows_button.setAccessibleName('Refresh running apps for game profiles')
        running_row.addWidget(self.refresh_windows_button)
        running_layout.addLayout(running_row)
        self._windows_status_label = self._label('Open your app, then refresh if it is not listed.')
        running_layout.addWidget(self._windows_status_label)
        running_layout.addStretch()
        self._source_pages.addWidget(running)
        app_layout.addWidget(self._source_pages, 1)
        picker_actions = QHBoxLayout()
        self.cancel_add_button = self._button('Discard')
        picker_actions.addWidget(self.cancel_add_button)
        picker_actions.addStretch()
        self.continue_button = self._button('Continue', primary=True)
        picker_actions.addWidget(self.continue_button)
        app_layout.addLayout(picker_actions)
        self._flow.addWidget(self._app_page)

        self._editor_page = QWidget()
        editor_layout = QVBoxLayout(self._editor_page)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(12)
        self._editor_title = self._label('Profile settings', heading=True)
        editor_layout.addWidget(self._editor_title)
        self._section_tabs = QTabBar()
        self._style_tabs(self._section_tabs)
        self._section_tabs.setAccessibleName('Profile settings category')
        for title in ('Clips', 'Recording', 'Audio', 'Camera'):
            self._section_tabs.addTab(title)
        editor_layout.addWidget(self._section_tabs)
        self._editor_scroll = QScrollArea()
        self._editor_scroll.setWidgetResizable(True)
        self._editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._editor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._editor_scroll.setAccessibleName('Game profile fields')
        self._editor_scroll.setObjectName('profileFieldsScroll')
        from ui.style import scrollbar_qss
        set_theme_style(self._editor_scroll, lambda: scrollbar_qss() + f"""
            QScrollArea#profileFieldsScroll {{ background: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER}; border-radius: {Sizes.RADIUS_SM}px; }}
            QScrollArea#profileFieldsScroll > QWidget > QWidget {{ background: {Colors.SURFACE_1}; }}
            QScrollBar:vertical {{ background: {Colors.SURFACE_1}; margin: 0; }}
        """)
        self._editor_content = QWidget()
        self._editor_content.setStyleSheet('background: transparent;')
        self._editor_content.setMinimumWidth(0)
        self._editor_content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._editor_layout = QVBoxLayout(self._editor_content)
        self._editor_layout.setContentsMargins(16, 16, 16, 16)
        self._editor_layout.setSpacing(16)
        self._editor_scroll.setWidget(self._editor_content)
        editor_layout.addWidget(self._editor_scroll, 1)
        # Capture state belongs to the editor, not the profile library.
        self._capture_label = self._label()
        self._capture_label.hide()
        editor_layout.addWidget(self._capture_label)
        actions = QHBoxLayout()
        self.discard_button = self._button('Discard')
        self.save_button = self._button('Save', primary=True)
        actions.addWidget(self.discard_button)
        actions.addStretch()
        actions.addWidget(self.save_button)
        editor_layout.addLayout(actions)
        self._flow.addWidget(self._editor_page)

        self._sections = []
        self.new_button.clicked.connect(self._begin_add_profile)
        self.profile_list.currentRowChanged.connect(self._on_row_changed)
        self.profile_list.itemClicked.connect(self._open_profile)
        self.profile_list.itemActivated.connect(self._open_profile)
        self.profile_list.customContextMenuRequested.connect(self._profile_context_menu)
        self._source_tabs.currentChanged.connect(self._source_changed)
        self.manual_path_edit.textChanged.connect(self._sync_actions)
        self.manual_path_edit.returnPressed.connect(self._continue_add_profile)
        self.browse_button.clicked.connect(self._browse_new_app)
        self.refresh_windows_button.clicked.connect(self.refresh_windows_requested.emit)
        self.running_windows_combo.currentIndexChanged.connect(self._sync_actions)
        self.continue_button.clicked.connect(self._continue_add_profile)
        self.cancel_add_button.clicked.connect(self._show_library)
        self._section_tabs.currentChanged.connect(self._show_section)
        self.discard_button.clicked.connect(self._discard_and_close)
        self.save_button.clicked.connect(self._save_and_close)
        self._flow.currentChanged.connect(self._resize_flow)
        self._sync_actions()

    def _style_tabs(self, tabs):
        tabs.setExpanding(False)
        tabs.setDrawBase(False)
        set_theme_style(tabs, lambda: f"""
            QTabBar::tab {{ color: {Colors.TEXT_DIM}; background: transparent;
                padding: 10px 16px; border-bottom: 2px solid transparent;
                font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px; }}
            QTabBar::tab:selected {{ color: {Colors.ACCENT}; border-bottom-color: {Colors.ACCENT}; }}
            QTabBar::tab:hover {{ background: {Colors.SURFACE_2}; }}
            QTabBar::tab:focus {{ background: {Colors.ACCENT_SOFT}; }}
        """)

    def _show_library(self):
        self._flow.setCurrentWidget(self._library_page)
        self._state_label.hide()
        self.reload_button.setVisible(self._future_catalog)
        self.new_button.setFocus()
        self._resize_flow()

    def _resize_flow(self, *_args):
        if not self._embedded:
            return
        if self._flow.currentWidget() is self._library_page:
            height = min(360, 120 + max(1, self.profile_list.count()) * 52)
        elif self._flow.currentWidget() is self._app_page:
            height = 360
        else:
            height = 680
        self.setFixedHeight(height)

    def _begin_add_profile(self):
        if self._catalog is None or self._future_catalog or not self._resolve_draft():
            return
        self._state_label.hide()
        self.manual_path_edit.clear()
        self._source_tabs.setCurrentIndex(0)
        self._flow.setCurrentWidget(self._app_page)
        self._sync_actions()
        self.manual_path_edit.setFocus()

    def _source_changed(self, index):
        self._source_pages.setCurrentIndex(index)
        self._state_label.hide()
        self._sync_actions()
        if index == 1 and sys.platform == 'win32':
            self.refresh_windows_requested.emit()
            self.running_windows_combo.setFocus()
        else:
            self.manual_path_edit.setFocus()

    def _browse_new_app(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Choose game executable', '', 'Windows executable (*.exe)')
        if path:
            self.manual_path_edit.setText(path)

    def _continue_add_profile(self):
        if self._catalog is None or self._future_catalog:
            return
        path = (self.manual_path_edit.text().strip() if self._source_tabs.currentIndex() == 0
                else self.running_windows_combo.currentData())
        try:
            canonical = canonical_exe_path(path)
        except (ProfileValidationError, ValueError, TypeError):
            self._show_error('Choose an app with a full path to its .exe file, for example C:\\Games\\Game.exe.')
            return
        existing = next((p for p in self._catalog.profiles if p.exe_path == canonical), None)
        if existing is not None:
            self._select_row_by_id(existing.id)
            self._load_selected_profile()
            self._open_profile()
            self._show_info('This app already has a profile. You can edit it here.')
            return
        self._new_profile(exe_path=path)
        self.refresh_devices_requested.emit()

    def _open_profile(self, *_args):
        if self._draft is None or self._future_catalog:
            return
        self._state_label.hide()
        self._flow.setCurrentWidget(self._editor_page)
        self._section_tabs.setCurrentIndex(0)
        self._show_section(0)
        self.refresh_devices_requested.emit()
        self._name_edit.setFocus()

    def _profile_context_menu(self, position):
        item = self.profile_list.itemAt(position)
        if item is None or self._future_catalog:
            return
        self.profile_list.setCurrentItem(item)
        if self.profile_list.currentItem() is not item:
            return
        menu = QMenu(self.profile_list)
        set_theme_style(menu, context_menu_qss)
        rename = menu.addAction('Rename')
        delete = menu.addAction('Delete')
        action = menu.exec(self.profile_list.viewport().mapToGlobal(position))
        menu.deleteLater()
        if action == rename:
            self._rename_profile()
            if self._dirty and self.save_draft():
                self._show_library()
        elif action == delete:
            self._delete_profile()

    def _show_section(self, index):
        for section_index, section in enumerate(self._sections):
            section.setVisible(section_index == index)
        self._editor_scroll.verticalScrollBar().setValue(0)

    def _save_and_close(self):
        if self.save_draft():
            self.apply_requested.emit()
            self._show_library()

    def _discard_and_close(self):
        if self._restore_canonical_draft():
            self._write_failed = False
            self._show_library()

    # ---------- catalog/draft lifecycle ----------
    def _load_catalog(self):
        self._refresh_global_cache()
        self._write_failed = False
        self._future_catalog = False
        self._catalog_error = ''
        try:
            self._catalog = self.settings_manager.read_game_capture_profiles()
        except (ProfileDataError, ValueError, TypeError, OSError) as exc:
            self._catalog = None
            self._future_catalog = True
            self._catalog_error = str(exc) or 'Profile catalog is unavailable.'
        self._draft = None
        self._canonical_draft = None
        self._draft_is_new = False
        self._dirty = False
        self._rebuild_rows()
        if self._future_catalog:
            self._state_label.setText(f'Profile catalog unavailable: {self._catalog_error}')
            self._state_label.setStyleSheet(label_body(Colors.ERROR, Fonts.SIZE_LABEL))
        else:
            self._state_label.clear()
        self._show_library()
        self._state_label.setVisible(self._future_catalog)
        self._sync_actions()

    def _reload_catalog(self):
        """Reload committed catalog state after an explicit user choice."""
        if not self._resolve_draft():
            return
        self._load_catalog()

    def _rebuild_rows(self, select_id: str | None = None):
        self._loading = True
        with QSignalBlocker(self.profile_list):
            self.profile_list.clear()
            if self._catalog is not None:
                for profile in self._catalog.profiles:
                    item = QListWidgetItem(profile.name)
                    item.setData(Qt.ItemDataRole.UserRole, profile.id)
                    item.setToolTip(_tooltip_literal(profile.exe_path))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                    self.profile_list.addItem(item)
                if select_id:
                    for row in range(self.profile_list.count()):
                        if self.profile_list.item(row).data(Qt.ItemDataRole.UserRole) == select_id:
                            self.profile_list.setCurrentRow(row)
                            break
                elif self.profile_list.count():
                    self.profile_list.setCurrentRow(0)
        self._loading = False
        if self._catalog is None or not self.profile_list.count():
            self._draft = None
            self._flow.setCurrentWidget(self._library_page)
        elif self._draft is None:
            self._load_selected_profile()
        self._sync_actions()

    def _selected_id(self) -> str | None:
        item = self.profile_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _profile_by_id(self, profile_id: str | None):
        if self._catalog is None or not profile_id:
            return None
        return next((profile for profile in self._catalog.profiles if profile.id == profile_id), None)

    def _load_selected_profile(self):
        profile = self._profile_by_id(self._selected_id())
        if profile is None:
            self._draft = None
            self._canonical_draft = None
            self._flow.setCurrentWidget(self._library_page)
            self._sync_actions()
            return
        self._canonical_draft = profile.to_dict()
        self._draft = copy.deepcopy(self._canonical_draft)
        self._draft_is_new = False
        self._dirty = False
        self._build_editor()
        self._sync_actions()

    def _mark_dirty(self, *_args):
        if self._loading or self._draft is None:
            return
        self._dirty = True
        self._sync_actions()

    def _draft_prompt(self) -> int:
        return _DraftDecisionDialog(self).exec()

    def _restore_canonical_draft(self) -> bool:
        """Restore controls and draft data from the last committed profile."""
        if self._draft_is_new:
            self._draft = None
            self._canonical_draft = None
            self._draft_is_new = False
            self._dirty = False
            self._flow.setCurrentWidget(self._library_page)
            self._sync_actions()
            return True
        profile_id = (self._canonical_draft or self._draft or {}).get('id')
        profile = self._profile_by_id(profile_id)
        if profile is None:
            return False
        self._canonical_draft = profile.to_dict()
        self._draft = copy.deepcopy(self._canonical_draft)
        self._dirty = False
        self._build_editor()
        self._flow.setCurrentWidget(self._editor_page)
        self._sync_actions()
        return True

    def _select_row_by_id(self, profile_id: str | None) -> None:
        with QSignalBlocker(self.profile_list):
            for row in range(self.profile_list.count()):
                if (profile_id and
                        self.profile_list.item(row).data(Qt.ItemDataRole.UserRole) == profile_id):
                    self.profile_list.setCurrentRow(row)
                    return
            self.profile_list.setCurrentRow(-1)

    def _resolve_draft(self, target_id: str | None = None) -> bool:
        if not self._dirty:
            return True
        choice = self._draft_prompt()
        if choice == 1:
            return self.save_draft(select_id=target_id)
        if choice == 2:
            return self._restore_canonical_draft()
        return False

    def _on_row_changed(self, row: int):
        if self._loading:
            return
        target_item = self.profile_list.item(row) if row >= 0 else None
        target_id = (str(target_item.data(Qt.ItemDataRole.UserRole))
                     if target_item is not None else None)
        current_id = (self._draft or {}).get('id')
        if self._dirty and not self._resolve_draft(target_id):
            self._select_row_by_id(current_id)
            return
        self._load_selected_profile()

    def set_available_windows(self, records):
        """Display the same detached window identities as Allowed games."""
        selected = self.running_windows_combo.currentData()
        with QSignalBlocker(self.running_windows_combo):
            self.running_windows_combo.clear()
            for record in records or ():
                record = _detached_window_record(record)
                if record is None:
                    continue
                self.running_windows_combo.addItem(
                    record.get('icon', QIcon()), record['display_name'], record['exe_path'])
                self.running_windows_combo.setItemData(
                    self.running_windows_combo.count() - 1,
                    _tooltip_literal(record['exe_path']), Qt.ItemDataRole.ToolTipRole)
            index = self.running_windows_combo.findData(selected) if selected else -1
            self.running_windows_combo.setCurrentIndex(
                index if index >= 0 else (0 if self.running_windows_combo.count() else -1))
        self.running_windows_combo.setPlaceholderText('No running apps found')
        self._sync_actions()

    def set_windows_status(self, message=''):
        self._windows_status_label.setText(str(message or (
            'Open your app, then refresh if it is not listed.' if sys.platform == 'win32'
            else 'Running apps are available on Windows. Enter a path instead.')))

    def _new_running_profile(self):
        path = self.running_windows_combo.currentData()
        if path:
            self._new_profile(exe_path=path)

    def _new_profile(self, *, exe_path=''):
        if self._catalog is None or self._future_catalog or not self._resolve_draft():
            return
        profile_id = str(uuid.uuid4())
        self._draft = {
            'id': profile_id, 'revision': 1,
            'name': ntpath.splitext(ntpath.basename(exe_path))[0] if exe_path else 'New profile',
            'exe_path': exe_path, 'overrides': {},
        }
        self._canonical_draft = None
        self._draft_is_new = True
        self._dirty = True
        with QSignalBlocker(self.profile_list):
            self.profile_list.setCurrentRow(-1)
            self.profile_list.clearSelection()
        self._build_editor()
        self._flow.setCurrentWidget(self._editor_page)
        self._sync_actions()
        self._section_tabs.setCurrentIndex(0)
        self._show_section(0)
        self._state_label.hide()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _rename_profile(self):
        if self._draft is None or self._future_catalog:
            return
        value, accepted = FthrInputDialog.get_text(
            self, 'Rename profile', 'Profile name', text=str(self._draft.get('name', '')))
        if accepted and value.strip():
            self._draft['name'] = value.strip()
            self._name_edit.setText(value.strip())
            self._mark_dirty()

    def _delete_profile(self):
        if self._catalog is None or self._future_catalog:
            return
        if self._dirty and not self._resolve_draft():
            return
        profile_id = self._selected_id()
        profile = self._profile_by_id(profile_id)
        if profile is None:
            return
        if not FthrMessageDialog.question(
                self, 'Delete profile?', f'Delete “{profile.name}”?',
                plain_text=True):
            return
        try:
            candidate = delete_profile(self._catalog, profile.id)
            if not self.settings_manager.save_game_capture_profiles(
                    candidate, expected_revision=self._catalog.revision):
                self._show_error(
                    'Profile could not be deleted. The catalog may have changed '
                    'or the disk write failed. Your profile list is unchanged.')
                return
        except (ProfileDataError, ProfileValidationError, OSError, ValueError, TypeError) as exc:
            self._show_error(f'Profile could not be deleted: {exc}')
            return
        self._catalog = candidate
        self._draft = None
        self._dirty = False
        self.catalog_changed.emit(candidate.revision)
        self._rebuild_rows()
        self.apply_requested.emit()

    def save_draft(self, *, select_id: str | None = None) -> bool:
        """Validate and atomically persist the current draft."""
        if self._catalog is None or self._draft is None or self._future_catalog:
            return False
        try:
            profile_data = self._collect_draft()
            if self._draft_is_new:
                candidate = create_profile(
                    self._catalog, profile_data['name'], profile_data['exe_path'],
                    profile_data['overrides'], profile_id=profile_data['id'])
            else:
                candidate = update_profile(
                    self._catalog, profile_data['id'], name=profile_data['name'],
                    exe_path=profile_data['exe_path'], overrides=profile_data['overrides'])
            if not self.settings_manager.save_game_capture_profiles(
                    candidate, expected_revision=self._catalog.revision):
                self._show_error(
                    'Profile could not be saved. The catalog may have changed '
                    'or the disk write failed. Draft kept for review.')
                return False
        except (ProfileDataError, ProfileValidationError, OSError, ValueError, TypeError) as exc:
            self._show_error(f'Profile was not saved: {exc}')
            return False
        self._catalog = candidate
        saved_id = profile_data['id']
        self._write_failed = False
        self._draft_is_new = False
        self._dirty = False
        self._canonical_draft = next(
            profile.to_dict() for profile in candidate.profiles
            if profile.id == saved_id)
        self._draft = copy.deepcopy(self._canonical_draft)
        self.catalog_changed.emit(candidate.revision)
        self._rebuild_rows(select_id or saved_id)
        self._load_selected_profile()
        self._show_info('Profile saved.')
        return True

    def _collect_draft(self) -> dict[str, Any]:
        if self._draft is None:
            raise ProfileValidationError('no profile draft selected')
        overrides: dict[str, dict[str, Any]] = {}
        for binding in self._bindings.values():
            if binding.toggle.isChecked():
                overrides.setdefault(binding.group, {})[binding.key] = binding.read()
        return {
            'id': str(self._draft.get('id', '')),
            'name': self._name_edit.text().strip(),
            'exe_path': self._exe_edit.text().strip(),
            'overrides': overrides,
        }

    # ---------- leaf editor ----------
    def _build_editor(self):
        self._loading = True
        self._bindings.clear()
        self._binding_rows.clear()
        self._sections.clear()
        while self._editor_layout.count():
            item = self._editor_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

        if self._draft is None:
            self._loading = False
            return
        self._name_edit = QLineEdit(str(self._draft.get('name', '')))
        self._name_edit.setAccessibleName('Profile name')
        self._name_edit.setMinimumWidth(0)
        self._name_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        set_theme_style(self._name_edit, self._line_edit_qss)
        self._name_edit.textChanged.connect(self._mark_dirty)
        self._exe_edit = QLineEdit(str(self._draft.get('exe_path', '')))
        self._exe_edit.setAccessibleName('Executable path')
        self._exe_edit.setToolTip(_tooltip_literal(self._draft.get('exe_path', '')))
        self._exe_edit.setMinimumWidth(0)
        self._exe_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        set_theme_style(self._exe_edit, self._line_edit_qss)
        self._exe_edit.textChanged.connect(self._mark_dirty)
        identity = _ResponsiveIdentity(self._name_edit, self._exe_edit)
        for label in identity._labels:
            set_theme_style(label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        self._editor_layout.addWidget(identity)
        hint = QLabel('Settings follow your defaults until you turn on Customize.')
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self._editor_layout.addWidget(hint)

        override_data = self._draft.get('overrides', {})
        for group in ('clips', 'recording', 'audio', 'camera'):
            section = QFrame()
            section.setObjectName('profileSection')
            set_theme_style(section, lambda: 'QFrame#profileSection { background: transparent; border: none; }')
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 16, 0, 8)
            section_layout.setSpacing(12)
            heading = QLabel(_GROUP_TITLES[group])
            set_theme_style(heading, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY_L))
            section_layout.addWidget(heading)
            for leaf_group, key, label, kind in _LEAVES:
                if leaf_group != group:
                    continue
                binding = self._make_binding(group, key, label, kind, override_data.get(group, {}).get(key, _MISSING))
                self._bindings[key] = binding
                row = self._binding_row(label, binding)
                self._binding_rows[key] = row
                section_layout.addWidget(row)
            self._editor_layout.addWidget(section)
            self._sections.append(section)
        self._editor_layout.addStretch(1)
        self._editor_title.setText('New profile' if self._draft_is_new else str(self._draft.get('name', 'Profile settings')))
        self._show_section(self._section_tabs.currentIndex())
        self._refresh_inherited_labels()
        self._loading = False

    def _binding_row(self, label: str, binding: _Binding) -> QWidget:
        title = QLabel(label)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setToolTip(label)
        set_theme_style(title, lambda: label_body(Colors.TEXT, Fonts.SIZE_LABEL))
        binding.global_value.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(binding.global_value, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_MICRO))
        return _ResponsiveBindingRow(
            title, binding.toggle, binding.editor, binding.global_value)

    def _make_binding(self, group: str, key: str, label: str, kind: str, value: Any) -> _Binding:
        toggle = QCheckBox('Customize')
        toggle.setAccessibleName(f'Override {label}')
        set_theme_style(toggle, checkbox_qss)
        has_override = value is not _MISSING
        toggle.setChecked(has_override)

        if kind == 'bool':
            editor = QCheckBox('Enabled')
            editor.setAccessibleName(label)
            set_theme_style(editor, checkbox_qss)
            editor.setChecked(bool(value if has_override else self._global_value(key)))
            read = lambda editor=editor: bool(editor.isChecked())
            write = lambda item, editor=editor: editor.setChecked(bool(item))
        elif kind in ('resolution', 'quality', 'audio_mode', 'source_mode', 'tri_bool'):
            editor = self._make_combo()
            editor.setAccessibleName(label)
            for display, data in _ENUMS[kind]:
                editor.addItem(display, data)
            write = lambda item, editor=editor: self._set_combo_value(editor, item)
            read = lambda editor=editor: editor.currentData()
            write(value if has_override else self._global_value(key))
        elif kind == 'microphone':
            editor = self._make_combo()
            editor.setAccessibleName(label)
            self._fill_endpoint_combo(editor, self._microphones, value if has_override else self._global_value(key))
            read = lambda editor=editor: editor.currentData()
            write = lambda item, editor=editor: self._fill_endpoint_combo(editor, self._microphones, item)
        elif kind == 'playback':
            editor = self._make_combo()
            editor.setAccessibleName(label)
            self._fill_endpoint_combo(editor, self._playback, value if has_override else self._global_value(key))
            read = lambda editor=editor: editor.currentData()
            write = lambda item, editor=editor: self._fill_endpoint_combo(editor, self._playback, item)
        elif kind == 'camera':
            editor = self._make_combo()
            editor.setAccessibleName(label)
            self._fill_camera_combo(editor, value if has_override else self._global_value(key))
            read = lambda editor=editor: editor.currentData()
            write = lambda item, editor=editor: self._fill_camera_combo(editor, item)
        elif kind == 'tracks':
            editor = _TrackRows(self)
            editor.setAccessibleName(label)
            editor.set_rows(value if has_override else self._global_value(key))
            read = editor.value
            write = editor.set_rows
        elif kind == 'rect':
            editor = self._make_rect_editor()
            read = lambda editor=editor: self._rect_value(editor)
            write = lambda item, editor=editor: self._set_rect(editor, item)
            write(value if has_override else self._global_value(key))
        else:
            editor = NumberSpinBox()
            editor.setAccessibleName(label)
            if kind == 'fps':
                editor.setRange(15, 240)
                editor.setSuffix(' FPS')
            elif kind == 'bitrate':
                editor.setRange(500, 200_000)
                editor.setSingleStep(500)
                editor.setSuffix(' kbps')
            elif kind == 'volume':
                editor.setRange(0, 200)
                editor.setSuffix('%')
            else:
                editor.setRange(5, 900)
                editor.setSuffix(' s')
            editor.setMinimumWidth(0)
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            editor.setValue(int(value if has_override else self._global_value(key)))
            read = lambda editor=editor: int(editor.value())
            write = lambda item, editor=editor: editor.setValue(int(item))

        binding = _Binding(group, key, kind, toggle, editor, QLabel(), read, write)
        toggle.toggled.connect(lambda checked, binding=binding: self._toggle_binding(binding, checked))
        if hasattr(editor, 'valueChanged'):
            editor.valueChanged.connect(self._mark_dirty)
        if hasattr(editor, 'toggled'):
            editor.toggled.connect(self._mark_dirty)
        if hasattr(editor, 'currentIndexChanged'):
            editor.currentIndexChanged.connect(self._mark_dirty)
            if kind == 'source_mode':
                editor.currentIndexChanged.connect(
                    lambda _index, editor=editor: self._sync_source_capture_mode(editor.currentData()))
        if isinstance(editor, _TrackRows):
            editor.changed.connect(self._mark_dirty)
        if kind == 'rect':
            for spin in editor.findChildren(QDoubleSpinBox):
                spin.valueChanged.connect(self._mark_dirty)
        self._toggle_binding(binding, has_override)
        return binding

    def _sync_source_capture_mode(self, source_mode):
        if source_mode not in ('combined', 'device', 'programs', 'devices'):
            return
        binding = self._bindings.get('audio_capture_mode')
        if binding is not None:
            binding.toggle.setChecked(True)
            binding.write('combined' if source_mode == 'combined' else 'separated')

    def _toggle_binding(self, binding: _Binding, checked: bool):
        binding.editor.setEnabled(bool(checked))
        self._refresh_inherited_labels()
        self._mark_dirty()

    def _read_global_key(self, key: str):
        getter = getattr(self.settings_manager, 'get', None)
        if callable(getter):
            return getter(key, _GLOBAL_DEFAULTS.get(key))
        settings = getattr(self.settings_manager, 'settings', {})
        return settings.get(key, _GLOBAL_DEFAULTS.get(key))

    def _refresh_global_cache(self):
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for group, key, _label, _kind in _LEAVES:
            try:
                raw = self._read_global_key(key)
                values[key] = _validate_leaf(group, key, raw)
            except (OSError, TypeError, ValueError) as exc:
                values[key] = copy.deepcopy(_GLOBAL_DEFAULTS[key])
                errors[key] = str(exc) or 'invalid global value'
        self._global_values = values
        self._global_errors = errors

    def _global_value(self, key: str):
        return copy.deepcopy(self._global_values.get(key, _GLOBAL_DEFAULTS.get(key)))

    def _refresh_inherited_labels(self):
        if not self._bindings:
            return
        for binding in self._bindings.values():
            global_value = self._global_value(binding.key)
            global_text = self._format_value(binding.key, global_value)
            if binding.key in self._global_errors:
                global_text = f'Unavailable (invalid global: {self._global_errors[binding.key]})'
            binding.global_value.setText(
                f'Default: {global_text}')
            if not binding.toggle.isChecked():
                binding.editor.setEnabled(False)

    def _format_value(self, key: str, value: Any) -> str:
        if key in ('mic_device_id', 'playback_device_id'):
            if value is None:
                return 'System default'
            if value == '':
                return 'System default'
            for record in self._microphones + self._playback:
                if record.get('endpoint_id') == value:
                    return record.get('display_name', str(value))
            return 'Saved device (Unavailable)'
        if key == 'camera_device_index':
            for record in self._cameras:
                if record.get('index') == value:
                    return record.get('display_name', f'Camera {value}')
            return 'Saved camera (Unavailable)'
        if key == 'audio_device_tracks':
            records = value if isinstance(value, (list, tuple)) else []
            return ', '.join(str(item.get('name', 'Audio device')) for item in records) or 'None'
        if key == 'camera_overlay_rect' and isinstance(value, dict):
            return 'x={x:g}, y={y:g}, w={w:g}, h={h:g}'.format(**value)
        if value is None:
            return 'Automatic'
        if value is False:
            return 'Disabled'
        if value is True:
            return 'Enabled'
        return str(value)

    def _make_rect_editor(self) -> QWidget:
        return _ResponsiveRectEditor()

    def _rect_value(self, widget: QWidget) -> dict[str, float]:
        return {name: float(widget.findChild(QDoubleSpinBox, f'cameraRect{name.upper()}').value()) for name in ('x', 'y', 'w', 'h')}

    def _set_rect(self, widget: QWidget, value: Any):
        value = value if isinstance(value, dict) else _GLOBAL_DEFAULTS['camera_overlay_rect']
        for name in ('x', 'y', 'w', 'h'):
            spin = widget.findChild(QDoubleSpinBox, f'cameraRect{name.upper()}')
            with QSignalBlocker(spin):
                try:
                    spin.setValue(float(value.get(name, 0.0)))
                except (TypeError, ValueError):
                    spin.setValue(float(_GLOBAL_DEFAULTS['camera_overlay_rect'][name]))

    def _set_combo_value(self, combo: QComboBox, value: Any):
        index = combo.findData(value)
        if index < 0:
            index = 0
        with QSignalBlocker(combo):
            combo.setCurrentIndex(index)

    def _fill_endpoint_combo(self, combo: QComboBox, records: list[dict[str, Any]], selected=None, *, allow_default=True):
        with QSignalBlocker(combo):
            combo.clear()
            if allow_default:
                combo.addItem('System default', selected if selected in (None, '') else None)
            for record in records:
                endpoint = record.get('endpoint_id')
                if endpoint is None:
                    continue
                label = str(record.get('display_name', endpoint))
                if record.get('is_default'):
                    label += ' · Default'
                if not record.get('available', True):
                    label += ' (Unavailable)'
                combo.addItem(label, endpoint)
                combo.setItemData(
                    combo.count() - 1, _tooltip_literal(label),
                    Qt.ItemDataRole.ToolTipRole)
            if selected in (None, '') and allow_default:
                combo.setCurrentIndex(0)
                combo.setToolTip(_tooltip_literal(combo.currentText()))
                return
            found = combo.findData(selected)
            if found < 0 and selected:
                combo.addItem('Saved device (Unavailable)', selected)
                found = combo.count() - 1
                combo.setItemData(
                    found, _tooltip_literal(combo.itemText(found)),
                    Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(found)
            combo.setToolTip(_tooltip_literal(combo.currentText()))

    def _fill_camera_combo(self, combo: QComboBox, selected=0):
        with QSignalBlocker(combo):
            combo.clear()
            for record in self._cameras:
                index = record.get('index')
                label = str(record.get('display_name', f'Camera {index}'))
                if record.get('is_default'):
                    label += ' · Default'
                if not record.get('available', True):
                    label += ' (Unavailable)'
                combo.addItem(label, index)
                combo.setItemData(
                    combo.count() - 1, _tooltip_literal(label),
                    Qt.ItemDataRole.ToolTipRole)
            found = combo.findData(selected)
            if found < 0:
                combo.addItem('Saved camera (Unavailable)', selected)
                found = combo.count() - 1
                combo.setItemData(
                    found, _tooltip_literal(combo.itemText(found)),
                    Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(found)
            combo.setToolTip(_tooltip_literal(combo.currentText()))

    # ---------- passive adapters/status ----------
    def set_capture_status(self, active, pending, message):
        """Render detached accepted/pending state supplied by MainWindow."""
        self._capture_state = (active, pending, str(message or ''))
        parts = []
        if active is None:
            parts.append('Active capture: No active capture')
        elif active is not None:
            parts.append(f'Active profile: {self._status_value(active)}')
        if pending:
            parts.append(f'Pending: {self._status_value(pending)}')
        if message:
            parts.append(str(message))
        self._capture_label.setText(' · '.join(parts))
        self._capture_label.setVisible(bool(pending or message))

    def _status_value(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get('name') or value.get('exe_path') or 'profile')
        return str(value)

    def set_audio_devices(self, microphones, playback):
        self._microphones = [item for item in (_friendly_record(record) for record in (microphones or [])) if item]
        self._playback = [item for item in (_friendly_record(record) for record in (playback or [])) if item]
        self._refresh_inventory_controls()

    def set_camera_devices(self, records):
        self._cameras = [item for item in (_friendly_record(record, camera=True) for record in (records or [])) if item]
        self._refresh_inventory_controls()

    def refresh_global_values(self):
        """Refresh inherited editors from current global keys only.

        An override is intentionally left untouched, including an unsaved
        draft value.  Invalid inherited data is shown in the row and does not
        prevent a valid sparse override from being saved.
        """
        if self._draft is None:
            return
        was_loading = self._loading
        dirty_before = self._dirty
        self._loading = True
        try:
            self._refresh_global_cache()
            for binding in self._bindings.values():
                if not binding.toggle.isChecked():
                    with QSignalBlocker(binding.editor):
                        binding.write(self._global_value(binding.key))
            self._refresh_inherited_labels()
        finally:
            self._dirty = dirty_before
            self._loading = was_loading

    def showEvent(self, event):  # noqa: N802 - Qt API name
        self.refresh_global_values()
        super().showEvent(event)

    def _refresh_inventory_controls(self):
        if self._draft is None:
            return
        was_loading = self._loading
        dirty_before = self._dirty
        self._loading = True
        try:
            for key in ('mic_device_id', 'playback_device_id', 'audio_device_tracks',
                        'camera_device_index'):
                binding = self._bindings.get(key)
                if binding is None:
                    continue
                value = binding.read()
                with QSignalBlocker(binding.editor):
                    binding.write(value)
            self._refresh_inherited_labels()
        finally:
            self._dirty = dirty_before
            self._loading = was_loading

    # ---------- public/testing convenience ----------
    def refresh(self):
        """Retry catalog loading while retaining an unavailable raw document."""
        if not self._resolve_draft():
            return
        self._load_catalog()

    def _show_error(self, message: str):
        self._state_label.setText(str(message))
        self._state_label.show()
        self._state_label.setStyleSheet(label_body(Colors.ERROR, Fonts.SIZE_LABEL))
        if self._flow.currentWidget() is not self._app_page:
            self._write_failed = True
            self.reload_button.show()

    def _show_info(self, message: str):
        self._state_label.setText(str(message))
        self._state_label.show()
        self._state_label.setStyleSheet(label_body(Colors.SUCCESS, Fonts.SIZE_LABEL))

    def _sync_actions(self):
        ready = self._catalog is not None and not self._future_catalog
        editable = self._draft is not None and ready
        self.new_button.setEnabled(ready)
        windows_available = sys.platform == 'win32'
        self.running_windows_combo.setEnabled(windows_available and ready)
        self.refresh_windows_button.setEnabled(windows_available)
        self.running_windows_combo.setToolTip(_tooltip_literal(
            self.running_windows_combo.currentData() or
            ('Refresh to find running apps' if windows_available else 'Available on Windows')))
        selected = (bool(self.manual_path_edit.text().strip())
                    if self._source_tabs.currentIndex() == 0 else
                    windows_available and bool(self.running_windows_combo.currentData()))
        self.continue_button.setEnabled(ready and selected)
        self.save_button.setEnabled(editable)
        self.discard_button.setEnabled(editable)
        self.reload_button.setEnabled(True)
        self.reload_button.setVisible(self._future_catalog or self._write_failed)


_MISSING = object()


__all__ = ['GameProfilesWidget']
