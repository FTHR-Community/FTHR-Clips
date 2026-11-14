"""Settings discovery keeps inactive pages lazy and opens the selected control."""
import pytest
from PySide6.QtCore import QPoint, Qt

import main
from core.settings_manager import SettingsManager
from ui.settings_search import CATEGORY_NAMES, search_settings


@pytest.fixture
def settings_page(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(main._SettingsPage, '_start_encoder_probe', lambda *a, **k: None)
    page = main._SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.resize(1180, 760)
    page.show()
    return page


def test_search_matches_words_aliases_and_category():
    assert search_settings('MIC gain')[0].target == 'mic_vol_slider'
    assert search_settings('clip profiles')[0].category == 7
    assert search_settings('whitelist')[0].category == 8
    assert CATEGORY_NAMES[8] == 'Clip / Allowed games'
    assert search_settings('install plugin')[0].category == 9
    assert not search_settings('   ')
    assert not search_settings('no-such-setting-xyz')
    assert not search_settings('reduce animations')


def test_profiles_share_running_app_discovery_and_cached_results(settings_page):
    page = settings_page
    page.set_capture_admission_windows([{'exe_path': r'C:\Games\First.exe'}])
    page.navigate_category_to(7)
    widget = page.profiles_widget
    assert widget.running_windows_combo.currentData() == r'C:\Games\First.exe'
    requests = []
    page.capture_admission_refresh_windows_requested.connect(lambda: requests.append(True))
    widget.refresh_windows_requested.emit()
    assert requests == [True]
    page.set_capture_admission_windows([{'exe_path': r'C:\Games\Second.exe'}])
    page.set_capture_admission_status('1 running window(s) available.')
    assert widget.running_windows_combo.currentData() == r'C:\Games\Second.exe'
    assert widget._windows_status_label.text() == '1 running window(s) available.'


def test_typing_does_not_materialize_inactive_pages(settings_page, qtbot):
    page = settings_page
    assert page._loaded_categories == {0}
    page.settings_search.setText('microphone')
    qtbot.wait(20)
    assert page._loaded_categories == {0}
    assert page.settings_search_results.isVisible()
    assert not page.stack.isVisible()
    assert page.settings_search_results.topLevelItem(0).childCount() >= 3


def test_enter_opens_control_and_reveals_it(settings_page, qtbot):
    page = settings_page
    page.resize(1180, 420)
    page.settings_search.setText('watermark')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)
    qtbot.wait(30)
    assert page.settings_search.text() == ''
    assert not page.settings_search_results.isVisible()
    assert page.watermark_check.hasFocus()
    assert page._search_highlight.parentWidget() is page.watermark_check
    assert page._search_highlight.isVisible()
    assert page._search_highlight.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    scroll = page.stack.currentWidget().findChild(main.QScrollArea)
    center = page.watermark_check.mapTo(scroll.viewport(), page.watermark_check.rect().center())
    assert scroll.viewport().rect().contains(center)


def test_profiles_live_under_clip_and_top_navigation_skips_them(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('game profiles')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)
    assert page._tab_buttons[7].isHidden()
    assert page._tab_buttons[1].isChecked()
    assert not page._clip_navigation.isVisible()
    assert page._clip_embedded_sections[7].expanded
    page.navigate_category(1)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 10)
    assert not page._clip_navigation.isVisible()


def test_clip_disclosures_load_once_and_preserve_profile_draft(settings_page, qtbot):
    page = settings_page
    from core.game_profiles import create_profile, empty_catalog
    page.sm.save_game_capture_profiles(create_profile(
        empty_catalog(), 'Example', r'C:\Games\Example.exe'), expected_revision=0)
    page.navigate_category_to(1)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)
    assert 7 not in page._loaded_categories
    assert 8 not in page._loaded_categories
    section = page._clip_embedded_sections[7]
    section._header.click()
    assert 7 in page._loaded_categories
    assert section.expanded
    widget = page.profiles_widget
    widget._name_edit.setText('Unsaved name')
    section.collapse()
    section.expand()
    assert page.profiles_widget is widget
    assert widget._name_edit.text() == 'Unsaved name'
    assert widget._dirty
    assert 8 not in page._loaded_categories
    allowed = page._clip_embedded_sections[8]
    allowed._header.click()
    assert 8 in page._loaded_categories
    assert allowed.expanded
    assert page.stack.currentIndex() == 1


