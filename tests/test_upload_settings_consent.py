from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

from core.upload_manager import CATBOX_LEGAL_VERSION
from core.uploader_bundle_manifest import (
    UPLOADER_PRIVACY_VERSION,
    UPLOADER_TERMS_VERSION,
)
import ui.upload_settings_widget as upload_ui


class _Manager:
    def __init__(self):
        self.values = {
            'upload_provider': 'catbox',
            'upload_mode': 'manual',
        }
        self.uploader_installed = False
        self.provider_versions = {}
        self.uploader_activation = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        return True

    def is_enabled(self):
        return bool(self.values.get('upload_enabled') and self.uploader_installed)

    def is_plugin_installed(self):
        return self.uploader_installed

    def uploader_legal_text(self):
        return 'Uploader terms', 'Uploader privacy'

    def activate_plugin(self, terms, privacy):
        self.uploader_activation = (terms, privacy)
        self.uploader_installed = True
        self.values['upload_enabled'] = True
        return True, 'installed'

    def set_plugin_enabled(self, enabled):
        self.values['upload_enabled'] = bool(enabled)
        self.save_settings()
        return True, 'updated'

    def provider_consent_current(self, provider):
        expected = CATBOX_LEGAL_VERSION
        return self.provider_versions.get(provider) == expected

    def record_provider_consent(self, provider, version):
        self.provider_versions[provider] = version
        return True






def test_sftp_ui_has_masked_password_and_no_third_party_consent(qapp):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('sftp'))
    assert widget._ensure_provider_ready('sftp')
    assert not widget.sftp_panel.isHidden()
    assert widget.custom_panel.isHidden()
    assert widget.website_btn.isHidden()
    assert widget.sftp_fields['sftp_password'].echoMode() == upload_ui.QLineEdit.EchoMode.Password
    assert widget.sftp_fields['sftp_port'].text() == '22'
    assert not widget.sftp_public_check.isChecked()
    assert not widget.sftp_fields['sftp_public_base_url'].isEnabled()


def test_sftp_save_uses_vault_and_keeps_password_out_of_settings(qapp):
    manager = _Manager()
    manager.uploader_installed = True
    manager.values['upload_enabled'] = True
    saved = []
    manager.sftp_credentials = lambda config: saved.append(dict(config)) or (True, 'saved')
    widget = upload_ui.UploadSettingsWidget(manager)
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('sftp'))
    widget.sftp_fields['sftp_host'].setText('example.com')
    widget.sftp_fields['sftp_username'].setText('tester')
    widget.sftp_fields['sftp_password'].setText(' secret ')
    widget.sftp_public_check.setChecked(True)
    widget.sftp_fields['sftp_public_base_url'].setText('https://videos.example.com/clips/')
    assert widget.sftp_fields['sftp_public_base_url'].isEnabled()
    widget._on_save()
    assert saved[0]['sftp_password'] == ' secret '
    assert 'sftp_password' not in manager.values
    assert manager.values['sftp_host'] == 'example.com'
    assert widget.sftp_fields['sftp_password'].text() == ''
    assert manager.values['sftp_public_enabled'] is True
    assert manager.values['sftp_public_base_url'] == 'https://videos.example.com/clips/'


def test_enable_installs_uploader_only_after_both_legal_acceptances(
        qapp, monkeypatch):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)
    install_calls = []
    monkeypatch.setattr(
        upload_ui,
        '_legal_install_dialog',
        lambda *args, **kwargs: install_calls.append(kwargs['title']) or True,
    )
    monkeypatch.setattr(upload_ui, '_provider_consent_dialog', lambda *_args: True)

    widget._on_enabled_changed(2)

    assert manager.uploader_activation == (
        UPLOADER_TERMS_VERSION, UPLOADER_PRIVACY_VERSION)
    assert manager.provider_versions['catbox'] == CATBOX_LEGAL_VERSION
    assert install_calls == ['Install FTHR Upload Extension']
    assert not widget._body.isHidden()




