"""Scoped keyboard dispatch and shortcut settings controls."""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget,
)

from core.navigation_bindings import (
    BINDING_DEFINITIONS, binding_tokens, normalize_binding,
)
from ui.style import (
    Colors, Fonts, button_outline_qss, checkbox_qss, label_body, scrollbar_qss, set_theme_style,
)


_MODIFIER_KEYS = {
    Qt.Key.Key_Control: 'Ctrl', Qt.Key.Key_Alt: 'Alt',
    Qt.Key.Key_Shift: 'Shift', Qt.Key.Key_Meta: 'Meta',
}


def key_name(event):
    if event.key() in _MODIFIER_KEYS:
        return _MODIFIER_KEYS[event.key()]
    if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return 'Return'
    if event.key() == Qt.Key.Key_Plus:
        return 'Plus'
    if event.key() == Qt.Key.Key_Escape:
        return 'Escape'
    return QKeySequence(event.key()).toString(QKeySequence.SequenceFormat.PortableText)


def modifier_names(event):
    return {name for flag, name in (
        (Qt.KeyboardModifier.ControlModifier, 'Ctrl'),
        (Qt.KeyboardModifier.AltModifier, 'Alt'),
        (Qt.KeyboardModifier.ShiftModifier, 'Shift'),
        (Qt.KeyboardModifier.MetaModifier, 'Meta'),
    ) if event.modifiers() & flag}


class KeyboardNavigation(QObject):
    """Consume shortcuts only in this window, leaving nested dialogs alone."""

    def __init__(self, root, bindings, actions, contexts, *, owner=None, repeat_actions=()):
        super().__init__(owner if owner is not None else root)
        self.root = root
        self.bindings = bindings
        self.actions = actions
        self.contexts = contexts
        self._held = set()
        self._pending = None
        self.repeat_actions = frozenset(repeat_actions)
        app = QApplication.instance()
        if not hasattr(app, '_navigation_keys_down'):
            app._navigation_keys_down = set()
        self._keys_down = app._navigation_keys_down
        self._refresh_prefix_keys()
        app.installEventFilter(self)
        bindings.changed.connect(self._bindings_changed)

    def _reset_keys(self):
        self._held.clear()
        self._pending = None

    def _refresh_prefix_keys(self):
        self._prefix_keys = set()
        for action, combo in self.bindings.values.items():
            if not self.bindings.action_enabled(action):
                continue
            keys = [key for key in combo.split('+') if key not in _MODIFIER_KEYS.values()]
            self._prefix_keys.update(keys[:-1])

    def _bindings_changed(self):
        self._keys_down.clear()
        self._reset_keys()
        self._refresh_prefix_keys()

    def _clear_inactive_keys(self):
        if QApplication.instance().applicationState() != Qt.ApplicationState.ApplicationActive:
            self._keys_down.clear()
            self._reset_keys()

    def dispose(self):
        QApplication.instance().removeEventFilter(self)
        self._reset_keys()

    def _available(self, obj):
        if not isinstance(obj, QWidget) or obj.window() is not self.root:
            return False
        if QApplication.activePopupWidget() is not None:
            return False
        modal = QApplication.activeModalWidget()
        return ((modal is None or modal is self.root)
                and not obj.property('bindingCapture')
                and not getattr(obj, '_recording', False))

    def _choices(self):
        contexts = self.contexts()
        return [(action, binding_tokens(self.bindings.get(action)))
                for action, _, _, scope, _ in BINDING_DEFINITIONS
                if scope in contexts and action in self.actions
                and self.bindings.action_enabled(action)
                and self.bindings.get(action)]

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.ApplicationDeactivate:
            # Windows briefly reports the app inactive while replacing its
            # foreground dialog. Clear physical prefixes only if focus stays
            # outside the app, rather than breaking a held Tab mid-handoff.
            QTimer.singleShot(50, self, self._clear_inactive_keys)
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide) and obj is self.root:
            self._reset_keys()
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress,
                        QEvent.Type.KeyRelease):
            return False
        name = key_name(event)
        if not event.isAutoRepeat() and name not in _MODIFIER_KEYS.values():
            if kind == QEvent.Type.KeyPress and name in self._prefix_keys:
                self._keys_down.add(name)
            elif kind == QEvent.Type.KeyRelease:
                self._keys_down.discard(name)
        if not self._available(obj):
            # Native input first arrives at QWindow, then at the focused
            # QWidget. Ignore the first delivery without losing a held Tab.
            # Only an owned widget entering a popup/recorder resets the chord.
            if isinstance(obj, QWidget) and obj.window() is self.root:
                self._reset_keys()
            return False
        choices = self._choices()
        if not choices:
            self._reset_keys()
            return False
        if kind == QEvent.Type.KeyRelease:
            if event.isAutoRepeat():
                return False
            self._held.discard(name)
            if self._pending is not None and not self._held:
                action = self._pending
                self._pending = None
                self.actions[action]()
                return True
            return False
        # Physical keys survive intentional window handoffs: holding Tab
        # can open several clips even though each editor has a new controller.
        tokens = frozenset(self._keys_down | self._held | modifier_names(event) | {name})
        # Text editing keeps its cursor, selection and native shortcuts.
        # Tab is reserved for application chords rather than focus traversal.
        editing = isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit))
        if (not modifier_names(event) - {'Shift'}
                and name in ('Tab', 'Backtab', 'Left', 'Right', 'Up', 'Down', 'Return')
                and (not editing or name in ('Tab', 'Backtab'))):
            # Item traversal/activation is disabled. Explicit chords still
            # work; text fields retain their native editing keys.
            if not any(tokens == combo or (tokens < combo
                       and (combo - tokens) - set(_MODIFIER_KEYS.values()))
                       for _, combo in choices):
                event.accept()
                return True
        lifecycle_match = any(action in {'editor_minimize', 'editor_close'}
                              and combo == tokens for action, combo in choices)
        if editing and name != 'Tab' and 'Tab' not in tokens and not lifecycle_match:
            self._reset_keys()
            return False
        matches = [action for action, combo in choices if combo == tokens]
        prefixes = [combo for _, combo in choices if tokens < combo
                    and (combo - tokens) - set(_MODIFIER_KEYS.values())]
        if kind == QEvent.Type.ShortcutOverride:
            if matches or prefixes:
                event.accept()
                return True
            return False
        if name in _MODIFIER_KEYS.values():
            return False
        if not matches and not prefixes:
            self._pending = None
            return False
        if event.isAutoRepeat():
            if matches and matches[0] in self.repeat_actions:
                self.actions[matches[0]]()
            return True
        self._held.add(name)
        if prefixes:
            self._pending = matches[0] if matches else None
        elif matches:
            self._pending = None
            self.actions[matches[0]]()
        event.accept()
        return True


