"""Compact settings sections that stack when their controls need more space."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QBoxLayout, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QToolButton, QCheckBox, QFrame, QSizePolicy)

from ui.style import Colors, Fonts, Sizes, ThemedDropdownArrow, label_body, set_theme_style


class AccordionSection(QFrame):
    """Collapsible section with an immediate, stable expand/collapse."""

    def __init__(self, title: str, number: str = '', parent=None):
        super().__init__(parent)
        self.setObjectName('accordionSection')
        self._title = title
        self._expanded = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QPushButton()
        self._header.setObjectName('accordionHeader')
        self._header.setCheckable(True)
        self._header.setAccessibleName(title)
        self._header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._header.setFixedHeight(Sizes.CAPTURE_CONTROL_H)
        self._header.clicked.connect(self.toggle)

        h_layout = QHBoxLayout(self._header)
        h_layout.setContentsMargins(16, 0, 16, 0)
        h_layout.setSpacing(12)

        if number:
            num_lbl = QLabel(number)
            h_layout.addWidget(num_lbl)

        title_lbl = QLabel(title)
        h_layout.addWidget(title_lbl)
        h_layout.addStretch()

        self._indicator = ThemedDropdownArrow()
        h_layout.addWidget(self._indicator)

        layout.addWidget(self._header)

        self._body = QWidget()
        self._body.setObjectName('accordionBody')
        self._body.setVisible(False)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5)
        self._body_layout.setSpacing(8)
        layout.addWidget(self._body)
        self.refresh_theme()

    def add_content(self, widget: QWidget):
        # Scope the surface style to direct content wrappers.
        widget.setObjectName('accordionContent')
        self._body_layout.addWidget(widget)

    def add_layout(self, layout):
        self._body_layout.addLayout(layout)

    def clear_content(self):
        """Remove the current body widgets so theme styles can be rebuilt."""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                child_layout = item.layout()
                while child_layout.count():
                    child_item = child_layout.takeAt(0)
                    child_widget = child_item.widget()
                    if child_widget is not None:
                        child_widget.deleteLater()

    def refresh_theme(self):
        """Refresh the section shell and header after a theme change."""
        set_theme_style(self, lambda: (f'''
            QFrame#accordionSection {{
                background-color: {Colors.SURFACE_3};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
            QPushButton#accordionHeader {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-radius: {Sizes.RADIUS_CARD}px;
                text-align: left;
            }}
            QPushButton#accordionHeader:hover {{
                background-color: {Colors.CARD_BG_HI};
            }}
            QPushButton#accordionHeader:focus {{
                border: 1px solid {Colors.ACCENT};
            }}
            QWidget#accordionBody,
            QWidget#accordionBody > QWidget#accordionContent {{
                background-color: transparent;
            }}
            QWidget#accordionBody QFrame#iconRow,
            QWidget#accordionBody QFrame#iconTintRow,
            QWidget#accordionBody QFrame#soundRow {{
                background-color: transparent;
                border: none;
            }}
        '''))
        for label in self._header.findChildren(QLabel):
            if label.text() == self._title:
                set_theme_style(label, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px;'
                    f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                    f' letter-spacing: {Fonts.TRACK_LABEL}px;'
                    f' background: transparent;'))
            else:
                set_theme_style(label, lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px;'
                    f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                    f' background: transparent;'))
        self._indicator.refresh_theme()

    def toggle(self):
        self._expanded = not self._expanded
        self._header.setChecked(self._expanded)
        self._indicator.setExpanded(self._expanded)
        self._body.setVisible(self._expanded)

    def expand(self):
        if not self._expanded:
            self.toggle()

    def collapse(self):
        if self._expanded:
            self.toggle()

    @property
    def expanded(self) -> bool:
        return self._expanded


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
    section = AccordionSection(title)
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
