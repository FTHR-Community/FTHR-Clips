"""Native key delivery, dormant editors, sidebar switching and teardown."""

import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from core.clip_metadata_manager import ClipMetadataManager
from core.navigation_bindings import NavigationBindings
from core.settings_manager import SettingsManager
from ui.clip_viewer import ClipViewer


@pytest.fixture
def editor_shell(qtbot, monkeypatch, tmp_path):
    """Use real editor/media widgets with capture and media preparation isolated."""
    from main import MainWindow
    from ui import clip_grid
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *_: (10, 1920, 1080, 30, 1_000_000, 1_000_000))
    for method in ('_set_media_source', '_prepare_playback_source',
                   '_discover_audio_sources_async', '_load_timeline_thumbnails_if_idle'):
        monkeypatch.setattr(ClipViewer, method, lambda *_: None)
    root = QMainWindow()
    qtbot.addWidget(root)
    root.resize(1100, 720)
    root.settings_manager = SettingsManager()
    root.clip_metadata_manager = ClipMetadataManager()
    root._active_clip_viewer = root._active_editor_session = None
    root._editor_sessions = []
    root._shutdown_requested = root._background_ui_paused = False
    root.bridge = None
    root._clip_readiness = SimpleNamespace(can_access=lambda _: True)
    root.upload_manager = SimpleNamespace(is_enabled=lambda: False, enqueue_upload=lambda *_: None)
    root.push_error = lambda *_args, **_kwargs: None
    root._restart_capture_engine = lambda: None
    root._settings_page_widget = SimpleNamespace(
        navigate_category=lambda *_: None, set_background_ui_paused=lambda *_: None)
    paths = [str(tmp_path / f'{name}.mp4') for name in ('first', 'second', 'third', 'fourth')]
    for path in paths:
        Path(path).touch()
    root.paths = paths
    root.main_stack = QStackedWidget()
    root.main_stack.addWidget(QWidget())
    settings = QWidget()
    settings_layout = QVBoxLayout(settings)
    root.settings_items = [QPushButton('First setting'), QPushButton('Second setting')]
    for button in root.settings_items:
        settings_layout.addWidget(button)
    root.main_stack.addWidget(settings)
    root.setCentralWidget(root.main_stack)
    root.nav_panel = QWidget(root.main_stack.widget(0))
    library_layout = QVBoxLayout(root.main_stack.widget(0))
    library_layout.addWidget(root.nav_panel)
    root.clip_scroll_area = QScrollArea()
    tall_library = QWidget()
    tall_library.setMinimumHeight(3000)
    root.clip_scroll_area.setWidget(tall_library)
    library_layout.addWidget(root.clip_scroll_area)
    root._sidebar_layout = QVBoxLayout(root.nav_panel)
    root.close_settings_btn = QToolButton()
    root.settings_gear_btn = QToolButton()
    root._navigation_group = QButtonGroup(root)
    for index, button in enumerate((root.close_settings_btn, root.settings_gear_btn)):
        button.setText(('Clips', 'Settings')[index])
        button.setCheckable(True)
        root._navigation_group.addButton(button, index)
        root._sidebar_layout.addWidget(button)
    root._sidebar_layout.addStretch()
    root.clip_grid = SimpleNamespace(
        is_linked_import=lambda _: False, set_background_paused=lambda _: None,
        adjacent_clip=lambda path, step: paths[paths.index(path) + step]
        if 0 <= paths.index(path) + step < len(paths) else None,
        thumbnails=[], navigation_paths=lambda: [],
    )
    for name in ('cap_settings_popup', 'source_popup', 'hotkey_popup', 'game_detection_popup'):
        setattr(root, name, QWidget(root))
    root.game_detection_popup.detection_switch = SimpleNamespace(toggle=lambda: None)
    for name in (
        '_on_clip_opened', '_show_editor_session', '_editor_finished', '_navigate_editor_clip',
        '_add_editor_session', '_remove_editor_session', '_close_editor_session',
        '_editor_session_menu', '_refresh_editor_sidebar', '_park_active_editor',
        '_minimize_active_editor', '_restore_editor_session', '_navigate_sidebar',
        '_set_main_page', '_toggle_settings_page', '_setup_keyboard_navigation',
        '_scroll_clip_library', '_restart_engine_shortcut',
        '_cancel_editor_transition', '_editor_transition_finished',
    ):
        setattr(root, name, MethodType(getattr(MainWindow, name), root))
    root._setup_keyboard_navigation()
    root.close_settings_btn.clicked.connect(lambda: root._set_main_page(0))
    root.settings_gear_btn.clicked.connect(lambda: root._set_main_page(1))
    root.show()
    yield root
    root._shutdown_requested = True
    if root._active_clip_viewer is not None:
        root._active_clip_viewer._close()
    for session in list(root._editor_sessions):
        root._remove_editor_session(session)


