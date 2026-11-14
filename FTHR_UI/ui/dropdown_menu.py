"""Shared rounded popup rows for native and FTHR combo boxes."""
from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QRegion
from PySide6.QtWidgets import QComboBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem
from ui.style import Colors, Sizes


class DropdownItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + 24, max(Sizes.COMBO_ITEM_H + 8, size.height()))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = opt.rect.adjusted(3, 2, -3, -2)
        enabled = bool(opt.state & QStyle.StateFlag.State_Enabled)
        selected = bool(opt.state & (QStyle.StateFlag.State_Selected |
                                     QStyle.StateFlag.State_MouseOver))
        if selected and enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Colors.CARD_BG_HI))
            painter.drawRoundedRect(rect, Sizes.RADIUS_SM, Sizes.RADIUS_SM)
        text_rect = rect.adjusted(10, 0, -10, 0)
        if not opt.icon.isNull():
            extent = max(16, min(24, opt.decorationSize.width()))
            icon_rect = text_rect.adjusted(0, (text_rect.height() - extent) // 2, 0, 0)
            icon_rect.setSize(QSize(extent, extent))
            opt.icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter,
                           QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled)
            text_rect.setLeft(icon_rect.right() + 10)
        painter.setFont(opt.font)
        painter.setPen(QColor(Colors.TEXT if enabled else Colors.TEXT_MUTED))
        text = opt.fontMetrics.elidedText(opt.text, Qt.TextElideMode.ElideRight,
                                         max(0, text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.restore()


class DropdownPopupFilter(QObject):
    @staticmethod
    def _round_popup(popup):
        path = QPainterPath()
        path.addRoundedRect(QRectF(popup.rect()), Sizes.RADIUS_MD, Sizes.RADIUS_MD)
        popup.setMask(QRegion(path.toFillPolygon().toPolygon()))

    @staticmethod
    def _fit_rows(popup):
        combo = popup.parentWidget()
        if not isinstance(combo, QComboBox) or not popup.isVisible():
            return
        view = combo.view()
        if combo.count() > combo.maxVisibleItems():
            return
        needed = sum(view.sizeHintForRow(row) for row in range(combo.count()))
        extra = needed - view.viewport().height()
        # Qt sizes the container before its styled border is applied. Recover
        # that inset so a short menu doesn't acquire a needless scrollbar.
        if 0 < extra <= 16:
            screen = popup.screen().availableGeometry()
            height = min(popup.height() + extra, screen.height())
            popup.resize(popup.width(), height)
            if popup.geometry().bottom() > screen.bottom():
                popup.move(popup.x(), screen.bottom() - height + 1)

    def eventFilter(self, watched, event):
        if isinstance(watched, QComboBox) and event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
            if not getattr(watched, '_fthr_popup_styled', False):
                watched._fthr_popup_styled = True
                view = watched.view()
                view.setItemDelegate(DropdownItemDelegate(view))
                view.setMouseTracking(True)
        if (event.type() in (QEvent.Type.Show, QEvent.Type.Resize)
                and watched.metaObject().className() == 'QComboBoxPrivateContainer'):
            watched.setObjectName('fthrDropdownPopup')
            sheet = f'''
                QFrame#fthrDropdownPopup {{ background: {Colors.SURFACE_2};
                    border: 1px solid {Colors.BORDER_HI}; border-radius: {Sizes.RADIUS_MD}px; }}
            '''
            if event.type() == QEvent.Type.Show and watched.styleSheet() != sheet:
                watched.setStyleSheet(sheet)
            if event.type() == QEvent.Type.Show:
                QTimer.singleShot(0, watched, lambda: self._fit_rows(watched))
            # Apply after Qt's resize handler, which may reset the native mask.
            QTimer.singleShot(0, watched, lambda: self._round_popup(watched))
        return super().eventFilter(watched, event)
