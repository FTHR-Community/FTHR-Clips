"""Shared toolbar popup shell and hotkey binding controls."""

import sys

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.hotkey_manager import (
    CONTROLLER_BUTTON_ORDER,
    HotkeyManager,
    custom_clip_seconds,
    format_controller_combo,
    format_keyboard_combo,
    normalize_controller_combo,
    normalize_keyboard_combo,
)
from core.window_geometry import popup_rect_for_anchor
from ui.style import Colors, Fonts, Sizes, WheelSafeComboBox, combo_qss, label_uppercase, set_theme_style


class PopupPanel(QFrame):
    """Base class for top-bar dropdown panels (capture settings, source, hotkeys)."""

    popup_hidden = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('popupPanel')
        self.setMinimumWidth(300)
        # Do not dispatch to a subclass override before that subclass has
        # created its child widgets.
        self._apply_popup_style()

    def _apply_popup_style(self):
        set_theme_style(self, lambda: (f'''
            QFrame#popupPanel {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QFrame#popupPanel QLabel {{
                color: {Colors.TEXT};
                background-color: transparent;
            }}
            QFrame#popupPanel[attached="true"] {{
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
            }}
        '''))

    def refresh_theme(self):
        """Refresh the shared popup shell after a theme change."""
        self._apply_popup_style()

    def hideEvent(self, event):  # noqa: N802 - Qt API name
        """Tell the trigger button when Qt closes the popup for any reason.

        Popup windows are also hidden by Qt when the user clicks outside them.
        That path does not pass through MainWindow's toggle handlers, so the
        trigger arrow needs the popup's actual visibility transition.
        """
        super().hideEvent(event)
        button = getattr(self, '_attached_button', None)
        if button is not None and hasattr(button, 'setExpanded'):
            button.setExpanded(False)
        self.popup_hidden.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        button = getattr(self, '_attached_button', None)
        if button is None or not self.isVisible():
            return
        anchor = button.mapToGlobal(QPoint(0, button.height()))
        if abs(self.y() - anchor.y()) > 2:
            return  # A screen-edge fallback may place the panel above its button.
        left = max(1, anchor.x() - self.x() + 1)
        right = min(self.width() - 1, anchor.x() - self.x() + button.width() - 1)
        if right > left:
            painter = QPainter(self)
            painter.fillRect(QRect(left, 0, right - left, 1), QColor(Colors.SURFACE_2))

    def show_below(self, button: QWidget):
        self._attached_button = button
        self.setProperty('attached', True)
        self.style().unpolish(self)
        self.style().polish(self)
        if hasattr(button, 'setExpanded'):
            button.setExpanded(True)
        self.adjustSize()
        anchor_pos = button.mapToGlobal(QPoint(0, 0))
        pos = button.mapToGlobal(QPoint(0, button.height()))
        x = pos.x()
        y = pos.y()
        area = None
        margin = 8

        # Popups are wider than several top-bar buttons. Anchor from the left
        # while there is room, then right-align to the trigger and clamp to the
        # usable screen. This keeps the panel visible in maximized/fullscreen
        # windows and on secondary monitors with non-zero origins.
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()

            max_w = max(160, area.width() - margin * 2)
            max_h = max(120, area.height() - margin * 2)
            if self.width() > max_w or self.height() > max_h:
                self.resize(min(self.width(), max_w), min(self.height(), max_h))
            anchor = QRect(
                anchor_pos.x(), anchor_pos.y(),
                button.width(), button.height())
            placed = popup_rect_for_anchor(
                anchor, self.width(), self.height(), area, margin=margin)
            x, y = placed.x(), placed.y()
            if placed.size() != self.size():
                self.resize(placed.size())

        self.move(QPoint(x, y))

        self.show()
        # Apply the anchor again once the native popup exists; some platforms
        # offset the first show by the host window's frame margins.
        self.move(QPoint(x, y))

        # Qt may add a one-pixel native popup frame after show(). Clamp the
        # realized frame as well so the visible border respects the margin.
        if area is not None:
            frame = self.frameGeometry()
            delta_x = 0
            delta_y = 0
            if frame.left() < area.left() + margin:
                delta_x = area.left() + margin - frame.left()
            elif frame.right() > area.right() - margin:
                delta_x = area.right() - margin - frame.right()
            if frame.top() < area.top() + margin:
                delta_y = area.top() + margin - frame.top()
            elif frame.bottom() > area.bottom() - margin:
                delta_y = area.bottom() - margin - frame.bottom()
            if delta_x or delta_y:
                self.move(self.pos() + QPoint(delta_x, delta_y))

        self.raise_()


def clip_length_label(seconds: int) -> str:
    minutes = seconds // 60
    if not minutes:
        return f'{seconds}s'
    value = f'{seconds / 60:.2f}'.rstrip('0').rstrip('.')
    return f'{value}m'