def _open(root, index=0):
    root._on_clip_opened(root.paths[index], QPixmap(), QRect())
    return root._active_clip_viewer


def test_reduced_motion_switch_and_restore_preserve_editor_state(editor_shell, monkeypatch):
    from ui.motion import set_reduce_motion
    root = editor_shell
    root.settings_manager.set('reduce_ui_animations', True)
    set_reduce_motion(True)
    try:
        viewer = _open(root)
        viewer.showNormal()
        viewer.resize(1000, 650)
        viewer.trim_slider.set_playhead(.45)
        assert viewer.trim_slider.split_at_playhead()
        snapshot = viewer._capture_editor_state()
        monkeypatch.setattr(viewer, 'grab', lambda *_:
                            pytest.fail('editor snapshot with reduced motion'))
        root._minimize_active_editor(viewer)
        assert getattr(root, '_editor_flight', None) is None
        session = root._editor_sessions[0]
        root._restore_editor_session(session)
        assert root._active_clip_viewer is viewer
        assert not viewer.isMaximized()
        assert viewer.size() == QSize(1000, 650)
        assert viewer.windowOpacity() == 1.0
        assert viewer._capture_editor_state() == snapshot
        root._navigate_editor_clip(viewer, 1)
        assert viewer.clip_path == root.paths[1]
        assert viewer._clip_slide is None
        assert viewer.clip_page is not None
    finally:
        set_reduce_motion(False)


def _native_chord(viewer, key, modifier=Qt.KeyboardModifier.NoModifier, tab=False):
    if getattr(viewer.parentWidget(), '_editor_flight', None) is not None:
        QTest.qWait(260)
    viewer.activateWindow()
    QApplication.processEvents()
    window = viewer.windowHandle()
    if tab:
        QTest.keyPress(window, Qt.Key.Key_Tab)
    QTest.keyClick(window, key, modifier)
    if tab:
        # The action may destroy the editor before the physical Tab release;
        # the OS sends that release to the newly active window.
        active = QApplication.activeWindow()
        QTest.keyRelease(window if isValid(window) else active.windowHandle(), Qt.Key.Key_Tab)


def test_native_tab_arrows_replace_the_clip_without_hiding_or_recreating_the_window(editor_shell):
    root = editor_shell
    first = _open(root)
    old_page = first.clip_page
    old_token = old_page._prepare_cancel
    QApplication.processEvents()
    handle = int(first.winId())
    transitions = []

    class Observer(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.Hide, QEvent.Type.Show):
                transitions.append(event.type())
            return False

    observer = Observer(first)
    first.installEventFilter(observer)
    _native_chord(first, Qt.Key.Key_Right, tab=True)
    assert old_token.is_set()
    second = root._active_clip_viewer
    assert second is first
    assert int(second.winId()) == handle
    assert second.clip_path == root.paths[1]
    assert second.clip_page is old_page
    assert second._play_when_ready
    assert second._clip_slide is not None
    assert second._clip_slide._horizontal
    assert second._clip_slide._direction == 1
    assert len(root._editor_sessions) == 1
    _native_chord(second, Qt.Key.Key_Left, tab=True)
    # A second keypress in the same burst opens immediately, without another
    # full-window capture or restarting a slide over the newest destination.
    assert second._clip_slide is None
    assert not transitions
    assert root._active_clip_viewer is first
    assert int(first.winId()) == handle
    assert root._active_clip_viewer.clip_path == root.paths[0]
    final = root._active_clip_viewer
    _native_chord(final, Qt.Key.Key_Escape)
    assert final._teardown_complete
    assert root._active_clip_viewer is None
    assert not root._editor_sessions


