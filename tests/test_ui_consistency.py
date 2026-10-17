"""Hit targets, settings geometry, and live themes after the UI cleanup."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QFrame

from core.settings_manager import SettingsManager
from core.theme_manager import ThemeManager, DEFAULT_COLORS, CUSTOMIZABLE_COLOR_GROUPS
from ui import frameless_window_windows as frameless
from ui.style import Colors, Sizes


@pytest.mark.parametrize('physical_size', [(1920, 1080), (2560, 1440), (3840, 2160)])
@pytest.mark.parametrize('scale', [1.0, 1.25, 1.5, 2.0])
def test_right_titlebar_controls_remain_client_hits(
        qtbot, monkeypatch, physical_size, scale):
    from PySide6.QtWidgets import QWidget
    width, height = physical_size
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(round(width / scale), round(height / scale))
    button = QPushButton('Close', window)
    button.setGeometry(window.width() - 48, 0, 48, 36)
    window.show()
    monkeypatch.setattr(window, 'devicePixelRatioF', lambda: scale)
    # A scale override need not match the OS DPI. Using 96 here reproduces
    # the old lookup missing the rightmost button as the scale increases.
    monkeypatch.setattr(frameless, 'dpi_for_window', lambda _: 96)
    origin = QPoint(-width, -200)
    monkeypatch.setattr(frameless, 'get_window_rect',
                        lambda _: QRect(origin, QSize(*physical_size)))
    client = QPoint(round((window.width() - 24) * scale), round(18 * scale))
    monkeypatch.setattr(frameless, 'screen_to_client_physical', lambda *_: client)
    assert frameless.native_hit_test(window, origin.x() + client.x(),
                                    origin.y() + client.y(),
                                    titlebar_height=36) == frameless.HT_CLIENT
    pressed = []
    button.clicked.connect(lambda: pressed.append(True))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert pressed == [True]


def test_client_hits_do_not_fall_through_to_native_frame_handling():
    rectangle = QRect(0, 0, 3840, 2160)
    for x, y, interactive in ((3600, 900, False), (3839, 20, True)):
        assert frameless.classify_native_hit(
            rectangle, x, y, client_y=y,
            titlebar_height_physical=72, resize_border_physical=12,
            maximized=False, interactive=interactive) == frameless.HT_CLIENT


def test_settings_padding_and_live_theme_cover_detached_dialogs(
        qtbot, monkeypatch, tmp_path):
    import main
    from ui.dialogs import FthrDialog
    from ui.app_style import apply_app_style
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    monkeypatch.setattr(main._SettingsPage, '_populate_mic_devices', lambda _: None)
    monkeypatch.setattr(main._SettingsPage, '_populate_playback_devices', lambda _: None)
    monkeypatch.setattr(main._SettingsPage, '_populate_camera_devices', lambda _: None)
    monkeypatch.setattr(main._SettingsPage, '_start_encoder_probe', lambda _: None)
    old_colors = {name: getattr(Colors, name) for name in DEFAULT_COLORS}
    old_aliases = {name: getattr(Colors, name) for name in
                   ('BAR_BG', 'BAR_FG', 'BAR_FG_DIM', 'BAR_HAIRLINE')}
    page = main._SettingsPage(SettingsManager())
    dialog = FthrDialog('Theme coverage')
    qtbot.addWidget(page)
    qtbot.addWidget(dialog)
    page.resize(1150, 760)
    page.show()
    page.stack.setCurrentIndex(2)
    page.stack._finish_slide()
    QApplication.processEvents()
    if hasattr(page, 'playback_combo'):
        label = page.findChild(QLabel, 'playbackDeviceLabel')
        refresh = page.findChild(QPushButton, 'playbackDeviceRefresh')
        assert page.playback_combo.x() - (label.x() + label.width()) >= Sizes.ROW_GAP
        assert refresh.x() - (page.playback_combo.x() + page.playback_combo.width()) >= Sizes.ROW_GAP
    # The Visuals controls are now created on their first explicit use.
    page.stack.widget(3)
    options = page.findChild(QFrame, 'overlayOptionsPanel')
    assert 'background: transparent' in options.styleSheet()
    assert 'border: none' in options.styleSheet()
    assert {token for _, group in CUSTOMIZABLE_COLOR_GROUPS for token, _ in group} == set(DEFAULT_COLORS)
    assert {swatch.token for swatch in page._customize_page._swatches} == set(DEFAULT_COLORS)
    theme = ThemeManager()
    try:
        for index, name in enumerate(DEFAULT_COLORS):
            theme.set_color(name, f'#{index + 1:06x}')
        page._on_theme_applied()
        assert Colors.BAR_BG == Colors.SHELL_BG
        assert Colors.BG in dialog.styleSheet()
        assert Colors.BORDER_HI in dialog.styleSheet()
        assert Colors.TEXT in page._customize_page._export_btn.styleSheet()
        assert Colors.ACCENT in page._customize_page._apply_btn.styleSheet()
        for surface in (page, dialog):
            for widget in [surface, *surface.findChildren(main.QWidget)]:
                recipe = getattr(widget, '_theme_style_factory', None)
                if recipe is not None:
                    assert widget.styleSheet() == recipe()
    finally:
        for name, value in {**old_colors, **old_aliases}.items():
            setattr(Colors, name, value)
        apply_app_style(QApplication.instance())
        ThemeManager._instance = None
