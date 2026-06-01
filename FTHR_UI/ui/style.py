"""
FTHR design tokens — the one file that decides what the whole app looks like.

Rule of thumb: NO hardcoded colors, fonts, or magic pixel values anywhere else
in the UI. If you catch yourself typing '#00ffaa' in a widget, stop, and import
it from here instead. The day we rebrand (or a user themes the app), I do not
want to grep 40 files. Everything funnels through here, themes patch it, done.

Vibe check (the actual brand identity):
- Pure black canvas + neutral grays. Matches fthrclips.com. Looks expensive.
- Teal (#00ffaa) is THE accent — hover, active, the recording dot. Use sparingly,
  it loses its punch if everything's teal.
- Oswald for the shouty uppercase labels; Segoe UI for stuff people actually read.
- Sharp 0px corners everywhere on an 8px grid. Rounded corners are for clouds.
"""
from __future__ import annotations


# ─── Palette ──────────────────────────────────────────────────────────
class Colors:
    # Body canvas — pure black, matching fthrclips.com
    BG          = '#000000'
    SURFACE_1   = '#0a0a0a'   # raised — card body, panels
    SURFACE_2   = '#111111'   # raised more — popups, dialogs
    SURFACE_3   = '#1c1c1c'   # hover state, sub-panels

    # Shell surfaces (top bar + status row + filter row).
    SHELL_BG    = '#000000'
    SHELL_BG_2  = '#0a0a0a'   # status row tint, slightly raised
    SHELL_DIVIDER = '#222222'

    # Card surfaces (clip cards in the grid)
    CARD_BG     = '#0a0a0a'
    CARD_BG_HI  = '#111111'   # hover
    CARD_BORDER = '#222222'

    # Hairlines and borders
    HAIRLINE    = '#111111'
    BORDER      = '#222222'
    BORDER_HI   = '#333333'

    # Text
    TEXT        = '#ffffff'
    TEXT_DIM    = '#888888'   # secondary copy on dark
    TEXT_MUTED  = '#555555'   # tertiary / timestamps
    TEXT_GHOST  = '#222222'   # empty-state mark

    # Brand accent (FTHR teal)
    ACCENT      = '#00ffaa'
    ACCENT_DIM  = '#00aa72'
    ACCENT_SOFT = '#0a2218'   # quiet teal background tint for pills

    # Functional states
    ERROR       = '#cc0000'
    SUCCESS     = '#00aa00'
    DELETE      = '#cc0000'

    # Legacy — kept for files that import it.
    BAR_BG      = '#000000'
    BAR_FG      = '#ffffff'
    BAR_FG_DIM  = '#888888'
    BAR_HAIRLINE = '#222222'


# ─── Type ─────────────────────────────────────────────────────────────
class Fonts:
    # Display face — Oswald is bundled in fonts/. Fallback chain stays graceful.
    DISPLAY = '"Oswald", "Bahnschrift", "Segoe UI", sans-serif'
    BODY    = '"Segoe UI", "Inter", sans-serif'

    # Modular scale, ratio ≈1.2, anchored at 11px body
    SIZE_MICRO  = 9
    SIZE_LABEL  = 10
    SIZE_BODY   = 11
    SIZE_BODY_L = 13
    SIZE_H3     = 16
    SIZE_H2     = 22
    SIZE_H1     = 32

    # Tracking presets (px)
    TRACK_LABEL   = 2
    TRACK_DISPLAY = 4
    TRACK_HEADING = 6


# ─── Geometry ─────────────────────────────────────────────────────────
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
    UNIFIED_BAR_H = 56 # unified top bar that merges header + status row
    FILTER_H    = 52   # all-clips / filter / sort row
    STATS_BAR_H = 28
    BUTTON_H    = 32
    INPUT_H     = 32
    HW_BAR_H    = 36

    # Radius — sharp corners everywhere, matching fthrclips.com
    RADIUS      = 0
    RADIUS_SM   = 0
    RADIUS_MD   = 0
    RADIUS_CARD = 0
    RADIUS_PILL = 0

    # Borders
    BORDER_W   = 1
    HAIRLINE_W = 1


# ─── Inline label helpers ─────────────────────────────────────────────
def label_display(
    color: str = Colors.TEXT,
    size: int = Fonts.SIZE_H2,
    tracking: int = Fonts.TRACK_DISPLAY,
) -> str:
    """Oswald display label — for page titles and brand marks."""
    return (
        f'color: {color}; font-size: {size}px; font-weight: bold;'
        f' font-family: {Fonts.DISPLAY}; letter-spacing: {tracking}px;'
        f' background: transparent;'
    )


def label_uppercase(
    color: str = Colors.TEXT,
    size: int = Fonts.SIZE_LABEL,
    tracking: int = Fonts.TRACK_LABEL,
) -> str:
    """Oswald uppercase tracking label — for group titles, section headers."""
    return (
        f'color: {color}; font-size: {size}px; font-weight: bold;'
        f' font-family: {Fonts.DISPLAY}; letter-spacing: {tracking}px;'
        f' background: transparent;'
    )