def test_minimize_button_releases_resources_and_restores_autoplay_edit(editor_shell, qtbot):
    root = editor_shell
    viewer = _open(root)
    slider = viewer.trim_slider
    slider.set_playhead(0.4)
    viewer._split_at_playhead()
    viewer._on_seek_requested(0.65)
    slider.set_zoom(4)
    undo = tuple(viewer._undo_stack)
    snapshot = viewer._capture_editor_state()
    viewer._source_volumes['game'] = 37
    viewer._source_mutes['game'] = True
    viewer.minimize_btn.click()
    assert root._active_clip_viewer is None
    flight = root._editor_flight
    assert flight is not None and flight._compact
    assert viewer._teardown_complete and viewer._prepare_cancel.is_set()
    assert not viewer.isVisible() and not viewer.isMinimized()
    assert viewer.player.source().isEmpty()
    assert viewer.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert viewer._audio_mixer is None
    assert viewer._idle_memory_timer.isActive()
    assert not any(timer.isActive() for timer in viewer.findChildren(QTimer)
                   if timer is not viewer._idle_memory_timer)
    session = root._editor_sessions[0]
    assert root._sidebar_layout.indexOf(session.button) == 2
    qtbot.waitUntil(lambda: session.button.isVisible())
    assert session.state['snapshot'] == snapshot
    assert session.state['position_ms'] == 6500
    # Restoring during the minimize flight must cancel its pending completion.
    session.button.click()
    assert not flight.isVisible()
    restored = root._active_clip_viewer
    assert restored is viewer
    assert session.state is None
    assert restored._capture_editor_state() == snapshot
    assert tuple(restored._undo_stack) == undo
    assert restored.trim_slider.zoom_factor == 4
    assert restored.trim_slider.playhead_pct == 0.65
    assert restored._renderer_resume_position_ms == 6500
    assert restored._play_when_ready
    assert restored._source_volumes['game'] == 37
    assert restored._source_mutes['game']
    restored._undo()
    assert len(restored.trim_slider.segments) == 1


@pytest.mark.parametrize('maximized', [False, True])
def test_restore_finishes_at_original_editor_size_without_second_resize(
        editor_shell, qtbot, maximized):
    root = editor_shell
    viewer = _open(root)
    qtbot.wait(50)  # settle deferred native frame styling before changing window state
    if not maximized:
        viewer.showNormal()
        qtbot.waitUntil(lambda: not viewer.isMaximized())
        viewer.setGeometry(40, 50, 1000, 650)
    elif QApplication.platformName() == 'offscreen':
        # The offscreen screen is only 800 px wide. Remove the explicit
        # desktop minimum in this geometry fixture so maximize can fit it.
        viewer.clip_page.setMinimumWidth(0)
        viewer.setMinimumWidth(0)
    QApplication.processEvents()
    original = QRect(viewer.mapToGlobal(viewer.rect().topLeft()), viewer.size())
    viewer.minimize_btn.click()
    session = root._editor_sessions[0]
    assert session.window_geometry == original
    session.button.click()
    restored = root._active_clip_viewer
    qtbot.waitUntil(lambda: getattr(root, '_editor_flight', None) is not None)
    flight = root._editor_flight
    destination = flight._animation.endValue().translated(flight.geometry().topLeft())
    assert destination == original
    resolved = QRect(restored.mapToGlobal(restored.rect().topLeft()), restored.size())
    assert resolved == destination
    qtbot.waitUntil(lambda: restored.windowOpacity() == 1.0)
    assert QRect(restored.mapToGlobal(restored.rect().topLeft()), restored.size()) == resolved
    assert restored.isMaximized() == maximized


