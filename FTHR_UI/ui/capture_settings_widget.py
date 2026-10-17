"""
capture_settings_widget.py
Compact single-row settings bar with hardware encoding status indicator
and capture source (desktop / window) selector.
"""

import sys
import ctypes
from html import escape
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                              QPushButton, QFrame, QSizePolicy,
                              QFileIconProvider)
from PySide6.QtCore import Qt, QSize, QFileInfo, Signal
from PySide6.QtGui import QIcon, QCursor
from ui.style import set_theme_style
from ui.style import Colors, Fonts, Sizes, ThemedDropdownArrow, WheelSafeComboBox
from ui.window_preview import WindowPreview
from core.clip_grouping import clip_group_for_source

if sys.platform == 'win32':
    import ctypes.wintypes as wintypes


# Window enumeration — Windows only.
# capture via wlr-screencopy; per-window targeting is not available.

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
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD))
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return None

        try:
            buf  = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass
    return None


def _enumerate_capturable_windows():
    """List visible, titled capture targets using Win32, hyprctl, or xdotool.

    Records contain hwnd, title, display_name, exe_path, exe_name, is_game,
    and icon (QIcon or None).
    is_game is a borderless/fullscreen heuristic.
    """
    if sys.platform != 'win32':
        from core.game_detector import _enumerate_linux_windows
        return _enumerate_linux_windows()


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
            'exe_path': '',
            'exe_name': '',
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
        entry['exe_path'] = exe or ''
        entry['exe_name'] = (exe.replace('/', '\\').rsplit('\\', 1)[-1]
                             if exe else '')
        entry['display_name'] = name or entry['title']
        entry['icon']         = icon

    # Sort: detected games first, then alphabetically by display name
    results.sort(key=lambda x: (not x['is_game'], x['display_name'].lower()))
    return results


# Shared combobox style (kept for capture-source picker). These are functions,
# rather than import-time constants, so Apply Theme can refresh this legacy
# compact widget along with the newer top-bar popup.
def _combo_style() -> str:
    return f'''
    QComboBox {{
        combobox-popup: 0;
        background-color: {Colors.SURFACE_2};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 3px 8px;
        color: {Colors.TEXT};
        font-size: 11px;
        font-family: {Fonts.BODY};
        min-width: 90px;
        max-height: 24px;
    }}
    QComboBox:hover {{
        background-color: {Colors.SURFACE_1};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0;
        height: 0;
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background-color: {Colors.SURFACE_2};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: 11px;
        selection-background-color: {Colors.SURFACE_3};
        selection-color: {Colors.ACCENT};
        padding: 2px;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 22px;
        padding-left: 4px;
    }}
'''

def _label_style() -> str:
    return f'''
    color: {Colors.TEXT_DIM};
    font-size: 10px;
    font-family: {Fonts.DISPLAY};
    letter-spacing: 0px;
'''

def _restart_style() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.ACCENT};
        border: none;
        color: {Colors.BG};
        font-size: 10px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        letter-spacing: 0px;
        padding: 4px 12px;
        border-radius: {Sizes.RADIUS_MD}px;
    }}
    QPushButton:hover {{
        background-color: {Colors.TEXT};
    }}
    QPushButton:pressed {{
        background-color: {Colors.ACCENT_DIM};
        color: {Colors.TEXT};
    }}
