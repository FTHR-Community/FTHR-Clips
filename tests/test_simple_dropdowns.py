from __future__ import annotations

from ui.customize_page import _AccordionSection
from main import _DropdownCombo, TopBarButton
from ui.clip_viewer import ClipViewer
from ui.style import (
    Colors, Sizes, ThemedDropdownArrow, ThemedDropdownButton, combo_qss,
)
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QWidget


def test_combo_style_uses_a_real_dropdown_popup():
    assert 'combobox-popup: 0' in combo_qss()


def test_customize_sections_toggle_immediately_without_animation(qtbot):
    section = _AccordionSection('Colors', '01')
    qtbot.addWidget(section)

    assert not section.expanded
    assert isinstance(section._indicator, ThemedDropdownArrow)
    assert not section._body.isVisible()
    assert not hasattr(section, '_anim')

    section.expand()
    assert section.expanded
    assert section._indicator._expanded
    assert not section._body.isHidden()

    section.collapse()
    assert not section.expanded
    assert not section._indicator._expanded
    assert section._body.isHidden()


def test_topbar_button_uses_themed_arrow_instead_of_a_text_glyph(qtbot):
    button = TopBarButton('SOURCE')
    qtbot.addWidget(button)

    assert isinstance(button, ThemedDropdownButton)
    assert button.text() == 'SOURCE'
    assert '▾' not in button.text()


def test_clip_editor_panel_uses_themed_arrow_instead_of_plus_minus(qtbot):
    viewer = ClipViewer.__new__(ClipViewer)
    panel, _ = viewer._build_collapsible_panel('CLIP DETAILS', expanded=False)
    qtbot.addWidget(panel)

    button = panel.findChild(ThemedDropdownButton, 'panelHeader')
    assert button is not None
    assert button.text() == 'CLIP DETAILS'
    assert not button._dropdown_expanded

    button.click()
    assert button._dropdown_expanded


def test_customize_section_content_uses_gray_surface_and_safe_insets(qtbot):
    section = _AccordionSection('Capture Card', '05')
    content = QWidget()
    section.add_content(content)
    qtbot.addWidget(section)

    margins = section._body_layout.contentsMargins()

    assert content.objectName() == 'accordionContent'
    assert 'QWidget#accordionBody > QWidget#accordionContent' in section.styleSheet()
    assert f'background-color: {Colors.SURFACE_3}' in section.styleSheet()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
        Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5)


def test_combo_popup_opens_below_the_control(qtbot):
    combo = _DropdownCombo()
    qtbot.addWidget(combo)
    combo.addItems(['480p', '720p', '1080p', '1440p', 'Source'])
    combo.setStyleSheet(combo_qss())
    combo.resize(220, 36)
    combo.show()
    qtbot.waitExposed(combo)

    combo.showPopup()
    qtbot.wait(50)
    popup = combo.view().window()
    combo_bottom = combo.mapToGlobal(combo.rect().bottomLeft()).y()

    assert popup.frameGeometry().top() >= combo_bottom
    assert popup.frameGeometry().width() >= combo.width()
    combo.hidePopup()


def test_closed_combo_ignores_wheel_selection_changes(qtbot):
    combo = _DropdownCombo()
    qtbot.addWidget(combo)
    combo.addItems(['One', 'Two', 'Three'])
    combo.setCurrentIndex(1)
    event = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False)

    combo.wheelEvent(event)

    assert combo.currentIndex() == 1
    assert not event.isAccepted()