def test_minimize_defers_library_previews_until_flight_finishes(editor_shell, qtbot):
    root = editor_shell
    viewer = _open(root)
    pauses = []
    root.clip_grid.set_background_paused = pauses.append
    viewer.minimize_btn.click()
    assert root._editor_flight is not None
    assert pauses == []
    qtbot.waitUntil(lambda: root._editor_flight is None)
    assert pauses == [False]
    assert root._active_clip_viewer is None
    assert len(root._editor_sessions) == 1


def test_timeline_display_option_persists_across_editor_restore(editor_shell):
    root = editor_shell
    viewer = _open(root)
    menu = viewer.trim_slider._context_menu_for_pct(.4)
    hide = next(action for action in menu.actions() if action.text() == 'Hide deleted segments')
    hide.trigger()
    assert viewer.trim_slider.hide_deleted_segments
    assert root.settings_manager.get('hide_deleted_timeline_segments')
    viewer.minimize_btn.click()
    root._editor_sessions[0].button.click()
    assert root._active_clip_viewer.trim_slider.hide_deleted_segments


def test_minimize_during_export_keeps_editor_and_skips_flight(editor_shell, monkeypatch):
    root = editor_shell
    viewer = _open(root)
    notices = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *_: notices.append(True))
    viewer._export_thread = SimpleNamespace(is_alive=lambda: True)
    try:
        viewer.minimize_btn.click()
        assert notices == [True]
        assert root._active_clip_viewer is viewer
        assert viewer.isVisible()
        assert not viewer._teardown_complete
        assert getattr(root, '_editor_flight', None) is None
    finally:
        viewer._export_thread = None


def test_held_tab_switches_several_clips_in_the_same_window(editor_shell):
    root = editor_shell
    viewer = _open(root)
    QApplication.processEvents()
    QTest.keyPress(viewer.windowHandle(), Qt.Key.Key_Tab)
    for key, expected in ((Qt.Key.Key_Right, 1), (Qt.Key.Key_Right, 2), (Qt.Key.Key_Left, 1)):
        current = root._active_clip_viewer
        QTest.keyClick(current.windowHandle(), key)
        assert root._active_clip_viewer is viewer
        assert root._active_clip_viewer.clip_path == root.paths[expected]
    current = root._active_clip_viewer
    QTest.keyRelease(current.windowHandle(), Qt.Key.Key_Tab)
    QTest.keyClick(current.windowHandle(), Qt.Key.Key_Left)
    assert root._active_clip_viewer is current


@pytest.mark.parametrize('target', ['page', 'minimize', 'video'])
def test_space_after_sidebar_restore_only_toggles_playback(editor_shell, target):
    root = editor_shell
    viewer = _open(root)
    viewer.minimize_btn.click()
    root._editor_sessions[0].button.click()
    restored = root._active_clip_viewer
    focus = {'page': restored.clip_page, 'minimize': restored.minimize_btn,
             'video': restored._native_video_widget}[target]
    focus.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    focus.setFocus()
    assert restored._play_when_ready
    _native_chord(restored, Qt.Key.Key_Space)
    assert root._active_clip_viewer is restored
    assert not restored._teardown_complete
    assert not restored._play_when_ready


def test_shift_escape_minimizes_while_editing_metadata(editor_shell):
    from PySide6.QtWidgets import QLineEdit
    root = editor_shell
    viewer = _open(root)
    edit = viewer.findChild(QLineEdit, 'detailEdit')
    edit.parentWidget().show()
    viewer._sidebar_scroll.ensureWidgetVisible(edit)
    edit.setFocus()
    QApplication.processEvents()
    assert edit.hasFocus()
    _native_chord(viewer, Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier)
    assert root._active_clip_viewer is None
    assert len(root._editor_sessions) == 1


