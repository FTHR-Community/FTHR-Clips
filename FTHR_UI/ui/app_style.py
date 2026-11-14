"""Application-wide Qt style setup for the custom Windows UI."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

from ui.style import Colors, Fonts, Sizes, slider_qss, combo_qss


class _SettingsWheelStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint in (QStyle.StyleHint.SH_Widget_Animate,
                    QStyle.StyleHint.SH_Widget_Animation_Duration):
            return 0
        if hint == QStyle.StyleHint.SH_ComboBox_AllowWheelScrolling:
            # Native fallback combos follow the same rule on every page.
            return 0
        return super().styleHint(hint, option, widget, returnData)


def configure_qt_style() -> None:
    """Disable host desktop theming before QApplication snapshots it."""
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    QApplication.setDesktopSettingsAware(False)


def apply_app_style(app: QApplication) -> None:
    """Apply the FTHR palette to native fallback widgets."""
    app.setEffectEnabled(Qt.UIEffect.UI_General, False)
    if not hasattr(app, '_fthr_dropdown_filter'):
        from ui.dropdown_menu import DropdownPopupFilter
        app._fthr_dropdown_filter = DropdownPopupFilter(app)
        app.installEventFilter(app._fthr_dropdown_filter)
    if not hasattr(app, '_fthr_visible_scroll_filter'):
        from ui.wheel_scroll import VisibleScrollWheelFilter
        app._fthr_visible_scroll_filter = VisibleScrollWheelFilter(app)
        app.installEventFilter(app._fthr_visible_scroll_filter)
    # Qt wraps the base style in QStyleSheetStyle after QSS is installed;
    # its objectName no longer identifies Fusion. Configure it once per app.
    if not getattr(app, '_fthr_fusion_configured', False):
        if "Fusion" in QStyleFactory.keys():
            app.setStyle(_SettingsWheelStyle("Fusion"))
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
    sheet = slider_qss() + combo_qss() + f'''
        QPushButton, QLineEdit, QAbstractSpinBox, QComboBox {{
            border-radius: {Sizes.RADIUS_MD}px;
        }}
        QGroupBox {{ border-radius: {Sizes.RADIUS_CARD}px; }}
    '''
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
