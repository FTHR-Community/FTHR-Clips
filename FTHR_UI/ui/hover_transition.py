"""Short hover fades for styled controls, without changing their input or layout."""
from PySide6.QtCore import QEasingCurve, QEvent, QObject, Qt, QVariantAnimation
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QLineEdit,
    QMenu, QScrollBar, QSlider, QStyle, QStyleOptionSlider, QWidget,
)
from shiboken6 import isValid
from ui.motion import reduce_motion


class _HoverFade(QWidget):
    def __init__(self, control, frame, rect):
        super().__init__(control)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(rect)
        self._frame = frame
        self._opacity = 1.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(120)
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self.dispose)

    def _advance(self, opacity):
        self._opacity = opacity
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(self.rect(), self._frame)

    def dispose(self):
        control = self.parentWidget()
        if control is not None and getattr(control, '_hover_fade', None) is self:
            control._hover_fade = None
        self.animation.stop()
        self.hide()
        self.deleteLater()


class HoverTransitions(QObject):
    """Blend the previous appearance over Qt's new hover appearance for 120 ms."""
    _controls = (QAbstractButton, QComboBox, QLineEdit, QAbstractSpinBox,
                 QSlider, QScrollBar)
    _events = frozenset((QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseMove,
                        QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.EnabledChange,
                        QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                        QEvent.Type.Wheel, QEvent.Type.StyleChange))

    def __init__(self, app):
        super().__init__(app)
        self._capturing = False
        app.installEventFilter(self)

    def eventFilter(self, control, event):
        kind = event.type()
        if self._capturing or kind not in self._events or not isinstance(control, QWidget):
            return False
        if control.property('nativeHoverAnimation'):
            return False
        view = control.parentWidget()
        item_view = isinstance(view, QAbstractItemView) and view.viewport() is control
        supported = (isinstance(control, self._controls)
                     or isinstance(control, QMenu) or item_view
                     or control.objectName() == 'clipCard')
        if not supported or reduce_motion(control):
            return False
        fade = getattr(control, '_hover_fade', None)
        if fade is not None and not isValid(fade):
            control._hover_fade = fade = None
        if kind in (QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.EnabledChange,
                    QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                    QEvent.Type.Wheel, QEvent.Type.StyleChange):
            if fade is not None:
                fade.dispose()
            if kind == QEvent.Type.Hide:
                control._hover_target = None
                control._hover_target_rect = None
            return False
        if isinstance(control, (QSlider, QScrollBar)) and kind == QEvent.Type.MouseMove:
            option = QStyleOptionSlider()
            control.initStyleOption(option)
            complex_control = (QStyle.ComplexControl.CC_ScrollBar if isinstance(control, QScrollBar)
                               else QStyle.ComplexControl.CC_Slider)
            target = control.style().hitTestComplexControl(
                complex_control, option, event.position().toPoint(), control)
            if target != getattr(control, '_hover_subcontrol', None):
                control._hover_subcontrol = target
                self._begin_fade(control, control.rect(), fade)
            return False
        if isinstance(control, QMenu) or item_view:
            if kind == QEvent.Type.MouseMove and control.isVisible():
                pos = event.position().toPoint()
                target = control.actionAt(pos) if isinstance(control, QMenu) else view.indexAt(pos)
                previous = getattr(control, '_hover_target', None)
                if target != previous:
                    old_rect = getattr(control, '_hover_target_rect', None)
                    if isinstance(control, QMenu):
                        rect = control.actionGeometry(target) if target is not None else (old_rect or control.rect())
                    else:
                        rect = view.visualRect(target) if target.isValid() else (old_rect or control.rect())
                    control._hover_target = target
                    control._hover_target_rect = rect
                    self._begin_fade(control, rect.united(old_rect) if old_rect else rect, fade)
            elif kind == QEvent.Type.Leave:
                control._hover_target = None
            return False
        if (kind not in (QEvent.Type.Enter, QEvent.Type.Leave)
                or not control.isVisible() or not control.isEnabled()):
            return False
        self._begin_fade(control, control.rect(), fade, kind)
        return False

    def _begin_fade(self, control, rect, fade, kind=None):
        # Skip large rendering surfaces. Only hovered controls get an overlay;
        # no timer, effect or snapshot is retained once the fade completes.
        if rect.width() * rect.height() * control.devicePixelRatioF() ** 2 > 1_000_000:
            return
        self._capturing = True
        try:
            under_mouse = control.testAttribute(Qt.WidgetAttribute.WA_UnderMouse)
            if kind is not None:
                control.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, kind == QEvent.Type.Leave)
            frame = control.grab(rect)
            control.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, under_mouse)
            if fade is not None:
                fade.dispose()
            fade = _HoverFade(control, frame, rect)
            control._hover_fade = fade
            fade.show()
            fade.raise_()
            fade.animation.start()
        finally:
            self._capturing = False


def install_hover_transitions(app):
    if not hasattr(app, '_hover_transitions'):
        app._hover_transitions = HoverTransitions(app)
