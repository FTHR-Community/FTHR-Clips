"""
capture_settings_widget.py
Compact single-row settings bar with hardware encoding status indicator
and capture source (desktop / window) selector.
"""
import sys
import ctypes
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                              QComboBox, QPushButton, QFrame, QApplication,
                              QSizePolicy, QFileIconProvider)
from PyQt6.QtCore import Qt, QSize, QFileInfo, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QIcon, QImage, QPixmap, QCursor
from ui.style import Colors

if sys.platform == 'win32':
    import ctypes.wintypes as wintypes


# ---------------------------------------------------------------------------
# Window enumeration — Windows only.
# Returns empty list on Linux: the Linux engine only supports full-desktop
# capture via wlr-screencopy; per-window targeting is not available.
# ---------------------------------------------------------------------------

def _get_exe_info(exe_path: str, size: int = 16) -> 'tuple[str | None, QIcon | None]':
    if sys.platform != 'win32':
        return None, None
    name = None
    try:
        class _SHFILEINFOW(ctypes.Structure):
            _fields_ = [
                ('hIcon',         ctypes.c_size_t),
                ('iIcon',         ctypes.c_int),
                ('dwAttributes',  ctypes.c_ulong),
                ('szDisplayName', ctypes.c_wchar * 260),
                ('szTypeName',    ctypes.c_wchar * 80),
            ]

        SHGFI_DISPLAYNAME = 0x000000200
        shfi = _SHFILEINFOW()
        ret  = ctypes.windll.shell32.SHGetFileInfoW(
            exe_path, 0, ctypes.byref(shfi), ctypes.sizeof(shfi),
            SHGFI_DISPLAYNAME)
        if ret:
            name = shfi.szDisplayName.strip() or None
            if name and name.lower().endswith('.exe'):
                name = name[:-4]
    except Exception:
        pass

    try:
        provider = QFileIconProvider()
        icon = provider.icon(QFileInfo(exe_path))
        if icon.isNull():
            icon = None
    except Exception:
        icon = None

    return name, icon


def _get_hwnd_exe_path(hwnd: int) -> 'str | None':
    if sys.platform != 'win32':
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        pid       = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return None

        try:
            buf  = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass
    return None


