"""
upload_settings_widget.py — Upload tab in the Settings page.

Layout
------
    ── UPLOAD ──────────────────────────────────────
      [☐] Enable clip uploads

    ── SERVER ──────────────────────────────────────
      Server URL    [_______________________________]
      Auth header   [_______________________________]  (optional)
                                    [ TEST CONNECTION ]
      Status:  "Not tested"  /  "OK (200)"  /  "Failed: ..."

    ── AUTO UPLOAD ─────────────────────────────────
      (•) Upload immediately after capture
      ( ) Upload on an interval:  [5 ▼] [minutes ▼]
      ( ) Manual only (right-click → Upload)

    ── POST-UPLOAD ─────────────────────────────────
      [☐] Auto-delete local clip after successful upload
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit,
    QPushButton, QRadioButton, QButtonGroup, QComboBox, QScrollArea,
    QFrame,
)

from ui.style import (
    Colors, Fonts, Sizes,
    label_uppercase, label_body,
    BUTTON_PRIMARY_QSS, BUTTON_OUTLINE_QSS,
    CHECKBOX_QSS, LINEEDIT_QSS, RADIOBUTTON_QSS, COMBO_QSS,
    scrollbar_qss,
)


# ─── Section-header helper (matches settings-page style in main.py) ───────────

def _section_header(title: str) -> QWidget:
    row = QWidget()
    row.setStyleSheet('background: transparent;')
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(10)
    lbl = QLabel(title.upper())
    lbl.setStyleSheet(
        f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px; font-weight: 700;'
        f' letter-spacing: 2px; background: transparent; border: none;'
        f' font-family: {Fonts.DISPLAY};'
    )
    hl.addWidget(lbl)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f'background: {Colors.SHELL_DIVIDER}; border: none;')
    hl.addWidget(line, 1)
    return row


def _field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
    lbl.setMinimumWidth(90)
    return lbl


# ─── Main widget ─────────────────────────────────────────────────────────────

class UploadSettingsWidget(QWidget):
    """Drop-in page widget for the Upload settings tab."""

    def __init__(self, settings_manager, parent=None, no_scroll=False):
        super().__init__(parent)
        self._sm = settings_manager
        self._no_scroll = no_scroll
        self._test_thread: threading.Thread | None = None
        self._setup_ui()
        self._load_settings()

    # ── Build UI ─────────────────────────────────────────────────────────

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        page = QWidget()
        page.setStyleSheet('background: transparent;')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 16, 32)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        if not self._no_scroll:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet(scrollbar_qss())
            outer.addWidget(scroll)

        # ── Enable toggle ────────────────────────────────────────────────
        layout.addWidget(_section_header('Upload'))
        layout.addSpacing(12)

        self.enable_check = QCheckBox('Enable clip uploads')
        self.enable_check.setStyleSheet(CHECKBOX_QSS)
        self.enable_check.stateChanged.connect(self._on_enabled_changed)
        layout.addWidget(self.enable_check)

        # ── Collapsible body (hidden when uploads disabled) ──────────────
        self._body = QWidget()
        self._body.setStyleSheet('background: transparent;')
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # ── Server ───────────────────────────────────────────────────────
        body_layout.addSpacing(16)  # gap between enable checkbox and SERVER section
        body_layout.addWidget(_section_header('Server'))
        body_layout.addSpacing(12)

        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        url_row.addWidget(_field_label('Server URL'))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('https://your-server.example.com/upload')
        self.url_edit.setStyleSheet(LINEEDIT_QSS)
        url_row.addWidget(self.url_edit, 1)
        body_layout.addLayout(url_row)
        body_layout.addSpacing(8)

        auth_row = QHBoxLayout()
        auth_row.setSpacing(10)
        auth_row.addWidget(_field_label('Auth header'))
        self.auth_edit = QLineEdit()
        self.auth_edit.setPlaceholderText('Bearer token123   (optional)')
        self.auth_edit.setStyleSheet(LINEEDIT_QSS)
        auth_row.addWidget(self.auth_edit, 1)
        body_layout.addLayout(auth_row)
        body_layout.addSpacing(12)

        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        self.test_btn = QPushButton('TEST CONNECTION')
        self.test_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        self.test_btn.setFixedWidth(180)
        self.test_btn.clicked.connect(self._on_test_connection)
        test_row.addWidget(self.test_btn)
        self._test_status = QLabel('Not tested')
        self._test_status.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
        test_row.addWidget(self._test_status)
        test_row.addStretch()
        body_layout.addLayout(test_row)

        # ── Auto upload ──────────────────────────────────────────────────
        body_layout.addSpacing(24)
        body_layout.addWidget(_section_header('Auto Upload'))
        body_layout.addSpacing(12)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self.mode_immediate = QRadioButton('Upload immediately after capture')
        self.mode_immediate.setStyleSheet(RADIOBUTTON_QSS)
        self._mode_group.addButton(self.mode_immediate, 0)
        body_layout.addWidget(self.mode_immediate)
        body_layout.addSpacing(8)

        interval_row = QHBoxLayout()
        interval_row.setSpacing(8)
        self.mode_interval = QRadioButton('Upload on an interval:')
        self.mode_interval.setStyleSheet(RADIOBUTTON_QSS)
        self._mode_group.addButton(self.mode_interval, 1)
        interval_row.addWidget(self.mode_interval)

        self.interval_value = QComboBox()
        self.interval_value.addItems(['1', '2', '5', '10', '15', '30', '60'])
        self.interval_value.setCurrentText('5')
        self.interval_value.setStyleSheet(COMBO_QSS)
        self.interval_value.setFixedWidth(64)
        interval_row.addWidget(self.interval_value)

        self.interval_unit = QComboBox()
        self.interval_unit.addItems(['minutes', 'hours', 'days'])
        self.interval_unit.setStyleSheet(COMBO_QSS)
        self.interval_unit.setFixedWidth(90)
        interval_row.addWidget(self.interval_unit)
        interval_row.addStretch()
        body_layout.addLayout(interval_row)
        body_layout.addSpacing(8)

        self.mode_manual = QRadioButton('Manual only  (right-click a clip → Upload)')
        self.mode_manual.setStyleSheet(RADIOBUTTON_QSS)
        self._mode_group.addButton(self.mode_manual, 2)
        body_layout.addWidget(self.mode_manual)

        # Enable/disable interval combos based on selection
        self._mode_group.idClicked.connect(self._on_mode_changed)

        # ── Post-upload ──────────────────────────────────────────────────
        body_layout.addSpacing(24)
        body_layout.addWidget(_section_header('Post-Upload'))
        body_layout.addSpacing(12)

        self.auto_delete_check = QCheckBox('Auto-delete local clip after successful upload')
        self.auto_delete_check.setStyleSheet(CHECKBOX_QSS)
        body_layout.addWidget(self.auto_delete_check)

        # ── Save button ──────────────────────────────────────────────────
        body_layout.addSpacing(28)
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_btn = QPushButton('SAVE UPLOAD SETTINGS')
        save_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        save_btn.clicked.connect(self._on_save)
        save_row.addWidget(save_btn)
        save_row.addStretch()
        body_layout.addLayout(save_row)
        body_layout.addStretch()

        layout.addWidget(self._body)

        if self._no_scroll:
            outer.addWidget(page)
        else:
            scroll.setWidget(page)

    # ── Load / save ───────────────────────────────────────────────────────

    def _load_settings(self):
        enabled = self._sm.get('upload_enabled', False)
        self.enable_check.setChecked(enabled)
        self._body.setVisible(enabled)

        self.url_edit.setText(self._sm.get('upload_server_url', ''))
        self.auth_edit.setText(self._sm.get('upload_auth_header', ''))

        mode = self._sm.get('upload_mode', 'manual')
        self.mode_immediate.setChecked(mode == 'immediate')
        self.mode_interval.setChecked(mode == 'interval')
        self.mode_manual.setChecked(mode == 'manual')

        val  = str(self._sm.get('upload_interval_value', 5))
        unit = self._sm.get('upload_interval_unit', 'minutes')
        idx  = self.interval_value.findText(val)
        self.interval_value.setCurrentIndex(idx if idx >= 0 else 2)
        self.interval_unit.setCurrentText(unit)

        self.auto_delete_check.setChecked(self._sm.get('upload_auto_delete', False))
        self._update_interval_controls()

    def _on_save(self):
        self._sm.set('upload_enabled', self.enable_check.isChecked())
        self._sm.set('upload_server_url', self.url_edit.text().strip())
        self._sm.set('upload_auth_header', self.auth_edit.text().strip())

        mode_map = {0: 'immediate', 1: 'interval', 2: 'manual'}
        self._sm.set('upload_mode', mode_map.get(self._mode_group.checkedId(), 'manual'))

        try:
            val = int(self.interval_value.currentText())
        except ValueError:
            val = 5
        self._sm.set('upload_interval_value', val)
        self._sm.set('upload_interval_unit', self.interval_unit.currentText())
        self._sm.set('upload_auto_delete', self.auto_delete_check.isChecked())
        self._sm.save_settings()

        # Notify UploadManager to re-apply interval timer
        if hasattr(self._sm, '_upload_manager_ref'):
            try:
                self._sm._upload_manager_ref.refresh_settings()
            except Exception:
                pass

        self._test_status.setText('Settings saved')
        self._test_status.setStyleSheet(
            label_body(Colors.SUCCESS, Fonts.SIZE_BODY))
        QTimer.singleShot(3000, lambda: (
            self._test_status.setText('Not tested'),
            self._test_status.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)),
        ))

    # ── Connection test ───────────────────────────────────────────────────

    def _on_test_connection(self):
        url  = self.url_edit.text().strip()
        auth = self.auth_edit.text().strip()
        if not url:
            self._test_status.setText('Enter a server URL first')
            self._test_status.setStyleSheet(label_body(Colors.ERROR, Fonts.SIZE_BODY))
            return

        self.test_btn.setEnabled(False)
        self._test_status.setText('Testing…')
        self._test_status.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))

        def _run():
            from core.upload_manager import test_server_connection
            try:
                ok, msg = test_server_connection(url, auth)
            except Exception as e:
                ok, msg = False, str(e)
            QTimer.singleShot(0, lambda: self._on_test_done(ok, msg))

        self._test_thread = threading.Thread(target=_run, daemon=True)
        self._test_thread.start()

    def _on_test_done(self, ok: bool, msg: str):
        self.test_btn.setEnabled(True)
        color = Colors.SUCCESS if ok else Colors.ERROR
        self._test_status.setText(msg)
        self._test_status.setStyleSheet(label_body(color, Fonts.SIZE_BODY))

    # ── Control state handlers ────────────────────────────────────────────

    def _on_enabled_changed(self, state):
        self._body.setVisible(bool(state))

    def _on_mode_changed(self, btn_id: int):
        self._update_interval_controls()

    def _update_interval_controls(self):
        interval_on = self.mode_interval.isChecked()
        self.interval_value.setEnabled(interval_on)
        self.interval_unit.setEnabled(interval_on)
