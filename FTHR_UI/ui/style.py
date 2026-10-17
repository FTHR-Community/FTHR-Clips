"""Shared color, font, and spacing tokens for FTHR widgets.

Near-black green surfaces, a bright mint accent, and a clear type hierarchy.
Imported themes can override fonts and colors.
"""
from __future__ import annotations


from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QComboBox, QPushButton, QWidget

from core.theme_manager import DEFAULT_COLORS, DEFAULT_FONTS


# Palette
class Colors:
    BG          = DEFAULT_COLORS['BG']
    SURFACE_1   = DEFAULT_COLORS['SURFACE_1']
    SURFACE_2   = DEFAULT_COLORS['SURFACE_2']
    SURFACE_3   = DEFAULT_COLORS['SURFACE_3']

    # Shell surfaces (top bar + status row + filter row).
    SHELL_BG    = DEFAULT_COLORS['SHELL_BG']
    SHELL_BG_2  = DEFAULT_COLORS['SHELL_BG_2']
    SHELL_DIVIDER = DEFAULT_COLORS['SHELL_DIVIDER']

    # Card surfaces (clip cards in the grid)
    CARD_BG     = DEFAULT_COLORS['CARD_BG']
    CARD_BG_HI  = DEFAULT_COLORS['CARD_BG_HI']
    CARD_BORDER = DEFAULT_COLORS['CARD_BORDER']

    # Hairlines and borders
    HAIRLINE    = DEFAULT_COLORS['HAIRLINE']
    BORDER      = DEFAULT_COLORS['BORDER']
    BORDER_HI   = DEFAULT_COLORS['BORDER_HI']

    # Text
    TEXT        = DEFAULT_COLORS['TEXT']
    TEXT_DIM    = DEFAULT_COLORS['TEXT_DIM']
    TEXT_MUTED  = DEFAULT_COLORS['TEXT_MUTED']
    TEXT_GHOST  = DEFAULT_COLORS['TEXT_GHOST']

    # Brand accent (FTHR teal)
    ACCENT      = DEFAULT_COLORS['ACCENT']
    ACCENT_DIM  = DEFAULT_COLORS['ACCENT_DIM']
    ACCENT_SOFT = DEFAULT_COLORS['ACCENT_SOFT']

    # Functional states
    ERROR       = DEFAULT_COLORS['ERROR']
    ERROR_SOFT  = DEFAULT_COLORS['ERROR_SOFT']
    WARNING     = DEFAULT_COLORS['WARNING']
    SUCCESS     = DEFAULT_COLORS['SUCCESS']
    DELETE      = DEFAULT_COLORS['DELETE']

    # Legacy — kept for files that import it.
    BAR_BG      = SHELL_BG
    BAR_FG      = TEXT
    BAR_FG_DIM  = TEXT_DIM
    BAR_HAIRLINE = SHELL_DIVIDER


# Type
class Fonts:
    # Bundled Oswald Regular, SemiBold, and Bold provide the full hierarchy.
    # Qt resolves the platform fallbacks if an imported font is unavailable.
    BRAND = '"Oswald", "Bahnschrift", sans-serif'
    DEFAULT_DISPLAY_FAMILY = DEFAULT_FONTS['display']
    DEFAULT_BODY_FAMILY = DEFAULT_FONTS['body']
    DISPLAY_FAMILY = DEFAULT_DISPLAY_FAMILY
    BODY_FAMILY = DEFAULT_BODY_FAMILY
    DISPLAY = '"Oswald", "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif'
    BODY    = '"Oswald", "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif'

    # Compact desktop scale, with hierarchy carried by size and weight.
    SIZE_MICRO  = 12
    SIZE_LABEL  = 14
    SIZE_BODY   = 15
    SIZE_BODY_L = 17
    SIZE_TOOLBAR = 15
    SIZE_H3     = 20
    SIZE_H2     = 26
    SIZE_H1     = 36

    # Tracking presets (px)
    TRACK_LABEL   = 0
    TRACK_DISPLAY = 0
    TRACK_HEADING = 0

    @classmethod
    def configure(cls, display_family: str = '', body_family: str = '') -> None:
        """Apply the font families stored in the current exported theme."""
        cls.DISPLAY_FAMILY = str(display_family or cls.DEFAULT_DISPLAY_FAMILY)
        cls.BODY_FAMILY = str(body_family or cls.DEFAULT_BODY_FAMILY)

        def _quoted(family: str) -> str:
            return '"' + family.replace('"', '') + '"'

        cls.DISPLAY = (
            f'{_quoted(cls.DISPLAY_FAMILY)}, "Segoe UI", '
            '"Noto Sans", "DejaVu Sans", sans-serif'
        )
        cls.BODY = (
            f'{_quoted(cls.BODY_FAMILY)}, "Segoe UI", "Noto Sans", '
            '"DejaVu Sans", "Ubuntu", "Cantarell", sans-serif'
        )