def test_ctrl_c_splits_from_the_playback_control(editor_shell):
    viewer = _open(editor_shell)
    viewer.trim_slider.set_playhead(0.4)
    viewer.play_btn.setFocus()
    _native_chord(viewer, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert len(viewer.trim_slider.segments) == 2
    assert viewer.trim_slider.segments[0].end_pct == pytest.approx(0.4)


def test_split_default_migration_preserves_custom_binding(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr/settings.json'
    config.parent.mkdir()
    for previous, expected in [('Ctrl+S', 'Ctrl+C'), ('Alt+C', 'Alt+C')]:
        config.write_text(json.dumps({'navigation_bindings_version': 4,
                                     'navigation_bindings': {'editor_split': previous}}))
        assert NavigationBindings(SettingsManager()).get('editor_split') == expected
        assert json.loads(config.read_text())['navigation_bindings_version'] == 5


def test_three_editors_can_be_parked_and_fourth_needs_a_free_slot(editor_shell, monkeypatch):
    root = editor_shell
    for index in range(3):
        viewer = _open(root, index)
        _native_chord(viewer, Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier)
        assert root._active_clip_viewer is None
    assert len(root._editor_sessions) == 3
    assert all(session.state is not None for session in root._editor_sessions)
    messages = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: messages.append(args[1]))
    _open(root, 3)
    assert root._active_clip_viewer is None
    assert len(root._editor_sessions) == 3
    assert messages == ['Three editors are open']
    root._close_editor_session(root._editor_sessions[1])
    _open(root, 3)
    assert root._active_clip_viewer.clip_path == root.paths[3]
    assert len(root._editor_sessions) == 3


def test_capacity_denial_preserves_active_editor_and_its_export_state(
        editor_shell, monkeypatch):
    """A rejected fourth editor must not tear down the active third editor."""
    root = editor_shell
    for index in range(2):
        viewer = _open(root, index)
        _native_chord(viewer, Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier)
        assert root._active_clip_viewer is None

    active = _open(root, 2)
    active_state = active._capture_editor_state()
    export_cancel = active._export_cancel
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: None)

    _open(root, 3)

    assert root._active_clip_viewer is active
    assert active.clip_path == root.paths[2]
    assert not export_cancel.is_set()
    assert active._capture_editor_state() == active_state
    assert len(root._editor_sessions) == 3


