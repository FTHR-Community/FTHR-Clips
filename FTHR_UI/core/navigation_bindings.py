"""Persisted, application-local keyboard bindings (never global OS hotkeys)."""

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QKeySequence


# Action, label, default, context, settings group.
BINDING_DEFINITIONS = (
    ('switch_panel', 'Switch Clips / Settings', 'Tab+S', 'application', 'Application'),
    ('sidebar_next', 'Next sidebar destination', 'Shift+Down', 'application', 'Application'),
    ('sidebar_previous', 'Previous sidebar destination', 'Shift+Up', 'application', 'Application'),
    ('toggle_detection', 'Toggle game detection', 'Shift+G', 'main', 'Application'),
    ('restart_capture', 'Restart capture engine', 'Alt+Shift+R', 'main', 'Application'),
    ('library_scroll_up', 'Scroll clips up', 'Up', 'library', 'Clips'),
    ('library_scroll_down', 'Scroll clips down', 'Down', 'library', 'Clips'),
    ('category_next', 'Next settings category', 'Shift+Right', 'settings', 'Settings'),
    ('category_previous', 'Previous settings category', 'Shift+Left', 'settings', 'Settings'),
    ('editor_split', 'Split segment here', 'Ctrl+C', 'editor', 'Clip editor'),
    ('editor_delete', 'Delete segment', 'Ctrl+D', 'editor', 'Clip editor'),
    ('editor_restore', 'Reinstate segment', 'Ctrl+W', 'editor', 'Clip editor'),
    ('editor_crop', 'Quick crop', 'Ctrl+Q', 'editor', 'Clip editor'),
    ('editor_export', 'Export now', 'Ctrl+E', 'editor', 'Clip editor'),
    ('editor_minimize', 'Minimize editor', 'Shift+Escape', 'editor', 'Clip editor'),
    ('editor_close', 'Close editor', 'Escape', 'editor', 'Clip editor'),
    ('editor_next', 'Open next clip', 'Tab+Right', 'editor', 'Clip editor'),
    ('editor_previous', 'Open previous clip', 'Tab+Left', 'editor', 'Clip editor'),
    ('editor_play', 'Play / pause', 'Space', 'editor', 'Clip editor'),
    ('editor_undo', 'Undo', 'Ctrl+Z', 'editor', 'Clip editor'),
    ('editor_redo', 'Redo', 'Ctrl+Y', 'editor', 'Clip editor'),
    ('editor_redo_alt', 'Redo (alternate)', 'Ctrl+Shift+Z', 'editor', 'Clip editor'),
)
DEFAULT_NAVIGATION_BINDINGS = {row[0]: row[2] for row in BINDING_DEFINITIONS}
SHORTCUT_CATEGORIES = tuple(dict.fromkeys(row[4] for row in BINDING_DEFINITIONS))
_MODIFIERS = ('Ctrl', 'Alt', 'Shift', 'Meta')
_ALIASES = {'enter': 'Return', 'esc': 'Escape', 'control': 'Ctrl',
            'win': 'Meta', 'cmd': 'Meta'}


def normalize_binding(value: str) -> str:
    """Canonicalize modifier order and allow nonmodifier chords such as Tab+S."""
    tokens = []
    for raw in str(value or '').split('+'):
        token = raw.strip()
        if not token:
            continue
        token = _ALIASES.get(token.lower(), token)
        modifier = next((mod for mod in _MODIFIERS
                         if mod.lower() == token.lower()), None)
        if modifier:
            token = modifier
        elif token != 'Plus':
            sequence = QKeySequence(token)
            if sequence.isEmpty() or sequence[0].key() == 0x1ffffff:
                raise ValueError(f'Unknown key: {token}')
            token = sequence.toString(QKeySequence.SequenceFormat.PortableText)
            if token == 'Esc':
                token = 'Escape'
        if token not in tokens:
            tokens.append(token)
    if tokens and all(token in _MODIFIERS for token in tokens):
        raise ValueError('Include a key as well as modifiers.')
    return '+'.join([mod for mod in _MODIFIERS if mod in tokens]
                    + [token for token in tokens if token not in _MODIFIERS])


def binding_tokens(value: str) -> frozenset[str]:
    return frozenset(value.split('+')) if value else frozenset()


class NavigationBindings(QObject):
    changed = Signal()

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.enabled = bool(settings_manager.get('shortcuts_enabled', True)) if settings_manager else True
        saved_categories = settings_manager.get('shortcut_categories_enabled', {}) if settings_manager else {}
        self.categories = {category: bool(saved_categories.get(category, True))
                           if isinstance(saved_categories, dict) else True
                           for category in SHORTCUT_CATEGORIES}
        saved = settings_manager.get('navigation_bindings', {}) if settings_manager else {}
        self.values = dict(DEFAULT_NAVIGATION_BINDINGS)
        if isinstance(saved, dict):
            for action, value in saved.items():
                if action in self.values and isinstance(value, str):
                    try:
                        self.values[action] = normalize_binding(value)
                    except ValueError:
                        # Keep the default when an older or invalid saved shortcut cannot parse.
                        pass

    def get(self, action):
        return self.values[action]

    def category_enabled(self, category):
        return self.enabled and self.categories.get(category, True)

    def action_enabled(self, action):
        category = next(row[4] for row in BINDING_DEFINITIONS if row[0] == action)
        return self.category_enabled(category)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self._save_enabled()

    def set_category_enabled(self, category, enabled):
        if category not in self.categories:
            raise ValueError(f'Unknown shortcut category: {category}')
        self.categories[category] = bool(enabled)
        self._save_enabled()

    def _save_enabled(self):
        if self.sm is not None:
            self.sm.set('shortcuts_enabled', self.enabled)
            self.sm.set('shortcut_categories_enabled', dict(self.categories))
            self.sm.save_settings()
        self.changed.emit()

    def set(self, action, value):
        value = normalize_binding(value)
        scopes = {row[0]: row[3] for row in BINDING_DEFINITIONS}
        labels = {row[0]: row[1] for row in BINDING_DEFINITIONS}
        for other, combo in self.values.items():
            pair = {scopes[action], scopes[other]}
            overlapping = (scopes[action] == scopes[other]
                           or 'application' in pair
                           or pair in ({'main', 'settings'}, {'main', 'library'}))
            if (other != action and value and overlapping
                    and binding_tokens(combo) == binding_tokens(value)):
                raise ValueError(f'Already used by {labels[other]}. Clear or change it first.')
        self.values[action] = value
        if self.sm:
            self.sm.set('navigation_bindings', dict(self.values))
            self.sm.save_settings()
        self.changed.emit()

    def reset(self):
        self.values = dict(DEFAULT_NAVIGATION_BINDINGS)
        if self.sm:
            self.sm.set('navigation_bindings', dict(self.values))
            self.sm.save_settings()
        self.changed.emit()


def navigation_bindings_for(settings_manager, parent=None):
    """Share live edits between settings and each newly opened editor."""
    bindings = getattr(settings_manager, '_navigation_bindings', None)
    if bindings is None:
        bindings = NavigationBindings(settings_manager, parent)
        if settings_manager is not None:
            settings_manager._navigation_bindings = bindings
    return bindings
