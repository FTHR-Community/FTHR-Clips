"""Regressions for empty library slots and live settings customization."""
import os
from types import SimpleNamespace
import pytest

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QScrollArea

from ui import clip_grid


def _settings(path):
    return SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(path), 'imported_clip_folders': [],
    }.get(key, default))


def test_thumbnail_queue_drains_without_a_new_scan_or_capture(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(clip_grid, '_MAX_THUMBNAIL_QUEUE', 2)
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path / 'cache'))
    image = QImage(16, 9, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    for i in range(8):
        assert image.save(str(tmp_path / f'{i}.png'))
    grid = clip_grid.ClipGrid(_settings(tmp_path))
    qtbot.addWidget(grid)
    try:
        qtbot.waitUntil(lambda: grid._active_scan_worker is None)
        grid.refresh_timer.stop()
        grid._debounce_timer.stop()
        grid._watcher.blockSignals(True)
        qtbot.waitUntil(lambda: len(grid.thumbnails) == 8
                       and all(card._thumbnail_ready for card in grid.thumbnails))
        assert grid._thumbnail_queue_peak <= 2
        assert not grid._deferred_thumbnail_paths
    finally:
        grid.shutdown()


def test_many_small_date_sections_do_not_reserve_offscreen_card_budget(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    # Each section fits individually, but their total exceeds the live budget.
    for section in range(4):
        for i in range(30):
            path = tmp_path / f'{section}-{i}.mp4'
            path.touch()
            stamp = 1_700_000_000 - section * 86400
            os.utime(path, (stamp, stamp))
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1100, 600)
    scroll.setWidgetResizable(True)
    grid = clip_grid.ClipGrid(_settings(tmp_path))
    scroll.setWidget(grid)
    scroll.show()
    try:
        qtbot.waitUntil(lambda: grid._active_scan_worker is None)
        qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)
        assert not grid._section_grids
        assert len(grid._virtual_sections) == 4
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        qtbot.wait(50)
        grid._update_virtualized_cards()
        assert any('3-' in os.path.basename(path) for path in grid._thumb_widgets)
        scroll.verticalScrollBar().setValue(0)
        qtbot.wait(50)
        grid._update_virtualized_cards()
        assert any('0-' in os.path.basename(path) for path in grid._thumb_widgets)
        assert len(grid._thumb_widgets) <= clip_grid._MAX_MATERIALIZED_CARDS
    finally:
        grid.shutdown()


