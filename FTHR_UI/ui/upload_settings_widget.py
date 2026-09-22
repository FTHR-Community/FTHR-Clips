"""Consent-first settings UI for the optional upload packages."""

from __future__ import annotations

import secrets
import threading

from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
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
    LUSTFUL_LEGAL_VERSION,
)
from core.uploader_bundle_manifest import (
    HARDWARE_POLICY_VERSION,
    UPLOADER_PRIVACY_VERSION,
    UPLOADER_TERMS_VERSION,
)
from ui.style import set_theme_style
from ui.style import (
    Colors,
    Fonts,
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
LUSTFUL_HOME_URL = 'https://fthr.lustful.wtf/'
LUSTFUL_TERMS_URL = 'https://fthr.lustful.wtf/tos'
LUSTFUL_PRIVACY_URL = 'https://fthr.lustful.wtf/privacy'
LUSTFUL_DONATE_URL = 'https://fthr.lustful.wtf/donate'
_CUSTOM_PROVIDER = 'custom'


def _section_header(title: str) -> QWidget:
    row = QWidget()
    row.setStyleSheet('background: transparent;')
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    label = QLabel(title.upper())
    set_theme_style(label,
        lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px; font-weight: 700;'
        f' letter-spacing: 2px; background: transparent; border: none;'
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

    heading = QLabel(title.upper())
    set_theme_style(heading, lambda: (label_uppercase(Colors.ACCENT, Fonts.SIZE_H3, 2)))
    layout.addWidget(heading)
    summary = QLabel(explanation)
    summary.setWordWrap(True)
    set_theme_style(summary, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY_L)))
    layout.addWidget(summary)

    terms_title = QLabel('TERMS OF SERVICE')
    set_theme_style(terms_title, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL, 1)))
    layout.addWidget(terms_title)
    terms_view = QTextEdit()
    terms_view.setReadOnly(True)
    terms_view.setPlainText(terms)
    terms_view.setMinimumHeight(150)
    set_theme_style(terms_view, lambda: (f'background: {Colors.SURFACE_1}; color: {Colors.TEXT}; '
        f'border: 1px solid {Colors.BORDER}; padding: 8px;'))
    layout.addWidget(terms_view, 1)

    privacy_title = QLabel('PRIVACY POLICY')
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
    cancel = QPushButton('CANCEL')
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
    name = 'Catbox' if provider == 'catbox' else 'Lustful'
    dialog = FthrDialog(f'Connect {name}', parent, width=560)
    layout = dialog.body_layout
    layout.setContentsMargins(28, 24, 28, 12)
    layout.setSpacing(14)
    title = QLabel(f'CONNECT {name.upper()}')
    set_theme_style(title, lambda: (label_uppercase(Colors.ACCENT, Fonts.SIZE_H3, 2)))
    layout.addWidget(title)

    if provider == 'catbox':
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
    else:
        copy = (
            'Lustful binds one account to one physical device. It receives your '
            'account ID, uploaded files, file metadata, and a UUID derived locally '
            'from this PC’s hardware identity. Normal files expire after seven days.')
        links = QLabel(
            '<a href="terms">OPEN LUSTFUL TERMS</a>  ·  '
            '<a href="privacy">OPEN LUSTFUL PRIVACY POLICY</a>')

        def _open_lustful(target: str) -> None:
            QDesktopServices.openUrl(QUrl(
                LUSTFUL_TERMS_URL if target == 'terms' else LUSTFUL_PRIVACY_URL))

        links.linkActivated.connect(_open_lustful)
        accept_copy = 'I accept Lustful’s Terms of Service and Privacy Policy.'

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
    cancel = QPushButton('CANCEL')
    set_theme_style(cancel, button_outline_qss)
    cancel.clicked.connect(dialog.reject)
    buttons.addWidget(cancel)
    accept = QPushButton('ACCEPT & CONTINUE')
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
    _account_finished = Signal(bool, object, str)
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
        self._account_thread: threading.Thread | None = None
        self._test_finished.connect(self._on_test_done)
        self._account_finished.connect(self._on_account_done)
        self._setup_ui()
        self._load_settings()
        self._loading = False

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
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
                background: #f4d43a;
                border: 1px solid #ffe66b;
                color: #0a0a0a;
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_BODY_L}px;
                font-weight: bold;
                letter-spacing: 2px;
                min-height: 42px;
                padding: 0 18px;
                text-align: left;
            }}
            QPushButton:hover {{ background: #ffe66b; border-color: #ffffff; }}
            QPushButton:pressed {{ background: #d9b900; }}
        '''
        self.catbox_donate_btn = QPushButton(
            'HELP COVER CATBOX HOSTING  ·  DONATE >')
        self.catbox_donate_btn.setStyleSheet(support_qss)
        self.catbox_donate_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(CATBOX_SUPPORT_URL)))
        support_row.addWidget(self.catbox_donate_btn, 1)
        self.lustful_donate_btn = QPushButton(
            'HELP COVER LUSTFUL HOSTING  ·  DONATE >')
        self.lustful_donate_btn.setStyleSheet(support_qss)
        self.lustful_donate_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(LUSTFUL_DONATE_URL)))
        support_row.addWidget(self.lustful_donate_btn, 1)
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
        self.provider_combo.addItem('Lustful', 'lustful')
        self.provider_combo.addItem('Discord Webhook', 'discord_webhook')
        self.provider_combo.addItem('Your server', _CUSTOM_PROVIDER)
        set_theme_style(self.provider_combo, combo_qss)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self.provider_combo, 1)
        self.website_btn = QPushButton('OPEN CATBOX')
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

        self.discord_panel = QFrame()
        set_theme_style(self.discord_panel,
            lambda: (f'background: {Colors.SURFACE_1}; border: 1px solid {Colors.BORDER}; '
            f'border-left: 3px solid {Colors.ACCENT};'))
        discord_body = QVBoxLayout(self.discord_panel)
        discord_body.setContentsMargins(16, 14, 16, 14)
        discord_body.setSpacing(8)

        discord_url_row = QHBoxLayout()
        discord_url_row.setSpacing(10)
        discord_url_row.addWidget(_field_label('Webhook URL'))
        self.discord_url_edit = QLineEdit()
        self.discord_url_edit.setPlaceholderText('https://discord.com/api/webhooks/ID/TOKEN')
        set_theme_style(self.discord_url_edit, lineedit_qss)
        discord_url_row.addWidget(self.discord_url_edit, 1)
        discord_body.addLayout(discord_url_row)
        body.addWidget(self.discord_panel)

        self.custom_panel = QFrame()
        set_theme_style(self.custom_panel,
            lambda: (f'background: {Colors.SURFACE_1}; border: 1px solid {Colors.BORDER}; '
            f'border-left: 3px solid {Colors.ACCENT};'))
        custom_body = QVBoxLayout(self.custom_panel)
        custom_body.setContentsMargins(16, 14, 16, 14)
        custom_body.setSpacing(8)

        server_url_row = QHBoxLayout()
        server_url_row.setSpacing(10)
        server_url_row.addWidget(_field_label('Server URL'))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText('https://your-server.example.com/upload')
        set_theme_style(self.server_url_edit, lineedit_qss)
        server_url_row.addWidget(self.server_url_edit, 1)
        custom_body.addLayout(server_url_row)

        server_auth_row = QHBoxLayout()
        server_auth_row.setSpacing(10)
        server_auth_row.addWidget(_field_label('Auth header'))
        self.server_auth_edit = QLineEdit()
        self.server_auth_edit.setPlaceholderText('Bearer token123  (optional)')
        set_theme_style(self.server_auth_edit, lineedit_qss)
        server_auth_row.addWidget(self.server_auth_edit, 1)
        custom_body.addLayout(server_auth_row)
        body.addWidget(self.custom_panel)

        self.lustful_panel = QFrame()
        set_theme_style(self.lustful_panel,
            lambda: (f'background: {Colors.SURFACE_1}; border: 1px solid {Colors.BORDER}; '
            f'border-left: 3px solid {Colors.ACCENT};'))
        account = QVBoxLayout(self.lustful_panel)
        account.setContentsMargins(16, 14, 16, 14)
        self.account_state = QLabel('NOT CONNECTED')
        set_theme_style(self.account_state,
            lambda: (label_uppercase(Colors.TEXT_MUTED, Fonts.SIZE_LABEL, 1)))
        account.addWidget(self.account_state)
        self.account_edit = QLineEdit()
        self.account_edit.setPlaceholderText('Existing Lustful account ID for log in')
        set_theme_style(self.account_edit, lineedit_qss)
        account_row = QHBoxLayout()
        account_row.addWidget(self.account_edit, 1)
        self.login_btn = QPushButton('LOG IN')
        set_theme_style(self.login_btn, button_outline_qss)
        self.login_btn.clicked.connect(lambda: self._start_account_action('login'))
        account_row.addWidget(self.login_btn)
        self.register_btn = QPushButton('CREATE ACCOUNT')
        set_theme_style(self.register_btn, button_primary_qss)
        self.register_btn.clicked.connect(lambda: self._start_account_action('register'))
        account_row.addWidget(self.register_btn)
        account.addLayout(account_row)
        self.account_status = QLabel(
            'Hardware Identity is installed separately only after Lustful consent.')
        self.account_status.setWordWrap(True)
        set_theme_style(self.account_status,
            lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)))
        account.addWidget(self.account_status)
        self.logout_btn = QPushButton('LOG OUT LOCALLY')
        set_theme_style(self.logout_btn, button_outline_qss)
        self.logout_btn.clicked.connect(self._logout)
        account.addWidget(self.logout_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addSpacing(10)
        body.addWidget(self.lustful_panel)

        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        self.test_btn = QPushButton('TEST CONNECTION')
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
        interval_row.setSpacing(10)
        every_label = QLabel('EVERY')
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
        self.save_btn = QPushButton('SAVE UPLOAD SETTINGS')
        set_theme_style(self.save_btn, button_primary_qss)
        self.save_btn.clicked.connect(self._on_save)
        body.addWidget(self.save_btn, alignment=Qt.AlignmentFlag.AlignLeft)

    def _load_settings(self) -> None:
        enabled = self._sm.is_enabled()
        self.enable_check.setChecked(enabled)
        self._body.setVisible(enabled)
        provider = self._sm.get('upload_provider', 'catbox')
        if provider == 'fthr':
            provider = 'lustful'
        if provider in {'own_server', 'your_server'}:
            provider = _CUSTOM_PROVIDER
        if provider in {'discord', 'discord_webhook'}:
            provider = 'discord_webhook'
        index = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self._selected_provider = provider
        self.catbox_userhash.setText(self._sm.get('catbox_userhash', ''))
        self.server_url_edit.setText(self._sm.get('upload_server_url', ''))
        self.server_auth_edit.setText(self._sm.get('upload_auth_header', ''))
        self.discord_url_edit.setText(self._sm.get('discord_webhook_url', '') or self._sm.get('discord_active_webhook', ''))
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
        self._load_account()
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
        if provider == _CUSTOM_PROVIDER:
            return True
        if not self._sm.provider_consent_current(provider):
            if not _provider_consent_dialog(self, provider):
                return False
            version = (
                CATBOX_LEGAL_VERSION if provider == 'catbox' else LUSTFUL_LEGAL_VERSION)
            if not self._sm.record_provider_consent(provider, version):
                FthrMessageDialog.warning(
                    self, 'Uploader', 'Provider consent could not be saved.')
                return False
        if provider != 'lustful' or self._sm.is_hardware_identity_installed():
            return True
        try:
            terms, privacy = self._sm.hardware_legal_text()
        except Exception as exc:
            FthrMessageDialog.warning(
                self, 'Hardware Identity Unavailable', str(exc))
            return False
        if not _legal_install_dialog(
                self,
                title='Install Lustful Hardware Identity',
                explanation=(
                    'Lustful requires a device-bound account. This separate local '
                    'capability reads the OS machine identity and returns only a '
                    'derived UUID to the uploader.'),
                terms=terms,
                privacy=privacy,
                install_label='ACCEPT & INSTALL HARDWARE IDENTITY'):
            return False
        ok, message = self._sm.activate_hardware_identity(HARDWARE_POLICY_VERSION)
        if not ok:
            FthrMessageDialog.warning(
                self, 'Hardware Identity Installation Failed', message)
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
        lustful = provider == 'lustful'
        custom = provider == _CUSTOM_PROVIDER
        discord = provider == 'discord_webhook'
        self.catbox_panel.setVisible(catbox)
        self.lustful_panel.setVisible(lustful)
        self.custom_panel.setVisible(custom)
        self.discord_panel.setVisible(discord)
        self.website_btn.setVisible(not custom and not discord)
        self.website_btn.setText('OPEN CATBOX' if catbox else 'OPEN LUSTFUL')
        self.catbox_donate_btn.setVisible(catbox)
        self.lustful_donate_btn.setVisible(lustful)

    def _open_provider(self) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        if provider == _CUSTOM_PROVIDER:
            url = self.server_url_edit.text().strip()
        else:
            url = 'https://catbox.moe/' if provider == 'catbox' else LUSTFUL_HOME_URL
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_save(self) -> None:
        provider = self.provider_combo.currentData() or 'catbox'
        if self.enable_check.isChecked() and not self._ensure_provider_ready(provider):
            return
        if provider == 'lustful' and self.enable_check.isChecked() and not self._sm.local_account():
            self._set_account_status('Create or log in to a Lustful account first.', False)
            return
        self._sm.set('upload_provider', provider)
        self._sm.set('catbox_userhash', self.catbox_userhash.text().strip())
        server_url = self.server_url_edit.text().strip()
        if server_url and not server_url.lower().startswith(('http://', 'https://')):
            server_url = f'https://{server_url}'
            self.server_url_edit.setText(server_url)
        self._sm.set('upload_server_url', server_url)
        self._sm.set('upload_auth_header', self.server_auth_edit.text().strip())
        discord_url = self.discord_url_edit.text().strip()
        self._sm.set('discord_webhook_url', discord_url)
        self._sm.set('discord_active_webhook', discord_url)
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
        if provider == 'discord_webhook' and not self.discord_url_edit.text().strip():
            self._test_status.setText('Enter a Discord Webhook URL first')
            set_theme_style(self._test_status, lambda: (label_body(Colors.ERROR, Fonts.SIZE_BODY)))
            return
        self.test_btn.setEnabled(False)
        self._test_status.setText('Testing…')

        def _run() -> None:
            try:
                ok, message = self._sm.test_connection({
                    'upload_provider': provider,
                    'catbox_userhash': self.catbox_userhash.text().strip(),
                    'upload_server_url': self.server_url_edit.text().strip(),
                    'upload_auth_header': self.server_auth_edit.text().strip(),
                    'discord_webhook_url': self.discord_url_edit.text().strip(),
                    'discord_active_webhook': self.discord_url_edit.text().strip(),
                })
            except Exception as exc:
                ok, message = False, str(exc)
            self._test_finished.emit(ok, message)

        self._test_thread = threading.Thread(target=_run, daemon=True)
        self._test_thread.start()

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

    def _start_account_action(self, mode: str) -> None:
        if not self._ensure_provider_ready('lustful'):
            return
        account_id = (
            secrets.token_hex(8)
            if mode == 'register'
            else self.account_edit.text().strip())
        if not account_id:
            self._set_account_status('Enter an account ID first.', False)
            return
        self.login_btn.setEnabled(False)
        self.register_btn.setEnabled(False)
        self._set_account_status(
            'Creating a new Lustful account…' if mode == 'register'
            else 'Logging in to Lustful…',
            None)

        def _run() -> None:
            try:
                ok, data, message = self._sm.account_action(mode, account_id)
            except Exception as exc:
                ok, data, message = False, {}, str(exc)
            self._account_finished.emit(ok, data, message)

        self._account_thread = threading.Thread(target=_run, daemon=True)
        self._account_thread.start()

    def _on_account_done(self, ok: bool, data: object, message: str) -> None:
        self.login_btn.setEnabled(True)
        self.register_btn.setEnabled(True)
        if not ok:
            self._set_account_status(message or 'Could not connect.', False)
            return
        result = data if isinstance(data, dict) else {}
        self.account_edit.setText(str(result.get('account_id', '')))
        self._load_account()
        self._set_account_status(message or 'Lustful account connected.', True)

    def _load_account(self) -> None:
        account = self._sm.local_account()
        if account:
            self.account_edit.setText(str(account.get('account_id', '')))
            self.account_state.setText('CONNECTED')
            set_theme_style(self.account_state,
                lambda: (label_uppercase(Colors.SUCCESS, Fonts.SIZE_LABEL, 1)))
            self.logout_btn.setVisible(True)
        else:
            self.account_state.setText('NOT CONNECTED')
            set_theme_style(self.account_state,
                lambda: (label_uppercase(Colors.TEXT_MUTED, Fonts.SIZE_LABEL, 1)))
            self.logout_btn.setVisible(False)

    def _logout(self) -> None:
        try:
            ok, message = self._sm.logout_account()
        except Exception as exc:
            ok, message = False, str(exc)
        if not ok:
            self._set_account_status(message or 'Could not log out.', False)
            return
        self.account_edit.clear()
        self._load_account()
        self._set_account_status('Local Lustful account removed.', True)

    def _set_account_status(self, message: str, ok: bool | None) -> None:
        self.account_status.setText(message)
        set_theme_style(self.account_status,
            lambda ok=ok: label_body(
                Colors.SUCCESS if ok is True else
                Colors.ERROR if ok is False else Colors.TEXT_DIM, Fonts.SIZE_BODY))

    def _update_interval_controls(self) -> None:
        enabled = self.mode_interval.isChecked()
        self.interval_value.setEnabled(enabled)
        self.interval_unit.setEnabled(enabled)