def test_disable_toggle_updates_manager_and_hides_settings(qapp):
    manager = _Manager()
    manager.uploader_installed = True
    manager.values['upload_enabled'] = True
    widget = upload_ui.UploadSettingsWidget(manager)

    widget.enable_check.click()

    assert manager.values['upload_enabled'] is False
    assert widget._body.isHidden()


def test_upload_toggle_is_saved_without_pressing_save_button(qapp):
    manager = _Manager()
    manager.uploader_installed = True
    manager.provider_versions['catbox'] = CATBOX_LEGAL_VERSION
    saves = []
    manager.save_settings = lambda: saves.append(True) or True
    widget = upload_ui.UploadSettingsWidget(manager)

    widget.enable_check.click()

    assert manager.values['upload_enabled'] is True
    assert len(saves) == 1
    assert not widget._body.isHidden()

    widget.enable_check.click()

    assert manager.values['upload_enabled'] is False
    assert len(saves) == 2
    assert widget._body.isHidden()


def test_failed_connection_test_emits_reason_for_bottom_error_bar(qapp):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)
    failures = []
    widget.connection_failed.connect(failures.append)

    widget._on_test_done(False, 'Provider timed out')

    assert failures == ['Provider timed out']


def test_auto_compress_setting_is_saved_with_upload_settings(qapp):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)

    widget.auto_compress_check.setChecked(True)
    widget._on_save()

    assert manager.values['upload_auto_compress'] is True


def test_donation_buttons_open_official_provider_pages(qapp, monkeypatch):
    manager = _Manager()
    opened = []
    monkeypatch.setattr(
        upload_ui.QDesktopServices,
        'openUrl',
        lambda url: opened.append(url.toString()) or True,
    )
    widget = upload_ui.UploadSettingsWidget(manager)

    widget.catbox_donate_btn.click()

    assert opened == [upload_ui.CATBOX_SUPPORT_URL]


def test_only_selected_provider_support_bar_is_visible(qapp):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)
    widget.show()

    assert not widget.catbox_donate_btn.isHidden()
    widget.provider_combo.setCurrentIndex(
        widget.provider_combo.findData('custom'))

    assert widget.catbox_donate_btn.isHidden()


def test_custom_provider_exposes_and_saves_server_connection_fields(qapp):
    manager = _Manager()
    widget = upload_ui.UploadSettingsWidget(manager)

    widget.provider_combo.setCurrentIndex(
        widget.provider_combo.findData('custom'))
    widget.server_url_edit.setText('clips.example/upload')
    widget.server_auth_edit.setText('Bearer test-token')
    widget._on_save()

    assert not widget.custom_panel.isHidden()
    assert widget.catbox_panel.isHidden()
    assert manager.values['upload_provider'] == 'custom'
    assert manager.values['upload_server_url'] == 'https://clips.example/upload'
    assert manager.values['upload_auth_header'] == 'Bearer test-token'


