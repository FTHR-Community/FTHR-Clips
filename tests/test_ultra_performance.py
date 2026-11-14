"""Exercise Ultra Performance boundaries without a native capture engine."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('initially_enabled', [False, True])
def test_compact_shell_suspends_and_restores_runtime(
        qtbot, tmp_path, monkeypatch, initially_enabled):
    import main
    from core.settings_manager import SettingsManager
    from ui.capture_card_client import CaptureCardClient

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(CaptureCardClient, '_launch', lambda _self: None)
    monkeypatch.setattr(main, 'CaptureBridge', lambda: SimpleNamespace())
    monkeypatch.setattr(main.MainWindow, '_setup_hotkeys', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, '_start_mic_recorder', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, '_create_system_tray', lambda _self: False)
    monkeypatch.setattr(main.MainWindow, 'stop_engine', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, '_update_status', lambda _self: None)
    start_engine = Mock()
    monkeypatch.setattr(main.MainWindow, 'start_engine', start_engine)
    settings = SettingsManager()
    settings.set('ultra_performance_mode', initially_enabled)
    settings.set('pause_ui_in_background', False)
    settings.set('capture_card_enabled', False)
    settings.set('sound_volume_clip', 45)
    assert settings.save_settings()

    window = main.MainWindow()
    qtbot.addWidget(window)
    try:
        window.show()
        qtbot.waitUntil(lambda: window._file_operations_ready)
        if not initially_enabled:
            window._set_ultra_performance_mode(True)

        assert window._ultra_page.isVisible()
        assert window.capture_toolbar_scroll.isHidden()
        assert window._full_app_shell.isHidden()
        assert not window.clip_grid.refresh_timer.isActive()
        assert window.clip_grid._watcher.directories() == []
        assert not window.library_storage_controller._cadence.isActive()
        assert window.upload_manager._suspended
        assert not window.record_btn.isVisible()
        assert not window._set_main_page(1)
        assert SettingsManager().get('ultra_performance_mode') is True

        window._ultra_page.exit_requested.emit()
        assert window._ultra_page.isHidden()
        assert window._full_app_shell.isVisible()
        window.resize(1280, 720)
        assert window.width() == 1280
        window.main_mode_cluster.setMinimumWidth(1800)
        toolbar_scroll = window.capture_toolbar_scroll.horizontalScrollBar()
        qtbot.waitUntil(lambda: toolbar_scroll.maximum() > 0)
        toolbar_scroll.setValue(toolbar_scroll.maximum())
        for label in ('Cancel', 'Stop ≈ 00:00:42', 'Record'):
            window.record_btn.setText(label)
            assert window.record_btn.isVisibleTo(window)
            assert window.record_btn.parentWidget() is window.capture_toolbar
            assert window.capture_toolbar.rect().contains(window.record_btn.geometry())
        assert window.clip_grid.refresh_timer.isActive()
        assert window.library_storage_controller._cadence.isActive()
        assert not window.upload_manager._suspended
        assert start_engine.call_count == 1
        restored = SettingsManager()
        assert restored.get('ultra_performance_mode') is False
        assert restored.get('capture_card_enabled') is False
        assert restored.get('sound_volume_clip') == 45
    finally:
        monkeypatch.setattr(main.QApplication, 'instance', staticmethod(lambda: None))
        window._perform_full_shutdown()
        window._shutdown_requested = True


def test_suspended_uploader_parks_old_work_but_skips_new_automatic_uploads(
        qtbot, monkeypatch, tmp_path):
    from core.clip_readiness import ClipReadinessRegistry
    from core.upload_manager import UploadManager

    readiness = ClipReadinessRegistry()
    manager = UploadManager(Mock(), readiness)
    monkeypatch.setattr(manager, 'is_enabled', lambda: True)
    monkeypatch.setattr(manager, 'is_uploaded', lambda _path: False)
    monkeypatch.setattr(manager, 'get', lambda _key, _default=None: 'immediate')
    monkeypatch.setattr(manager, '_compression_preflight', lambda _path: (False, '', 0))
    sent = []
    ready = threading.Event()

    def deliver(path, **_kwargs):
        sent.append(path)
        ready.set()
        return True, 'ok'

    monkeypatch.setattr(manager, '_do_single_upload', deliver)
    old = str(tmp_path / 'queued.mp4')
    new = str(tmp_path / 'during-mode.mp4')
    for path in (old, new):
        with open(path, 'wb') as file:
            file.write(b'complete')
    manager.notify_clip_saved(old)
    manager.set_suspended(True)
    assert manager.notify_clip_saved(new).is_set()
    assert readiness.can_access(new)
    manager.start()
    try:
        assert not ready.wait(0.2)
        manager.set_suspended(False)
        qtbot.waitUntil(ready.is_set)
        assert sent == [old]
    finally:
        manager.stop()


def test_existing_save_holds_overlay_providers_through_late_response():
    from main import MainWindow

    operation = SimpleNamespace(result_emitted=False)
    request = object()
    configure = Mock()
    disable = Mock(return_value=True)
    host = SimpleNamespace(
        _ultra_effects_pending=True,
        _ultra_performance=True,
        _file_operations_ready=True,
        _capture_resources_busy=lambda: False,
        _save_state=SimpleNamespace(current=operation),
        _accepted_capture_session=SimpleNamespace(request=request),
        _camera_provider=object(),
        _configure_keyboard_capture_for_request=configure,
        _request_camera_apply=disable,
    )
    MainWindow._sync_ultra_effect_providers(host)
    configure.assert_not_called()
    disable.assert_not_called()

    operation.result_emitted = True
    MainWindow._sync_ultra_effect_providers(host)
    configure.assert_called_once_with(request)
    assert disable.call_args.args[0].enabled is False
    assert not host._ultra_effects_pending


def test_audio_inventory_keeps_pinned_identity_until_an_explicit_edit(
        qtbot, monkeypatch):
    from ui import ultra_performance

    monkeypatch.setattr(ultra_performance.sys, 'platform', 'win32')
    monkeypatch.setattr(ultra_performance.UltraAudioDialog, '_discover', lambda *_: None)
    dialog = ultra_performance.UltraAudioDialog({
        'mic_device_id': '{input}', 'mic_device_name': 'Old microphone label',
        'playback_device_id': '{output}',
    }, None)
    qtbot.addWidget(dialog)
    assert dialog.values() == {}

    with qtbot.assertNotEmitted(dialog.settings_changed):
        dialog._apply_inventory(
            [('{input}', 'USB microphone')], [('{output}', 'USB speakers')], '')
    assert dialog.values() == {}
    with qtbot.waitSignal(dialog.settings_changed) as change:
        dialog.microphone.setCurrentIndex(0)
    assert change.args == [{'mic_device_id': None, 'mic_device_name': None}]
    dialog.mark_saved()
    assert dialog.values() == {}