def test_card_does_not_start_transparent_or_require_rounded_duration(
        qtbot, tmp_path):
    image = QImage(16, 9, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    path = tmp_path / 'thumbnail.jpg'
    assert image.save(str(path))
    card = clip_grid.ClipThumbnail(str(tmp_path / 'short.mp4'))
    qtbot.addWidget(card)
    card.fade_in(delay_ms=600)
    assert card.graphicsEffect() is None
    card.set_video_thumbnail(str(path), 0)
    assert card._thumbnail_ready


def test_settings_apply_repaints_existing_scroll_surfaces_and_controls(
        qtbot, monkeypatch, tmp_path):
    from core.settings_manager import SettingsManager
    from core.theme_manager import ThemeManager
    from main import _SettingsPage, _load_fonts
    from ui.app_style import apply_app_style
    from ui.style import Colors, Fonts
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    _load_fonts()
    # Ensure test cleanup restores every token mutated by Apply.
    for token, value in vars(Colors).copy().items():
        if token.isupper():
            monkeypatch.setattr(Colors, token, value)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.resize(1500, 780)
    page.show()
    theme = ThemeManager()
    original_fonts = Fonts.DISPLAY_FAMILY, Fonts.BODY_FAMILY
    app = QApplication.instance()
    original_palette = app.palette()
    try:
        for color in ('#ff55ff', '#125637'):
            theme.set_color('BG', color)
            theme.set_color('SURFACE_2', color)
            theme.set_color('SURFACE_3', color)
            theme.set_color('ACCENT', '#abcdef')
            combo = page.preset_combo
            selected = combo.currentIndex()
            page._on_theme_applied()
            assert page.preset_combo is combo
            assert combo.currentIndex() == selected
            assert f'background-color: {color}' in combo.styleSheet()
            assert '#abcdef' in page.export_presets_widget.new_btn.styleSheet()
            for index in (0, 1, 3):
                page.stack.setCurrentIndex(index)
                qtbot.wait(30)
                area = page.stack.widget(index).findChild(QScrollArea)
                assert color in area.styleSheet()
                area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
                qtbot.wait(30)
                rendered = area.viewport().grab().toImage()
                # Empty right padding in the viewport is the actual canvas.
                assert rendered.pixelColor(rendered.width() - 2, 5).name() == color
            if color == '#ff55ff':
                page.stack.setCurrentIndex(0)
                qtbot.wait(30)
                page.grab().save(str(tmp_path / 'settings-pink.png'))
    finally:
        Fonts.configure(*original_fonts)
        apply_app_style(app)
        app.setPalette(original_palette)


def test_diagnostic_zip_drag_uses_native_copy_file_url(qtbot, monkeypatch, tmp_path):
    from ui import diagnostic_report
    path = tmp_path / 'report with spaces.zip'
    path.write_bytes(b'zip fixture')
    button = diagnostic_report.DiagnosticReportFile()
    qtbot.addWidget(button)
    button.set_file(path)
    button.show()
    drags = []

    class Drag:
        def __init__(self, _parent):
            drags.append(self)

        def setMimeData(self, data):
            self.data = data

        def exec(self, actions):
            self.actions = actions

    monkeypatch.setattr(diagnostic_report, 'QDrag', Drag)
    opened = []
    monkeypatch.setattr(diagnostic_report.QDesktopServices, 'openUrl', opened.append)
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(button.width() + 50, 10),
                        QPointF(500, 10), Qt.MouseButton.NoButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    button.mouseMoveEvent(event)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert len(drags) == 1
    assert drags[0].data.urls()[0].toLocalFile() == str(path).replace('\\', '/')
    assert drags[0].actions == Qt.DropAction.CopyAction
    assert not opened  # Completing a drag must not also trigger a click.
    path.unlink()
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    button.mouseMoveEvent(event)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert len(drags) == 1
    assert 'export again' in button.text()


@pytest.mark.parametrize('outcome', ['success', 'cancelled', 'failed'])
def test_export_publishes_drag_target_only_after_zip_success(
        qtbot, monkeypatch, tmp_path, outcome):
    import main
    from ui import diagnostic_report

    path = tmp_path / 'report.zip'
    file_button = diagnostic_report.DiagnosticReportFile()
    qtbot.addWidget(file_button)
    exports, dialogs, warnings = [], [], []

    def export(output, **_kwargs):
        exports.append(output)
        if outcome == 'failed':
            raise OSError('read-only output directory')
        output.write_bytes(b'completed zip fixture')
        return output

    def dialog(output, _parent):
        assert output.is_file()
        assert file_button._path == output.resolve()
        return SimpleNamespace(exec=lambda: dialogs.append(output))

    host = SimpleNamespace(
        _diagnostics=SimpleNamespace(short_id='test', export_zip=export),
        _settings_page_widget=SimpleNamespace(diagnostic_report_file=file_button))
    monkeypatch.setattr(main.QFileDialog, 'getSaveFileName',
                        lambda *_: ('' if outcome == 'cancelled' else str(path), ''))
    monkeypatch.setattr(main.FthrMessageDialog, 'warning', lambda *_: warnings.append(True))
    monkeypatch.setattr(diagnostic_report, 'DiagnosticReportDialog', dialog)
    main.MainWindow._export_diagnostic_report(host)
    assert bool(exports) == (outcome != 'cancelled')
    assert bool(dialogs) == (outcome == 'success')
    assert bool(warnings) == (outcome == 'failed')
    assert (file_button._path is not None) == (outcome == 'success')
