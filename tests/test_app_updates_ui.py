"""Updates stay asynchronous and require explicit installer launch consent."""
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from core.app_updates import UpdateCancelled, UpdateRelease
from ui.app_updates import AppUpdateController, AppUpdatesWidget, AUTO_CHECK_KEY, UpdateDialog


class Settings:
    def __init__(self):
        self.values = {}
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        self.saved += 1
        return True


@pytest.fixture
def controller(qtbot, tmp_path):
    release = UpdateRelease('99.0.0', 'https://downloads.fthrclips.com/app.exe',
                            hashlib.sha256(b'installer').hexdigest(), 9, '<b>Plain notes</b>')
    api = SimpleNamespace(DEFAULT_MANIFEST_URL='https://example.invalid/manifest',
                          UpdateCancelled=UpdateCancelled,
                          check_for_update=lambda *args: release)
    def download(release, destination, progress, cancel_event):
        progress(9, 9)
        path = tmp_path / 'update.exe'
        path.write_bytes(b'installer')
        return path
    api.download_update = download
    c = AppUpdateController(Settings(), api=api, platform='win32', packaged=True,
                            destination=tmp_path, launcher=lambda path: None)
    yield c
    c.shutdown()


def finished(qtbot, c):
    qtbot.waitUntil(lambda: not c.busy)


def test_single_job_and_result_shared_with_late_widget(controller, qtbot):
    c = controller
    gate = threading.Event()
    release = c.api.check_for_update()
    c.api.check_for_update = lambda *args: (gate.wait(3), release)[1]
    assert c.check()
    assert not c.check()
    assert not c.download()
    gate.set()
    finished(qtbot, c)
    widget = AppUpdatesWidget(c)
    qtbot.addWidget(widget)
    assert widget.download.isEnabled()
    assert widget.notes.text() == '<b>Plain notes</b>'
    assert widget.notes.textFormat() == Qt.TextFormat.PlainText
    assert widget.status.text() == 'Version 99.0.0 is available.'


def test_download_progress_cancellation_and_retry(controller, qtbot):
    c = controller
    c.check()
    finished(qtbot, c)
    def download(release, destination, progress, cancel_event):
        progress(3, 9)
        cancel_event.wait(3)
        raise UpdateCancelled('Cancelled')
    c.api.download_update = download
    assert c.download()
    qtbot.waitUntil(lambda: c.progress == (3, 9))
    assert not c.check()
    c.cancel()
    finished(qtbot, c)
    assert 'cancelled' in c.status
    assert c.installer is None
    def fail(*args, **kwargs):
        raise OSError('Offline')
    c.api.check_for_update = fail
    c.check()
    finished(qtbot, c)
    assert 'Offline' in c.status
    assert not c.busy


def test_automatic_opt_out_persists_and_dev_does_not_check(controller, qtbot):
    c = controller
    calls = []
    c.api.check_for_update = lambda *args: calls.append(args)
    c.set_automatic(False)
    assert c.settings.values[AUTO_CHECK_KEY] is False
    assert c.settings.saved == 1
    c.start()
    c.auto_check()
    assert not c.busy and not calls
    c.set_automatic(True)
    c.packaged = False
    c.auto_check()
    assert not calls
    assert c.check()  # Manual checks remain usable in a Windows source checkout.
    finished(qtbot, c)
    assert len(calls) == 1


def test_platform_and_shutdown(controller, qtbot):
    c = controller
    c.supported = False
    widget = AppUpdatesWidget(c)
    qtbot.addWidget(widget)
    assert not widget.check.isEnabled()
    assert not widget.download.isEnabled()
    assert not widget.automatic.isEnabled()
    assert not c.check()
    c.shutdown()
    assert c._cancel.is_set()
    assert not c._poll.isActive()
    assert not c._daily.isActive()


def test_installer_requires_consent_and_fresh_hash(controller, qtbot, monkeypatch):
    c = controller
    launches = []
    c.launcher = launches.append
    c.check()
    finished(qtbot, c)
    c.download()
    finished(qtbot, c)
    assert c.installer and not launches
    monkeypatch.setattr(UpdateDialog, 'confirm', lambda *args, **kwargs: False)
    assert not c.open_installer()
    assert not launches
    monkeypatch.setattr(UpdateDialog, 'confirm', lambda *args, **kwargs: True)
    c.installer.write_bytes(b'changed')
    assert c.open_installer()
    finished(qtbot, c)
    assert not launches
    assert c.installer is None
    assert 'verification failed' in c.status
    c.download()
    finished(qtbot, c)
    assert c.open_installer()
    finished(qtbot, c)
    assert launches == [c.installer]


