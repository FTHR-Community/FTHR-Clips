"""Compact RAM-only album-folder navigation for the library rail."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal, QSize
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QMenu, QToolButton, QVBoxLayout, QWidget

from ui.icons import load_icon
from ui.style import Colors, Fonts, Sizes, set_theme_style


class _AlbumDropButton(QToolButton):
    def __init__(self, sidebar, album_id):
        super().__init__(sidebar)
        self._sidebar = sidebar
        self._album_id = album_id
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        self._sidebar._drag_over(self, self._album_id, event)

    def dragMoveEvent(self, event):
        self._sidebar._drag_over(self, self._album_id, event)

    def dragLeaveEvent(self, event):
        self._sidebar.clear_drop_targets()
        event.accept()

    def dropEvent(self, event):
        sidebar = self._sidebar
        valid = sidebar._accepts_drop(self._album_id, event)
        sidebar.clear_drop_targets()
        if valid and callable(sidebar._drop_handler) and sidebar._drop_handler(
                self._album_id, event.mimeData(), event.source()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class AlbumSidebar(QWidget):
    """Project the grid's album snapshot into stable folder buttons."""

    album_activated = Signal(str)
    create_requested = Signal()
    rename_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, parent=None, *, navigation_group=None):
        super().__init__(parent)
        self._navigation_group = navigation_group
        self._buttons: dict[str, QToolButton] = {}
        self._selected_id: str | None = None
        self._locked = False
        self._unavailable: str | None = None
        self._catalog: tuple[tuple[str, str], ...] = ()
        self._drop_validator = None
        self._drop_handler = None

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.create_button = QToolButton(self)
        self.create_button.setObjectName('albumCreateButton')
        self.create_button.setText('New Album')
        self.create_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.create_button.setCheckable(False)
        self.create_button.setFixedSize(78, 78)
        self.create_button.setIcon(load_icon('plus.svg', 24))
        self.create_button.setIconSize(QSize(24, 24))
        self.create_button.setAccessibleName('Create album folder')
        self.create_button.setToolTip('Create album folder')
        self.create_button.clicked.connect(self.create_requested)
        set_theme_style(self.create_button, self._button_style)
        self._layout.addWidget(
            self.create_button, alignment=Qt.AlignmentFlag.AlignHCenter)

    @staticmethod
    def _button_style() -> str:
        return f'''
            QToolButton {{
                color: {Colors.TEXT_DIM};
                background: transparent;
                border: 1px solid transparent;
                border-radius: {Sizes.RADIUS_MD}px;
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_MICRO}px;
                padding: 3px;
            }}
            QToolButton:hover, QToolButton:checked,
            QToolButton[albumDropTarget="true"] {{
                color: {Colors.ACCENT};
                background: {Colors.ACCENT_SOFT};
                border-color: {Colors.ACCENT_DIM};
            }}
            QToolButton:disabled {{ color: {Colors.TEXT_MUTED}; }}
        '''

    def button_for_album(self, album_id):
        return self._buttons.get(str(album_id))

    def set_drop_handlers(self, validator, handler):
        self._drop_validator = validator
        self._drop_handler = handler

    def clear_drop_targets(self, *, except_button=None):
        for button in self._buttons.values():
            if button is not except_button and button.property('albumDropTarget'):
                button.setProperty('albumDropTarget', False)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()

    def _accepts_drop(self, album_id, event):
        return bool(not self._locked and self.isEnabled()
                    and album_id in self._buttons
                    and event.possibleActions() & Qt.DropAction.CopyAction
                    and callable(self._drop_validator)
                    and self._drop_validator(album_id, event.mimeData(), event.source()))

    def _drag_over(self, button, album_id, event):
        if self._accepts_drop(album_id, event):
            self.clear_drop_targets(except_button=button)
            if not button.property('albumDropTarget'):
                button.setProperty('albumDropTarget', True)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            self.clear_drop_targets()
            event.ignore()

    def _create_album_button(self, album_id: str, name: str):
        button = _AlbumDropButton(self, album_id)
        button.setObjectName('albumSidebarButton')
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setCheckable(True)
        button.setFixedSize(78, 78)
        button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        button.setIcon(load_icon('folder.svg', 24))
        button.setIconSize(QSize(24, 24))
        button.setProperty('album_id', album_id)
        button.setAccessibleName(f'Album folder: {name}')
        button.setToolTip(name)
        button.clicked.connect(
            lambda checked=False, key=album_id: self.album_activated.emit(key))
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.customContextMenuRequested.connect(
            lambda pos, key=album_id, widget=button:
            self._show_context_menu(key, widget, pos))
        if self._navigation_group is not None:
            self._navigation_group.addButton(button)
        set_theme_style(button, self._button_style)
        return button

    def _show_context_menu(self, album_id: str, button, pos: QPoint):
        menu = QMenu(self)
        rename = menu.addAction('Rename')
        delete = menu.addAction('Delete')
        rename.setEnabled(not self._locked)
        delete.setEnabled(not self._locked)
        action = menu.exec(button.mapToGlobal(pos))
        if action is rename:
            self.rename_requested.emit(str(album_id))
        elif action is delete:
            self.delete_requested.emit(str(album_id))

    def _update_button_text(self, button, name: str):
        button.ensurePolished()
        metrics = button.fontMetrics()
        width = max(20, button.width() - 8)
        label = metrics.elidedText(
            str(name), Qt.TextElideMode.ElideRight, width)
        button.setText(label)
        button.setToolTip(str(name))
        button.setAccessibleName(f'Album folder: {name}')

    def set_state(self, state, *, active=True):
        """Apply a defensive grid snapshot without touching the metadata manager."""
        self.clear_drop_targets()
        state = dict(state or {})
        catalog = tuple(
            (str(album_id), str(name))
            for album_id, name in tuple(state.get('albums', ())))
        selected_id = state.get('selected_id')
        selected_id = str(selected_id) if selected_id is not None else None
        locked = bool(state.get('locked', False))
        unavailable = state.get('unavailable')
        unavailable = str(unavailable) if unavailable else None
        catalog_changed = catalog != self._catalog
        if catalog_changed:
            wanted = {album_id: name for album_id, name in catalog}
            for album_id in tuple(self._buttons):
                if album_id in wanted:
                    continue
                button = self._buttons.pop(album_id)
                if self._navigation_group is not None:
                    self._navigation_group.removeButton(button)
                self._layout.removeWidget(button)
                button.hide()
                button.deleteLater()
            for album_id, name in catalog:
                button = self._buttons.get(album_id)
                if button is None:
                    button = self._create_album_button(album_id, name)
                    self._buttons[album_id] = button
                self._update_button_text(button, name)
            # Keep creation first, then reorder existing UUID buttons without
            # recreating them when the manager returns a different catalog order.
            for index, (album_id, _name) in enumerate(catalog):
                button = self._buttons[album_id]
                self._layout.removeWidget(button)
                self._layout.insertWidget(
                    index + 1, button, 0, Qt.AlignmentFlag.AlignHCenter)
            self._catalog = catalog

        self._selected_id = selected_id
        self._locked = locked
        self._unavailable = unavailable
        group = self._navigation_group
        was_exclusive = group.exclusive() if group is not None else None
        if group is not None:
            group.setExclusive(False)
        try:
            self.create_button.setEnabled(not locked)
            for album_id, button in self._buttons.items():
                button.setEnabled(not locked)
                blocked = button.blockSignals(True)
                button.setChecked(active and album_id == selected_id)
                button.blockSignals(blocked)
        finally:
            if group is not None:
                group.setExclusive(bool(was_exclusive))
