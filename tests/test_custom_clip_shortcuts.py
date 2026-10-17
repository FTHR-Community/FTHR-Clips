from types import SimpleNamespace

from core.capture_settings import compute_buffer_seconds
from core.hotkey_manager import HotkeyManager, custom_clip_action


def test_buffer_retains_longest_custom_length():
    assert compute_buffer_seconds(30, [60, 300]) == 302
    assert compute_buffer_seconds(30, [900]) == 900
    assert compute_buffer_seconds(300, [60]) == 302
    assert compute_buffer_seconds(30, [True, 'bad', 901]) == 32


def test_custom_shortcuts_persist_dispatch_and_remove(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(HotkeyManager, '_register_windows_hotkey', lambda *args: True)
    manager = HotkeyManager()
    manager.configure_custom_clips([60, 300], register=False)
    short, long = custom_clip_action(60), custom_clip_action(300)
    try:
        assert manager.get_hotkey(short) == ''
        assert manager.set_hotkey(short, 'Ctrl+F6')
        assert manager.set_hotkey(long, 'Ctrl+F7')
        assert not manager.set_hotkey(long, 'Ctrl+F6')
        assert manager.set_controller_hotkey(long, 'LB+RB')
        assert manager.get_hotkey('save_clip') == 'F9'
        restored = HotkeyManager()
        try:
            restored.configure_custom_clips([60, 300], register=False)
            assert restored.get_hotkey(short) == 'Ctrl+F6'
            assert restored.get_hotkey(long) == 'Ctrl+F7'
            assert restored.get_controller_hotkey(long) == 'LB+RB'
            with qtbot.waitSignal(restored.custom_clip_triggered) as signal:
                restored._emit_action(long, source='keyboard')
            assert signal.args == [300]
            restored.configure_custom_clips([60], register=False)
            assert long not in restored.hotkeys
            with qtbot.assertNotEmitted(restored.custom_clip_triggered):
                restored._emit_action(long, source='keyboard')
            reloaded = HotkeyManager()
            try:
                reloaded.configure_custom_clips([60, 300], register=False)
                assert reloaded.get_hotkey(long) == ''
                assert reloaded.get_controller_hotkey(long) == ''
            finally:
                reloaded.cleanup()
        finally:
            restored.cleanup()
    finally:
        manager.cleanup()


def test_hotkey_popup_refreshes_custom_rows(qtbot, tmp_path, monkeypatch):
    from main import HotkeyPopup
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = HotkeyManager()
    try:
        manager.configure_custom_clips([60, 300], register=False)
        popup = HotkeyPopup(manager)
        qtbot.addWidget(popup)
        assert custom_clip_action(60) in popup._keyboard_buttons
        assert custom_clip_action(300) in popup._controller_buttons
        manager.configure_custom_clips([300], register=False)
        popup.reload_actions()
        assert custom_clip_action(60) not in popup._keyboard_buttons
        assert custom_clip_action(300) in popup._keyboard_buttons
    finally:
        manager.cleanup()


def test_custom_and_normal_shortcuts_save_their_own_duration(tmp_path, monkeypatch):
    from main import MainWindow
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    saved = []
    manager = HotkeyManager()
    host = SimpleNamespace(
        hotkey_manager=manager, _save_clip=saved.append,
        _capture_config=SimpleNamespace(active=SimpleNamespace(normal_clip_seconds=30)),
        clip_duration=30)
    try:
        manager.configure_custom_clips([300], register=False)
        manager.custom_clip_triggered.connect(host._save_clip)
        manager._emit_action(custom_clip_action(300), source='keyboard')
        MainWindow._on_hotkey_save_clip(host)
        assert saved == [300, 30]
    finally:
        manager.cleanup()
