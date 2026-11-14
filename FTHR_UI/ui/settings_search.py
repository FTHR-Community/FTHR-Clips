"""Search settings without constructing pages or starting device discovery."""
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit, QTreeWidget, QTreeWidgetItem

from ui.icons import load_icon
from ui.style import Colors, Fonts, lineedit_qss, set_theme_style


@dataclass(frozen=True)
class SettingDestination:
    title: str
    category: int
    target: str
    keywords: str = ''


CATEGORY_NAMES = ('General', 'Clip', 'Audio', 'Visuals', 'Customize',
                  'Performance', 'Shortcuts', 'Clip / Game Profiles',
                  'Clip / Allowed games', 'Plugins', 'Storage')

# Targets are an attribute on SettingsPage or the exact text of a section label.
# Keeping this index declarative preserves lazy page construction during search.
DESTINATIONS = tuple(SettingDestination(*row) for row in (
    ('Start with Windows', 0, 'autostart_check', 'startup autostart launch login'),
    ('Keep running when closed', 0, 'close_to_tray_check', 'close background exit minimize system tray'),
    ('Show support button', 0, 'kofi_widget_check', 'support donate ko-fi widget'),
    ('Capture error messages', 0, 'error_notifications_check', 'notifications failures'),
    ('Capture card notifications', 0, 'capture_card_mode_combo', 'popup alerts screenshots game detection'),
    ('Upload settings', 0, '_upload_settings_widget', 'sharing catbox vaultscope sftp provider connection'),
    ('Export presets', 0, 'export_presets_widget', 'quality discord size format'),
    ('Export diagnostic report', 0, 'export_diagnostic_btn', 'troubleshooting logs bug support'),
    ('Settings presets', 0, 'preset_combo', 'save load delete configuration'),
    ('App updates', 0, 'updates_toggle', 'update version installer download automatic check release'),
    ('App credits', 0, 'credits_toggle', 'about developers authors'),
    ('Clips storage folder', 10, 'clips_directory_edit', 'location path directory screenshots exports'),
    ('Manual recording folder', 10, 'recording_directory_edit', 'location path directory'),
    ('Attempt saves with low resources', 10, 'replay_storage_widget.allow_low_resources_check', 'low ram memory storage warning attempt save anyway'),
    ('Replay buffer', 10, 'replay_storage_widget.mode_combo', 'temporary storage ram memory disk budget automatic manual limit hdd ssd instant replay'),
    ('Capture cursor', 1, 'capture_cursor_check', 'mouse pointer system cursor crosshair capture recording'),
    ('Save low-FPS clips', 1, 'allow_low_fps_clips_check', 'low fps frame rate slow capture dropped frames replay'),
    ('Save clips with gaps', 1, 'allow_replay_gaps_check', 'missing frames dropped frames freeze stutter replay cadence'),
    ('Library storage and cleanup', 10, 'library_storage_panel', 'storage limits quota disk space automatic trash retention'),
    ('Library backup and restore', 10, 'library_backup_panel', 'verify resume recovery'),
    ('Import clips', 1, '_import_folders_container', 'scan add folders library'),
    ('Video encoder', 1, 'encoder_combo', 'gpu hardware nvenc amd amf intel qsv software'),
    ('Video codec', 1, 'codec_combo', 'h264 h265 hevc av1 encoding hdr sdr dynamic range'),
    ('Redetect encoders', 1, 'encoder_refresh_btn', 'drivers hardware detection'),
    ('Export watermark', 1, 'watermark_check', 'animated capture card shares'),
    ('Seek bar preview', 1, 'timeline_preview_strip_enabled_check', 'timeline thumbnails editor'),
    ('Hide deleted timeline segments', 1, 'hide_deleted_timeline_segments_check', 'editor cuts trim'),
    ('Edited clip duration', 1, 'grid_show_edited_duration_check', 'length grid deleted segments'),
    ('Game profiles', 7, 'profiles_widget', 'per game executable overrides capture settings fps resolution bitrate camera audio'),
    ('Audio capture', 2, 'audio_capture_check', 'sound recording system'),
    ('Playback device', 2, 'playback_combo', 'speakers headphones output sound'),
    ('Microphone device', 2, 'mic_combo', 'input mic recording'),
    ('Microphone volume', 2, 'mic_vol_slider', 'gain loudness level'),
    ('Microphone mono', 2, 'mic_mono_check', 'stereo downmix'),
    ('Microphone monitoring', 2, 'mic_loopback_check', 'listen loopback preview'),
    ('Separate audio tracks', 2, 'audio_separation_check', 'programs applications devices multitrack'),
    ('Audio source mode', 2, 'audio_source_combo', 'programs applications devices individual multitrack'),
    ('Notification sounds', 2, 'notification_sounds_check', 'volume alerts'),
    ('Notification monitor', 2, 'notif_monitor_combo', 'display screen capture card'),
    ('Camera overlay', 3, 'camera_check', 'webcam video position size'),
    ('Camera device', 3, 'camera_device_combo', 'webcam source'),
    ('Keyboard overlay', 3, 'keyboard_overlay_check', 'keys input visualization'),
    ('Image overlays', 3, 'image_overlay_check', 'logo picture opacity'),
    ('Theme and colors', 4, '_customize_page._colors_section', 'appearance accent background customize'),
    ('Typography', 4, '_customize_page._typography_section', 'font text size customize'),
    ('Ultra performance mode', 5, 'ultra_performance_check', 'minimal capture only pause library uploads overlays'),
    ('UI performance', 5, 'performance_clip_preview_check', 'hover previews memory cpu gpu'),
    ('Pause background UI', 5, 'performance_background_pause_check', 'idle hidden minimize efficiency'),
    ('Encoder performance preset', 5, 'performance_encoder_section', 'nvidia nvenc speed quality'),
    ('Keyboard shortcuts', 6, 'shortcut_settings', 'bindings hotkeys navigation keys'),
    ('Only allowed games', 8, 'capture_admission_widget', 'allowlist whitelist privacy executable approved capture'),
    ('Install a plugin', 9, 'plugin_settings_widget.choose_button', 'plugins add package zip theme export presets'),
    ('Installed plugins', 9, 'plugin_settings_widget.installed_heading', 'manage enable disable apply remove uninstall'),
))


