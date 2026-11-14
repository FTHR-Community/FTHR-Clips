"""Settings page for local plugin packages."""
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.plugin_manager import PluginError, PluginManager
from ui.dialogs import FthrDialog
from ui.style import (
    Colors, Fonts, Sizes, button_outline_qss, button_primary_qss,
    label_body, scrollbar_qss, set_theme_style,
)


_TYPE_LABELS = {'theme': 'Theme', 'export-presets': 'Export presets', 'test': 'Test plugin',
                'feature': 'Feature plugin'}
_THEME_NOTE = 'Applying a theme saves it to Customize. Disabling or removing its plugin leaves that theme in place.'


def _label(text='', *, heading=False, muted=False):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    set_theme_style(label, lambda: (
        label_body(Colors.TEXT_DIM if muted else Colors.TEXT,
                   Fonts.SIZE_H3 if heading else Fonts.SIZE_BODY)
        + (' font-weight: 600;' if heading else '')
    ))
    return label


def _button(text, action, *, primary=False):
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        set_theme_style(button, button_primary_qss)
    else:
        set_theme_style(button, lambda: button_outline_qss() + f'''
            QPushButton:focus {{ border-color: {Colors.ACCENT}; }}
            QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}
        ''')
    button.clicked.connect(action)
    return button


def _plugin_details(plugin):
    return '  ·  '.join((_TYPE_LABELS[plugin['type']], f"v{plugin['version']}", plugin['author']))