'''


# SettingBlock — expandable tile replacing the label+combobox pattern

class SettingBlock(QWidget):
    """
    A clickable tile that shows a setting label + current value.
    Clicking it expands a list of options below the header.
    Only one block in a group should be open at a time — the parent manages this
    by connecting to `opened` and calling `collapse()` on siblings.
    """

    value_changed = Signal(str)   # emits the selected option string
    opened        = Signal(object)  # emits self so siblings can collapse

    _BLOCK_W = 110  # fixed width for each tile

    def __init__(self, label: str, options: list, current: str, parent=None):
        super().__init__(parent)
        self._label   = label
        self._options = options
        self._current = current
        self._expanded = False
        self._option_labels: list[QLabel] = []
        self._setup()

    def _setup(self):
        self.setFixedWidth(self._BLOCK_W)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.MinimumExpanding)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # header (always visible)
        self._header = QFrame()
        self._header.setObjectName('settingHeader')
        self._header.setFixedHeight(52)
        self._header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(self._header, lambda: (self._header_style(False)))

        h_layout = QVBoxLayout(self._header)
        h_layout.setContentsMargins(10, 8, 10, 8)
        h_layout.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setSpacing(0)

        self._label_widget = QLabel(self._label)
        set_theme_style(self._label_widget, lambda: (f'color: {Colors.TEXT_DIM}; font-size: 9px; font-family: {Fonts.DISPLAY};'
            'letter-spacing: 0px; font-weight: 600; background: transparent; border: none;'))
        top_row.addWidget(self._label_widget)
        top_row.addStretch()

        self._arrow = ThemedDropdownArrow()
        top_row.addWidget(self._arrow)

        self._value_widget = QLabel(self._current)
        set_theme_style(self._value_widget, lambda: (f'color: {Colors.TEXT}; font-size: 14px; font-family: {Fonts.DISPLAY};'
            'font-weight: 600; background: transparent; border: none;'))

        h_layout.addLayout(top_row)
        h_layout.addWidget(self._value_widget)
        outer.addWidget(self._header)

        # options panel (hidden by default)
        self._options_frame = QFrame()
        self._options_frame.setObjectName('settingOptions')
        set_theme_style(self._options_frame, lambda: (f'QFrame#settingOptions {{ background-color: {Colors.SURFACE_1};'
            f'border: 1px solid {Colors.BORDER_HI}; border-top: none; }}'))
        opts_layout = QVBoxLayout(self._options_frame)
        opts_layout.setContentsMargins(0, 4, 0, 4)
        opts_layout.setSpacing(0)

        for opt in self._options:
            lbl = QLabel(opt)
            is_selected = (opt == self._current)
            set_theme_style(lbl, lambda is_selected=is_selected: (self._option_style(is_selected)))
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

    def _header_style(self, expanded: bool) -> str:
        border_color = Colors.ACCENT if expanded else Colors.BORDER_HI
        bg = Colors.SURFACE_3 if expanded else Colors.SURFACE_2
        return (
            f'QFrame#settingHeader {{'
            f'  background-color: {bg};'
            f'  border: 1px solid {border_color};'
            f'}}'
        )

    def _option_style(self, selected: bool) -> str:
        if selected:
            return (
                f'color: {Colors.ACCENT}; font-size: 12px; font-family: {Fonts.DISPLAY};'
                f'font-weight: 600; background: transparent; padding-left: 2px;'
            )
        return (
            f'color: {Colors.TEXT_DIM}; font-size: 12px; font-family: {Fonts.DISPLAY};'
            'background: transparent; padding-left: 2px;'
        )

    def refresh_theme(self):
        """Reapply all local styles after the shared theme changes."""
        set_theme_style(self._header, lambda: (self._header_style(self._expanded)))
        set_theme_style(self._label_widget, lambda: (f'color: {Colors.TEXT_DIM}; font-size: 9px; font-family: {Fonts.DISPLAY};'
            'letter-spacing: 0px; font-weight: 600; background: transparent; border: none;'))
        self._arrow.refresh_theme()
        set_theme_style(self._value_widget, lambda: (f'color: {Colors.TEXT}; font-size: 14px; font-family: {Fonts.DISPLAY};'
            'font-weight: 600; background: transparent; border: none;'))
        set_theme_style(self._options_frame, lambda: (f'QFrame#settingOptions {{ background-color: {Colors.SURFACE_1};'
            f'border: 1px solid {Colors.BORDER_HI}; border-top: none; }}'))
        for i, label in enumerate(self._option_labels):
            set_theme_style(label, lambda i=i: (self._option_style(self._options[i] == self._current)))

    def toggle(self):
        if self._expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self):
        if self._expanded:
            return
        self._expanded = True
        set_theme_style(self._header, lambda: (self._header_style(True)))
        self._arrow.setExpanded(True)
        self._options_frame.setVisible(True)
        self.opened.emit(self)
        self.updateGeometry()

    def collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        set_theme_style(self._header, lambda: (self._header_style(False)))
        self._arrow.setExpanded(False)
        self._options_frame.setVisible(False)
        self.updateGeometry()

    def _select(self, option: str):
        self._current = option
        self._value_widget.setText(option)
        for i, lbl in enumerate(self._option_labels):
            set_theme_style(lbl, lambda i=i, option=option: (self._option_style(self._options[i] == option)))
        self.collapse()
        self.value_changed.emit(option)

    def set_value(self, option: str):
        """Programmatically update the displayed value without emitting."""
        self._current = option
        self._value_widget.setText(option)
        for i, lbl in enumerate(self._option_labels):
            set_theme_style(lbl, lambda i=i, option=option: (self._option_style(self._options[i] == option)))


class CaptureSettingsWidget(QWidget):
    """Single-row compact settings bar with hardware encoding status
    and capture source (Desktop / Window) selector."""

    clip_length_changed      = Signal(int)
    framerate_changed        = Signal(int)
    resolution_changed       = Signal(int, int)
    bitrate_changed          = Signal(int)
    restart_engine_requested = Signal()
    retry_hardware_encoding  = Signal()
    # Emitted when the user picks a new capture source and applies it.
    # Carries (capture_mode_str, hwnd_int) — e.g. ('desktop', 0) or ('window', 12345678)
    capture_source_changed   = Signal(str, int)

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

        # Row 1: four expandable setting blocks
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
        self.restart_button = QPushButton('Apply + restart')
        self.restart_button.setStyleSheet(_restart_style())
        self.restart_button.setVisible(False)
        self.restart_button.clicked.connect(self._on_restart_clicked)
        settings_row.addWidget(self.restart_button)

        main_layout.addLayout(settings_row)

        # Capture source row: Desktop | Window/Game picker
        source_row = QHBoxLayout()
        source_row.setSpacing(12)

        source_row.addWidget(self._label('Source'))

        self.capture_mode_combo = WheelSafeComboBox()
        self.capture_mode_combo.addItems(['Desktop', 'Window / Game'])
        self.capture_mode_combo.setStyleSheet(_combo_style())
        self.capture_mode_combo.setMaximumWidth(140)
        if self.current_capture_mode == 'window':
            self.capture_mode_combo.setCurrentIndex(1)
        self.capture_mode_combo.currentIndexChanged.connect(self._on_capture_mode_changed)
        source_row.addWidget(self.capture_mode_combo)

        # Window list — visible only in Window mode
        self.window_combo = WheelSafeComboBox()
        self.window_combo.setStyleSheet(_combo_style())
        self.window_combo.setMinimumWidth(260)
        self.window_combo.setIconSize(QSize(16, 16))
        self.window_combo.currentIndexChanged.connect(self._on_window_selected)
        window_column = QVBoxLayout()
        window_column.setContentsMargins(0, 0, 0, 0)
        window_column.setSpacing(5)
        window_column.addWidget(self.window_combo)
        self.window_preview = WindowPreview(self, eyebrow=False)
        self.window_preview.setMaximumWidth(300)
        self.window_preview.setVisible(False)
        window_column.addWidget(self.window_preview)
        source_row.addLayout(window_column)

        self.refresh_btn = QPushButton('Refresh')
        set_theme_style(self.refresh_btn, lambda: (f'''
            QPushButton {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
                padding: 3px 10px;
                color: {Colors.TEXT_DIM};
                font-size: 10px;
                font-family: {Fonts.DISPLAY};
                font-weight: 600;
                letter-spacing: 0px;
                max-height: 24px;
            }}
            QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.TEXT}; }}
        '''))
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

        # Hardware encoding status bar (hidden by default)
        self.hw_status_bar = QFrame()
        self.hw_status_bar.setObjectName('hwStatusBar')
        self._hardware_status_success = False
        self._apply_hw_status_style()
        self.hw_status_bar.setVisible(False)  # Hidden until failure

        hw_status_layout = QHBoxLayout(self.hw_status_bar)
        hw_status_layout.setContentsMargins(0, 0, 0, 0)
        hw_status_layout.setSpacing(12)

        # Warning icon + message
        warning_label = QLabel('Warning')
        set_theme_style(warning_label, lambda: (f'color: {Colors.TEXT}; font-size: 14px;'))
        hw_status_layout.addWidget(warning_label)

        self.hw_status_message = QLabel('Hardware encoding initialization failed - using software encoder (slower)')
        set_theme_style(self.hw_status_message, lambda: (f'''
            color: {Colors.TEXT};
            font-size: 11px;
            font-family: {Fonts.BODY};
            font-weight: 600;
        '''))
        hw_status_layout.addWidget(self.hw_status_message)

        hw_status_layout.addStretch()

        # Retry button
        self.retry_button = QPushButton('Retry')
        set_theme_style(self.retry_button, lambda: (f'''
            QPushButton {{
                background-color: {Colors.TEXT};
                border: none;
                color: {Colors.ERROR};
                font-size: 10px;
                font-family: {Fonts.DISPLAY};
                font-weight: 600;
                letter-spacing: 0px;
                padding: 4px 16px;
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QPushButton:hover {{
                background-color: {Colors.SURFACE_3};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SURFACE_2};
            }}
        '''))
        self.retry_button.clicked.connect(self._on_retry_hardware_encoding)
        hw_status_layout.addWidget(self.retry_button)

        main_layout.addWidget(self.hw_status_bar)

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_label_style())
        return lbl

    def _separator(self):
        sep = QLabel('|')
        set_theme_style(sep, lambda: (f'color: {Colors.BORDER_HI}; font-size: 12px;'))
        return sep

    def _combo(self, items, default_index, callback):
        combo = WheelSafeComboBox()
        combo.addItems(items)
        combo.setCurrentIndex(default_index)
        combo.setStyleSheet(_combo_style())
        combo.currentIndexChanged.connect(callback)
        return combo

    # Block management

    def _on_block_opened(self, opened_block):
        for block in self._setting_blocks:
            if block is not opened_block:
                block.collapse()

    # Signal handlers (block-based)

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
            self.settings_manager.set('target_window_name', '')
            self.settings_manager.set('target_clip_group', '')
            self.window_preview.set_window(None)
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
        self.window_preview.set_window(entry)
        self.settings_manager.set('target_hwnd', self.current_target_hwnd)
        self.settings_manager.set('target_window_name',
                                  entry.get('display_name', entry.get('title', '')))
        self.settings_manager.set('target_clip_group',
                                  clip_group_for_source(entry))
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
            if w.get('icon'):
                self.window_combo.addItem(w['icon'], label)
            else:
                self.window_combo.addItem(label)
            self.window_combo.setItemData(
                i, escape(str(w.get('title', '') or '')),
                Qt.ItemDataRole.ToolTipRole)
            if w['hwnd'] == self.current_target_hwnd:
                select_idx = i

        if self._window_list:
            self.window_combo.setCurrentIndex(select_idx)
            entry = self._window_list[select_idx]
            self.current_target_hwnd = entry['hwnd']
            self.settings_manager.set('target_hwnd', self.current_target_hwnd)
            self.window_preview.set_window(entry, force_refresh=True)
        else:
            self.window_preview.set_window(None)

        self.window_combo.blockSignals(False)

    def _update_window_picker_visibility(self):
        show = (self.current_capture_mode == 'window')
        self.window_combo.setVisible(show)
        self.refresh_btn.setVisible(show)
        self.window_preview.setVisible(show)

    def _show_restart_button(self):
        self.restart_needed = True
        self.restart_button.setVisible(True)

    def _on_restart_clicked(self):
        self.restart_button.setVisible(False)
        self.restart_needed = False
        self.restart_engine_requested.emit()

    def _on_retry_hardware_encoding(self):
        self.retry_hardware_encoding.emit()

    def _apply_hw_status_style(self, success: bool = False):
        self._hardware_status_success = bool(success)
        background = Colors.SUCCESS if success else Colors.ERROR
        border = Colors.ACCENT if success else Colors.DELETE
        self.hw_status_bar.setStyleSheet(f'''
            QFrame#hwStatusBar {{
                background-color: {background};
                border: 1px solid {border};
                border-radius: {Sizes.RADIUS_MD}px;
                padding: 8px 12px;
            }}
        ''')

    def refresh_theme(self):
        """Refresh compact capture controls after Apply Theme."""
        for block in getattr(self, '_setting_blocks', []):
            block.refresh_theme()
        if hasattr(self, 'restart_button'):
            self.restart_button.setStyleSheet(_restart_style())
        for combo in self.findChildren(WheelSafeComboBox):
            combo.setStyleSheet(_combo_style())
        if hasattr(self, 'window_preview'):
            self.window_preview.refresh_theme()
        if hasattr(self, 'refresh_btn'):
            set_theme_style(self.refresh_btn, lambda: (f'''
                QPushButton {{
                    background-color: {Colors.SURFACE_2};
                    border: 1px solid {Colors.BORDER_HI};
                    border-radius: {Sizes.RADIUS_MD}px;
                    padding: 3px 10px;
                    color: {Colors.TEXT_DIM};
                    font-size: 10px;
                    font-family: {Fonts.DISPLAY};
                    font-weight: 600;
                    letter-spacing: 0px;
                    max-height: 24px;
                }}
                QPushButton:hover {{
                    border-color: {Colors.ACCENT}; color: {Colors.TEXT};
                }}
            '''))
        if hasattr(self, 'hw_status_bar'):
            self._apply_hw_status_style(self._hardware_status_success)
        if hasattr(self, 'hw_status_message'):
            set_theme_style(self.hw_status_message, lambda: (f'''
                color: {Colors.TEXT}; font-size: 11px;
                font-family: {Fonts.BODY}; font-weight: 600;
            '''))
        if hasattr(self, 'retry_button'):
            set_theme_style(self.retry_button, lambda: (f'''
                QPushButton {{
                    background-color: {Colors.TEXT}; border: none;
                    color: {Colors.ERROR}; font-size: 10px;
                    font-family: {Fonts.DISPLAY}; font-weight: 600;
                    letter-spacing: 0px; padding: 4px 16px; border-radius: {Sizes.RADIUS_MD}px;
                }}
                QPushButton:hover {{ background-color: {Colors.SURFACE_3}; }}
                QPushButton:pressed {{ background-color: {Colors.SURFACE_2}; }}
            '''))

    def _update_bitrate(self):
        if self.current_bitrate_level == 'custom':
            bitrate = int(self.settings_manager.get('custom_bitrate_kbps', 25000))
        else:
            presets = self.BITRATE_PRESETS.get(
                self.current_resolution, self.BITRATE_PRESETS['source'])
            bitrate = presets.get(self.current_bitrate_level, presets['high'])
        self.bitrate_changed.emit(bitrate)

    def _get_dims(self, name):
        return {'480p': (854, 480), '720p': (1280, 720),
                '1080p': (1920, 1080), '1440p': (2560, 1440),
                'source': (0, 0)}.get(name, (0, 0))

    # Hardware encoding status

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
        self._apply_hw_status_style(success=True)
        self.hw_status_message.setText('Hardware encoding initialized successfully')
        self.hw_status_bar.setVisible(True)
        
        # Hide after 3 seconds
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, self.hide_hardware_encoding_error)

    # Getters

    def get_clip_length(self):    return self.current_clip_length
    def get_framerate(self):      return self.current_framerate
    def get_resolution(self):     return self._get_dims(self.current_resolution)
    def get_bitrate(self):        return self.BITRATE_PRESETS[self.current_resolution][self.current_bitrate_level]
    def get_capture_mode(self):   return self.current_capture_mode   # 'desktop' or 'window'
    def get_target_hwnd(self):    return self.current_target_hwnd    # int (0 = none)
