from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QScrollArea
from ui.settings_layout import SettingHelp


def test_help_opens_with_keyboard_without_changing_setting(qtbot):
    control = QCheckBox('Enable camera overlay')
    row = SettingHelp(control, 'Add your camera to saved clips.')
    qtbot.addWidget(row)
    row.resize(400, 120)
    row.show()
    assert not row.description.isVisible()
    row.button.setFocus()
    qtbot.keyClick(row.button, Qt.Key.Key_Space)
    assert row.description.isVisible()
    assert not control.isChecked()
    assert row.button.accessibleName() == 'About Enable camera overlay'
    qtbot.keyClick(row.button, Qt.Key.Key_Escape)
    assert not row.description.isVisible()
    assert not control.isChecked()


def test_settings_overlay_fits_narrow_width_and_keeps_help_available(
        qtbot, monkeypatch, tmp_path):
    import main
    from core.settings_manager import SettingsManager
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for method in ('_start_encoder_probe', '_populate_camera_devices',
                   '_refresh_keyboard_windows'):
        monkeypatch.setattr(main._SettingsPage, method, lambda *a, **k: None)
    page = main._SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.resize(1100, 850)
    page.show()
    page.navigate_category_to(3)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 3)
    qtbot.wait(50)
    assert page.camera_check.text() == 'Enable camera overlay'
    assert page.keyboard_overlay_check.text() == 'Enable keyboard overlay'
    row = page.camera_check.parentWidget()
    assert isinstance(row, SettingHelp)
    qtbot.mouseClick(row.button, Qt.MouseButton.LeftButton)
    assert row.description.isVisible()
    assert not page.camera_check.isChecked()
    scrolls = page.stack.currentWidget().findChildren(QScrollArea)
    assert scrolls
    assert all(scroll.horizontalScrollBar().maximum() == 0 for scroll in scrolls)


def test_visual_sections_align_controls_and_search_reveals_collapsed_section(
        qtbot, monkeypatch, tmp_path):
    import main
    from core.settings_manager import SettingsManager
    from ui.settings_search import search_settings
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for method in ('_start_encoder_probe', '_populate_camera_devices', '_refresh_keyboard_windows'):
        monkeypatch.setattr(main._SettingsPage, method, lambda *a, **k: None)
    page = main._SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.resize(1500, 1000)
    page.show()
    page.navigate_category_to(3)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 3)
    assert not page._overlay_sections['Keyboard'].expanded
    page._pending_search_destination = search_settings('keyboard overlay')[0]
    page._reveal_setting_result()
    assert page._overlay_sections['Keyboard'].expanded
    assert page.keyboard_overlay_check.isVisible()
    page._overlay_sections['Images'].expand()
    qtbot.wait(50)
    combos = (page.camera_device_combo, page.image_overlay_selector,
              page.image_overlay_fit, page.keyboard_window_combo)
    assert max(c.width() for c in combos) - min(c.width() for c in combos) <= 2
    assert page.image_overlay_opacity.width() == page.keyboard_intensity.width()
    editor = page.unified_overlay_editor
    background = page.input_overlay_preview_background_path
    assert background.parentWidget().parentWidget() is editor.parentWidget()
    assert background.mapTo(page, background.rect().topLeft()).y() > editor.mapTo(page, editor.rect().bottomLeft()).y()
