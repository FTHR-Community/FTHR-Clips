"""Folding must preserve settings, keyboard access, and search navigation."""
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea


def test_folded_pages_keep_controls_reachable(qtbot, monkeypatch, tmp_path):
    import main
    from core.settings_manager import SettingsManager
    from ui.customize_page import _AccordionSection
    from ui.app_style import apply_app_style
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for method in ('_start_encoder_probe', '_populate_camera_devices',
                   '_populate_mic_devices', '_populate_playback_devices',
                   '_start_audio_preview_if_current'):
        monkeypatch.setattr(main._SettingsPage, method, lambda *a, **k: None)
    apply_app_style(QApplication.instance())
    page = main._SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.resize(1100, 850)
    page.show()
    for index, target_name in ((0, 'capture_card_mode_combo'), (1, 'watermark_check'),
                               (2, 'mic_mono_check'), (5, 'parallel_exports_check'),
                               (6, 'shortcut_settings.enabled_check'),
                               (10, 'replay_storage_widget.mode_combo')):
        page.navigate_category_to(index)
        qtbot.waitUntil(lambda expected=index: page.stack.currentIndex() == expected)
        sections = page.stack.currentWidget().findChildren(_AccordionSection)
        assert len(sections) >= 2
        for section in sections:
            section.collapse()
        page._pending_search_destination = SimpleNamespace(category=index, target=target_name)
        page._reveal_setting_result()
        target = page
        for part in target_name.split('.'):
            target = getattr(target, part)
        assert target.isVisible()
        assert any(section.expanded for section in sections)
        for section in sections:
            section.expand()
        qtbot.wait(30)
        root = page.stack.currentWidget()
        scrolls = root.findChildren(QScrollArea)
        if isinstance(root, QScrollArea):
            scrolls.append(root)
        assert scrolls
        assert all(scroll.horizontalScrollBar().maximum() == 0 for scroll in scrolls)

    page.navigate_category_to(0)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 0)
    original = page.kofi_widget_check.isChecked()
    sections = page.stack.currentWidget().findChildren(_AccordionSection)
    system = next(section for section in sections if section._title == 'System')
    system.expand()
    system._header.setFocus()
    qtbot.keyClick(system._header, Qt.Key.Key_Space)
    assert not system.expanded
    assert page.kofi_widget_check.isChecked() == original
    page._pending_search_destination = SimpleNamespace(category=0, target='credits_toggle')
    page._reveal_setting_result()
    assert page.credits_toggle.isChecked()
    assert page.credits_toggle.parentWidget().expanded