def _record_button_qss(recording: bool = False, muted: bool = False) -> str:
    border = Colors.ACCENT if recording else Colors.BORDER
    background = Colors.ACCENT_SOFT if recording else Colors.SURFACE_1
    color = Colors.ACCENT if recording else (Colors.TEXT_DIM if muted else Colors.TEXT)
    return f'''
        QPushButton {{
            background-color: {background};
            border: {Sizes.BORDER_W}px solid {border};
            border-radius: {Sizes.RADIUS_MD}px;
            color: {color};
            font-size: {Fonts.SIZE_BODY}px;
            font-family: {Fonts.BODY};
            font-weight: 600;
            padding: 0 8px;
            min-height: 28px;
            text-align: left;
        }}
        QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
        QPushButton:pressed {{ background-color: {Colors.ACCENT_SOFT}; border-color: {Colors.ACCENT}; }}
    '''


def _clear_button_qss() -> str:
    return f'''
        QPushButton {{
            background-color: transparent;
            border: {Sizes.BORDER_W}px solid {Colors.BORDER};
            border-radius: {Sizes.RADIUS_MD}px;
            color: {Colors.TEXT_DIM};
            font-size: {Fonts.SIZE_MICRO}px;
            font-family: {Fonts.DISPLAY};
            letter-spacing: {Fonts.TRACK_LABEL}px;
            font-weight: 600;
            padding: 0 7px;
            min-height: 28px;
        }}
        QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
        QPushButton:disabled {{ border-color: {Colors.HAIRLINE}; color: {Colors.TEXT_GHOST}; }}
    '''


def _hotkey_action_frame_qss() -> str:
    return '''
        QFrame#hotkeyActionRow {
            background-color: transparent;
            border: none;
        }
    '''


