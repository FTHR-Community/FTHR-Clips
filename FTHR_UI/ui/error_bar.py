from __future__ import annotations

from ui.style import set_theme_style

from ui.icons import IconButton
from collections import deque
import sys

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from ui.style import Colors, Fonts, Sizes

class ErrorBar(QFrame):
    """Persistent error bar at the bottom of the main window.

    Call push() from the Qt main thread only. For background threads, use
    QTimer.singleShot(0, lambda: self.push(...)).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: deque[dict] = deque()
        self._current: dict | None = None
        self._action_buttons: list[QPushButton] = []

        self.setFixedHeight(Sizes.HW_BAR_H)
        self.setVisible(False)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(Sizes.SPACE_6, 0, Sizes.SPACE_6, 0)
        self._layout.setSpacing(10)

        self._icon = QLabel('Notice')
        self._layout.addWidget(self._icon)

        self._title = QLabel()
        self._layout.addWidget(self._title)

        sep = QLabel('—')
        set_theme_style(sep, lambda: (f'background: transparent; color: {Colors.TEXT};'))
        self._separator = sep
        self._layout.addWidget(sep)

        self._detail = QLabel()
        set_theme_style(self._detail, lambda: (f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.BODY};'
            f' background: transparent; color: {Colors.TEXT};'))
        self._layout.addWidget(self._detail)

        self._layout.addStretch()

        self._more_lbl = QLabel()
        set_theme_style(self._more_lbl, lambda: (f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' letter-spacing: 0px; background: transparent; color: {Colors.TEXT_DIM};'))
        self._more_lbl.setVisible(False)
        self._layout.addWidget(self._more_lbl)

        # Dismiss button — always last; action buttons inserted before it
        self._dismiss_btn = IconButton('close.png', label='Close')
        self._dismiss_btn.setFixedWidth(28)
        set_theme_style(self._dismiss_btn, lambda: ('QPushButton { background: transparent; border: none;'
            f' color: {Colors.TEXT_DIM}; font-size: 14px; }}'
            f'QPushButton:hover {{ color: {Colors.TEXT}; }}'))
        self._dismiss_btn.setToolTip('Dismiss all messages' if sys.platform == 'win32' else 'Dismiss message')
        self._dismiss_btn.clicked.connect(self.clear if sys.platform == 'win32' else self.dismiss_current)
        self._layout.addWidget(self._dismiss_btn)

    def refresh_theme(self) -> None:
        """Refresh both idle chrome and any currently displayed error."""
        set_theme_style(self._separator, lambda: (f'background: transparent; color: {Colors.TEXT};'))
        set_theme_style(self._detail, lambda: (f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.BODY};'
            f' background: transparent; color: {Colors.TEXT};'))
        set_theme_style(self._more_lbl, lambda: (f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' letter-spacing: 0px; background: transparent; color: {Colors.TEXT_DIM};'))
        set_theme_style(self._dismiss_btn, lambda: ('QPushButton { background: transparent; border: none;'
            f' color: {Colors.TEXT_DIM}; font-size: 14px; }}'
            f'QPushButton:hover {{ color: {Colors.TEXT}; }}'))
        if self._current is not None:
            self._show(self._current)

    # Public API

    def push(self, title: str, detail: str,
             level: str = 'error',
             actions: list[tuple[str, callable]] | None = None,
             *, key: str | None = None,
             dismiss_on_action: bool = True) -> None:
        """Add or update an error; an optional key identifies one incident."""
        entry = {
            'title':   title,
            'detail':  detail,
            'level':   level,
            'actions': list(actions or []),
            'key': key,
            'dismiss_on_action': bool(dismiss_on_action),
        }
        if key is not None:
            if self._current is not None and self._current.get('key') == key:
                self._show(entry)
                return
            for index, queued in enumerate(self._queue):
                if queued.get('key') == key:
                    self._queue[index] = entry
                    self._update_more_label()
                    return
        if self._current is None:
            self._show(entry)
        else:
            self._queue.append(entry)
            self._update_more_label()

    def dismiss_current(self) -> None:
        """Dismiss the current error; show the next one in the queue."""
        self._current = None
        self._clear_action_buttons()
        if self._queue:
            self._show(self._queue.popleft())
        else:
            self.setVisible(False)
        self._update_more_label()

    def clear(self) -> None:
        """Discard all queued errors and hide the bar."""
        self._queue.clear()
        self._current = None
        self._clear_action_buttons()
        self.setVisible(False)
        self._update_more_label()

    def remove_by_key(self, key: str) -> bool:
        """Remove exactly one keyed incident without disturbing other errors."""
        if self._current is not None and self._current.get('key') == key:
            self.dismiss_current()
            return True

        remaining = [entry for entry in self._queue
                     if entry.get('key') != key]
        removed = len(remaining) != len(self._queue)
        if removed:
            self._queue = deque(remaining)
            self._update_more_label()
        return removed

    # Internal helpers (also called from tests)

    def _fire_action(self, callback: callable, entry: dict | None = None) -> None:
        """Run an action and dismiss only the same entry if still current."""
        acted_entry = self._current if entry is None else entry
        if entry is not None and self._current is not entry:
            return
        callback()
        if (acted_entry is not None
                and self._current is acted_entry
                and acted_entry.get('dismiss_on_action', True)):
            self.dismiss_current()

    def _show(self, entry: dict) -> None:
        self._current = entry
        # Resolve the token at display time so an applied theme also affects
        # errors that arrive after the settings page was left open.
        color = {
            'error': Colors.ERROR,
            'warning': Colors.WARNING,
        }.get(entry['level'], Colors.ERROR)

        set_theme_style(self, lambda color=color: (f'''
            QFrame {{
                background-color: {Colors.SURFACE_1};
                border-top: 1px solid {color};
            }}
        '''))
        self._icon.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; background: transparent; color: {color};'
        )
        self._title.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px;'
            f' background: transparent; color: {color};'
        )
        self._title.setText(entry['title'])
        self._detail.setText(entry['detail'])
        self._add_action_buttons(entry['actions'], color)
        self.setVisible(True)

    def _add_action_buttons(self, actions: list[tuple[str, callable]],
                            color: str) -> None:
        self._clear_action_buttons()
        for label, callback in actions[:2]:
            btn = QPushButton(label)
            set_theme_style(btn, lambda color=color: (f'''
                QPushButton {{
                    background-color: transparent;
                    border: 1px solid {color};
                    color: {color};
                    font-size: {Fonts.SIZE_LABEL}px;
                    font-family: {Fonts.DISPLAY};
                    letter-spacing: {Fonts.TRACK_LABEL}px;
                    font-weight: 600;
                    padding: 3px 10px;
                }}
                QPushButton:hover {{
                    background-color: {color};
                    color: {Colors.TEXT};
                }}
            '''))
            cb = callback
            btn.clicked.connect(
                lambda _checked, c=cb, e=self._current: self._fire_action(c, e))
            # Insert before dismiss button (last widget)
            self._layout.insertWidget(self._layout.count() - 1, btn)
            self._action_buttons.append(btn)

    def _clear_action_buttons(self) -> None:
        for btn in self._action_buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._action_buttons.clear()

    def _update_more_label(self) -> None:
        n = len(self._queue)
        if n > 0:
            self._more_lbl.setText(f'+{n} MORE')
            self._more_lbl.setVisible(True)
        else:
            self._more_lbl.setVisible(False)
