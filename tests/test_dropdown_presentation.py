from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QComboBox

from ui.app_style import apply_app_style
from ui.dropdown_menu import DropdownItemDelegate


def test_plain_combo_popup_preserves_icons_disabled_rows_and_keyboard(qapp, qtbot):
    apply_app_style(qapp)
    combo = QComboBox()
    qtbot.addWidget(combo)
    pixmap = QPixmap(20, 20)
    pixmap.fill(QColor('green'))
    combo.addItem(QIcon(pixmap), 'First app', 'first')
    combo.addItem('Unavailable', 'disabled')
    combo.model().item(1).setEnabled(False)
    combo.addItem('Third app', 'third')
    combo.resize(260, 40)
    combo.show()
    combo.showPopup()
    qtbot.wait(30)
    assert isinstance(combo.view().itemDelegate(), DropdownItemDelegate)
    assert not combo.itemIcon(0).isNull()
    assert not combo.view().window().mask().isEmpty()
    assert combo.view().verticalScrollBar().maximum() == 0
    qtbot.keyClick(combo.view(), Qt.Key.Key_Down)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentData() == 'third'
    assert not combo.view().isVisible()


def test_allowed_games_keeps_host_icons_and_switch_saves(qapp, qtbot, tmp_path):
    from core.settings_manager import SettingsManager
    from ui.capture_admission import CaptureAdmissionWidget
    from ui.toggle_switch import ToggleSwitch
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {}
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    pixmap = QPixmap(24, 24)
    pixmap.fill(QColor('green'))
    icon = QIcon(pixmap)
    widget.set_available_windows([{'exe_path': r'C:\Games\Example.exe',
                                  'display_name': 'Example', 'icon': icon}])
    assert widget.running_windows_combo.itemIcon(0).cacheKey() == icon.cacheKey()
    widget.add_window_button.click()
    assert widget.executable_list.item(0).icon().cacheKey() == icon.cacheKey()
    assert isinstance(widget.enabled_checkbox, ToggleSwitch)
    widget.show()
    widget.enabled_checkbox.setFocus()
    qtbot.keyClick(widget.enabled_checkbox, Qt.Key.Key_Space)
    assert manager.read_capture_admission_policy().enabled
