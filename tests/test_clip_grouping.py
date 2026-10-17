"""Regression coverage for stable future clip folders."""

from types import SimpleNamespace
from pathlib import Path

from core.clip_grouping import clip_group_for_source, sanitize_clip_folder_name
import main as app_main


class _Settings:
    def __init__(self, **values):
        self.values = values
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, values):
        self.values.update(values)

    def save_settings(self):
        self.saved += 1


def test_minecraft_java_titles_share_one_future_group():
    assert clip_group_for_source({
        'title': 'Minecraft 1.21.4',
        'display_name': 'javaw',
        'exe_name': 'javaw.exe',
    }) == 'Minecraft'
    assert clip_group_for_source({
        'title': 'Minecraft 1.21.4 - Multiplayer (',
        'display_name': 'javaw',
        'exe_name': 'javaw.exe',
    }) == 'Minecraft'


def test_generic_hosts_keep_unrelated_titles_separate():
    first = clip_group_for_source({
        'title': 'Minecraft 1.21.4', 'exe_name': 'javaw.exe',
    })
    second = clip_group_for_source({
        'title': 'Mod Development IDE', 'exe_name': 'javaw.exe',
    })
    assert first == 'Minecraft'
    assert second == 'Mod Development IDE'
    assert first != second


def test_browser_title_is_not_misclassified_as_minecraft():
    assert clip_group_for_source({
        'title': 'Minecraft - YouTube - Firefox',
        'display_name': 'Firefox',
        'exe_name': 'firefox.exe',
    }) == 'firefox'


def test_normal_executable_is_stable_when_title_changes():
    assert clip_group_for_source({
        'title': 'Overwatch - Match 1', 'exe_name': 'Overwatch.exe',
    }) == 'overwatch'
    assert clip_group_for_source({
        'title': 'Overwatch - Match 2', 'exe_name': 'Overwatch.exe',
    }) == 'overwatch'


def test_missing_metadata_falls_back_to_safe_title():
    assert clip_group_for_source({'title': 'My Game: Test'}) == 'My Game Test'
    assert clip_group_for_source({'title': 'Minecraft 1.21.4'}) == 'Minecraft'


def test_manual_and_background_source_use_same_group():
    source = {
        'title': 'Minecraft 1.21.4 - Multiplayer (',
        'display_name': 'javaw',
        'exe_name': 'javaw.exe',
        'exe_path': r'C:\Java\bin\javaw.exe',
    }
    manual_group = clip_group_for_source(source)
    settings = _Settings(capture_mode='desktop', target_hwnd=0,
                         target_window_name='')
    fake_main = SimpleNamespace(settings_manager=settings)
    assert app_main.MainWindow._set_capture_window_source(
        fake_main, dict(source, hwnd=42))
    background_group = settings.get('target_clip_group')
    assert manual_group == background_group == 'Minecraft'


def test_windows_process_query_preserves_wide_handle_and_closes_it(monkeypatch):
    from ui import capture_settings_widget

    class _Callable:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    class _User32:
        def __init__(self):
            self.GetWindowThreadProcessId = _Callable(self._get_pid)

        @staticmethod
        def _get_pid(_hwnd, pid_ptr):
            pid_ptr._obj.value = 123
            return 1

    class _Kernel32:
        def __init__(self):
            self.opened = None
            self.closed = None
            self.OpenProcess = _Callable(self._open)
            self.QueryFullProcessImageNameW = _Callable(self._query)
            self.CloseHandle = _Callable(self._close)

        def _open(self, _access, _inherit, _pid):
            self.opened = 2**32 + 17
            return self.opened

        def _query(self, handle, _flags, buffer, _size):
            assert handle == self.opened
            buffer.value = r'C:\Games\minecraft.exe'
            return True

        def _close(self, handle):
            self.closed = handle
            return True

    kernel32 = _Kernel32()
    monkeypatch.setattr(capture_settings_widget.sys, 'platform', 'win32')
    monkeypatch.setattr(
        capture_settings_widget.ctypes, 'windll',
        SimpleNamespace(kernel32=kernel32, user32=_User32()),
    )

    assert capture_settings_widget._get_hwnd_exe_path(42) == \
        r'C:\Games\minecraft.exe'
    assert kernel32.closed == 2**32 + 17


