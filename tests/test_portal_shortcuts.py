"""Linux global hotkeys through the XDG GlobalShortcuts portal.

The portal replaces the old ``keyboard`` library: FTHR must never open
``/dev/input`` itself, must hand the desktop a spec-conformant trigger, and
must adopt whatever key the desktop actually kept.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core import hotkey_manager as hotkeys
from core import portal_shortcuts as portal


# Trigger and path helpers

@pytest.mark.parametrize('combo, trigger', [
    ('F9', 'F9'),
    ('Ctrl+Shift+S', 'CTRL+SHIFT+s'),
    ('Alt+C', 'ALT+c'),
    ('Win+Print', 'LOGO+Print'),
    ('Ctrl+Page Up', 'CTRL+Prior'),
    ('Esc', 'Escape'),
    ('Space', 'space'),
    ('7', '7'),
    ('', ''),
])
def test_to_portal_trigger_uses_shortcuts_spec_names(combo, trigger):
    assert portal.to_portal_trigger(combo) == trigger


def test_every_dropdown_key_has_a_trigger():
    for key in hotkeys.AVAILABLE_KEYS:
        combo = hotkeys.normalize_keyboard_combo(key)
        assert portal.to_portal_trigger(combo), key


def test_request_and_session_paths_follow_portal_layout():
    assert portal.request_path(':1.42', 'tok') == (
        '/org/freedesktop/portal/desktop/request/1_42/tok')
    assert portal.session_path(':1.42', 'sess') == (
        '/org/freedesktop/portal/desktop/session/1_42/sess')


def test_parse_bound_shortcuts_unwraps_jeepney_variants():
    # jeepney hands a{sv} values back as (signature, value) tuples.
    shortcuts = ('a(sa{sv})', [
        ('save_clip', {'description': ('s', 'Save clip'),
                       'trigger_description': ('s', 'F9')}),
        ('save_screenshot', {'description': ('s', 'Save screenshot')}),
        'garbage',
    ])
    assert portal.parse_bound_shortcuts(shortcuts) == {
        'save_clip': 'F9',
        'save_screenshot': '',
    }


def test_bind_payload_omits_empty_preferred_trigger(monkeypatch):
    client = portal.PortalShortcuts()
    client.bind({
        'save_clip': ('Save clip', 'Ctrl+Shift+S'),
        'start_recording': ('Start recording', ''),
    })
    kind, payload = client._jobs.get_nowait()
    assert kind == 'bind'
    assert payload == [
        ('save_clip', {'description': ('s', 'Save clip'),
                       'preferred_trigger': ('s', 'CTRL+SHIFT+s')}),
        ('start_recording', {'description': ('s', 'Start recording')}),
    ]


# The keyboard library is gone for good

def test_hotkey_manager_never_imports_input_device_hooks():
    import importlib
    source = importlib.util.find_spec('core.hotkey_manager').loader.get_data(
        hotkeys.__file__).decode()
    assert 'import keyboard' not in source
    assert '/dev/input' not in source
    assert 'keyboard' not in sys.modules or not hasattr(
        sys.modules['keyboard'], 'add_hotkey'), (
        'the evdev-based keyboard package must not be loaded by the app')


# HotkeyManager on Linux

class _FakePortal:
    """Stands in for PortalShortcuts: records bind() calls, no D-Bus."""

    def __init__(self):
        self.bound_payloads = []
        self.configured = 0
        self.stopped = False
        self.session_active = True

    def bind(self, shortcuts):
        self.bound_payloads.append(dict(shortcuts))

    def configure(self):
        self.configured += 1

    def stop(self):
        self.stopped = True


def _linux_manager(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Linux portal path')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(portal, 'is_available', lambda: True)
    monkeypatch.setattr(hotkeys.linux_runtime, 'ensure_desktop_entry', lambda: True)
    fake = _FakePortal()
    manager = hotkeys.HotkeyManager()
    manager._portal = fake
    return manager, fake


def test_register_all_announces_every_action_once(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_start_socket_server', lambda: None)
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)

    manager.register_all()

    assert len(fake.bound_payloads) == 1
    payload = fake.bound_payloads[0]
    assert set(payload) == set(hotkeys._HOTKEY_ACTIONS)
    assert payload['save_clip'] == ('Save clip', 'F9')
    # Unbound actions are still announced so the desktop editor lists them.
    assert payload['start_recording'] == ('Start recording', '')
    assert manager._keyboard_failed is False


def test_set_hotkey_rebinds_through_portal(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)

    assert manager.set_hotkey('save_clip', 'ctrl+shift+s') is True

    assert fake.bound_payloads[-1]['save_clip'] == ('Save clip', 'Ctrl+Shift+S')


def test_portal_activation_dispatches_unless_capturing(tmp_path, monkeypatch):
    manager, _ = _linux_manager(tmp_path, monkeypatch)
    fired = []
    manager.save_clip_triggered.connect(lambda: fired.append('save_clip'))

    manager._on_portal_activated('save_clip')
    assert fired == ['save_clip']

    manager._input_capture_depth = 1
    manager._last_emit_at = {}
    manager._on_portal_activated('save_clip')
    assert fired == ['save_clip'], 'a recording selector must pause dispatch'


def test_desktop_kept_key_is_adopted_and_reported(tmp_path, monkeypatch):
    manager, _ = _linux_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)
    warnings = []
    manager.error_occurred.connect(lambda t, d, level: warnings.append((t, d, level)))
    shown = []
    manager.desktop_bindings_changed.connect(shown.append)

    manager.set_hotkey('save_clip', 'ctrl+shift+s')
    # KDE keeps the shortcut it already knows and answers with it.
    manager._on_portal_bound({'save_clip': 'F9', 'save_screenshot': 'F12'})

    assert manager.get_hotkey('save_clip') == 'F9'
    assert shown == [{'save_clip': 'F9', 'save_screenshot': 'F12'}]
    assert warnings and warnings[0][2] == 'warning'
    assert 'Save clip: F9' in warnings[0][1]
    # The adopted key is what a restart will announce again.
    assert manager.config_file.exists()
    assert '"save_clip": "F9"' in manager.config_file.read_text()


def test_alternative_triggers_keep_the_primary_key(tmp_path, monkeypatch):
    manager, _ = _linux_manager(tmp_path, monkeypatch)
    warnings = []
    manager.error_occurred.connect(lambda *args: warnings.append(args))
    manager._portal_requested = {'save_clip': 'F9'}

    # A second trigger added in the desktop's editor must not corrupt the key.
    manager._on_portal_bound({'save_clip': 'F9, Alt+S'})

    assert manager.get_hotkey('save_clip') == 'F9'
    assert manager.desktop_triggers == {'save_clip': 'F9, Alt+S'}
    assert warnings == []


def test_matching_desktop_answer_is_silent(tmp_path, monkeypatch):
    manager, _ = _linux_manager(tmp_path, monkeypatch)
    warnings = []
    manager.error_occurred.connect(lambda *args: warnings.append(args))
    manager._portal_requested = {'save_clip': 'F9', 'save_screenshot': 'F12'}

    manager._on_portal_bound({'save_clip': 'F9', 'save_screenshot': 'F12'})

    assert warnings == []


def test_no_portal_backend_falls_back_to_socket_instructions(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Linux fallback path')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(portal, 'is_available', lambda: False)
    manager = hotkeys.HotkeyManager()

    assert manager._register_keyboard_hotkey('F9', object(), action='save_clip') is False
    assert manager._keyboard_failed is True
    assert manager.portal_active is False
    assert manager.open_desktop_shortcut_settings() is False


def test_portal_failure_without_session_reverts_to_fallback(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    fake.session_active = False
    warned = []
    monkeypatch.setattr(manager, '_warn_if_linux_hotkeys_dead', lambda: warned.append(True))
    fake.deleteLater = lambda: None

    manager._on_portal_failed('Desktop portal error: An app id is required')

    assert fake.stopped is True
    assert manager.portal_active is False
    assert warned == [True]


def test_cleanup_stops_portal(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    manager.cleanup()
    assert fake.stopped is True
    assert manager.portal_active is False


def test_socket_command_still_available_for_fallback_binds(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Linux hotkey socket')
    cmd = hotkeys.HotkeyManager.socket_command(SimpleNamespace(), 'save_clip')
    assert 'save_clip' in cmd and '-U' in cmd
