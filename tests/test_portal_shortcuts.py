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


# Signal validation: nothing the portal sends may take the worker down

class _Recorder:
    def __init__(self, client):
        self.activated, self.deactivated, self.bound, self.failed, self.ready = [], [], [], [], []
        client.activated.connect(self.activated.append)
        client.deactivated.connect(self.deactivated.append)
        client.bound.connect(self.bound.append)
        client.failed.connect(self.failed.append)
        client.session_ready.connect(lambda: self.ready.append(True))


def _client_with_session():
    client = portal.PortalShortcuts()
    client._session = '/org/freedesktop/portal/desktop/session/1_7/ours'
    return client, _Recorder(client)


def test_activated_requires_our_session_handle():
    client, seen = _client_with_session()
    client._handle_signal('Activated', portal.PORTAL_PATH,
                          ['/org/freedesktop/portal/desktop/session/1_7/other', 'save_clip', 1, {}])
    client._handle_signal('Activated', portal.PORTAL_PATH,
                          [client._session, 'save_clip', 2, {}])
    client._handle_signal('Deactivated', portal.PORTAL_PATH,
                          [client._session, 'save_clip', 3, {}])
    assert seen.activated == ['save_clip']
    assert seen.deactivated == ['save_clip']
    assert seen.failed == []


@pytest.mark.parametrize('body', [
    None, 'Activated', [], [42], ['/session/only'], [None, 'save_clip'],
    ['/org/freedesktop/portal/desktop/session/1_7/ours', 7],
])
def test_malformed_signals_are_dropped_and_reported_once(body):
    client, seen = _client_with_session()
    client._handle_signal('Activated', portal.PORTAL_PATH, body)
    client._handle_signal('Activated', portal.PORTAL_PATH, body)
    assert seen.activated == []
    assert len(seen.failed) == 1, 'one warning, not one per message'


def test_shortcuts_changed_checks_session_and_parses():
    client, seen = _client_with_session()
    client._handle_signal('ShortcutsChanged', portal.PORTAL_PATH,
                          ['/other', [('save_clip', {'trigger_description': ('s', 'F1')})]])
    client._handle_signal('ShortcutsChanged', portal.PORTAL_PATH,
                          [client._session, [('save_clip', {'trigger_description': ('s', 'F9')})]])
    client._handle_signal('ShortcutsChanged', portal.PORTAL_PATH, [client._session])
    assert seen.bound == [{'save_clip': 'F9'}]
    assert len(seen.failed) == 1


def test_response_is_only_handled_for_our_own_requests():
    client = portal.PortalShortcuts()
    seen = _Recorder(client)
    calls = []
    client._call = lambda *args: calls.append(args)
    client._expected_session = '/org/freedesktop/portal/desktop/session/1_7/expected'
    client._pending_bind = [('save_clip', {'description': ('s', 'Save clip')})]
    req = portal.request_path(':1.7', 'tok')

    client._handle_signal('Response', '/not/ours', [0, {}])
    assert client.session_active is False

    client._requests[req] = 'create_session'
    client._handle_signal('Response', req, ['bogus'])
    assert client.session_active is False and len(seen.failed) == 1

    client._requests[req] = 'create_session'
    client._handle_signal('Response', req, [0, {'session_handle': ('s', client._expected_session)}])
    assert client.session_active is True
    assert seen.ready == [True]
    assert calls and calls[0][0] == 'BindShortcuts', 'the queued bind goes out once the session exists'


def test_session_creation_retries_then_reports(monkeypatch):
    client = portal.PortalShortcuts()
    seen = _Recorder(client)
    attempts = []
    monkeypatch.setattr(portal.time, 'sleep', lambda s: attempts.append(('sleep', s)))

    def failing():
        client._session_attempts += 1
        raise RuntimeError('CreateSession failed: An app id is required')
    client._create_session = failing

    for _ in range(portal.SESSION_ATTEMPTS):
        client._request_session()
        if not client._jobs.empty():
            assert client._jobs.get_nowait() == ('retry_session', None)

    assert client._session_attempts == portal.SESSION_ATTEMPTS
    assert [s for kind, s in attempts] == list(portal.SESSION_RETRY_SECONDS)
    assert len(seen.failed) == 1 and 'app id' in seen.failed[0]


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


def test_hyprland_keeps_socket_path_unless_forced(tmp_path, monkeypatch):
    # xdg-desktop-portal-hyprland answers GlobalShortcuts but binds no keys
    # by itself, so the generated socket binds stay the Hyprland default.
    if sys.platform == 'win32':
        pytest.skip('Linux portal path')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(portal, 'is_available', lambda: True)
    monkeypatch.setattr(hotkeys.linux_runtime, 'ensure_desktop_entry', lambda: True)
    monkeypatch.delenv('FTHR_FORCE_PORTAL_SHORTCUTS', raising=False)
    import core.compositor as compositor
    monkeypatch.setattr(compositor, 'detect_compositor', lambda: 'hyprland')

    def _never(owner):
        raise AssertionError('portal client constructed on Hyprland')
    monkeypatch.setattr(portal, 'PortalShortcuts', _never)

    manager = hotkeys.HotkeyManager()
    assert manager._ensure_portal() is False
    assert manager.portal_active is False

    monkeypatch.setenv('FTHR_FORCE_PORTAL_SHORTCUTS', '1')
    assert hotkeys.HotkeyManager._hyprland_prefers_socket() is False


# Host app registration (org.freedesktop.host.portal.Registry)