class BindingButton(QPushButton):
    """Record arbitrary chords, including Tab as a nonmodifier key."""

    def __init__(self, bindings, action, error_label, parent=None):
        super().__init__(parent)
        self.bindings, self.action, self.error_label = bindings, action, error_label
        self._recording = False
        self._keys = []
        self._down = set()
        self._capture_manager = None
        self.setMinimumWidth(150)
        set_theme_style(self, button_outline_qss)
        self.clicked.connect(self._start_recording)
        bindings.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        value = self.bindings.get(self.action).replace('Return', 'Enter')
        self.setText(value or 'Unassigned')

    def _start_recording(self):
        self._recording = True
        self._keys.clear()
        self._down.clear()
        self.setProperty('bindingCapture', True)
        self._capture_manager = getattr(self.window(), 'hotkey_manager', None)
        if self._capture_manager is not None:
            self._capture_manager.begin_input_capture()
        self.error_label.clear()
        self.setText('Press shortcut…')
        self.setFocus()
        self.grabKeyboard()

    def event(self, event):
        if self._recording and event.type() in (
                QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if event.type() == QEvent.Type.ShortcutOverride or event.isAutoRepeat():
                event.accept()
                return True
            name = key_name(event)
            if event.type() == QEvent.Type.KeyPress:
                self._down.add(name)
                for token in sorted(modifier_names(event)) + [name]:
                    if token and token not in self._keys:
                        self._keys.append(token)
                self.setText('+'.join(self._keys).replace('Return', 'Enter'))
            else:
                self._down.discard(name)
                if not self._down and self._keys:
                    try:
                        self.bindings.set(self.action, normalize_binding('+'.join(self._keys)))
                    except ValueError as error:
                        self.error_label.setText(str(error))
                    self._stop_recording()
            event.accept()
            return True
        return super().event(event)

    def _stop_recording(self):
        self._recording = False
        self.setProperty('bindingCapture', False)
        self.releaseKeyboard()
        if self._capture_manager is not None:
            self._capture_manager.end_input_capture()
            self._capture_manager = None
        self.refresh()

    def focusOutEvent(self, event):
        if self._recording:
            self._stop_recording()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        if self._recording:
            self._stop_recording()
        super().hideEvent(event)


class KeyboardShortcutSettings(QScrollArea):
    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        set_theme_style(self, lambda: (
            f'QScrollArea {{ background: {Colors.BG}; border: none; }}' + scrollbar_qss()))
        set_theme_style(page, lambda: f'QWidget {{ background: {Colors.BG}; }}')
        layout = QVBoxLayout(page)
        beta = QLabel('Beta')
        set_theme_style(beta, lambda: label_body(Colors.ACCENT, Fonts.SIZE_BODY_L))
        layout.addWidget(beta)
        hint = QLabel('Click a binding, hold the keys together, then release to save. '
                      'Tab + S means hold Tab and press S. Shortcuts work while FTHR '
                      'is active; text fields keep their normal editing keys. '
                      'Up/Down scrolls Clips; Shift+Up/Down switches sidebar destinations.')
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(hint)
        self.bindings = bindings
        self.enabled_check = QCheckBox('Enable shortcuts')
        self.enabled_check.setToolTip('Disable all shortcuts listed below without removing your saved bindings.')
        set_theme_style(self.enabled_check, checkbox_qss)
        self.enabled_check.setChecked(bindings.enabled)
        self.enabled_check.toggled.connect(bindings.set_enabled)
        layout.addWidget(self.enabled_check)
        self.category_checks = {}
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        set_theme_style(self.error_label, lambda: label_body(Colors.DELETE, Fonts.SIZE_BODY))
        layout.addWidget(self.error_label)
        self.buttons = {}
        group = None
        for action, label, _, _, section in BINDING_DEFINITIONS:
            if group != section:
                header = QLabel(section)
                set_theme_style(header, lambda: (
                    label_body(Colors.ACCENT, Fonts.SIZE_BODY_L) + 'font-weight: 600;'))
                layout.addSpacing(14)
                layout.addWidget(header)
                check = QCheckBox(f'Enable {section.lower()} shortcuts')
                set_theme_style(check, checkbox_qss)
                check.setChecked(bindings.categories[section])
                check.toggled.connect(lambda enabled, category=section: bindings.set_category_enabled(category, enabled))
                self.category_checks[section] = check
                layout.addWidget(check)
                group = section
            row = QHBoxLayout()
            label_widget = QLabel(label)
            set_theme_style(label_widget, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY))
            row.addWidget(label_widget, 1)
            button = BindingButton(bindings, action, self.error_label)
            button.setAccessibleName(f'Change shortcut: {label}')
            self.buttons[action] = button
            row.addWidget(button)
            clear = QPushButton('Clear')
            set_theme_style(clear, button_outline_qss)
            clear.setAccessibleName(f'Clear shortcut: {label}')
            clear.clicked.connect(lambda _checked=False, key=action: bindings.set(key, ''))
            row.addWidget(clear)
            layout.addLayout(row)
        reset = QPushButton('Restore default shortcuts')
        set_theme_style(reset, button_outline_qss)
        reset.clicked.connect(bindings.reset)
        layout.addSpacing(18)
        layout.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        self.setWidget(page)
        bindings.changed.connect(self._refresh_enabled)
        self._refresh_enabled()

    def _refresh_enabled(self):
        self.enabled_check.blockSignals(True)
        self.enabled_check.setChecked(self.bindings.enabled)
        self.enabled_check.blockSignals(False)
        for category, check in self.category_checks.items():
            check.blockSignals(True)
            check.setChecked(self.bindings.categories[category])
            check.setEnabled(self.bindings.enabled)
            check.blockSignals(False)