def _enumerate_capturable_windows():
    """
    Return a list of dicts for every visible, titled window that is a
    reasonable capture target.  Each dict has:
        hwnd         : int        — Windows window handle
        title        : str        — raw window title bar text
        display_name : str        — friendly app name (from exe version info)
        is_game      : bool       — heuristic: borderless or full-screen window
        icon         : QIcon|None — app icon extracted from the exe
    Returns [] on Linux (desktop-only capture).
    """
    if sys.platform != 'win32':
        return []

    user32 = ctypes.windll.user32
    GWL_STYLE    = -16
    GWL_EXSTYLE  = -20
    WS_CAPTION       = 0x00C00000
    WS_POPUP         = 0x80000000
    WS_EX_TOOLWINDOW = 0x00000080

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    results  = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        title_len = user32.GetWindowTextLengthW(hwnd)
        if title_len == 0:
            return True
        buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, buf, title_len + 1)
        title = buf.value.strip()
        if not title or title == 'Program Manager':
            return True

        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW:
            return True

        style         = user32.GetWindowLongW(hwnd, GWL_STYLE)
        is_borderless = bool((style & WS_POPUP) and not (style & WS_CAPTION))

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        is_fullscreen = (rect.left == 0 and rect.top == 0
                         and rect.right == screen_w and rect.bottom == screen_h)

        results.append({
            'hwnd':    hwnd,
            'title':   title,
            'is_game': is_borderless or is_fullscreen,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)

    # Enrich each entry with display_name + icon, caching per exe path so we
    # only call SHGetFileInfo once when the same app has multiple windows.
    exe_cache: dict = {}   # exe_path → (display_name, icon)
    for entry in results:
        exe = _get_hwnd_exe_path(entry['hwnd'])
        if exe:
            if exe not in exe_cache:
                exe_cache[exe] = _get_exe_info(exe, 16)
            name, icon = exe_cache[exe]
        else:
            name, icon = None, None
        entry['display_name'] = name or entry['title']
        entry['icon']         = icon

    # Sort: detected games first, then alphabetically by display name
    results.sort(key=lambda x: (not x['is_game'], x['display_name'].lower()))
    return results


# Shared combobox style (kept for capture-source picker)
_COMBO_STYLE = '''
    QComboBox {
        background-color: #000000;
        border: 1px solid #333333;
        border-radius: 0px;
        padding: 3px 8px;
        color: #ffffff;
        font-size: 11px;
        font-family: 'Segoe UI', sans-serif;
        min-width: 90px;
        max-height: 24px;
    }
    QComboBox:hover {
        border-color: #ffffff;
        background-color: #0a0a0a;
    }
    QComboBox::drop-down {
        border: none;
        width: 16px;
    }
    QComboBox::down-arrow {
        image: none;
        border-left: 3px solid transparent;
        border-right: 3px solid transparent;
        border-top: 5px solid #ffffff;
        margin-right: 6px;
    }
    QComboBox QAbstractItemView {
        background-color: #000000;
        border: 1px solid #ffffff;
        color: #ffffff;
        font-size: 11px;
        selection-background-color: #ffffff;
        selection-color: #000000;
        padding: 2px;
    }
    QComboBox QAbstractItemView::item {
        min-height: 22px;
        padding-left: 4px;
    }
'''

_LABEL_STYLE = '''
    color: #666666;
    font-size: 10px;
    font-family: 'Segoe UI', sans-serif;
    letter-spacing: 1px;
'''

_RESTART_STYLE = '''
    QPushButton {
        background-color: #ffffff;
        border: none;
        color: #000000;
        font-size: 10px;
        font-family: 'Segoe UI', sans-serif;
        font-weight: bold;
        letter-spacing: 1px;
        padding: 4px 12px;
        border-radius: 0px;
    }
    QPushButton:hover {
        background-color: #cccccc;
    }
    QPushButton:pressed {
        background-color: #aaaaaa;
    }
'''


# ---------------------------------------------------------------------------
# SettingBlock — expandable tile replacing the label+combobox pattern
# ---------------------------------------------------------------------------

class SettingBlock(QWidget):
    """
    A clickable tile that shows a setting label + current value.
    Clicking it expands a list of options below the header.
    Only one block in a group should be open at a time — the parent manages this
    by connecting to `opened` and calling `collapse()` on siblings.
    """

    value_changed = pyqtSignal(str)   # emits the selected option string
    opened        = pyqtSignal(object)  # emits self so siblings can collapse

    _BLOCK_W = 110  # fixed width for each tile

    def __init__(self, label: str, options: list, current: str, parent=None):
        super().__init__(parent)
        self._label   = label
        self._options = options
        self._current = current
        self._expanded = False
        self._option_labels: list[QLabel] = []
        self._setup()

    # ------------------------------------------------------------------
    def _setup(self):
        self.setFixedWidth(self._BLOCK_W)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.MinimumExpanding)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- header (always visible) ---
        self._header = QFrame()
        self._header.setObjectName('settingHeader')
        self._header.setFixedHeight(52)
        self._header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._header.setStyleSheet(self._header_style(False))

        h_layout = QVBoxLayout(self._header)
        h_layout.setContentsMargins(10, 8, 10, 8)
        h_layout.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setSpacing(0)

        self._label_widget = QLabel(self._label)
        self._label_widget.setStyleSheet(
            'color: #666666; font-size: 9px; font-family: "Segoe UI", sans-serif;'
            'letter-spacing: 1px; font-weight: bold; background: transparent; border: none;'
        )
        top_row.addWidget(self._label_widget)
        top_row.addStretch()

        self._arrow = QLabel('▾')
        self._arrow.setStyleSheet(
            'color: #555555; font-size: 9px; background: transparent; border: none;'
        )
        top_row.addWidget(self._arrow)

        self._value_widget = QLabel(self._current)
        self._value_widget.setStyleSheet(
            'color: #ffffff; font-size: 14px; font-family: "Segoe UI", sans-serif;'
            'font-weight: bold; background: transparent; border: none;'
        )

        h_layout.addLayout(top_row)
        h_layout.addWidget(self._value_widget)
        outer.addWidget(self._header)

        # --- options panel (hidden by default) ---
        self._options_frame = QFrame()
        self._options_frame.setObjectName('settingOptions')
        self._options_frame.setStyleSheet(
            'QFrame#settingOptions { background-color: #0a0a0a;'
            'border: 1px solid #333333; border-top: none; }'
        )
        opts_layout = QVBoxLayout(self._options_frame)
        opts_layout.setContentsMargins(0, 4, 0, 4)
        opts_layout.setSpacing(0)

        for opt in self._options:
            lbl = QLabel(opt)
            is_selected = (opt == self._current)
            lbl.setStyleSheet(self._option_style(is_selected))
            lbl.setFixedHeight(28)
            lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            lbl.setContentsMargins(10, 0, 10, 0)
            lbl.mousePressEvent = lambda e, o=opt: self._select(o)
            lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._option_labels.append(lbl)
            opts_layout.addWidget(lbl)

        self._options_frame.setVisible(False)
        outer.addWidget(self._options_frame)

        self._header.mousePressEvent = lambda e: self.toggle()

    # ------------------------------------------------------------------
    def _header_style(self, expanded: bool) -> str:
        border_color = Colors.ACCENT if expanded else '#333333'
        bg = '#111111' if expanded else '#000000'
        return (
            f'QFrame#settingHeader {{'
            f'  background-color: {bg};'
            f'  border: 1px solid {border_color};'
            f'}}'
        )

    def _option_style(self, selected: bool) -> str:
        if selected:
            return (
                f'color: {Colors.ACCENT}; font-size: 12px; font-family: "Segoe UI", sans-serif;'
                f'font-weight: bold; background: transparent; padding-left: 2px;'
            )
        return (
            'color: #aaaaaa; font-size: 12px; font-family: "Segoe UI", sans-serif;'
            'background: transparent; padding-left: 2px;'
        )

    # ------------------------------------------------------------------
    def toggle(self):
        if self._expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self):
        if self._expanded:
            return
        self._expanded = True
        self._header.setStyleSheet(self._header_style(True))
        self._arrow.setText('▴')
        self._options_frame.setVisible(True)
        self.opened.emit(self)
        self.updateGeometry()

    def collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self._header.setStyleSheet(self._header_style(False))
        self._arrow.setText('▾')
        self._options_frame.setVisible(False)
        self.updateGeometry()

    # ------------------------------------------------------------------
    def _select(self, option: str):
        self._current = option
        self._value_widget.setText(option)
        for i, lbl in enumerate(self._option_labels):
            lbl.setStyleSheet(self._option_style(self._options[i] == option))
        self.collapse()
        self.value_changed.emit(option)

    def set_value(self, option: str):
        """Programmatically update the displayed value without emitting."""
        self._current = option
        self._value_widget.setText(option)
        for i, lbl in enumerate(self._option_labels):
            lbl.setStyleSheet(self._option_style(self._options[i] == option))