def themed_dropdown_pixmap(size: int = 14) -> QPixmap:
    """Return the current themed dropdown arrow at the requested size."""
    from ui.icons import icon_pixmap
    return icon_pixmap('dropdown.png', size)


def paint_dropdown_arrow(
        painter: QPainter,
        rect: QRect,
        expanded: bool = False,
        size: int = 14,
        right_padding: int = 8,
) -> None:
    """Paint the shared dropdown arrow inside *rect*."""
    pixmap = themed_dropdown_pixmap(size)
    if pixmap.isNull():
        return

    x = rect.right() - size - right_padding + 1
    y = rect.top() + (rect.height() - size) // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if expanded:
        painter.translate(x + size / 2.0, y + size / 2.0)
        painter.rotate(180)
        painter.drawPixmap(QRect(-size // 2, -size // 2, size, size), pixmap)
    else:
        painter.drawPixmap(x, y, pixmap)
    painter.restore()


class ThemedDropdownArrow(QWidget):
    """Small standalone dropdown indicator for custom expandable controls."""

    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self._size = size
        self._expanded = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.update()

    def refresh_theme(self) -> None:
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        del event
        painter = QPainter(self)
        paint_dropdown_arrow(
            painter, self.rect(), self._expanded,
            size=self._size, right_padding=0)
        painter.end()


class ThemedDropdownButton(QPushButton):
    """Push button with the shared dropdown arrow and open-state rotation."""

    def __init__(self, text: str = '', parent=None):
        self._dropdown_expanded = False
        self._dropdown_text = ''
        super().__init__(parent)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API name
        self._dropdown_text = str(text)
        super().setText(self._dropdown_text)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._dropdown_expanded == expanded:
            return
        self._dropdown_expanded = expanded
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        super().paintEvent(event)
        painter = QPainter(self)
        paint_dropdown_arrow(painter, self.rect(), self._dropdown_expanded)
        painter.end()


class WheelSafeComboBox(QComboBox):
    """A combo whose closed value cannot be changed by incidental scrolling.

    Ignoring the event lets the surrounding settings scroll area consume it.
    The popup view still receives wheel events normally while it is open.
    """

    # Specialized dropdowns that rotate the arrow while open handle the same
    # paint step themselves; ordinary settings combos use this shared path.
    _custom_arrow_managed = False

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        super().paintEvent(event)
        if self._custom_arrow_managed:
            return

        painter = QPainter(self)
        paint_dropdown_arrow(painter, self.rect())
        painter.end()

    def wheelEvent(self, event):  # noqa: N802 - Qt API name
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


def retarget_widget_font_styles(root, old_display: str, old_body: str) -> None:
    """Refresh already-created inline QSS after a theme font change."""
    if old_display == Fonts.DISPLAY and old_body == Fonts.BODY:
        return
    widgets = [root, *root.findChildren(QWidget)] if root is not None else []
    for widget in widgets:
        style_getter = getattr(widget, 'styleSheet', None)
        style_setter = getattr(widget, 'setStyleSheet', None)
        if not callable(style_getter) or not callable(style_setter):
            continue
        sheet = style_getter()
        if not sheet:
            continue
        updated = sheet.replace(old_display, Fonts.DISPLAY)
        updated = updated.replace(old_body, Fonts.BODY)
        if updated != sheet:
            style_setter(updated)


def set_theme_style(widget: QWidget, factory) -> None:
    """Keep the token-based recipe so an existing control can change themes.

    Replacing old hex values loses token identity when two tokens happen to
    share a color. Re-evaluate the recipe instead, preserving widget state.
    """
    widget._theme_style_factory = factory
    sheet = factory()
    if widget.styleSheet() != sheet:
        widget.setStyleSheet(sheet)


def refresh_theme_styles(root: QWidget) -> None:
    for widget in [root, *root.findChildren(QWidget)]:
        factory = getattr(widget, '_theme_style_factory', None)
        if factory is not None:
            sheet = factory()
            if widget.styleSheet() != sheet:
                widget.setStyleSheet(sheet)


# Geometry
class Sizes:
    # 8-px grid
    SPACE_2  = 4
    SPACE_3  = 8
    SPACE_4  = 12
    SPACE_5  = 16
    SPACE_6  = 20
    SPACE_7  = 24
    SPACE_8  = 32
    SPACE_9  = 48

    # Heights
    TOP_BAR_H   = 52
    HEADER_H    = 60   # logo + community + account row
    STATUS_H    = 48   # capture-status row
    TITLE_BAR_H = 36
    CAPTURE_BAR_H = 64
    CAPTURE_CONTROL_H = 44
    UNIFIED_BAR_H = TITLE_BAR_H  # compatibility for window controls
    FILTER_H    = 60
    STATS_BAR_H = 28
    BUTTON_H    = 38
    INPUT_H     = 38
    HW_BAR_H    = 36

    # Subtle rounding keeps dense controls and cards easy to distinguish.
    RADIUS      = 6
    RADIUS_SM   = 4
    RADIUS_MD   = 6
    RADIUS_CARD = 8
    RADIUS_PILL = 6

    PAGE_INSET = SPACE_7
    ROW_GAP = SPACE_4
    SECTION_GAP = SPACE_7

    # Borders
    BORDER_W   = 1
    HAIRLINE_W = 1


# Inline label helpers
def label_display(
    color: str | None = None,
    size: int = Fonts.SIZE_H2,
    tracking: int = Fonts.TRACK_DISPLAY,
) -> str:
    """Semibold page title with natural spacing."""
    color = Colors.TEXT if color is None else color
    return (
        f'color: {color}; font-size: {size}px; font-weight: 600;'
        f' font-family: {Fonts.DISPLAY}; letter-spacing: {tracking}px;'
        f' background: transparent;'
    )


def label_uppercase(
    color: str | None = None,
    size: int = Fonts.SIZE_LABEL,
    tracking: int = Fonts.TRACK_LABEL,
) -> str:
    """Compact semibold label. Kept under its old name for existing imports."""
    color = Colors.TEXT if color is None else color
    return (
        f'color: {color}; font-size: {size}px; font-weight: 600;'
        f' font-family: {Fonts.BODY}; letter-spacing: 0px;'
        f' background: transparent;'
    )


def label_body(color: str | None = None, size: int = Fonts.SIZE_BODY) -> str:
    """Regular-weight interface text in the chosen theme font."""
    color = Colors.TEXT if color is None else color
    return (
        f'color: {color}; font-size: {size}px;'
        f' font-family: {Fonts.BODY}; font-weight: 400; letter-spacing: 0px;'
        f' background: transparent;'
    )


# Capture status text (shown in the top bar)
def _status_text_base() -> str:
    return (
        f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.BODY};'
        f' letter-spacing: 0px; font-weight: normal;'
        f' background: transparent; border: none; padding: 0 2px;'
    )


def status_active_qss() -> str:
    return _status_text_base() + f' color: {Colors.ACCENT};'

def status_idle_qss() -> str:
    return _status_text_base() + f' color: {Colors.TEXT_MUTED};'

def status_warning_qss() -> str:
    return _status_text_base() + f' color: {Colors.ERROR};'


# Reusable QSS fragments
def combo_qss(background: str | None = None) -> str:
    """Return the shared combo style, with an optional non-default surface."""
    combo_background = background or Colors.SURFACE_3
    return f'''
    QComboBox {{
        combobox-popup: 0;
        background-color: {combo_background};
        border: 1px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 6px 12px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        min-width: 90px;
        min-height: 22px;
    }}
    QComboBox:hover {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.TEXT};
    }}
    QComboBox:focus {{
        background-color: {Colors.SURFACE_3};
        outline: none;
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; border: none; }}
    QComboBox QAbstractItemView {{
        background-color: {combo_background};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        selection-background-color: {Colors.SURFACE_3};
        selection-color: {Colors.ACCENT};
        padding: 0px;
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        background-color: {combo_background};
        color: {Colors.TEXT};
        min-height: 26px;
        padding-left: 10px;
        border-radius: 0px;
    }}
    QComboBox QAbstractItemView::item:hover {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.TEXT};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.ACCENT};
    }}
'''


def button_primary_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.ACCENT};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.BG};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 10px 18px;
    }}
    QPushButton:hover {{
        background-color: {Colors.TEXT};
    }}
    QPushButton:pressed {{
        background-color: {Colors.ACCENT_DIM};
        color: {Colors.TEXT};
    }}
    QPushButton:disabled {{
        background-color: {Colors.BORDER};
        color: {Colors.TEXT_MUTED};
    }}