def test_vaultscope_import_save_and_reload(qapp, monkeypatch):
    import json
    manager = _Manager()
    manager.uploader_installed = True
    manager.values['upload_enabled'] = True
    saved = []
    manager.sftp_credentials = lambda config: saved.append(dict(config)) or (True, 'saved')
    widget = upload_ui.UploadSettingsWidget(manager)
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('vaultscope'))
    assert not widget.vaultscope_panel.isHidden()
    assert not widget.sftp_panel.isHidden()
    opened = []
    monkeypatch.setattr(upload_ui.QDesktopServices, 'openUrl',
                        lambda url: opened.append(url.toString()) or True)
    widget.vaultscope_signup_btn.click()
    widget.website_btn.click()
    assert opened == [upload_ui.VAULTSCOPE_SIGNUP_URL] * 2
    widget.sftp_fields['sftp_fingerprint'].setText('old-server-pin')
    widget.sftp_fields['sftp_directory'].setText('/old-folder')
    widget.sftp_fields['sftp_public_base_url'].setText('https://old.example/')
    widget.sftp_public_check.setChecked(True)
    upload_ui.QApplication.clipboard().setText(json.dumps({
        'username': 'test', 'password': ' passwort123 ', 'port': 2022,
        'domain': 'sftp.vaultscope.de', 'providerName': 'VaultScope'}))
    try:
        widget.vaultscope_paste_btn.click()
    finally:
        upload_ui.QApplication.clipboard().clear()
    assert widget.sftp_fields['sftp_host'].text() == 'sftp.vaultscope.de'
    assert widget.sftp_fields['sftp_port'].text() == '2022'
    assert widget.sftp_fields['sftp_username'].text() == 'test'
    assert widget.sftp_fields['sftp_password'].text() == ' passwort123 '
    assert widget.sftp_fields['sftp_fingerprint'].text() == ''
    assert widget.sftp_fields['sftp_directory'].text() == '.'
    assert widget.sftp_fields['sftp_public_base_url'].text() == ''
    assert not widget.sftp_public_check.isChecked()
    assert not saved  # Import only fills the form.
    widget._on_save()
    assert saved[0]['sftp_password'] == ' passwort123 '
    assert 'sftp_password' not in manager.values
    assert manager.values['upload_provider'] == 'sftp'
    assert manager.values['sftp_provider_name'] == 'VaultScope'
    restored = upload_ui.UploadSettingsWidget(manager)
    assert restored.provider_combo.currentData() == 'vaultscope'
    assert restored.sftp_fields['sftp_port'].text() == '2022'
    assert restored.sftp_fields['sftp_password'].text() == ''
    restored.provider_combo.setCurrentIndex(restored.provider_combo.findData('sftp'))
    restored._on_save()
    assert manager.values['sftp_provider_name'] == ''


def test_vaultscope_invalid_import_preserves_form_without_exposing_credentials(qapp, monkeypatch):
    import json
    widget = upload_ui.UploadSettingsWidget(_Manager())
    widget.sftp_fields['sftp_host'].setText('existing.example')
    widget.sftp_fields['sftp_password'].setText('existing-secret')
    initial = widget._sftp_config()
    warnings = []
    monkeypatch.setattr(upload_ui.FthrMessageDialog, 'warning',
                        lambda *args: warnings.append(args[-1]))
    valid = {'username': 'test', 'password': 'private-secret', 'port': 2022,
             'domain': 'sftp.vaultscope.de', 'providerName': 'VaultScope'}
    invalid = ['not JSON', '[]', 'null', '{"password":"private-secret"}']
    for key, value in [('port', True), ('port', 0), ('port', 65536), ('port', 2.5),
                       ('username', ''), ('password', None), ('domain', 'other.example'),
                       ('providerName', 'Other')]:
        invalid.append(json.dumps({**valid, key: value}))
    try:
        for payload in invalid:
            upload_ui.QApplication.clipboard().setText(payload)
            widget._paste_vaultscope_login()
            assert widget._sftp_config() == initial
    finally:
        upload_ui.QApplication.clipboard().clear()
    assert len(warnings) == len(invalid)
    assert all('private-secret' not in warning for warning in warnings)


def test_vaultscope_connection_test_uses_sftp_transport(qapp, monkeypatch):
    widget = upload_ui.UploadSettingsWidget(_Manager())
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('vaultscope'))
    configs = []
    widget._sm.test_connection = lambda config: configs.append(dict(config)) or (True, 'ok')

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(upload_ui.threading, 'Thread', ImmediateThread)
    widget._on_test_connection()
    assert configs[0]['upload_provider'] == 'sftp'


def test_upload_ui_only_offers_supported_providers(qapp):
    widget = upload_ui.UploadSettingsWidget(_Manager())
    assert [widget.provider_combo.itemData(i) for i in range(widget.provider_combo.count())] == [
        'catbox', 'vaultscope', 'custom', 'sftp']
    assert not hasattr(widget, 'lustful_panel')
