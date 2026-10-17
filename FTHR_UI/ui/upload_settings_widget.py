"""Consent-first settings UI for the optional upload packages."""

from __future__ import annotations

import json
import threading

from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.upload_manager import (
    CATBOX_LEGAL_VERSION,
)
from core.uploader_bundle_manifest import (
    UPLOADER_PRIVACY_VERSION,
    UPLOADER_TERMS_VERSION,
)
from ui.style import set_theme_style
from ui.style import (
    Colors,
    Fonts,
    Sizes,
    button_outline_qss,
    button_primary_qss,
    checkbox_qss,
    combo_qss,
    label_body,
    label_uppercase,
    lineedit_qss,
    radiobutton_qss,
    scrollbar_qss,
    WheelSafeComboBox,
)
from ui.dialogs import FthrMessageDialog
from ui.dialogs import FthrDialog


CATBOX_API_URL = 'https://catbox.moe/user/api.php'
CATBOX_LEGAL_URL = 'https://catbox.moe/legal.php'
CATBOX_SUPPORT_URL = 'https://catbox.moe/support.php'
VAULTSCOPE_SIGNUP_URL = 'https://vaultscope.de/ps/fthr'
_CUSTOM_PROVIDER = 'custom'


def _section_header(title: str) -> QWidget:
    row = QWidget()
    row.setStyleSheet('background: transparent;')
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(Sizes.ROW_GAP)
    label = QLabel(title)
    set_theme_style(label,
        lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_H3}px; font-weight: 600;'
        f' letter-spacing: 0px; background: transparent; border: none;'
        f' font-family: {Fonts.DISPLAY};'))
    layout.addWidget(label)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    set_theme_style(line, lambda: (f'background: {Colors.SHELL_DIVIDER}; border: none;'))
    layout.addWidget(line, 1)
    return row


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    set_theme_style(label, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
    label.setMinimumWidth(90)
    return label


def _legal_install_dialog(
        parent: QWidget,
        *,
        title: str,
        explanation: str,
        terms: str,
        privacy: str,
        install_label: str) -> bool:
    dialog = FthrDialog(title, parent, width=740)
    dialog.resize(740, 660)
    layout = dialog.body_layout
    layout.setContentsMargins(24, 22, 24, 12)
    layout.setSpacing(12)

    heading = QLabel(title)
    set_theme_style(heading, lambda: (label_uppercase(Colors.ACCENT, Fonts.SIZE_H3, 2)))
    layout.addWidget(heading)
    summary = QLabel(explanation)
    summary.setWordWrap(True)
    set_theme_style(summary, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY_L)))
    layout.addWidget(summary)

    terms_title = QLabel('Terms of service')
    set_theme_style(terms_title, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL, 1)))
    layout.addWidget(terms_title)
    terms_view = QTextEdit()
    terms_view.setReadOnly(True)
    terms_view.setPlainText(terms)
    terms_view.setMinimumHeight(150)
    set_theme_style(terms_view, lambda: (f'background: {Colors.SURFACE_1}; color: {Colors.TEXT}; '
        f'border: 1px solid {Colors.BORDER}; padding: 8px;'))
    layout.addWidget(terms_view, 1)

    privacy_title = QLabel('Privacy policy')
    set_theme_style(privacy_title, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL, 1)))
    layout.addWidget(privacy_title)
    privacy_view = QTextEdit()
    privacy_view.setReadOnly(True)
    privacy_view.setPlainText(privacy)
    privacy_view.setMinimumHeight(150)
    set_theme_style(privacy_view, terms_view._theme_style_factory)
    layout.addWidget(privacy_view, 1)

    accept_terms = QCheckBox('I have read and accept the Terms of Service.')
    accept_privacy = QCheckBox('I have read and accept the Privacy Policy.')
    set_theme_style(accept_terms, checkbox_qss)
    set_theme_style(accept_privacy, checkbox_qss)
    layout.addWidget(accept_terms)
    layout.addWidget(accept_privacy)

    buttons = QHBoxLayout()
    buttons.addStretch()
    cancel = QPushButton('Cancel')
    set_theme_style(cancel, button_outline_qss)
    cancel.clicked.connect(dialog.reject)
    buttons.addWidget(cancel)
    install = QPushButton(install_label)
    set_theme_style(install, button_primary_qss)
    install.setEnabled(False)

    def _update_install() -> None:
        install.setEnabled(accept_terms.isChecked() and accept_privacy.isChecked())

    accept_terms.toggled.connect(_update_install)
    accept_privacy.toggled.connect(_update_install)
    install.clicked.connect(dialog.accept)
    buttons.addWidget(install)
    dialog.action_layout.addLayout(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted


def _provider_consent_dialog(parent: QWidget, provider: str) -> bool:
    if provider != 'catbox':
        return False
    name = 'Catbox'
    dialog = FthrDialog(f'Connect {name}', parent, width=560)
    layout = dialog.body_layout
    layout.setContentsMargins(28, 24, 28, 12)
    layout.setSpacing(Sizes.SPACE_5)
    title = QLabel(f'Connect {name}')
    set_theme_style(title, lambda: (label_uppercase(Colors.ACCENT, Fonts.SIZE_H3, 2)))
    layout.addWidget(title)

    copy = (
        'Catbox stores the clip, filename, file size, upload time, and your IP '
        'address. Files are publicly accessible to anyone with the link and may '
        'remain available until deleted. Review Catbox’s Terms, Privacy Policy, '
        'and Acceptable Use Policy before continuing.')
    links = QLabel(
        '<a href="catbox">OPEN CATBOX TERMS, PRIVACY & ACCEPTABLE USE</a>')
    links.linkActivated.connect(
        lambda _target: QDesktopServices.openUrl(QUrl(CATBOX_LEGAL_URL)))
    accept_copy = 'I accept Catbox’s Terms, Privacy Policy, and Acceptable Use Policy.'

    description = QLabel(copy)
    description.setWordWrap(True)
    set_theme_style(description, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY_L)))
    layout.addWidget(description)
    links.setTextFormat(Qt.TextFormat.RichText)
    links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    links.setOpenExternalLinks(False)
    set_theme_style(links, lambda: (label_body(Colors.ACCENT, Fonts.SIZE_BODY)))
    layout.addWidget(links)
    acceptance = QCheckBox(accept_copy)
    acceptance.setWordWrap(True) if hasattr(acceptance, 'setWordWrap') else None
    set_theme_style(acceptance, checkbox_qss)
    layout.addWidget(acceptance)
    buttons = QHBoxLayout()
    buttons.addStretch()
    cancel = QPushButton('Cancel')
    set_theme_style(cancel, button_outline_qss)
    cancel.clicked.connect(dialog.reject)
    buttons.addWidget(cancel)
    accept = QPushButton('Accept & continue')
    set_theme_style(accept, button_primary_qss)
    accept.setEnabled(False)
    acceptance.toggled.connect(accept.setEnabled)
    accept.clicked.connect(dialog.accept)
    buttons.addWidget(accept)
    dialog.action_layout.addLayout(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted


class UploadSettingsWidget(QWidget):
    """Settings surface backed by :class:`core.upload_manager.UploadManager`."""

    _test_finished = Signal(bool, str)
    connection_failed = Signal(str)

    def __init__(self, upload_manager, parent=None, no_scroll=False):
        super().__init__(parent)
        if hasattr(upload_manager, 'is_enabled'):
            self._sm = upload_manager
        else:
            # Lightweight settings-page tests construct the page without the
            # MainWindow-owned coordinator. Production always passes the
            # existing instance through ``_upload_manager_ref``.
            from core.upload_manager import UploadManager
            self._sm = UploadManager(upload_manager)
        self._no_scroll = no_scroll
        self._loading = True
        self._selected_provider = 'catbox'
        self._test_thread: threading.Thread | None = None
        self._test_finished.connect(self._on_test_done)
        self._setup_ui()
        self._load_settings()
        self._loading = False

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        page = QWidget()
        page.setStyleSheet('background: transparent;')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        if not self._no_scroll:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            set_theme_style(scroll, scrollbar_qss)
            scroll.setWidget(page)
            outer.addWidget(scroll)
        else:
            outer.addWidget(page)

        layout.addWidget(_section_header('Upload'))
        layout.addSpacing(12)
        self.enable_check = QCheckBox('Enable optional clip uploader')
        set_theme_style(self.enable_check, checkbox_qss)
        self.enable_check.stateChanged.connect(self._on_enabled_changed)
        layout.addWidget(self.enable_check)

        support_row = QHBoxLayout()
        support_row.setContentsMargins(0, 16, 0, 0)
        support_row.setSpacing(0)
        support_qss = f'''
            QPushButton {{
                background: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_BODY}px;
                font-weight: 400;
                letter-spacing: 0px;
                min-height: 42px;
                padding: 0 18px;
                text-align: left;
            }}
            QPushButton:hover {{ background: {Colors.SURFACE_3}; border-color: {Colors.BORDER_HI}; color: {Colors.TEXT}; }}
            QPushButton:pressed {{ background: {Colors.ACCENT_SOFT}; color: {Colors.TEXT}; }}
        '''
        self.catbox_donate_btn = QPushButton(
            'Support Catbox hosting')
        set_theme_style(self.catbox_donate_btn, lambda support_qss=support_qss: (support_qss))
        self.catbox_donate_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(CATBOX_SUPPORT_URL)))
        support_row.addWidget(self.catbox_donate_btn, 1)

        layout.addLayout(support_row)

        self._body = QWidget()
        self._body.setStyleSheet('background: transparent;')
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        layout.addWidget(self._body)

        body.addSpacing(20)
        body.addWidget(_section_header('Host'))
        body.addSpacing(12)
        provider_row = QHBoxLayout()
        provider_row.addWidget(_field_label('Provider'))
        self.provider_combo = WheelSafeComboBox()
        self.provider_combo.addItem('Catbox', 'catbox')
        self.provider_combo.addItem('VaultScope', 'vaultscope')
        self.provider_combo.addItem('Your server', _CUSTOM_PROVIDER)
        self.provider_combo.addItem('SFTP', 'sftp')
        set_theme_style(self.provider_combo, combo_qss)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self.provider_combo, 1)
        self.website_btn = QPushButton('Open Catbox')
        set_theme_style(self.website_btn, button_outline_qss)
        self.website_btn.clicked.connect(self._open_provider)
        provider_row.addWidget(self.website_btn)
        body.addLayout(provider_row)

        self.catbox_panel = QWidget()
        catbox_row = QHBoxLayout(self.catbox_panel)
        catbox_row.setContentsMargins(0, 8, 0, 0)
        catbox_row.addWidget(_field_label('User hash'))
        self.catbox_userhash = QLineEdit()
        self.catbox_userhash.setPlaceholderText('Optional Catbox account userhash')
        set_theme_style(self.catbox_userhash, lineedit_qss)
        catbox_row.addWidget(self.catbox_userhash, 1)
        body.addWidget(self.catbox_panel)

        self.custom_panel = QFrame()
        set_theme_style(self.custom_panel,
            lambda: (f'background: {Colors.SURFACE_1}; border: 1px solid {Colors.BORDER}; '
            f'border-left: 3px solid {Colors.ACCENT};'))
        custom_body = QVBoxLayout(self.custom_panel)
        custom_body.setContentsMargins(16, 16, 16, 16)
        custom_body.setSpacing(8)

        server_url_row = QHBoxLayout()
        server_url_row.setSpacing(Sizes.ROW_GAP)
        server_url_row.addWidget(_field_label('Server URL'))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText('https://your-server.example.com/upload')
        set_theme_style(self.server_url_edit, lineedit_qss)
        server_url_row.addWidget(self.server_url_edit, 1)
        custom_body.addLayout(server_url_row)

        server_auth_row = QHBoxLayout()
        server_auth_row.setSpacing(Sizes.ROW_GAP)
        server_auth_row.addWidget(_field_label('Auth header'))
        self.server_auth_edit = QLineEdit()
        self.server_auth_edit.setPlaceholderText('Bearer token123  (optional)')
        set_theme_style(self.server_auth_edit, lineedit_qss)
        server_auth_row.addWidget(self.server_auth_edit, 1)
        custom_body.addLayout(server_auth_row)
        body.addWidget(self.custom_panel)

        self.sftp_panel = QWidget()
        sftp_body = QVBoxLayout(self.sftp_panel)
        sftp_body.setContentsMargins(0, 12, 0, 12)
        self.vaultscope_panel = QWidget()
        vaultscope_body = QVBoxLayout(self.vaultscope_panel)
        vaultscope_body.setContentsMargins(0, 0, 0, 8)
        offer = QLabel('FTHR exclusive: get one month free when you sign up through our VaultScope link.')
        offer.setWordWrap(True)
        set_theme_style(offer, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY))
        vaultscope_body.addWidget(offer)
        self.vaultscope_signup_btn = QPushButton('Get one month free at VaultScope')
        set_theme_style(self.vaultscope_signup_btn, button_outline_qss)
        self.vaultscope_signup_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(VAULTSCOPE_SIGNUP_URL)))
        vaultscope_body.addWidget(self.vaultscope_signup_btn)
        self.vaultscope_paste_btn = QPushButton('Paste VaultScope login')
        set_theme_style(self.vaultscope_paste_btn, button_outline_qss)
        self.vaultscope_paste_btn.clicked.connect(self._paste_vaultscope_login)
        vaultscope_body.addWidget(self.vaultscope_paste_btn)
        sftp_body.addWidget(self.vaultscope_panel)
        self.sftp_fields = {}
        for key, title, placeholder in (
                ('sftp_host', 'Host', 'sftp.example.com'),
                ('sftp_port', 'Port', '22'),
                ('sftp_username', 'Username', 'Your SFTP username'),
                ('sftp_password', 'Password', 'Blank keeps the saved password for this host and user'),
                ('sftp_directory', 'Remote folder', '. (home directory) or /uploads'),
                ('sftp_fingerprint', 'Host key (optional)', 'Blank uses trusted known_hosts; SHA256:… pins a specific key'),
                ('sftp_public_base_url', 'Public folder URL', 'https://videos.example.com/clips/')):
            row = QHBoxLayout()
            row.addWidget(_field_label(title))
            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            set_theme_style(field, lineedit_qss)
            if key == 'sftp_password':
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self.sftp_fields[key] = field
            row.addWidget(field, 1)
            sftp_body.addLayout(row)
        self.sftp_public_check = QCheckBox('Generate public HTTPS sharing links for SFTP uploads')
        set_theme_style(self.sftp_public_check, checkbox_qss)
        self.sftp_public_check.toggled.connect(
            self.sftp_fields['sftp_public_base_url'].setEnabled)
        sftp_body.addWidget(self.sftp_public_check)
        note = QLabel(
            'Leave Host key blank to use a trusted key from your SSH known_hosts file or a key '
            'previously verified by FTHR. For a new host, enter the SHA256 fingerprint from '
            'the server operator and test once; FTHR remembers the verified key. '
            'Unknown or changed keys are rejected before sending credentials. '
            'Passwords are saved in Windows Credential Manager. Files get unique names. '
            'For sharing links, enter the HTTPS address that serves this exact remote folder. '
            'Enabling public links makes new files readable by the web server. '
            'Your server must already provide public web access; the URL setting does not set up hosting '
            'or call a publishing API. Leave it disabled for private SFTP storage.')
        note.setWordWrap(True)
        set_theme_style(note, lambda: label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
        sftp_body.addWidget(note)
        self.sftp_forget_btn = QPushButton('Forget saved password for this host and user')
        set_theme_style(self.sftp_forget_btn, button_outline_qss)
        self.sftp_forget_btn.clicked.connect(self._forget_sftp_password)
        sftp_body.addWidget(self.sftp_forget_btn)
        body.addWidget(self.sftp_panel)

        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        self.test_btn = QPushButton('Test connection')
        set_theme_style(self.test_btn, button_outline_qss)
        self.test_btn.clicked.connect(self._on_test_connection)
        test_row.addWidget(self.test_btn)
        self._test_status = QLabel('Not tested')
        set_theme_style(self._test_status, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)))
        test_row.addWidget(self._test_status)
        test_row.addStretch()
        body.addSpacing(12)
        body.addLayout(test_row)

        body.addSpacing(24)
        body.addWidget(_section_header('Auto Upload'))
        body.addSpacing(12)
        self._mode_group = QButtonGroup(self)
        self.mode_immediate = QRadioButton('Upload immediately after capture')
        self.mode_interval = QRadioButton('Upload on an interval')
        self.mode_manual = QRadioButton('Manual only  (right-click a clip → Upload)')
        for index, radio in enumerate(
                (self.mode_immediate, self.mode_interval, self.mode_manual)):
            set_theme_style(radio, radiobutton_qss)
            self._mode_group.addButton(radio, index)
        body.addWidget(self.mode_immediate)
        body.addSpacing(8)
        body.addWidget(self.mode_interval)
        interval_row = QHBoxLayout()
        interval_row.setContentsMargins(28, 4, 0, 0)
        interval_row.setSpacing(Sizes.ROW_GAP)
        every_label = QLabel('Every')
        set_theme_style(every_label,
            lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_MICRO, 1)))
        interval_row.addWidget(every_label)
        self.interval_value = WheelSafeComboBox()
        self.interval_value.addItems(['1', '2', '5', '10', '15', '30', '60'])
        set_theme_style(self.interval_value, combo_qss)
        self.interval_value.setFixedWidth(96)
        self.interval_unit = WheelSafeComboBox()
        self.interval_unit.addItems(['minutes', 'hours', 'days'])
        set_theme_style(self.interval_unit, combo_qss)
        self.interval_unit.setFixedWidth(150)
        interval_row.addWidget(self.interval_value)
        interval_row.addWidget(self.interval_unit)
        interval_row.addStretch()
        body.addLayout(interval_row)
        body.addSpacing(8)
        body.addWidget(self.mode_manual)
        self._mode_group.idClicked.connect(lambda _index: self._update_interval_controls())

        body.addSpacing(24)
        body.addWidget(_section_header('Upload Processing'))
        body.addSpacing(12)
        self.auto_compress_check = QCheckBox(
            'Automatically compress oversized clips before upload')
        set_theme_style(self.auto_compress_check, checkbox_qss)
        body.addWidget(self.auto_compress_check)

        body.addSpacing(24)
        body.addWidget(_section_header('Post-Upload'))
        body.addSpacing(12)
        self.auto_delete_check = QCheckBox(
            'Auto-delete local clip after a confirmed successful upload')
        set_theme_style(self.auto_delete_check, checkbox_qss)
        body.addWidget(self.auto_delete_check)
        body.addSpacing(24)
        self.save_btn = QPushButton('Save upload settings')
        set_theme_style(self.save_btn, button_primary_qss)
        self.save_btn.clicked.connect(self._on_save)
        body.addWidget(self.save_btn, alignment=Qt.AlignmentFlag.AlignLeft)

    def _load_settings(self) -> None:
        enabled = self._sm.is_enabled()
        self.enable_check.setChecked(enabled)
        self._body.setVisible(enabled)
        provider = self._sm.get('upload_provider', 'catbox')
        if provider == 'sftp' and self._sm.get('sftp_provider_name', '') == 'VaultScope':
            provider = 'vaultscope'
        if provider in {'own_server', 'your_server'}:
            provider = _CUSTOM_PROVIDER
        index = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        provider = self.provider_combo.currentData()
        self._selected_provider = provider
        self.catbox_userhash.setText(self._sm.get('catbox_userhash', ''))
        self.server_url_edit.setText(self._sm.get('upload_server_url', ''))
        self.server_auth_edit.setText(self._sm.get('upload_auth_header', ''))
        for key, field in self.sftp_fields.items():
            if key != 'sftp_password':
                default = '22' if key == 'sftp_port' else '.' if key == 'sftp_directory' else ''
                field.setText(str(self._sm.get(key, default)))
        self.sftp_public_check.setChecked(bool(self._sm.get('sftp_public_enabled', False)))
        self.sftp_fields['sftp_public_base_url'].setEnabled(self.sftp_public_check.isChecked())
        mode = self._sm.get('upload_mode', 'manual')
        self.mode_immediate.setChecked(mode == 'immediate')
        self.mode_interval.setChecked(mode == 'interval')
        self.mode_manual.setChecked(mode == 'manual')
        value = str(self._sm.get('upload_interval_value', 5))
        index = self.interval_value.findText(value)
        self.interval_value.setCurrentIndex(index if index >= 0 else 2)
        self.interval_unit.setCurrentText(self._sm.get('upload_interval_unit', 'minutes'))
        self.auto_compress_check.setChecked(
            self._sm.get('upload_auto_compress', False))
        self.auto_delete_check.setChecked(self._sm.get('upload_auto_delete', False))
        self._apply_provider(provider)
        self._update_interval_controls()

    def _on_enabled_changed(self, state: int) -> None:
        requested = bool(state)
        if self._loading:
            self._body.setVisible(requested)
            return
        if requested and not self._sm.is_plugin_installed():
            try:
                terms, privacy = self._sm.uploader_legal_text()
            except Exception as exc:
                self._reject_enable('Upload Extension Unavailable', str(exc))
                return
            accepted = _legal_install_dialog(
                self,
                title='Install FTHR Upload Extension',
                explanation=(
                    'This separately packaged component contains all provider network '
                    'code. FTHR Clips Core remains upload-free.'),
                terms=terms,
                privacy=privacy,
                install_label='ACCEPT & INSTALL UPLOADER',
            )
            if not accepted:
                self._reject_enable('', '')
                return
            ok, message = self._sm.activate_plugin(
                UPLOADER_TERMS_VERSION, UPLOADER_PRIVACY_VERSION)
            if not ok:
                self._reject_enable('Upload Extension Installation Failed', message)
                return
        else:
            ok, message = self._sm.set_plugin_enabled(requested)
            if not ok:
                FthrMessageDialog.warning(self, 'Upload Extension', message)
                self.enable_check.blockSignals(True)
                self.enable_check.setChecked(not requested)
                self.enable_check.blockSignals(False)
                self._body.setVisible(not requested)
                return
        if requested and not self._ensure_provider_ready(
                self.provider_combo.currentData() or 'catbox'):
            self._sm.set_plugin_enabled(False)
            self._reject_enable('', '')
            return
        self._body.setVisible(requested)

    def _reject_enable(self, title: str, message: str) -> None:
        if title and message:
            FthrMessageDialog.warning(self, title, message)
        self.enable_check.blockSignals(True)
        self.enable_check.setChecked(False)
        self.enable_check.blockSignals(False)
        self._body.setVisible(False)

    def _ensure_provider_ready(self, provider: str) -> bool:
        if provider in {_CUSTOM_PROVIDER, 'sftp', 'vaultscope'}:
            return True
        if not self._sm.provider_consent_current(provider):
            if not _provider_consent_dialog(self, provider):
                return False
            version = CATBOX_LEGAL_VERSION
            if not self._sm.record_provider_consent(provider, version):
                FthrMessageDialog.warning(
                    self, 'Uploader', 'Provider consent could not be saved.')
                return False
        return True

    def _on_provider_changed(self, _index: int) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        previous = self._selected_provider
        if (not self._loading and self.enable_check.isChecked()
                and not self._ensure_provider_ready(provider)):
            old_index = self.provider_combo.findData(previous)
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(old_index if old_index >= 0 else 0)
            self.provider_combo.blockSignals(False)
            self._apply_provider(previous)
            return
        self._selected_provider = provider
        self._apply_provider(provider)

    def _apply_provider(self, provider: str) -> None:
        catbox = provider == 'catbox'
        custom = provider == _CUSTOM_PROVIDER
        self.catbox_panel.setVisible(catbox)
        self.custom_panel.setVisible(custom)
        self.sftp_panel.setVisible(provider in {'sftp', 'vaultscope'})
        self.vaultscope_panel.setVisible(provider == 'vaultscope')
        self.website_btn.setVisible(provider in {'catbox', 'vaultscope'})
        self.website_btn.setText('OPEN VAULTSCOPE' if provider == 'vaultscope'
                                 else 'OPEN CATBOX')
        self.catbox_donate_btn.setVisible(catbox)

    def _open_provider(self) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        if provider == 'vaultscope':
            url = VAULTSCOPE_SIGNUP_URL
        elif provider == _CUSTOM_PROVIDER:
            url = self.server_url_edit.text().strip()
        else:
            url = 'https://catbox.moe/'
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_save(self) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        if self.enable_check.isChecked() and not self._ensure_provider_ready(provider):
            return
        if provider in {'sftp', 'vaultscope'}:
            try:
                config = self._sftp_config()
                ok, message = self._sm.sftp_credentials(config)
            except Exception:
                ok, message = False, 'SFTP settings could not be saved. Enable the Upload Extension first.'
            if not ok:
                FthrMessageDialog.warning(self, 'SFTP settings', message)
                return
            for key, value in config.items():
                if key != 'sftp_password':
                    self._sm.set(key, value)
            self.sftp_fields['sftp_password'].clear()
        self._sm.set('upload_provider', 'sftp' if provider == 'vaultscope' else provider)
        if provider in {'sftp', 'vaultscope'}:
            self._sm.set('sftp_provider_name', 'VaultScope' if provider == 'vaultscope' else '')
        self._sm.set('catbox_userhash', self.catbox_userhash.text().strip())
        server_url = self.server_url_edit.text().strip()
        if server_url and not server_url.lower().startswith(('http://', 'https://')):
            server_url = f'https://{server_url}'
            self.server_url_edit.setText(server_url)
        self._sm.set('upload_server_url', server_url)
        self._sm.set('upload_auth_header', self.server_auth_edit.text().strip())
        mode = {0: 'immediate', 1: 'interval', 2: 'manual'}.get(
            self._mode_group.checkedId(), 'manual')
        self._sm.set('upload_mode', mode)
        try:
            value = int(self.interval_value.currentText())
        except ValueError:
            value = 5
        self._sm.set('upload_interval_value', value)
        self._sm.set('upload_interval_unit', self.interval_unit.currentText())
        self._sm.set('upload_auto_compress', self.auto_compress_check.isChecked())
        self._sm.set('upload_auto_delete', self.auto_delete_check.isChecked())
        ok, message = self._sm.set_plugin_enabled(self.enable_check.isChecked())
        if not ok:
            FthrMessageDialog.warning(self, 'Uploader Settings', message)
            return
        self._test_status.setText('Settings saved')
        set_theme_style(self._test_status, lambda: (label_body(Colors.SUCCESS, Fonts.SIZE_BODY)))
        QTimer.singleShot(3000, lambda: self._test_status.setText('Not tested'))

    def _on_test_connection(self) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        if not self._ensure_provider_ready(provider):
            return
        if provider == _CUSTOM_PROVIDER and not self.server_url_edit.text().strip():
            self._test_status.setText('Enter a server URL first')
            set_theme_style(self._test_status, lambda: (label_body(Colors.ERROR, Fonts.SIZE_BODY)))
            return
        self.test_btn.setEnabled(False)
        self._test_status.setText('Testing…')
        config = {
            'upload_provider': 'sftp' if provider == 'vaultscope' else provider,
            'catbox_userhash': self.catbox_userhash.text().strip(),
            'upload_server_url': self.server_url_edit.text().strip(),
            'upload_auth_header': self.server_auth_edit.text().strip(),
            **self._sftp_config(),
        }

        def _run() -> None:
            try:
                ok, message = self._sm.test_connection(config)
            except Exception as exc:
                ok, message = False, str(exc)
            finally:
                config.pop('sftp_password', None)
            self._test_finished.emit(ok, message)

        self._test_thread = threading.Thread(target=_run, daemon=True)
        self._test_thread.start()

    def _paste_vaultscope_login(self) -> None:
        try:
            raw = QApplication.clipboard().text()
            if len(raw) > 65536:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('providerName') != 'VaultScope':
                raise ValueError()
            host = data.get('domain')
            username = data.get('username')
            password = data.get('password')
            port = data.get('port')
            if (not isinstance(host, str) or host.strip().lower() != 'sftp.vaultscope.de'
                    or not isinstance(username, str) or not username.strip()
                    or any(ord(c) < 32 for c in username)
                    or not isinstance(password, str) or not password
                    or type(port) is not int or not 1 <= port <= 65535):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            FthrMessageDialog.warning(
                self, 'VaultScope login',
                'Copy the complete VaultScope login JSON, then paste again. It must include '
                'providerName, domain, port, username and password.')
            return
        # Validate the whole payload before changing any field. Connection-specific
        # options from a previous server must not carry over to the imported login.
        values = {'sftp_host': host.strip().lower(), 'sftp_port': str(port),
                  'sftp_username': username.strip(), 'sftp_password': password,
                  'sftp_directory': '.', 'sftp_fingerprint': '', 'sftp_public_base_url': ''}
        for key, value in values.items():
            self.sftp_fields[key].setText(value)
        self.sftp_public_check.setChecked(False)
        self._test_status.setText('VaultScope login filled in. Test the connection, then save settings.')

    def _sftp_config(self) -> dict:
        return {
            **{key: field.text() if key == 'sftp_password' else field.text().strip()
               for key, field in self.sftp_fields.items()},
            'sftp_public_enabled': self.sftp_public_check.isChecked(),
        }

    def _forget_sftp_password(self) -> None:
        try:
            ok, message = self._sm.sftp_credentials(self._sftp_config(), forget=True)
        except Exception:
            ok, message = False, 'Enable the Upload Extension before managing saved credentials.'
        if ok:
            self.sftp_fields['sftp_password'].clear()
            self._test_status.setText('Saved SFTP password removed')
        else:
            FthrMessageDialog.warning(self, 'SFTP password', message)

    def _on_test_done(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        message = str(message or '').strip()
        if not ok and not message:
            message = 'The upload provider did not return a reason.'
        self._test_status.setText(message)
        set_theme_style(self._test_status, lambda ok=ok: (label_body(
            Colors.SUCCESS if ok else Colors.ERROR, Fonts.SIZE_BODY)))
        if not ok:
            self.connection_failed.emit(message)






    def _update_interval_controls(self) -> None:
        enabled = self.mode_interval.isChecked()
        self.interval_value.setEnabled(enabled)
        self.interval_unit.setEnabled(enabled)