def test_sidebar_shortcuts_cycle_pages_and_editors_and_tab_s_works_in_editor(editor_shell):
    root = editor_shell
    first = _open(root, 0)
    first.minimize_btn.click()
    second = _open(root, 1)
    second.minimize_btn.click()
    _native_chord(root, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert root.main_stack.currentIndex() == 1
    _native_chord(root, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    editor = root._active_clip_viewer
    assert editor.clip_path == root.paths[0]
    token = editor._prepare_cancel
    _native_chord(editor, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert token.is_set()
    editor = root._active_clip_viewer
    assert editor.clip_path == root.paths[1]
    _native_chord(editor, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert root._active_clip_viewer.clip_path == root.paths[0]
    _native_chord(root._active_clip_viewer, Qt.Key.Key_S, tab=True)
    assert root._active_clip_viewer is None
    assert root.main_stack.currentIndex() == 1
    _native_chord(root, Qt.Key.Key_S, tab=True)
    assert root.main_stack.currentIndex() == 0
    assert len(root._editor_sessions) == 2


def test_settings_blocks_item_keys_but_mouse_controls_still_work(editor_shell):
    root = editor_shell
    root._set_main_page(1)
    first, second = root.settings_items
    clicks = []
    first.clicked.connect(lambda: clicks.append('first'))
    first.setFocus()
    QApplication.processEvents()
    for key, modifiers in (
        (Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier),
    ):
        QTest.keyClick(root.windowHandle(), key, modifiers)
        assert first.hasFocus()
    assert not clicks and not second.hasFocus()
    QTest.mouseClick(first, Qt.MouseButton.LeftButton)
    assert clicks == ['first']


def test_no_timeline_keyboard_targets_or_shortcuts_remain(editor_shell):
    viewer = _open(editor_shell)
    assert not hasattr(viewer, 'timeline_keyboard_hint')
    assert not hasattr(viewer.trim_slider, 'keyboard_target')
    assert viewer.trim_slider.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert not any(key.startswith('timeline_') for key in viewer._navigation_bindings.values)


def test_old_shortcuts_migrate_and_removed_timeline_bindings_are_pruned(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr/settings.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'navigation_bindings': {
        'editor_next': 'Right', 'editor_previous': 'Alt+Left',
        'timeline_next': 'Alt+T', 'editor_split': 'Alt+S',
    }}))
    settings = SettingsManager()
    bindings = NavigationBindings(settings)
    assert bindings.get('editor_next') == 'Tab+Right'
    assert bindings.get('editor_previous') == 'Alt+Left'
    assert bindings.get('editor_split') == 'Alt+S'
    assert bindings.get('sidebar_next') == 'Shift+Down'
    saved = json.loads(config.read_text())
    assert saved['navigation_bindings_version'] == 5
    assert not any(key.startswith('timeline_') for key in saved['navigation_bindings'])
    assert SettingsManager().get('navigation_bindings') == bindings.values


def test_scroll_arrows_repeat_but_shift_arrows_switch_the_sidebar(editor_shell):
    root = editor_shell
    QApplication.processEvents()
    bar = root.clip_scroll_area.verticalScrollBar()
    bar.setValue(200)
    root.close_settings_btn.setFocus()
    QTest.keyPress(root.windowHandle(), Qt.Key.Key_Down)
    assert bar.value() > 200
    after_press = bar.value()
    QApplication.sendEvent(root.close_settings_btn, QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier, '', True))
    assert bar.value() > after_press
    QTest.keyRelease(root.windowHandle(), Qt.Key.Key_Down)
    QTest.keyClick(root.windowHandle(), Qt.Key.Key_Up)
    assert bar.value() == after_press
    QTest.keyClick(root.windowHandle(), Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert root.main_stack.currentIndex() == 1
    assert bar.value() == after_press


def test_new_clip_autoplays_once_media_and_audio_are_ready(editor_shell, monkeypatch, qtbot):
    viewer = _open(editor_shell)
    page = viewer.clip_page
    assert page._play_when_ready
    calls = []
    monkeypatch.setattr(page, '_toggle_play', lambda: calls.append('play'))
    page._media_ready = True
    page._audio_preparation_ready = False
    page._update_playback_readiness()
    QApplication.processEvents()
    assert not calls
    page._audio_preparation_ready = True
    page._update_playback_readiness()
    qtbot.waitUntil(lambda: calls == ['play'])
    page._update_playback_readiness()
    QApplication.processEvents()
    assert calls == ['play']


def test_old_default_escape_pair_is_swapped_and_item_bindings_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr/settings.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'navigation_bindings_version': 3, 'navigation_bindings': {
        'editor_minimize': 'Escape', 'editor_close': 'Shift+Escape',
        'item_next': 'Right', 'item_tab': 'Tab', 'editor_export': 'Alt+E',
    }}))
    settings = SettingsManager()
    values = settings.get('navigation_bindings')
    assert values['editor_close'] == 'Escape'
    assert values['editor_minimize'] == 'Shift+Escape'
    assert values['editor_export'] == 'Alt+E'
    assert not any(action.startswith('item_') for action in values)
    assert json.loads(config.read_text())['navigation_bindings_version'] == 5


def test_custom_escape_bindings_survive_migration(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr/settings.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'navigation_bindings_version': 3, 'navigation_bindings': {
        'editor_minimize': 'Alt+M', 'editor_close': 'Ctrl+Escape',
    }}))
    values = SettingsManager().get('navigation_bindings')
    assert values['editor_minimize'] == 'Alt+M'
    assert values['editor_close'] == 'Ctrl+Escape'


def test_switching_clips_cancels_retired_page_feedback_callbacks(editor_shell, qtbot):
    viewer = _open(editor_shell)
    old_page = viewer.clip_page
    old_page._split_at_playhead()  # at the start: a temporary button label
    assert old_page.split_btn.text() != 'SPLIT'
    editor_shell._navigate_editor_clip(viewer, 1)
    qtbot.wait(1250)
    assert isValid(old_page) and old_page is editor_shell._active_clip_viewer.clip_page
    assert editor_shell._active_clip_viewer is viewer
    assert viewer.clip_path == editor_shell.paths[1]