'''


def button_outline_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 9px 16px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
    QPushButton:pressed {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
        border-color: {Colors.ACCENT};
    }}
'''


def button_secondary_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.SURFACE_3};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 10px 18px;
    }}
    QPushButton:hover {{
        background-color: {Colors.BORDER};
        border-color: {Colors.BORDER_HI};
    }}
    QPushButton:pressed {{
        background-color: {Colors.SURFACE_2};
    }}
'''


def button_pill_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.ACCENT_SOFT};
        border: none;
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.ACCENT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 16px;
    }}
    QPushButton:hover {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
    }}
'''


def button_pill_ghost_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: 1px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.TEXT_DIM};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
'''


def button_ghost_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: none;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 6px 10px;
    }}
    QPushButton:hover {{
        color: {Colors.ACCENT};
    }}
'''


def slider_qss() -> str:
    return f'''
    QSlider {{
        background: transparent;
    }}
    QSlider::groove:horizontal {{
        background: {Colors.BORDER};
        height: 2px;
        border: none;
    }}
    QSlider::sub-page:horizontal {{ background: {Colors.ACCENT}; }}
    QSlider::add-page:horizontal {{ background: {Colors.BORDER}; }}
    QSlider::handle:horizontal {{
        background: {Colors.ACCENT};
        width: 12px; height: 12px;
        margin: -5px 0;
        border: none;
        border-radius: {Sizes.RADIUS_SM}px;
    }}
    QSlider::handle:horizontal:hover {{ background: {Colors.TEXT}; }}
    QSlider::handle:horizontal:pressed {{ background: {Colors.TEXT}; }}
'''


