"""Compact settings sections that stack when their controls need more space."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (QBoxLayout, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QToolButton, QCheckBox, QFrame, QSizePolicy)


def settings_section():
    section = QWidget()
    section.setStyleSheet('background: transparent;')
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return section, layout


def folded_settings_section(title, *, expanded=False):
    """Use the same immediate disclosure as Customize and Visuals."""
    from ui.customize_page import _AccordionSection
    section = _AccordionSection(title)
    content, layout = settings_section()
    layout.setSpacing(12)
    section.add_content(content)
    if expanded:
        section.expand()
    return section, layout


class SettingsSectionPair(QWidget):
    """Use two columns only when both sections' minimum widths fit."""
    def __init__(self, first, second, parent=None):
        super().__init__(parent)
        self._sections = (first, second)
        self._row = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(24)
        for section in self._sections:
            self._row.addWidget(section, 1, Qt.AlignmentFlag.AlignTop)

    def resizeEvent(self, event):
        required = sum(max(400, section.minimumSizeHint().width())
                       for section in self._sections) + 24
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= required
                     else QBoxLayout.Direction.TopToBottom)
        if self._row.direction() != direction:
            self._row.setDirection(direction)
            self.updateGeometry()
        super().resizeEvent(event)


class SettingHelp(QWidget):
    """A setting with keyboard-accessible, click-to-expand plain-language help."""
    def __init__(self, control, description, parent=None):
        super().__init__(parent)
        from ui.icons import load_icon
        from ui.style import Colors, Fonts, label_body, set_theme_style
        self.setObjectName('settingHelp')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(control, 1)
        self.button = QToolButton(self)
        self.button.setIcon(load_icon('info.svg', 16))
        self.button.setIconSize(QSize(16, 16))
        self.button.setFixedSize(28, 28)
        self.button.setCheckable(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        title = control.text() if hasattr(control, 'text') else control.accessibleName()
        self.button.setAccessibleName(f'About {title}')
        self.button.setToolTip(f'About {title}')
        set_theme_style(self.button, lambda: (
            f'QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; }}'
            f'QToolButton:hover, QToolButton:checked {{ background: {Colors.SURFACE_2}; border-color: {Colors.BORDER}; }}'
            f'QToolButton:focus {{ border-color: {Colors.ACCENT}; }}'))
        row.addWidget(self.button)
        layout.addLayout(row)
        self.description = QLabel(description, self)
        self.description.setWordWrap(True)
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setAccessibleName(description)
        set_theme_style(self.description, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.description.hide()
        layout.addWidget(self.description)
        self.button.toggled.connect(self.description.setVisible)
        control.setAccessibleDescription(description)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.button.isChecked():
            self.button.setChecked(False)
            self.button.setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)


def standardize_settings_spacing(page):
    """Give plain settings forms a shared row rhythm without restyling editors.

    Only ordinary QWidget/QFrame form containers are touched. Custom previews,
    composite controls and their internal layouts keep their own geometry.
    """
    for container in (page, *page.findChildren(QWidget)):
        if type(container) not in (QWidget, QFrame):
            continue
        layout = container.layout()
        if not isinstance(layout, QVBoxLayout) or layout.count() < 2:
            continue
        if layout.spacing() >= 20:  # Existing section groups already have a gap.
            continue
        for index in reversed(range(layout.count())):
            spacer = layout.itemAt(index).spacerItem()
            if spacer is None or spacer.sizeHint().height() <= 0:
                continue  # Preserve expanding stretches.
            height = spacer.sizeHint().height()
            if height <= 12:
                layout.takeAt(index)
            elif height <= 32:
                spacer.changeSize(0, 12, QSizePolicy.Policy.Minimum,
                                  QSizePolicy.Policy.Fixed)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    for check in page.findChildren(QCheckBox):
        check.setMinimumHeight(max(28, check.minimumHeight()))
