"""Passive editor for the capture admission allow-list.

The widget owns presentation and user intent only.  It does not discover
windows, inspect processes, or start capture.  The host publishes detached
window records through :meth:`set_available_windows` and listens for the
refresh signal before doing its own discovery.
"""

from __future__ import annotations

import os
import ntpath
import sys
from collections.abc import Mapping
from html import escape as _html_escape
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal, QSize, QFileInfo
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileIconProvider
from ui.toggle_switch import ToggleSwitch
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.capture_admission import (
    AdmissionDataError,
    AdmissionValidationError,
    add_executable,
    remove_executable,
    with_enabled,
)
from ui.style import (
    Colors,
    Fonts,
    Sizes,
    button_outline_qss,
    button_primary_qss,
    combo_qss,
    label_body,
    label_display,
    label_uppercase,
    set_theme_style,
    WheelSafeComboBox,
)


def _tooltip_literal(value: Any) -> str:
    """Keep dynamic paths literal when Qt treats tooltips as rich text."""
    text = str(value or '')
    escaped = _html_escape(text, quote=True).replace('\r\n', '<br>')
    escaped = escaped.replace('\r', '<br>').replace('\n', '<br>')
    return f'<qt>{escaped}</qt>'


def _elide_path(path: str, width: int = 64) -> str:
    """Show a useful path fragment while retaining the full path in a tooltip."""
    value = str(path or '')
    if len(value) <= width:
        return value
    tail = max(24, width - 14)
    return f'{value[:9]}…{value[-tail:]}'


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _detached_window_record(record: Any) -> dict[str, Any] | None:
    """Copy only the plain identity fields used by this presentation widget."""
    raw_path = _record_value(record, 'exe_path', '')
    path = str(raw_path or '').strip()
    if not path:
        return None
    display = _record_value(record, 'display_name', None)
    if not display:
        display = _record_value(record, 'title', None)
    if not display:
        display = os.path.basename(path) or path
    result = {'exe_path': path, 'display_name': str(display)}
    icon = _record_value(record, 'icon')
    if isinstance(icon, QIcon) and not icon.isNull():
        result['icon'] = QIcon(icon)
    return result