def test_switching_to_desktop_clears_previous_group():
    settings = _Settings(capture_mode='window', target_hwnd=42,
                         target_window_name='Minecraft',
                         target_clip_group='Minecraft')
    fake_main = SimpleNamespace(settings_manager=settings)
    assert app_main.MainWindow._set_capture_desktop_source(fake_main)
    assert settings.get('capture_mode') == 'desktop'
    assert settings.get('target_clip_group') == ''


def test_startup_desktop_reset_clears_stale_group():
    settings = _Settings(capture_mode='desktop', target_hwnd=0,
                         target_window_name='', target_clip_group='Minecraft')
    fake_main = SimpleNamespace(settings_manager=settings)
    app_main.MainWindow._prepare_startup_capture_source(fake_main)
    assert settings.get('target_clip_group') == ''
    assert settings.saved == 1


def test_dynamic_title_does_not_rewrite_same_group_on_each_poll(monkeypatch):
    from core.game_detector import GameWindow

    settings = _Settings(
        game_detection_enabled=True,
        game_detection_blacklist=[],
        capture_mode='window',
        target_hwnd=42,
        target_window_name='Minecraft 1.21.4 - Match 1',
        target_clip_group='Minecraft',
    )
    window = GameWindow(
        hwnd=42,
        pid=7,
        title='Minecraft 1.21.4 - Multiplayer (',
        exe_path=r'C:\Java\bin\javaw.exe',
        exe_name='javaw.exe',
    )
    fake_main = SimpleNamespace(
        settings_manager=settings,
        _active_game_window=None,
        _game_window_hwnd=lambda candidate: candidate.hwnd,
    )
    monkeypatch.setattr(app_main, 'is_capture_window_valid', lambda _hwnd: True)

    app_main.MainWindow._handle_game_candidate(fake_main, window)

    assert settings.get('target_window_name') == window.display_name
    assert settings.get('target_clip_group') == 'Minecraft'
    assert settings.saved == 0


def test_background_save_uses_persisted_group(monkeypatch, tmp_path):
    class _Bridge:
        def __init__(self):
            self.path = None

        def is_connected(self):
            return True

        def save_clip(self, path, _duration):
            self.path = path
            return False

    class _SaveState:
        def is_busy(self):
            return False

    settings = _Settings(
        capture_mode='window',
        target_hwnd=42,
        target_window_name='Minecraft 1.21.4 - Multiplayer (',
        target_clip_group='Minecraft',
    )
    bridge = _Bridge()
    fake_main = SimpleNamespace(
        _manual_record_state='idle',
        bridge=bridge,
        _capture_health_snapshot=None,
        capture_card=SimpleNamespace(show_error=lambda: None),
        push_error=lambda *_args, **_kwargs: None,
        _last_save_request=0.0,
        _save_state=_SaveState(),
        settings_manager=settings,
        source_popup=SimpleNamespace(
            window_combo=SimpleNamespace(currentIndex=lambda: 0),
            _window_list=[{
                'hwnd': 99,
                'title': 'Firefox - YouTube',
                'display_name': 'Firefox',
                'exe_name': 'firefox.exe',
            }],
        ),
        _pump_save_responses=lambda: None,
        _capture_health_log=SimpleNamespace(warning=lambda *_args: None),
    )
    monkeypatch.setattr(
        app_main, 'clips_directory_from', lambda _settings: tmp_path)
    monkeypatch.setattr(app_main, 'emit_event', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_main, 'evaluate_save_admission',
        lambda _snapshot, duration: SimpleNamespace(
            allowed=True, duration_seconds=duration),
    )

    app_main.MainWindow._save_clip(fake_main, duration_seconds=5)

    assert bridge.path is not None
    assert Path(bridge.path).parent.name == 'Minecraft'


def test_legacy_persisted_group_is_sanitized_without_migration():
    assert sanitize_clip_folder_name(r'Minecraft: 1.21.4') == 'Minecraft 1.21.4'
    assert sanitize_clip_folder_name('CON') == 'CON_game'
    assert sanitize_clip_folder_name('') == 'Unknown'