def test_empty_result_and_escape_preserve_page(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('no-such-setting-xyz')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    assert page.stack.currentIndex() == 0
    assert 'No settings found' in page.settings_search_results.topLevelItem(0).text(0)
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Escape)
    assert page.stack.isVisible()
    assert not page.settings_search_results.isVisible()


def test_keyboard_can_enter_results_and_return_to_search(settings_page, qtbot):
    page = settings_page
    page.settings_search.setFocus()
    page.settings_search.setText('folder')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Down)
    assert page.settings_search_results.hasFocus()
    qtbot.keyClick(page.settings_search_results, Qt.Key.Key_Escape)
    assert page.settings_search.hasFocus()
    assert page.settings_search.text() == ''


def test_search_remains_available_at_narrow_width(settings_page, qtbot):
    page = settings_page
    page.resize(800, 640)
    qtbot.wait(20)
    assert page.width() == 800
    assert page.settings_search.width() >= 160
    assert page._category_scroll.horizontalScrollBar().maximum() > 0
    page.navigate_category_to(9)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 9)
    assert page._category_scroll.horizontalScrollBar().value() > 0


def test_allowed_games_search_and_subnav_select_clip(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('only allowed games')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)
    assert page._tab_buttons[8].isHidden()
    assert page._tab_buttons[1].isChecked()
    assert not page._clip_navigation.isVisible()
    assert page._clip_embedded_sections[8].expanded
    page.navigate_category(1)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 10)
    page._tab_buttons[1].click()
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)
    page._clip_embedded_sections[8].expand()
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 1)


def test_plugins_search_selects_top_level_category(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('install plugin')
    assert page._loaded_categories == {0}
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 9)
    qtbot.waitUntil(lambda: page.plugin_settings_widget.choose_button.hasFocus())
    assert page._tab_buttons[9].isChecked()
    assert not page._clip_navigation.isVisible()


def test_results_group_by_category_and_collapse_without_loading(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('capture')
    results = page.settings_search_results
    categories = [results.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)
                  for i in range(results.topLevelItemCount())]
    assert categories == sorted(categories)
    assert len(categories) > 1
    group = results.topLevelItem(0)
    assert group.isExpanded()
    results.setCurrentItem(group)
    qtbot.keyClick(results, Qt.Key.Key_Return)
    assert not group.isExpanded()
    assert page._loaded_categories == {0}
    page.settings_search.setText('capture ')
    assert not results.topLevelItem(0).isExpanded()
    qtbot.keyClick(results, Qt.Key.Key_Right)
    assert results.topLevelItem(0).isExpanded()


def test_ultra_mode_search_reveals_toggle_without_persisting(settings_page, qtbot):
    page = settings_page
    changes = []
    page.ultra_performance_mode_changed.connect(changes.append)
    page.settings_search.setText('ultra')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 5)
    page.ultra_performance_check.click()
    assert changes == [True]
    assert not page.sm.get('ultra_performance_mode', False)
    page.set_ultra_performance_mode(False)
    assert not page.ultra_performance_check.isChecked()
    assert changes == [True]


def test_search_highlight_moves_and_clears_on_navigation(settings_page, qtbot):
    page = settings_page
    page.settings_search.setText('replay buffer')
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: '_search_highlight' in page.__dict__)
    assert page._search_highlight.parentWidget() is page.replay_storage_widget.mode_combo
    page.settings_search.setText('capture error')
    assert '_search_highlight' not in page.__dict__
    qtbot.keyClick(page.settings_search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: '_search_highlight' in page.__dict__)
    assert page._search_highlight.parentWidget() is page.error_notifications_check
    checkbox = page.error_notifications_check
    was_checked = checkbox.isChecked()
    qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton,
                     pos=QPoint(8, checkbox.height() // 2))
    assert checkbox.isChecked() is not was_checked
    page.navigate_category_to(1)
    assert '_search_highlight' not in page.__dict__