class CaptureAdmissionWidget(QWidget):
    """Allow-list editor backed by SettingsManager's revisioned CAS API."""

    policy_changed = Signal(object)
    refresh_windows_requested = Signal()

    def __init__(self, settings_manager, parent=None, *, windows_available=None, embedded=False):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._embedded = embedded
        self._app_icons = {}
        self._windows_available = (
            sys.platform == 'win32'
            if windows_available is None else bool(windows_available)
        )
        self._policy = None
        self._available_windows: list[dict[str, Any]] = []
        self._unavailable = False
        self._unavailable_raw = None
        self._unavailable_reason = ''
        self._capture_status_message = ''
        self.setObjectName('captureAdmissionWidget')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._build_ui()
        self.reload_policy()

    # ---------- construction and styling ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_4)
        root.setSpacing(Sizes.SPACE_3)

        header = QHBoxLayout()
        title = QLabel('Allowed games')
        title.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(title, lambda: label_display(Colors.TEXT, Fonts.SIZE_H2))
        if not self._embedded:
            header.addWidget(title)
        header.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self._status_label = self.status_label
        set_theme_style(self.status_label,
                        lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        header.addWidget(self.status_label, 1)
        root.addLayout(header)

        self.enabled_checkbox = ToggleSwitch()
        toggle_row = QHBoxLayout()
        toggle_label = QLabel('Only allowed games')
        toggle_label.setBuddy(self.enabled_checkbox)
        set_theme_style(toggle_label, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY))
        toggle_row.addWidget(toggle_label)
        toggle_row.addStretch()
        toggle_row.addWidget(self.enabled_checkbox)
        self.enabled_checkbox.setAccessibleName('Only allowed games')
        self.enabled_checkbox.setToolTip(_tooltip_literal(
            'Allow capture only when the active executable is on this list.'))
        root.addLayout(toggle_row)

        self.description_label = QLabel(
            'Record only apps on this list. Capture pauses when another app is active.')
        self.description_label.setTextFormat(Qt.TextFormat.PlainText)
        self.description_label.setWordWrap(True)
        set_theme_style(self.description_label,
                        lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        root.addWidget(self.description_label)

        self._platform_label = QLabel()
        self._platform_label.setTextFormat(Qt.TextFormat.PlainText)
        self._platform_label.setWordWrap(True)
        set_theme_style(self._platform_label,
                        lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        root.addWidget(self._platform_label)

        files_frame = QFrame()
        files_frame.setObjectName('captureAdmissionFiles')
        set_theme_style(files_frame, lambda: '''
            QFrame#captureAdmissionFiles {{
                background: transparent;
                border: none;
            }}
        ''')
        files_layout = QVBoxLayout(files_frame)
        files_layout.setContentsMargins(0, 8, 0, 0)
        files_layout.setSpacing(Sizes.SPACE_3)
        files_heading = QLabel('Allowed apps')
        files_heading.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(files_heading,
                        lambda: label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        files_layout.addWidget(files_heading)

        self.executable_list = QListWidget()
        self.executable_list.setIconSize(QSize(24, 24))
        self.executable_list.setAccessibleName('Approved executables')
        self.executable_list.setMinimumHeight(96)
        self.executable_list.setMaximumHeight(192)
        self.executable_list.setMinimumWidth(0)
        self.executable_list.setSizePolicy(QSizePolicy.Policy.Expanding,
                                           QSizePolicy.Policy.Expanding)
        self.executable_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.executable_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.executable_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection)
        set_theme_style(self.executable_list, lambda: f'''
            QListWidget {{
                background: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                outline: none;
            }}
            QListWidget::item {{ padding: 8px; border-radius: {Sizes.RADIUS_SM}px; }}
            QListWidget::item:selected {{
                background: {Colors.ACCENT_SOFT}; color: {Colors.ACCENT};
            }}
        ''')
        files_layout.addWidget(self.executable_list, 1)

        self.empty_label = QLabel('No apps added yet. Choose a running app below or browse for one.')
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setWordWrap(True)
        set_theme_style(self.empty_label,
                        lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        self._empty_label = self.empty_label
        files_layout.addWidget(self.empty_label)

        file_actions = QHBoxLayout()
        file_actions.setSpacing(Sizes.SPACE_3)
        self.add_file_button = QPushButton('Browse for app')
        self.add_file_button.setAccessibleName('Add executable')
        self.add_file_button.setToolTip(_tooltip_literal(
            'Choose the exact executable file to approve.'))
        self.remove_button = QPushButton('Remove selected')
        self.remove_button.setAccessibleName('Remove selected executable')
        for button in (self.add_file_button, self.remove_button):
            button.setMinimumHeight(Sizes.BUTTON_H)
            set_theme_style(button, button_outline_qss)
        file_actions.addWidget(self.add_file_button)
        file_actions.addWidget(self.remove_button)
        file_actions.addStretch(1)
        files_layout.addLayout(file_actions)
        root.addWidget(files_frame, 1)

        windows_frame = QFrame()
        windows_frame.setObjectName('captureAdmissionWindows')
        set_theme_style(windows_frame, lambda: '''
            QFrame#captureAdmissionWindows {{
                background: transparent;
                border: none;
            }}
        ''')
        windows_layout = QVBoxLayout(windows_frame)
        windows_layout.setContentsMargins(0, 8, 0, 0)
        windows_layout.setSpacing(Sizes.SPACE_3)
        windows_heading = QLabel('Add a running app')
        windows_heading.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(windows_heading,
                        lambda: label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        windows_layout.addWidget(windows_heading)

        window_tools = QHBoxLayout()
        window_tools.setSpacing(Sizes.SPACE_3)
        self.running_windows_combo = WheelSafeComboBox()
        self.running_windows_combo.setIconSize(QSize(20, 20))
        self.running_windows_combo.setAccessibleName('Running windows')
        self.running_windows_combo.setMinimumWidth(0)
        self.running_windows_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                                 QSizePolicy.Policy.Fixed)
        self.running_windows_combo.setMinimumHeight(Sizes.INPUT_H)
        self.running_windows_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.running_windows_combo.setMinimumContentsLength(0)
        self.running_windows_combo.setToolTip(_tooltip_literal(
            'Refresh windows to choose an exact executable identity.'))
        set_theme_style(self.running_windows_combo,
                        lambda: combo_qss(Colors.SURFACE_2))
        self.refresh_windows_button = QPushButton('Refresh')
        self.refresh_windows_button.setAccessibleName('Refresh running windows')
        self.refresh_windows_button.setMinimumHeight(Sizes.BUTTON_H)
        set_theme_style(self.refresh_windows_button, button_outline_qss)
        self.add_window_button = QPushButton('Add selected')
        self.add_window_button.setAccessibleName('Add selected running window')
        self.add_window_button.setMinimumHeight(Sizes.BUTTON_H)
        set_theme_style(self.add_window_button, button_primary_qss)
        window_tools.addWidget(self.running_windows_combo, 1)
        window_tools.addWidget(self.refresh_windows_button)
        window_tools.addWidget(self.add_window_button)
        windows_layout.addLayout(window_tools)
        root.addWidget(windows_frame)

        footer = QHBoxLayout()
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self._error_label = self.error_label
        set_theme_style(self.error_label,
                        lambda: label_body(Colors.ERROR, Fonts.SIZE_LABEL))
        footer.addWidget(self.error_label, 1)
        self.reload_button = QPushButton('Reload')
        self.reload_button.setAccessibleName('Reload capture admission policy')
        self.reload_button.setMinimumHeight(Sizes.BUTTON_H)
        self.reload_button.setToolTip(_tooltip_literal(
            'Read the policy from settings again.'))
        set_theme_style(self.reload_button, button_outline_qss)
        footer.addStretch()
        footer.addWidget(self.reload_button)
        root.addLayout(footer)
        root.addStretch(1)

        self.enabled_checkbox.stateChanged.connect(self._enabled_changed)
        self.add_file_button.clicked.connect(self._choose_executable)
        self.remove_button.clicked.connect(self._remove_selected)
        self.refresh_windows_button.clicked.connect(self._request_windows_refresh)
        self.add_window_button.clicked.connect(self._add_selected_window)
        self.reload_button.clicked.connect(self.reload_policy)
        self.executable_list.currentRowChanged.connect(
            lambda _row: self._sync_controls())
        self.running_windows_combo.currentIndexChanged.connect(
            lambda _index: self._sync_controls())

    # ---------- public passive API ----------
    def reload_policy(self) -> None:
        """Reload the persisted policy without resetting invalid raw data."""
        self._clear_error()
        try:
            policy = self.settings_manager.read_capture_admission_policy()
        except (AdmissionDataError, AdmissionValidationError, OSError,
                ValueError, TypeError, AttributeError) as exc:
            self._unavailable_raw = getattr(exc, 'raw',
                                            getattr(exc, 'raw_policy', None))
            self._policy = None
            self._unavailable = True
            self._unavailable_reason = str(exc).strip() or (
                'The saved policy could not be read.')
            self._render_unavailable(exc)
            return
        if policy is None:
            self._policy = None
            self._unavailable = True
            self._unavailable_reason = (
                'The saved capture admission policy is unavailable.')
            self._render_unavailable(
                ValueError('The saved capture admission policy is unavailable.'))
            return
        self._policy = policy
        self._unavailable = False
        self._unavailable_reason = ''
        self._unavailable_raw = None
        self._render_policy()

    def set_available_windows(self, records) -> None:
        """Render detached host-provided window identities; never enumerate."""
        detached = []
        for record in records or ():
            copied = _detached_window_record(record)
            if copied is not None:
                detached.append(copied)
        self._available_windows = detached
        with QSignalBlocker(self.running_windows_combo):
            self.running_windows_combo.clear()
            for record in detached:
                display = record['display_name']
                icon = self._app_icon(record['exe_path'], record.get('icon'))
                self.running_windows_combo.addItem(icon, display, record['exe_path'])
                index = self.running_windows_combo.count() - 1
                self.running_windows_combo.setItemData(
                    index, _tooltip_literal(record['exe_path']),
                    Qt.ItemDataRole.ToolTipRole)
        if self._policy is not None:
            self._render_policy()
        self._sync_controls()

    def _app_icon(self, path, provided=None):
        key = str(path).casefold()
        if isinstance(provided, QIcon) and not provided.isNull():
            self._app_icons[key] = QIcon(provided)
        if key not in self._app_icons:
            provider = QFileIconProvider()
            icon = provider.icon(QFileInfo(path))
            if icon.isNull():
                icon = provider.icon(QFileIconProvider.IconType.File)
            self._app_icons[key] = icon
        return self._app_icons[key]

    def set_capture_status(self, message) -> None:
        """Display a host status message as literal plain text."""
        text = str(message or '')
        self._capture_status_message = text
        if self._unavailable:
            return
        if text:
            self._set_label(self.status_label, text)
            self.status_label.setToolTip(_tooltip_literal(text))
        else:
            self._state_for_valid_policy()

    # ---------- policy rendering ----------
    def _render_policy(self) -> None:
        policy = self._policy
        if policy is None:
            self._render_unavailable(ValueError('Capture admission policy is unavailable.'))
            return
        with QSignalBlocker(self.enabled_checkbox):
            self.enabled_checkbox.setChecked(bool(getattr(policy, 'enabled', False)))
        self.executable_list.clear()
        for raw_path in tuple(getattr(policy, 'executable_paths', ())):
            path = str(raw_path)
            name = ntpath.splitext(ntpath.basename(path))[0] or path
            item = QListWidgetItem(self._app_icon(path), name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setData(Qt.ItemDataRole.ToolTipRole, _tooltip_literal(path))
            self.executable_list.addItem(item)
        self.empty_label.setVisible(self.executable_list.count() == 0)
        self._state_for_valid_policy()
        self._sync_controls()

    def _render_unavailable(self, error: Exception) -> None:
        reason = self._unavailable_reason or str(error).strip() or (
            'The saved policy could not be read.')
        self._set_label(self.status_label, f'Capture admission unavailable: {reason}')
        self.status_label.setToolTip(_tooltip_literal(reason))
        self._set_label(
            self.error_label,
            'Reload after settings are updated, or restart after fixing the settings file.')
        self.executable_list.clear()
        self.empty_label.setVisible(True)
        with QSignalBlocker(self.enabled_checkbox):
            self.enabled_checkbox.setChecked(False)
        self._sync_controls()

    def _state_for_valid_policy(self) -> None:
        if not self._windows_available:
            self._set_label(self.platform_label, '')
            self._platform_label.setText(
                'Windows window admission is unavailable on this platform.')
            if self._capture_status_message:
                self._set_label(self.status_label, self._capture_status_message)
                self.status_label.setToolTip(
                    _tooltip_literal(self._capture_status_message))
            else:
                self._set_label(self.status_label,
                                'Policy loaded; Windows controls are unavailable.')
                self.status_label.setToolTip('')
            return
        self._set_label(self._platform_label, '')
        if self._capture_status_message:
            self._set_label(self.status_label, self._capture_status_message)
            self.status_label.setToolTip(_tooltip_literal(self._capture_status_message))
        else:
            self._set_label(self.status_label, '')
            self.status_label.setToolTip('')

    @property
    def platform_label(self):
        return self._platform_label

    @staticmethod
    def _set_label(label: QLabel, text: str) -> None:
        label.setText(str(text or ''))
        label.setVisible(bool(text))

    def _sync_controls(self) -> None:
        editable = self._policy is not None and not self._unavailable and self._windows_available
        selected_path = self.running_windows_combo.currentData(
            Qt.ItemDataRole.UserRole)
        self.running_windows_combo.setToolTip(
            _tooltip_literal(selected_path) if selected_path else
            _tooltip_literal('Refresh windows to choose an exact executable identity.'))
        self.enabled_checkbox.setEnabled(editable)
        self.add_file_button.setEnabled(editable)
        self.refresh_windows_button.setEnabled(editable)
        self.running_windows_combo.setEnabled(editable)
        self.add_window_button.setEnabled(
            editable and self.running_windows_combo.currentIndex() >= 0
            and bool(self.running_windows_combo.currentData(Qt.ItemDataRole.UserRole)))
        self.remove_button.setEnabled(
            editable and self.executable_list.currentItem() is not None)
        self.reload_button.setEnabled(True)

    # ---------- user actions ----------
    def _enabled_changed(self, state: int) -> None:
        if not self._editable:
            self._restore_enabled_checkbox()
            return
        try:
            candidate = with_enabled(self._policy, bool(state))
        except (AdmissionDataError, AdmissionValidationError, OSError,
                TypeError, ValueError) as exc:
            self._show_candidate_error('change enabled state', exc)
            self._restore_enabled_checkbox()
            return
        changed = self._commit(candidate, 'change enabled state')
        if not changed and self._policy is not None:
            self._restore_enabled_checkbox()

    def _choose_executable(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Choose executable', '',
            'Executable files (*.exe);;All files (*)')
        if path:
            self._add_path(path)

    def _add_selected_window(self) -> None:
        path = self.running_windows_combo.currentData(Qt.ItemDataRole.UserRole)
        if path:
            self._add_path(path)

    def _add_path(self, path: str) -> None:
        if not self._editable:
            return
        try:
            candidate = add_executable(self._policy, str(path))
        except (AdmissionDataError, AdmissionValidationError, OSError,
                TypeError, ValueError) as exc:
            self._show_candidate_error('add executable', exc)
            return
        self._commit(candidate, 'add executable')

    def _remove_selected(self) -> None:
        if not self._editable:
            return
        item = self.executable_list.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else ''
        if path:
            try:
                candidate = remove_executable(self._policy, str(path))
            except (AdmissionDataError, AdmissionValidationError, OSError,
                    TypeError, ValueError) as exc:
                self._show_candidate_error('remove executable', exc)
                return
            self._commit(candidate, 'remove executable')

    def _request_windows_refresh(self) -> None:
        if self._windows_available and not self._unavailable:
            self.refresh_windows_requested.emit()

    def _commit(self, candidate, operation: str) -> bool:
        current = self._policy
        if not self._editable:
            return False
        if candidate == current:
            return True
        expected_revision = int(getattr(current, 'revision', 0))
        try:
            saved = self.settings_manager.save_capture_admission_policy(
                candidate, expected_revision=expected_revision)
        except (AdmissionDataError, AdmissionValidationError, OSError,
                ValueError, TypeError, AttributeError) as exc:
            self._show_commit_error(operation, exc)
            return False
        if saved is not True:
            self._show_commit_error(
                operation,
                ValueError('The approved list changed or the settings could '
                           'not be written.'))
            return False
        self._policy = candidate
        self.policy_changed.emit(int(getattr(candidate, 'revision', 0)))
        try:
            self._render_policy()
        except (AdmissionDataError, AdmissionValidationError, OSError,
                TypeError, ValueError) as exc:
            self._unavailable = True
            self._show_render_error(operation, exc)
            self._sync_controls()
        else:
            self._clear_error()
        return True

    @property
    def _editable(self) -> bool:
        return (self._policy is not None and not self._unavailable
                and self._windows_available)

    def _restore_enabled_checkbox(self) -> None:
        if self._policy is None:
            return
        with QSignalBlocker(self.enabled_checkbox):
            self.enabled_checkbox.setChecked(
                bool(getattr(self._policy, 'enabled', False)))

    def _show_candidate_error(self, operation: str, error: Exception) -> None:
        reason = str(error).strip() or 'The requested value is invalid.'
        self._set_label(self.error_label,
                        f'Could not {operation}: {reason} No changes were saved.')
        self.error_label.setToolTip(_tooltip_literal(reason))

    def _show_render_error(self, operation: str, error: Exception) -> None:
        reason = str(error).strip() or 'The saved policy could not be displayed.'
        self._set_label(
            self.error_label,
            f'Save succeeded, could not refresh after {operation}. '
            f'Reload and retry. {reason}')
        self.error_label.setToolTip(_tooltip_literal(reason))

    def _show_commit_error(self, operation: str, error: Exception) -> None:
        reason = str(error).strip() or 'The settings file rejected the change.'
        self._set_label(self.error_label,
                        f'Could not {operation}: Save failed. Reload and retry. '
                        f'{reason}')
        self.error_label.setToolTip(_tooltip_literal(reason))

    def _clear_error(self) -> None:
        self._set_label(self.error_label, '')
        self.error_label.setToolTip('')