class CaptureSettingsWidget(QWidget):
    """Single-row compact settings bar with hardware encoding status
    and capture source (Desktop / Window) selector."""

    clip_length_changed      = pyqtSignal(int)
    framerate_changed        = pyqtSignal(int)
    resolution_changed       = pyqtSignal(int, int)
    bitrate_changed          = pyqtSignal(int)
    restart_engine_requested = pyqtSignal()
    retry_hardware_encoding  = pyqtSignal()
    # Emitted when the user picks a new capture source and applies it.
    # Carries (capture_mode_str, hwnd_int) — e.g. ('desktop', 0) or ('window', 12345678)
    capture_source_changed   = pyqtSignal(str, int)

    BITRATE_PRESETS = {
        '480p':   {'low': 2500,  'medium': 5000,  'high': 10000},
        '720p':   {'low': 5000,  'medium': 12000, 'high': 20000},
        '1080p':  {'low': 10000, 'medium': 25000, 'high': 50000},
        '1440p':  {'low': 15000, 'medium': 35000, 'high': 60000},
        'source': {'low': 10000, 'medium': 25000, 'high': 50000},
    }

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings_manager      = settings_manager
        self.current_clip_length   = settings_manager.get('clip_length',    30)
        self.current_framerate     = settings_manager.get('framerate',       60)
        self.current_resolution    = settings_manager.get('resolution',      'source')
        self.current_bitrate_level = settings_manager.get('bitrate_level',  'high')
        self.current_capture_mode  = settings_manager.get('capture_mode',   'desktop')
        self.current_target_hwnd   = settings_manager.get('target_hwnd',    0)
        self.restart_needed        = False
        # Cached window list: list of {'hwnd', 'title', 'is_game'}
        self._window_list          = []
        self._setup_ui()
        self._update_bitrate()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(8)

        # --- Row 1: four expandable setting blocks ---
        settings_row = QHBoxLayout()
        settings_row.setSpacing(8)
        settings_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Clip Length block
        _clip_labels = ['5s', '10s', '15s', '30s', '45s', '1m', '2m', '3m', '5m', '10m', '15m']
        _clip_values = [5, 10, 15, 30, 45, 60, 120, 180, 300, 600, 900]
        _clip_cur = _clip_labels[_clip_values.index(self.current_clip_length)] \
            if self.current_clip_length in _clip_values else '30s'
        self.clip_block = SettingBlock('CLIP LENGTH', _clip_labels, _clip_cur)
        self.clip_block.value_changed.connect(self._on_clip_block_changed)
        self.clip_block.opened.connect(self._on_block_opened)
        settings_row.addWidget(self.clip_block)

        # Framerate block
        _fps_labels = ['30', '60', '120', '144', '165', '240', '360']
        _fps_values = [30, 60, 120, 144, 165, 240, 360]
        _fps_cur = str(self.current_framerate) \
            if self.current_framerate in _fps_values else '60'
        self.fps_block = SettingBlock('FRAMERATE', _fps_labels, _fps_cur)
        self.fps_block.value_changed.connect(self._on_fps_block_changed)
        self.fps_block.opened.connect(self._on_block_opened)
        settings_row.addWidget(self.fps_block)

        # Resolution block
        _res_labels = ['480p', '720p', '1080p', '1440p', 'Source']
        _res_map    = {'480p': '480p', '720p': '720p', '1080p': '1080p',
                       '1440p': '1440p', 'source': 'Source'}
        _res_cur = _res_map.get(self.current_resolution.lower(), 'Source')
        self.res_block = SettingBlock('RESOLUTION', _res_labels, _res_cur)
        self.res_block.value_changed.connect(self._on_res_block_changed)
        self.res_block.opened.connect(self._on_block_opened)
        settings_row.addWidget(self.res_block)

        # Quality block
        _quality_labels = ['Low', 'Medium', 'High']
        _quality_map = {'low': 'Low', 'medium': 'Medium', 'high': 'High'}
        _quality_cur = _quality_map.get(self.current_bitrate_level, 'High')
        self.quality_block = SettingBlock('QUALITY', _quality_labels, _quality_cur)
        self.quality_block.value_changed.connect(self._on_quality_block_changed)
        self.quality_block.opened.connect(self._on_block_opened)
        settings_row.addWidget(self.quality_block)

        self._setting_blocks = [
            self.clip_block, self.fps_block, self.res_block, self.quality_block
        ]

        settings_row.addStretch()

        # Restart button
        self.restart_button = QPushButton('APPLY + RESTART')
        self.restart_button.setStyleSheet(_RESTART_STYLE)
        self.restart_button.setVisible(False)
        self.restart_button.clicked.connect(self._on_restart_clicked)
        settings_row.addWidget(self.restart_button)

        main_layout.addLayout(settings_row)

        # ------------------------------------------------------------------
        # Capture source row: Desktop | Window/Game picker
        # ------------------------------------------------------------------
        source_row = QHBoxLayout()
        source_row.setSpacing(12)

        source_row.addWidget(self._label('SOURCE'))

        self.capture_mode_combo = QComboBox()
        self.capture_mode_combo.addItems(['Desktop', 'Window / Game'])
        self.capture_mode_combo.setStyleSheet(_COMBO_STYLE)
        self.capture_mode_combo.setMaximumWidth(140)
        if self.current_capture_mode == 'window':
            self.capture_mode_combo.setCurrentIndex(1)
        self.capture_mode_combo.currentIndexChanged.connect(self._on_capture_mode_changed)
        source_row.addWidget(self.capture_mode_combo)

        # Window list — visible only in Window mode
        self.window_combo = QComboBox()
        self.window_combo.setStyleSheet(_COMBO_STYLE)
        self.window_combo.setMinimumWidth(260)
        self.window_combo.setIconSize(QSize(16, 16))
        self.window_combo.currentIndexChanged.connect(self._on_window_selected)
        source_row.addWidget(self.window_combo)

        self.refresh_btn = QPushButton('REFRESH')
        self.refresh_btn.setStyleSheet('''
            QPushButton {
                background-color: #000000;
                border: 1px solid #333333;
                border-radius: 0px;
                padding: 3px 10px;
                color: #666666;
                font-size: 10px;
                font-family: 'Segoe UI', sans-serif;
                font-weight: bold;
                letter-spacing: 1px;
                max-height: 24px;
            }
            QPushButton:hover { border-color: #ffffff; color: #ffffff; }
        ''')
        self.refresh_btn.clicked.connect(self._refresh_window_list)
        source_row.addWidget(self.refresh_btn)

        source_row.addStretch()
        main_layout.addLayout(source_row)

        # Show / hide window picker based on current mode
        self._update_window_picker_visibility()

        # If we start in window mode, populate the list immediately so the user
        # doesn't have to click REFRESH before seeing anything.
        if self.current_capture_mode == 'window':
            prev_hwnd = self.current_target_hwnd
            self._refresh_window_list()
            # If the saved HWND wasn't found (stale / different session) and a
            # new window was auto-selected, prompt the user to restart the engine
            # so it picks up the correct capture target.
            if self.current_target_hwnd != prev_hwnd:
                self._show_restart_button()

        # ------------------------------------------------------------------
        # Hardware encoding status bar (hidden by default)
        # ------------------------------------------------------------------
        self.hw_status_bar = QFrame()
        self.hw_status_bar.setObjectName('hwStatusBar')
        self.hw_status_bar.setStyleSheet('''
            QFrame#hwStatusBar {
                background-color: #cc0000;
                border: 1px solid #ff0000;
                border-radius: 0px;
                padding: 8px 12px;
            }
        ''')
        self.hw_status_bar.setVisible(False)  # Hidden until failure

        hw_status_layout = QHBoxLayout(self.hw_status_bar)
        hw_status_layout.setContentsMargins(0, 0, 0, 0)
        hw_status_layout.setSpacing(12)

        # Warning icon + message
        warning_label = QLabel('⚠')
        warning_label.setStyleSheet('color: #ffffff; font-size: 14px;')
        hw_status_layout.addWidget(warning_label)

        self.hw_status_message = QLabel('Hardware encoding initialization failed - using software encoder (slower)')
        self.hw_status_message.setStyleSheet('''
            color: #ffffff;
            font-size: 11px;
            font-family: 'Segoe UI', sans-serif;
            font-weight: bold;
        ''')
        hw_status_layout.addWidget(self.hw_status_message)

        hw_status_layout.addStretch()

        # Retry button
        self.retry_button = QPushButton('RETRY')
        self.retry_button.setStyleSheet('''
            QPushButton {
                background-color: #ffffff;
                border: none;
                color: #cc0000;
                font-size: 10px;
                font-family: 'Segoe UI', sans-serif;
                font-weight: bold;
                letter-spacing: 1px;
                padding: 4px 16px;
                border-radius: 0px;
            }
            QPushButton:hover {
                background-color: #eeeeee;
            }
            QPushButton:pressed {
                background-color: #cccccc;
            }
        ''')
        self.retry_button.clicked.connect(self._on_retry_hardware_encoding)
        hw_status_layout.addWidget(self.retry_button)

        main_layout.addWidget(self.hw_status_bar)

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_LABEL_STYLE)
        return lbl

    def _separator(self):
        sep = QLabel('|')
        sep.setStyleSheet('color: #333333; font-size: 12px;')
        return sep

    def _combo(self, items, default_index, callback):
        combo = QComboBox()
        combo.addItems(items)
        combo.setCurrentIndex(default_index)
        combo.setStyleSheet(_COMBO_STYLE)
        combo.currentIndexChanged.connect(callback)
        return combo

    # ------------------------------------------------------------------
    # Block management
    # ------------------------------------------------------------------

    def _on_block_opened(self, opened_block):
        for block in self._setting_blocks:
            if block is not opened_block:
                block.collapse()

    # ------------------------------------------------------------------
    # Signal handlers (block-based)
    # ------------------------------------------------------------------

    def _on_clip_block_changed(self, label: str):
        label_to_sec = {'5s': 5, '10s': 10, '15s': 15, '30s': 30, '45s': 45,
                        '1m': 60, '2m': 120, '3m': 180, '5m': 300, '10m': 600, '15m': 900}
        self.current_clip_length = label_to_sec.get(label, 30)
        self.settings_manager.set('clip_length', self.current_clip_length)
        self.settings_manager.save_settings()
        self.clip_length_changed.emit(self.current_clip_length)

    def _on_fps_block_changed(self, label: str):
        self.current_framerate = int(label)
        self.settings_manager.set('framerate', self.current_framerate)
        self.settings_manager.save_settings()
        self._show_restart_button()
        self.framerate_changed.emit(self.current_framerate)

    def _on_res_block_changed(self, label: str):
        self.current_resolution = label.lower()
        self.settings_manager.set('resolution', self.current_resolution)
        self.settings_manager.save_settings()
        self._show_restart_button()
        self._update_bitrate()
        dims = self._get_dims(self.current_resolution)
        self.resolution_changed.emit(dims[0], dims[1])

    def _on_quality_block_changed(self, label: str):
        self.current_bitrate_level = label.lower()
        self.settings_manager.set('bitrate_level', self.current_bitrate_level)
        self.settings_manager.save_settings()
        self._update_bitrate()

    def _on_capture_mode_changed(self, index):
        self.current_capture_mode = 'window' if index == 1 else 'desktop'
        self.settings_manager.set('capture_mode', self.current_capture_mode)
        if self.current_capture_mode == 'desktop':
            self.current_target_hwnd = 0
            self.settings_manager.set('target_hwnd', 0)
        self._update_window_picker_visibility()
        if self.current_capture_mode == 'window':
            self._refresh_window_list()
        self._show_restart_button()
        self.settings_manager.save_settings()

    def _on_window_selected(self, index):
        if index < 0 or index >= len(self._window_list):
            return
        entry = self._window_list[index]
        self.current_target_hwnd = entry['hwnd']
        self.settings_manager.set('target_hwnd', self.current_target_hwnd)
        self.settings_manager.save_settings()
        self._show_restart_button()

    def _refresh_window_list(self):
        self._window_list = _enumerate_capturable_windows()
        self.window_combo.blockSignals(True)
        self.window_combo.clear()

        # Try to re-select the previously chosen HWND
        select_idx = 0
        for i, w in enumerate(self._window_list):
            label = ('★ ' if w['is_game'] else '') + w['display_name']
            if w['icon']:
                self.window_combo.addItem(w['icon'], label)
            else:
                self.window_combo.addItem(label)
            if w['hwnd'] == self.current_target_hwnd:
                select_idx = i

        if self._window_list:
            self.window_combo.setCurrentIndex(select_idx)
            entry = self._window_list[select_idx]
            self.current_target_hwnd = entry['hwnd']
            self.settings_manager.set('target_hwnd', self.current_target_hwnd)

        self.window_combo.blockSignals(False)

    def _update_window_picker_visibility(self):
        show = (self.current_capture_mode == 'window')
        self.window_combo.setVisible(show)
        self.refresh_btn.setVisible(show)

    def _show_restart_button(self):
        self.restart_needed = True
        self.restart_button.setVisible(True)

    def _on_restart_clicked(self):
        self.restart_button.setVisible(False)
        self.restart_needed = False
        self.restart_engine_requested.emit()

    def _on_retry_hardware_encoding(self):
        print("[UI] Retry hardware encoding requested")
        self.retry_hardware_encoding.emit()

    def _update_bitrate(self):
        bitrate = self.BITRATE_PRESETS[self.current_resolution][self.current_bitrate_level]
        print(f"→ Bitrate set to: {bitrate} kbps ({bitrate / 1000:.1f} Mbps)")
        self.bitrate_changed.emit(bitrate)

    def _get_dims(self, name):
        return {'480p': (854, 480), '720p': (1280, 720),
                '1080p': (1920, 1080), '1440p': (2560, 1440),
                'source': (0, 0)}.get(name, (0, 0))

    # ------------------------------------------------------------------
    # NEW: Public methods to control hardware encoding status bar
    # ------------------------------------------------------------------

    def show_hardware_encoding_error(self, message: str = None):
        """Show the red error bar when hardware encoding fails"""
        if message:
            self.hw_status_message.setText(message)
        self.hw_status_bar.setVisible(True)

    def hide_hardware_encoding_error(self):
        """Hide the error bar when hardware encoding succeeds"""
        self.hw_status_bar.setVisible(False)

    def show_hardware_encoding_success(self):
        """Briefly show success, then hide"""
        self.hw_status_bar.setStyleSheet('''
            QFrame#hwStatusBar {
                background-color: #00aa00;
                border: 1px solid #00ff00;
                border-radius: 0px;
                padding: 8px 12px;
            }
        ''')
        self.hw_status_message.setText('✓ Hardware encoding initialized successfully')
        self.hw_status_bar.setVisible(True)
        
        # Hide after 3 seconds
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(3000, self.hide_hardware_encoding_error)

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_clip_length(self):    return self.current_clip_length
    def get_framerate(self):      return self.current_framerate
    def get_resolution(self):     return self._get_dims(self.current_resolution)
    def get_bitrate(self):        return self.BITRATE_PRESETS[self.current_resolution][self.current_bitrate_level]
    def get_capture_mode(self):   return self.current_capture_mode   # 'desktop' or 'window'
    def get_target_hwnd(self):    return self.current_target_hwnd    # int (0 = none)