class _KeyboardBindingButton(QPushButton):
    binding_changed = Signal()

    def __init__(self, hotkey_manager: HotkeyManager, action: str, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.action = action
        self._recording = False
        self._held_keys = set()
        self._recorded_order = []
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(100)
        self.setFixedHeight(30)
        self.clicked.connect(self._toggle_recording)
        self.refresh_value()

    def refresh_value(self):
        if not self._recording:
            value = self.hotkey_manager.get_hotkey(self.action, device='keyboard')
            self.setText(format_keyboard_combo(value))
            set_theme_style(self, lambda value=value: (_record_button_qss(muted=not bool(value))))

    def cancel_recording(self):
        if self._recording:
            self._finish_recording(commit=False)

    def _toggle_recording(self):
        if self._recording:
            self._finish_recording(commit=False)
        else:
            self._recording = True
            self._held_keys.clear()
            self._recorded_order.clear()
            self.hotkey_manager.begin_input_capture()
            self.setText('Press keys')
            set_theme_style(self, lambda: (_record_button_qss(recording=True)))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.grabKeyboard()

    def _remember_key(self, key_name: str):
        if key_name:
            self._held_keys.add(key_name)
            if key_name not in self._recorded_order:
                self._recorded_order.append(key_name)

    def keyPressEvent(self, event):
        if not self._recording:
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat():
            event.accept()
            return
        for modifier in self.hotkey_manager.qt_modifier_names(event):
            self._remember_key(modifier)
        self._remember_key(self.hotkey_manager.qt_key_name(event))
        preview = normalize_keyboard_combo(self._recorded_order)
        if preview:
            self.setText(format_keyboard_combo(preview))
        event.accept()

    def keyReleaseEvent(self, event):
        if not self._recording:
            super().keyReleaseEvent(event)
            return
        if event.isAutoRepeat():
            event.accept()
            return
        key_name = self.hotkey_manager.qt_key_name(event)
        if key_name:
            self._held_keys.discard(key_name)
        still_down_modifiers = set(self.hotkey_manager.qt_modifier_names(event))
        for modifier in ('Ctrl', 'Alt', 'Shift', 'Win'):
            if modifier not in still_down_modifiers:
                self._held_keys.discard(modifier)
        if not self._held_keys and self._recorded_order:
            QTimer.singleShot(70, lambda: self._finish_recording(commit=True))
        event.accept()

    def focusOutEvent(self, event):
        self.cancel_recording()
        super().focusOutEvent(event)

    def _finish_recording(self, commit: bool):
        if not self._recording:
            return
        combo = normalize_keyboard_combo(self._recorded_order)
        self._recording = False
        self.releaseKeyboard()
        self._held_keys.clear()
        self._recorded_order.clear()
        if commit and combo and self.hotkey_manager.set_hotkey(
                self.action, combo, device='keyboard'):
            self.binding_changed.emit()
        self.hotkey_manager.end_input_capture()
        self.refresh_value()


class _ControllerBindingButton(QPushButton):
    binding_changed = Signal()

    def __init__(self, hotkey_manager: HotkeyManager, action: str, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.action = action
        self._recording = False
        self._recorded_order = []
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(108)
        self.setFixedHeight(30)
        self.setEnabled(sys.platform == 'win32')
        self.clicked.connect(self._toggle_recording)
        self.refresh_value()

    def refresh_value(self):
        if not self._recording:
            value = self.hotkey_manager.get_controller_hotkey(self.action)
            self.setText(format_controller_combo(value))
            set_theme_style(self, lambda value=value: (_record_button_qss(muted=not bool(value))))

    def cancel_recording(self):
        if self._recording:
            self._finish_recording(commit=False)

    def _toggle_recording(self):
        if self._recording:
            self._finish_recording(commit=False)
            return
        self._recording = True
        self._recorded_order.clear()
        self.hotkey_manager.begin_input_capture()
        self.hotkey_manager.controller_buttons_changed.connect(
            self._on_controller_buttons_changed)
        self.setText('Press buttons')
        set_theme_style(self, lambda: (_record_button_qss(recording=True)))
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def _on_controller_buttons_changed(self, buttons):
        if not self._recording:
            return
        pressed = set(buttons or [])
        if pressed:
            for button_name in CONTROLLER_BUTTON_ORDER:
                if button_name in pressed and button_name not in self._recorded_order:
                    self._recorded_order.append(button_name)
            for button_name in sorted(pressed):
                if button_name not in self._recorded_order:
                    self._recorded_order.append(button_name)
            preview = normalize_controller_combo(self._recorded_order)
            if preview:
                self.setText(format_controller_combo(preview))
            return
        if self._recorded_order:
            QTimer.singleShot(70, lambda: self._finish_recording(commit=True))

    def _finish_recording(self, commit: bool):
        if not self._recording:
            return
        combo = normalize_controller_combo(self._recorded_order)
        try:
            self.hotkey_manager.controller_buttons_changed.disconnect(
                self._on_controller_buttons_changed)
        except (RuntimeError, TypeError):
            # The popup can be destroyed before its optional disconnect.
            pass
        self._recording = False
        self._recorded_order.clear()
        if commit and combo and self.hotkey_manager.set_hotkey(
                self.action, combo, device='controller'):
            self.binding_changed.emit()
        self.hotkey_manager.end_input_capture()
        self.refresh_value()


class HotkeyPopup(PopupPanel):
    """Edit keyboard and controller bindings for capture actions."""

    def __init__(self, hotkey_manager: HotkeyManager, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.setMinimumWidth(340)
        self._binding_buttons = []
        self._keyboard_buttons = {}
        self._keyboard_clear_buttons = {}
        self._controller_buttons = {}
        self._controller_clear_buttons = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        self._title_label = QLabel('Hotkeys')
        set_theme_style(self._title_label, self._title_style)
        layout.addWidget(self._title_label)
        self.device_combo = WheelSafeComboBox()
        self.device_combo.addItems(['Keyboard', 'Controller'])
        self.device_combo.setAccessibleName('Shortcut device')
        set_theme_style(self.device_combo, combo_qss)
        self.device_combo.currentIndexChanged.connect(self._set_binding_device)
        layout.addWidget(self.device_combo)
        self.actions_scroll = QScrollArea()
        self.actions_scroll.setWidgetResizable(True)
        self.actions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.actions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_theme_style(self.actions_scroll, lambda: (
            'QScrollArea { background: transparent; border: none; }'
            'QScrollArea > QWidget > QWidget { background: transparent; }'))
        self.actions_widget = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_widget)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(6)
        self.actions_scroll.setWidget(self.actions_widget)
        layout.addWidget(self.actions_scroll)
        self.reload_actions()

    def reload_actions(self):
        for button in self._binding_buttons:
            button.cancel_recording()
        while self.actions_layout.count():
            item = self.actions_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._binding_buttons.clear()
        self._keyboard_buttons.clear()
        self._keyboard_clear_buttons.clear()
        self._controller_buttons.clear()
        self._controller_clear_buttons.clear()
        actions = [
            ('CAPTURE CLIP', 'save_clip'),
            ('HIGHLIGHT', 'mark_highlight'),
            ('SCREENSHOT', 'save_screenshot'),
            ('CONFIRM GAME', 'confirm_game_detection'),
            ('DISMISS GAME', 'dismiss_game_detection'),
            ('START RECORDING', 'start_recording'),
            ('STOP RECORDING', 'stop_recording'),
        ]
        custom_actions = sorted(
            (custom_clip_seconds(action), action)
            for action in self.hotkey_manager.hotkeys
            if custom_clip_seconds(action) is not None)
        actions[1:1] = [(f'CLIP {clip_length_label(seconds)}', action)
                        for seconds, action in custom_actions]
        for label_text, action_key in actions:
            self.actions_layout.addWidget(self._create_action_row(label_text, action_key))
        self.actions_scroll.setFixedHeight(min(360, len(actions) * 36 - 6))
        self._set_binding_device(self.device_combo.currentIndex())
        self.adjustSize()

    def _create_action_row(self, label_text: str, action_key: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName('hotkeyActionRow')
        set_theme_style(frame, lambda: (_hotkey_action_frame_qss()))
        grid = QGridLayout(frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        action_label = QLabel(label_text.capitalize())
        set_theme_style(action_label, lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        action_label.setFixedWidth(106)
        grid.addWidget(action_label, 0, 0)

        keyboard_btn = _KeyboardBindingButton(self.hotkey_manager, action_key)
        controller_btn = _ControllerBindingButton(self.hotkey_manager, action_key)
        keyboard_clear = QPushButton('Clear')
        keyboard_clear.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        keyboard_clear.setFixedSize(46, 30)
        set_theme_style(keyboard_clear, lambda: (_clear_button_qss()))
        keyboard_clear.setAccessibleName(f'Clear keyboard shortcut for {label_text.lower()}')
        keyboard_clear.clicked.connect(
            lambda _checked=False, action=action_key: self._clear_keyboard_bind(action))
        keyboard_btn.binding_changed.connect(
            lambda action=action_key: self._sync_keyboard_clear(action))
        clear_btn = QPushButton('Clear')
        clear_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        clear_btn.setFixedSize(46, 30)
        set_theme_style(clear_btn, lambda: (_clear_button_qss()))
        clear_btn.setAccessibleName(f'Clear controller shortcut for {label_text.lower()}')
        clear_btn.clicked.connect(
            lambda _checked=False, action=action_key: self._clear_controller_bind(action))
        controller_btn.binding_changed.connect(
            lambda action=action_key: self._sync_controller_clear(action))

        self._binding_buttons.extend([keyboard_btn, controller_btn])
        self._keyboard_buttons[action_key] = keyboard_btn
        self._keyboard_clear_buttons[action_key] = keyboard_clear
        self._controller_buttons[action_key] = controller_btn
        self._controller_clear_buttons[action_key] = clear_btn
        grid.addWidget(keyboard_btn, 0, 1)
        grid.addWidget(keyboard_clear, 0, 2)
        grid.addWidget(controller_btn, 0, 1)
        grid.addWidget(clear_btn, 0, 2)
        self._sync_keyboard_clear(action_key)
        self._sync_controller_clear(action_key)
        return frame

    def _set_binding_device(self, index):
        for button in self._binding_buttons:
            button.cancel_recording()
        controller = index == 1
        for button in (*self._keyboard_buttons.values(), *self._keyboard_clear_buttons.values()):
            button.setVisible(not controller)
        for button in (*self._controller_buttons.values(), *self._controller_clear_buttons.values()):
            button.setVisible(controller)

    def _clear_controller_bind(self, action_key: str):
        self._controller_buttons[action_key].cancel_recording()
        self.hotkey_manager.set_hotkey(action_key, '', device='controller')
        self._controller_buttons[action_key].refresh_value()
        self._sync_controller_clear(action_key)

    def _clear_keyboard_bind(self, action_key: str):
        self._keyboard_buttons[action_key].cancel_recording()
        self.hotkey_manager.set_hotkey(action_key, '', device='keyboard')
        self._keyboard_buttons[action_key].refresh_value()
        self._sync_keyboard_clear(action_key)

    def _sync_keyboard_clear(self, action_key: str):
        self._keyboard_clear_buttons[action_key].setEnabled(
            bool(self.hotkey_manager.get_hotkey(action_key, device='keyboard')))

    def _sync_controller_clear(self, action_key: str):
        clear_btn = self._controller_clear_buttons[action_key]
        clear_btn.setEnabled(
            sys.platform == 'win32'
            and bool(self.hotkey_manager.get_controller_hotkey(action_key)))

    def refresh_theme(self):
        super().refresh_theme()
        for button in self._binding_buttons:
            button.refresh_value()
        for action_key, button in self._keyboard_clear_buttons.items():
            set_theme_style(button, lambda: (_clear_button_qss()))
            self._sync_keyboard_clear(action_key)
        for action_key, button in self._controller_clear_buttons.items():
            set_theme_style(button, lambda: (_clear_button_qss()))
            self._sync_controller_clear(action_key)
        for frame in self.findChildren(QFrame):
            if frame.objectName() == 'hotkeyActionRow':
                set_theme_style(frame, lambda: (_hotkey_action_frame_qss()))
        set_theme_style(self._title_label, self._title_style)

    @staticmethod
    def _title_style() -> str:
        return label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)

    def hideEvent(self, event):
        for button in self._binding_buttons:
            button.cancel_recording()
        super().hideEvent(event)
