from __future__ import annotations

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget


class _SettingsStub:
    def __init__(self) -> None:
        self.settings = {
            'clip_length': 60,
            'framerate': 60,
            'resolution': 'source',
            'bitrate_level': 'custom',
            'custom_bitrate_kbps': 34_500,
            'game_detection_mode': 'auto',
            'game_detection_fallback_desktop': True,
            'game_detection_custom_games': [],
        }

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def save_settings(self):
        return True


class _HotkeyStub:
    hotkeys = {
        'confirm_game_detection': 'F8',
        'dismiss_game_detection': 'F7',
    }


def test_capture_bitrate_stepper_aligns_with_selectors(qtbot):
    from main import CaptureSettingsPopup

    popup = CaptureSettingsPopup(_SettingsStub())
    qtbot.addWidget(popup)
    popup.show()
    qtbot.waitExposed(popup)

    assert popup.custom_bitrate_spin.x() == popup.qual_combo.x()
    assert popup.bitrate_minus_btn.x() > popup.custom_bitrate_spin.width() // 2
    assert popup.bitrate_plus_btn.geometry().right() == (
        popup.custom_bitrate_spin.width() - 6
    )


def test_game_detection_uses_two_segmented_modes(qtbot):
    from main import GameDetectionPopup

    popup = GameDetectionPopup(_SettingsStub(), _HotkeyStub())
    qtbot.addWidget(popup)

    assert not hasattr(popup, 'detection_switch')
    assert popup.auto_switch_btn.isChecked()
    assert not popup.prompt_switch_btn.isChecked()
    assert popup.fallback_switch.isChecked()
    assert 'font-family' in popup.auto_switch_btn.styleSheet()

    popup.prompt_switch_btn.click()
    assert popup.prompt_switch_btn.isChecked()
    assert not popup.auto_switch_btn.isChecked()


def test_popup_frame_is_clamped_inside_the_usable_screen(qtbot):
    from main import _PopupPanel

    screen = QApplication.primaryScreen().availableGeometry()
    host = QWidget()
    qtbot.addWidget(host)
    host.setGeometry(QRect(screen.right() - 104, screen.top() + 16, 96, 44))
    trigger = QPushButton('HOTKEYS', host)
    trigger.setGeometry(0, 0, 96, 32)
    host.show()
    qtbot.waitExposed(host)

    popup = _PopupPanel()
    qtbot.addWidget(popup)
    popup.setMinimumSize(456, 320)
    QVBoxLayout(popup)
    popup.show_below(trigger)
    qtbot.waitExposed(popup)

    safe_area = screen.adjusted(8, 8, -8, -8)
    assert safe_area.contains(popup.frameGeometry())


def test_source_popup_is_wide_and_tracks_trigger_open_state(qtbot):
    from main import SourcePopup, TopBarButton

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(700, 500)
    trigger = TopBarButton('SOURCE', host)
    trigger.setGeometry(20, 20, 200, 40)
    host.show()
    popup = SourcePopup(_SettingsStub())
    qtbot.addWidget(popup)
    trigger.bindPopup(popup)
    popup.show_below(trigger)
    assert popup.mapToGlobal(QPoint()).x() == trigger.mapToGlobal(QPoint()).x()
    qtbot.wait(50)
    assert trigger.width() == popup.width() == 320
    assert trigger.property('popupOpen') is True
    popup.hide()
    assert trigger.property('popupOpen') is False