def checkbox_qss() -> str:
    return f'''
    QCheckBox {{
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY_L}px;
        font-family: {Fonts.BODY};
        spacing: 10px;
        background: transparent;
    }}
    QCheckBox::indicator {{
        width: 14px; height: 14px;
        border: {Sizes.BORDER_W}px solid {Colors.TEXT};
        background-color: {Colors.BG};
        border-radius: {Sizes.RADIUS_SM}px;
    }}
    QCheckBox::indicator:hover {{
        border-color: {Colors.ACCENT};
    }}
    QCheckBox::indicator:checked {{
        background-color: {Colors.ACCENT};
        border-color: {Colors.ACCENT};
    }}
'''


def groupbox_qss() -> str:
    return f'''
    QGroupBox {{
        background-color: {Colors.SURFACE_1};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        margin-top: 16px;
        padding: 22px 18px 16px 18px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-weight: 600;
        font-family: {Fonts.DISPLAY};
        letter-spacing: {Fonts.TRACK_LABEL}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 14px;
        padding: 0 8px;
        color: {Colors.ACCENT};
        background-color: {Colors.BG};
    }}
'''


def scrollbar_qss() -> str:
    return f'''
    QScrollArea {{ border: none; background-color: {Colors.BG}; }}
    QScrollBar:vertical {{
        background-color: {Colors.BG};
        width: 8px;
        margin: 4px 0;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background-color: {Colors.BORDER_HI};
        min-height: 32px;
        border-radius: {Sizes.RADIUS_SM}px;
    }}
    QScrollBar::handle:vertical:hover {{ background-color: {Colors.ACCENT}; }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{ height: 0px; background: none; border: none; }}
    QScrollBar::add-page:vertical,
    QScrollBar::sub-page:vertical {{ background: none; }}
    QScrollBar:horizontal {{
        background-color: {Colors.SHELL_BG_2};
        height: 8px;
        border: none;
    }}
    QScrollBar::handle:horizontal {{ background: {Colors.BORDER_HI}; min-width: 48px; border-radius: {Sizes.RADIUS_SM}px; }}
    QScrollBar::handle:horizontal:hover {{ background: {Colors.ACCENT}; }}
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{ width: 0; background: none; border: none; }}
    QScrollBar::add-page:horizontal,
    QScrollBar::sub-page:horizontal {{ background: none; }}
'''


