"""Real Qt input coverage for configurable navigation and editor transitions."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QDialog, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.navigation_bindings import (
    DEFAULT_NAVIGATION_BINDINGS, NavigationBindings, normalize_binding,
)
from core.settings_manager import SettingsManager
from ui.keyboard_navigation import (
    BindingButton, KeyboardNavigation, KeyboardShortcutSettings,
)


def test_bindings_merge_old_settings_and_persist_changes(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr' / 'settings.json'
    config.parent.mkdir()
    config.write_text('{"navigation_bindings": {"editor_split": "Alt+S"}}')
    settings = SettingsManager()
    bindings = NavigationBindings(settings)
    assert bindings.get('editor_split') == 'Alt+S'
    assert bindings.get('restart_capture') == 'Alt+Shift+R'
    bindings.set('editor_next', 'Ctrl+Right')
    bindings.set('editor_minimize', '')
    loaded = NavigationBindings(SettingsManager())
    assert loaded.get('editor_next') == 'Ctrl+Right'
    assert loaded.get('editor_minimize') == ''
    with pytest.raises(ValueError, match='Already used'):
        bindings.set('library_scroll_down', 'Shift+Down')
    # Main-window keys can also be assigned in the separate editor context.
    bindings.set('editor_split', 'Shift+G')
    bindings.reset()
    assert bindings.values == DEFAULT_NAVIGATION_BINDINGS


def test_shortcut_enable_switches_persist_without_losing_custom_bindings(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    bindings = NavigationBindings(SettingsManager())
    bindings.set('editor_split', 'Alt+S')
    settings = KeyboardShortcutSettings(bindings)
    qtbot.addWidget(settings)
    settings.category_checks['Clip editor'].setChecked(False)
    assert not bindings.action_enabled('editor_split')
    assert bindings.action_enabled('library_scroll_up')
    settings.enabled_check.setChecked(False)
    assert all(not check.isEnabled() for check in settings.category_checks.values())
    restored = NavigationBindings(SettingsManager())
    assert not restored.enabled
    assert not restored.categories['Clip editor']
    assert restored.get('editor_split') == 'Alt+S'
    settings.enabled_check.setChecked(True)
    assert not bindings.action_enabled('editor_split')
    settings.category_checks['Clip editor'].setChecked(True)
    assert bindings.action_enabled('editor_split')
    assert bindings.get('editor_split') == 'Alt+S'


def test_category_switches_change_live_dispatch_and_master_restores_native_tab(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    first, second = QPushButton('First'), QPushButton('Second')
    layout.addWidget(first)
    layout.addWidget(second)
    bindings = NavigationBindings()
    calls = []
    controller = KeyboardNavigation(root, bindings, {
        'toggle_detection': lambda: calls.append('app'),
        'library_scroll_up': lambda: calls.append('clips'),
    }, lambda: {'application', 'main', 'library'})
    root.show()
    first.setFocus()
    bindings.set_category_enabled('Application', False)
    QTest.keyClick(first, Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(first, Qt.Key.Key_Up)
    assert calls == ['clips']
    bindings.set_category_enabled('Application', True)
    QTest.keyClick(first, Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier)
    assert calls == ['clips', 'app']
    bindings.set_enabled(False)
    assert not controller._prefix_keys
    QTest.keyClick(first, Qt.Key.Key_Up)
    assert calls == ['clips', 'app']
    root.activateWindow()
    first.setFocus()
    qtbot.waitUntil(lambda: first.hasFocus())
    QTest.keyClick(first, Qt.Key.Key_Tab)
    assert second.hasFocus()


def test_tab_chord_switches_panel_and_item_keys_do_not_traverse_or_activate(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    first, second = QPushButton('First'), QPushButton('Second')
    layout.addWidget(first)
    layout.addWidget(second)
    switches, clicks = [], []
    first.clicked.connect(lambda: clicks.append(True))
    KeyboardNavigation(root, NavigationBindings(), {
        'switch_panel': lambda: switches.append('switch'),
    }, lambda: {'application', 'main', 'library'})
    root.show()
    first.setFocus()
    qtbot.waitUntil(lambda: first.hasFocus())
    QTest.keyPress(root.windowHandle(), Qt.Key.Key_Tab)
    QTest.keyClick(root.windowHandle(), Qt.Key.Key_S)
    QTest.keyRelease(root.windowHandle(), Qt.Key.Key_Tab)
    assert switches == ['switch']
    for key in (Qt.Key.Key_Tab, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Return):
        QTest.keyClick(first, key)
        assert first.hasFocus()
    assert not clicks


@pytest.mark.parametrize('action,key,modifiers', [
    ('toggle_detection', Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier),
    ('restart_capture', Qt.Key.Key_R,
     Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier),
    ('category_next', Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier),
    ('category_previous', Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier),
    ('library_scroll_up', Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier),
    ('library_scroll_down', Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier),
    ('editor_split', Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier),
    ('editor_delete', Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier),
    ('editor_restore', Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier),
    ('editor_crop', Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier),
    ('editor_export', Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier),
    ('editor_next', Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier),
    ('editor_previous', Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier),
    ('sidebar_next', Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier),
    ('sidebar_previous', Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier),
])
def test_default_chords_dispatch_once_from_child_focus(qtbot, action, key, modifiers):
    root = QDialog()
    qtbot.addWidget(root)
    button = QPushButton('Child', root)
    QVBoxLayout(root).addWidget(button)
    calls = []
    navigation = KeyboardNavigation(root, NavigationBindings(), {
        action: lambda: calls.append(action),
    }, lambda: {'application', 'main', 'library', 'editor'} | (
        {'settings'} if action.startswith('category_') else set()))
    root.show()
    button.setFocus()
    if action in ('editor_next', 'editor_previous'):
        QTest.keyPress(root.windowHandle(), Qt.Key.Key_Tab)
    QTest.keyClick(root.windowHandle(), key, modifiers)
    if action in ('editor_next', 'editor_previous'):
        QTest.keyRelease(root.windowHandle(), Qt.Key.Key_Tab)
    assert calls == [action]
    assert not navigation._held


def test_rebinding_applies_immediately_and_text_fields_keep_editing(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    button = QPushButton('Button')
    edit = QLineEdit('abc')
    layout.addWidget(button)
    layout.addWidget(edit)
    calls = []
    bindings = NavigationBindings()
    navigation = KeyboardNavigation(root, bindings, {
        'library_scroll_down': lambda: calls.append('scroll'),
    }, lambda: {'application', 'main', 'library'})
    root.show()
    button.setFocus()
    qtbot.waitUntil(lambda: button.hasFocus())
    bindings.set('library_scroll_down', 'Alt+N')
    QTest.keyClick(button, Qt.Key.Key_Right)
    QTest.keyClick(button, Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)
    assert calls == ['scroll']
    edit.setFocus()
    edit.setCursorPosition(3)
    QTest.keyClick(edit, Qt.Key.Key_Left)
    assert edit.cursorPosition() == 2
    QTest.keyClick(edit, Qt.Key.Key_Tab)
    assert edit.hasFocus()
    assert navigation.parent() is root


def test_shortcut_recording_accepts_tab_chords_without_running_navigation(qtbot):
    bindings = NavigationBindings()
    root = KeyboardShortcutSettings(bindings)
    qtbot.addWidget(root)
    calls = []
    navigation = KeyboardNavigation(root, bindings, {
        'switch_panel': lambda: calls.append('panel'),
    }, lambda: {'application', 'main', 'library'})
    root.show()
    button = root.buttons['switch_panel']
    assert isinstance(button, BindingButton)
    button.click()
    QTest.keyPress(button, Qt.Key.Key_Tab)
    QTest.keyClick(button, Qt.Key.Key_S)
    QTest.keyRelease(button, Qt.Key.Key_Tab)
    assert bindings.get('switch_panel') == 'Tab+S'
    assert not button._recording
    assert calls == []
    button = root.buttons['editor_split']
    button.click()
    QTest.keyClick(button, Qt.Key.Key_F4, Qt.KeyboardModifier.AltModifier)
    assert bindings.get('editor_split') == 'Alt+F4'
    assert navigation.parent() is root


def test_recording_starts_by_mouse_and_accepts_enter_as_a_custom_binding(qtbot):
    bindings = NavigationBindings()
    root = KeyboardShortcutSettings(bindings)
    qtbot.addWidget(root)
    KeyboardNavigation(root, bindings, {}, lambda: {'application', 'settings'})
    root.show()
    button = root.buttons['editor_export']
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Return)
    assert not button._recording
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.keyClick(button, Qt.Key.Key_Return)
    assert not button._recording
    assert bindings.get('editor_export') == 'Return'


def test_nested_dialog_and_popup_input_does_not_trigger_parent(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    calls = []
    navigation = KeyboardNavigation(root, NavigationBindings(), {
        'library_scroll_down': lambda: calls.append('scroll'),
    }, lambda: {'application', 'main', 'library'})
    root.show()
    child = QDialog(root)
    qtbot.addWidget(child)
    child.setModal(True)
    child.show()
    QTest.keyClick(child, Qt.Key.Key_Down)
    assert calls == []
    assert navigation.parent() is root


def test_settings_categories_wrap_without_item_navigation(
        qtbot, monkeypatch, tmp_path):
    from main import _SettingsPage
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    navigation = KeyboardNavigation(page, page.navigation_bindings, {
        'category_next': lambda: page.navigate_category(1),
        'category_previous': lambda: page.navigate_category(-1),
    }, lambda: {'application', 'main', 'settings'})
    page.resize(1100, 720)
    page.show()
    QTest.keyClick(page, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 6)
    assert page.stack._slide._horizontal
    assert page.stack._slide._direction == -1
    assert page._tab_buttons[6].isChecked()
    assert page.stack.currentWidget() is page.shortcut_settings
    items = list(page.shortcut_settings.buttons.values())
    items[0].setFocus()
    QTest.keyClick(items[0], Qt.Key.Key_Tab)
    assert items[0].hasFocus()
    QTest.keyClick(items[0], Qt.Key.Key_Right)
    QTest.keyClick(items[0], Qt.Key.Key_Return)
    assert not items[0]._recording
    assert items[0].hasFocus()
    QTest.keyClick(items[0], Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 0)
    assert page.stack._slide._horizontal
    assert page.stack._slide._direction == 1
    assert page._tab_buttons[0].isChecked()
    assert navigation.parent() is page


def test_adjacent_clips_follow_the_full_virtual_library(qtbot, monkeypatch, tmp_path):
    from ui import clip_grid
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    for index in range(220):
        (tmp_path / f'{index:03d}.mp4').touch()
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1100, 600)
    scroll.setWidgetResizable(True)
    grid = clip_grid.ClipGrid(settings)
    scroll.setWidget(grid)
    scroll.show()
    try:
        qtbot.waitUntil(lambda: grid._active_scan_worker is None)
        qtbot.waitUntil(lambda: not grid._resize_timer.isActive())
        paths = grid.navigation_paths()
        assert len(paths) == 220
        assert paths[-1] not in grid._thumb_widgets
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        qtbot.waitUntil(lambda: paths[-1] in grid._thumb_widgets)
        assert len(grid._thumb_widgets) <= clip_grid._MAX_MATERIALIZED_CARDS
        assert grid.adjacent_clip(paths[-1], 1) is None
        assert grid.adjacent_clip(paths[-1], -1) == paths[-2]
    finally:
        grid.shutdown()


def test_delete_restore_shortcuts_are_directional_and_export_now_does_not_cancel(qtbot):
    from ui.clip_viewer import ClipViewer, TrimSlider
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.set_playhead(0.4)
    timeline.split_at_playhead()
    button = QPushButton()
    qtbot.addWidget(button)
    calls = []
    viewer = SimpleNamespace(
        trim_slider=timeline, delete_segment_btn=button,
        _toggle_selected_segment=lambda: (calls.append('toggle'), timeline.toggle_selected_deleted()),
        _export_clip=lambda: calls.append('export'),
    )
    ClipViewer._set_selected_segment_deleted(viewer, True)
    ClipViewer._set_selected_segment_deleted(viewer, True)
    assert timeline.segments[1].deleted
    ClipViewer._set_selected_segment_deleted(viewer, False)
    ClipViewer._set_selected_segment_deleted(viewer, False)
    assert not timeline.segments[1].deleted
    assert calls == ['toggle', 'toggle']
    ClipViewer._export_now(viewer)
    assert calls[-1] == 'export'


def test_key_names_normalize_enter_escape_and_modifier_order():
    assert normalize_binding('shift+alt+r') == 'Alt+Shift+R'
    assert normalize_binding('Shift+Enter') == 'Shift+Return'
    assert normalize_binding('Esc') == 'Escape'
    with pytest.raises(ValueError):
        normalize_binding('Ctrl')