def test_general_page_uses_shared_controller_and_search(controller, qtbot, monkeypatch, tmp_path):
    import main
    from core.settings_manager import SettingsManager
    from ui.settings_search import search_settings
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(main._SettingsPage, '_start_encoder_probe', lambda *args, **kwargs: None)
    page = main._SettingsPage(SettingsManager(), update_controller=controller)
    qtbot.addWidget(page)
    assert page.app_updates_widget.controller is controller
    assert search_settings('installer')[0].target == 'updates_toggle'
    assert page._loaded_categories == {0}

    page.resize(800, 500)
    page.show()
    destination = search_settings('App updates')[0]
    page._open_setting_result(destination)
    qtbot.wait(80)
    section = page.updates_toggle.parentWidget()
    assert section.expanded
    page._open_setting_result(destination)
    qtbot.wait(80)
    assert section.expanded


@pytest.mark.parametrize('legacy_value', [None, True, False, 'true', 1])
def test_no_background_network_without_privacy_consent(controller, legacy_value):
    from ui.app_updates import AUTO_CONSENT_KEY
    c = controller
    c.settings.values[AUTO_CHECK_KEY] = legacy_value
    calls = []
    c.check = lambda: calls.append('network')
    c.start()
    c.auto_check()
    c._startup.timeout.emit()
    c._daily.timeout.emit()
    assert not c.automatic
    assert not calls
    c.settings.values[AUTO_CONSENT_KEY] = True
    assert c.automatic is (legacy_value is True)


@pytest.mark.parametrize('accept', [False, True])
def test_privacy_notice_before_opt_in(controller, qtbot, accept):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.app_updates import AUTO_CONSENT_KEY
    c = controller
    widget = AppUpdatesWidget(c)
    qtbot.addWidget(widget)
    seen = []
    def respond():
        notice = QApplication.activeModalWidget()
        assert isinstance(notice, UpdateDialog)
        assert not c.automatic
        assert 'IP address' in notice.message.text()
        assert 'no longer stay offline' in notice.message.text()
        assert notice.dismiss_button.text() == 'Cancel'
        seen.append(True)
        button = notice.action_button if accept else notice.dismiss_button
        button.click()
    QTimer.singleShot(0, respond)
    widget.automatic.setChecked(True)
    assert seen
    assert c.automatic is accept
    assert widget.automatic.isChecked() is accept
    assert c.settings.get(AUTO_CONSENT_KEY, False) is accept
    if accept:
        widget.automatic.setChecked(False)
        assert not c.automatic
        assert not c.settings.get(AUTO_CONSENT_KEY)


def test_failed_consent_save_stays_off(controller):
    c = controller
    c.settings.save_settings = lambda: False
    c.set_automatic(True)
    assert not c.automatic
    assert 'Could not save' in c.status


def test_missing_settings_stays_off(qtbot):
    c = AppUpdateController(None, platform='win32', packaged=True)
    assert not c.automatic
    c.set_automatic(True)
    assert not c.automatic
    c.shutdown()


def test_download_hidden_until_newer_release_and_hidden_again_when_current(controller, qtbot):
    c = controller
    widget = AppUpdatesWidget(c)
    qtbot.addWidget(widget)
    assert widget.download.isHidden()
    c.check()
    finished(qtbot, c)
    assert not widget.download.isHidden()
    assert widget.download.isEnabled()
    c.api.check_for_update = lambda *args: None
    c.check()
    finished(qtbot, c)
    assert widget.download.isHidden()
    assert not widget.download.isEnabled()
    assert not c.download()
    assert c.status == 'You are using the latest version.'


@pytest.mark.parametrize('automatic', [False, True])
def test_only_background_checks_notify(controller, qtbot, automatic):
    notices = []
    c = controller
    c.update_available.connect(notices.append)
    if automatic:
        c.set_automatic(True)
        c.auto_check()
    else:
        c.check()
    finished(qtbot, c)
    assert c.release is not None
    assert bool(notices) is automatic


def test_update_dialog_escape_and_safe_default(qtbot):
    dialog = UpdateDialog('Automatic update checks', 'Internet access notice.',
                          action='Enable automatic checks')
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.dismiss_button.isDefault()
    assert not dialog.action_button.autoDefault()
    assert 'background' in dialog.action_button.styleSheet()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


@pytest.mark.parametrize('already_in_updates', [False, True])
def test_background_notice_respects_visible_updates_and_opens_destination(
        controller, qtbot, already_in_updates):
    import main
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    updates = QWidget(host)
    updates.setVisible(already_in_updates)
    calls = []
    host._open_settings_category = lambda index: calls.append(('category', index))
    host._settings_page_widget = SimpleNamespace(
        app_updates_widget=updates,
        _open_setting_result=lambda result: calls.append(('target', result.target)))
    main.MainWindow._show_update_available(host, controller.api.check_for_update())
    if already_in_updates:
        assert not hasattr(host, '_update_notice')
    else:
        notice = host._update_notice
        assert isinstance(notice, UpdateDialog)
        assert not notice.isModal()
        notice.action_button.click()
        assert calls == [('category', 0), ('target', 'updates_toggle')]
        assert not notice.isVisible()