def card_qss() -> str:
    return f'''
    QFrame#clipCard {{
        background-color: {Colors.CARD_BG};
        border: 1px solid {Colors.CARD_BORDER};
        border-radius: {Sizes.RADIUS_CARD}px;
    }}
    QFrame#clipCard:hover {{
        background-color: {Colors.CARD_BG_HI};
        border-color: {Colors.BORDER_HI};
    }}
'''


def details_panel_qss() -> str:
    return f'''
    QFrame#detailsPanel {{
        background-color: {Colors.SURFACE_1};
        border: 1px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
    }}
    QPushButton#panelHeader {{
        background-color: transparent;
        border: none;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 12px 14px;
        text-align: left;
    }}
    QPushButton#panelHeader:hover {{ color: {Colors.ACCENT}; }}
'''


def tooltip_qss() -> str:
    return f'''
    QToolTip {{
        background-color: {Colors.BG};
        border: {Sizes.BORDER_W}px solid {Colors.ACCENT};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        padding: 4px 8px;
    }}
'''


def lineedit_qss() -> str:
    return f'''
    QLineEdit {{
        background-color: {Colors.SURFACE_2};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 6px 10px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        selection-background-color: {Colors.ACCENT};
        selection-color: {Colors.BG};
    }}
    QLineEdit:hover {{
        border-color: {Colors.BORDER_HI};
    }}
    QLineEdit:focus {{
        border-color: {Colors.ACCENT};
        outline: none;
    }}
    QLineEdit:disabled {{
        background-color: {Colors.SURFACE_1};
        color: {Colors.TEXT_MUTED};
        border-color: {Colors.BORDER};
    }}
'''


def radiobutton_qss() -> str:
    return f'''
    QRadioButton {{
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY_L}px;
        font-family: {Fonts.BODY};
        spacing: 10px;
        background: transparent;
    }}
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: {Sizes.BORDER_W}px solid {Colors.TEXT};
        background-color: {Colors.BG};
        border-radius: 7px;
    }}
    QRadioButton::indicator:hover {{
        border-color: {Colors.ACCENT};
    }}
    QRadioButton::indicator:checked {{
        background-color: {Colors.ACCENT};
        border-color: {Colors.ACCENT};
    }}
'''


def context_menu_qss() -> str:
    return f'''
    QMenu {{
        background-color: {Colors.SURFACE_1};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        padding: 4px 0;
    }}
    QMenu::item {{ padding: 8px 22px; }}
    QMenu::item:selected {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.ACCENT};
    }}
    QMenu::separator {{
        height: 1px;
        background: {Colors.BORDER};
        margin: 4px 0;
    }}
'''