def label_body(color: str = Colors.TEXT, size: int = Fonts.SIZE_BODY) -> str:
    """Segoe UI body text."""
    return (
        f'color: {color}; font-size: {size}px;'
        f' font-family: {Fonts.BODY};'
        f' background: transparent;'
    )


# ─── Status pill (shown in the status row, beneath the header) ────────
def _pill_base() -> str:
    return (
        f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
        f' letter-spacing: {Fonts.TRACK_LABEL}px; font-weight: bold;'
        f' padding: 5px 14px; border-radius: {Sizes.RADIUS_PILL}px;'
    )


# Capturing — teal pill, dark text (the "live, recording" state)
STATUS_ACTIVE  = _pill_base() + (
    f' color: {Colors.BG}; background: {Colors.ACCENT};'
)
# Idle / connecting — quiet teal-on-dark pill
STATUS_IDLE    = _pill_base() + (
    f' color: {Colors.ACCENT}; background: {Colors.ACCENT_SOFT};'
    f' border: 1px solid {Colors.ACCENT_SOFT};'
)
# Warning / disconnected — error tone
STATUS_WARNING = _pill_base() + (
    f' color: {Colors.ERROR}; background: transparent;'
    f' border: 1px solid {Colors.ERROR};'
)


# ─── Reusable QSS fragments ───────────────────────────────────────────
COMBO_QSS = f'''
    QComboBox {{
        background-color: {Colors.SURFACE_2};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 6px 12px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        min-width: 90px;
        min-height: 22px;
    }}
    QComboBox:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.TEXT};
    }}
    QComboBox:focus {{
        border-color: {Colors.ACCENT};
        outline: none;
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; border: none; }}
    QComboBox QAbstractItemView {{
        background-color: {Colors.SURFACE_2};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        selection-background-color: {Colors.SURFACE_3};
        selection-color: {Colors.ACCENT};
        padding: 4px;
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        background-color: {Colors.SURFACE_2};
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


BUTTON_PRIMARY_QSS = f'''
    QPushButton {{
        background-color: {Colors.ACCENT};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.BG};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
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


BUTTON_OUTLINE_QSS = f'''
    QPushButton {{
        background-color: transparent;
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
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


# Secondary "Export" button — solid mid-tone surface
BUTTON_SECONDARY_QSS = f'''
    QPushButton {{
        background-color: {Colors.SURFACE_3};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
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


# "Join Community" / "Get Invite" rounded pill
BUTTON_PILL_QSS = f'''
    QPushButton {{
        background-color: {Colors.ACCENT_SOFT};
        border: none;
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.ACCENT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 16px;
    }}
    QPushButton:hover {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
    }}
'''


BUTTON_PILL_GHOST_QSS = f'''
    QPushButton {{
        background-color: transparent;
        border: 1px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.TEXT_DIM};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
'''


BUTTON_GHOST_QSS = f'''
    QPushButton {{
        background-color: transparent;
        border: none;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 6px 10px;
    }}
    QPushButton:hover {{
        color: {Colors.ACCENT};
    }}
'''


SLIDER_QSS = f'''
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
        border-radius: 0px;
    }}
    QSlider::handle:horizontal:hover {{ background: {Colors.TEXT}; }}
    QSlider::handle:horizontal:pressed {{ background: {Colors.TEXT}; }}
'''


CHECKBOX_QSS = f'''
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
        border-radius: 0px;
    }}
    QCheckBox::indicator:hover {{
        border-color: {Colors.ACCENT};
    }}
    QCheckBox::indicator:checked {{
        background-color: {Colors.ACCENT};
        border-color: {Colors.ACCENT};
    }}
'''


GROUPBOX_QSS = f'''
    QGroupBox {{
        background-color: {Colors.SURFACE_1};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        margin-top: 16px;
        padding: 22px 18px 16px 18px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-weight: bold;
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
        border-radius: 0px;
    }}
    QScrollBar::handle:vertical:hover {{ background-color: {Colors.ACCENT}; }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{ height: 0px; background: none; border: none; }}
    QScrollBar::add-page:vertical,
    QScrollBar::sub-page:vertical {{ background: none; }}
'''

SCROLLBAR_QSS = scrollbar_qss()


# ─── New: card-shape primitives (for clip cards in the grid) ──────────
CARD_QSS = f'''
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


# ─── New: collapsible "details" panel (used in clip viewer sidebar) ──
DETAILS_PANEL_QSS = f'''
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
        font-weight: bold;
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

TOOLTIP_QSS = tooltip_qss()


LINEEDIT_QSS = f'''
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


RADIOBUTTON_QSS = f'''
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


CONTEXT_MENU_QSS = f'''
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