def search_settings(query):
    words = query.casefold().split()
    if not words:
        return ()
    matches = [entry for entry in DESTINATIONS if all(
        word in f'{entry.title} {CATEGORY_NAMES[entry.category]} {entry.keywords}'.casefold()
        for word in words)]
    return tuple(sorted(matches, key=lambda entry: (
        not all(word in entry.title.casefold() for word in words), entry.category)))


class SettingsSearchField(QLineEdit):
    results_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('settingsSearch')
        self.setPlaceholderText('Search settings…')
        self.setAccessibleName('Search settings')
        # Qt's built-in clear button fades even with application effects off.
        # An ordinary trailing action changes visibility immediately.
        self._clear_action = self.addAction(
            load_icon('close.png', 16), QLineEdit.ActionPosition.TrailingPosition)
        self._clear_action.setText('Clear search')
        self._clear_action.setVisible(False)
        self._clear_action.triggered.connect(self.clear)
        self.textChanged.connect(lambda text: self._clear_action.setVisible(bool(text)))
        self.setMinimumWidth(160)
        self.setMaximumWidth(280)
        set_theme_style(self, lineedit_qss)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Down and self.text().strip():
            self.results_requested.emit()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.clear()
            self.dismissed.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class SettingsSearchResults(QTreeWidget):
    """Category groups stay lazy: expanding a result never builds its page."""
    destination_requested = Signal(object)
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('settingsSearchResults')
        self.setAccessibleName('Settings search results grouped by category')
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(24)
        self.setWordWrap(True)
        self.setExpandsOnDoubleClick(False)
        self._collapsed_categories = set()
        self.itemCollapsed.connect(lambda item: self._remember_expansion(item, False))
        self.itemExpanded.connect(lambda item: self._remember_expansion(item, True))
        set_theme_style(self, lambda: f"""
            QTreeWidget {{ background: {Colors.BG}; color: {Colors.TEXT};
                border: none; font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px; }}
            QTreeWidget::item {{ padding: 8px 12px; border: 1px solid transparent; }}
            QTreeWidget::item:selected {{ background: {Colors.ACCENT_SOFT};
                color: {Colors.TEXT}; border-color: {Colors.ACCENT}; }}
            QTreeWidget::item:hover {{ background: {Colors.SURFACE_2}; }}
        """)
        self.itemClicked.connect(self.open_item)
        self.itemActivated.connect(self.open_item)

    def _remember_expansion(self, item, expanded):
        category = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if category is not None:
            if expanded:
                self._collapsed_categories.discard(category)
            else:
                self._collapsed_categories.add(category)

    def set_query(self, query):
        self.clear()
        groups = {}
        for entry in search_settings(query):
            groups.setdefault(entry.category, []).append(entry)
        first_result = None
        for category in sorted(groups):
            entries = groups[category]
            group = QTreeWidgetItem([f'{CATEGORY_NAMES[category]} ({len(entries)})'])
            group.setData(0, Qt.ItemDataRole.UserRole + 1, category)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            self.addTopLevelItem(group)
            for entry in entries:
                item = QTreeWidgetItem([entry.title])
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                group.addChild(item)
            expanded = category not in self._collapsed_categories
            group.setExpanded(expanded)
            if first_result is None:
                first_result = group.child(0) if expanded else group
        if first_result is not None:
            self.setCurrentItem(first_result)
        else:
            item = QTreeWidgetItem(['No settings found. Try “microphone”, “folder” or “profiles”.'])
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addTopLevelItem(item)

    def open_item(self, item, column=0):
        entry = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if entry is not None:
            self.destination_requested.emit(entry)
        elif item is not None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.dismissed.emit()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open_item(self.currentItem())
            event.accept()
        else:
            super().keyPressEvent(event)
