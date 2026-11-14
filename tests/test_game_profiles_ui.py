"""Focused behavioural checks for the passive game-profile editor."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path


from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QPalette
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QPushButton, QStyleFactory, QWidget
from PySide6.QtTest import QTest

from core.camera_overlay import DEFAULT_OVERLAY_RECT
from core.game_profiles import create_profile, empty_catalog, update_profile
from core.settings_manager import SettingsManager
from ui.app_style import apply_app_style
from ui.game_profiles import GameProfilesWidget
from ui.style import Colors, Fonts


def _manager(tmp_path, **settings):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = dict(settings)
    return manager


def _saved_manager(tmp_path, *, overrides=None, **settings):
    manager = _manager(tmp_path, **settings)
    catalog = create_profile(
        empty_catalog(), 'Game', r'C:\Games\Game.exe', overrides or {})
    assert manager.save_game_capture_profiles(catalog, expected_revision=0)
    return manager


def _saved_two_profile_manager(tmp_path):
    manager = _manager(tmp_path)
    first = create_profile(empty_catalog(), 'First', r'C:\Games\First.exe')
    assert manager.save_game_capture_profiles(first, expected_revision=0)
    second = create_profile(first, 'Second', r'C:\Games\Second.exe')
    assert manager.save_game_capture_profiles(second, expected_revision=1)
    return manager


def test_widget_exposes_passive_api_and_all_21_leaves(qapp, tmp_path):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)

    assert len(widget._bindings) == 21
    assert widget.profile_list.count() == 1
    assert widget._flow.currentWidget() is widget._library_page
    assert widget._catalog.profiles[0].exe_path == r'c:\games\game.exe'

    status = []
    widget.apply_requested.connect(lambda: status.append('apply'))
    widget.profile_list.setCurrentRow(0)
    assert status == []
    widget._open_profile()
    widget.save_button.click()
    assert status == ['apply']


def test_running_app_creates_prefilled_draft_and_saves_exact_path(qapp, tmp_path):
    manager = _manager(tmp_path)
    widget = GameProfilesWidget(manager)
    requests = []
    widget.refresh_windows_requested.connect(lambda: requests.append(True))
    widget.refresh_windows_requested.emit()
    assert requests == [True]
    records = [{'title': 'Game window', 'exe_path': r'C:\Games\Game.exe'},
               {'title': 'No executable'}]
    widget.set_available_windows(records)
    records[0]['exe_path'] = r'C:\Other.exe'
    assert widget.running_windows_combo.count() == 1
    widget._new_running_profile()
    assert widget._name_edit.text() == 'Game'
    assert widget._exe_edit.text() == r'C:\Games\Game.exe'
    assert not widget._catalog.profiles
    assert widget.save_draft()
    assert widget._catalog.profiles[0].exe_path == r'c:\games\game.exe'


def test_running_app_refresh_preserves_selection_and_cancel_keeps_draft(
        qapp, tmp_path, monkeypatch):
    widget = GameProfilesWidget(_saved_manager(tmp_path))
    records = [{'exe_path': r'C:\One.exe'}, {'exe_path': r'C:\Two.exe'}]
    widget.set_available_windows(records)
    widget.running_windows_combo.setCurrentIndex(1)
    widget.set_available_windows(list(reversed(records)))
    assert widget.running_windows_combo.currentData() == r'C:\Two.exe'
    widget._name_edit.setText('Unsaved name')
    original_id = widget._draft['id']
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)
    widget._new_running_profile()
    assert widget._draft['id'] == original_id
    assert widget._name_edit.text() == 'Unsaved name'
    widget.set_available_windows([])
    widget._source_tabs.setCurrentIndex(1)
    assert not widget.continue_button.isEnabled()


def test_profile_flow_has_only_add_on_landing_and_save_discard_in_editor(
        qapp, qtbot, tmp_path):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    qtbot.addWidget(widget)
    widget.resize(900, 720)
    widget.show()
    assert [b.text() for b in widget.findChildren(QPushButton) if b.isVisible()] == ['Add Profile']
    widget.new_button.click()
    assert widget._flow.currentWidget() is widget._app_page
    assert not widget.continue_button.isEnabled()
    widget.manual_path_edit.setText(r'C:\Games\Second.exe')
    widget.continue_button.click()
    assert widget._flow.currentWidget() is widget._editor_page
    assert widget._name_edit.text() == 'Second'
    assert len(manager.read_game_capture_profiles().profiles) == 1
    assert widget.save_button.isVisible() and widget.discard_button.isVisible()
    for index in range(4):
        widget._section_tabs.setCurrentIndex(index)
        assert [s.isVisible() for s in widget._sections] == [i == index for i in range(4)]
    widget._bindings['framerate'].toggle.setChecked(True)
    widget._bindings['framerate'].editor.setValue(144)
    applied = []
    widget.apply_requested.connect(lambda: applied.append(True))
    widget.save_button.click()
    assert applied == [True]
    assert widget._flow.currentWidget() is widget._library_page
    saved = manager.read_game_capture_profiles().profiles[-1]
    assert saved.overrides['clips']['framerate'] == 144
    item = widget.profile_list.item(widget.profile_list.count() - 1)
    qtbot.mouseClick(widget.profile_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=widget.profile_list.visualItemRect(item).center())
    assert widget._flow.currentWidget() is widget._editor_page
    widget._name_edit.setText('Discard this edit')
    widget.discard_button.click()
    assert widget._flow.currentWidget() is widget._library_page
    assert manager.read_game_capture_profiles().profiles[-1].name == 'Second'
    assert applied == [True]


def test_picker_running_app_validation_duplicates_and_discard(qapp, tmp_path):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget.new_button.click()
    widget.manual_path_edit.setText('relative.exe')
    widget.continue_button.click()
    assert widget._flow.currentWidget() is widget._app_page
    assert 'full path' in widget._state_label.text()
    requests = []
    widget.refresh_windows_requested.connect(lambda: requests.append(True))
    widget._source_tabs.setCurrentIndex(1)
    if __import__('sys').platform == 'win32':
        assert requests == [True]
    widget.set_available_windows([{'exe_path': r'C:\Games\New.exe'}])
    widget._continue_add_profile()
    assert widget._draft_is_new
    assert widget._exe_edit.text() == r'C:\Games\New.exe'
    widget.discard_button.click()
    assert len(manager.read_game_capture_profiles().profiles) == 1
    widget.new_button.click()
    widget.manual_path_edit.setText(r'C:\GAMES\GAME.EXE')
    widget.continue_button.click()
    assert not widget._draft_is_new
    assert widget._flow.currentWidget() is widget._editor_page
    assert 'already has a profile' in widget._state_label.text()


def test_save_failure_keeps_editor_and_never_applies(qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._open_profile()
    widget._name_edit.setText('Keep my draft')
    applied = []
    widget.apply_requested.connect(lambda: applied.append(True))
    monkeypatch.setattr(manager, 'save_game_capture_profiles', lambda *a, **k: False)
    widget.save_button.click()
    assert widget._flow.currentWidget() is widget._editor_page
    assert widget._name_edit.text() == 'Keep my draft'
    assert widget._dirty
    assert not widget.reload_button.isHidden()
    assert applied == []


def test_context_menu_renames_and_deletes_selected_profile(qapp, qtbot, tmp_path, monkeypatch):
    manager = _saved_two_profile_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    qtbot.addWidget(widget)
    widget.resize(900, 720)
    widget.show()
    chosen = [0]
    def choose():
        menu = QApplication.activePopupWidget()
        assert [a.text() for a in menu.actions()] == ['Rename', 'Delete']
        menu.setActiveAction(menu.actions()[chosen[0]])
        QTest.keyClick(menu, Qt.Key.Key_Return)
    monkeypatch.setattr('ui.game_profiles.FthrInputDialog.get_text',
                        lambda *a, **k: ('Renamed second', True))
    second = widget.profile_list.item(1)
    QTimer.singleShot(20, choose)
    widget._profile_context_menu(widget.profile_list.visualItemRect(second).center())
    assert manager.read_game_capture_profiles().profiles[1].name == 'Renamed second'
    assert widget._flow.currentWidget() is widget._library_page
    chosen[0] = 1
    monkeypatch.setattr('ui.game_profiles.FthrMessageDialog.question', lambda *a, **k: True)
    second = widget.profile_list.item(1)
    QTimer.singleShot(20, choose)
    widget._profile_context_menu(widget.profile_list.visualItemRect(second).center())
    assert [p.name for p in manager.read_game_capture_profiles().profiles] == ['First']


def test_profile_editor_is_responsive_without_horizontal_content_overflow(
        qapp, qtbot, tmp_path):
    """Exercise the styled page at desktop and narrow editor widths."""
    old_style_name = qapp.style().objectName()
    old_font = QFont(qapp.font())
    old_palette = QPalette(qapp.palette())
    old_sheet = qapp.styleSheet()
    had_fusion_flag = hasattr(qapp, '_fthr_fusion_configured')
    old_fusion_flag = getattr(qapp, '_fthr_fusion_configured', None)
    had_hover_transitions = hasattr(qapp, '_hover_transitions')
    old_hover_transitions = getattr(qapp, '_hover_transitions', None)
    owned_hover_transitions = None
    font_ids = []
    widget = None

    def layout_ready(page):
        viewport = page._editor_scroll.viewport()
        if viewport.width() <= 0:
            return False
        if not all(
                row._editor.width() > 0
                and row._toggle.width() > 0
                and row._global_value.width() > 0
                for row in page._binding_rows.values()):
            return False
        for control in page._editor_content.findChildren(QWidget):
            if not control.isVisible() or control.width() <= 0:
                continue
            origin = control.mapTo(viewport, QPoint(0, 0))
            if origin.x() < 0 or origin.x() + control.width() > viewport.width():
                return False
        horizontal_bar = page._editor_scroll.horizontalScrollBar()
        return horizontal_bar.maximum() == 0 and not horizontal_bar.isVisible()

    def layout_diagnostics(page):
        viewport = page._editor_scroll.viewport()
        horizontal_bar = page._editor_scroll.horizontalScrollBar()
        vertical_bar = page._editor_scroll.verticalScrollBar()
        details = [
            f'widget={page.width()}x{page.height()}',
            f'viewport={viewport.width()}x{viewport.height()}',
            f'content={page._editor_content.width()}x{page._editor_content.height()}',
            f'hbar={horizontal_bar.maximum()}',
            f'vbar={vertical_bar.value()}/{vertical_bar.maximum()}',
        ]
        for key, row in page._binding_rows.items():
            details.append(
                f'{key}: row={row.width()}x{row.height()} '
                f'editor={row._editor.width()} toggle={row._toggle.width()} '
                f'global={row._global_value.width()} wide={row._wide}')
        for control in page._editor_content.findChildren(QWidget):
            if not control.isVisible() or control.width() <= 0:
                continue
            origin = control.mapTo(viewport, QPoint(0, 0))
            right = origin.x() + control.width()
            if origin.x() < 0 or right > viewport.width():
                label = control.objectName() or control.accessibleName() or control.__class__.__name__
                details.append(f'overflow={label} bounds={origin.x()}..{right}')
        camera = page._bindings['camera_overlay_rect'].editor
        camera_origin = camera.mapTo(viewport, QPoint(0, 0))
        spin_bounds = []
        for spin in camera.findChildren(QDoubleSpinBox):
            origin = spin.mapTo(camera, QPoint(0, 0))
            spin_bounds.append(
                f'{spin.objectName()}={origin.x()},{origin.y()},'
                f'{spin.width()}x{spin.height()}')
        details.append(
            f'camera={camera_origin.x()},{camera_origin.y()},'
            f'{camera.width()}x{camera.height()} '
            f'spins=[{";".join(spin_bounds)}]')
        return '; '.join(details)

    def wait_for_layout(page, width, height):
        try:
            qtbot.waitUntil(
                lambda: page.isVisible() and page.width() == width
                and page.height() == height and layout_ready(page),
                timeout=3000,
            )
        except Exception as exc:
            raise AssertionError(
                f'profile layout did not settle at {width}x{height}: '
                f'{layout_diagnostics(page)}') from exc

    def camera_fits_visible_viewport(page):
        viewport = page._editor_scroll.viewport()
        editor = page._bindings['camera_overlay_rect'].editor
        origin = editor.mapTo(viewport, QPoint(0, 0))
        if (origin.y() < 0 or origin.y() + editor.height() > viewport.height()
                or origin.x() < 0 or origin.x() + editor.width() > viewport.width()):
            return False
        spins = editor.findChildren(QDoubleSpinBox)
        if len(spins) != 4:
            return False
        return all(
            spin.isVisible() and spin.width() > 0 and spin.height() > 0
            and spin.mapTo(editor, QPoint(0, 0)).x() >= 0
            and spin.mapTo(editor, QPoint(0, 0)).x() + spin.width() <= editor.width()
            and spin.mapTo(editor, QPoint(0, 0)).y() >= 0
            and spin.mapTo(editor, QPoint(0, 0)).y() + spin.height() <= editor.height()
            for spin in spins
        )

    def settle_at(width, height):
        widget.resize(width, height)
        wait_for_layout(widget, width, height)
        assert layout_ready(widget)
        bar = widget._editor_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        try:
            qtbot.waitUntil(lambda: camera_fits_visible_viewport(widget), timeout=3000)
        except Exception as exc:
            raise AssertionError(
                f'camera rectangle did not reach the viewport at {width}x{height}: '
                f'{layout_diagnostics(widget)}') from exc

    try:
        font_dir = Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'assets' / 'fonts'
        for font_path in sorted(font_dir.glob('Oswald-*.ttf')):
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            assert font_id >= 0, f'could not register bundled font: {font_path}'
            font_ids.append(font_id)
        apply_app_style(qapp)
        assert qapp.palette().color(QPalette.ColorRole.Window) == QColor(Colors.BG)
        assert QFontInfo(qapp.font()).family() == Fonts.BODY_FAMILY
        assert qapp.font().pixelSize() == Fonts.SIZE_BODY

        manager = _saved_manager(
            tmp_path,
            overrides={'camera': {'camera_overlay_rect': {
                'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}}},
        )
        widget = GameProfilesWidget(manager)
        widget.set_audio_devices(
            [{'endpoint_id': 'mic-1', 'display_name': 'A very long microphone name that must stay in a tooltip',
              'available': True}],
            [{'endpoint_id': 'playback-1', 'display_name': 'A very long playback endpoint name that must stay in a tooltip',
              'available': True}],
        )
        widget.set_camera_devices([{
            'index': 0,
            'display_name': 'A very long camera name that must stay in a tooltip',
            'available': True,
        }])
        widget._open_profile()
        widget._section_tabs.setCurrentIndex(3)
        widget.show()
        settle_at(1100, 720)
        assert widget._editor_scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert not widget.profile_list.horizontalScrollBar().isVisible()
        settle_at(1004, 900)
        settle_at(1440, 900)
        settle_at(1004, 900)
    finally:
        if widget is not None:
            for control in (widget, *widget.findChildren(QWidget)):
                fade = control.__dict__.get('_hover_fade')
                if fade is not None:
                    fade.dispose()
        if had_hover_transitions:
            qapp._hover_transitions = old_hover_transitions
        else:
            hover_transitions = getattr(qapp, '_hover_transitions', None)
            if hover_transitions is not None and hover_transitions is not old_hover_transitions:
                qapp.removeEventFilter(hover_transitions)
                hover_transitions.deleteLater()
                owned_hover_transitions = hover_transitions
            if hasattr(qapp, '_hover_transitions'):
                del qapp._hover_transitions
        if old_style_name:
            qapp.setStyle(QStyleFactory.create(old_style_name))
        qapp.setFont(old_font)
        qapp.setPalette(old_palette)
        qapp.setStyleSheet(old_sheet)
        if had_fusion_flag:
            qapp._fthr_fusion_configured = old_fusion_flag
        elif hasattr(qapp, '_fthr_fusion_configured'):
            del qapp._fthr_fusion_configured
        if widget is not None:
            widget.close()
            widget.deleteLater()
        qapp.processEvents()
        if widget is not None:
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        if owned_hover_transitions is not None:
            QCoreApplication.sendPostedEvents(
                owned_hover_transitions, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        for font_id in font_ids:
            QFontDatabase.removeApplicationFont(font_id)


def test_sparse_override_saves_once_without_mutating_global_settings(qapp, tmp_path):
    manager = _saved_manager(tmp_path, framerate=60)
    widget = GameProfilesWidget(manager)

    binding = widget._bindings['framerate']
    binding.toggle.setChecked(True)
    binding.editor.setValue(144)
    assert manager.get('framerate') == 60
    assert widget.save_draft() is True
    saved = manager.read_game_capture_profiles().profiles[0]
    assert saved.overrides['clips']['framerate'] == 144
    assert manager.get('framerate') == 60


def test_actual_add_rename_delete_crud_keeps_stable_id_and_emits_revision(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path, framerate=60)
    widget = GameProfilesWidget(manager)
    revisions = []
    widget.catalog_changed.connect(revisions.append)
    monkeypatch.setattr(
        'ui.game_profiles.FthrInputDialog.get_text',
        lambda *_args, **_kwargs: ('Renamed', True),
    )
    original_id = widget._draft['id']
    widget._rename_profile()
    assert widget.save_draft() is True
    assert manager.read_game_capture_profiles().profiles[0].id == original_id
    assert manager.read_game_capture_profiles().profiles[0].name == 'Renamed'
    assert revisions

    widget._new_profile()
    widget._name_edit.setText('Second')
    widget._exe_edit.setText(r'C:\Games\Second.exe')
    assert widget.save_draft() is True
    assert len(manager.read_game_capture_profiles().profiles) == 2

    monkeypatch.setattr(
        'ui.game_profiles.FthrMessageDialog.question',
        lambda *_args, **_kwargs: True,
    )
    widget._delete_profile()
    assert len(manager.read_game_capture_profiles().profiles) == 1
    assert widget.save_draft() is True

    saved = manager.read_game_capture_profiles().profiles[0]
    assert saved.id == widget._draft['id']
    assert manager.get('framerate') == 60


def test_cas_failure_keeps_draft_and_error(qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    binding = widget._bindings['clip_length']
    binding.toggle.setChecked(True)
    binding.editor.setValue(90)

    monkeypatch.setattr(manager, 'save_game_capture_profiles', lambda *_args, **_kwargs: False)
    assert widget.save_draft() is False
    assert widget._dirty is True
    assert widget._collect_draft()['overrides']['clips']['clip_length'] == 90
    assert 'could not be saved' in widget._state_label.text()


def test_row_switch_save_preserves_target_uuid_and_reloads_target(qapp, tmp_path, monkeypatch):
    manager = _saved_two_profile_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    first_id = widget.profile_list.item(0).data(Qt.ItemDataRole.UserRole)
    second_id = widget.profile_list.item(1).data(Qt.ItemDataRole.UserRole)
    widget._name_edit.setText('First edited')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 1)

    widget.profile_list.setCurrentRow(1)

    saved = manager.read_game_capture_profiles()
    assert saved.profiles[0].name == 'First edited'
    assert widget._draft['id'] == second_id
    assert widget.profile_list.currentItem().data(Qt.ItemDataRole.UserRole) == second_id
    assert first_id != second_id


def test_row_switch_save_failure_restores_original_selection_and_dirty_draft(
        qapp, tmp_path, monkeypatch):
    manager = _saved_two_profile_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    first_id = widget.profile_list.item(0).data(Qt.ItemDataRole.UserRole)
    widget._name_edit.setText('Unsaved')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 1)
    monkeypatch.setattr(manager, 'save_game_capture_profiles', lambda *_args, **_kwargs: False)

    widget.profile_list.setCurrentRow(1)

    assert widget.profile_list.currentItem().data(Qt.ItemDataRole.UserRole) == first_id
    assert widget._name_edit.text() == 'Unsaved'
    assert widget._dirty is True


def test_row_switch_discard_restores_canonical_controls(qapp, tmp_path, monkeypatch):
    manager = _saved_two_profile_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 2)
    widget._name_edit.setText('Discard me')

    widget.profile_list.setCurrentRow(1)

    assert widget._draft['name'] == 'Second'
    assert widget._name_edit.text() == 'Second'
    assert widget._dirty is False


def test_delete_discard_then_cancel_keeps_canonical_editor_and_no_write(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._name_edit.setText('Discard me')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 2)
    monkeypatch.setattr(
        'ui.game_profiles.FthrMessageDialog.question',
        lambda *_args, **_kwargs: False,
    )

    widget._delete_profile()

    assert widget._name_edit.text() == 'Game'
    assert widget._dirty is False
    assert manager.read_game_capture_profiles().profiles[0].name == 'Game'


def test_new_profile_cancel_keeps_existing_draft(qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._name_edit.setText('Keep me')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)

    widget._new_profile()

    assert widget._name_edit.text() == 'Keep me'
    assert widget._draft['name'] == 'Game'
    assert widget._dirty is True


def test_new_profile_cancel_from_single_row_preserves_visible_new_draft(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._new_profile()
    widget._name_edit.setText('Unsaved new')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)

    widget.profile_list.setCurrentRow(0)

    assert widget.profile_list.currentRow() == -1
    assert widget._draft_is_new is True
    assert widget._name_edit.text() == 'Unsaved new'
    assert widget._dirty is True


def test_inherited_false_zero_default_and_global_refresh_preserve_draft(qapp, tmp_path):
    manager = _saved_manager(
        tmp_path,
        audio_capture_enabled=False,
        mic_volume=0,
        mic_device_id=None,
        clip_length=30,
        framerate=60,
    )
    widget = GameProfilesWidget(manager)
    assert not widget._bindings['audio_capture_enabled'].editor.isEnabled()
    assert 'Disabled' in widget._bindings['audio_capture_enabled'].global_value.text()
    assert '0' in widget._bindings['mic_volume'].global_value.text()
    assert 'System default' in widget._bindings['mic_device_id'].global_value.text()

    override = widget._bindings['framerate']
    override.toggle.setChecked(True)
    override.editor.setValue(144)
    manager.settings['clip_length'] = 90
    widget.refresh_global_values()

    inherited = widget._bindings['clip_length']
    assert inherited.editor.value() == 90
    assert not inherited.editor.isEnabled()
    assert override.editor.value() == 144
    assert widget._dirty is True


def test_clean_global_refresh_updates_editor_without_dirtying(qapp, tmp_path):
    manager = _saved_manager(tmp_path, clip_length=30)
    widget = GameProfilesWidget(manager)
    assert widget._dirty is False
    manager.settings['clip_length'] = 90

    widget.refresh_global_values()

    assert widget._bindings['clip_length'].editor.value() == 90
    assert widget._dirty is False
    assert widget.save_button.isEnabled()


def test_global_refresh_validates_each_leaf_once_and_does_not_mask_valid_values(
        qapp, qtbot, tmp_path, monkeypatch):
    import ui.game_profiles as profile_ui

    expected_leaves = (
        ('clips', 'clip_length'),
        ('clips', 'framerate'),
        ('clips', 'resolution'),
        ('clips', 'bitrate_level'),
        ('clips', 'custom_bitrate_kbps'),
        ('recording', 'recording_framerate'),
        ('recording', 'recording_resolution'),
        ('recording', 'recording_bitrate_level'),
        ('recording', 'recording_custom_bitrate_kbps'),
        ('audio', 'audio_capture_enabled'),
        ('audio', 'audio_capture_mode'),
        ('audio', 'audio_source_mode'),
        ('audio', 'audio_device_tracks'),
        ('audio', 'per_app_audio_tracks'),
        ('audio', 'mic_device_id'),
        ('audio', 'playback_device_id'),
        ('audio', 'mic_downmix_mono'),
        ('audio', 'mic_volume'),
        ('camera', 'camera_enabled'),
        ('camera', 'camera_device_index'),
        ('camera', 'camera_overlay_rect'),
    )
    expected_keys = tuple(key for _group, key in expected_leaves)
    validation_calls = []
    validate_leaf = profile_ui._validate_leaf

    def count_validation(group, key, value):
        validation_calls.append((group, key))
        return validate_leaf(group, key, value)

    monkeypatch.setattr(profile_ui, '_validate_leaf', count_validation)

    class CountingManager(SettingsManager):
        def __init__(self):
            self.config_file = Path(tmp_path) / 'settings.json'
            self.settings = {'framerate': 'bad', 'mic_volume': 0, 'clip_length': 90}
            self.get_calls = []

        def get(self, key, default=None):
            self.get_calls.append(key)
            return self.settings.get(key, default)

    manager = CountingManager()
    catalog = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    assert manager.save_game_capture_profiles(catalog, expected_revision=0)
    manager.get_calls.clear()
    widget = GameProfilesWidget(manager)
    qtbot.addWidget(widget)
    QCoreApplication.sendEvent(widget.save_button, QEvent(QEvent.Type.StyleChange))

    assert len(expected_keys) == 21
    assert set(widget._bindings) == set(expected_keys)
    assert Counter(key for key in manager.get_calls if key in expected_keys) == Counter(expected_keys)
    assert set(manager.get_calls) <= set(expected_keys)
    assert Counter(validation_calls) == Counter(expected_leaves)
    assert widget._bindings['clip_length'].editor.value() == 90
    assert widget._bindings['mic_volume'].editor.value() == 0
    assert 'invalid global' in widget._bindings['framerate'].global_value.text()


def test_global_validation_matches_core_for_overflow_unsafe_and_duplicate_values(
        qapp, tmp_path):
    raw_rect = {'x': 10 ** 400, 'y': 0.1, 'w': 0.5, 'h': 0.5}
    manager = _saved_manager(
        tmp_path,
        clip_length=90,
        framerate=120,
        mic_volume=0,
        mic_device_id='unsafe\x00endpoint',
        audio_device_tracks=[
            {'id': 'duplicate', 'name': 'One'},
            {'id': 'duplicate', 'name': 'Two'},
        ],
        camera_overlay_rect=raw_rect,
    )
    widget = GameProfilesWidget(manager)

    assert widget._global_values['clip_length'] == 90
    assert widget._global_values['mic_volume'] == 0
    assert 'clip_length' not in widget._global_errors
    assert 'mic_volume' not in widget._global_errors
    assert 'mic_device_id' in widget._global_errors
    assert 'audio_device_tracks' in widget._global_errors
    assert 'camera_overlay_rect' in widget._global_errors
    assert widget._global_values['camera_overlay_rect'] == DEFAULT_OVERLAY_RECT


def test_invalid_inherited_global_is_visible_but_valid_override_can_save(qapp, tmp_path):
    manager = _saved_manager(tmp_path, framerate='invalid')
    widget = GameProfilesWidget(manager)
    assert 'invalid global' in widget._bindings['framerate'].global_value.text()

    binding = widget._bindings['framerate']
    binding.toggle.setChecked(True)
    binding.editor.setValue(120)
    assert widget.save_draft() is True
    assert manager.read_game_capture_profiles().profiles[0].overrides['clips']['framerate'] == 120


def test_inventory_refresh_preserves_missing_saved_choices(qapp, tmp_path):
    manager = _saved_manager(
        tmp_path,
        overrides={
            'audio': {'mic_device_id': 'mic-missing'},
            'camera': {'camera_device_index': 7},
        },
    )
    widget = GameProfilesWidget(manager)
    widget.set_audio_devices(
        [{'endpoint_id': 'mic-1', 'display_name': 'Desk microphone', 'available': True}],
        [{'endpoint_id': 'playback-1', 'display_name': 'Headphones', 'available': True}],
    )
    widget.set_camera_devices([{'index': 0, 'display_name': 'Webcam', 'available': True}])

    assert widget._bindings['mic_device_id'].editor.currentData() == 'mic-missing'
    assert widget._bindings['camera_device_index'].editor.currentData() == 7
    assert manager.settings['game_capture_profiles']['profiles'][0]['overrides']['camera']['camera_device_index'] == 7


def test_none_and_empty_endpoint_defaults_round_trip_without_first_device_fallback(
        qapp, tmp_path):
    manager = _saved_manager(
        tmp_path,
        overrides={'audio': {'mic_device_id': None, 'playback_device_id': ''}},
    )
    widget = GameProfilesWidget(manager)
    widget.set_audio_devices(
        [{'endpoint_id': 'mic-1', 'display_name': 'Mic', 'available': True}],
        [{'endpoint_id': 'playback-1', 'display_name': 'Playback', 'available': True}],
    )

    assert widget._bindings['mic_device_id'].editor.currentData() is None
    assert widget._bindings['playback_device_id'].editor.currentData() == ''
    playback_editor = widget._bindings['playback_device_id'].editor
    assert sum(playback_editor.itemText(index) == 'System default'
               for index in range(playback_editor.count())) == 1
    assert all('explicit' not in playback_editor.itemText(index).lower()
               for index in range(playback_editor.count()))
    widget._bindings['clip_length'].toggle.setChecked(True)
    widget._bindings['clip_length'].editor.setValue(90)
    assert widget.save_draft() is True
    saved_audio = manager.read_game_capture_profiles().profiles[0].overrides['audio']
    assert saved_audio['mic_device_id'] is None
    assert saved_audio['playback_device_id'] == ''


def test_tracks_are_atomic_and_endpoint_ids_are_hidden_from_primary_copy(qapp, tmp_path):
    manager = _saved_manager(
        tmp_path,
        overrides={'audio': {'audio_device_tracks': [
            {'id': 'endpoint-1', 'name': 'Game mix'},
            {'id': 'endpoint-2', 'name': 'Voice'},
        ]}},
    )
    widget = GameProfilesWidget(manager)
    widget.set_audio_devices(
        [{'endpoint_id': 'mic-only', 'display_name': 'Microphone', 'available': True}],
        [{'endpoint_id': 'endpoint-1', 'display_name': 'Playback mix', 'available': True},
         {'endpoint_id': 'endpoint-2', 'display_name': 'Voice input', 'available': True}],
    )
    tracks = widget._bindings['audio_device_tracks'].editor
    assert tracks.rows[0][0].text() == 'Game mix'
    assert tracks.rows[0][1].currentText() == 'Playback mix'
    assert tracks.rows[0][1].currentData() == 'endpoint-1'
    assert 'endpoint-1' not in tracks.rows[0][1].currentText()
    assert len(tracks.value()) == 2


def test_tracks_use_playback_inventory_and_keep_partial_rows_on_refresh(
        qapp, tmp_path):
    manager = _saved_manager(tmp_path, overrides={'audio': {'audio_device_tracks': []}})
    widget = GameProfilesWidget(manager)
    widget.set_audio_devices(
        [{'endpoint_id': 'mic-only', 'display_name': 'Microphone', 'available': True}],
        [{'endpoint_id': 'playback-only', 'display_name': 'Game output', 'available': True}],
    )
    tracks = widget._bindings['audio_device_tracks'].editor
    name, combo = tracks.rows[0]
    name.setText('Voice')
    widget.set_audio_devices(
        [{'endpoint_id': 'mic-only', 'display_name': 'Microphone', 'available': True}],
        [{'endpoint_id': 'playback-only', 'display_name': 'Game output v2', 'available': True}],
    )
    assert name.text() == 'Voice'
    assert combo.findData('mic-only') < 0
    assert combo.findData('playback-only') >= 0
    before = manager.read_game_capture_profiles().to_dict()
    assert widget.save_draft() is False
    assert 'audio track id must not be empty' in widget._state_label.text().lower()
    assert name.text() == 'Voice'
    assert manager.read_game_capture_profiles().to_dict() == before


def test_rect_only_edit_is_dirty_but_programmatic_refresh_is_clean(qapp, tmp_path):
    manager = _saved_manager(
        tmp_path,
        overrides={'camera': {'camera_overlay_rect': {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}}},
    )
    widget = GameProfilesWidget(manager)
    rect = widget._bindings['camera_overlay_rect'].editor
    x_spin = rect.findChild(QDoubleSpinBox, 'cameraRectX')
    x_spin.setValue(0.2)
    assert widget._dirty is True
    widget._dirty = False
    widget._set_rect(rect, {'x': 0.3, 'y': 0.2, 'w': 0.3, 'h': 0.4})
    assert widget._dirty is False


def test_saved_normalized_values_are_reloaded_into_controls(qapp, tmp_path):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    tracks = widget._bindings['audio_device_tracks']
    tracks.toggle.setChecked(True)
    rows = tracks.editor.rows
    rows[0][0].setText('Mic-?')
    rows[0][1].addItem('Friendly', 'endpoint-1')
    rows[0][1].setCurrentIndex(rows[0][1].count() - 1)
    rect = widget._bindings['camera_overlay_rect']
    rect.toggle.setChecked(True)
    rect.editor.findChild(QDoubleSpinBox, 'cameraRectX').setValue(0.9)
    rect.editor.findChild(QDoubleSpinBox, 'cameraRectW').setValue(0.5)

    assert widget.save_draft() is True
    fresh_tracks = widget._bindings['audio_device_tracks']
    fresh_rows = fresh_tracks.editor.rows
    assert fresh_tracks is not tracks
    assert fresh_rows[0][0].text() == 'Mic-'
    fresh_rect = widget._bindings['camera_overlay_rect'].editor
    assert fresh_rect.findChild(QDoubleSpinBox, 'cameraRectX').value() == 0.5
    assert fresh_rect.findChild(QDoubleSpinBox, 'cameraRectW').value() == 0.5
    assert widget._dirty is False
    saved = manager.read_game_capture_profiles().profiles[0]
    assert saved.overrides['audio']['audio_device_tracks'][0]['name'] == 'Mic-'


def test_writer_false_and_oserror_keep_draft_without_conflict_only_claim(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    binding = widget._bindings['clip_length']
    binding.toggle.setChecked(True)
    binding.editor.setValue(90)
    monkeypatch.setattr(manager, '_persist_settings_candidate', lambda _candidate: False)
    assert widget.save_draft() is False
    assert widget._dirty is True
    assert 'disk write failed' in widget._state_label.text()

    def fail_writer(_candidate):
        raise OSError('replace failed')

    monkeypatch.setattr(manager, '_persist_settings_candidate', fail_writer)
    assert widget.save_draft() is False
    assert widget._dirty is True


def test_reload_cancel_and_discard_are_guarded(qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._name_edit.setText('Unsaved')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)
    widget._reload_catalog()
    assert widget._name_edit.text() == 'Unsaved'
    assert widget._dirty is True

    monkeypatch.setattr(widget, '_draft_prompt', lambda: 2)
    widget._reload_catalog()
    assert widget._name_edit.text() == 'Game'
    assert widget._dirty is False


def test_public_refresh_uses_save_discard_cancel_boundary(qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._name_edit.setText('Unsaved')
    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)

    widget.refresh()

    assert widget._name_edit.text() == 'Unsaved'
    assert widget._dirty is True


def test_delete_confirmation_is_plain_text_and_status_distinguishes_none(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    calls = []
    monkeypatch.setattr(
        'ui.game_profiles.FthrMessageDialog.question',
        lambda *args, **kwargs: calls.append(kwargs) or False,
    )
    widget._draft['name'] = '<literal>'
    widget._delete_profile()
    assert calls[0]['plain_text'] is True

    widget.set_capture_status(None, None, '')
    assert 'No active capture' in widget._capture_label.text()
    widget.set_capture_status({'name': 'Global defaults', 'exe_path': ''}, None, '')
    assert 'Global defaults' in widget._capture_label.text()


def test_future_catalog_retry_is_reachable_outside_editor_stack(qapp, tmp_path):
    manager = _manager(tmp_path)
    future = {'version': 99, 'revision': 4, 'profiles': [], 'future': {'keep': True}}
    manager.settings['game_capture_profiles'] = future
    widget = GameProfilesWidget(manager)
    widget.show()
    qapp.processEvents()
    assert widget.reload_button.isVisible()
    assert widget._flow.currentWidget() is widget._library_page
    widget.close()


def test_future_catalog_is_preserved_and_editing_disabled(qapp, tmp_path):
    manager = _manager(tmp_path)
    future = {'version': 99, 'revision': 4, 'profiles': [], 'future': {'keep': True}}
    manager.settings['game_capture_profiles'] = future
    manager.config_file.write_text(json.dumps(manager.settings), encoding='utf-8')
    widget = GameProfilesWidget(manager)

    assert widget._future_catalog is True
    assert widget.new_button.isEnabled() is False
    assert widget.save_button.isEnabled() is False
    assert 'unavailable' in widget._state_label.text().lower()
    assert manager.settings['game_capture_profiles'] == future


def test_status_is_plain_text_and_refresh_is_passive(qapp, tmp_path):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    refreshes = []
    widget.refresh_devices_requested.connect(lambda: refreshes.append(True))
    widget.set_capture_status(
        {'name': 'Competitive', 'exe_path': r'C:\Games\Game.exe'},
        {'name': 'Pending'},
        'Waiting for capture resources',
    )
    assert 'Competitive' in widget._capture_label.text()
    assert 'C:\\Games' not in widget._capture_label.text()
    assert 'Waiting for capture resources' in widget._capture_label.text()
    widget._open_profile()
    assert refreshes == [True]
    assert manager.get('game_capture_profiles') == manager.settings['game_capture_profiles']


def test_same_manager_competing_revision_save_reload_cancel_and_discard(
        qapp, tmp_path, monkeypatch):
    manager = _saved_manager(tmp_path)
    widget = GameProfilesWidget(manager)
    widget._name_edit.setText('Widget draft')
    current = manager.read_game_capture_profiles()
    external = update_profile(
        current, current.profiles[0].id, name='External committed')
    assert manager.save_game_capture_profiles(
        external, expected_revision=current.revision) is True
    disk_after_external = manager.read_game_capture_profiles().to_dict()

    assert widget.save_draft() is False
    assert widget._dirty is True
    assert widget._name_edit.text() == 'Widget draft'
    assert manager.read_game_capture_profiles().to_dict() == disk_after_external

    monkeypatch.setattr(widget, '_draft_prompt', lambda: 0)
    widget.refresh()
    assert widget._name_edit.text() == 'Widget draft'
    assert widget._dirty is True

    monkeypatch.setattr(widget, '_draft_prompt', lambda: 2)
    widget.refresh()
    assert widget._name_edit.text() == 'External committed'
    assert widget._dirty is False