def test_switching_to_a_parked_clip_reuses_its_saved_edit(editor_shell):
    root = editor_shell
    first = _open(root, 1)
    first.trim_slider.set_playhead(0.4)
    first._split_at_playhead()
    snapshot = first._capture_editor_state()
    first.minimize_btn.click()
    second = _open(root, 0)
    window_id = int(second.winId())
    root._navigate_editor_clip(second, 1)
    assert root._active_clip_viewer is second
    assert int(second.winId()) == window_id
    assert len(root._editor_sessions) == 1
    assert second._capture_editor_state() == snapshot
    assert second._play_when_ready
    second._close()
    assert not root._editor_sessions


def test_embedded_editor_fullscreen_targets_persistent_outer_window(editor_shell):
    editor = _open(editor_shell)
    page = editor.clip_page
    page._toggle_fullscreen()
    assert editor.isFullScreen()
    assert not page.isWindow()
    page._exit_fullscreen()
    assert not editor.isFullScreen()
    page._toggle_fullscreen()
    page._handle_escape_shortcut()
    assert not editor.isFullScreen()
    assert editor_shell._active_clip_viewer is editor


def test_clip_replacement_reuses_widgets_and_releases_previous_media(editor_shell, monkeypatch):
    viewer = _open(editor_shell)
    page, player = viewer.clip_page, viewer.player
    token = page._prepare_cancel
    widgets = (page.trim_slider, page.video_widget, page._native_video_widget, page.play_btn)
    editor_shell._navigate_editor_clip(viewer, 1)
    assert viewer.clip_page is page
    assert token.is_set() and player.source().isEmpty()
    assert viewer.player is not player
    assert widgets == (page.trim_slider, page.video_widget, page._native_video_widget, page.play_btn)
    assert viewer.clip_path == editor_shell.paths[1]


def test_failed_clip_replacement_restores_edits_and_undo(editor_shell, monkeypatch, qtbot):
    viewer = _open(editor_shell)
    viewer.trim_slider.set_playhead(.4)
    viewer._split_at_playhead()
    snapshot = viewer._capture_editor_state()
    undo = tuple(viewer._undo_stack)
    original = viewer.clip_page.load_clip
    failed_tokens = []

    def load(path, **kwargs):
        original(path, **kwargs)
        if path == editor_shell.paths[1]:
            failed_tokens.append(viewer._prepare_cancel)
            raise RuntimeError('replacement failed')

    monkeypatch.setattr(viewer.clip_page, 'load_clip', load)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *_: None)
    editor_shell._navigate_editor_clip(viewer, 1)
    assert viewer.clip_path == editor_shell.paths[0]
    assert viewer._capture_editor_state() == snapshot
    assert tuple(viewer._undo_stack) == undo
    assert len(editor_shell._editor_sessions) == 1
    assert failed_tokens[0].is_set()


def test_close_during_export_keeps_owner_alive_without_blocking(editor_shell, qtbot):
    import threading
    viewer = _open(editor_shell)
    page = viewer.clip_page
    release = threading.Event()
    cancellation = threading.Event()
    page._export_job = SimpleNamespace(
        request_cancel=cancellation.set,
        cancel=lambda: (_ for _ in ()).throw(AssertionError('blocking cancel')))
    page._export_thread = threading.Thread(target=lambda: release.wait(3), daemon=True)
    page._export_thread.start()
    try:
        viewer._close()
        assert cancellation.is_set() and not viewer.isVisible()
        assert isValid(viewer) and isValid(page)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
        assert editor_shell._active_clip_viewer is viewer
        assert page._export_thread.is_alive()
    finally:
        release.set()
    qtbot.waitUntil(lambda: editor_shell._active_clip_viewer is None)
    qtbot.waitUntil(lambda: not isValid(viewer))