class _FakeRouter:
    def __init__(self, reply=None, exc=None):
        self.sent = []
        self._reply = reply
        self._exc = exc

    def send_and_get_reply(self, msg, timeout=None):
        self.sent.append(msg)
        if self._exc is not None:
            raise self._exc
        return self._reply


def _reply(message_type, body=()):
    return SimpleNamespace(header=SimpleNamespace(message_type=message_type), body=body)


def test_register_app_id_calls_the_host_registry():
    jeepney = pytest.importorskip('jeepney')
    router = _FakeRouter(_reply(jeepney.MessageType.method_return))

    assert portal.register_app_id(router, 'fthr-clips') == ''

    (msg,) = router.sent
    fields = msg.header.fields
    assert fields[jeepney.HeaderFields.interface] == 'org.freedesktop.host.portal.Registry'
    assert fields[jeepney.HeaderFields.member] == 'Register'
    assert fields[jeepney.HeaderFields.destination] == portal.PORTAL_BUS_NAME
    assert msg.body == ('fthr-clips', {})


def test_register_app_id_reports_refusal_and_timeout():
    jeepney = pytest.importorskip('jeepney')
    refused = _FakeRouter(_reply(
        jeepney.MessageType.error,
        ("Could not register app ID: App info not found for 'fthr-clips'",)))
    assert 'App info not found' in portal.register_app_id(refused, 'fthr-clips')

    assert portal.register_app_id(
        _FakeRouter(exc=TimeoutError()), 'fthr-clips') == 'TimeoutError'


@pytest.mark.parametrize('has_entry, app_id', [(True, 'fthr-clips'), (False, '')])
def test_portal_client_registers_only_with_a_desktop_entry(
        tmp_path, monkeypatch, has_entry, app_id):
    # xdg-desktop-portal refuses an id without a matching .desktop file, and
    # a refused Register is harmless but noisy; skip it when there is none.
    if sys.platform == 'win32':
        pytest.skip('Linux portal path')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(portal, 'is_available', lambda: True)
    monkeypatch.setattr(hotkeys.linux_runtime, 'ensure_desktop_entry', lambda: has_entry)
    monkeypatch.setattr(hotkeys.HotkeyManager, '_hyprland_prefers_socket',
                        staticmethod(lambda: False))
    created = []

    class _Client(_FakePortal):
        def __init__(self, owner, app_id=''):
            super().__init__()
            created.append(app_id)
            signal = SimpleNamespace(connect=lambda slot: None)
            self.activated = self.bound = self.session_ready = self.failed = signal

        def start(self):
            return True

    monkeypatch.setattr(portal, 'PortalShortcuts', _Client)

    assert hotkeys.HotkeyManager()._ensure_portal() is True
    assert created == [app_id]


def test_clear_holds_although_the_desktop_keeps_the_key(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)
    manager._on_portal_bound({'save_clip': 'F9', 'save_screenshot': 'F12'})
    fired = []
    manager.save_screenshot_triggered.connect(lambda: fired.append('shot'))

    assert manager.set_hotkey('save_screenshot', '') is True
    assert fake.bound_payloads[-1]['save_screenshot'] == ('Save screenshot', '')
    # The portal has no unbind: the desktop answers with its old key and
    # still delivers it. Neither may undo the clear.
    manager._on_portal_bound({'save_clip': 'F9', 'save_screenshot': 'F12'})
    manager._on_portal_activated('save_screenshot')
    assert manager.get_hotkey('save_screenshot') == ''
    assert manager.get_hotkey('save_clip') == 'F9'
    assert fired == []

    # A restart keeps the clear.
    restarted = hotkeys.HotkeyManager()
    restarted._portal = _FakePortal()
    restarted._on_portal_bound({'save_screenshot': 'F12'})
    restarted._on_portal_activated('save_screenshot')
    assert restarted.get_hotkey('save_screenshot') == ''

    # Assigning a key in FTHR makes the action live again.
    assert manager.set_hotkey('save_screenshot', 'F11') is True
    manager._last_emit_at = {}
    manager._on_portal_activated('save_screenshot')
    assert fired == ['shot']


def test_custom_clip_actions_reach_portal_and_hyprland(tmp_path, monkeypatch):
    manager, fake = _linux_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_start_socket_server', lambda: None)
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)
    manager.configure_custom_clips([60])
    action = hotkeys.custom_clip_action(60)

    assert fake.bound_payloads[-1][action] == ('Save 60-second clip', '')
    manager.set_hotkey(action, 'F6')
    assert action in manager._hyprland_bind_actions()

    seconds = []
    manager.custom_clip_triggered.connect(seconds.append)
    manager._on_portal_activated(action)
    assert seconds == [60]


def test_register_all_starts_the_socket_server_once(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Linux hotkey socket')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(portal, 'is_available', lambda: False)
    manager = hotkeys.HotkeyManager()
    monkeypatch.setattr(manager, '_apply_compositor_config', lambda: None)
    monkeypatch.setattr(manager, '_warn_if_linux_hotkeys_dead', lambda: None)
    monkeypatch.setattr(hotkeys.linux_runtime, 'hotkey_socket_path',
                        lambda create_dir=True: str(tmp_path / 'hotkey.sock'))
    errors = []
    manager.error_occurred.connect(lambda *args: errors.append(args))
    try:
        manager.register_all()
        assert manager._socket_running
        # Every shortcut capture ends in register_all(); the live socket from
        # the first call must not be reported as "in use by another instance".
        manager.begin_input_capture()
        manager.end_input_capture()
        assert errors == []
    finally:
        manager.cleanup()
