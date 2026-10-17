"""Application-wide Qt style setup for the custom Windows UI."""
from __future__ import annotations

import os

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from ui.style import Colors, Fonts, Sizes


def configure_qt_style() -> None:
    """Disable host desktop theming before QApplication snapshots it."""
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    QApplication.setDesktopSettingsAware(False)


def apply_app_style(app: QApplication) -> None:
    """Apply the FTHR palette to native fallback widgets."""
    # Qt wraps the base style in QStyleSheetStyle after QSS is installed;
    # its objectName no longer identifies Fusion. Configure it once per app.
    if not getattr(app, '_fthr_fusion_configured', False):
        if "Fusion" in QStyleFactory.keys():
            app.setStyle(QStyleFactory.create("Fusion"))
        app._fthr_fusion_configured = True

    font = QFont(Fonts.BODY_FAMILY)
    font.setPixelSize(Fonts.SIZE_BODY)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    if app.font() != font:
        app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(Colors.BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(Colors.SURFACE_1))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(Colors.TEXT_MUTED))
    palette.setColor(QPalette.ColorRole.Button, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(Colors.ERROR))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(Colors.ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(Colors.BG))
    if app.palette() != palette:
        app.setPalette(palette)
    # A fallback for bespoke controls and dialogs that only style their color.
    # Local recipes can still override the radius for a specific control.
    sheet = f'''
        QPushButton, QLineEdit, QAbstractSpinBox, QComboBox {{
            border-radius: {Sizes.RADIUS_MD}px;
        }}
        QGroupBox {{ border-radius: {Sizes.RADIUS_CARD}px; }}
    '''
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
    from ui.hover_transition import install_hover_transitions
    install_hover_transitions(app)
