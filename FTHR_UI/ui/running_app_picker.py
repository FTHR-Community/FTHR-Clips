"""Searchable running-application picker shared by audio capture controls."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QDialogButtonBox,
)

from core.running_apps import list_running_apps, application_icon
from ui.style import (
    Colors, Fonts, set_theme_style, label_body, button_outline_qss,
    lineedit_qss, scrollbar_qss,
)


class RunningAppPickerDialog(QDialog):
    def __init__(self, parent=None, *, title='Choose a running app', excluded=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 440)
        self._excluded = set(excluded)
        layout = QVBoxLayout(self)
        hint = QLabel('Choose an executable. The rule also applies when the app restarts.')
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_MICRO))
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search running apps')
        self.search.setAccessibleName('Search running apps')
        set_theme_style(self.search, lineedit_qss)
        row.addWidget(self.search, 1)
        refresh = QPushButton('Refresh')
        refresh.clicked.connect(self.refresh)
        set_theme_style(refresh, button_outline_qss)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.apps = QListWidget()
        self.apps.setAccessibleName('Running applications')
        self.apps.setIconSize(QSize(24, 24))
        self.apps.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.apps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_theme_style(self.apps, lambda: (
            f'QListWidget {{ background:{Colors.SURFACE_1}; color:{Colors.TEXT}; '
            f'border:1px solid {Colors.BORDER}; border-radius:6px; }} '
            'QListWidget::item { padding:8px; } '
            f'QListWidget::item:selected {{ background:{Colors.SURFACE_3}; '
            f'color:{Colors.ACCENT}; }} ' + scrollbar_qss()))
        layout.addWidget(self.apps, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        set_theme_style(self.status, lambda: label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO))
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm.setText('Exclude app')
        self.confirm.setEnabled(False)
        for button in buttons.buttons():
            set_theme_style(button, button_outline_qss)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.apps.itemSelectionChanged.connect(
            lambda: self.confirm.setEnabled(self.selected_app() is not None))
        self.apps.itemDoubleClicked.connect(lambda _item: self.accept())
        self.search.textChanged.connect(self._filter)
        self.refresh()

    def selected_app(self):
        item = self.apps.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def refresh(self):
        self.apps.clear()
        try:
            apps = list_running_apps()
        except (OSError, RuntimeError):
            apps = []
        for app in apps:
            if app.identity in self._excluded:
                continue
            item = QListWidgetItem(application_icon(app.identity),
                                   f'{app.name}  ({app.identity}.exe)')
            item.setData(Qt.ItemDataRole.UserRole, app)
            item.setToolTip(f'{app.name} ({app.identity}.exe)')
            self.apps.addItem(item)
        self._filter()

    def _filter(self, _text=None):
        query = self.search.text().casefold().strip()
        visible = 0
        for index in range(self.apps.count()):
            item = self.apps.item(index)
            item.setHidden(query not in item.text().casefold())
            visible += not item.isHidden()
        if self.apps.currentItem() is not None and self.apps.currentItem().isHidden():
            self.apps.setCurrentItem(None)
        self.status.setText(
            f'{visible} running apps. Apps without readable executable information may be absent.'
            if visible else ('No apps match your search.' if query else
                             'No available running apps. Try Refresh.'))
