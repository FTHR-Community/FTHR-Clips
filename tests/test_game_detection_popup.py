from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))

import main as app_main  # noqa: E402


class _Settings:
    def __init__(self):
        self.values = {
            'game_detection_mode': 'auto',
            'game_detection_enabled': True,
            'game_detection_custom_games': [],
            'game_detection_fallback_desktop': False,
        }
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        self.saved += 1


class _Hotkeys:
    hotkeys = {
        'confirm_game_detection': 'F8',
        'dismiss_game_detection': 'F7',
    }


def test_switch_responds_across_its_entire_surface(qtbot):
    from PySide6.QtCore import QPoint, Qt
    switch = app_main._ToggleSwitch()
    qtbot.addWidget(switch)
    switch.show()
    for x, y in [(2, 10), (19, 10), (35, 10), (19, 2), (19, 18)]:
        previous = switch.isChecked()
        qtbot.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
        assert switch.isChecked() != previous
    switch.setEnabled(False)
    previous = switch.isChecked()
    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(35, 10))
    assert switch.isChecked() == previous


def test_removing_rules_clears_nested_row_widgets(qapp, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    monkeypatch.setattr(app_main.sys, 'platform', 'win32')
    settings = _Settings()
    settings.values['game_detection_custom_games'] = [{'exe_name': 'manual.exe'}]
    settings.values['game_detection_blacklist'] = [{'exe_name': 'blocked.exe'}]
    popup = app_main.GameDetectionPopup(settings, _Hotkeys())
    popup.show()
    qapp.processEvents()
    old_labels = [label for label in popup.findChildren(app_main.QLabel)
                  if label.text() in {'manual.exe', 'blocked.exe'}]
    assert len(old_labels) == 2
    popup._remove_rule(0)
    assert all(label.isHidden() for label in old_labels)
    popup._remove_blacklist_rule(0)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    labels = [label.text() for label in popup.findChildren(app_main.QLabel)]
    assert 'manual.exe' not in labels
    assert 'blocked.exe' not in labels
    assert labels.count('No blocked applications.') == 1
    assert labels.count('No manual game rules.') == 1
    popup.close()


def test_browse_executable_adds_and_persists_game(
    qapp, tmp_path: Path, monkeypatch,
) -> None:
    executable = tmp_path / ('game.exe' if sys.platform == 'win32' else 'game')
    executable.write_bytes(b'MZ')
    if sys.platform != 'win32':
        executable.chmod(0o755)
    settings = _Settings()
    popup = app_main.GameDetectionPopup(settings, _Hotkeys())
    monkeypatch.setattr(
        app_main.QFileDialog, 'getOpenFileName',
        lambda *_args, **_kwargs: (str(executable), ''),
    )

    popup._browse_executable()
    qapp.processEvents()

    rules = settings.values['game_detection_custom_games']
    assert len(rules) == 1
    assert rules[0]['exe_path'] == str(executable)
    assert settings.saved == 1
    assert popup.browser_status.text().startswith('Added ')


def test_cancelled_game_picker_keeps_form_usable(qapp, monkeypatch) -> None:
    settings = _Settings()
    popup = app_main.GameDetectionPopup(settings, _Hotkeys())
    monkeypatch.setattr(
        app_main.QFileDialog, 'getOpenFileName',
        lambda *_args, **_kwargs: ('', ''),
    )

    popup._browse_executable()
    qapp.processEvents()

    assert settings.values['game_detection_custom_games'] == []
    assert popup.browser_status.text() == 'Selection cancelled.'
    assert popup.isEnabled()


def test_invalid_game_selection_shows_useful_error(qapp, tmp_path: Path) -> None:
    selected = tmp_path / 'missing.exe'
    settings = _Settings()
    popup = app_main.GameDetectionPopup(settings, _Hotkeys())

    message = popup._validate_game_selection(str(selected), folder=False)

    assert 'no longer exists' in message
    assert settings.values['game_detection_custom_games'] == []


def test_windows_running_window_can_be_added_and_blocked(qapp, monkeypatch):
    from core import game_detector
    from ui import capture_settings_widget
    monkeypatch.setattr(app_main.sys, 'platform', 'win32')
    monkeypatch.setattr(capture_settings_widget, '_enumerate_capturable_windows',
                        lambda: [{'hwnd': 101, 'title': 'Example Game', 'display_name': 'Example'}])
    window = game_detector.GameWindow(hwnd=101, pid=202, title='Example Game',
                                     exe_path=r'C:\Games\game.exe', exe_name='game.exe')
    monkeypatch.setattr(game_detector, 'get_window_info', lambda hwnd: window if hwnd == 101 else None)
    settings = _Settings()
    popup = app_main.GameDetectionPopup(settings, _Hotkeys())
    popup._refresh_running_windows()
    popup._add_running_window(False)
    popup._add_running_window(False)
    popup._add_running_window(True)
    assert len(settings.values['game_detection_custom_games']) == 1
    assert len(settings.values['game_detection_blacklist']) == 1
    assert settings.values['game_detection_blacklist'][0]['exe_path'] == window.exe_path
    assert not hasattr(popup, 'exe_edit')
    assert not any('CROP' in b.text() for b in popup.findChildren(app_main.QPushButton))
    popup._remove_blacklist_rule(0)
    assert settings.values['game_detection_blacklist'] == []
    popup.close()