class _PluginCover(QLabel):
    """A bounded thumbnail: preserve the author's image instead of cropping it."""
    def __init__(self):
        super().__init__()
        self.setObjectName('pluginCover')
        self.setFixedSize(152, 104)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_theme_style(self, lambda: f'''
            QLabel#pluginCover {{ background: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER}; border-radius: {Sizes.RADIUS_MD}px; }}
        ''')
        self.hide()

    def set_cover(self, data, name):
        pixmap = QPixmap()
        valid = bool(data) and pixmap.loadFromData(data)
        self.clear()
        if valid:
            self.setPixmap(pixmap.scaled(140, 92, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        self.setAccessibleName(f'{name} cover')
        self.setVisible(valid)


class RemovePluginDialog(FthrDialog):
    def __init__(self, name, parent=None):
        super().__init__('Remove plugin', parent, width=420)
        self.body_layout.addWidget(_label(f'Remove {name}?', heading=True))
        self.body_layout.addWidget(_label(
            'The plugin will stop and be removed. Saved preferences are kept for reinstallation.',
            muted=True))
        self.action_layout.addStretch()
        self.cancel_button = _button('Cancel', self.reject)
        self.remove_button = _button('Remove plugin', self.accept)
        set_theme_style(self.remove_button, lambda: button_outline_qss() + f'''
            QPushButton {{ color: {Colors.ERROR}; border-color: {Colors.ERROR}; }}
            QPushButton:hover, QPushButton:pressed {{ background: {Colors.ERROR}; color: {Colors.BG}; }}
            QPushButton:focus {{ border: 2px solid {Colors.TEXT}; }}
        ''')
        self.cancel_button.setDefault(True)
        self.remove_button.setAutoDefault(False)
        self.action_layout.addWidget(self.cancel_button)
        self.action_layout.addWidget(self.remove_button)
        self.cancel_button.setFocus()


class _PluginZipDropArea(QFrame):
    archive_dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName('pluginZipDropArea')
        self.setAccessibleName('Plugin ZIP drop area')
        self.setAcceptDrops(True)
        self.setMinimumHeight(184)
        self.setProperty('dragActive', False)
        set_theme_style(self, lambda: f"""
            QFrame#pluginZipDropArea {{
                background: {Colors.SURFACE_1};
                border: 1px dashed {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
            QFrame#pluginZipDropArea[dragActive="true"] {{
                background: {Colors.ACCENT_SOFT};
                border-color: {Colors.ACCENT};
            }}
        """)

    def _set_drag_active(self, active):
        if self.property('dragActive') != active:
            self.setProperty('dragActive', active)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    @staticmethod
    def _dropped_archive(event):
        urls = event.mimeData().urls()
        if len(urls) != 1 or not (event.possibleActions() & Qt.DropAction.CopyAction):
            return None
        url = urls[0]
        if not url.isLocalFile() or url.host():
            return None
        filename = url.toLocalFile()
        if not filename or filename.startswith(('//', '\\\\')):
            return None
        archive = Path(filename)
        if not archive.is_absolute() or archive.suffix.casefold() != '.zip':
            return None
        try:
            return filename if archive.is_file() else None
        except OSError:
            return None

    def dragEnterEvent(self, event):
        accepted = self._dropped_archive(event) is not None
        self._set_drag_active(accepted)
        if accepted:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.property('dragActive'):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event):
        self._set_drag_active(False)
        archive = self._dropped_archive(event)
        if archive is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.archive_dropped.emit(archive)


class PluginSettingsWidget(QScrollArea):
    theme_applied = Signal()
    presets_changed = Signal()

    def __init__(self, parent=None, *, manager=None, runtime=None):
        super().__init__(parent)
        self.runtime = runtime
        self.manager = manager if manager is not None else (
            runtime.manager if runtime is not None else PluginManager())
        self._archive = None
        self._row_actions = {}
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_theme_style(self, scrollbar_qss)

        page = QWidget()
        set_theme_style(page, lambda: f'QWidget#pluginSettingsContent {{ background: {Colors.BG}; }}')
        page.setObjectName('pluginSettingsContent')
        self.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(Sizes.SPACE_3)

        layout.addWidget(_label('Install a plugin', heading=True))
        self.drop_area = _PluginZipDropArea()
        self.drop_area.archive_dropped.connect(self._review_archive)
        drop_layout = QVBoxLayout(self.drop_area)
        drop_layout.setContentsMargins(Sizes.SPACE_7, Sizes.SPACE_6, Sizes.SPACE_7, Sizes.SPACE_6)
        drop_layout.setSpacing(Sizes.SPACE_3)
        drop_layout.addStretch()
        title = _label('Drop a plugin ZIP here', heading=True)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(title)
        hint = _label('Review the package before installing.', muted=True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(hint)
        self.choose_button = _button('Choose ZIP…', self._choose_archive, primary=True)
        drop_layout.addWidget(self.choose_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        drop_layout.addStretch()
        layout.addWidget(self.drop_area)

        self._preview = QWidget()
        preview_row = QHBoxLayout(self._preview)
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(Sizes.SPACE_4)
        self._preview_cover = _PluginCover()
        preview_row.addWidget(self._preview_cover, 0, Qt.AlignmentFlag.AlignTop)
        preview_layout = QVBoxLayout()
        preview_row.addLayout(preview_layout, 1)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(Sizes.SPACE_2)
        self._preview_name = _label(heading=True)
        self._preview_details = _label(muted=True)
        preview_layout.addWidget(self._preview_name)
        preview_layout.addWidget(self._preview_details)
        preview_actions = QHBoxLayout()
        self.install_button = _button('Install', self._install, primary=True)
        self.cancel_button = _button('Cancel', self._cancel_preview)
        preview_actions.addWidget(self.install_button)
        preview_actions.addWidget(self.cancel_button)
        preview_actions.addStretch()
        preview_layout.addLayout(preview_actions)
        layout.addWidget(self._preview)
        self._preview.hide()

        self.status = _label()
        self.status.setAccessibleName('Plugin activity')
        layout.addWidget(self.status)
        self.status.hide()

        layout.addSpacing(Sizes.SPACE_3)
        self.installed_heading = _label('Installed plugins', heading=True)
        self.installed_heading.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        layout.addWidget(self.installed_heading)
        self._installed = QVBoxLayout()
        self._installed.setSpacing(Sizes.SPACE_3)
        layout.addLayout(self._installed)
        layout.addStretch(1)
        self.refresh()

    def _show_status(self, message, *, reveal=False):
        self.status.setText(message)
        self.status.setVisible(bool(message))
        if reveal:
            self.widget().layout().activate()
            self.ensureWidgetVisible(self.status, 0, Sizes.SPACE_3)

    def _cancel_preview(self):
        self._archive = None
        self._preview.hide()
        self._show_status('')
        self.choose_button.setFocus()

    def _choose_archive(self):
        archive, _ = QFileDialog.getOpenFileName(self, 'Choose a plugin', '', 'ZIP packages (*.zip)')
        if archive:
            self._review_archive(archive)

    def _review_archive(self, archive):
        self._archive = None
        self._preview.hide()
        self._preview_cover.set_cover(b'', '')
        try:
            manifest = self.manager.inspect_archive(archive)
            cover = self.manager.archive_cover(archive) if manifest.get('cover') else b''
            installed = next((item for item in self.manager.list_plugins()
                              if item['id'] == manifest['id']), None)
        except (PluginError, OSError) as error:
            self._show_status(f'Could not read {Path(archive).name}. {error}', reveal=True)
            return

        self._archive = archive
        self._show_status('')
        self._preview_name.setText(manifest['name'])
        self._preview_cover.set_cover(cover, manifest['name'])
        details = _plugin_details(manifest)
        if manifest.get('description'):
            details += '\n' + manifest['description']
        if installed is not None:
            details += f"\nInstalled version: {installed['version']}"
        if manifest['type'] == 'test':
            details += '\nThis package tests installation and does not change your settings.'
        elif manifest['type'] == 'feature':
            details += '\nRuns Python code with the app’s permissions, including microphone and file access. Install only if you trust the author.'
        self._preview_details.setText(details)
        self.install_button.setText('Update plugin' if installed is not None else 'Install plugin')
        self._preview.show()
        self.install_button.setFocus()
        self.widget().layout().activate()
        self.ensureWidgetVisible(self.install_button, 0, Sizes.SPACE_3)

    def _install(self):
        if self._archive is None:
            return
        try:
            plugin = self.manager.install(self._archive)
        except (PluginError, OSError) as error:
            self._show_status(f'Could not install the plugin. {error}', reveal=True)
            return

        self._cancel_preview()
        self._show_status(f"{plugin['name']} v{plugin['version']} is installed.")
        self.refresh()
        if plugin['type'] == 'export-presets':
            self.presets_changed.emit()

    def refresh(self):
        if self.runtime is not None:
            self.runtime.refresh()
        self._row_actions.clear()
        while self._installed.count():
            row = self._installed.takeAt(0).widget()
            row.hide()
            row.deleteLater()

        try:
            plugins = self.manager.list_plugins()
        except (PluginError, OSError) as error:
            self._installed.addWidget(_label(f'Could not read installed plugins. {error}'))
            return
        if not plugins:
            self._installed.addWidget(_label('No plugins installed.', muted=True))
            return

        if any(plugin['type'] == 'theme' for plugin in plugins):
            self._installed.addWidget(_label(_THEME_NOTE, muted=True))
        for plugin in plugins:
            row = self._plugin_row(plugin)
            self._installed.addWidget(row)
            row.show()

    def _plugin_row(self, plugin):
        plugin_id = plugin['id']
        name = plugin['name']
        enabled = plugin['enabled']
        row = QFrame()
        row.setObjectName('installedPlugin')
        set_theme_style(row, lambda: f'''
            QFrame#installedPlugin {{ background: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER}; border-radius: {Sizes.RADIUS_MD}px; }}
        ''')
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(Sizes.SPACE_3, Sizes.SPACE_3, Sizes.SPACE_3, Sizes.SPACE_3)
        row_layout.setSpacing(Sizes.SPACE_4)
        if plugin.get('cover'):
            cover = _PluginCover()
            try:
                cover.set_cover(self.manager.cover_image(plugin_id), name)
            except (PluginError, OSError):
                pass  # Damaged artwork must not prevent managing a plugin.
            row_layout.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addLayout(layout, 1)
        layout.setSpacing(Sizes.SPACE_2)
        layout.addWidget(_label(name, heading=True))
        state = 'Enabled' if enabled else 'Disabled'
        layout.addWidget(_label(f'{_plugin_details(plugin)}  ·  {state}', muted=True))
        if plugin.get('description'):
            layout.addWidget(_label(plugin['description']))
        if self.runtime is not None and plugin_id in self.runtime.errors:
            layout.addWidget(_label('Could not start: ' + self.runtime.errors[plugin_id]))

        actions = QHBoxLayout()
        actions.setSpacing(Sizes.SPACE_2)
        if plugin['type'] == 'feature' and self.runtime is not None:
            entry = self.runtime.active.get(plugin_id)
            label = getattr(entry[1], 'configure_label', 'Settings') if entry else 'Settings'
            configure = _button(label, partial(self.runtime.configure, plugin_id))
            configure.setAccessibleName(f'{name} settings')
            configure.setEnabled(plugin_id in self.runtime.active)
            actions.addWidget(configure)
        if plugin['type'] == 'theme':
            apply = _button('Apply theme', partial(self._apply_theme, plugin))
            apply.setAccessibleName(f'Apply {name} theme')
            apply.setEnabled(enabled)
            actions.addWidget(apply)
        toggle_label = 'Disable' if enabled else 'Enable'
        toggle = _button(toggle_label, partial(self._toggle, plugin))
        toggle.setAccessibleName(f'{toggle_label} {name}')
        self._row_actions[plugin_id] = toggle
        actions.addWidget(toggle)
        remove = _button('Remove', partial(self._remove, plugin))
        remove.setAccessibleName(f'Remove {name}')
        actions.addWidget(remove)
        actions.addStretch()
        layout.addLayout(actions)
        return row

    def _apply_theme(self, plugin):
        try:
            self.manager.apply_theme(plugin['id'])
        except (PluginError, OSError) as error:
            self._show_status(f'Could not apply the theme. {error}', reveal=True)
            return
        self._show_status(f"{plugin['name']} was applied and saved in Customize.", reveal=True)
        self.theme_applied.emit()

    def _toggle(self, plugin):
        enabled = not plugin['enabled']
        action = 'enable' if enabled else 'disable'
        try:
            self.manager.set_enabled(plugin['id'], enabled)
        except (PluginError, OSError) as error:
            self._show_status(f'Could not {action} the plugin. {error}', reveal=True)
            return
        self._show_status(f"{plugin['name']} is {action}d.")
        self.refresh()
        self._row_actions.get(plugin['id'], self.choose_button).setFocus()
        if plugin['type'] == 'export-presets':
            self.presets_changed.emit()

    def _remove(self, plugin):
        name = plugin['name']
        box = RemovePluginDialog(name, self)
        accepted = box.exec() == QDialog.DialogCode.Accepted
        box.deleteLater()
        if not accepted:
            return
        try:
            self.manager.uninstall(plugin['id'])
        except (PluginError, OSError) as error:
            self._show_status(f'Could not remove the plugin. {error}', reveal=True)
            return
        self._show_status(f'{name} was removed.')
        self.refresh()
        self.choose_button.setFocus()
        if plugin['type'] == 'export-presets':
            self.presets_changed.emit()
