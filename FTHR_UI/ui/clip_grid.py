"""Virtualized library grid with background file discovery and thumbnail loading."""

from ui.style import set_theme_style
from ui.image_memory import ImageLRU, start_image_cache_maintenance
import os
import weakref
import subprocess
import threading
import hashlib
import math
import stat as stat_module
import time
import unicodedata
from bisect import bisect_right
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout,
    QMenu, QApplication,
    QPushButton, QSizePolicy, QLineEdit, QStackedWidget, QCheckBox,
    QTextEdit, QPlainTextEdit, QAbstractSpinBox,
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QRunnable, QThreadPool, QObject, QEvent,
    QFileSystemWatcher, QRect, QPoint,
    QUrl, QSize, QRectF, QPointF,
)
from PySide6.QtGui import QDesktopServices, QPixmap, QPainter, QColor, QLinearGradient, QPainterPath, QPen
from PySide6.QtGui import QImage, QImageReader, QIcon, QFontMetrics, QPixmapCache, QDrag
from ui.library_album_drag import AlbumDragMimeData, AlbumDragSnapshot
from ui.library_thumbnails import (
    _ImageThumbnailWorker, _ThumbnailWorker, _get_cached_duration_path,
    _get_cached_thumb_path, _get_negative_cache_path,
    get_cached_clip_metadata as get_cached_clip_metadata, read_cached_metadata,
)

from core.clip_files import (
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    is_completed_video_path,
    is_fthr_temporary_dir,
)
from core.library_selection import LibrarySelection
from core.library_index import (
    LibraryRecord, LibraryScanResult, canonical_media_path,
    scan_library, recorded_media_details,
)
from core.save_trace import SAVE_TRACE_ENABLED, mark_save_trace
from core.media_process import media_process_snapshot
from core.library_ownership import (
    MediaOwnership, classify_media_path, media_file_identity,
)
from core.clip_mutations import renamed_clip_path, rename_clip
from core.clip_metadata_types import MutationResult, FileOperationState
from shiboken6 import isValid
from core.audio_manifest import manifest_path_for
from core.theme_manager import ThemeManager
from core.library_tags import format_tags_text, normalize_tags, parse_tags_text
from core.library_searches import SearchCriteria, normalize_search_criteria
from core.library_covers import (
    MAX_COVER_PNG_BYTES, inspect_png,
)
from core.settings_manager import (
    StoragePathError, clips_directory_from, ensure_storage_directory,
)
from core.field_diagnostics import (
    emit_event, get_diagnostic_session, process_memory_bytes,
)
from ui.style import WheelSafeComboBox, paint_dropdown_arrow


class _DropdownCombo(WheelSafeComboBox):
    """QComboBox that shows icons/dropdown.png as its arrow, rotated when open."""

    _custom_arrow_managed = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False

    def showPopup(self):
        self._popup_open = True
        self.update()
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self._popup_open = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        paint_dropdown_arrow(p, self.rect(), self._popup_open)
        p.end()

from ui.style import (
    Colors, Fonts, Sizes,
    label_body, label_display, lineedit_qss,
    context_menu_qss, combo_qss, button_primary_qss, button_secondary_qss, checkbox_qss,
)
from ui.dialogs import FthrInputDialog, FthrMessageDialog
from ui.clip_cover_dialog import ClipCoverDialog


_MEDIA_FILTERS = (
    ('all', 'All clips'),
    ('clips', 'Clips only'),
    ('screenshots', 'Screenshots'),
    ('imported', 'Imported clips'),
    ('favorites', 'Favorites'),
)


class _MediaFilterCombo(_DropdownCombo):
    """Keep the combo selection while presenting hierarchical menu choices."""

    game_selected = Signal(str)

    def __init__(self, game_groups, parent=None):
        super().__init__(parent)
        self._game_groups = game_groups
        self._tag_menu = None
        self._tag_count = 0
        self._favorites_indicator = False
        self._game_selection = None
        self.addItems([label for _, label in _MEDIA_FILTERS])

    def set_filter(self, kind, game):
        self._game_selection = game
        for index, (_, label) in enumerate(_MEDIA_FILTERS):
            self.setItemText(index, label)
        game_index = len(_MEDIA_FILTERS)
        if game is not None:
            if self.count() == game_index:
                self.addItem(game)
            else:
                self.setItemText(game_index, game)
            self.setCurrentIndex(game_index)
        else:
            self.setCurrentIndex(next(
                index for index, (name, _) in enumerate(_MEDIA_FILTERS) if name == kind))
            if self.count() > game_index:
                self.removeItem(game_index)
        self._update_caption()

    def set_indicators(self, tag_count, favorites):
        self._tag_count = tag_count
        self._favorites_indicator = favorites
        self._update_caption()

    def _update_caption(self):
        index = self.currentIndex()
        if index < 0:
            return
        base = self._game_selection or _MEDIA_FILTERS[index][1]
        indicators = []
        if self._favorites_indicator and index != 4:
            indicators.append('Favorites')
        if self._tag_count:
            indicators.append(f'{self._tag_count} tag' + ('s' if self._tag_count != 1 else ''))
        label = ' · '.join((*reversed(indicators), base))
        self.setItemText(index, label)
        self.setToolTip(label + '\nFilter library media, games and tags')

    def showPopup(self):
        menu = QMenu(self)
        menu.setStyleSheet(context_menu_qss())
        menu.setMinimumWidth(self.width())
        for index, (_, label) in enumerate(_MEDIA_FILTERS):
            action = menu.addAction(label)
            action.setData(index)
            action.setProperty('fthrMediaFilterChoice', index)
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
        games = menu.addMenu('Games')
        groups = self._game_groups()
        for name in groups:
            action = games.addAction(name.replace('&', '&&'))
            action.setData(name)
            action.setProperty('fthrMediaFilterChoice', name)
            action.setCheckable(True)
            action.setChecked(self.currentIndex() == len(_MEDIA_FILTERS)
                              and name == self._game_selection)
        if not groups:
            games.addAction('No game clips').setEnabled(False)
        if self._tag_menu is not None:
            self._tag_menu.setTitle(f'Tags ({self._tag_count})' if self._tag_count else 'Tags')
            menu.addMenu(self._tag_menu)
        self._popup_open = True
        self.update()
        try:
            action = menu.exec(self.mapToGlobal(QPoint(0, self.height())))
            # Popup styling can reparent submenu actions; identify our choices
            # explicitly so game selections survive that native-menu lifecycle.
            choice = (action.property('fthrMediaFilterChoice')
                      if action is not None else None)
        finally:
            self.hidePopup()
            menu.deleteLater()
        if isinstance(choice, str):
            self.game_selected.emit(choice)
        elif isinstance(choice, int):
            self.setCurrentIndex(choice)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Space,
                           Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_F4):
            self.showPopup()
            event.accept()
            return
        super().keyPressEvent(event)


_GRID_EXCLUDED_DIRS = {'Exported', 'Shared'}
_VIDEO_EXTS = VIDEO_SUFFIXES
_IMAGE_EXTS = IMAGE_SUFFIXES


def _rgba(hex_color: str, alpha: int) -> str:
    """Turn a theme hex token into a QSS rgba color with the given alpha."""
    color = QColor(hex_color)
    return f'rgba({color.red()},{color.green()},{color.blue()},{alpha})'


def _is_grid_excluded_dir(path: str) -> bool:
    """Keep app-owned post-processing directories out of the library scan."""

    name = os.path.basename(os.fspath(path))
    return name in _GRID_EXCLUDED_DIRS or is_fthr_temporary_dir(name)


def _show_in_file_manager(file_path: str) -> None:
    """Open the containing folder and highlight ``file_path`` when possible."""

    path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
    subprocess.Popen(f'explorer.exe /select,"{path}"')
    return



# Cards flex with the viewport. The thumbnail *surface* remains 16:9, while
# source pixels fill it with a centered crop, preserving their aspect ratio.
_CARD_W      = 360  # preferred / cache sizing reference
_CARD_MIN_W  = 300
_CARD_MAX_W  = 440
_THUMB_H     = 180
_CARD_BODY_H = 88
_CARD_H      = _THUMB_H + _CARD_BODY_H
_MAX_MATERIALIZED_CARDS = 96
_VIRTUAL_OVERSCAN_ROWS = 2
_MAX_THUMBNAIL_QUEUE = 128
_ALBUM_UNSET = object()


def _humanize_relative(ts: float) -> str:
    """Turn a file mtime into a 'X days ago' / 'just now' style string."""
    now = datetime.now()
    when = datetime.fromtimestamp(ts)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return 'just now'
    mins = secs // 60
    if mins < 60:
        return f'{mins} min ago'
    hours = mins // 60
    if hours < 24:
        return f'{hours} hr ago'
    days = hours // 24
    if days == 1:
        return 'yesterday'
    if days < 7:
        return f'{days} days ago'
    weeks = days // 7
    if weeks < 5:
        return f'{weeks} wk ago'
    months = days // 30
    if months < 12:
        return f'{months} mo ago'
    return f'{days // 365} yr ago'


def _section_label_for(ts: float) -> str:
    """Readable date label for grouping cards under date headers."""
    return datetime.fromtimestamp(ts).strftime('%a, %b %d')


def _game_name_from_path(file_path: str, clips_root: str | None = None) -> str:
    """Best-effort game / source name from the parent folder."""
    parent = os.path.basename(os.path.dirname(file_path))
    root_name = os.path.basename(os.path.normpath(
        clips_root or os.path.expanduser('~/FTHR_Clips')))
    if parent in (root_name, ''):
        return 'Desktop'
    return parent


def _clip_title_from_filename(file_path: str) -> str:
    """Display the actual editable filename, without its media extension."""
    return Path(file_path).stem


class _CoverDecodeSignals(QObject):
    finished = Signal(str, str, object, object, int)


class _CoverDecodeWorker(QRunnable):
    """Read only a bounded cover-sized image away from the GUI thread."""

    def __init__(self, path, cache_key, reference, signals, cancel_event, job_id=0):
        super().__init__()
        self.path = path
        self.cache_key = cache_key
        self.reference = dict(reference)
        self.signals = signals
        self.cancel_event = cancel_event
        self.job_id = int(job_id)
        self._finished = False

    def _emit_finished(self, image, error):
        if self._finished:
            return
        self._finished = True
        self.signals.finished.emit(
            self.path, self.cache_key, image, error, self.job_id)

    def run(self):
        image = QImage()
        error = None
        try:
            if self.cancel_event.is_set():
                error = 'cancelled'
                return
            for candidate in (Path(self.path).parent.parent,
                              Path(self.path).parent, Path(self.path)):
                info = os.lstat(candidate)
                attributes = getattr(info, 'st_file_attributes', 0)
                if (os.path.islink(candidate)
                        or bool(attributes & getattr(
                            stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))):
                    raise OSError('cover asset namespace is not a safe file')
            before = os.stat(self.path)
            if before.st_size <= 0 or before.st_size > MAX_COVER_PNG_BYTES:
                raise ValueError('cover asset exceeds the 8 MiB limit')
            with open(self.path, 'rb') as source:
                payload = source.read(MAX_COVER_PNG_BYTES + 1)
            after_read = os.stat(self.path)
            if len(payload) > MAX_COVER_PNG_BYTES:
                raise ValueError('cover asset exceeds the 8 MiB limit')
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after_read.st_dev, after_read.st_ino,
                        after_read.st_size, after_read.st_mtime_ns)):
                raise OSError('cover asset changed while it was read')
            parsed = inspect_png(payload, require_normalized=True)
            if (hashlib.sha256(payload).hexdigest()
                    != self.reference.get('sha256')):
                raise OSError('cover asset digest does not match its reference')
            if (parsed[0] != self.reference.get('width')
                    or parsed[1] != self.reference.get('height')):
                raise OSError('cover asset dimensions do not match its reference')
            image = QImage.fromData(payload, 'PNG')
            if image.isNull():
                raise ValueError('cover image could not be decoded')
            size = image.size()
            if size.width() > 640 or size.height() > 360:
                size.scale(640, 360, Qt.AspectRatioMode.KeepAspectRatio)
                image = image.scaled(
                    size, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            if self.cancel_event.is_set():
                image = QImage()
                error = 'cancelled'
                return
        except (OSError, RuntimeError, TypeError, ValueError) as caught:
            error = str(caught)
        finally:
            self._emit_finished(image, error)


class _FileCollectSignals(QObject):
    # raw_files: set[str], sorted_pairs: list[(mtime, path)], imported: set[str], subdirs: list[str]
    finished = Signal(object, object, object, object)
    # generation, bounded discovery snapshot.  The final ``finished`` result
    # remains authoritative for global ordering and overlap deduplication.
    batch = Signal(int, object)


class _FileCollectWorker(QRunnable):
    """Scans folders, applies filter, sorts by mtime — all off the main thread."""

    def __init__(self, clips_dir: str, import_dirs: list,
                 filter_: str, sort_: str, cancel_event=None, recorded_media=None,
                 active_output_paths=()):
        super().__init__()
        self.clips_dir   = clips_dir
        self.import_dirs = import_dirs
        self.filter_     = filter_
        self.sort_       = sort_
        self.cancel_event = cancel_event or threading.Event()
        self.recorded_media = recorded_media or {}
        self.active_output_paths = frozenset(active_output_paths)
        self.result: LibraryScanResult | None = None
        self.signals     = _FileCollectSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self):
        generation = int(getattr(self, 'generation', 0))
        self.result = scan_library(
            self.clips_dir,
            self.import_dirs,
            filter_=self.filter_,
            sort_=self.sort_,
            cancel_event=self.cancel_event,
            recorded_media=self.recorded_media,
            active_output_paths=self.active_output_paths,
            on_batch=lambda records: self.signals.batch.emit(
                generation, records),
        )
        records = self.result.records
        found = {record.path for record in self.result.all_records}
        imported = {record.path for record in self.result.all_records
                    if record.imported}
        pairs = [(record.mtime, record.path) for record in records]
        self.signals.finished.emit(
            found, pairs, imported, list(self.result.watched_directories))



class _RenameLabel(QLabel):
    rename_requested = Signal()

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_full_text(self, text):
        self._full_text = text
        self.setToolTip(text + '\nDouble-click to rename; F2 also works')
        self._elide()

    def _elide(self):
        super().setText(QFontMetrics(self.font()).elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.rename_requested.emit()
            event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F2:
            self.rename_requested.emit()
        else:
            super().keyPressEvent(event)


class _RenameEdit(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class _CardFavoriteButton(QPushButton):
    """Draw the same centered star at any display scale."""

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.property('fthrInterfaceIconOverridden'):
            return
        override_size = self.property('fthrInterfaceIconSize')
        star_radius = 9.5 if override_size is None else min(override_size, self.width(), self.height()) / 2
        painter = QPainter(self)
        self._paint_star(painter, QRectF(self.rect()), star_radius)
        painter.end()

    def interface_icon_artwork(self):
        image = QPixmap(22, 22)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self._paint_star(painter, QRectF(image.rect()), 9.5)
        painter.end()
        return image

    def _paint_star(self, painter, bounds, star_radius):
        star = QPainterPath()
        for point in range(10):
            angle = point * math.pi / 5
            radius = star_radius if point % 2 == 0 else star_radius * (4.4 / 9.5)
            vertex = QPointF(radius * math.sin(angle), -radius * math.cos(angle))
            if point == 0:
                star.moveTo(vertex)
            else:
                star.lineTo(vertex)
        star.closeSubpath()

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(bounds.center() - star.boundingRect().center())
        color = QColor('#ffffff')
        if not self.isEnabled():
            color.setAlpha(90)
        pen = QPen(color, 1.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if self.isChecked():
            painter.setBrush(color)
        painter.drawPath(star)


class _CardShareButton(QPushButton):
    """Paint the share action with immediate hover feedback."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.action_name = 'upload'

    def hideEvent(self, event):
        self.setProperty('actionHovered', False)
        super().hideEvent(event)

    def paintEvent(self, event):
        if self.property('fthrInterfaceAppearance'):
            super().paintEvent(event)
            return
        hovered = self.property('actionHovered') or self.isDown()
        background = QColor(Colors.ACCENT_SOFT if hovered else Colors.ACCENT)
        tint = QColor(Colors.ACCENT if hovered else Colors.BG)
        border = QColor(Colors.ACCENT)
        if not self.isEnabled():
            background, tint, border = map(QColor, (Colors.SURFACE_3, Colors.TEXT_MUTED, Colors.BORDER))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(background)
        painter.setPen(QPen(QColor(Colors.TEXT) if self.hasFocus() else border,
                            2 if self.hasFocus() else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 4, 4)
        size = self.iconSize()
        icon_rect = QRect(QPoint((self.width() - size.width()) // 2,
                                (self.height() - size.height()) // 2), size)
        _card_icon(self.action_name, tint.name()).paint(painter, icon_rect)


class _WorkerCancellation:
    """Cancel one decode without cancelling the other visible thumbnails."""
    def __init__(self, parent_event):
        self.parent_event = parent_event
        self.local_event = threading.Event()

    def set(self):
        self.local_event.set()

    def is_set(self):
        return self.local_event.is_set() or self.parent_event.is_set()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else 0.05
            if remaining <= 0:
                return False
            self.local_event.wait(min(0.05, remaining))
        return True


def _card_icon(name: str, color: str, size: int = 28) -> QIcon:
    theme = ThemeManager()
    custom = theme.get_custom_icon_path(name + '.png')
    return _render_card_icon(name, color, size, str(custom) if custom else '')


@lru_cache(maxsize=128)
def _render_card_icon(name: str, color: str, size: int, custom: str) -> QIcon:
    from ui.icons import icon_pixmap
    return QIcon(icon_pixmap(name + '.png', size, color, custom_path=custom))


class _ToolbarCountLabel(QLabel):
    def __init__(self, text, parent=None):
        super().__init__('', parent)
        self._full_text = ''
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text)
        self.setAccessibleName(self._full_text)
        self._fit()

    def _fit(self):
        super().setText(QFontMetrics(self.font()).elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, '_full_text'):
            self._fit()


class _CardSelectionOutline(QWidget):
    """Paint above opaque thumbnail/body children without intercepting input."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.ACCENT), 2))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1),
                                Sizes.RADIUS_CARD, Sizes.RADIUS_CARD)
        painter.end()


class ClipThumbnail(QFrame):
    clicked = Signal(str)
    preload_requested = Signal(str)
    opened = Signal(str, QPixmap, QRect)
    deleted = Signal(str)
    renamed = Signal(str, str)
    upload_requested = Signal(str)
    library_flags_changed = Signal(str)
    overlap_requested = Signal(str)

    def __init__(self, file_path: str, is_video: bool = True, imported: bool = False,
                 upload_enabled: bool = False, uploaded: bool = False,
                 upload_info: dict | None = None, ready: bool = True,
                 card_width: int = _CARD_W, parent=None,
                 clips_root: str | None = None, defer_image_load: bool = False, metadata_manager=None):
        super().__init__(parent)
        self.file_path, self.is_video, self.imported = file_path, is_video, imported
        self.upload_enabled, self.uploaded = upload_enabled, uploaded
        self.upload_info = dict(upload_info or {})
        self.upload_link = ''
        self.ready = ready
        self._metadata_manager = metadata_manager
        self._library_generation = object()
        self._library_flags = None
        self._delete_identity = None
        self._clips_root = clips_root or os.path.expanduser('~/FTHR_Clips')
        self._interface_card_designer = None
        self._thumbnail_ready = False
        self._source_duration = 0.0
        self._duration_provider = None
        self._thumbnail_fingerprint = ''
        self._thumb_pixmap = QPixmap()
        self._cover_pixmap = QPixmap()
        self._cover_cache_key = ''
        self._card_width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(card_width)))
        self._thumb_height = round(self._card_width * 9 / 16)
        self._mutation_busy = False
        self._rename_handler = None
        self._trash_handler = None
        self._mutation_token = None
        self._mutation_guard_owned = False
        self._editing_name = False
        self._prepare_mutation = None
        self._finish_mutation = None
        self._wait_for_readers = None
        self._mutation_timer = QTimer(self)
        self._mutation_timer.setSingleShot(True)
        self._mutation_timer.timeout.connect(self._try_mutation)
        self._upload_status = ''
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._clear_action_feedback)
        self._selection_handler = None
        self._album_drag_handler = None
        self._thumbnail_press = None
        self._album_menu_provider = None
        self._album_record_provider = None
        self._album_membership_handler = None
        self._tag_details_provider = None
        self._tag_edit_handler = None
        self._cover_handler = None
        self._clip_actions_provider = None
        self._batch_locked = False
        self._overlap_paths = ()
        self.setObjectName('clipCard')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._setup_ui()
        self._selection_outline = _CardSelectionOutline(self)
        self.selection_checkbox = QCheckBox(self._thumb_frame)
        self.selection_checkbox.setObjectName('cardSelectionCheckbox')
        self.selection_checkbox.setAccessibleName('Select clip')
        self.selection_checkbox.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.selection_checkbox.setGeometry(8, 8, 28, 28)
        set_theme_style(self.selection_checkbox, checkbox_qss)
        self.selection_checkbox.hide()
        self.selection_checkbox.clicked.connect(self._selection_checkbox_clicked)
        self.resize_card(self._card_width, force=True)
        self.set_ready(ready)
        self.refresh_library_flags()
        if not is_video and not defer_image_load:
            self._load_image_thumbnail()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._thumb_frame = QFrame(self)
        self._thumb_frame.setObjectName('cardThumb')
        self._thumb_frame.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb_frame.mousePressEvent = self._on_thumbnail_click
        self._thumb_frame.mouseMoveEvent = self._on_thumbnail_move
        self._thumb_frame.mouseReleaseEvent = self._on_thumbnail_release
        self.thumb_label = QLabel(self._thumb_frame)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.thumb_label.setStyleSheet('background: transparent; border: none;')
        if self.is_video:
            self._play_icon = QLabel(self._thumb_frame)
            self._play_icon.setPixmap(_card_icon('play', Colors.TEXT, 36).pixmap(36, 36))
            self._play_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._play_icon.setStyleSheet('background: transparent; border: none;')
            self._play_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._play_icon.hide()
            self.duration_label = QLabel('0:00', self._thumb_frame)
            self.duration_label.setObjectName('cardDurationBadge')
            self.duration_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._imported_badge = QLabel('Imported', self._thumb_frame)
        self._imported_badge.setObjectName('cardImportedBadge')
        self._imported_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._imported_badge.setVisible(self.imported)
        self._upload_badge = QLabel('Uploaded', self._thumb_frame)
        self._upload_badge.setObjectName('cardUploadedBadge')
        self._upload_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._upload_badge.setVisible(self.uploaded)
        self._action_feedback = QLabel(self._thumb_frame)
        self._action_feedback.setObjectName('cardActionFeedback')
        self._action_feedback.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._action_feedback.hide()
        self._overlap_button = QPushButton(self._thumb_frame)
        self._overlap_button.setObjectName('cardOverlapButton')
        self._overlap_button.setFixedSize(32, 32)
        self._overlap_button.setIconSize(QSize(20, 20))
        self._overlap_button.setAccessibleName('Merge overlapping clips')
        self._overlap_button.setToolTip('Create one continuous clip from overlapping recordings. Originals are kept.')
        self._overlap_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._overlap_button.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self._overlap_button.clicked.connect(self._request_overlap_merge)
        self._overlap_button.hide()
        self._finalizing_badge = None
        layout.addWidget(self._thumb_frame)

        self._body_frame = QFrame(self)
        self._body_frame.setObjectName('cardBody')
        body = QHBoxLayout(self._body_frame)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(8)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.title_label = _RenameLabel(_clip_title_from_filename(self.file_path))
        self.title_label.setObjectName('cardTitle')
        self.title_label.set_full_text(_clip_title_from_filename(self.file_path))
        self.title_label.rename_requested.connect(self._begin_rename)
        self.title_edit = _RenameEdit()
        self.title_edit.setObjectName('cardRename')
        self.title_edit.setToolTip('Enter to save; Escape to cancel')
        self.title_edit.setAccessibleName('Clip filename')
        self.title_edit.editingFinished.connect(self._commit_rename)
        self.title_edit.cancelled.connect(self._cancel_rename)
        self._title_stack = QStackedWidget()
        self._title_stack.setMinimumWidth(0)
        self._title_stack.setFixedHeight(32)
        self._title_stack.addWidget(self.title_label)
        self._title_stack.addWidget(self.title_edit)
        info.addWidget(self._title_stack)
        self.metadata_label = QLabel('Reading clip details…' if self.is_video else 'Screenshot')
        self.metadata_label.setObjectName('cardMetadata')
        self.metadata_label.setMinimumWidth(0)
        self.metadata_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(self.metadata_label)
        body.addLayout(info, 1)
        self._link_actions = QWidget(self._body_frame)
        actions = QHBoxLayout(self._link_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.share_btn = _CardShareButton()
        self.share_btn.setObjectName('cardShareBtn')
        self.share_btn.setFixedSize(48, 52)
        self.share_btn.setIconSize(QSize(28, 28))
        self.share_btn.clicked.connect(self._on_share_click)
        self.share_btn.installEventFilter(self)
        actions.addWidget(self.share_btn)
        # The single visible action becomes Copy link after upload.
        self.copy_link_btn = self.share_btn
        self.open_link_btn = QPushButton(self)
        self.open_link_btn.hide()
        self.open_link_btn.clicked.connect(self._open_upload_link)
        body.addWidget(self._link_actions)
        self.favorite_btn = _CardFavoriteButton(self._thumb_frame)
        self.favorite_btn.setObjectName('cardFavoriteButton')
        self.favorite_btn.setFixedSize(34, 34)
        self.favorite_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.favorite_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.clicked.connect(self._toggle_favorite)
        self.delete_btn = QPushButton()
        self.delete_btn.setObjectName('cardDeleteBtn')
        self.delete_btn.setFixedSize(48, 52)
        self.delete_btn.setIconSize(QSize(28, 28))
        self.delete_btn.setToolTip('Move to recoverable Trash' if not self.imported else 'Linked original retained')
        self.delete_btn.setAccessibleName('Move clip to Trash')
        self.delete_btn.clicked.connect(lambda: self._confirm_delete(allow_linked=True))
        self.menu_btn = self.delete_btn  # legacy accessor
        body.addWidget(self.delete_btn)
        layout.addWidget(self._body_frame)
        self.refresh_theme()
        self.set_upload_info(self.upload_info)
        for button in (self.share_btn, self.delete_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

    @staticmethod
    def _card_qss():
        return f'''
            QFrame#clipCard {{ background: {Colors.CARD_BG}; border: none; border-radius: {Sizes.RADIUS_CARD}px; }}
            QFrame#cardThumb {{ background: {Colors.CARD_BG}; border: none; border-top-left-radius: {Sizes.RADIUS_CARD}px; border-top-right-radius: {Sizes.RADIUS_CARD}px; }}
            QFrame#cardBody, QStackedWidget {{ background: transparent; border: none; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#cardTitle {{ color: {Colors.TEXT}; font-family: {Fonts.BODY}; font-size: 15px; font-weight: 600; letter-spacing: 0px; }}
            QLabel#cardTitle:focus {{ color: {Colors.ACCENT}; }}
            QLabel#cardMetadata {{ color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY}; font-size: 12px; font-weight: 400; }}
            QLineEdit#cardRename {{ background: {Colors.SURFACE_1}; color: {Colors.TEXT}; border: 1px solid {Colors.ACCENT}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 17px; padding: 0 3px; }}
            QPushButton#cardShareBtn {{ background: {Colors.ACCENT}; color: {Colors.BG}; border: 1px solid {Colors.ACCENT}; border-radius: 4px; }}
            QPushButton#cardShareBtn:hover, QPushButton#cardShareBtn:pressed, QPushButton#cardShareBtn[actionHovered="true"] {{ color: {Colors.ACCENT}; border: 1px solid {Colors.ACCENT}; background: {Colors.ACCENT_SOFT}; }}
            QPushButton#cardFavoriteButton, QPushButton#cardFavoriteButton:checked {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 0; }}
            QPushButton#cardFavoriteButton:hover {{ background: rgba(255, 255, 255, 24); border-color: rgba(255, 255, 255, 28); }}
            QPushButton#cardFavoriteButton:pressed {{ background: rgba(255, 255, 255, 42); border-color: rgba(255, 255, 255, 35); }}
            QPushButton#cardFavoriteButton:focus {{ border: 2px solid {Colors.ACCENT}; }}
            QPushButton#cardFavoriteButton:disabled {{ background: transparent; border-color: transparent; }}
            QPushButton#cardDeleteBtn {{ background: {Colors.DELETE}; color: {Colors.TEXT}; border: 1px solid {Colors.DELETE}; border-radius: 4px; }}
            QPushButton#cardDeleteBtn:hover {{ border-color: {Colors.ERROR}; background: {Colors.ERROR_SOFT}; }}
            QPushButton#cardDeleteBtn:disabled {{ background: {Colors.SURFACE_3}; border-color: {Colors.BORDER}; color: {Colors.TEXT_MUTED}; }}
            QPushButton:focus {{ border: 2px solid {Colors.TEXT}; }}
            QPushButton:disabled {{ background: {Colors.SURFACE_3}; border-color: {Colors.BORDER}; }}
            QPushButton#cardOverlapButton {{ color: {Colors.BG}; background: {Colors.ACCENT}; border: 1px solid {Colors.ACCENT}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 12px; font-weight: 600; padding: 0; }}
            QPushButton#cardOverlapButton:hover {{ background: {Colors.ACCENT}; color: {Colors.BG}; }}
            QPushButton#cardOverlapButton:focus {{ border: 2px solid {Colors.TEXT}; }}
            QLabel#cardDurationBadge {{ color: {Colors.TEXT}; background: {_rgba(Colors.SURFACE_1, 230)}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 14px; padding: 2px 9px; }}
            QLabel#cardImportedBadge, QLabel#cardUploadedBadge {{ color: {Colors.BG}; background: {Colors.ACCENT}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.DISPLAY}; font-size: 12px; padding: 2px 7px; letter-spacing: 0px; }}
            QLabel#cardFinalizingBadge {{ color: {Colors.BG}; background: {Colors.WARNING}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 12px; padding: 3px 7px; }}
            QLabel#cardActionFeedback {{ color: {Colors.TEXT}; background: {Colors.SURFACE_3}; border: 1px solid {Colors.ACCENT_DIM}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 14px; padding: 4px 8px; }}
        '''

    def refresh_theme(self):
        set_theme_style(self, lambda: (self._card_qss()))
        self._overlap_button.setIcon(_card_icon('link', Colors.BG, 20))
        self.delete_btn.setIcon(_card_icon('delete', Colors.TEXT))
        self._share_icon_key = None
        self._update_share_action()
        if hasattr(self, '_play_icon'):
            self._play_icon.setPixmap(_card_icon('play', Colors.TEXT, 36).pixmap(36, 36))
        self.resize_card(self._card_width, force=True)

    def rebind(self, file_path, *, imported, upload_enabled, uploaded,
               upload_info, ready, card_width):
        """Reuse a card of the same media type without rebuilding its widget tree."""
        self._feedback_timer.stop()
        self._mutation_timer.stop()
        self._mutation_token = None
        self._library_generation = object()
        self._library_flags = None
        self._delete_identity = None
        self._mutation_busy = False
        self._mutation_guard_owned = False
        self._upload_status = ''
        self._action_feedback.hide()
        self.file_path = file_path
        self.setProperty('selected', False)
        self._selection_outline.hide()
        self.set_overlap_paths(())
        self._source_duration = 0.0
        self.imported = imported
        self.upload_enabled = upload_enabled
        self.uploaded = uploaded
        self._thumbnail_ready = False
        self._thumbnail_fingerprint = ''
        self._thumb_pixmap = QPixmap()
        self.clear_custom_cover()
        self.thumb_label.clear()
        self._title_stack.setCurrentIndex(0)
        self.title_label.set_full_text(_clip_title_from_filename(file_path))
        self.title_edit.clear()
        self.metadata_label.setText('Reading clip details…' if self.is_video else 'Screenshot')
        self.metadata_label.setToolTip('')
        self._imported_badge.setVisible(imported)
        self._upload_badge.setVisible(uploaded)
        if self.is_video:
            self.duration_label.setText('0:00')
            self._play_icon.hide()
        self.delete_btn.setToolTip('Move to recoverable Trash' if not imported else 'Linked original retained')
        self.set_upload_info(upload_info)
        self.set_ready(ready)
        self.resize_card(card_width, force=True)
        self.refresh_library_flags()

    def _update_share_action(self):
        has_link = bool(self.upload_link)
        self.share_btn.action_name = 'link' if has_link else 'upload'
        icon_key = ('link' if has_link else 'upload',
                    Colors.ACCENT if self.share_btn.property('actionHovered') else Colors.BG)
        if getattr(self, '_share_icon_key', None) != icon_key:
            self.share_btn.setIcon(_card_icon(*icon_key))
            self._share_icon_key = icon_key
        sftp = self.upload_link.startswith('sftp://')
        label = 'Copy SFTP location' if sftp else 'Copy link' if has_link else 'Upload clip'
        self.share_btn.setAccessibleName(label)
        self.share_btn.setToolTip(
            'Copy SFTP location (requires an SFTP client and login)' if sftp else
            label if has_link or self.upload_enabled else
            'Enable the uploader in Settings to upload this clip')
        self.share_btn.setEnabled(self.ready and not self._mutation_busy and not self._batch_locked and not self._upload_status and (has_link or (self.is_video and self.upload_enabled)))

    def eventFilter(self, obj, event):
        if obj is self.share_btn and event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            hovered = event.type() == QEvent.Type.Enter
            self.share_btn.setProperty('actionHovered', hovered)
            self.share_btn.update()
            tint = Colors.ACCENT if hovered else Colors.BG
            self._share_icon_key = ('link' if self.upload_link else 'upload', tint)
            self.share_btn.setIcon(_card_icon(*self._share_icon_key))
        return super().eventFilter(obj, event)

    def _show_action_feedback(self, message, *, duration_ms=2000):
        self._feedback_timer.stop()
        self._action_feedback.setText(message)
        self._action_feedback.adjustSize()
        self._action_feedback.move(40 if not self.selection_checkbox.isHidden() else 8,
                                   48 if self._overlap_paths else 8)
        self._action_feedback.setVisible(bool(message))
        self._action_feedback.raise_()
        self.favorite_btn.raise_()
        if message and duration_ms:
            self._feedback_timer.start(duration_ms)

    def _clear_action_feedback(self):
        self._show_action_feedback(self._upload_status, duration_ms=0)

    def set_upload_status(self, message, *, feedback=''):
        self._upload_status = message
        self._show_action_feedback(message or feedback, duration_ms=0 if message else 2500)
        self._update_share_action()

    def set_uploaded(self, val):
        self.uploaded = val
        self._upload_badge.setVisible(val)
        self._update_share_action()

    def set_overlap_paths(self, paths):
        self._overlap_paths = tuple(paths) if self.is_video else ()
        self._overlap_button.setVisible(bool(self._overlap_paths) and self.ready)
        self._overlap_button.setEnabled(self.ready and not self._mutation_busy and not self._batch_locked)
        self._overlap_button.move(40 if not self.selection_checkbox.isHidden() else 8, 8)
        self._overlap_button.raise_()

    def _request_overlap_merge(self):
        if self._overlap_paths and self.ready and not self._mutation_busy and not self._batch_locked:
            self.overlap_requested.emit(self.file_path)

    def set_ready(self, ready):
        self.ready = bool(ready)
        self.set_overlap_paths(self._overlap_paths)
        if self.ready and self._finalizing_badge is not None:
            self._finalizing_badge.deleteLater()
            self._finalizing_badge = None
        elif not self.ready and self._finalizing_badge is None:
            self._finalizing_badge = QLabel('Finalizing', self._thumb_frame)
            self._finalizing_badge.setObjectName('cardFinalizingBadge')
            self._finalizing_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._finalizing_badge.adjustSize()
            self._finalizing_badge.move(
                40 if hasattr(self, 'selection_checkbox') and not self.selection_checkbox.isHidden() else 8, 8)
            self._finalizing_badge.show()
        self.delete_btn.setEnabled(self.ready and not self._mutation_busy
                                   and self._library_flags is not None
                                   and not self._library_flags['protected'])
        self.favorite_btn.setEnabled(self.ready and not self._mutation_busy and self._library_flags is not None)
        self.title_label.setEnabled(self.ready and not self._mutation_busy)
        self._update_share_action()
        if self._batch_locked:
            for widget in (self.favorite_btn, self.delete_btn, self.title_label):
                widget.setEnabled(False)

    @staticmethod
    def _upload_url(info):
        if isinstance(info, dict):
            for key in ('url', 'raw_url'):
                value = str(info.get(key, '') or '').strip()
                if value.startswith(('https://', 'http://')):
                    return value
                if value.startswith('sftp://'):
                    url = QUrl(value)
                    if (url.isValid() and url.host() and not url.userInfo()
                            and url.path().startswith('/') and len(url.path()) > 1
                            and (url.port() == -1 or 1 <= url.port() <= 65535)):
                        return value
        return ''

    def set_upload_info(self, info):
        self.upload_info = dict(info or {})
        self.upload_link = self._upload_url(self.upload_info)
        self.open_link_btn.setEnabled(bool(self.upload_link))
        self._link_actions.setVisible(self.is_video or bool(self.upload_link))
        self._update_share_action()

    def _copy_upload_link(self):
        if self.upload_link:
            QApplication.clipboard().setText(self.upload_link)
            self._show_action_feedback(
                'SFTP location copied' if self.upload_link.startswith('sftp://') else 'Link copied')

    def _open_upload_link(self):
        if self.upload_link:
            QDesktopServices.openUrl(QUrl(self.upload_link))

    @staticmethod
    def card_height(width, designer=None):
        natural = round(width * 9 / 16) + _CARD_BODY_H
        height = designer.card_dimensions()[1] if designer is not None else None
        return natural if height is None else max(_CARD_BODY_H + 48, min(720, height))

    def resize_card(self, width, force=False):
        width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(width)))
        if width == self._card_width and not force:
            return
        designer = self._interface_card_designer
        if designer is not None:
            designer.restore_card(self)
        self._card_width = width
        total_height = self.card_height(width, designer)
        self._thumb_height = total_height - _CARD_BODY_H
        self.setFixedSize(width, total_height)
        self._thumb_frame.setFixedSize(width, self._thumb_height)
        self.thumb_label.setGeometry(0, 0, width, self._thumb_height)
        self._body_frame.setFixedSize(width, _CARD_BODY_H)
        if hasattr(self, '_play_icon'):
            self._play_icon.setGeometry(0, 0, width, self._thumb_height)
            self.duration_label.adjustSize()
            self.duration_label.move(width - self.duration_label.width() - 10, self._thumb_height - self.duration_label.height() - 8)
        badge_y = self._thumb_height - 8
        if self.imported:
            self._imported_badge.adjustSize()
            badge_y -= self._imported_badge.height()
            self._imported_badge.move(8, badge_y)
            badge_y -= 4
        self._upload_badge.adjustSize()
        self._upload_badge.move(8, badge_y - self._upload_badge.height())
        if self._finalizing_badge is not None:
            self._finalizing_badge.adjustSize()
            self._finalizing_badge.move(40 if hasattr(self, 'selection_checkbox') and not self.selection_checkbox.isHidden() else 8, 8)
        self.favorite_btn.move(width - self.favorite_btn.width() - 8, 8)
        if hasattr(self, 'selection_checkbox'):
            self.selection_checkbox.move(8, 8)
            self.selection_checkbox.raise_()
        self.favorite_btn.raise_()
        self._render_thumbnail()
        if hasattr(self, '_selection_outline'):
            self._selection_outline.setGeometry(self.rect())
            self._selection_outline.raise_()
            self._selection_outline.update()
        if designer is not None:
            designer.apply_card(self)

    def _render_thumbnail(self):
        source_pixmap = (self._cover_pixmap
                         if not self._cover_pixmap.isNull()
                         else self._thumb_pixmap)
        if source_pixmap.isNull():
            self.thumb_label.clear()
            return
        # Compose once when the image, card size or theme changes. No effect
        # widgets or animation timers run while the library is idle.
        width, height = self._card_width, self._thumb_height
        ratio = self.devicePixelRatioF()
        designer = self._interface_card_designer
        if designer is not None:
            width, height = self.thumb_label.width(), self.thumb_label.height()
        background, radius = Colors.CARD_BG, Sizes.RADIUS_CARD
        if designer is not None:
            background, radius = designer.thumbnail_skin(background, radius)
        radius = min(radius, width / 2, height / 2)
        # Qt's shared LRU has a fixed memory budget. Preserve composed images
        # across card recycling without retaining a second unbounded library.
        # Source identity, geometry, DPR and theme all invalidate the key.
        cache_key = f'fthr/card/{source_pixmap.cacheKey()}/{width}/{height}/{ratio}/{radius}/{background}'
        cached = QPixmapCache.find(cache_key)
        if cached is not None:
            self.thumb_label.setPixmap(cached)
            return
        source = source_pixmap.scaled(
            round(width * ratio), round(height * ratio),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        rendered = QPixmap(round(width * ratio), round(height * ratio))
        rendered.setDevicePixelRatio(ratio)
        rendered.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rendered)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded = QPainterPath()
        rounded.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        rounded.addRect(QRectF(0, radius, width, height - radius))
        rounded.setFillRule(Qt.FillRule.WindingFill)
        painter.setClipPath(rounded)
        crop = QRectF((source.width() - rendered.width()) / 2,
                      (source.height() - rendered.height()) / 2,
                      rendered.width(), rendered.height())
        painter.drawPixmap(QRectF(0, 0, width, height), source, crop)
        fade = QLinearGradient(0, height * .64, 0, height)
        if background is None:
            # Expose an edited image/gradient beneath the thumbnail fade.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            fade.setColorAt(0, QColor(0, 0, 0, 255))
            fade.setColorAt(1, QColor(0, 0, 0, 0))
        else:
            transparent = QColor(background)
            transparent.setAlpha(0)
            fade.setColorAt(0, transparent)
            fade.setColorAt(1, QColor(background))
        painter.fillRect(QRectF(0, height * .64, width, height * .36), fade)
        painter.end()
        QPixmapCache.insert(cache_key, rendered)
        self.thumb_label.setPixmap(rendered)

    def release_thumbnail_image(self):
        """Drop only this card's images; keep indexed metadata and controls."""
        if not self._thumb_pixmap.isNull():
            cache_key = f'fthr/card/{self._thumb_pixmap.cacheKey()}/{self._card_width}/{self._thumb_height}/{self.devicePixelRatioF()}/{Sizes.RADIUS_CARD}/{Colors.CARD_BG}'
            QPixmapCache.remove(cache_key)
        self._thumb_pixmap = QPixmap()
        self.clear_custom_cover()
        self.thumb_label.clear()
        self._thumbnail_ready = False

    def set_custom_cover(self, pixmap, cache_key=''):
        """Set a grid-only cover pixmap without changing media metadata."""
        self._cover_pixmap = pixmap if isinstance(pixmap, QPixmap) else QPixmap()
        self._cover_cache_key = str(cache_key or '')
        self._render_thumbnail()

    def clear_custom_cover(self):
        self._cover_pixmap = QPixmap()
        self._cover_cache_key = ''
        if self._thumb_pixmap.isNull():
            self.thumb_label.clear()

    def _on_share_click(self):
        if not self.ready or self._mutation_busy or self._batch_locked:
            return
        if self.upload_link:
            self._copy_upload_link()
        elif self.is_video and self.upload_enabled:
            self._show_action_feedback('Upload requested')
            self.upload_requested.emit(self.file_path)

    def set_selection_state(self, selected, visible, *, locked=False):
        self._batch_locked = bool(locked)
        self.selection_checkbox.blockSignals(True)
        self.selection_checkbox.setChecked(bool(selected))
        self.selection_checkbox.blockSignals(False)
        self.selection_checkbox.setVisible(bool(visible))
        self._action_feedback.move(40 if visible else 8, 48 if self._overlap_paths else 8)
        if self._finalizing_badge is not None:
            self._finalizing_badge.move(40 if visible else 8, 8)
        self.selection_checkbox.setEnabled(not locked)
        self.selection_checkbox.raise_()
        changed = self.property('selected') != bool(selected)
        self.setProperty('selected', bool(selected))
        self._selection_outline.setGeometry(self.rect())
        self._selection_outline.setVisible(bool(selected))
        self._selection_outline.raise_()
        self._selection_outline.update()
        if changed:
            self.style().unpolish(self)
            self.style().polish(self)
        self.set_ready(self.ready)
        if locked:
            for widget in (self.favorite_btn, self.delete_btn, self.share_btn, self.title_label):
                widget.setEnabled(False)

    def _selection_checkbox_clicked(self):
        if self._selection_handler is not None:
            self._selection_handler(self.file_path, Qt.KeyboardModifier.ControlModifier, True)

    def _on_thumbnail_click(self, event):
        self._thumbnail_press = None
        if event.button() == Qt.MouseButton.LeftButton:
            record = (self._album_record_provider(self.file_path)
                      if callable(self._album_record_provider) else None)
            self._thumbnail_press = (event.position().toPoint(), self.file_path,
                                     self._library_generation, record, event.modifiers())
            event.accept()

    def _on_thumbnail_move(self, event):
        press = self._thumbnail_press
        if press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        if (event.position().toPoint() - press[0]).manhattanLength() < QApplication.startDragDistance():
            return
        # Consume before entering Qt's nested drag event loop, even if cancelled.
        self._thumbnail_press = None
        event.accept()
        if (self.file_path == press[1] and self._library_generation is press[2]
                and callable(self._album_drag_handler)):
            self._album_drag_handler(press[1], press[3])

    def _on_thumbnail_release(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        press, self._thumbnail_press = self._thumbnail_press, None
        event.accept()
        if (press is None or self.file_path != press[1]
                or self._library_generation is not press[2]
                or (callable(self._album_record_provider)
                    and self._album_record_provider(self.file_path) != press[3])
                or not self._thumb_frame.rect().contains(event.position().toPoint())):
            return
        if self._selection_handler is not None:
            if self._selection_handler(self.file_path, press[4], False):
                event.accept()
                return
        if self.ready and not self._mutation_busy and not self._batch_locked:
            self.clicked.emit(self.file_path)
            self._emit_opened()
            event.accept()

    def _emit_opened(self):
        if self.is_video and self.ready and not self._mutation_busy:
            self.opened.emit(self.file_path, self._thumb_pixmap,
                             QRect(self.mapToGlobal(QPoint(0, 0)), self.size()))

    def _begin_rename(self):
        if not self.ready or self._mutation_busy or self._batch_locked:
            return
        self._editing_name = True
        self.title_edit.setText(Path(self.file_path).stem)
        self._title_stack.setCurrentIndex(1)
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _cancel_rename(self):
        self._editing_name = False
        self._title_stack.setCurrentIndex(0)

    def _commit_rename(self):
        if not self._editing_name:
            return
        path, generation = self.file_path, self._library_generation
        name = self.title_edit.text()
        self._cancel_rename()
        try:
            destination = renamed_clip_path(self.file_path, name)
        except (OSError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Rename failed', str(error))
            return
        if destination == self.file_path:
            return
        if self.imported and not FthrMessageDialog.question(
                self, 'Rename linked original', f'Rename the original file in its source folder?\n\n{self.file_path}'):
            return
        if self.file_path == path and self._library_generation is generation:
            self._begin_mutation('rename', destination)

    def refresh_library_flags(self):
        reader = getattr(self._metadata_manager, 'get_library_flags', None)
        self._library_flags = None
        try:
            if callable(reader):
                self._library_flags = reader(self.file_path)
        except (OSError, ValueError, TypeError):
            pass  # Status remains visibly unavailable; deletion stays disabled.
        flags = self._library_flags
        self.favorite_btn.setEnabled(flags is not None and self.ready and not self._mutation_busy)
        self.favorite_btn.setChecked(bool(flags and flags['favorite']))
        self.favorite_btn.setAccessibleName('Remove favorite' if flags and flags['favorite'] else 'Add favorite')
        self.favorite_btn.setToolTip(
            'Favorite status unavailable' if flags is None else
            'Remove favorite to allow deletion' if flags['favorite'] else
            'Add favorite and protect from deletion')
        self.delete_btn.setEnabled(self.ready and not self._mutation_busy
                                   and flags is not None and not flags['protected'])
        self.delete_btn.setToolTip('Protection unavailable; deletion blocked' if flags is None else
                                   'Remove favorite to move this clip to Trash' if flags['protected'] else
                                   'Linked original retained' if self.imported else 'Move to recoverable Trash')

    def _toggle_favorite(self, *, expected_path=None, expected_generation=None):
        if self._batch_locked:
            return
        path, generation = self.file_path, self._library_generation
        if (not self.ready or self._mutation_busy or
                (expected_path is not None and expected_path != path) or
                (expected_generation is not None and expected_generation is not generation)):
            return
        try:
            reader = getattr(self._metadata_manager, 'get_library_flags', None)
            setter = getattr(self._metadata_manager, 'set_library_flags', None)
            if not callable(reader) or not callable(setter):
                raise OSError('Library protection is unavailable')
            flags = reader(path)
            value = not flags['favorite']
            if self.file_path != path or self._library_generation is not generation or self._mutation_busy:
                return
            setter(path, favorite=value)
            self.refresh_library_flags()
            self.library_flags_changed.emit(path)  # May immediately recycle this card.
        except (OSError, ValueError, TypeError) as error:
            self.refresh_library_flags()
            FthrMessageDialog.warning(self, 'Library flags unchanged', str(error))

    def _check_delete_protection(self):
        guard = getattr(self._metadata_manager, 'delete_guard', None)
        if not callable(guard):
            raise OSError('Library protection is unavailable; deletion is blocked')
        with guard(self.file_path, expected_identity=self._delete_identity):
            pass  # Confirmation/admission check; unlink takes its own fresh guard.

    def _confirm_delete(self, *, allow_linked=False):
        if self._batch_locked or not self.ready or self._mutation_busy:
            return
        path, generation = self.file_path, self._library_generation
        if self.imported or classify_media_path(path, self._clips_root, []) is not MediaOwnership.FTHR_OWNED:
            FthrMessageDialog.information(self, 'Original retained',
                                         'Linked and imported originals are never moved to this library Trash.',
                                         plain_text=True)
            return
        try:
            self._delete_identity = media_file_identity(path)
            self._check_delete_protection()
        except (OSError, ValueError, TypeError) as error:
            FthrMessageDialog.warning(self, 'Move to Trash blocked', str(error), plain_text=True)
            return
        if FthrMessageDialog.question(self, 'Move to Trash',
                                     f'Move {Path(path).name} to Trash? You can restore it later.', plain_text=True):
            if self.file_path == path and self._library_generation is generation:
                self._begin_mutation('delete')

    def _begin_mutation(self, kind, destination=None):
        if self._batch_locked:
            return
        if self._mutation_busy:
            return
        asynchronous = kind == 'delete' or kind == 'rename' and self._rename_handler is not None
        if kind == 'delete':
            try:
                if self._delete_identity is None:
                    self._delete_identity = media_file_identity(self.file_path)
                self._check_delete_protection()
            except (OSError, ValueError, TypeError) as error:
                FthrMessageDialog.warning(self, 'Delete blocked', str(error))
                return
        reason = (self._prepare_mutation(self.file_path)
                  if self._prepare_mutation and not asynchronous else '')
        if reason:
            FthrMessageDialog.warning(self, 'Clip is busy', reason)
            return
        self._mutation_busy = True
        self._mutation_guard_owned = not asynchronous
        self._mutation_token = object()
        self._mutation_kind, self._mutation_destination = kind, destination
        self._mutation_attempts = 0
        self._mutation_old_path = self.file_path
        try:
            cache = _get_cached_thumb_path(self.file_path)
            self._mutation_caches = (cache, _get_cached_duration_path(cache), _get_negative_cache_path(cache))
        except OSError:
            self._mutation_caches = ()
        self.set_ready(self.ready)
        self._try_mutation()

    def _try_mutation(self):
        if self._mutation_kind == 'delete':
            reference, token, old_path = weakref.ref(self), self._mutation_token, self._mutation_old_path
            handler = getattr(self, '_trash_handler', None)
            if not callable(handler):
                self._mutation_failed('The library Trash service is unavailable. The file was kept.')
                return
            finished = False

            def completed(result):
                nonlocal finished
                if finished:
                    return
                finished = True
                card = reference()
                if card is None or not isValid(card) or card._mutation_token is not token or card.file_path != old_path:
                    return
                card._release_mutation()
                if not result.succeeded:
                    FthrMessageDialog.warning(card, 'Move to Trash incomplete',
                                             result.error or 'The original was retained for recovery.', plain_text=True)
            try:
                handler(old_path, self._delete_identity, completed)
            except Exception as error:
                # Completion can happen synchronously before a handler raises.
                completed(MutationResult('', FileOperationState.ROLLED_BACK, old_path, old_path, str(error)))
            return
        if self._mutation_kind == 'delete':
            try:
                self._check_delete_protection()
            except (OSError, ValueError, TypeError) as error:
                self._mutation_failed(str(error))
                return
        if self._mutation_kind == 'rename' and self._rename_handler is not None:
            reference, token = weakref.ref(self), self._mutation_token
            old_path = self._mutation_old_path
            finished = False

            def completed(result):
                nonlocal finished
                if finished:
                    return
                finished = True
                card = reference()
                if (card is None or not isValid(card) or card._mutation_token is not token
                        or card.file_path != old_path):
                    return
                if result.succeeded:
                    card.file_path = result.new_path
                    card.title_label.set_full_text(Path(result.new_path).stem)
                    for cache in card._mutation_caches:
                        try:
                            os.remove(cache)
                        except OSError:
                            pass  # Optional thumbnail caches may already be absent.
                card._release_mutation()
                if not result.succeeded:
                    FthrMessageDialog.warning(card, 'File change failed', result.error or 'Rename did not finish.')

            try:
                self._rename_handler(old_path, self._mutation_destination,
                                     bool(self.imported), completed)
            except Exception as error:
                # A synchronous rejection may already have called completed.
                completed(MutationResult('', FileOperationState.ROLLED_BACK, old_path,
                                         self._mutation_destination, str(error)))
            return
        self._mutation_attempts += 1
        if self._wait_for_readers and not self._wait_for_readers(self.file_path):
            if self._mutation_attempts < 30:
                self._mutation_timer.start(100)
                return
            self._mutation_failed('The thumbnail reader is still closing. Try again in a moment.')
            return
        try:
            old_path = self.file_path
            if self._mutation_kind == 'rename':
                rename_clip(old_path, self._mutation_destination)
                self.file_path = self._mutation_destination
            else:
                raise OSError('Trash service unavailable; permanent deletion is disabled')
        except OSError as error:
            if getattr(error, 'winerror', 0) in (32, 33) and self._mutation_attempts < 30:
                self._mutation_timer.start(100)
                return
            self._mutation_failed(str(error))
            return
        except (ValueError, TypeError) as error:
            self._mutation_failed(str(error))
            return
        for cache in self._mutation_caches:
            try:
                os.remove(cache)
            except OSError:
                pass
        self._release_mutation()
        if self._mutation_kind == 'rename':
            self.title_label.set_full_text(Path(self.file_path).stem)
            self.renamed.emit(old_path, self.file_path)
        else:
            self.deleted.emit(old_path)

    def _release_mutation(self):
        self._mutation_busy = False
        if self._finish_mutation and self._mutation_guard_owned:
            self._mutation_guard_owned = False
            self._finish_mutation(self._mutation_old_path)
        self.set_ready(self.ready)
        self.refresh_library_flags()

    def _mutation_failed(self, reason):
        self._release_mutation()
        FthrMessageDialog.warning(self, 'File change failed', reason)

    def contextMenuEvent(self, event):
        if not self.ready or self._mutation_busy or self._batch_locked:
            return
        path, generation = self.file_path, self._library_generation
        album_record = (self._album_record_provider(path)
                        if callable(self._album_record_provider) else None)
        album_state = (self._album_menu_provider(path)
                       if callable(self._album_menu_provider) else {})
        tag_details = (self._tag_details_provider(path)
                       if callable(self._tag_details_provider) else None)
        menu = QMenu(self)
        set_theme_style(menu, lambda: (context_menu_qss()))
        open_action = menu.addAction('Open in editor')
        merge_action = (menu.addAction('Merge overlapping clips…')
                        if self._overlap_paths and self.is_video else None)
        rename_action = menu.addAction('Rename')
        explorer_action = menu.addAction('Show in file manager')
        copy_action = menu.addAction('Copy path')
        edit_tags_action = menu.addAction('Edit tags')
        edit_tags_action.setEnabled(
            album_record is not None and album_record.file_identity is not None)
        cover_action = menu.addAction('Cover...')
        cover_action.setEnabled(
            callable(self._cover_handler)
            and album_record is not None
            and album_record.file_identity is not None)
        link_action = menu.addAction(
            'Open SFTP location' if self.upload_link.startswith('sftp://')
            else 'Open uploaded link') if self.upload_link else None
        clip_actions = {}
        if self.is_video and self._clip_actions_provider is not None:
            for label, callback in self._clip_actions_provider(path):
                clip_actions[menu.addAction(label)] = callback
        favorite_action = menu.addAction('Remove favorite' if self._library_flags and self._library_flags['favorite'] else 'Add favorite')
        favorite_action.setEnabled(self._library_flags is not None)
        add_album_menu = menu.addMenu('Add to album')
        for album_id, album_name in album_state.get('albums', ()):
            add_album_menu.addAction(str(album_name)).setData(str(album_id))
        add_album_menu.setEnabled(bool(album_state.get('albums')) and album_record is not None)
        remove_album_action = menu.addAction('Remove from current album')
        remove_album_action.setEnabled(
            bool(album_state.get('current_id')) and album_record is not None)
        delete_action = menu.addAction('Move to Trash')
        try:
            action = menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
        if (self.file_path != path or self._library_generation is not generation
                or self._mutation_busy or self._batch_locked):
            return
        if action == favorite_action:
            self._toggle_favorite(expected_path=path, expected_generation=generation)
            return
        if action == open_action:
            self._emit_opened()
        elif merge_action is not None and action == merge_action:
            self._request_overlap_merge()
        elif action == rename_action:
            self._begin_rename()
        elif action == explorer_action:
            _show_in_file_manager(self.file_path)
        elif action == copy_action:
            QApplication.clipboard().setText(self.file_path)
        elif action == edit_tags_action and callable(self._tag_edit_handler):
            self._tag_edit_handler(
                path, album_record, generation, tag_details, self)
        elif action == cover_action and callable(self._cover_handler):
            self._cover_handler(path, album_record, generation, self)
        elif link_action and action == link_action:
            self._open_upload_link()
        elif action in clip_actions:
            clip_actions[action]()
        elif (action is not None and action.parent() is add_album_menu
              and album_record is not None):
            album_id = action.data()
            if (callable(self._album_membership_handler)
                    and self._album_record_provider(path) == album_record):
                self._album_membership_handler(
                    'add', str(album_id), path, album_record, generation)
        elif action == remove_album_action and album_record is not None:
            album_id = album_state.get('current_id')
            if (album_id and callable(self._album_membership_handler)
                    and self._album_record_provider(path) == album_record):
                self._album_membership_handler(
                    'remove', str(album_id), path, album_record, generation)
        elif action == delete_action:
            self._confirm_delete(allow_linked=True)

    def enterEvent(self, event):
        if hasattr(self, '_play_icon'):
            self._play_icon.setVisible(self.ready and not self._mutation_busy)
        if self.is_video and self.ready and not self._mutation_busy:
            self.preload_requested.emit(self.file_path)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if hasattr(self, '_play_icon'):
            self._play_icon.hide()
        super().leaveEvent(event)

    def _load_image_thumbnail(self):
        reader = QImageReader(self.file_path)
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.isValid():
            reader.setScaledSize(source_size.scaled(640, 360, Qt.AspectRatioMode.KeepAspectRatio))
        self.set_image_thumbnail(reader.read())

    def set_image_thumbnail(self, image):
        if image is not None and not image.isNull():
            self._thumb_pixmap = QPixmap.fromImage(image)
            self._thumbnail_ready = True
            self.metadata_label.setText('Screenshot')
            self._render_thumbnail()

    def set_video_thumbnail(self, cache_path, duration):
        pixmap = QPixmap(cache_path)
        if not pixmap.isNull():
            self._thumb_pixmap = pixmap
            self._thumbnail_ready = True
            self._render_thumbnail()
        metadata = read_cached_metadata(_get_cached_duration_path(cache_path))
        self.set_source_duration(metadata[0] if metadata else duration)
        if metadata:
            _, width, height, fps, bitrate, _total = metadata
            details = [f'{height}p']
            if fps:
                details.append(f'{fps:g} FPS')
            if bitrate:
                details.append(f'{bitrate / 1_000_000:g} Mbps')
            self.metadata_label.setText(' · '.join(details))
            self.metadata_label.setToolTip(f'{width} × {height}  /  ' + ' / '.join(details[1:]))
        else:
            self.metadata_label.setText('Video clip')
        self.resize_card(self._card_width, force=True)


    def set_source_duration(self, duration):
        self._source_duration = duration
        self.refresh_duration_label()

    def refresh_duration_label(self):
        if not self.is_video:
            return
        duration = (self._duration_provider(self.file_path, self._source_duration)
                    if self._duration_provider else self._source_duration)
        seconds = int(duration + 1e-6)
        self.duration_label.setText(f'{seconds // 60}:{seconds % 60:02d}' if duration > 0 else '--:--')
        if self._interface_card_designer is not None:
            self.resize_card(self._card_width, force=True)
            return
        self.duration_label.adjustSize()
        self.duration_label.move(self._card_width - self.duration_label.width() - 10,
                                 self._thumb_height - self.duration_label.height() - 8)



class ClipGrid(QWidget):
    add_import_folder_requested = Signal()
    manage_import_folders_requested = Signal()
    import_feedback = Signal(str, str, bool)
    overlap_merge_requested = Signal(object)
    clip_clicked          = Signal(str)
    clip_preload_requested = Signal(str)
    clip_opened           = Signal(str, QPixmap, QRect)
    screenshot_clicked    = Signal(str)
    clip_upload_requested = Signal(str)
    clip_renamed = Signal(str, str)

    # Compact gutters keep the media surface visually dominant while still
    # separating cards at desktop widths.
    _H_MARGIN = 48
    _GRID_SPACING = 12

    library_flags_changed = Signal(str)
    bulk_export_requested = Signal(object)
    bulk_flags_finished = Signal(object)
    bulk_trash_finished = Signal(object)
    bulk_albums_finished = Signal(object)
    bulk_tags_finished = Signal(object)
    library_details_changed = Signal(str)
    album_catalog_changed = Signal(object)
    album_view_changed = Signal(object)
    album_drag_finished = Signal()
    trash_view_requested = Signal()

    def __init__(self, settings_manager=None, parent=None, *, metadata_manager=None,
                 autostart=True, clip_actions_provider=None, bulk_clip_actions_provider=None,
                 toolbar_layout=None):
        super().__init__(parent)
        self._clip_actions_provider = clip_actions_provider
        self._bulk_clip_actions_provider = bulk_clip_actions_provider
        self._selection = LibrarySelection()
        self._album_drag_lease = None
        self._album_drag_revision = 0
        self._album_drag_ordered = ()
        self._selection_mode = False
        self._bulk_export_enabled = False
        self._bulk_job = None
        self._album_id = None
        self._search_criteria = SearchCriteria()
        self._search_tags: tuple[str, ...] = ()
        self._favorites_only = False
        self._game_filter = None
        self._saved_search_id: str | None = None
        self._saved_search_update_target: str | None = None
        self._saved_searches: tuple = ()
        self._search_unavailable_reason: str | None = None
        self._search_syncing = False
        self._search_text_pending = False
        self._query_apply_cache_only = False
        self._search_query_cache: dict[str, dict] = {}
        self._query_snapshot_dirty = True
        self._favorite_snapshot: dict[str, dict[str, int]] = {}
        self._favorite_snapshot_dirty = True
        self._album_snapshot_dirty = True
        self._album_bindings: dict[str, dict[str, int]] = {}
        self._albums: tuple = ()
        self._published_album_catalog = None
        self._published_album_view = None
        self._bulk_timer = QTimer(self)
        self._bulk_timer.setSingleShot(True)
        self._bulk_timer.timeout.connect(self._process_bulk_flags)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._sm              = settings_manager
        self._metadata_manager = metadata_manager
        self._upload_checker  = None   # callable(path) -> bool
        self._upload_info_checker = None  # callable(path) -> dict | None
        self._upload_enabled  = None   # callable() -> bool
        self._readiness_checker = None  # callable(path) -> bool
        self._scanning_started = False
        self._scroll_bar = None
        self._viewport_widget = None
        self._rename_handler = None
        self._trash_handler = None
        self._mutation_prepare = None
        self._mutation_finish = None
        self._mutation_paths = set()
        self._mutation_refresh_pending = False
        self._trash_rebuild_pending = False
        self._album_rebuild_pending = False
        self._details_rebuild_pending = False
        self.clips_dir      = str(clips_directory_from(self._sm))
        self.thumbnails     = []
        from ui.clip_overlap import OverlapDiscovery
        self._overlaps = {}
        self._overlap_discovery = OverlapDiscovery(self)
        self._overlap_discovery.changed.connect(self._on_overlaps_changed)
        self._thumb_widgets = {}
        self._interface_card_designer = None
        self._recycled_cards: list[ClipThumbnail] = []
        # Retain decoded images and labels, not widgets or running workers.
        # Returning to a recent row avoids JPEG reads, metadata reads, and jobs.
        self._recent_thumbnails = ImageLRU(8 * 1024 * 1024, max_entries=96)
        # Cover payloads are a separate, small visual cache.  They never
        # participate in normal thumbnail fingerprints or duration metadata.
        self._cover_bindings_snapshot: dict[str, dict] = {}
        self._cover_pixmap_cache = OrderedDict()
        self._cover_cache_limit = 24
        self._cover_generation = 0
        self._cover_decode_jobs = {}
        # Compatibility alias for existing diagnostics; job records remain
        # until their terminal callback so late results cannot evict a new job.
        self._cover_decode_inflight = self._cover_decode_jobs
        self._cover_decode_pending = OrderedDict()
        self._cover_decode_demands = OrderedDict()
        self._cover_decode_demand_sequence = 0
        self._cover_decode_failed = set()
        self._cover_decode_job_id = 0
        self._cover_committed_overrides = {}
        app = QApplication.instance()
        self._cover_decode_pool = QThreadPool(app or self)
        self._cover_decode_pool.setMaxThreadCount(2)
        # Keep the sender alive with QApplication while the Grid receiver is
        # being torn down; Qt disconnects the receiver automatically.
        self._cover_decode_signals = _CoverDecodeSignals(
            app or self)
        self._cover_decode_signals.finished.connect(self._on_cover_decoded)
        start_image_cache_maintenance()
        self._idle_memory_timer = QTimer(self)
        self._idle_memory_timer.setSingleShot(True)
        self._idle_memory_timer.setInterval(15_000)
        self._idle_memory_timer.timeout.connect(self._trim_hidden_memory)
        self._upload_statuses = {}
        self._known_files   = set()
        self._imported_files: set = set()
        # Save completion updates one card in place.  Keep the date-section
        # layouts indexed so adding a clip never has to tear down and recreate
        # every card in a large library.
        self._section_grids: dict[str, QGridLayout] = {}
        self._section_files: dict[str, list[str]] = {}
        self._section_widgets: dict[str, QWidget] = {}
        self._virtual_ready: dict[str, bool] = {}
        self._virtual_sections: dict[str, QWidget] = {}
        self._card_virtual_sections: dict[str, str] = {}
        self._virtual_initialized = False
        self._records: dict[str, LibraryRecord] = {}
        self._records_by_path: dict[str, LibraryRecord] = {}
        self._library_details_cache: dict[str, dict] = {}
        self._library_details_snapshot_dirty = True
        self._pending_details_signal_paths: set[str] = set()
        self._scan_generation = 0
        self._active_output_paths: frozenset[str] = frozenset()
        self._active_scan_worker: _FileCollectWorker | None = None
        self._pending_scan_request: tuple[str, str] | None = None
        self._shutdown_started = False
        self._storage_error_reported = False
        self._thumbnail_jobs_inflight: set[str] = set()
        self._deferred_thumbnail_paths: set[str] = set()
        self._thumbnail_job_fingerprints: dict[str, str] = {}
        # The set above is kept for bounded queue metrics and compatibility,
        # while this token map is the ownership authority.  A late terminal
        # signal from an older generation must never remove a replacement job
        # for the same path.
        self._thumbnail_job_owners: dict[str, tuple[int, str]] = {}
        # Keep signal senders alive until their queued terminal delivery. A
        # QRunnable may finish before the GUI gets back to its event queue.
        self._thumbnail_workers: dict[str, QRunnable] = {}
        self._thumbnail_cancel_event = threading.Event()
        self._thumbnail_generation = 0
        self._pending_saved_clips: dict[str, bool] = {}
        # CLIP_SAVED can arrive while discovery is already in flight.  Keep
        # the published record until that generation is merged so the final
        # scan cannot erase a valid just-saved clip it raced with.
        self._scan_upserts: dict[str, LibraryRecord] = {}
        self._thread_pool   = QThreadPool()
        # Cap at 2 workers. FFmpeg thumbnail decode is already heavy on disk
        # and CPU; throwing 16 threads at it just thrashes and makes everything
        # slower. 400ms of profiling led me here. don't touch this.
        self._thread_pool.setMaxThreadCount(2)
        # File scans must not sit behind a long queue of video decoders.  A
        # separate single-worker pool makes filter/sort changes respond as soon
        # as the current scan (if any) completes.
        self._scan_thread_pool = QThreadPool()
        self._scan_thread_pool.setMaxThreadCount(1)
        # Pre-compute the column count from the primary screen's available
        # width so the first paint already matches the maximized window.
        # Without this, the grid renders at 3 cols then snaps to N cols once
        # the resize event fires after showMaximized() — visible lag.
        initial_width = self._initial_viewport_width()
        (self._current_columns,
         self._current_card_widths) = self._layout_metrics(initial_width)

        self._filter = 'all'
        self._sort = 'newest'
        self._background_paused = False
        self._background_refresh_pending = False
        self._backup_restore_refresh_pending = False
        # Keep transition workers (and, critically, their signal objects) alive
        # until the queued completion callback has run on the GUI thread.
        self._diagnostic_scan_started: dict[int, float] = {}
        self._thumbnail_jobs_submitted = 0
        self._thumbnail_jobs_completed = 0
        self._thumbnail_diagnostics_completed = 0
        self._thumbnail_total_elapsed_ms = 0
        self._metadata_total_elapsed_ms = 0
        self._thumbnail_cache_hits = 0
        self._thumbnail_queue_peak = 0

        # Coalesce scroll/layout requests into one bounded update. This timer
        # runs after a change or to finish a bounded creation burst, never idle.
        self._virtual_update_timer = QTimer(self)
        self._virtual_update_timer.setSingleShot(True)
        self._virtual_update_timer.setInterval(16)
        self._virtual_update_timer.timeout.connect(self._update_virtualized_cards)
        # Rebind cards immediately, but do not start decoders for rows flying
        # past the viewport. Cached images remain available during scrolling.
        self._scrolling = False
        self._scroll_settle_timer = QTimer(self)
        self._scroll_settle_timer.setSingleShot(True)
        self._scroll_settle_timer.setInterval(90)
        self._scroll_settle_timer.timeout.connect(self._resume_scroll_thumbnails)

        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(180)
        self._search_debounce_timer.timeout.connect(self._commit_search_text)

        # Watcher must exist before _load_clips() runs
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        self._setup_ui(toolbar_layout)
        self._refresh_album_catalog()
        self._refresh_saved_searches()

        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._load_clips)

        # Debounced resize — only rebuilds the grid when columns would change.
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self.refresh_timer = QTimer()
        self.refresh_timer.setInterval(30000)
        self.refresh_timer.timeout.connect(self._load_clips)

        # Declare the painted library surface opaque so Qt can reuse scrolled
        # pixels instead of repainting the shell through transparent ancestors.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, QColor(Colors.BG).alpha() == 255)
        if autostart:
            self.start_scanning()

    def set_scan_suspended(self, suspended: bool) -> None:
        if suspended == getattr(self, '_scan_suspended', False):
            return
        self._scan_suspended = suspended
        if suspended:
            self.set_background_paused(True)
            paths = self._watcher.directories()
            if paths:
                self._watcher.removePaths(paths)
            self._pending_saved_clips.clear()
            self._background_refresh_pending = True

    def start_scanning(self):
        if (self._scanning_started or self._shutdown_started
                or getattr(self, '_scan_suspended', False)):
            return
        self._scanning_started = True
        # MainWindow creates the grid before recovery/migration completes.
        # Refresh the RAM catalog at the first post-migration scan boundary.
        self._refresh_album_catalog()
        self._watch_subdir(self.clips_dir)
        self.refresh_timer.start()
        self._load_clips()

    def _refresh_album_catalog(self, *, selected_id=_ALBUM_UNSET):
        """Publish the manager's RAM album catalog and keep the combo stable."""
        reader = getattr(self._metadata_manager, 'list_albums', None)
        try:
            albums = tuple(reader()) if callable(reader) else ()
        except (OSError, RuntimeError, TypeError, ValueError):
            albums = ()
        prior = self._album_id if selected_id is _ALBUM_UNSET else selected_id
        available = {str(album.album_id) for album in albums}
        criteria_album = getattr(self._search_criteria, 'album_id', None)
        reason_before = self._search_unavailable_reason
        # A criteria row can outlive its album (or the catalog reader can
        # temporarily fail).  Keep its UUID for inspection instead of
        # silently turning the query into All albums.
        criteria_missing = (
            criteria_album is not None
            and str(criteria_album) not in available)
        keep_missing = (criteria_missing and (
            prior is None or str(prior) == str(criteria_album)
            or self._saved_search_id is not None))
        if criteria_missing and keep_missing:
            prior = criteria_album
            if self._search_unavailable_reason is None:
                self._search_unavailable_reason = (
                    'Referenced album is unavailable.')
        elif criteria_missing and self._search_unavailable_reason is None:
            self._search_unavailable_reason = 'Referenced album is unavailable.'
        elif prior is not None and str(prior) not in available:
            prior = None
            if criteria_album is not None and self._saved_search_id is None:
                self._search_criteria = SearchCriteria(
                    text=self._search_criteria.text,
                    tags_all=self._search_criteria.tags_all,
                    album_id=None,
                    kind=self._search_criteria.kind,
                    favorites_only=self._search_criteria.favorites_only,
                    sort=self._search_criteria.sort)
        elif (keep_missing and str(prior) not in available
              and self._search_unavailable_reason is None):
            self._search_unavailable_reason = 'Referenced album is unavailable.'
        elif (criteria_album is not None and str(criteria_album) in available
              and self._search_unavailable_reason == 'Referenced album is unavailable.'):
            self._search_unavailable_reason = None
        self._albums = albums
        combo = getattr(self, 'album_combo', None)
        if combo is not None:
            blocked = combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItem('All albums', None)
                for album in albums:
                    name = str(album.name)
                    combo.addItem(name, str(album.album_id))
                    index = combo.count() - 1
                    combo.setItemData(index, name, Qt.ItemDataRole.ToolTipRole)
                target = combo.findData(prior)
                combo.setCurrentIndex(target if target >= 0 else 0)
            finally:
                combo.blockSignals(blocked)
        self._album_id = str(prior) if prior is not None else None
        self._album_snapshot_dirty = True
        self._update_album_bulk_menu()
        if self._search_unavailable_reason != reason_before:
            self._details_rebuild_pending = True
        if hasattr(self, 'selection_toolbar'):
            self._update_selection_ui()
        self._publish_album_navigation_state()

    def album_navigation_state(self):
        """Return the current album projection without consulting storage."""
        criteria = getattr(self, '_search_criteria', SearchCriteria())
        selected_id = getattr(criteria, 'album_id', None)
        return {
            'albums': tuple(
                (str(album.album_id), str(album.name))
                for album in getattr(self, '_albums', ())),
            'selected_id': (str(selected_id) if selected_id is not None else None),
            'locked': bool(getattr(self, '_bulk_job', None) is not None
                           or getattr(self, '_shutdown_started', False)),
            'unavailable': getattr(self, '_search_unavailable_reason', None),
        }

    def _publish_album_navigation_state(self):
        """Emit only changed RAM album snapshots for the shell/sidebar."""
        state = self.album_navigation_state()
        catalog = tuple(state['albums'])
        if catalog != self._published_album_catalog:
            self._published_album_catalog = catalog
            self.album_catalog_changed.emit(catalog)
        view = dict(state)
        if view != self._published_album_view:
            self._published_album_view = view
            self.album_view_changed.emit(dict(view))

    def select_album_view(self, album_id=None):
        """Select an album folder, or reset the visible query to All albums."""
        if self._shutdown_started or self._bulk_job is not None:
            return False
        selected_id = str(album_id) if album_id is not None else None
        if selected_id is not None and not any(
                str(album.album_id) == selected_id for album in self._albums):
            return False
        self._saved_search_update_target = None
        self._game_filter = None
        criteria = SearchCriteria(
            text='', tags_all=(), album_id=selected_id, kind='all',
            favorites_only=False,
            sort=getattr(self._search_criteria, 'sort', 'newest'))
        self.clear_selection()
        return bool(self._set_search_criteria(criteria))

    def rename_album_view(self, album_id, name=None, *, _require_selected=False):
        """Rename the captured UUID through the existing album manager."""
        if self._shutdown_started or self._bulk_job is not None:
            return False
        album_id = str(album_id) if album_id is not None else None
        current = next((album for album in self._albums
                        if str(album.album_id) == album_id), None)
        renamer = getattr(self._metadata_manager, 'rename_album', None)
        if current is None or not callable(renamer):
            return False
        criteria_before = self._search_criteria
        saved_before = self._saved_search_id
        update_target_before = self._saved_search_update_target
        if (_require_selected
                and str(getattr(criteria_before, 'album_id', None)) != album_id):
            return False
        if name is None:
            name, accepted = FthrInputDialog.get_text(
                self, 'Rename album', 'Album name:', text=str(current.name))
            if not accepted:
                return False
        if (self._shutdown_started or self._bulk_job is not None
                or self._search_criteria != criteria_before
                or self._saved_search_id != saved_before
                or self._saved_search_update_target != update_target_before
                or (_require_selected and self._album_id != album_id)
                or not any(str(album.album_id) == album_id
                           for album in self._albums)):
            return False
        try:
            renamer(album_id, name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(
                self, 'Album not renamed', str(error), plain_text=True)
            return False
        current_selection = getattr(self._search_criteria, 'album_id', None)
        self._refresh_album_catalog(selected_id=current_selection)
        self._apply_current_index()
        return True

    def delete_album_view(self, album_id, *, _require_selected=False):
        """Delete only the captured album membership and keep media files."""
        if self._shutdown_started or self._bulk_job is not None:
            return False
        album_id = str(album_id) if album_id is not None else None
        current = next((album for album in self._albums
                        if str(album.album_id) == album_id), None)
        deleter = getattr(self._metadata_manager, 'delete_album', None)
        if current is None or not callable(deleter):
            return False
        criteria_before = self._search_criteria
        saved_before = self._saved_search_id
        update_target_before = self._saved_search_update_target
        if (_require_selected
                and str(getattr(criteria_before, 'album_id', None)) != album_id):
            return False
        if not FthrMessageDialog.question(
                self, 'Delete album',
                f'Delete album "{current.name}"? Clips and files will be kept.',
                plain_text=True):
            return False
        if (self._shutdown_started or self._bulk_job is not None
                or self._search_criteria != criteria_before
                or self._saved_search_id != saved_before
                or self._saved_search_update_target != update_target_before
                or (_require_selected and self._album_id != album_id)
                or not any(str(album.album_id) == album_id
                           for album in self._albums)):
            return False
        try:
            deleter(album_id)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(
                self, 'Album not deleted', str(error), plain_text=True)
            return False
        current_selection = getattr(self._search_criteria, 'album_id', None)
        self._refresh_album_catalog(selected_id=current_selection)
        self._apply_current_index()
        return True

    def _refresh_album_snapshot(self):
        """Read one defensive binding snapshot for the current index refresh."""
        if self._album_id is None:
            self._album_bindings = {}
            self._album_snapshot_dirty = False
            return
        reader = getattr(self._metadata_manager, 'album_bindings', None)
        if not callable(reader):
            self._album_bindings = {}
            self._album_snapshot_dirty = False
            return
        try:
            snapshot = reader(self._album_id)
            self._album_bindings = {
                str(path_key): dict(identity)
                for path_key, identity in dict(snapshot).items()
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            self._album_bindings = {}
        self._album_snapshot_dirty = False

    def _album_menu_state(self, _path=None):
        return {
            'albums': tuple((str(album.album_id), str(album.name))
                            for album in self._albums),
            'current_id': self._album_id,
        }

    def _album_record_snapshot(self, path):
        return self._record_for_path(path)

    def _dispatch_single_album(self, operation, album_id, path, record, generation):
        if (self._shutdown_started or self._bulk_job is not None
                or self._record_for_path(path) != record):
            return
        self.start_bulk_album_membership(
            album_id, remove=operation == 'remove', records=(record,))

    def _update_album_bulk_menu(self):
        menu = getattr(self, '_bulk_album_menu', None)
        if menu is None:
            return
        menu.clear()
        for album in self._albums:
            action = menu.addAction(str(album.name))
            action.setData(str(album.album_id))
            action.triggered.connect(
                lambda checked=False, album_id=str(album.album_id):
                self.start_bulk_album_membership(album_id))
        menu.setEnabled(bool(self._albums))

    def _new_album(self):
        if self._bulk_job is not None or self._shutdown_started:
            return
        manager = self._metadata_manager
        creator = getattr(manager, 'create_album', None)
        if not callable(creator):
            FthrMessageDialog.warning(
                self, 'Albums unavailable', 'Album management is unavailable.',
                plain_text=True)
            return
        name, accepted = FthrInputDialog.get_text(
            self, 'New album', 'Album name:')
        if (not accepted or self._bulk_job is not None
                or self._shutdown_started):
            return
        try:
            album = creator(name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Album not created', str(error), plain_text=True)
            return
        self._refresh_album_catalog(selected_id=album.album_id)
        self.select_album_view(album.album_id)

    def _rename_album(self):
        return self.rename_album_view(self._album_id, _require_selected=True)

    def _delete_album(self):
        return self.delete_album_view(self._album_id, _require_selected=True)

    def _on_album_changed(self, _index):
        if self._bulk_job is not None or self._search_syncing:
            return
        combo = self.album_combo
        value = combo.currentData()
        self.select_album_view(value)

    def _criteria_from_controls(self, *, text=None, tags=None):
        kind = self._filter if self._filter in {
            'all', 'clips', 'screenshots', 'imported', 'favorites'} else 'all'
        favorites_only = bool(getattr(self, '_favorites_only', False))
        mapping = {
            'version': 1,
            'text': self.search_edit.text() if text is None else text,
            'tags_all': list(self._search_tags if tags is None else tags),
            'album_id': self._album_id,
            'kind': kind,
            'favorites_only': favorites_only,
            'sort': self._sort if self._sort in {'newest', 'oldest'} else 'newest',
        }
        return normalize_search_criteria(mapping)

    def current_search_criteria(self):
        """Return normalized visible criteria, committing pending text first."""
        if self._search_debounce_timer.isActive():
            self._commit_search_text()
        reason = self._search_unavailable_reason
        missing_typed_album = (
            self._search_criteria.album_id is not None
            and isinstance(reason, str)
            and reason.startswith('Referenced album'))
        if reason and not missing_typed_album:
            raise ValueError(reason)
        if self._saved_search_id is not None:
            item = self._saved_search_by_id(self._saved_search_id)
            if item is not None and item.criteria is None:
                raise ValueError(item.error or 'Saved search is unavailable.')
        return self._search_criteria

    def _mark_search_custom(self):
        if self._search_unavailable_reason:
            self._search_debounce_timer.stop()
            self._update_search_status()
            self._update_search_controls()
            return
        if self._saved_search_id is not None:
            self._saved_search_update_target = str(self._saved_search_id)
        self._saved_search_id = None
        combo = getattr(self, 'saved_search_combo', None)
        if combo is not None:
            blocked = combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(None)))
            combo.blockSignals(blocked)
        self._update_saved_search_action()
        self._update_search_status()

    def _apply_search_from_controls(self):
        if self._shutdown_started or self._bulk_job is not None:
            return False
        try:
            criteria = self._criteria_from_controls()
        except (TypeError, ValueError) as error:
            self._mark_search_custom()
            self._search_unavailable_reason = str(error)
            self._details_rebuild_pending = True
            self._update_search_controls()
            self._apply_query_index()
            return False
        return self._set_search_criteria(criteria)

    def _set_search_criteria(self, criteria, *, saved_search_id=None,
                              unavailable=None, update_text=True):
        criteria = normalize_search_criteria(criteria)
        previous_album = self._album_id
        self._search_debounce_timer.stop()
        self._search_text_pending = False
        self._search_criteria = criteria
        self._search_tags = tuple(criteria.tags_all)
        self._saved_search_id = (str(saved_search_id)
                                 if saved_search_id is not None else None)
        if saved_search_id is not None:
            self._saved_search_update_target = str(saved_search_id)
        self._search_unavailable_reason = (
            str(unavailable) if unavailable is not None else None)
        self._favorites_only = bool(criteria.favorites_only)
        previous_syncing = self._search_syncing
        self._search_syncing = True
        controls = (
            self.search_edit, self.filter_combo, self.sort_combo,
            self.favorites_only_check, self.album_combo,
            self.saved_search_combo,
        )
        blocked_states = [(widget, widget.blockSignals(True))
                          for widget in controls]
        try:
            if update_text:
                self.search_edit.setText(criteria.text)
            kind = criteria.kind
            self._filter = kind
            self.filter_combo.set_filter(kind, self._game_filter)
            self._sort = criteria.sort
            self.sort_combo.setCurrentIndex(0 if criteria.sort == 'newest' else 1)
            self.favorites_only_check.setChecked(bool(criteria.favorites_only))
            self._album_id = criteria.album_id
            target = self.album_combo.findData(criteria.album_id)
            self.album_combo.setCurrentIndex(target if target >= 0 else 0)
            saved_target = self.saved_search_combo.findData(self._saved_search_id)
            self.saved_search_combo.setCurrentIndex(
                saved_target if saved_target >= 0 else 0)
            selected_search = self._saved_search_by_id(self._saved_search_id)
            self.saved_search_combo.setToolTip(
                str(selected_search.name) if selected_search is not None
                else 'Saved searches')
        finally:
            for widget, was_blocked in blocked_states:
                widget.blockSignals(was_blocked)
            self._search_syncing = previous_syncing
        if saved_search_id is not None or previous_album != criteria.album_id:
            self._refresh_album_catalog(selected_id=criteria.album_id)
        elif previous_album != self._album_id:
            self._album_snapshot_dirty = True
        self._update_saved_search_action()
        self._details_rebuild_pending = True
        self._update_search_status()
        self._update_search_controls()
        self._apply_query_index()
        self._publish_album_navigation_state()
        return True

    def set_search_criteria(self, criteria, *, saved_search_id=None):
        """Apply strict criteria atomically and perform one guarded Apply."""
        try:
            normalized = normalize_search_criteria(criteria)
        except (TypeError, ValueError) as error:
            self._search_unavailable_reason = str(error)
            self._details_rebuild_pending = True
            self._update_search_status()
            self._apply_query_index()
            return False
        self._game_filter = None
        return self._set_search_criteria(
            normalized, saved_search_id=saved_search_id)

    def set_search_tags(self, tags):
        """Set exact tag criteria through the saved-search validator."""
        criteria = self._criteria_from_controls(tags=tags)
        self._mark_search_custom()
        return self._set_search_criteria(criteria)

    def _on_search_text_changed(self, _text):
        if self._search_syncing or self._shutdown_started:
            return
        self._search_text_pending = True
        self._mark_search_custom()
        self._search_debounce_timer.start()

    def _commit_search_text(self):
        if self._shutdown_started:
            return
        if self._bulk_job is not None:
            self._details_rebuild_pending = True
            return
        try:
            criteria = self._criteria_from_controls()
        except (TypeError, ValueError) as error:
            self._search_unavailable_reason = str(error)
            self._search_text_pending = False
            self._details_rebuild_pending = True
            self._update_search_status()
            self._update_search_controls()
            self._apply_query_index()
            return
        self._set_search_criteria(criteria, update_text=False)

    def _commit_pending_search_after_batch(self) -> bool:
        """Commit the latest visible text once a metadata batch releases."""
        if not getattr(self, '_search_text_pending', False):
            return False
        self._commit_search_text()
        return True

    def _on_favorites_only_changed(self, checked):
        if self._search_syncing or self._bulk_job is not None:
            return
        self._favorites_only = bool(checked)
        if checked:
            self._refresh_library_details_snapshot()
            self._favorite_snapshot_dirty = True
            self._query_snapshot_dirty = True
        self._mark_search_custom()
        self._apply_search_from_controls()

    def _refresh_tag_filter_menu(self):
        menu = self._tag_filter_menu
        menu.clear()
        values = set(self._search_tags)
        for item in self._library_details_cache.values():
            for tag in item.get('tags', ()):
                try:
                    tag_criteria = normalize_search_criteria({
                        'version': 1, 'text': '', 'tags_all': [tag],
                        'album_id': None, 'kind': 'all',
                        'favorites_only': False, 'sort': 'newest',
                    })
                except (TypeError, ValueError):
                    # Legacy tag values outside the strict search codec stay
                    # selectable only through their exact raw value.
                    if isinstance(tag, str) and len(tag) <= 32768:
                        values.add(tag)
                    continue
                values.update(tag_criteria.tags_all)
        if not values:
            empty = menu.addAction('No tags available')
            empty.setEnabled(False)
        else:
            for value in sorted(values):
                action = menu.addAction(
                    QFontMetrics(menu.font()).elidedText(
                        value, Qt.TextElideMode.ElideMiddle, 220))
                action.setData(value)
                action.setToolTip(value)
                action.setCheckable(True)
                action.setChecked(value in self._search_tags)
        menu.addSeparator()
        clear = menu.addAction('Clear tags')
        clear.setData(None)

    def _on_tag_filter_action(self, action):
        if self._bulk_job is not None or self._shutdown_started:
            return
        if action.data() is None:
            tags = ()
        else:
            tags = tuple(sorted(
                str(candidate.data()) for candidate in self._tag_filter_menu.actions()
                if candidate.isCheckable() and candidate.isChecked()
                and candidate.data() is not None))
        self._search_tags = tags
        self._mark_search_custom()
        self._apply_search_from_controls()

    def _update_search_status(self):
        label = getattr(self, 'search_status_label', None)
        if label is None:
            return
        reason = self._search_unavailable_reason
        if reason:
            label.setText(str(reason))
            label.setToolTip(str(reason))
            label.setVisible(True)
        elif self._saved_search_id:
            item = self._saved_search_by_id(self._saved_search_id)
            label.setText(f'Active search: {item.name}' if item else 'Active saved search')
            label.setToolTip('Reset clears this search without deleting it.')
            label.setVisible(True)
        else:
            label.clear()
            label.setToolTip('')
            label.setVisible(False)
        combo = getattr(self, 'filter_combo', None)
        if combo is not None:
            combo.set_indicators(len(self._search_tags), self._favorites_only)
        self.search_edit.setToolTip(', '.join(self._search_tags) if self._search_tags else 'Search clip names, tags and descriptions')

    def _update_saved_search_action(self):
        menu = getattr(self, '_saved_search_menu', None)
        if menu is None or not menu.actions():
            return
        target = self._saved_search_by_id(self._saved_search_update_target)
        update = menu.actions()[2] if len(menu.actions()) > 2 else None
        if update is not None:
            update.setText(
                f'Update "{target.name}"' if target is not None else 'Update')
            update.setToolTip(
                f'Update saved search "{target.name}"'
                if target is not None else 'Select a saved search to update')

    def _update_search_controls(self):
        locked = self._bulk_job is not None
        unavailable = self._search_unavailable_reason is not None
        self._tag_filter_menu.setEnabled(not locked and not unavailable)
        for widget in (getattr(self, 'search_edit', None),
                       getattr(self, 'tags_filter_btn', None),
                       getattr(self, 'favorites_only_check', None)):
            if widget is not None:
                widget.setEnabled(not locked and not unavailable)
        for widget in (getattr(self, 'saved_search_combo', None),):
            if widget is not None:
                widget.setEnabled(not locked)
        menu = getattr(self, '_saved_search_menu', None)
        if menu is not None:
            actions = menu.actions()
            if len(actions) >= 4:
                target = self._saved_search_by_id(self._saved_search_update_target)
                active = self._saved_search_by_id(self._saved_search_id)
                serializable = self._game_filter is None
                actions[0].setEnabled(not locked and not unavailable and serializable)
                actions[1].setEnabled(not locked and active is not None)
                actions[2].setEnabled(not locked and not unavailable
                                       and serializable and target is not None)
                actions[3].setEnabled(not locked and active is not None)
        manage = getattr(self, 'saved_search_manage_btn', None)
        if manage is not None:
            manage.setEnabled(not locked)
        reset = getattr(self, 'reset_search_btn', None)
        if reset is not None:
            reset.setEnabled(not locked)

    def refresh_saved_searches(self):
        """Refresh the saved-search RAM catalog without changing the query."""
        reason_before = self._search_unavailable_reason
        reader = getattr(self._metadata_manager, 'list_saved_searches', None)
        try:
            searches = tuple(reader()) if callable(reader) else ()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            if self._saved_search_id is not None:
                self._mark_saved_search_unavailable(self._saved_search_id, error)
            else:
                self._search_unavailable_reason = str(error)
                self._details_rebuild_pending = True
                self._update_search_status()
                self._apply_current_index()
            return self._saved_searches
        self._saved_searches = searches
        current = self._saved_search_by_id(self._saved_search_id)
        if self._saved_search_id is not None and current is None:
            self._search_unavailable_reason = 'Saved search is unavailable.'
        elif (current is not None and current.criteria is None):
            self._search_unavailable_reason = (
                current.error or 'Saved search is unavailable.')
        elif current is not None and current.error:
            # The manager's fresh row carries the authoritative reason for a
            # typed missing-album reference; keep it over a stale UI reason.
            self._search_unavailable_reason = str(current.error)
        elif self._search_unavailable_reason and current is not None:
            album_id = getattr(current.criteria, 'album_id', None)
            album_ids = {str(album.album_id) for album in self._albums}
            if (self._search_unavailable_reason !=
                    'Referenced album is unavailable.'
                    or album_id is None or str(album_id) in album_ids):
                self._search_unavailable_reason = None
        combo = getattr(self, 'saved_search_combo', None)
        if combo is not None:
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem('Custom', None)
            for item in searches:
                combo.addItem(str(item.name), str(item.search_id))
                index = combo.count() - 1
                tooltip = str(item.error) if item.error else str(item.name)
                combo.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
            target = combo.findData(self._saved_search_id)
            if self._saved_search_id is not None and target < 0:
                combo.addItem('Unavailable saved search', self._saved_search_id)
                target = combo.count() - 1
                combo.setItemData(
                    target, str(self._search_unavailable_reason),
                    Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(target if target >= 0 else 0)
            combo.setToolTip(
                str(combo.currentText()) if target > 0 else 'Saved searches')
            combo.blockSignals(blocked)
        self._update_saved_search_action()
        self._update_search_status()
        self._update_search_controls()
        if (self._search_unavailable_reason != reason_before
                or (current is None and self._saved_search_id is not None)):
            self._details_rebuild_pending = True
            self._apply_current_index()
        return self._saved_searches

    def _refresh_saved_searches(self):
        return self.refresh_saved_searches()

    def _saved_search_by_id(self, search_id):
        key = str(search_id) if search_id is not None else None
        return next((item for item in self._saved_searches
                     if str(item.search_id) == key), None)

    def _mark_saved_search_unavailable(self, search_id, reason):
        self._saved_search_id = str(search_id)
        self._search_unavailable_reason = str(reason)
        self._search_debounce_timer.stop()
        self._search_text_pending = False
        combo = getattr(self, 'saved_search_combo', None)
        if combo is not None:
            blocked = combo.blockSignals(True)
            index = combo.findData(self._saved_search_id)
            if index < 0:
                combo.addItem('Unavailable saved search', self._saved_search_id)
                index = combo.count() - 1
                combo.setItemData(index, str(reason), Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.setToolTip(str(combo.currentText()))
            combo.blockSignals(blocked)
        self._update_saved_search_action()
        self._details_rebuild_pending = True
        self._update_search_status()
        self._update_search_controls()
        self._apply_current_index()

    def _on_saved_search_changed(self, _index):
        if self._search_syncing or self._bulk_job is not None:
            return
        search_id = self.saved_search_combo.currentData()
        if search_id is None:
            if self._search_unavailable_reason:
                blocked = self.saved_search_combo.blockSignals(True)
                index = self.saved_search_combo.findData(self._saved_search_id)
                if index >= 0:
                    self.saved_search_combo.setCurrentIndex(index)
                self.saved_search_combo.blockSignals(blocked)
                return
            self._saved_search_id = None
            self._update_saved_search_action()
            self._update_search_status()
            self._update_search_controls()
            return
        item = self._saved_search_by_id(search_id)
        if item is None:
            return
        if item.criteria is None:
            self._mark_saved_search_unavailable(
                item.search_id, item.error or 'Saved search is unavailable.')
            return
        self._set_search_criteria(
            item.criteria, saved_search_id=item.search_id, unavailable=item.error)

    def apply_saved_search(self, search_id):
        self._game_filter = None
        self._sync_media_filter()
        item = self._saved_search_by_id(search_id)
        if item is None:
            self.refresh_saved_searches()
            item = self._saved_search_by_id(search_id)
        if item is None:
            self._mark_saved_search_unavailable(
                search_id, 'Saved search is unavailable.')
            return False
        if item.criteria is None:
            self._mark_saved_search_unavailable(
                item.search_id, item.error or 'Saved search is unavailable.')
            return False
        return self._set_search_criteria(
            item.criteria, saved_search_id=item.search_id, unavailable=item.error)

    def _new_saved_search(self):
        if self._bulk_job is not None or self._shutdown_started or self._game_filter is not None:
            return
        creator = getattr(self._metadata_manager, 'create_saved_search', None)
        if not callable(creator):
            return
        try:
            criteria = self.current_search_criteria()
        except (TypeError, ValueError) as error:
            FthrMessageDialog.warning(
                self, 'Saved search not created', str(error), plain_text=True)
            return
        name, accepted = FthrInputDialog.get_text(self, 'New saved search', 'Name:')
        if (not accepted or self._bulk_job is not None or self._shutdown_started
                or self._game_filter is not None):
            return
        try:
            saved = creator(name, criteria)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Saved search not created', str(error), plain_text=True)
            return
        self.refresh_saved_searches()
        self._set_search_criteria(criteria, saved_search_id=saved.search_id)

    def _rename_saved_search(self):
        if self._bulk_job is not None or self._shutdown_started:
            return
        search_id = self._saved_search_id
        item = self._saved_search_by_id(search_id)
        renamer = getattr(self._metadata_manager, 'rename_saved_search', None)
        if search_id is None or item is None or not callable(renamer):
            return
        name, accepted = FthrInputDialog.get_text(
            self, 'Rename saved search', 'Name:', text=str(item.name))
        if (not accepted or self._bulk_job is not None or self._shutdown_started
                or self._saved_search_id != search_id):
            return
        try:
            renamer(search_id, name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Saved search not renamed', str(error), plain_text=True)
            return
        self.refresh_saved_searches()

    def _update_saved_search(self):
        if self._bulk_job is not None or self._shutdown_started or self._game_filter is not None:
            return
        search_id = self._saved_search_update_target
        updater = getattr(self._metadata_manager, 'update_saved_search', None)
        if search_id is None or not callable(updater):
            return
        self.refresh_saved_searches()
        item = self._saved_search_by_id(search_id)
        if (item is None or item.criteria is None
                or self._search_unavailable_reason is not None):
            FthrMessageDialog.warning(
                self, 'Saved search not updated',
                'The saved search is unavailable.', plain_text=True)
            return
        try:
            criteria = self.current_search_criteria()
        except (TypeError, ValueError) as error:
            FthrMessageDialog.warning(
                self, 'Saved search not updated', str(error), plain_text=True)
            return
        if (self._bulk_job is not None or self._shutdown_started
                or self._game_filter is not None
                or self._saved_search_update_target != search_id):
            return
        try:
            updater(search_id, criteria)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Saved search not updated', str(error), plain_text=True)
            return
        self.refresh_saved_searches()

    def _delete_saved_search(self):
        if self._bulk_job is not None or self._shutdown_started:
            return
        search_id = self._saved_search_id
        item = self._saved_search_by_id(search_id)
        deleter = getattr(self._metadata_manager, 'delete_saved_search', None)
        if search_id is None or item is None or not callable(deleter):
            return
        if not FthrMessageDialog.question(
                self, 'Delete saved search',
                f'Delete saved search "{item.name}"?', plain_text=True):
            return
        if (self._bulk_job is not None or self._shutdown_started
                or self._saved_search_id != search_id):
            return
        try:
            deleter(search_id)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Saved search not deleted', str(error), plain_text=True)
            return
        # Keep the effective criteria and any unavailable reason as Custom.
        self._saved_search_id = None
        if self._saved_search_update_target == search_id:
            self._saved_search_update_target = None
        self.refresh_saved_searches()
        self._update_search_status()

    def _reset_search(self):
        if self._bulk_job is not None or self._shutdown_started:
            return
        self._saved_search_id = None
        self._saved_search_update_target = None
        self._search_unavailable_reason = None
        self._game_filter = None
        self._set_search_criteria(SearchCriteria())

    def set_interface_background_painter(self, painter):
        self._interface_background_painter = painter
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(Colors.BG))
        interface_background = getattr(self, '_interface_background_painter', None)
        if interface_background is not None:
            interface_background(painter)
        painter.end()

    def _connect_scroll_updates(self) -> None:
        """Attach after parenting/show, with one connection per current bar."""
        viewport = self.parentWidget()
        old_viewport = self._viewport_widget
        if old_viewport is not viewport:
            if old_viewport is not None and isValid(old_viewport):
                old_viewport.removeEventFilter(self)
            self._viewport_widget = viewport
            if viewport is not None:
                viewport.installEventFilter(self)
        parent, bar = viewport, None
        while parent is not None:
            getter = getattr(parent, 'verticalScrollBar', None)
            if callable(getter):
                bar = getter()
                break
            parent = parent.parentWidget()
        old_bar = self._scroll_bar
        if old_bar is bar:
            return
        if old_bar is not None and isValid(old_bar):
            old_bar.valueChanged.disconnect(self._on_library_scrolled)
            old_bar.rangeChanged.disconnect(self._request_virtual_update)
        self._scroll_bar = bar
        if bar is not None:
            bar.valueChanged.connect(self._on_library_scrolled)
            bar.rangeChanged.connect(self._request_virtual_update)

    def event(self, event):
        handled = super().event(event)
        if event.type() == QEvent.Type.ParentChange and hasattr(self, '_scroll_bar'):
            self._connect_scroll_updates()
        return handled

    def _on_library_scrolled(self, *_args):
        if self._background_paused or self._shutdown_started:
            return
        self._scrolling = True
        self._scroll_settle_timer.start()
        self._request_virtual_update()

    def _resume_scroll_thumbnails(self):
        self._scrolling = False
        self._requeue_visible_thumbnail_jobs()

    def is_linked_import(self, path: str) -> bool:
        target = os.path.normcase(os.path.realpath(path))
        return any(
            os.path.normcase(os.path.realpath(candidate)) == target
            for candidate in self._imported_files
        )

    def set_upload_checker(self, checker):
        """checker(path: str) -> bool  — True if the clip has been uploaded."""
        self._upload_checker = checker
        self._refresh_upload_states()

    def set_upload_info_checker(self, checker):
        """checker(path: str) -> dict | None — persisted provider result."""
        self._upload_info_checker = checker
        self._refresh_upload_states()

    def set_upload_enabled_checker(self, checker):
        """checker() -> bool  — True if uploads are enabled (shows Upload menu item)."""
        self._upload_enabled = checker
        enabled = bool(checker and checker())
        for card in self._thumb_widgets.values():
            card.upload_enabled = enabled
            card._update_share_action()

    def set_trash_handler(self, handler):
        self._trash_handler = handler
        for card in self.thumbnails:
            card._trash_handler = self._dispatch_trash if handler else None

    def _dispatch_trash(self, path, identity, completed, *, defer_refresh=False):
        identity = dict(identity) if identity is not None else None
        paths = (path, str(manifest_path_for(path)))
        self._mutation_paths.update(paths)
        finished = False

        def done(result):
            nonlocal finished
            if finished:
                return
            finished = True
            self._mutation_paths.difference_update(paths)
            if not isValid(self) or self._shutdown_started:
                return
            try:
                completed(result)
            finally:
                if not defer_refresh and isValid(self) and not self._shutdown_started:
                    if result.succeeded:
                        self.remove_trashed_paths(((path, identity),))
                    else:
                        self._flush_trash_refresh()
        try:
            if not callable(self._trash_handler):
                raise OSError('The library Trash service is unavailable')
            self._trash_handler(path, identity, done)
        except Exception as error:
            done(MutationResult('', FileOperationState.ROLLED_BACK, path, path, str(error)))

    def start_bulk_trash(self):
        if self._bulk_job is not None or self._shutdown_started or not self._selection.has_selection:
            return
        records = self.selected_records
        if not FthrMessageDialog.question(self, 'Move selected clips to Trash',
                                         f'Move {len(records)} selected clips to recoverable Trash? '
                                         'Favorites, linked clips and busy files will be kept.', plain_text=True):
            return
        if self._shutdown_started or self._bulk_job is not None or records != self.selected_records:
            return
        self._bulk_job = dict(kind='trash', records=records, index=0, successes=[], failures=[], cancel=False)
        self._update_selection_ui()
        self._bulk_timer.start(0)

    def _process_bulk_trash(self):
        job = self._bulk_job
        if job is None or self._shutdown_started:
            return
        if job['cancel'] or job['index'] >= len(job['records']):
            self._bulk_job = None
            self.remove_trashed_paths(job['successes'])
            self._commit_pending_search_after_batch()
            self._update_selection_ui()
            summary = f"{len(job['successes'])} moved to Trash, {len(job['failures'])} kept"
            if job['cancel']:
                summary += ' (cancelled)'
            self._set_bulk_status(summary)
            self.bulk_trash_finished.emit(dict(successes=tuple(path for path, _ in job['successes']),
                                               failures=tuple(job['failures']), cancelled=job['cancel']))
            if job['failures'] and not self._shutdown_started:
                detail = '\n'.join(f'{Path(path).name}: {reason}' for path, reason in job['failures'][:10])
                FthrMessageDialog.warning(self, 'Some clips were kept', summary + '\n' + detail, plain_text=True)
            return
        record = job['records'][job['index']]
        job['index'] += 1

        def done(result):
            if not isValid(self) or self._shutdown_started or self._bulk_job is not job:
                return
            if result.succeeded:
                job['successes'].append((record.path, record.file_identity))
            else:
                job['failures'].append((record.path, result.error or 'The file was kept'))
            self._set_bulk_status(f"{len(job['successes'])} moved to Trash, {len(job['failures'])} kept")
            self._bulk_timer.start(0)
        if record.file_identity is None:
            done(MutationResult('', FileOperationState.ROLLED_BACK, record.path, record.path,
                                'No complete selected-file evidence; refresh and select again'))
        else:
            self._dispatch_trash(record.path, record.file_identity, done, defer_refresh=True)

    def start_bulk_album_membership(self, album_id, *, remove=False, records=None):
        """Queue one guarded album membership operation per immutable record."""
        if self._bulk_job is not None or self._shutdown_started:
            return
        album_id = str(album_id) if album_id is not None else None
        if album_id is None or not self._albums:
            return
        if not any(str(album.album_id) == album_id for album in self._albums):
            return
        captured = tuple(records if records is not None else self.selected_records)
        if not captured:
            return
        self._bulk_job = dict(
            kind='album_membership', album_id=album_id, remove=bool(remove),
            records=captured, index=0, changed=[], unchanged=[], failures=[],
            cancel=False)
        action = 'Removing from album' if remove else 'Adding to album'
        self._set_bulk_status(f'{action}…')
        self._update_selection_ui()
        self._bulk_timer.start(0)

    def _process_bulk_album_membership(self, job):
        if job['cancel'] or job['index'] >= len(job['records']):
            self._bulk_job = None
            if job['index']:
                # Album adoption can make previously hidden legacy details
                # readable. Refresh once at the batch boundary before cards
                # are republished, never once per membership item.
                self._library_details_snapshot_dirty = True
                self._query_snapshot_dirty = True
                self._favorite_snapshot_dirty = True
                if self._mutation_paths:
                    self._library_details_snapshot_dirty = True
                self._album_snapshot_dirty = True
            pending_applied = self._commit_pending_search_after_batch()
            if not pending_applied:
                self._apply_current_index()
            self._update_selection_ui()
            summary = (
                f"{len(job['changed'])} changed, "
                f"{len(job['unchanged'])} unchanged, "
                f"{len(job['failures'])} failed")
            if job['cancel']:
                summary += ' (cancelled)'
            self._set_bulk_status(summary)
            result = dict(
                album_id=job['album_id'], remove=job['remove'],
                changed=tuple(job['changed']),
                unchanged=tuple(job['unchanged']),
                failed=tuple(job['failures']),
                failures=tuple(job['failures']),
                cancelled=bool(job['cancel']))
            self.bulk_albums_finished.emit(result)
            if job['failures'] and not self._shutdown_started:
                detail = '\n'.join(
                    f'{Path(path).name}: {reason}'
                    for path, reason in job['failures'][:10])
                FthrMessageDialog.warning(
                    self, 'Some album changes failed',
                    summary + '\n' + detail, plain_text=True)
            return

        deadline = time.monotonic() + 0.008
        processed = 0
        method_name = ('remove_album_member' if job['remove']
                       else 'add_album_member')
        method = getattr(self._metadata_manager, method_name, None)
        while (job['index'] < len(job['records']) and not job['cancel']
               and processed < 8 and time.monotonic() < deadline):
            record = job['records'][job['index']]
            job['index'] += 1
            processed += 1
            try:
                if record.file_identity is None:
                    raise ValueError(
                        'No complete selected-file evidence; refresh and select again')
                if self._readiness_checker and not self._readiness_checker(record.path):
                    raise OSError('Clip is not ready for changes')
                mutation_keys = {
                    canonical_media_path(path) for path in self._mutation_paths}
                if record.identity in mutation_keys:
                    raise OSError('Clip has a pending file operation')
                if not callable(method):
                    raise OSError('Album membership is unavailable')
                changed = method(
                    job['album_id'], record.path,
                    expected_identity=record.file_identity)
            except Exception as error:
                job['failures'].append((record.path, str(error)))
            else:
                (job['changed'] if changed else job['unchanged']).append(record.path)
        self._set_bulk_status(
            f"{len(job['changed'])} changed, {len(job['unchanged'])} unchanged, "
            f"{len(job['failures'])} failed")
        if job['index'] < len(job['records']) and not job['cancel']:
            self._bulk_timer.start(0)
            return
        self._bulk_timer.start(0)

    def _prompt_bulk_tags(self, operation):
        if (self._bulk_job is not None or self._shutdown_started
                or not self.selected_records):
            return
        records = tuple(self.selected_records)
        generation = self._scan_generation
        labels = {'add': 'Add tags', 'remove': 'Remove tags', 'replace': 'Replace tags'}
        text, accepted = FthrInputDialog.get_text(
            self, labels.get(operation, 'Edit tags'),
            'Tags (comma separated):')
        if (not accepted or self._shutdown_started or self._bulk_job is not None
                or generation != self._scan_generation
                or records != self.selected_records):
            return
        try:
            tags = parse_tags_text(text)
        except (TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Tags unchanged', str(error), plain_text=True)
            return
        if operation in {'add', 'remove'} and not tags:
            return
        self.start_bulk_tags(operation, tags, records=records)

    def _edit_card_tags(self, path, record, card_generation, details, card):
        if (self._shutdown_started or self._bulk_job is not None
                or record is None
                or self._record_for_path(path) != record
                or card.file_path != path
                or card._library_generation is not card_generation):
            return
        details_unknown = details is None
        details = details or {}
        try:
            initial = ('' if details.get('error')
                       else format_tags_text(details.get('tags', ())))
        except (TypeError, ValueError):
            initial = ''
            details = {**details, 'error': 'Stored tags are unavailable.'}
        if details.get('error'):
            prompt = 'Stored tags are invalid. Enter replacement tags (comma separated):'
        elif details_unknown:
            prompt = ('Existing tags are not verified; confirming replaces them, '
                      'and leaving this field empty clears any existing tags.\n'
                      'Tags (comma separated):')
        else:
            prompt = 'Tags (comma separated):'
        text, accepted = FthrInputDialog.get_text(
            self, 'Edit tags', prompt, text=initial)
        if (not accepted or self._shutdown_started or self._bulk_job is not None
                or card.file_path != path
                or card._library_generation is not card_generation
                or self._record_for_path(path) != record):
            return
        try:
            tags = parse_tags_text(text)
        except (TypeError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Tags unchanged', str(error), plain_text=True)
            return
        self.start_bulk_tags('replace', tags, records=(record,))

    def start_bulk_tags(self, operation, tags, *, records=None):
        """Run one guarded, cancellable tag operation over captured records."""
        if (operation not in {'add', 'remove', 'replace'}
                or self._bulk_job is not None or self._shutdown_started):
            return
        try:
            normalized = normalize_tags(tags)
        except (TypeError, ValueError):
            # Invalid tag input is rejected before a job can mutate metadata.
            return
        captured = tuple(records if records is not None else self.selected_records)
        if not captured:
            return
        self._bulk_job = dict(
            kind='tags', operation=operation, tags=normalized,
            records=captured, index=0, changed=[], unchanged=[], failures=[],
            cancel=False)
        self._set_bulk_status('Updating tags…')
        self._update_selection_ui()
        self._bulk_timer.start(0)

    def _process_bulk_tags(self, job):
        if job['cancel'] or job['index'] >= len(job['records']):
            changed = tuple(job['changed'])
            self._bulk_job = None
            if job['index']:
                self._library_details_snapshot_dirty = True
                self._favorite_snapshot_dirty = True
                self._query_snapshot_dirty = True
                self._details_rebuild_pending = True
            pending_applied = self._commit_pending_search_after_batch()
            if job['index'] and not pending_applied:
                self._apply_current_index()
            if changed:
                self._pending_details_signal_paths.update(changed)
                if not self._details_rebuild_pending:
                    self._emit_pending_library_details_changed()
            elif (job['cancel']
                  and getattr(self, '_details_rebuild_pending', False)):
                # The batch is released before flushing a debt left by an
                # earlier refresh; an untouched cancellation must still let
                # the existing guarded apply path consume that debt.
                self._flush_trash_refresh()
            self._update_selection_ui()
            summary = (f"{len(job['changed'])} changed, "
                       f"{len(job['unchanged'])} unchanged, "
                       f"{len(job['failures'])} failed")
            if job['cancel']:
                summary += ' (cancelled)'
            self._set_bulk_status(summary)
            result = dict(
                operation=job['operation'], tags=tuple(job['tags']),
                changed=tuple(job['changed']), unchanged=tuple(job['unchanged']),
                failed=tuple(job['failures']), failures=tuple(job['failures']),
                cancelled=bool(job['cancel']))
            self.bulk_tags_finished.emit(result)
            if job['failures'] and not self._shutdown_started:
                detail = '\n'.join(
                    f'{Path(path).name}: {reason}'
                    for path, reason in job['failures'][:10])
                FthrMessageDialog.warning(
                    self, 'Some tag changes failed', summary + '\n' + detail,
                    plain_text=True)
            return

        deadline = time.monotonic() + 0.008
        processed = 0
        updater = getattr(self._metadata_manager, 'update_tags', None)
        while (job['index'] < len(job['records']) and not job['cancel']
               and processed < 8 and time.monotonic() < deadline):
            record = job['records'][job['index']]
            job['index'] += 1
            processed += 1
            try:
                if record.file_identity is None:
                    raise ValueError('No complete file evidence; refresh and select the clip again')
                if self._readiness_checker and not self._readiness_checker(record.path):
                    raise OSError('Clip is not ready for changes')
                current = self._record_for_path(record.path)
                if current != record:
                    raise OSError('The clip changed; refresh and select it again')
                mutation_keys = {canonical_media_path(path) for path in self._mutation_paths}
                if record.identity in mutation_keys:
                    raise OSError('Clip has a pending file operation')
                if not callable(updater):
                    raise OSError('Library tags are unavailable')
                kwargs = dict(expected_identity=record.file_identity)
                if job['operation'] == 'replace':
                    kwargs['replace'] = job['tags']
                elif job['operation'] == 'add':
                    kwargs['add'] = job['tags']
                else:
                    kwargs['remove'] = job['tags']
                changed = updater(record.path, **kwargs)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._library_details_cache.pop(record.identity, None)
                job['failures'].append((record.path, str(error)))
            else:
                (job['changed'] if changed else job['unchanged']).append(record.path)
        self._set_bulk_status(
            f"{len(job['changed'])} changed, {len(job['unchanged'])} unchanged, "
            f"{len(job['failures'])} failed")
        self._bulk_timer.start(0)

    def remove_trashed_paths(self, evidence):
        """Publish only the committed identities, never a later path replacement."""
        expected = {
            os.path.normcase(os.path.abspath(os.path.normpath(path))): dict(identity)
            for path, identity in evidence if identity is not None
        }
        if not expected:
            self._flush_trash_refresh()
            return
        # A successful commit must be reconciled even when the filesystem
        # watcher did not emit an event for the move.
        self._mutation_refresh_pending = True

        def matches(record):
            key = os.path.normcase(os.path.abspath(os.path.normpath(record.path)))
            return key in expected and record.file_identity == expected[key]

        def committed_record(record):
            """Match the committed identity, or a stale record whose path vanished.

            A card may hold an older stat tuple than the fresh identity used by
            the file-operation admission.  Once the path is gone, that stale
            record is still the committed source; if a replacement exists at
            the same path it must remain visible.
            """
            key = os.path.normcase(os.path.abspath(os.path.normpath(record.path)))
            if key not in expected:
                return False
            if record.file_identity == expected[key]:
                return True
            try:
                os.lstat(record.path)
            except (FileNotFoundError, NotADirectoryError):
                return True
            except OSError:
                return False
            return False

        # A previously collected result must not put the removed identity back.
        # The runnable still owns its reference until its finished callback.
        if self._active_scan_worker is not None:
            self._scan_generation += 1
            self._active_scan_worker.cancel()
            if self._pending_scan_request is None:
                self._pending_scan_request = ('foreground_refresh', self._filter)
        removed = {record.path for record in self._records.values() if matches(record)}
        for key, record in tuple(self._scan_upserts.items()):
            if committed_record(record):
                self._scan_upserts.pop(key, None)
        # Use the same committed-path rule for the published records.  The
        # strict identity branch protects a later replacement at the same
        # path, while the missing-path branch clears an older stat snapshot.
        committed = {record.path for record in self._records.values()
                     if committed_record(record)}
        removed.update(committed)
        if removed:
            self._replace_records({key: record for key, record in self._records.items()
                                   if record.path not in removed})
            self._known_files.difference_update(removed)
            self._imported_files.difference_update(removed)
            for path in removed:
                self._virtual_ready.pop(path, None)
                self._pending_saved_clips.pop(path, None)
            # Retain a debt even when the gate is currently open.  The flush
            # below owns the single rebuild, so consecutive successful
            # removals cannot apply once immediately and then apply the older
            # debt a second time.
            self._trash_rebuild_pending = True
        self._flush_trash_refresh()

    def _flush_trash_refresh(self):
        """Coalesce watcher and batch requests into the existing pending scan."""
        if self._shutdown_started:
            return
        if self._background_paused:
            if (getattr(self, '_trash_rebuild_pending', False)
                    or getattr(self, '_album_rebuild_pending', False)
                    or getattr(self, '_details_rebuild_pending', False)
                    or self._mutation_refresh_pending
                    or self._pending_scan_request is not None):
                self._background_refresh_pending = True
            return
        if self._mutation_paths or (
                self._bulk_job is not None
                and self._bulk_job.get('kind')
                in {'trash', 'album_membership', 'tags'}):
            return
        if getattr(self, '_details_rebuild_pending', False):
            self._apply_current_index()
            if self._details_rebuild_pending:
                return
            # One successful index apply satisfies any coalesced detail debt.
            self._trash_rebuild_pending = False
            self._album_rebuild_pending = False
        elif (getattr(self, '_trash_rebuild_pending', False)
                or getattr(self, '_album_rebuild_pending', False)):
            self._trash_rebuild_pending = False
            self._apply_current_index()
        requested = (self._mutation_refresh_pending
                     or getattr(self, '_bulk_scan_pending', False)
                     or self._pending_scan_request is not None)
        self._mutation_refresh_pending = False
        self._bulk_scan_pending = False
        if requested:
            if self._pending_scan_request is None:
                self._pending_scan_request = ('foreground_refresh', self._filter)
            if self._active_scan_worker is None:
                QTimer.singleShot(0, self, self._start_pending_scan)

    def _start_pending_scan(self):
        """Consume one request even when multiple finite callbacks were queued."""
        if self._shutdown_started or self._active_scan_worker is not None:
            return
        pending = self._pending_scan_request
        if pending is None:
            return
        self._pending_scan_request = None
        if self._mutation_paths or (
                self._bulk_job is not None and self._bulk_job.get('kind') == 'trash'):
            self._mutation_refresh_pending = True
            return
        if self._background_paused and not self._pending_import_roots:
            self._background_refresh_pending = True
            return
        self._begin_scan(pending[0])

    def set_rename_handler(self, handler):
        self._rename_handler = handler
        for card in (*self.thumbnails, *self._recycled_cards):
            card._rename_handler = self._dispatch_rename if handler else None

    def _record_for_path(self, path):
        # Published display paths are exact keys; no filesystem resolution or
        # library-wide search belongs in card creation or selection refresh.
        return self._records_by_path.get(path)

    def _on_overlaps_changed(self, overlaps):
        self._overlaps = overlaps
        for path, card in self._thumb_widgets.items():
            card.set_overlap_paths(overlaps.get(path, ()))

    def _request_overlap_merge(self, path):
        card = self._thumb_widgets.get(path)
        if card is None or not card.ready or card._mutation_busy or card._batch_locked:
            return
        candidates = tuple(other for other in self._overlaps.get(path, ())
                           if other in self._records_by_path
                           and (not self._readiness_checker or self._readiness_checker(other)))
        if not candidates:
            return
        partner = candidates[0]
        if len(candidates) > 1:
            menu = QMenu(self)
            set_theme_style(menu, context_menu_qss)
            for other in candidates:
                action = menu.addAction(_clip_title_from_filename(other))
                action.setToolTip(other)
                action.setData(other)
            action = menu.exec(card._overlap_button.mapToGlobal(QPoint(0, card._overlap_button.height())))
            partner = action.data() if action is not None else None
            menu.deleteLater()
        if (partner and partner in self._overlaps.get(path, ()) and card.file_path == path
                and card.ready and not card._mutation_busy and not card._batch_locked
                and (not self._readiness_checker or self._readiness_checker(partner))):
            self.overlap_merge_requested.emit((path, partner))

    def _replace_records(self, records):
        active = getattr(self, '_active_output_paths', frozenset())
        if active:
            records = {key: record for key, record in records.items() if key not in active}
        self._album_drag_revision += 1
        self._records = records
        self._records_by_path = {record.path: record for record in records.values()}
        self._overlap_discovery.update(records.values())
        self._library_details_snapshot_dirty = True
        self._query_snapshot_dirty = True
        self._favorite_snapshot_dirty = True
        self._album_snapshot_dirty = True
        self._refresh_library_cover_snapshot()

    @property
    def selected_records(self):
        return self._selection.selected_records

    def _set_selection_mode(self, enabled):
        if self._bulk_job is not None:
            button = getattr(self, 'select_mode_btn', None)
            if button is not None and button.isChecked() != self._selection_mode:
                blocked = button.blockSignals(True)
                button.setChecked(self._selection_mode)
                button.blockSignals(blocked)
            return
        self._selection_mode = bool(enabled)
        if not self._selection_mode:
            self._selection.clear()
        button = getattr(self, 'select_mode_btn', None)
        if button is not None:
            if button.isChecked() != self._selection_mode:
                blocked = button.blockSignals(True)
                button.setChecked(self._selection_mode)
                button.blockSignals(blocked)
            button.setText('Done selecting' if self._selection_mode else 'Select')
            button.setToolTip(
                'Done selecting clips' if self._selection_mode
                else 'Select multiple clips')
            button.setAccessibleName(
                'Finish selecting clips' if self._selection_mode
                else 'Select multiple clips')
        self._update_selection_ui()

    def _select_card(self, path, modifiers, checkbox=False):
        if self._bulk_job is not None or self._shutdown_started:
            return True
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if (checkbox or control) and not self._selection_mode:
            self._set_selection_mode(True)
        if not self._selection_mode:
            return False
        record = self._record_for_path(path)
        if record is not None:
            self._selection.select(record, self._filtered_records(),
                                   toggle=control or checkbox or (self._selection_mode and not shift),
                                   extend=shift)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._update_selection_ui()
        return True

    def _start_album_drag(self, path, record):
        if (self._album_drag_lease is not None or self._shutdown_started
                or self._bulk_job is not None or record is None
                or self._record_for_path(path) != record):
            return
        ordered = tuple(self._filtered_records())
        records = (self.selected_records if self._selection.contains(record) else (record,))
        current = {item.identity: item for item in ordered}
        if not records or any(current.get(item.identity) != item for item in records):
            return
        # Readiness may probe legacy files: do it once, never on drag-move.
        if (self._readiness_checker
                and any(not self._readiness_checker(item.path) for item in records)):
            return
        snapshot = AlbumDragSnapshot(tuple(records), ordered, tuple(self._albums),
                                     self._search_criteria, self._album_drag_revision)
        mime = AlbumDragMimeData()
        drag = QDrag(self)
        drag.setMimeData(mime)
        preview = QPixmap(160, 44)
        preview.fill(Qt.GlobalColor.transparent)
        painter = QPainter(preview)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(Colors.SURFACE_2))
        painter.setPen(QColor(Colors.ACCENT))
        painter.drawRoundedRect(1, 1, 158, 42, 8, 8)
        painter.setPen(QColor(Colors.TEXT))
        painter.drawText(preview.rect(), Qt.AlignmentFlag.AlignCenter,
                         f'{len(records)} clip' + ('s' if len(records) != 1 else ''))
        painter.end()
        drag.setPixmap(preview)
        drag.setHotSpot(QPoint(16, 22))
        self._album_drag_lease = (mime, snapshot)
        try:
            drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            if isValid(self):
                self._album_drag_lease = None
                self.album_drag_finished.emit()
            if isValid(drag):
                drag.deleteLater()

    def can_drop_album_records(self, album_id, mime, source):
        lease = self._album_drag_lease
        if (source is not self or lease is None or mime is not lease[0]
                or not isinstance(mime, AlbumDragMimeData)
                or self._shutdown_started or self._bulk_job is not None
                or self._mutation_paths):
            return False
        snapshot = lease[1]
        if (snapshot.revision != self._album_drag_revision
                or snapshot.criteria != self._search_criteria
                or snapshot.catalog != tuple(self._albums)
                or not any(str(album.album_id) == str(album_id) for album in self._albums)
                or snapshot.ordered != self._album_drag_ordered):
            return False
        for record in snapshot.records:
            card = self._thumb_widgets.get(record.path)
            if (record.file_identity is None
                    or self._record_for_path(record.path) != record
                    or (card is not None and not card.ready)):
                return False
        return (self._album_drag_lease is lease and self._bulk_job is None
                and not self._shutdown_started
                and snapshot.revision == self._album_drag_revision)

    def drop_album_records(self, album_id, mime, source):
        if not self.can_drop_album_records(album_id, mime, source):
            return False
        records = self._album_drag_lease[1].records
        self.start_bulk_album_membership(str(album_id), records=records)
        return self._bulk_job is not None

    def select_all_results(self):
        if self._bulk_job is None:
            self._set_selection_mode(True)
            self._selection.select_all(self._filtered_records())
            self._update_selection_ui()

    def clear_selection(self):
        if self._bulk_job is None:
            self._selection.clear()
            self._update_selection_ui()

    def _update_selection_ui(self):
        count = self._selection.count
        locked = self._bulk_job is not None
        visible = self._selection_mode or locked
        self.selection_toolbar.setVisible(visible)
        self.selection_count_label.setText(f'{count} selected')
        for widget in (self.select_mode_btn, self.select_all_btn, self.clear_selection_btn,
                       self.filter_combo, self.sort_combo):
            widget.setEnabled(not locked)
        for widget in (getattr(self, 'album_combo', None),
                       getattr(self, 'album_manage_btn', None)):
            if widget is not None:
                widget.setEnabled(not locked)
        self._update_search_controls()
        self.bulk_actions_btn.setEnabled(bool(count) and not locked)
        remove_album_action = getattr(self, '_bulk_remove_album_action', None)
        if remove_album_action is not None:
            remove_album_action.setEnabled(bool(self._album_id) and not locked)
        self.bulk_export_btn.setEnabled(bool(count) and self._bulk_export_enabled and not locked)
        self.bulk_cancel_btn.setVisible(locked)
        for card in tuple(self._thumb_widgets.values()):
            record = self._record_for_path(card.file_path)
            card.set_selection_state(
                                     visible and record is not None
                                     and self._selection.contains(record),
                                     visible, locked=locked)
        self._publish_album_navigation_state()

    def set_bulk_export_enabled(self, enabled):
        self._bulk_export_enabled = bool(enabled)
        self._update_selection_ui()

    def _set_bulk_status(self, text):
        self.bulk_status_label.setText(text)
        self.bulk_status_label.setVisible(bool(text))

    def _request_bulk_export(self):
        if self._bulk_export_enabled and self._bulk_job is None and self.selected_records:
            self.bulk_export_requested.emit(self.selected_records)

    def _refresh_bulk_plugin_actions(self):
        menu = self._bulk_actions_menu
        for action in self._bulk_plugin_actions:
            menu.removeAction(action)
            action.deleteLater()
        self._bulk_plugin_actions.clear()
        if (self._bulk_clip_actions_provider is None or self._bulk_job is not None
                or self._shutdown_started):
            return
        records = tuple(record for record in self.selected_records if record.kind == 'video')
        if not records:
            return
        paths = tuple(record.path for record in records)
        before = self._bulk_plugin_insert_before
        for label, callback in self._bulk_clip_actions_provider(paths):
            if not self._bulk_plugin_actions:
                self._bulk_plugin_actions.append(menu.insertSeparator(before))
            action = menu.addAction(label)
            menu.insertAction(before, action)
            action.triggered.connect(
                lambda _checked=False, run=callback: self._invoke_bulk_plugin_action(records, run))
            self._bulk_plugin_actions.append(action)

    def _invoke_bulk_plugin_action(self, records, callback):
        if (self._shutdown_started or self._bulk_job is not None
                or records != tuple(item for item in self.selected_records if item.kind == 'video')):
            return
        for record in records:
            path = record.path
            if (self._record_for_path(path) != record or path in self._mutation_paths
                    or (self._readiness_checker and not self._readiness_checker(path))):
                FthrMessageDialog.warning(
                    self, 'Clip unavailable',
                    f'{Path(path).name} is unavailable. Update your selection and try again.',
                    plain_text=True)
                return
        callback()

    def _handle_selection_key(self, event):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return False
        if event.key() == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.select_all_results()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            if self._bulk_job is not None:
                self.cancel_bulk_flags()
            else:
                self._set_selection_mode(False)
            event.accept()
        else:
            return False
        return True

    def keyPressEvent(self, event):
        if not self._handle_selection_key(event):
            super().keyPressEvent(event)

    def _transfer_selection_rename(self, old_path, new_path):
        records = self._records_from_paths({new_path})
        new = records.get(canonical_media_path(new_path))
        if new is not None:
            old = self._records_by_path.get(old_path)
            if old is not None:
                from dataclasses import replace
                new = replace(new, root_ids=old.root_ids, imported=old.imported,
                              recorded=old.recorded, recorded_group=old.recorded_group)
            self._selection.rename(old_path, new)
            if old_path in self._imported_files:
                self._imported_files.discard(old_path)
                self._imported_files.add(new_path)
            old = self._records_by_path.pop(old_path, None)
            self._records.pop(old.identity if old is not None else canonical_media_path(old_path), None)
            self._records[new.identity] = new
            self._records_by_path[new.path] = new
            self._library_details_snapshot_dirty = True
            self._query_snapshot_dirty = True
            self._favorite_snapshot_dirty = True
            self._album_snapshot_dirty = True
            self._update_selection_ui()

    def start_bulk_flags(self, *, favorite):
        if self._bulk_job is not None or self._shutdown_started or not self.selected_records:
            return
        self._bulk_job = dict(records=self.selected_records, index=0, successes=[], failures=[],
                              cancel=False, favorite=favorite)
        self._set_bulk_status('Updating selected clips…')
        self._update_selection_ui()
        self._bulk_timer.start(0)

    def cancel_bulk_flags(self):
        if self._bulk_job is not None:
            self._bulk_job['cancel'] = True

    def _process_bulk_flags(self):
        job = self._bulk_job
        if job is None or self._shutdown_started:
            return
        if job.get('kind') == 'trash':
            self._process_bulk_trash()
            return
        if job.get('kind') == 'album_membership':
            self._process_bulk_album_membership(job)
            return
        if job.get('kind') == 'tags':
            self._process_bulk_tags(job)
            return
        deadline = time.monotonic() + 0.008
        processed = 0
        while (job['index'] < len(job['records']) and not job['cancel']
               and processed < 8 and time.monotonic() < deadline):
            record = job['records'][job['index']]
            job['index'] += 1
            processed += 1
            try:
                if record.file_identity is None:
                    raise ValueError('No complete file evidence; refresh and select the clip again')
                if self._readiness_checker and not self._readiness_checker(record.path):
                    raise OSError('Clip is not ready for changes')
                if record.identity in {canonical_media_path(path) for path in self._mutation_paths}:
                    raise OSError('Clip has a pending file operation')
                setter = getattr(self._metadata_manager, 'set_library_flags', None)
                if not callable(setter):
                    raise OSError('Library protection is unavailable')
                setter(record.path, favorite=job['favorite'],
                       expected_identity=record.file_identity)
            except Exception as error:
                # A failed item must not strand the GUI batch or hide later results.
                job['failures'].append((record.path, str(error)))
            else:
                job['successes'].append(record.path)
                for card in tuple(self._thumb_widgets.values()):
                    if card.file_path == record.path:
                        card.refresh_library_flags()
                        card.set_selection_state(True, True, locked=True)
                self.library_flags_changed.emit(record.path)
        self._set_bulk_status(
            f"{len(job['successes'])} updated, {len(job['failures'])} failed")
        if job['index'] < len(job['records']) and not job['cancel']:
            self._bulk_timer.start(0)
            return
        self._bulk_job = None
        if job['index']:
            # Flag writes may adopt legacy metadata. Publish one fresh details
            # snapshot after the last write, before any filtered-card apply.
            self._refresh_library_details_snapshot()
            self._favorite_snapshot_dirty = True
            self._query_snapshot_dirty = True
            if self._mutation_paths:
                self._library_details_snapshot_dirty = True
        pending_applied = self._commit_pending_search_after_batch()
        if (self._query_uses_metadata() and job['successes']
                and not pending_applied):
            self._details_rebuild_pending = True
            self._apply_current_index()
        self._update_selection_ui()
        if job['cancel']:
            self._set_bulk_status(self.bulk_status_label.text() + ' (cancelled)')
        self.bulk_flags_finished.emit(dict(successes=tuple(job['successes']),
                                          failures=tuple(job['failures']), cancelled=job['cancel']))
        if job['failures'] and not self._shutdown_started:
            detail = '\n'.join(f'{Path(path).name}: {error}' for path, error in job['failures'][:5])
            FthrMessageDialog.warning(self, 'Some clips were not updated',
                                      self.bulk_status_label.text() + '\n' + detail)

    def _dispatch_rename(self, old_path, new_path, allow_linked, completed):
        paths = (old_path, new_path)
        self._mutation_paths.update(paths)
        finished = False

        def done(result):
            nonlocal finished
            if finished:
                return
            finished = True
            # Queue reservations belong to Main, independently of this card.
            self._mutation_paths.difference_update(paths)
            try:
                completed(result)
            finally:
                if isValid(self) and not self._shutdown_started:
                    if result.succeeded:
                        self._transfer_selection_rename(old_path, new_path)
                        self.clip_renamed.emit(old_path, new_path)
                    self.force_refresh()

        try:
            self._rename_handler(old_path, new_path, allow_linked, done)
        except Exception as error:
            # A handler may reject synchronously before raising afterwards.
            done(MutationResult('', FileOperationState.ROLLED_BACK, old_path, new_path, str(error)))

    def set_mutation_callbacks(self, prepare, finish):
        self._mutation_prepare, self._mutation_finish = prepare, finish
        for card in self.thumbnails:
            card._prepare_mutation = self._prepare_card_mutation
            card._finish_mutation = self._finish_card_mutation

    def _prepare_card_mutation(self, path):
        reason = self._mutation_prepare(path) if self._mutation_prepare else ''
        if not reason:
            self._mutation_paths.add(path)
        return reason

    def _finish_card_mutation(self, path):
        self._mutation_paths.discard(path)
        if self._mutation_finish:
            self._mutation_finish(path)
        self._flush_trash_refresh()

    def _release_thumbnail_reader(self, path):
        key = canonical_media_path(path)
        for candidate, worker in tuple(self._thumbnail_workers.items()):
            if canonical_media_path(candidate) == key:
                worker.cancel_event.set()
        return not any(canonical_media_path(candidate) == key
                       for candidate in self._thumbnail_jobs_inflight)

    def _on_clip_renamed(self, old_path, new_path):
        self._transfer_selection_rename(old_path, new_path)
        # Migrate history before the newly named card is scanned.
        self.clip_renamed.emit(old_path, new_path)
        self.force_refresh()

    def _refresh_library_details_snapshot(self):
        """Publish one defensive metadata snapshot for the current index."""
        reader = getattr(self._metadata_manager, 'metadata_bindings', None)
        try:
            raw = reader() if callable(reader) else {}
        except (OSError, RuntimeError, TypeError, ValueError):
            raw = {}
        published = {}
        if isinstance(raw, dict):
            for identity, item in raw.items():
                record = self._records.get(str(identity))
                if record is None or not isinstance(item, dict):
                    continue
                expected = record.file_identity
                cached_identity = item.get('identity')
                if (expected is None or not isinstance(cached_identity, dict)
                        or cached_identity != expected):
                    continue
                description = item.get('description', '')
                if not isinstance(description, str):
                    continue
                tags = item.get('tags', ())
                error = item.get('error')
                if not isinstance(tags, (list, tuple)):
                    if error is None:
                        continue
                    tags = ()
                if error is not None and not isinstance(error, str):
                    error = str(error)
                published[str(identity)] = {
                    'identity': dict(expected),
                    'tags': tuple(tags),
                    'description': description,
                    'error': error,
                }
        self._library_details_cache = published
        self._library_details_snapshot_dirty = False
        self._query_snapshot_dirty = True

    def _refresh_library_cover_snapshot(self):
        """Publish cover references once at an index/metadata boundary."""
        if not hasattr(self, '_cover_bindings_snapshot'):
            self._cover_bindings_snapshot = {}
        if not hasattr(self, '_cover_generation'):
            self._cover_generation = 0
        reader = getattr(self._metadata_manager, 'cover_bindings', None)
        try:
            raw = reader() if callable(reader) else {}
        except (OSError, RuntimeError, TypeError, ValueError):
            raw = {}
        published = {}
        if isinstance(raw, dict):
            for path_key, item in raw.items():
                if not isinstance(item, dict):
                    continue
                record = self._records.get(str(path_key))
                if record is None or item.get('identity') != record.file_identity:
                    continue
                reference = item.get('reference')
                if reference is not None and not isinstance(reference, dict):
                    continue
                published[record.identity] = {
                    'clip_id': item.get('clip_id'),
                    'identity': dict(record.file_identity),
                    'reference': dict(reference) if reference is not None else None,
                    'error': item.get('error'),
                }
        self._cover_bindings_snapshot = published
        self._cover_generation += 1
        for path, override in tuple(self._cover_committed_overrides.items()):
            record = self._record_for_path(path)
            binding = (self._cover_bindings_snapshot.get(record.identity)
                       if record is not None else None)
            override_reference = self._canonical_cover_reference(
                override.get('reference'))
            if record is None or record.file_identity != override['identity']:
                self._cover_committed_overrides.pop(path, None)
            elif (override['cleared'] and binding is not None
                  and binding.get('reference') is None):
                self._cover_committed_overrides.pop(path, None)
            elif (not override['cleared'] and binding is not None
                  and binding.get('reference') == override_reference):
                self._cover_committed_overrides.pop(path, None)
        valid_keys = {
            cache_key for identity, binding in published.items()
            if (record := self._records.get(identity)) is not None
            and (cache_key := self._cover_cache_key_for(record, binding)) is not None
        }
        self._cover_decode_failed.intersection_update(valid_keys)
        self._prune_cover_decode_jobs()

    def _cover_cache_key_for(self, record, binding):
        reference = binding.get('reference') if isinstance(binding, dict) else None
        if not isinstance(reference, dict):
            return None
        revision = reference.get('revision')
        clip_id = binding.get('clip_id')
        if not isinstance(revision, str) or not isinstance(clip_id, str):
            return None
        if binding.get('identity') != record.file_identity:
            return None
        identity = tuple(sorted((record.file_identity or {}).items()))
        return f'{clip_id}:{revision}:{identity}'

    def _dispatch_cover_decode_jobs(self):
        if (getattr(self, '_background_paused', False)
                or getattr(self, '_shutdown_started', False)):
            return
        while len(self._cover_decode_jobs) < 32:
            candidates = [
                (cache_key, demand)
                for cache_key, demand in self._cover_decode_demands.items()
                if cache_key not in self._cover_decode_jobs
            ]
            if not candidates:
                break
            cache_key, demand = min(
                candidates,
                key=lambda item: (item[1]['priority'], item[1]['sequence']))
            self._cover_decode_job_id += 1
            job = dict(demand)
            job.update({
                'cancel': threading.Event(),
                'job_id': self._cover_decode_job_id,
                'started': False,
            })
            self._cover_decode_jobs[cache_key] = job
            self._cover_decode_pending[cache_key] = job

        active = sum(1 for job in self._cover_decode_jobs.values()
                     if job['started'])
        while active < 2 and self._cover_decode_pending:
            cache_key, job = min(
                self._cover_decode_pending.items(),
                key=lambda item: (item[1]['priority'], item[1]['sequence']))
            self._cover_decode_pending.pop(cache_key, None)
            if job['cancel'].is_set():
                self._cover_decode_jobs.pop(cache_key, None)
                self._cover_decode_demands.pop(cache_key, None)
                continue
            job['started'] = True
            active += 1
            self._cover_decode_pool.start(_CoverDecodeWorker(
                job['path'], cache_key, job['reference'],
                self._cover_decode_signals, job['cancel'], job['job_id']))

    def _enqueue_cover_decode(self, path, cache_key, reference, record):
        if cache_key in self._cover_decode_failed:
            return
        self._cover_decode_demand_sequence += 1
        card = self._thumb_widgets.get(record.path)
        priority = 0 if card is not None and card.isVisible() else 1
        demand = self._cover_decode_demands.get(cache_key)
        if demand is None:
            demand = {
                'path': path,
                'source_path': record.path,
                'identity': dict(record.file_identity),
                'reference': dict(reference),
                'priority': priority,
                'sequence': self._cover_decode_demand_sequence,
            }
            self._cover_decode_demands[cache_key] = demand
        else:
            demand['priority'] = min(demand['priority'], priority)
            if priority == 0:
                demand['sequence'] = self._cover_decode_demand_sequence
        job = self._cover_decode_jobs.get(cache_key)
        if job is not None and not job['started']:
            job['priority'] = demand['priority']
            job['sequence'] = demand['sequence']
        if len(self._cover_decode_demands) > 96:
            candidates = [
                (key, item) for key, item in self._cover_decode_demands.items()
                if key not in self._cover_decode_jobs and item['priority'] > 0
            ]
            if not candidates:
                candidates = [
                    (key, item) for key, item in self._cover_decode_demands.items()
                    if key not in self._cover_decode_jobs
                ]
            if candidates:
                old_key, _ = max(
                    candidates,
                    key=lambda item: (item[1]['priority'], item[1]['sequence']))
                self._cover_decode_demands.pop(old_key, None)
        self._dispatch_cover_decode_jobs()

    def _cancel_cover_decode_for_record(self, record):
        if record is None:
            return
        keys = [
            cache_key for cache_key, demand in self._cover_decode_demands.items()
            if demand['source_path'] == record.path
            and demand['identity'] == record.file_identity
        ]
        for cache_key in keys:
            job = self._cover_decode_jobs.get(cache_key)
            self._cancel_cover_decode_jobs(
                cache_key=cache_key,
                job_id=job['job_id'] if job is not None else None)

    def _cancel_cover_decode_jobs(self, *, cache_key=None, job_id=None):
        keys = (tuple(self._cover_decode_jobs)
                if cache_key is None else (cache_key,))
        for current_key in keys:
            job = self._cover_decode_jobs.get(current_key)
            if job is None:
                self._cover_decode_demands.pop(current_key, None)
                continue
            if job_id is not None and job['job_id'] != job_id:
                continue
            job['cancel'].set()
            self._cover_decode_demands.pop(current_key, None)
            if not job['started']:
                self._cover_decode_pending.pop(current_key, None)
                self._cover_decode_jobs.pop(current_key, None)
        if cache_key is None:
            self._cover_decode_demands.clear()
        self._dispatch_cover_decode_jobs()

    def _prune_cover_decode_jobs(self):
        for cache_key, job in tuple(self._cover_decode_jobs.items()):
            record = self._record_for_path(job['source_path'])
            binding = (self._cover_bindings_snapshot.get(record.identity)
                       if record is not None else None)
            current_key = (self._cover_cache_key_for(record, binding)
                           if record is not None else None)
            if current_key != cache_key:
                self._cancel_cover_decode_jobs(
                    cache_key=cache_key, job_id=job['job_id'])
        for cache_key, demand in tuple(self._cover_decode_demands.items()):
            if cache_key in self._cover_decode_jobs:
                continue
            record = self._record_for_path(demand['source_path'])
            binding = (self._cover_bindings_snapshot.get(record.identity)
                       if record is not None else None)
            if (record is None
                    or self._cover_cache_key_for(record, binding) != cache_key):
                self._cover_decode_demands.pop(cache_key, None)
        self._dispatch_cover_decode_jobs()

    def _apply_cover_to_card(self, card):
        record = self._record_for_path(card.file_path)
        if record is None:
            card.clear_custom_cover()
            return
        override = self._cover_committed_overrides.get(card.file_path)
        if override is not None:
            if record.file_identity != override['identity']:
                self._cover_committed_overrides.pop(card.file_path, None)
            elif override['cleared']:
                card.clear_custom_cover()
                card._render_thumbnail()
                return
            else:
                pixmap = override.get('pixmap')
                if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                    card.set_custom_cover(pixmap, 'committed-cover-pending-reload')
                    return
        binding = self._cover_bindings_snapshot.get(record.identity)
        cache_key = self._cover_cache_key_for(record, binding)
        if cache_key is None or binding.get('error'):
            card.clear_custom_cover()
            card._render_thumbnail()
            return
        # A materialized card may already own the validated cover even after
        # the bounded shared LRU evicted it.  Keep that card stable instead of
        # repeatedly re-enqueueing the same asset while the viewport drains.
        if (getattr(card, '_cover_cache_key', '') == cache_key
                and isinstance(getattr(card, '_cover_pixmap', None), QPixmap)
                and not card._cover_pixmap.isNull()):
            return
        cached = self._cover_pixmap_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            self._cover_pixmap_cache.move_to_end(cache_key)
            card.set_custom_cover(cached, cache_key)
            return
        card.clear_custom_cover()
        card._render_thumbnail()
        manager = self._metadata_manager
        asset_reader = getattr(manager, 'cover_asset_path_for_read', None)
        reference = binding.get('reference')
        if not callable(asset_reader):
            return
        try:
            asset_path = str(asset_reader(binding['clip_id'], reference['revision']))
        except (OSError, RuntimeError, TypeError, ValueError):
            # A missing or unsafe asset is a cover-cache miss; normal media
            # thumbnail rendering remains available.
            return
        self._enqueue_cover_decode(asset_path, cache_key, reference, record)

    def _on_cover_decoded(self, path, cache_key, image, error, job_id=None):
        job = self._cover_decode_jobs.get(cache_key)
        if job is None or (job_id is not None and job['job_id'] != job_id):
            return
        self._cover_decode_jobs.pop(cache_key, None)
        self._cover_decode_pending.pop(cache_key, None)
        if (self._shutdown_started or self._background_paused
                or job['cancel'].is_set()):
            if self._shutdown_started or self._background_paused:
                self._cover_decode_demands.pop(cache_key, None)
            # A resumed card may have registered fresh demand while this
            # cancelled job still owned the token. Keep it for a new decode.
            self._dispatch_cover_decode_jobs()
            return
        if error is not None or not isinstance(image, QImage) or image.isNull():
            if error != 'cancelled':
                self._cover_decode_failed.add(cache_key)
                self._cover_decode_demands.pop(cache_key, None)
            self._dispatch_cover_decode_jobs()
            return
        record = self._record_for_path(job['source_path'])
        binding = (self._cover_bindings_snapshot.get(record.identity)
                   if record is not None else None)
        if (record is None
                or record.file_identity != job['identity']
                or self._cover_cache_key_for(record, binding) != cache_key):
            self._cover_decode_demands.pop(cache_key, None)
            self._dispatch_cover_decode_jobs()
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._cover_decode_failed.add(cache_key)
            self._cover_decode_demands.pop(cache_key, None)
            self._dispatch_cover_decode_jobs()
            return
        self._cover_pixmap_cache[cache_key] = pixmap
        self._cover_pixmap_cache.move_to_end(cache_key)
        while len(self._cover_pixmap_cache) > self._cover_cache_limit:
            self._cover_pixmap_cache.popitem(last=False)
        for card in tuple(self._thumb_widgets.values()):
            record = self._record_for_path(card.file_path)
            if record is not None and self._cover_cache_key_for(
                    record, self._cover_bindings_snapshot.get(record.identity)) == cache_key:
                override = self._cover_committed_overrides.get(card.file_path)
                if (override is not None
                        and override.get('identity') == record.file_identity):
                    self._apply_cover_to_card(card)
                else:
                    card.set_custom_cover(pixmap, cache_key)
        self._cover_decode_demands.pop(cache_key, None)
        self._dispatch_cover_decode_jobs()

    def _cover_save_allowed(self, path, record, generation, card):
        """Check mutable grid state immediately before cover-write admission."""
        if (self._shutdown_started or self._bulk_job is not None
                or self._mutation_paths):
            return False
        if (card.file_path != path or card._library_generation is not generation
                or card._mutation_busy or card._batch_locked or not card.ready):
            return False
        current = self._record_for_path(path)
        return (current is record and current.file_identity == record.file_identity)

    def _open_cover_dialog(self, path, record, generation, card):
        """Open the explicit single-card cover action after fresh capture."""
        if (self._shutdown_started or self._bulk_job is not None
                or record is None or self._record_for_path(path) != record
                or card.file_path != path or card._library_generation is not generation):
            return
        manager = self._metadata_manager
        if manager is None or not isinstance(record.file_identity, dict):
            return
        binding = self._cover_bindings_snapshot.get(record.identity) or {}
        if binding.get('identity') != record.file_identity:
            # A replacement generation must not reuse its predecessor's CAS
            # revision or preview; the manager will create/adopt the current
            # generation under the explicit expected identity.
            binding = {}
        resolver = getattr(manager, 'resolve_media', None)
        clip_id = binding.get('clip_id')
        if callable(resolver):
            try:
                resolved = resolver(path)
            except (OSError, RuntimeError, TypeError, ValueError):
                resolved = None
            if resolved is not None and resolved.clip_id is not None:
                clip_id = str(resolved.clip_id)
        if card.file_path != path or self._record_for_path(path) != record:
            return
        reference = binding.get('reference')
        dialog = ClipCoverDialog(
            manager, path, expected_identity=dict(record.file_identity),
            expected_clip_id=clip_id, existing_reference=reference,
            duration_seconds=card._source_duration, is_video=card.is_video,
            initial_pixmap=(card._cover_pixmap
                            if not card._cover_pixmap.isNull()
                            else card._thumb_pixmap),
            save_guard=lambda: self._cover_save_allowed(
                path, record, generation, card), parent=self)
        dialog.cover_saved.connect(
            lambda payload, expected_path=path, expected_generation=generation,
                   expected_identity=dict(record.file_identity),
                   expected_clip_id=clip_id:
                self._on_cover_saved(
                    expected_path, expected_generation, payload,
                    expected_identity, expected_clip_id))
        try:
            dialog.exec()
        finally:
            # The dialog owns preview widgets and queued receiver connections;
            # release it after the nested event loop instead of retaining one
            # hidden QDialog per cover action on the grid.
            dialog.deleteLater()

    @staticmethod
    def _canonical_cover_reference(reference):
        if reference is None:
            return None
        if isinstance(reference, dict):
            return dict(reference)
        to_dict = getattr(reference, 'to_dict', None)
        if callable(to_dict):
            try:
                value = to_dict()
            except (AttributeError, TypeError, ValueError):
                # A malformed postcommit reference must not replace a
                # verified cover with an untrusted overlay.
                return None
            return dict(value) if isinstance(value, dict) else None
        return None

    def _on_cover_saved(self, path, generation, payload,
                        expected_identity=None, expected_clip_id=None):
        if self._shutdown_started:
            return
        if not isinstance(payload, dict):
            return
        card = self._thumb_widgets.get(path)
        current = self._record_for_path(path)
        if (current is None
                or (expected_identity is not None
                    and current.file_identity != expected_identity)):
            return
        if expected_clip_id is not None:
            current_binding = self._cover_bindings_snapshot.get(current.identity)
            if (isinstance(current_binding, dict)
                    and current_binding.get('clip_id') not in (
                        None, str(expected_clip_id))):
                return
        if payload.get('reload_needed'):
            if card is None or card._library_generation is not generation:
                return
            current_binding = self._cover_bindings_snapshot.get(current.identity)
            current_key = self._cover_cache_key_for(current, current_binding)
            if current_key is not None:
                self._cancel_cover_decode_jobs(cache_key=current_key)
            if payload.get('cleared'):
                self._cover_committed_overrides[path] = {
                    'identity': dict(current.file_identity),
                    'reference': None,
                    'pixmap': QPixmap(),
                    'cleared': True,
                }
                card.clear_custom_cover()
                card._render_thumbnail()
                self._start_thumbnail_worker(path)
            else:
                pixmap = payload.get('pixmap')
                reference = self._canonical_cover_reference(
                    payload.get('reference'))
                if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                    self._cover_committed_overrides[path] = {
                        'identity': dict(current.file_identity),
                        'reference': reference,
                        'pixmap': pixmap,
                        'cleared': False,
                    }
                    card.set_custom_cover(pixmap, 'committed-cover-pending-reload')
            return
        self._cover_committed_overrides.pop(path, None)
        self._refresh_library_cover_snapshot()
        if card is None or card._library_generation is not generation:
            return
        if payload.get('reference') is None:
            card.clear_custom_cover()
            card._render_thumbnail()
            self._start_thumbnail_worker(path)
            return
        self._apply_cover_to_card(card)

    @staticmethod
    def _search_tag_key(value):
        try:
            criteria = normalize_search_criteria({
                'version': 1, 'text': '', 'tags_all': [value],
                'album_id': None, 'kind': 'all',
                'favorites_only': False, 'sort': 'newest',
            })
        except (TypeError, ValueError):
            # Unsupported legacy tags cannot be converted into a search key.
            return None
        return criteria.tags_all[0] if criteria.tags_all else None

    @staticmethod
    def _search_text_value(value):
        if not isinstance(value, str):
            return ''
        value = unicodedata.normalize('NFC', value)
        result = []
        pending_space = False
        for char in value:
            if char.isspace() or unicodedata.category(char) in {'Cc', 'Cf'}:
                pending_space = True
                continue
            if pending_space and result:
                result.append(' ')
            pending_space = False
            result.append(char)
        return ''.join(result).casefold().strip()

    def _refresh_favorite_snapshot(self):
        reader = getattr(self._metadata_manager, 'flagged_paths', None)
        favorites: dict[str, dict[str, int]] = {}
        try:
            paths = reader('favorite') if callable(reader) else ()
            for path in paths:
                identity = canonical_media_path(path)
                record = self._records.get(identity)
                details = self._library_details_cache.get(identity)
                if (record is not None and isinstance(details, dict)
                        and record.file_identity is not None
                        and details.get('identity') == record.file_identity):
                    favorites[identity] = dict(record.file_identity)
        except (OSError, RuntimeError, TypeError, ValueError):
            # Failed flag reads fail closed; no arbitrary item becomes a favorite.
            favorites = {}
        self._favorite_snapshot = favorites
        self._favorite_snapshot_dirty = False

    def _refresh_query_snapshot(self):
        """Build one RAM-only query view for the published library index."""
        if getattr(self, '_favorite_snapshot_dirty', True):
            self._refresh_favorite_snapshot()
        if not getattr(self, '_query_snapshot_dirty', True):
            return
        snapshot = {}
        for record in self._records.values():
            details = self._library_details_cache.get(record.identity)
            tags = ()
            description = ''
            if (isinstance(details, dict)
                    and details.get('identity') == record.file_identity):
                tags = details.get('tags', ())
                description = details.get('description', '')
            tag_keys = frozenset(
                key for key in (self._search_tag_key(tag) for tag in tags)
                if key is not None)
            parts = [self._search_text_value(Path(record.path).name),
                     self._search_text_value(description)]
            parts.extend(sorted(tag_keys))
            snapshot[record.identity] = {
                'identity': (dict(record.file_identity)
                             if record.file_identity is not None else None),
                'tags': tag_keys,
                'haystack': ' '.join(part for part in parts if part),
            }
        self._search_query_cache = snapshot
        self._query_snapshot_dirty = False

    def library_details_for(self, path):
        """Return a defensive cached details row without performing I/O."""
        record = self._records_by_path.get(path)
        if record is None:
            return None
        item = self._library_details_cache.get(record.identity)
        if item is None or item.get('identity') != record.file_identity:
            return None
        return {
            'identity': dict(item['identity']),
            'tags': tuple(item.get('tags', ())),
            'description': str(item.get('description', '')),
            'error': item.get('error'),
        }

    def refresh_library_details(self, path=None):
        """Refresh the cached metadata snapshot and publish one grid update."""
        if self._shutdown_started:
            return None
        # Publish the explicit metadata read before a guarded Apply. A
        # concurrent mutation may defer the visual rebuild, but must not lose
        # this fresh cache or make the next Apply read it a second time.
        self._refresh_library_details_snapshot()
        # Flag changes are published through this boundary by older callers;
        # refresh the cached favorite projection together with metadata.
        self._favorite_snapshot_dirty = True
        self._query_snapshot_dirty = True
        self._details_rebuild_pending = True
        self._apply_current_index()
        if path is None:
            return None
        return self.library_details_for(path)

    def _emit_pending_library_details_changed(self):
        paths = tuple(self._pending_details_signal_paths)
        self._pending_details_signal_paths.clear()
        for path in paths:
            self.library_details_changed.emit(path)

    def refresh_library_flags(self, path):
        self._library_details_snapshot_dirty = True
        self._favorite_snapshot_dirty = True
        self._query_snapshot_dirty = True
        key = canonical_media_path(path)
        for card in tuple(self._thumb_widgets.values()):
            if canonical_media_path(card.file_path) == key:
                card.refresh_library_flags()
        if self._query_uses_metadata():
            self._details_rebuild_pending = True
            self._apply_current_index()
        else:
            # Keep a clean publication ready for a later query without
            # rebuilding the default card index for a flag-only update.
            self._refresh_library_details_snapshot()
            self._refresh_query_snapshot()

    def _on_library_flags_changed(self, path):
        self.refresh_library_flags(path)
        self.library_flags_changed.emit(path)

    def set_readiness_checker(self, checker):
        self._readiness_checker = checker

    def _refresh_upload_states(self):
        """Refresh badges and returned-link actions on cards already in the grid."""
        for path, card in self._thumb_widgets.items():
            uploaded = bool(self._upload_checker and self._upload_checker(path))
            info = (
                self._upload_info_checker(path)
                if uploaded and self._upload_info_checker else None)
            card.set_upload_info(info)
            card.set_uploaded(uploaded)

    def refresh_theme(self):
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, QColor(Colors.BG).alpha() == 255)
        self.update()
        _render_card_icon.cache_clear()
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            card.refresh_theme()

    def _viewport_width(self) -> int:
        """The scroll-area viewport is our parent — read its width so column
        calculation works even when the grid layout is wider than the viewport."""
        vp = self.parentWidget()
        return vp.width() if vp else self.width()

    def _initial_viewport_width(self) -> int:
        """Best-guess viewport width before the parent has been laid out.

        This lets the first build match the maximized window and avoids a
        visible reflow during startup.
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return 1100
        return screen.availableGeometry().width()

    @classmethod
    def _layout_metrics(cls, viewport_width: int, preferred=None, spacing=None) -> tuple[int, list[int]]:
        """Return column count and exact per-column widths for a viewport."""
        available = max(_CARD_MIN_W, int(viewport_width) - cls._H_MARGIN)
        spacing = cls._GRID_SPACING if spacing is None else spacing
        if preferred is not None:
            width = min(available, max(_CARD_MIN_W, min(_CARD_MAX_W, preferred)))
            cols = max(1, (available + spacing) // (width + spacing))
            return cols, [width] * cols
        cols = max(
            1,
            int((available + spacing)
                // (_CARD_W + spacing)),
        )
        while cols > 1:
            card_width = (
                available - spacing * (cols - 1)) // cols
            if card_width >= _CARD_MIN_W:
                break
            cols -= 1
        while True:
            card_width = (
                available - spacing * (cols - 1)) // cols
            if card_width <= _CARD_MAX_W:
                break
            if (available - spacing * cols) // (cols + 1) < _CARD_MIN_W:
                break
            cols += 1

        usable = min(available - spacing * (cols - 1), cols * _CARD_MAX_W)
        base, remainder = divmod(usable, cols)
        widths = [base + (1 if col < remainder else 0)
                  for col in range(cols)]
        return cols, widths

    def _interface_grid_spacing(self):
        designer = self._interface_card_designer
        margin = designer.card_gap() if designer is not None else None
        return self._GRID_SPACING if margin is None else margin

    def _active_layout_metrics(self, viewport_width):
        designer = self._interface_card_designer
        if designer is None:
            return self._layout_metrics(viewport_width)
        width, _height = designer.card_dimensions()
        return self._layout_metrics(viewport_width, width, self._interface_grid_spacing())

    def _initial_columns_from_screen(self) -> int:
        """Compatibility helper used by a few downstream integrations."""
        return self._active_layout_metrics(self._initial_viewport_width())[0]

    def _compute_columns(self) -> int:
        return self._active_layout_metrics(self._viewport_width())[0]

    def showEvent(self, event):
        super().showEvent(event)
        # A hidden initial scan may process events before setWidget parents
        # this grid. Show is a reliable retry, and attachment is idempotent.
        self._connect_scroll_updates()
        self._resize_timer.start(0)

    def eventFilter(self, obj, event):
        if obj is self._library_toolbar and event.type() == QEvent.Type.KeyPress:
            return self._handle_selection_key(event)
        # Debounce resize events to avoid rebuilding the grid mid-drag.
        if (obj is self._viewport_widget and event.type() == event.Type.Resize
                and not self._background_paused and not self._shutdown_started):
            self._resize_timer.start(200)
        return super().eventFilter(obj, event)

    def _on_resize_settled(self):
        new_cols, new_widths = self._active_layout_metrics(self._viewport_width())
        if (new_cols != self._current_columns
                or new_widths != self._current_card_widths):
            self._current_columns = new_cols
            self._current_card_widths = new_widths
            self._relayout_grids()
        self._request_virtual_update()

    def _on_dir_changed(self, path: str):
        if not self._scanning_started:
            return
        if self._background_paused:
            self._background_refresh_pending = True
            return
        self._debounce_timer.start(500)

    def _trim_hidden_memory(self):
        if not self._background_paused:
            return
        self._recent_thumbnails.clear()
        self._cover_pixmap_cache.clear()
        viewport = self._scroll_viewport()
        if viewport is not None:
            for card in self._thumb_widgets.values():
                bounds = QRect(card.mapTo(viewport, QPoint()), card.size())
                if (not bounds.intersects(viewport.rect())
                        and not card._editing_name and not card._mutation_busy):
                    card.release_thumbnail_image()
        # Preserve materialized cards, scroll position and active interactions.
        # A spare row is enough to avoid rebuilding controls on the next scroll.
        limit = max(1, self._current_columns)
        while len(self._recycled_cards) > limit:
            self._recycled_cards.pop().deleteLater()

    def _requeue_materialized_cover_demands(self):
        """Re-admit only current cards after a paused cover lane resumes."""
        if self._shutdown_started:
            return
        for card in tuple(self._thumb_widgets.values())[:_MAX_MATERIALIZED_CARDS]:
            self._apply_cover_to_card(card)

    def set_background_paused(self, paused: bool) -> None:
        """Suspend library scans while the containing app is in background.

        Capture/save completion can still invalidate the library through
        ``force_refresh``. While paused that work is coalesced into one refresh
        when the UI becomes active again.
        """
        paused = bool(paused) or getattr(self, '_scan_suspended', False)
        if paused == self._background_paused:
            return
        self._background_paused = paused
        if paused:
            self._idle_memory_timer.start()
            settle_timer = getattr(self, '_scroll_settle_timer', None)
            if settle_timer is not None:
                settle_timer.stop()
            self._scrolling = False
            self._virtual_update_timer.stop()
            self._resize_timer.stop()
            # Cancel queued and active decodes cooperatively. Do not clear the
            # pool or release ownership here: a queued/active QRunnable still
            # owns its token and must reach its terminal diagnostic callback.
            # Releasing it globally would let a replacement job run alongside
            # the old worker and would make a late result able to strand or
            # remove the replacement's ownership.
            self._thumbnail_cancel_event.set()
            self._thumbnail_generation += 1
            self._scan_generation += 1
            self._cancel_cover_decode_jobs()
            if self._active_scan_worker is not None:
                self._active_scan_worker.cancel()
            self.refresh_timer.stop()
            self._debounce_timer.stop()
            return

        # A cancelled QRunnable that was already in the pool can finish after
        # this method returns.  Start a fresh generation before re-enriching
        # visible cards so its late signal cannot own or clear a replacement
        # job.
        self._idle_memory_timer.stop()
        if not self._scanning_started or self._shutdown_started:
            self._flush_trash_refresh()
            return
        self._thumbnail_cancel_event = threading.Event()
        self._resize_timer.start(0)
        self._request_virtual_update()
        self._requeue_materialized_cover_demands()
        self._requeue_visible_thumbnail_jobs()
        self.refresh_timer.start()
        if self._backup_restore_refresh_pending:
            self._refresh_album_catalog()
            self.refresh_saved_searches()
            self.refresh_library_details()
            self._backup_restore_refresh_pending = False
        if self._background_refresh_pending:
            self._background_refresh_pending = False
            self._known_files = None
            trace_refresh = SAVE_TRACE_ENABLED and bool(self._pending_saved_clips)
            if trace_refresh:
                mark_save_trace('ui_library_resume_scan_request_begin')
            try:
                self._load_clips()
            finally:
                if trace_refresh:
                    mark_save_trace('ui_library_resume_scan_request_end')
        pending = self._pending_saved_clips
        self._pending_saved_clips = {}
        trace_pending = SAVE_TRACE_ENABLED and bool(pending)
        if trace_pending:
            mark_save_trace('ui_library_resume_upserts_begin', clip_count=len(pending))
        try:
            for path, ready in pending.items():
                self.upsert_saved_clip(path, ready=ready)
        finally:
            if trace_pending:
                mark_save_trace('ui_library_resume_upserts_end', clip_count=len(pending))
        self._flush_trash_refresh()


    def _setup_ui(self, toolbar_layout):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 0 if toolbar_layout is not None else 18, 24, 24)
        self.layout.setSpacing(0)

        self._library_toolbar = QWidget()
        self._library_toolbar.setObjectName('libraryToolbar')
        self._library_toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        header = QVBoxLayout(self._library_toolbar)
        header.setSpacing(0)
        if toolbar_layout is not None:
            header.setContentsMargins(24, 18, 24, 8)
            toolbar_layout.addWidget(self._library_toolbar)
            self._library_toolbar.installEventFilter(self)
        else:
            header.setContentsMargins(0, 0, 0, 8)
            self.layout.addWidget(self._library_toolbar)
        set_theme_style(self._library_toolbar, self._toolbar_qss)

        filt = QFrame()
        filt.setObjectName('filterBar')
        filt.setFixedHeight(Sizes.FILTER_H)
        fl = QHBoxLayout(filt)
        fl.setContentsMargins(8, 0, 8, 0)
        fl.setSpacing(5)

        self._all_count_label = _ToolbarCountLabel('All clips')
        self._all_count_label.setObjectName('allCountLabel')
        self._all_count_label.setTextFormat(Qt.TextFormat.PlainText)
        self._all_count_label.setFixedWidth(90)
        self._all_count_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        fl.addWidget(self._all_count_label)


        self.album_combo = _DropdownCombo()
        self.album_combo.setObjectName('albumCombo')
        self.album_combo.setMinimumWidth(120)
        self.album_combo.setMaximumWidth(220)
        self.album_combo.setToolTip('Filter by album')
        self.album_combo.currentIndexChanged.connect(self._on_album_changed)
        set_theme_style(self.album_combo, lambda: (combo_qss()))
        fl.addWidget(self.album_combo)

        self.album_manage_btn = QPushButton('Albums')
        self.album_manage_btn.setObjectName('albumManageButton')
        self.album_manage_btn.setToolTip('Manage albums')
        album_menu = QMenu(self.album_manage_btn)
        album_menu.addAction('New', self._new_album)
        album_menu.addAction('Rename', self._rename_album)
        album_menu.addAction('Delete', self._delete_album)
        self.album_manage_btn.setMenu(album_menu)
        self._album_manage_menu = album_menu
        set_theme_style(
            self.album_manage_btn,
            lambda: button_secondary_qss() + 'QPushButton { padding: 6px 10px; }')
        fl.addWidget(self.album_manage_btn)
        # The folder rail is the visible album navigator.  Keep these widgets
        # alive as compatibility adapters for existing callers and tests.
        self.album_combo.setVisible(False)
        self.album_manage_btn.setVisible(False)

        self.filter_combo = _MediaFilterCombo(self._game_groups)
        set_theme_style(self.filter_combo, lambda: combo_qss() + 'QComboBox { min-width: 0; padding-left: 8px; padding-right: 0px; }')
        self.filter_combo.setFixedWidth(136)
        self.filter_combo.setAccessibleName('Filter library media')
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.filter_combo.game_selected.connect(self._on_game_filter_changed)
        fl.addWidget(self.filter_combo)

        self.sort_combo = _DropdownCombo()
        self.sort_combo.addItems(['Newest', 'Oldest'])
        set_theme_style(self.sort_combo, lambda: combo_qss() + 'QComboBox { min-width: 0; padding-left: 8px; padding-right: 0px; }')
        self.sort_combo.setFixedWidth(108)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        fl.addWidget(self.sort_combo)

        self.select_mode_btn = QPushButton('Select')
        self.select_mode_btn.setObjectName('librarySelectMode')
        self.select_mode_btn.setAccessibleName('Select multiple clips')
        self.select_mode_btn.setToolTip('Select multiple clips')
        self.select_mode_btn.setFixedWidth(106)
        self.select_mode_btn.setCheckable(True)
        self.select_mode_btn.toggled.connect(self._set_selection_mode)
        fl.addWidget(self.select_mode_btn)
        header.addWidget(filt)

        self.search_edit = QLineEdit(filt)
        self.search_edit.setMinimumWidth(120)
        self.search_edit.setAccessibleName('Search clips, tags and descriptions')
        self.search_edit.setObjectName('librarySearchEdit')
        self.search_edit.setMaxLength(200)
        self.search_edit.setPlaceholderText('Search clips, tags and descriptions')
        self.search_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        set_theme_style(self.search_edit, lambda: lineedit_qss())
        fl.insertWidget(1, self.search_edit, 1)

        self.tags_filter_btn = QPushButton('Tags', self)
        self.tags_filter_btn.setObjectName('libraryTagsFilterButton')
        self.tags_filter_btn.setToolTip('Filter by tags')
        self._tag_filter_menu = QMenu(self.tags_filter_btn)
        self._tag_filter_menu.aboutToShow.connect(self._refresh_tag_filter_menu)
        self._tag_filter_menu.triggered.connect(self._on_tag_filter_action)
        self.tags_filter_btn.setMenu(self._tag_filter_menu)
        set_theme_style(
            self.tags_filter_btn,
            lambda: button_secondary_qss() + 'QPushButton { padding: 6px 10px; }')
        # Compatibility adapters retain search APIs/data without toolbar clutter.
        # Active tag/favorite criteria are reflected in the media filter caption.
        self.tags_filter_btn.hide()
        self.filter_combo._tag_menu = self._tag_filter_menu
        set_theme_style(self._tag_filter_menu, context_menu_qss)

        self.favorites_only_check = QCheckBox('Favorites only', self)
        self.favorites_only_check.setObjectName('favoritesOnlyCheck')
        self.favorites_only_check.toggled.connect(self._on_favorites_only_changed)
        set_theme_style(self.favorites_only_check, lambda: checkbox_qss())
        self.favorites_only_check.hide()

        self.saved_search_combo = _DropdownCombo(self)
        self.saved_search_combo.setObjectName('savedSearchCombo')
        self.saved_search_combo.setMinimumWidth(120)
        self.saved_search_combo.setMaximumWidth(220)
        self.saved_search_combo.setToolTip('Saved searches')
        self.saved_search_combo.currentIndexChanged.connect(
            self._on_saved_search_changed)
        set_theme_style(self.saved_search_combo, lambda: combo_qss())
        self.saved_search_combo.hide()

        self.saved_search_manage_btn = QPushButton('Saved', self)
        self.saved_search_manage_btn.setObjectName('savedSearchManageButton')
        self.saved_search_manage_btn.setToolTip('Manage saved searches')
        saved_menu = QMenu(self.saved_search_manage_btn)
        saved_menu.addAction('New', self._new_saved_search)
        saved_menu.addAction('Rename', self._rename_saved_search)
        saved_menu.addAction('Update', self._update_saved_search)
        saved_menu.addAction('Delete', self._delete_saved_search)
        self.saved_search_manage_btn.setMenu(saved_menu)
        self._saved_search_menu = saved_menu
        set_theme_style(
            self.saved_search_manage_btn,
            lambda: button_secondary_qss() + 'QPushButton { padding: 6px 10px; }')
        self.saved_search_manage_btn.hide()

        self.reset_search_btn = QPushButton('Reset')
        self.reset_search_btn.setObjectName('resetSearchButton')
        self.reset_search_btn.setToolTip('Reset search filters')
        self.reset_search_btn.clicked.connect(self._reset_search)
        set_theme_style(
            self.reset_search_btn,
            lambda: button_secondary_qss() + 'QPushButton { padding: 6px 10px; }')
        fl.insertWidget(2, self.reset_search_btn)

        self.search_status_label = QLabel('')
        self.search_status_label.setObjectName('searchStatusLabel')
        self.search_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.search_status_label.setVisible(False)
        self.search_status_label.setMaximumWidth(680)
        self.search_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        set_theme_style(
            self.search_status_label,
            lambda: label_display(Colors.TEXT_DIM, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL))
        header.addWidget(self.search_status_label)

        self.selection_toolbar = QWidget(self)
        selection_layout = QVBoxLayout(self.selection_toolbar)
        selection_layout.setContentsMargins(0, 8, 0, 8)
        selection_layout.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        selection_layout.addLayout(toolbar)
        self.selection_count_label = QLabel('0 selected')
        self.selection_count_label.setContentsMargins(0, 0, 6, 0)
        toolbar.addWidget(self.selection_count_label)
        self.select_all_btn = QPushButton('Select all results')
        self.select_all_btn.clicked.connect(self.select_all_results)
        toolbar.addWidget(self.select_all_btn)
        self.clear_selection_btn = QPushButton('Clear')
        self.clear_selection_btn.clicked.connect(self.clear_selection)
        toolbar.addWidget(self.clear_selection_btn)
        self.bulk_actions_btn = QPushButton('Actions')
        menu = QMenu(self.bulk_actions_btn)
        self._bulk_actions_menu = menu
        menu.addAction('Favorite', lambda: self.start_bulk_flags(favorite=True))
        menu.addAction('Unfavorite', lambda: self.start_bulk_flags(favorite=False))
        menu.addSeparator()
        self._bulk_album_menu = menu.addMenu('Add to album')
        self._bulk_remove_album_action = menu.addAction(
            'Remove from current album',
            lambda: self.start_bulk_album_membership(
                self._album_id, remove=True))
        menu.addSeparator()
        menu.addAction('Add tags…', lambda: self._prompt_bulk_tags('add'))
        menu.addAction('Remove tags…', lambda: self._prompt_bulk_tags('remove'))
        self._bulk_plugin_insert_before = menu.addSeparator()
        menu.addAction('Move to Trash', self.start_bulk_trash)
        self._bulk_plugin_actions = []
        menu.aboutToShow.connect(self._refresh_bulk_plugin_actions)
        self.bulk_actions_btn.setMenu(menu)
        toolbar.addWidget(self.bulk_actions_btn)
        self.bulk_export_btn = QPushButton('Export originals…')
        self.bulk_export_btn.setToolTip('Export original files, without editor changes')
        self.bulk_export_btn.clicked.connect(self._request_bulk_export)
        toolbar.addWidget(self.bulk_export_btn)
        self.bulk_cancel_btn = QPushButton('Cancel')
        self.bulk_cancel_btn.clicked.connect(self.cancel_bulk_flags)
        toolbar.addWidget(self.bulk_cancel_btn)
        self.bulk_status_label = QLabel('')
        self.bulk_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.bulk_status_label.setWordWrap(True)
        self.bulk_status_label.hide()
        selection_layout.addWidget(self.bulk_status_label)
        toolbar.addStretch()
        for button in (self.select_mode_btn, self.select_all_btn, self.clear_selection_btn,
                       self.bulk_actions_btn, self.bulk_cancel_btn):
            set_theme_style(button, lambda: button_secondary_qss() + 'QPushButton { padding: 6px 10px; }')
        set_theme_style(self.bulk_export_btn, lambda: button_primary_qss() + 'QPushButton { padding: 6px 10px; }')
        set_theme_style(menu, context_menu_qss)
        for label in (self.selection_count_label, self.bulk_status_label):
            set_theme_style(label, lambda: label_display(Colors.TEXT_DIM, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL))
        header.addWidget(self.selection_toolbar)
        self._update_selection_ui()

        self._pending_import_roots = set()
        self._import_baseline = set()
        self._import_status = QLabel()
        set_theme_style(self._import_status, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self._import_status.hide()
        self._import_actions = QWidget()
        actions = QHBoxLayout(self._import_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self._add_import_folder = QPushButton('Add folder')
        set_theme_style(self._add_import_folder, button_primary_qss)
        self._add_import_folder.clicked.connect(self.add_import_folder_requested.emit)
        actions.addWidget(self._add_import_folder)
        self._manage_import_folders = QPushButton('Manage folders')
        set_theme_style(self._manage_import_folders, button_secondary_qss)
        self._manage_import_folders.clicked.connect(self.manage_import_folders_requested.emit)
        actions.addWidget(self._manage_import_folders)
        actions.addStretch()
        self._import_actions.hide()
        self.layout.addWidget(self._import_actions)
        self._import_status.setWordWrap(True)
        self.layout.addWidget(self._import_status)

        self._sections_host = QWidget()
        self._sections_host.setStyleSheet('background: transparent;')
        self._sections_layout = QVBoxLayout(self._sections_host)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(20)
        self.layout.addWidget(self._sections_host)

        self._empty_widget = QWidget()
        self._empty_widget.setStyleSheet('background: transparent;')
        ev_layout = QVBoxLayout(self._empty_widget)
        ev_layout.setContentsMargins(0, 96, 0, 0)
        ev_layout.setSpacing(0)
        ev_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._no_clips_lbl = QLabel('No clips yet')
        set_theme_style(self._no_clips_lbl, lambda: (label_display(Colors.TEXT, Fonts.SIZE_H2, 0)))
        self._no_clips_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev_layout.addWidget(self._no_clips_lbl)

        self._empty_detail_lbl = QLabel()
        self._empty_detail_lbl.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(self._empty_detail_lbl, lambda: (label_display(Colors.TEXT_DIM, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        self._empty_detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_detail_lbl.setVisible(False)
        ev_layout.addSpacing(Sizes.SPACE_2)
        ev_layout.addWidget(self._empty_detail_lbl)

        ev_layout.addSpacing(Sizes.SPACE_4)

        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(0, 0, 0, 0)
        mark_row.addStretch()
        mark = QFrame()
        mark.setFixedSize(56, 2)
        set_theme_style(mark, lambda: (f'background: {Colors.ACCENT}; border: none;'))
        mark_row.addWidget(mark)
        mark_row.addStretch()
        ev_layout.addLayout(mark_row)

        self.layout.addWidget(self._empty_widget)
        self.layout.addStretch()

        set_theme_style(self, lambda: (self._grid_qss()))

    @staticmethod
    def _toolbar_qss() -> str:
        return f'''
            QWidget#libraryToolbar {{
                background: {Colors.BG};
            }}
            QFrame#filterBar {{
                background: {Colors.SURFACE_1};
                border-radius: {Sizes.RADIUS_MD}px;
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#allCountLabel {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: 600;
                font-size: {Fonts.SIZE_LABEL}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
        '''

    @staticmethod
    def _grid_qss() -> str:
        return f'''
            QPushButton#viewToggle, QPushButton#viewToggleActive {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-size: 14px;
            }}
            QPushButton#viewToggle:hover, QPushButton#viewToggleActive:hover {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QPushButton#viewToggleActive {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QLabel.sectionHeader {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: 600;
                font-size: {Fonts.SIZE_BODY_L}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QLabel.sectionSub {{
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                background: transparent;
            }}
        '''


    @staticmethod
    def _game_group(record: LibraryRecord, clips_root: Path) -> str | None:
        if record.kind != 'video' or record.imported:
            return None
        group = getattr(record, 'recorded_group', None)
        if not group:
            try:
                relative = Path(record.path).relative_to(clips_root)
            except ValueError:
                # A recording in a previous storage root keeps its game folder.
                if not getattr(record, 'recorded', False):
                    return None
                group = Path(record.path).parent.name
            else:
                if len(relative.parts) < 2:
                    return None
                group = relative.parts[0]
        if group.casefold() in {'desktop', 'recordings', 'screenshots', 'exported', 'shared'}:
            return None
        return group

    def _game_groups(self) -> list[str]:
        clips_root = Path(self.clips_dir)
        groups = {}
        for record in self._records.values():
            game = self._game_group(record, clips_root)
            if game is not None:
                groups.setdefault(game.casefold(), game)
        return sorted(groups.values(), key=str.casefold)

    def _sync_media_filter(self):
        blocked = self.filter_combo.blockSignals(True)
        try:
            self.filter_combo.set_filter(self._filter, self._game_filter)
        finally:
            self.filter_combo.blockSignals(blocked)

    def _on_game_filter_changed(self, game: str):
        if self._bulk_job is not None or self._search_syncing or self._shutdown_started:
            return
        self.clear_selection()
        self._game_filter = game
        self._filter = 'all'
        self._sync_media_filter()
        self._mark_search_custom()
        self._apply_search_from_controls()

    def _on_filter_changed(self, idx: int):
        if (self._bulk_job is not None or self._search_syncing
                or self._shutdown_started or not 0 <= idx < len(_MEDIA_FILTERS)):
            return
        self.clear_selection()
        self._game_filter = None
        self._filter = _MEDIA_FILTERS[idx][0]
        self._sync_media_filter()
        if self._filter == 'favorites':
            self._library_details_snapshot_dirty = True
            self._favorite_snapshot_dirty = True
            self._query_snapshot_dirty = True
        self._mark_search_custom()
        self._apply_search_from_controls()

    def _on_sort_changed(self, idx: int):
        if self._bulk_job is not None:
            return
        self._sort = ['newest', 'oldest'][idx]
        if self._search_syncing:
            return
        self._mark_search_custom()
        if self._shutdown_started:
            return
        self._apply_search_from_controls()

    def _filtered_records(self, *, ordered: bool = True) -> list[LibraryRecord]:
        criteria = getattr(self, '_search_criteria', None)
        legacy_host = criteria is None
        if legacy_host:
            kind = getattr(self, '_filter', 'all')
            if kind == 'favorites':
                kind = 'all'
                favorites_only = True
            else:
                favorites_only = False
            criteria = SearchCriteria(
                album_id=getattr(self, '_album_id', None),
                kind=kind if kind in {
                    'all', 'clips', 'screenshots', 'imported'} else 'all',
                favorites_only=favorites_only,
                sort=(getattr(self, '_sort', 'newest')
                      if getattr(self, '_sort', 'newest')
                      in {'newest', 'oldest'} else 'newest'))
        unavailable = getattr(self, '_search_unavailable_reason', None)
        records = list(getattr(self, '_records', {}).values())
        if unavailable:
            return []
        game = getattr(self, '_game_filter', None)
        if game is not None:
            clips_root = Path(self.clips_dir)
            game_key = game.casefold()
            records = [
                record for record in records
                if (self._game_group(record, clips_root) or '').casefold() == game_key
            ]
        album_id = criteria.album_id
        if album_id is not None:
            records = [
                record for record in records
                if getattr(self, '_album_bindings', {}).get(record.identity)
                == record.file_identity
            ]
        kind = criteria.kind
        if kind == 'clips':
            records = [record for record in records if record.kind == 'video']
        elif kind == 'screenshots':
            records = [record for record in records if record.kind == 'image']
        elif kind == 'imported':
            records = [record for record in records if record.imported]
        if kind == 'favorites' or criteria.favorites_only:
            favorites = getattr(self, '_favorite_snapshot', set())
            if isinstance(favorites, dict):
                records = [
                    record for record in records
                    if (record.file_identity is not None
                        and favorites.get(record.identity)
                        == record.file_identity)
                ]
            else:
                # Compatibility for lightweight legacy hosts that still expose
                # the pre-C9 set-shaped favorite cache.
                records = [record for record in records
                           if record.identity in favorites]
        if criteria.tags_all or criteria.text:
            query_cache = getattr(self, '_search_query_cache', {})
            tokens = tuple(criteria.text.casefold().split())
            required_tags = set(criteria.tags_all)
            filtered = []
            for record in records:
                row = query_cache.get(record.identity, {})
                if row.get('identity') != record.file_identity:
                    continue
                if required_tags and not required_tags.issubset(row.get('tags', ())):
                    continue
                haystack = row.get('haystack', '')
                if tokens and any(token not in haystack for token in tokens):
                    continue
                filtered.append(record)
            records = filtered
        if not ordered:
            return records
        sort = criteria.sort
        if sort == 'oldest':
            records.sort(key=lambda record: record.mtime_ns)
        elif legacy_host and getattr(self, '_sort', 'newest') == 'longest':
            records.sort(key=lambda record: record.size, reverse=True)
        else:
            records.sort(key=lambda record: record.mtime_ns, reverse=True)
        return records

    def _records_from_paths(self, paths: set[str]) -> dict[str, LibraryRecord]:
        result: dict[str, LibraryRecord] = {}
        recorded_media = self._recorded_media_snapshot()
        for raw in paths:
            try:
                info = os.stat(raw)
            except OSError:
                continue
            identity = canonical_media_path(raw)
            suffix = Path(raw).suffix.casefold()
            kind = 'video' if suffix in _VIDEO_EXTS else 'image'
            recorded, group = recorded_media_details(raw, {
                'st_dev': int(info.st_dev), 'st_ino': int(info.st_ino),
                'size': int(info.st_size), 'mtime_ns': int(info.st_mtime_ns),
            }, recorded_media)
            result[identity] = LibraryRecord(
                os.path.abspath(os.path.normpath(raw)), identity, (),
                raw in self._imported_files and not recorded, kind,
                int(info.st_size), int(info.st_mtime_ns), int(info.st_dev), int(info.st_ino),
                recorded=recorded, recorded_group=group)
        return result

    def _apply_query_index(self):
        """Apply criteria using already-published RAM snapshots only."""
        previous = self._query_apply_cache_only
        self._query_apply_cache_only = True
        try:
            return self._apply_current_index()
        finally:
            self._query_apply_cache_only = previous

    def _apply_current_index(self) -> None:
        self._album_drag_revision = getattr(self, '_album_drag_revision', 0) + 1
        cache_only = self._query_apply_cache_only
        if not cache_only:
            # A normal index publication is a fresh RAM snapshot boundary.
            # Query-only applies below deliberately reuse the clean snapshots.
            # Album membership is the one snapshot that the historical index
            # boundary always refreshes. Metadata/favorites/query snapshots
            # remain conditional so a caller that already published them does
            # not pay for a second metadata_bindings read.
            self._album_snapshot_dirty = True
        if self._mutation_paths or (self._bulk_job is not None
                                    and self._bulk_job.get('kind')
                                    in {'trash', 'album_membership', 'tags'}):
            self._mutation_refresh_pending = True
            self._details_rebuild_pending = True
            if self._album_id is not None:
                self._album_rebuild_pending = True
            return
        if self._library_details_snapshot_dirty:
            self._refresh_library_details_snapshot()
        self._details_rebuild_pending = False
        self._album_rebuild_pending = False
        self._all_count_label.setToolTip(self._filter_heading())
        # Discovery can finish before the first debounced Resize. Use the
        # real viewport now, otherwise scrolling initially targets a range
        # calculated for the primary monitor's much wider column count.
        self._current_columns, self._current_card_widths = self._active_layout_metrics(
            self._viewport_width())
        self._records_by_path = {record.path: record for record in self._records.values()}
        if getattr(self, '_album_snapshot_dirty', True):
            self._refresh_album_snapshot()
        self._refresh_query_snapshot()
        records = self._filtered_records()
        self._album_drag_ordered = tuple(records)
        self._selection.reconcile(records)
        self._update_selection_ui()
        self._deferred_thumbnail_paths.clear()
        self._thumbnail_cancel_event.set()
        self._thumbnail_generation += 1
        self._thumbnail_cancel_event = threading.Event()
        self._clear_sections()
        self.thumbnails.clear()
        self._thumb_widgets.clear()
        self._virtual_initialized = False
        self._virtual_ready.clear()
        if not records:
            self._show_empty(True)
            criteria = getattr(self, '_search_criteria', SearchCriteria())
            has_query = (
                self._search_unavailable_reason or criteria.text
                or criteria.tags_all or criteria.favorites_only
                or criteria.kind == 'favorites'
                or self._saved_search_id is not None)
            if has_query:
                self._all_count_label.setText('No matching clips')
            else:
                self._all_count_label.setText(self._filter_heading())
            self._emit_pending_library_details_changed()
            return
        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(records)})')
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for record in records:
            key = _section_label_for(record.mtime)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(record.path)
        global_idx = 0
        for section_key in order:
            files = groups[section_key]
            self._add_section(section_key, files, global_idx)
            global_idx += len(files)
        self._request_virtual_update()
        self._emit_pending_library_details_changed()

    def _filter_heading(self) -> str:
        if self._search_unavailable_reason:
            return 'SEARCH UNAVAILABLE'
        if self._album_id is not None:
            album = next((album for album in self._albums
                          if str(album.album_id) == self._album_id), None)
            if album is not None:
                return str(album.name)
        if self._game_filter is not None:
            return self._game_filter
        if self._filter == 'favorites':
            return 'Favorites'
        if self._filter == 'clips':
            return 'CLIPS'
        if self._filter == 'screenshots':
            return 'SCREENSHOTS'
        if self._filter == 'imported':
            return 'IMPORTED CLIPS'
        return 'ALL CLIPS'


    def set_active_output_paths(self, paths) -> None:
        """Hide recorder-owned files until finalization releases their ownership.

        Invalidate in-flight scans as well as their immutable input snapshot;
        otherwise a queued result could republish a just-reserved output.
        """
        active = frozenset(canonical_media_path(path) for path in paths)
        if active == self._active_output_paths:
            return
        self._active_output_paths = active
        self._scan_generation += 1
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
            self._pending_scan_request = ('foreground_refresh', self._filter)
        self._scan_upserts = {key: value for key, value in self._scan_upserts.items()
                              if key not in active}
        self._pending_saved_clips = {
            path: ready for path, ready in self._pending_saved_clips.items()
            if canonical_media_path(path) not in active}
        if any(key in active for key in self._records):
            self._replace_records(self._records)
            self._known_files = {record.path for record in self._records.values()}
            self._imported_files = {record.path for record in self._records.values()
                                    if record.imported}
            self._apply_current_index()
        if not self._shutdown_started:
            self.force_refresh()

    def _recorded_media_snapshot(self):
        reader = getattr(self._metadata_manager, 'recorded_media_snapshot', None)
        return reader() if callable(reader) else {}

    def _collect_media_files(self) -> set:
        """Compatibility helper; production refreshes use ``_FileCollectWorker``."""
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        result = scan_library(
            self.clips_dir, import_dirs, filter_='all', sort_=self._sort,
            recorded_media=self._recorded_media_snapshot(),
            active_output_paths=getattr(self, '_active_output_paths', ()))
        self._imported_files = set(result.imported_paths)
        for path in result.watched_directories:
            self._watch_subdir(path)
        self._replace_records({record.identity: record for record in result.all_records})
        return {record.path for record in result.all_records}

    def _watch_subdir(self, path: str):
        if (self._scanning_started and not self._shutdown_started
                and not getattr(self, '_scan_suspended', False)
                and os.path.isdir(path) and path not in self._watcher.directories()):
            self._watcher.addPath(path)

    def _sync_watched_directories(self, paths) -> None:
        """Keep QFileSystemWatcher bounded to the latest scanner result."""
        if (self._shutdown_started or not self._scanning_started
                or getattr(self, '_scan_suspended', False)):
            return
        wanted = {os.path.abspath(os.path.normpath(path)) for path in paths}
        current = set(self._watcher.directories())
        for path in current - wanted:
            self._watcher.removePath(path)
        for path in wanted - current:
            if os.path.isdir(path):
                self._watcher.addPath(path)


    def _load_clips(self):
        if not self._scanning_started:
            return
        if self._mutation_paths:
            self._mutation_refresh_pending = True
            return
        if self._background_paused:
            self._background_refresh_pending = True
            return
        if self._shutdown_started:
            return
        if not os.path.isdir(self.clips_dir):
            try:
                ensure_storage_directory(self.clips_dir)
            except StoragePathError as error:
                self._report_storage_unavailable(error)
                return
            if self.clips_dir not in self._watcher.directories():
                self._watcher.addPath(self.clips_dir)
            self._storage_error_reported = False
            self._show_empty(True)
            return
        self._begin_scan('foreground_refresh')

    def _report_storage_unavailable(self, error: StoragePathError) -> None:
        emit_event('storage', 'library_root_unavailable', state='FAILED',
                   path=self.clips_dir, detail=str(error))
        if self._storage_error_reported:
            return
        self._storage_error_reported = True
        FthrMessageDialog.warning(
            self, 'Clips Folder Unavailable',
            f'{error}\n\nFTHR will not switch to another folder. Restore access '
            'or choose a writable clips folder in Settings.')

    def _begin_scan(self, scan_kind: str) -> None:
        """Start one cancellable scan; at most one follow-up is retained."""
        if (not self._scanning_started or self._shutdown_started
                or getattr(self, '_scan_suspended', False)):
            return
        if self._bulk_job is not None and self._bulk_job.get('kind') == 'trash':
            self._bulk_scan_pending = True
            return
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        self._scan_generation += 1
        self._overlap_discovery.invalidate()
        generation = self._scan_generation
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
            self._pending_scan_request = (scan_kind, self._filter)
            return
        self._pending_scan_request = None
        cancel_event = threading.Event()
        worker = _FileCollectWorker(
            self.clips_dir, import_dirs, 'all', self._sort,
            cancel_event=cancel_event, recorded_media=self._recorded_media_snapshot(),
            active_output_paths=getattr(self, '_active_output_paths', ()))
        worker.generation = generation
        worker.scan_kind = scan_kind
        self._active_scan_worker = worker
        started = time.monotonic()
        self._diagnostic_scan_started[generation] = started
        emit_event(
            'library', 'scan_started', state='SCANNING', scan_kind=scan_kind,
            generation=generation, root_count=1 + len(import_dirs),
            active_workers=self._scan_thread_pool.activeThreadCount())

        def _on_done(raw, pairs, imported, subdirs):
            if self._active_scan_worker is worker:
                self._active_scan_worker = None
            elapsed = round((time.monotonic() - started) * 1000)
            result = worker.result
            if result is None or result.stats.cancelled:
                emit_event('library', 'scan_cancelled', state='CANCELLED',
                           generation=generation, elapsed_ms=elapsed)
            elif (generation == self._scan_generation and self._background_paused
                  and not self._shutdown_started):
                self._finish_import_feedback(result.all_records)
                self._background_refresh_pending = True
            elif generation == self._scan_generation and not self._shutdown_started:
                self._finish_import_feedback(result.all_records)
                self._sync_watched_directories(subdirs)
                next_records = {record.identity: record
                                for record in result.all_records}
                for identity, upserted in list(self._scan_upserts.items()):
                    try:
                        if os.path.isfile(upserted.path):
                            next_records.setdefault(identity, upserted)
                    except OSError:
                        # A deleted path must not be resurrected by a late
                        # CLIP_SAVED notification.
                        continue
                self._scan_upserts.clear()
                self._imported_files = set(result.imported_paths)
                self._known_files = {record.path for record in result.all_records}
                summary = result.stats.as_dict() | {
                    'scan_kind': scan_kind,
                    'generation': generation,
                    'candidate_media_count': len(result.all_records),
                    'candidate_video_count': sum(
                        record.kind == 'video' for record in result.all_records),
                    'imported_media_count': len(self._imported_files),
                    'elapsed_ms': elapsed,
                    'thumbnail_widget_cache_entries': len(self._thumb_widgets),
                    'metadata_cache_entries': len(self._records),
                    'active_thumbnail_workers': self._thread_pool.activeThreadCount(),
                    'active_scan_workers': self._scan_thread_pool.activeThreadCount(),
                    'process_memory_bytes': process_memory_bytes(),
                }
                emit_event('library', 'scan_completed', state='COMPLETED', **summary)
                session = get_diagnostic_session()
                if session is not None:
                    session.update_summary('library', summary)
                if next_records != self._records:
                    self._replace_records(next_records)
                    self._apply_current_index()
                else:
                    self._overlap_discovery.update(self._records.values())
                    rebuild_blocked = (
                        self._mutation_paths
                        or (self._bulk_job is not None
                            and self._bulk_job.get('kind')
                            in {'trash', 'album_membership', 'tags'}))
                    if (self._details_rebuild_pending and not rebuild_blocked):
                        self._apply_current_index()
                    elif ((self._trash_rebuild_pending
                         or self._album_rebuild_pending)
                            and not rebuild_blocked):
                        self._trash_rebuild_pending = False
                        self._apply_current_index()
                    else:
                        self._refresh_album_snapshot()
                        self._requeue_visible_thumbnail_jobs()
            self._diagnostic_scan_started.pop(generation, None)
            if self._pending_scan_request and not self._shutdown_started:
                QTimer.singleShot(0, self, self._start_pending_scan)

        worker.signals.finished.connect(_on_done)
        worker.signals.batch.connect(self._on_scan_batch)
        self._scan_thread_pool.start(worker)
        # Give a tiny initial scan a chance to publish before callers inspect
        # a freshly constructed ClipGrid (and before the first paint).  The
        # wait is hard-bounded; large libraries continue asynchronously and
        # never occupy the main thread for the duration of their scan.
        if (scan_kind == 'foreground_refresh' and not self._records
                and not self.isVisible()):
            self._scan_thread_pool.waitForDone(100)
            QApplication.processEvents()

    def _on_scan_batch(self, generation: int, records) -> None:
        """Publish the first discovery batch before the scan finishes.

        Keep later batches on the worker to avoid repeated Qt model rebuilds;
        publish the sorted result once the generation completes.
        """
        if (self._shutdown_started or self._background_paused or generation != self._scan_generation
                or self._active_scan_worker is None
                or getattr(self._active_scan_worker, 'generation', None)
                != generation or self._records):
            if generation != self._scan_generation:
                emit_event('library', 'stale_scan_batch_discarded',
                           state='DISCARDED', generation=generation)
            return
        active = getattr(self, '_active_output_paths', frozenset())
        partial = {record.identity: record for record in records
                   if record.identity not in active}
        if not partial:
            return
        self._replace_records(partial)
        self._known_files = {record.path for record in partial.values()}
        self._imported_files = {
            record.path for record in partial.values() if record.imported}
        emit_event('library', 'scan_batch_published', state='PARTIAL',
                   generation=generation, record_count=len(partial))
        self._apply_current_index()

    def _finish_thumbnail_job(
            self, file_path: str, generation: int, fingerprint: str) -> bool:
        """Release one job only when the callback still owns its token."""
        token = (generation, fingerprint)
        if self._thumbnail_job_owners.get(file_path) != token:
            return False
        self._thumbnail_job_owners.pop(file_path, None)
        self._thumbnail_workers.pop(file_path, None)
        self._thumbnail_jobs_inflight.discard(file_path)
        if self._thumbnail_job_fingerprints.get(file_path) == fingerprint:
            self._thumbnail_job_fingerprints.pop(file_path, None)
        return True

    def _requeue_visible_thumbnail_jobs(self) -> None:
        """Re-enrich visible cards without rebuilding the library index."""
        if self._background_paused or self._shutdown_started:
            return
        for file_path, card in tuple(self._thumb_widgets.items()):
            if card.ready and not card._thumbnail_ready:
                self._start_thumbnail_worker(file_path)

    def _indexed_record(self, file_path: str):
        # Scanned paths are already absolute/canonical. Avoid resolving them
        # through Windows filesystem handles again for each scrolling card.
        return (self._records.get(os.path.normcase(os.path.normpath(file_path)))
                or self._records.get(canonical_media_path(file_path)))

    def _thumbnails_enabled(self):
        return self._sm is None or bool(self._sm.get('library_thumbnails_enabled', True))

    def apply_thumbnail_preference(self):
        if self._thumbnails_enabled():
            self._requeue_visible_thumbnail_jobs()
            return
        for worker in tuple(self._thumbnail_workers.values()):
            worker.cancel_event.set()
        self._recent_thumbnails.clear()
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            card.release_thumbnail_image()
        self._deferred_thumbnail_paths.clear()

    def _start_thumbnail_worker(self, file_path: str) -> None:
        if not self._thumbnails_enabled():
            return
        if self._background_paused or self._shutdown_started:
            return
        card = self._thumb_widgets.get(file_path)
        if (card is None or not card.ready
                or card._mutation_busy
                or card._thumbnail_ready
                or file_path in self._thumbnail_jobs_inflight):
            return
        getattr(self, '_deferred_thumbnail_paths', set()).discard(file_path)
        try:
            fingerprint = self._indexed_record(file_path).fingerprint
        except AttributeError:
            fingerprint = ''
        cached = self._recent_thumbnails.get(file_path)
        if cached is not None:
            if cached[0] == fingerprint:
                self._recent_thumbnails.move_to_end(file_path)
                _, card._thumb_pixmap, duration, text, tooltip = cached
                card._thumbnail_ready = True
                card._thumbnail_fingerprint = fingerprint
                if card.is_video:
                    card.set_source_duration(duration)
                card.metadata_label.setText(text)
                card.metadata_label.setToolTip(tooltip)
                card._render_thumbnail()
                return
            self._recent_thumbnails.pop(file_path, None)
        if getattr(self, '_scrolling', False):
            self._deferred_thumbnail_paths.add(file_path)
            return
        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
            self._deferred_thumbnail_paths.add(file_path)
            return
        generation = self._thumbnail_generation
        if card.is_video:
            worker = _ThumbnailWorker(file_path, _WorkerCancellation(self._thumbnail_cancel_event))
            worker.signals.finished.connect(
                lambda path, cache, duration, expected=fingerprint, generation=generation:
                    self._on_thumb_ready(
                        path, cache, duration, expected, generation))
        else:
            worker = _ImageThumbnailWorker(
                file_path, _WorkerCancellation(self._thumbnail_cancel_event))
            worker.signals.finished.connect(
                lambda path, image, expected=fingerprint, generation=generation:
                    self._on_image_thumb_ready(
                        path, image, expected, generation))
        worker.signals.diagnostic_finished.connect(
            lambda elapsed_ms, metadata_ms, cache_hit,
                   path=file_path, generation=generation, expected=fingerprint:
                self._on_thumb_diagnostic(
                    elapsed_ms, metadata_ms, cache_hit,
                    path, generation, expected))
        self._thumbnail_jobs_inflight.add(file_path)
        self._thumbnail_workers[file_path] = worker
        self._thumbnail_job_owners[file_path] = (generation, fingerprint)
        self._thumbnail_queue_peak = max(
            self._thumbnail_queue_peak, len(self._thumbnail_jobs_inflight))
        self._thumbnail_job_fingerprints[file_path] = fingerprint
        self._thumbnail_jobs_submitted += 1
        self._thread_pool.start(worker)

    def set_interface_card_designer(self, designer):
        self._interface_card_designer = designer
        self._current_columns, self._current_card_widths = self._active_layout_metrics(
            self._viewport_width())
        for card in self._thumb_widgets.values():
            previous = card._interface_card_designer
            if previous is not None:
                previous.restore_card(card)
            card._interface_card_designer = designer
            card.resize_card(card._card_width, force=True)
            if previous is not None and previous.cards_hidden() and designer is None:
                card.show()
        self._relayout_grids()

    def _create_thumbnail(
            self, file_path: str, card_width: int,
            *, ready_override: bool | None = None, parent=None) -> ClipThumbnail:
        is_video = is_completed_video_path(file_path)
        uploaded = bool(
            self._upload_checker and self._upload_checker(file_path))
        upload_info = (
            self._upload_info_checker(file_path)
            if uploaded and self._upload_info_checker else None)
        ready = bool(
            ready_override
            if ready_override is not None
            else (self._readiness_checker(file_path)
                  if self._readiness_checker else True))
        options = dict(
            imported=file_path in self._imported_files,
            upload_enabled=bool(self._upload_enabled and self._upload_enabled()),
            uploaded=uploaded, upload_info=upload_info, ready=ready,
            card_width=card_width)
        pool = self._recycled_cards
        match = next((index for index, card in enumerate(pool)
                      if card.is_video == is_video), None)
        if match is not None:
            thumb = pool.pop(match)
            if thumb.parentWidget() is not (parent or self._sections_host):
                thumb.setParent(parent or self._sections_host)
            thumb.rebind(file_path, **options)
        else:
            if pool and len(self._thumb_widgets) + len(pool) >= _MAX_MATERIALIZED_CARDS:
                pool.pop().deleteLater()
            thumb = ClipThumbnail(
                file_path, is_video=is_video, **options,
                clips_root=self.clips_dir, defer_image_load=not is_video,
                parent=parent or self._sections_host, metadata_manager=self._metadata_manager)
            thumb.library_flags_changed.connect(self._on_library_flags_changed)
            thumb.opened.connect(self.clip_opened.emit)
            thumb.preload_requested.connect(self.clip_preload_requested.emit)
            thumb.overlap_requested.connect(self._request_overlap_merge)
            thumb.clicked.connect(self.clip_clicked.emit if is_video else self.screenshot_clicked.emit)
            thumb._prepare_mutation = self._prepare_card_mutation
            thumb._finish_mutation = self._finish_card_mutation
            thumb._wait_for_readers = self._release_thumbnail_reader
            thumb.renamed.connect(self._on_clip_renamed)
            thumb.deleted.connect(self._on_clip_deleted)
            thumb.upload_requested.connect(self.clip_upload_requested.emit)
        thumb._trash_handler = self._dispatch_trash if self._trash_handler else None
        thumb._selection_handler = self._select_card
        thumb._album_drag_handler = self._start_album_drag
        thumb._album_menu_provider = self._album_menu_state
        thumb._album_record_provider = self._album_record_snapshot
        thumb._album_membership_handler = self._dispatch_single_album
        thumb._tag_details_provider = self.library_details_for
        thumb._tag_edit_handler = self._edit_card_tags
        thumb._cover_handler = self._open_cover_dialog
        thumb._clip_actions_provider = self._clip_actions_provider
        thumb.set_overlap_paths(self._overlaps.get(file_path, ()))
        self._apply_cover_to_card(thumb)
        record = self._record_for_path(file_path)
        selection_visible = self._selection_mode or self._bulk_job is not None
        thumb.set_selection_state(
                                  selection_visible and record is not None
                                  and self._selection.contains(record),
                                  selection_visible,
                                  locked=self._bulk_job is not None)
        thumb._rename_handler = self._dispatch_rename if self._rename_handler else None
        thumb.set_upload_status(self._upload_statuses.get(file_path, ''))
        thumb._duration_provider = self._display_duration
        self.thumbnails.append(thumb)
        self._thumb_widgets[file_path] = thumb
        thumb._interface_card_designer = self._interface_card_designer
        if self._interface_card_designer is not None:
            thumb.resize_card(thumb._card_width, force=True)
        return thumb

    def set_upload_status(self, path, message, *, feedback=''):
        if message:
            self._upload_statuses[path] = message
        else:
            self._upload_statuses.pop(path, None)
        card = self._thumb_widgets.get(path)
        if card is not None:
            card.set_upload_status(message, feedback=feedback)

    def _add_section(
            self, header_text: str, files: list[str], starting_idx: int,
            *, insert_at: int | None = None,
            ready_overrides: dict[str, bool] | None = None):
        """Build a date header with a small grid or a virtual canvas.

        Above the card budget, store geometry and position only visible cards
        through _layout_virtual_section instead of allocating every layout item.
        """
        section = QFrame()
        section.setStyleSheet('background: transparent;')
        sl = QVBoxLayout(section)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)

        # Header row: date + dropdown caret (visual)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(10)

        date_lbl = QLabel(header_text)
        date_lbl.setProperty('class', 'sectionHeader')
        set_theme_style(date_lbl, lambda: (f'color: {Colors.TEXT};'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' font-size: {Fonts.SIZE_BODY_L}px;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'))
        hdr_row.addWidget(date_lbl)

        # Game/source name comes from the most-recent file's parent folder
        sample_game = (
            _game_name_from_path(files[0], self.clips_dir) if files else '')
        if sample_game:
            sub_lbl = QLabel(f'·  {sample_game}')
            set_theme_style(sub_lbl, lambda: (f'color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY};'
                f' font-size: {Fonts.SIZE_BODY}px; background: transparent;'))
            hdr_row.addWidget(sub_lbl)

        hdr_row.addStretch(1)
        sl.addLayout(hdr_row)

        self._section_files[header_text] = list(files)
        self._section_widgets[header_text] = section
        for fp, value in (ready_overrides or {}).items():
            self._virtual_ready[fp] = bool(value)

        # Do not create a partially materialized grid with one spacer per
        # undisplayed file.  That old placeholder model was still O(total
        # files) in both QObject and QLayoutItem count.
        use_virtual = (
            self._global_virtualization_required()
            or len(files) > _MAX_MATERIALIZED_CARDS
            or len(self._thumb_widgets) + len(files)
                > _MAX_MATERIALIZED_CARDS)
        if use_virtual:
            canvas = QWidget(section)
            canvas.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            canvas.setMinimumWidth(1)
            self._virtual_sections[header_text] = canvas
            sl.addWidget(canvas)
            self._set_virtual_canvas_height(header_text)
        else:
            cols = self._current_columns
            widths = self._current_card_widths
            grid = QGridLayout()
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            grid.setHorizontalSpacing(self._interface_grid_spacing())
            grid.setVerticalSpacing(self._interface_grid_spacing())
            grid.setContentsMargins(0, 0, 0, 0)
            for i, fp in enumerate(files):
                ready_override = self._virtual_ready.get(fp)
                row, col = divmod(i, cols)
                thumb = self._create_thumbnail(
                    fp, widths[col], ready_override=ready_override)
                grid.addWidget(
                    thumb, row, col,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                )
                self._start_thumbnail_worker(fp)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            sl.addLayout(grid)
            self._section_grids[header_text] = grid
        if insert_at is None:
            self._sections_layout.addWidget(section)
        else:
            self._sections_layout.insertWidget(insert_at, section)

    def _scroll_viewport(self):
        parent = self.parentWidget()
        for _ in range(5):
            if parent is None:
                return None
            if (hasattr(parent, 'verticalScrollBar')
                    and callable(parent.verticalScrollBar)):
                return parent.viewport()
            parent = parent.parentWidget()
        return None

    def navigation_paths(self, *, videos_only=False):
        """Return the displayed order, including unmaterialized library cards."""
        paths = []
        keys = {widget: key for key, widget in self._section_widgets.items()}
        for index in range(self._sections_layout.count()):
            key = keys.get(self._sections_layout.itemAt(index).widget())
            paths.extend(self._section_files.get(key, ()))
        if videos_only:
            paths = [path for path in paths
                     if Path(path).suffix.casefold() in _VIDEO_EXTS
                     and (self._readiness_checker(path)
                          if self._readiness_checker else self._virtual_ready.get(path, True))
                     and path not in self._mutation_paths]
        return paths

    def adjacent_clip(self, path, step):
        paths = self.navigation_paths()
        if path not in paths:
            return None
        index = paths.index(path) + step
        # Check only candidate files. Stating every file in a large or linked
        # library on each arrow press would block the GUI on filesystem I/O.
        while 0 <= index < len(paths):
            candidate = paths[index]
            ready = (self._readiness_checker(candidate) if self._readiness_checker
                     else self._virtual_ready.get(candidate, True))
            if (Path(candidate).suffix.casefold() in _VIDEO_EXTS
                    and ready and candidate not in self._mutation_paths
                    and os.path.isfile(candidate)):
                return candidate
            index += step
        return None

    def _set_virtual_canvas_height(self, section_key: str) -> None:
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return
        cols = max(1, self._current_columns)
        rows = (len(files) + cols - 1) // cols
        height = max(0, rows * self._virtual_row_height()
                     - (self._interface_grid_spacing() if rows else 0))
        if canvas.height() != height or canvas.minimumHeight() != height:
            canvas.setFixedHeight(height)

    def _virtual_card_budget(self, viewport=None) -> int | None:
        """Estimate visible plus overscan cards for a real scroll viewport."""
        if viewport is None:
            viewport = self._scroll_viewport()
        if viewport is None:
            return None
        try:
            height = int(viewport.height())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # Detached test hosts can expose a transiently invalid viewport.
            return None
        if height <= 0:
            return None
        row_height = max(1, self._virtual_row_height())
        visible_rows = max(1, (height + row_height - 1) // row_height)
        rows = visible_rows + (_VIRTUAL_OVERSCAN_ROWS * 2) + 1
        return min(_MAX_MATERIALIZED_CARDS, max(1, rows * self._current_columns))

    def _global_virtualization_required(self, viewport=None) -> bool:
        """Return whether the current index needs a virtual canvas globally."""
        total = len(self._records)
        if total > _MAX_MATERIALIZED_CARDS:
            return True
        budget = self._virtual_card_budget(viewport)
        return budget is not None and total > budget

    def _virtual_row_height(self) -> int:
        width = max(self._current_card_widths, default=_CARD_W)
        return (ClipThumbnail.card_height(width, self._interface_card_designer)
                + self._interface_grid_spacing())

    def _release_virtual_card(self, path: str) -> None:
        card = self._thumb_widgets.get(path)
        if card is not None and (card._mutation_busy or card._editing_name):
            return
        for popup in (QApplication.activeModalWidget(), QApplication.activePopupWidget()):
            if card is not None and popup is not None and card.isAncestorOf(popup):
                return
        record = self._record_for_path(path)
        self._cancel_cover_decode_for_record(record)
        self._deferred_thumbnail_paths.discard(path)
        card = self._thumb_widgets.pop(path, None)
        self._card_virtual_sections.pop(path, None)
        if card is None:
            return
        worker = self._thumbnail_workers.get(path)
        if worker is not None:
            # Cancel this file only. Its terminal callback still owns cleanup;
            # clearing the thread pool would strand queued workers' tokens.
            worker.cancel_event.set()
        if card._thumbnail_ready:
            self._recent_thumbnails[path] = (
                card._thumbnail_fingerprint,
                card._thumb_pixmap,
                card._source_duration if card.is_video else 0.0,
                card.metadata_label.text(), card.metadata_label.toolTip())
            self._recent_thumbnails.move_to_end(path)
            while len(self._recent_thumbnails) > _MAX_MATERIALIZED_CARDS:
                self._recent_thumbnails.popitem(last=False)
        try:
            self.thumbnails.remove(card)
        except ValueError:
            pass
        if card._interface_card_designer is not None:
            card._interface_card_designer.restore_card(card)
            card._interface_card_designer = None
        card.hide()
        card._feedback_timer.stop()
        card._thumb_pixmap = QPixmap()
        card.thumb_label.clear()
        # Discard excess spare controls even while scrolling a large library.
        if len(self._recycled_cards) < max(4, self._current_columns * 2):
            self._recycled_cards.append(card)
        else:
            card.deleteLater()

    def _convert_section_to_virtual(self, section_key: str) -> None:
        """Replace a small grid before it can grow an unbounded placeholder list."""
        grid = self._section_grids.get(section_key)
        section = self._section_widgets.get(section_key)
        if grid is None or section is None:
            return
        section_layout = section.layout()
        if section_layout is None:
            return
        grid_index = next(
            (index for index in range(section_layout.count())
             if section_layout.itemAt(index).layout() is grid), None)
        if grid_index is None:
            return
        section_layout.takeAt(grid_index)
        # Keep already-created cards alive when a resize changes an eager
        # section into a virtual one.  They can own an active rename editor,
        # a mutation token, or a popup; deleting/reparenting them here loses
        # that UI state.  The cards remain children of their section and are
        # positioned against their actual parent by the next materialization
        # pass.
        for path in self._section_files.get(section_key, ()):
            card = self._thumb_widgets.get(path)
            if card is not None:
                self._card_virtual_sections[path] = section_key
        while grid.count():
            grid.takeAt(0)
        self._section_grids.pop(section_key, None)
        canvas = QWidget(section)
        canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._virtual_sections[section_key] = canvas
        section_layout.insertWidget(grid_index, canvas)
        # The canvas is a new sibling and therefore initially above the
        # adopted cards in the child stacking order. Raise the cards only
        # after it exists so their hit targets remain usable.
        for path in self._section_files.get(section_key, ()):
            card = self._thumb_widgets.get(path)
            if card is not None:
                card.raise_()
        self._set_virtual_canvas_height(section_key)
        # Defer card creation to the shared bounded timer. Converting a
        # section during an upsert must not synchronously rebuild its viewport.
        self._request_virtual_update()

    def _virtual_section_plan(
            self, section_key: str, viewport, *, include_overscan=True):
        """Calculate visibility once; card coordinates live in the stable host."""
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return []
        self._set_virtual_canvas_height(section_key)
        cols = max(1, self._current_columns)
        row_height = self._virtual_row_height()
        rows = (len(files) + cols - 1) // cols
        if viewport is None:
            row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        else:
            try:
                top = canvas.mapTo(viewport, QPoint(0, 0)).y()
                overscan = _VIRTUAL_OVERSCAN_ROWS if include_overscan else 0
                row_start = max(0, int((-top) // row_height) - overscan)
                row_end = min(
                    rows,
                    int((viewport.height() - top) // row_height)
                    + overscan + 1,
                )
            except (RuntimeError, AttributeError):
                row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        wanted: list[tuple[str, int, int]] = []
        for row in range(row_start, row_end):
            for col in range(cols):
                index = row * cols + col
                if index >= len(files):
                    break
                wanted.append((files[index], row, col))
        # A very tall viewport must still respect the global live-card bound.
        return wanted[:_MAX_MATERIALIZED_CARDS]

    def _interface_cards_hidden(self):
        designer = self._interface_card_designer
        return designer is not None and designer.cards_hidden()

    def _layout_virtual_section(self, section_key: str, *, release_only=False) -> None:
        """Refresh one changed section outside the batched scrolling path."""
        wanted = self._virtual_section_plan(section_key, self._scroll_viewport())
        wanted_paths = {path for path, _, _ in wanted}
        for path, owner in list(self._card_virtual_sections.items()):
            if owner == section_key and path not in wanted_paths:
                self._release_virtual_card(path)
        if release_only:
            return
        self._materialize_virtual_section(section_key, wanted)

    def _materialize_virtual_section(
            self, section_key: str, wanted, *, deadline=None,
            position_existing=True, create_missing=True,
            allow_progress=False) -> bool:
        if not wanted:
            return True
        canvas = self._virtual_sections[section_key]
        widths = self._current_card_widths
        row_height = self._virtual_row_height()
        created_paths = set()
        for path, row, col in wanted:
            card = self._thumb_widgets.get(path)
            if card is None:
                if not create_missing:
                    continue
                if len(self._thumb_widgets) >= _MAX_MATERIALIZED_CARDS:
                    break
                if deadline is not None and time.perf_counter() >= deadline:
                    # A non-scrolling initial tick must still make progress
                    # when the previous card consumed the whole budget. During
                    # scrolling an expired tick may yield zero as before.
                    if not allow_progress or created_paths:
                        return False
                card = self._create_thumbnail(
                    path, widths[col],
                    ready_override=self._virtual_ready.get(path),
                    parent=self._sections_host)
                self._card_virtual_sections[path] = section_key
                self._start_thumbnail_worker(path)
                created_paths.add(path)
            elif self._card_virtual_sections.get(path) != section_key:
                self._card_virtual_sections[path] = section_key
            card_parent = card.parentWidget() or self._sections_host
            origin = canvas.mapTo(card_parent, QPoint(0, 0))
            column_offset = origin.x() + sum(
                widths[:col]) + col * self._interface_grid_spacing()
            if not position_existing:
                if path not in created_paths:
                    continue
                # Give newly-created controls usable geometry immediately;
                # existing-card geometry is handled only after all sections
                # have had a chance to create missing visible cards.
                card.resize_card(widths[col])
                geometry = QRect(
                    column_offset,
                    origin.y() + row * row_height,
                    widths[col], card.height())
                if card.geometry() != geometry:
                    card.setGeometry(geometry)
                if card.isHidden():
                    card.setVisible(not self._interface_cards_hidden())
                    card.raise_()
                continue
            # Keep the widget tree under one parent across date boundaries.
            # Reparenting every child causes stylesheet/layout work on Windows.
            card.resize_card(widths[col])
            geometry = QRect(
                column_offset,
                origin.y() + row * row_height,
                widths[col], card.height())
            if card.geometry() != geometry:
                card.setGeometry(geometry)
            if card.isHidden():
                card.setVisible(not self._interface_cards_hidden())
                card.raise_()
        return True

    def _request_virtual_update(self, *_args) -> None:
        if (not self._shutdown_started and not self._background_paused
                and not self._virtual_update_timer.isActive()):
            self._virtual_update_timer.start()

    def _update_virtualized_cards(self, *_args) -> None:
        """Materialize only visible/overscan records and release distant cards."""
        if self._shutdown_started or self._background_paused:
            return
        viewport = self._scroll_viewport()
        # A scan can finish before the scroll area is laid out. Re-evaluate
        # the global threshold once a real viewport exists so a previously
        # eager multi-section index is converted before the next frame.
        if self._global_virtualization_required(viewport):
            for key in tuple(self._section_grids):
                self._convert_section_to_virtual(key)
        for key in tuple(self._virtual_sections):
            self._set_virtual_canvas_height(key)
        sections_layout = self._sections_host.layout()
        if sections_layout is not None:
            sections_layout.activate()
        plans = {
            key: self._virtual_section_plan(key, viewport)
            for key in tuple(self._virtual_sections)
        }
        visible_plans = {
            key: self._virtual_section_plan(
                key, viewport, include_overscan=False)
            for key in tuple(self._virtual_sections)
        }
        # Build one global target list.  Visible cards from every section have
        # priority; overscan fills the remaining live-card budget.  Keeping
        # the old per-section union here would allow 96 stale overscan cards
        # to occupy the budget before a newly visible section is considered.
        work = []
        seen = set()
        for section_key, wanted in visible_plans.items():
            for item in wanted:
                if item[0] not in seen:
                    work.append((section_key, item))
                    seen.add(item[0])
        for section_key, wanted in plans.items():
            for item in wanted:
                if item[0] not in seen:
                    work.append((section_key, item))
                    seen.add(item[0])
        work = work[:_MAX_MATERIALIZED_CARDS]
        target_paths = {item[1][0] for item in work}
        geometry_work = {}
        for section_key, item in work:
            geometry_work.setdefault(section_key, []).append(item)
        # Release globally before allocating, including when returning to newer
        # dates. Do not scan the full card registry once for every date section.
        for path in tuple(self._card_virtual_sections):
            if path not in target_paths:
                self._release_virtual_card(path)
        # Creating a Qt card polishes its whole child tree. During a fast wheel
        # burst, yield after a small budget rather than creating a row at once.
        # Recalculate visibility on the next frame, so obsolete work is skipped.
        deadline = time.perf_counter() + .004
        # Creation is globally budgeted before existing-card geometry work.
        # A large earlier section must not consume the tick while a newly
        # visible section waits for its first card.
        allow_progress = not self._scrolling
        for section_key, item in work:
            before_count = len(self._thumb_widgets)
            if not self._materialize_virtual_section(
                    section_key, (item,), deadline=deadline,
                    position_existing=False,
                    allow_progress=allow_progress):
                self._request_virtual_update()
                break
            if len(self._thumb_widgets) > before_count:
                # Only the first expired non-scrolling item gets the explicit
                # progress exception; subsequent items remain 4 ms-budgeted.
                allow_progress = False
        # Recompute geometry after the creation pass even when it yielded.
        # Creating a card can activate section layouts and move later canvases;
        # postponing this pass would leave a card one header-height too high.
        for section_key, wanted in geometry_work.items():
            self._materialize_virtual_section(
                section_key, wanted, create_missing=False)
        missing = any(path not in self._thumb_widgets for path in target_paths)
        complete = not missing or len(self._thumb_widgets) >= _MAX_MATERIALIZED_CARDS
        self._virtual_initialized = complete
        if not complete:
            self._request_virtual_update()

    def _insert_saved_path(self, files: list[str], file_path: str) -> None:
        """Insert after equal keys using scanner metadata, without filesystem I/O."""
        if file_path in files:
            return
        longest = self._sort == 'longest'
        direction = 1 if self._sort == 'oldest' else -1
        # Use raw paths: canonicalizing each path can itself touch the disk.
        values = {
            record.path: direction * (record.size if longest else record.mtime_ns)
            for record in self._records.values()
        }
        # Match the previous sort's zero fallback for unavailable metadata.
        key = lambda path: values.get(path, 0)
        files.insert(bisect_right(files, key(file_path), key=key), file_path)

    def _query_requires_rebuild(self) -> bool:
        """Return whether incremental saved-clip insertion could skip a predicate."""
        criteria = getattr(self, '_search_criteria', SearchCriteria())
        return bool(
            getattr(self, '_search_unavailable_reason', None)
            or getattr(self, '_filter', 'all') != 'all'
            or getattr(self, '_game_filter', None) is not None
            or criteria.text
            or criteria.tags_all
            or criteria.album_id
            or criteria.kind != 'all'
            or criteria.favorites_only
            or criteria.sort != 'newest'
        )

    def _query_uses_metadata(self) -> bool:
        """Return whether a metadata/favorite change can alter visibility."""
        criteria = getattr(self, '_search_criteria', SearchCriteria())
        return bool(
            getattr(self, '_filter', 'all') == 'favorites'
            or criteria.text
            or criteria.tags_all
            or criteria.favorites_only)

    def _mark_upsert_cache_dirty(self):
        self._library_details_snapshot_dirty = True
        self._query_snapshot_dirty = True
        self._favorite_snapshot_dirty = True
        self._album_snapshot_dirty = True

    def _refresh_saved_record(self, file_path):
        """Refresh one save boundary's complete record evidence."""
        try:
            info = os.stat(file_path)
        except OSError:
            return None, False
        identity = canonical_media_path(file_path)
        existing = self._records.get(identity)
        if existing is None:
            existing = next(
                (candidate for candidate in self._records.values()
                 if candidate.path == file_path),
                None,
            )
        if existing is None:
            record = LibraryRecord(
                file_path, identity, (canonical_media_path(self.clips_dir),),
                file_path in self._imported_files, 'video', int(info.st_size),
                int(info.st_mtime_ns), int(info.st_dev), int(info.st_ino))
            is_new_record = True
        else:
            # A saved clip may replace a discovered record at the same path.
            # Preserve its provenance/category projection while refreshing
            # every filesystem identity field from this save boundary.
            from dataclasses import replace
            record = replace(
                existing, path=file_path, identity=identity,
                size=int(info.st_size), mtime_ns=int(info.st_mtime_ns),
                st_dev=int(info.st_dev), st_ino=int(info.st_ino))
            is_new_record = False
        for old_identity, old_record in tuple(self._records.items()):
            if (old_record.path == file_path and old_identity != identity):
                self._records.pop(old_identity, None)
        for old_identity, old_record in tuple(self._scan_upserts.items()):
            if (old_record.path == file_path and old_identity != identity):
                self._scan_upserts.pop(old_identity, None)
        self._records[identity] = record
        self._records_by_path[record.path] = record
        self._overlap_discovery.update(self._records.values())
        self._mark_upsert_cache_dirty()
        if self._active_scan_worker is not None:
            self._scan_upserts[identity] = record
        return record, is_new_record

    def _publish_query_upsert(self) -> bool:
        """Publish an upsert through the same cached predicate as a scan."""
        if not self._query_requires_rebuild():
            return False
        self._library_details_snapshot_dirty = True
        self._favorite_snapshot_dirty = True
        self._query_snapshot_dirty = True
        self._details_rebuild_pending = True
        self._apply_current_index()
        if self._details_rebuild_pending:
            # A concurrent file mutation defers the full card rebuild. Keep
            # already-published navigation sections from exposing a replaced
            # record that the defensive cache no longer accepts; the guarded
            # Apply will republish cards when the mutation is released.
            visible = {
                record.path for record in self._filtered_records(ordered=False)
            }
            for section_key, paths in tuple(self._section_files.items()):
                self._section_files[section_key] = [
                    path for path in paths if path in visible
                ]
        return True

    def upsert_saved_clip(self, file_path: str, *, ready: bool) -> None:
        """Insert or finalize a known saved path without rescanning the library.

        Update only its date section so save work stays independent of library size.
        """
        if getattr(self, '_scan_suspended', False):
            self._background_refresh_pending = True
            if SAVE_TRACE_ENABLED:
                mark_save_trace('ui_library_upsert_deferred', reason='scan_suspended')
            return
        update_started = time.monotonic()
        file_path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
        if canonical_media_path(file_path) in getattr(self, '_active_output_paths', ()):
            return
        if not is_completed_video_path(file_path):
            return

        if self._background_paused:
            self._pending_saved_clips[file_path] = (
                bool(ready)
                or self._pending_saved_clips.get(file_path, False))
            if self._background_paused:
                self._background_refresh_pending = True
            if SAVE_TRACE_ENABLED:
                mark_save_trace('ui_library_upsert_deferred', reason='background_paused')
            return

        if self._known_files is None:
            self._known_files = set(self._thumb_widgets)
        self._known_files.add(file_path)

        existing = self._thumb_widgets.get(file_path)
        previous_record = self._records.get(canonical_media_path(file_path))
        previous_identity = (dict(previous_record.file_identity)
                             if previous_record is not None
                             and previous_record.file_identity is not None else None)
        record, is_new_record = self._refresh_saved_record(file_path)
        if existing is not None:
            if (record is not None and previous_identity is not None
                    and record.file_identity != previous_identity):
                if previous_record is not None:
                    self._cancel_cover_decode_for_record(previous_record)
                    self._cover_bindings_snapshot.pop(previous_record.identity, None)
                self._cover_bindings_snapshot.pop(record.identity, None)
                self._cover_committed_overrides.pop(file_path, None)
                self._cover_decode_failed.clear()
                existing.clear_custom_cover()
                existing._render_thumbnail()
            self._virtual_ready[file_path] = bool(ready)
            existing.set_ready(ready)
            if ready:
                self._start_thumbnail_worker(file_path)
            self._publish_query_upsert()
            emit_event(
                'library', 'saved_clip_upserted', state='UPDATED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        # A virtualized record may already be indexed without having a live
        # card. Do not create a second path entry for a CLIP_SAVED race.
        identity = record.identity if record is not None else canonical_media_path(file_path)
        if record is not None and not is_new_record:
            self._virtual_ready[file_path] = bool(ready)
            self._publish_query_upsert()
            emit_event(
                'library', 'saved_clip_upserted', state='INDEXED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round((time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            self._update_virtualized_cards()
            return

        section_key = _section_label_for(record.mtime) if record is not None else 'OTHER'

        if identity in self._records and self._publish_query_upsert():
            emit_event(
                'library', 'saved_clip_upserted', state='ADDED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round((time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        # A locally saved video is not visible in screenshot/imported-only
        # views.  It is still added to _known_files so the debounced filesystem
        # watcher sees no unexplained change and does not trigger a rebuild.
        if self._album_id is not None or self._filter not in {'all', 'clips'}:
            emit_event(
                'library', 'saved_clip_upserted', state='FILTERED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        if self._global_virtualization_required():
            for key in tuple(self._section_grids):
                self._convert_section_to_virtual(key)

        grid = self._section_grids.get(section_key)
        virtual_canvas = self._virtual_sections.get(section_key)
        if grid is None and virtual_canvas is not None:
            files = self._section_files.setdefault(section_key, [])
            self._insert_saved_path(files, file_path)
            self._virtual_ready[file_path] = bool(ready)
            self._set_virtual_canvas_height(section_key)
            self._request_virtual_update()
            self._show_empty(False)
            self._all_count_label.setText(
                f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
            emit_event(
                'library', 'saved_clip_upserted', state='ADDED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return
        if grid is None:
            insert_at = 0 if self._sort != 'oldest' else None
            self._add_section(
                section_key, [file_path], 0, insert_at=insert_at,
                ready_overrides={file_path: bool(ready)})
            if section_key in self._virtual_sections:
                self._request_virtual_update()
        else:
            files = self._section_files.setdefault(section_key, [])
            self._insert_saved_path(files, file_path)
            self._virtual_ready[file_path] = bool(ready)
            # Convert before the section can acquire placeholder items beyond
            # the global live-card budget. The virtual canvas has O(visible)
            # children regardless of how many records the section contains.
            if (len(files) > _MAX_MATERIALIZED_CARDS
                    or len(self._thumb_widgets) > _MAX_MATERIALIZED_CARDS):
                self._convert_section_to_virtual(section_key)
                self._set_virtual_canvas_height(section_key)
                self._show_empty(False)
                self._all_count_label.setText(
                    f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
                emit_event(
                    'library', 'saved_clip_upserted', state='ADDED',
                    ready=bool(ready), full_scan=False,
                    elapsed_ms=round(
                        (time.monotonic() - update_started) * 1000),
                    visible_card_count=len(self._thumb_widgets))
                return
            # Reflow this small section only; no spacer is needed because this
            # branch is guaranteed to remain within the live-card bound.
            while grid.count():
                grid.takeAt(0)
            cols = self._current_columns
            widths = self._current_card_widths
            for index, path in enumerate(files):
                row, col = divmod(index, cols)
                card = self._thumb_widgets.get(path)
                if card is None and len(self._thumb_widgets) < _MAX_MATERIALIZED_CARDS:
                    card = self._create_thumbnail(
                        path, widths[col],
                        ready_override=self._virtual_ready.get(path))
                if card is not None and index < _MAX_MATERIALIZED_CARDS:
                    card.resize_card(widths[col])
                    grid.addWidget(
                        card, row, col,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            if file_path in self._thumb_widgets:
                self._start_thumbnail_worker(file_path)
            self._request_virtual_update()

        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
        emit_event(
            'library', 'saved_clip_upserted', state='ADDED',
            ready=bool(ready), full_scan=False,
            elapsed_ms=round((time.monotonic() - update_started) * 1000),
            visible_card_count=len(self._thumb_widgets))

    def _relayout_grids(self):
        """Reposition existing cards for the new column count.

        Reusing widgets avoids flicker and restarting thumbnail workers on resize.
        """
        cols = self._current_columns
        widths = self._current_card_widths
        for si in range(self._sections_layout.count()):
            section_widget = self._sections_layout.itemAt(si).widget()
            if section_widget is None:
                continue
            section_layout = section_widget.layout()
            if section_layout is None:
                continue
            for li in range(section_layout.count()):
                item = section_layout.itemAt(li)
                if item is None:
                    continue
                grid = item.layout()
                if not isinstance(grid, QGridLayout):
                    continue
                grid.setHorizontalSpacing(self._interface_grid_spacing())
                grid.setVerticalSpacing(self._interface_grid_spacing())
                widgets = []
                for col in range(grid.columnCount()):
                    grid.setColumnMinimumWidth(col, 0)
                    grid.setColumnStretch(col, 0)
                grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                while grid.count():
                    child = grid.takeAt(0)
                    w = child.widget()
                    if w:
                        widgets.append(w)
                for i, w in enumerate(widgets):
                    if isinstance(w, ClipThumbnail):
                        w.resize_card(widths[i % cols])
                    grid.addWidget(
                        w, i // cols, i % cols,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    )
                for col, width in enumerate(widths):
                    grid.setColumnMinimumWidth(col, width)
        for section_key in tuple(self._virtual_sections):
            self._set_virtual_canvas_height(section_key)
        self._request_virtual_update()

    def set_clips_directory(self, path: str, *, prepared: bool = False) -> None:
        """Switch the primary library root and refresh the visible media."""
        if prepared:
            try:
                raw_path = os.fspath(path)
                if (not isinstance(raw_path, str) or not raw_path.strip()
                        or '\x00' in raw_path or not Path(raw_path).is_absolute()):
                    raise StoragePathError(
                        'The prepared clips storage folder must be a non-empty '
                        'absolute path without NUL characters.')
                # Settings already created, write-probed, and persisted this
                # exact path. Do not probe it again before adopting the saved
                # root; _load_clips reports if it became unavailable meanwhile.
                resolved = raw_path
            except (OSError, TypeError, ValueError) as error:
                storage_error = (error if isinstance(error, StoragePathError)
                                 else StoragePathError(
                                     'The prepared clips storage folder is invalid.'))
                self._report_storage_unavailable(storage_error)
                return
        else:
            try:
                resolved = str(ensure_storage_directory(
                    path, probe_writable=True))
            except StoragePathError as error:
                self._report_storage_unavailable(error)
                return
        if resolved == self.clips_dir:
            self.force_refresh()
            return

        # The prepared settings handoff must not perform a second filesystem
        # resolution before adopting its already-persisted absolute root.
        old_root = (Path(self.clips_dir) if prepared else
                    Path(self.clips_dir).resolve(strict=False))
        for watched in list(self._watcher.directories()):
            watched_path = Path(watched)
            if not prepared:
                watched_path = watched_path.resolve(strict=False)
            try:
                watched_path.relative_to(old_root)
            except ValueError:
                continue
            self._watcher.removePath(watched)

        self.clips_dir = resolved
        self._storage_error_reported = False
        self._known_files = set()
        self._imported_files = set()
        if (self._scanning_started and not getattr(self, '_scan_suspended', False)
                and resolved not in self._watcher.directories()):
            self._watcher.addPath(resolved)
        self._load_clips()

    def _display_duration(self, path, source_duration):
        if (self._sm and self._sm.get('grid_show_edited_duration', False)
                and self._metadata_manager is not None):
            return self._metadata_manager.edited_duration(path, source_duration)
        return source_duration

    def refresh_duration_labels(self):
        for card in self._thumb_widgets.values():
            card.refresh_duration_label()

    def track_import_folder(self, folder):
        if not self._pending_import_roots:
            self._import_baseline = {record.identity for record in self._records.values()
                                     if record.imported}
        self._pending_import_roots.add(canonical_media_path(folder))
        self.show_import_feedback(folder, 'Indexing recording folder…', False)

    def show_import_feedback(self, folder, message, can_view):
        self._import_status.setText(f'{Path(folder).name or folder} · {message}')
        self._import_status.setVisible(self._filter == 'imported')
        self.import_feedback.emit(folder, message, can_view)

    def _finish_import_feedback(self, records):
        if not self._pending_import_roots:
            return
        for folder in self._sm.get('imported_clip_folders', []):
            root = canonical_media_path(folder)
            if root not in self._pending_import_roots:
                continue
            videos = [r for r in records if r.imported and r.kind == 'video' and root in r.root_ids]
            count = sum(r.identity not in self._import_baseline for r in videos)
            if count:
                message = f'{count} new imported clip{"s" if count != 1 else ""}.'
            elif videos:
                message = 'These clips are already in the library.'
            elif not os.path.isdir(folder):
                message = 'Folder unavailable. Locate it in Manage folders.'
            else:
                formats = ', '.join(suffix[1:].upper() for suffix in VIDEO_SUFFIXES)
                message = f'No supported videos found. Supported formats include {formats}.'
            self.show_import_feedback(folder, message, bool(videos))
        self._pending_import_roots.clear()
        self._import_baseline.clear()

    def force_refresh(self):
        """Clear the file cache and immediately reload — called when import folders change."""
        if self._background_paused:
            self._background_refresh_pending = True
            if self._pending_import_roots and not self._mutation_paths:
                self._begin_scan('import_feedback')
            return
        self._known_files = set()
        self._load_clips()

    def refresh_after_backup_restore(self):
        """Refresh restored catalog state while retaining the current query."""
        if self._shutdown_started:
            return
        if self._background_paused:
            self._background_refresh_pending = True
            self._backup_restore_refresh_pending = True
            return
        self._refresh_album_catalog()
        self.refresh_saved_searches()
        self.refresh_library_details()
        self.force_refresh()

    def shutdown(self) -> None:
        """Stop library timers/workers within a bounded grace period."""
        self.cancel_bulk_flags()
        self._bulk_timer.stop()
        self._bulk_job = None
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._overlap_discovery.shutdown()
        self._recent_thumbnails.clear()
        for timer_name in ('refresh_timer', '_debounce_timer', '_search_debounce_timer',
                           '_resize_timer', '_idle_memory_timer',
                           '_virtual_update_timer', '_scroll_settle_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
        self._pending_scan_request = None
        self._thumbnail_cancel_event.set()
        self._cancel_cover_decode_jobs()
        self._thread_pool.clear()
        self._scan_thread_pool.clear()
        # Third-party decoders are bounded to a short grace period; shutdown
        # must remain responsive if one ignores cancellation.
        self._thread_pool.waitForDone(1000)
        self._scan_thread_pool.waitForDone(1000)
        for file_path, (generation, fingerprint) in tuple(
                self._thumbnail_job_owners.items()):
            self._finish_thumbnail_job(file_path, generation, fingerprint)
        self._active_scan_worker = None
        for path in list(self._watcher.directories()):
            self._watcher.removePath(path)

    def _clear_sections(self):
        self._cancel_cover_decode_jobs()
        # Virtual cards are direct host children so crossing dates never needs
        # reparenting. Dispose those explicitly when replacing the index.
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            if card.parentWidget() is self._sections_host:
                card.hide()
                card.deleteLater()
        self._recycled_cards.clear()
        self._section_grids.clear()
        self._section_files.clear()
        self._section_widgets.clear()
        self._virtual_sections.clear()
        self._card_virtual_sections.clear()
        while self._sections_layout.count():
            item = self._sections_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _show_empty(self, show: bool):
        self._import_actions.setVisible(self._filter == 'imported')
        self._import_status.setVisible(self._filter == 'imported' and bool(self._import_status.text()))
        if show:
            detail = ''
            if self._search_unavailable_reason:
                self._no_clips_lbl.setText('Search unavailable')
                detail = str(self._search_unavailable_reason)
            elif (self._search_criteria.text or self._search_criteria.tags_all
                  or self._search_criteria.favorites_only
                  or self._saved_search_id is not None):
                self._no_clips_lbl.setText('No matching clips')
            elif self._album_id is not None:
                self._no_clips_lbl.setText('No clips in this album')
                detail = self._filter_heading()
            elif self._filter == 'favorites':
                self._no_clips_lbl.setText('No favorites yet')
                detail = 'Use the star on a clip to add a favorite and protect it from deletion.'
            elif self._filter == 'screenshots':
                self._no_clips_lbl.setText('No screenshots yet')
                hotkeys = self._sm.get('hotkeys', {}) if self._sm else {}
                screenshot_key = hotkeys.get('save_screenshot', 'F12')
                detail = f'PRESS {screenshot_key} TO TAKE A SCREENSHOT'
            elif self._filter == 'clips':
                self._no_clips_lbl.setText('No clips yet')
            elif self._filter == 'imported' and self._game_filter is None:
                self._no_clips_lbl.setText('No imported clips')
                detail = 'Add a folder to see its clips here. Your originals stay in place.'
            elif self._game_filter is not None:
                self._no_clips_lbl.setText('No clips for this game')
                detail = self._game_filter
            else:
                self._no_clips_lbl.setText('No clips yet')
            self._empty_detail_lbl.setText(detail)
            self._empty_detail_lbl.setVisible(bool(detail))
        self._empty_widget.setVisible(show)
        self._sections_host.setVisible(not show)

    def _on_clip_deleted(self, file_path: str):
        identity = canonical_media_path(file_path)
        removed = self._records.pop(identity, None)
        if removed is not None:
            self._records_by_path.pop(removed.path, None)
        self._library_details_snapshot_dirty = True
        self._query_snapshot_dirty = True
        self._favorite_snapshot_dirty = True
        self._album_snapshot_dirty = True
        if isinstance(self._known_files, set):
            self._known_files.discard(file_path)
        self._imported_files.discard(file_path)
        self._virtual_ready.pop(file_path, None)
        for section_key, files in list(self._section_files.items()):
            if file_path not in files:
                continue
            self._section_files[section_key] = [path for path in files
                                                if path != file_path]
        # This rebuild uses the in-memory index only; no filesystem walk is
        # scheduled for an external delete.
        self._apply_current_index()

    def _on_thumb_ready(self, file_path: str, cache_path: str, duration: int,
                        expected_fingerprint: str = '', generation: int | None = None):
        if not self._thumbnails_enabled():
            return
        current = self._indexed_record(file_path)
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget:
            widget.set_video_thumbnail(cache_path, duration)
            widget._thumbnail_fingerprint = expected_fingerprint or (current.fingerprint if current else '')

    def _on_image_thumb_ready(
            self, file_path: str, image: QImage,
            expected_fingerprint: str = '', generation: int | None = None):
        if not self._thumbnails_enabled():
            return
        current = self._indexed_record(file_path)
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget is not None:
            widget.set_image_thumbnail(image)
            widget._thumbnail_fingerprint = expected_fingerprint or (current.fingerprint if current else '')

    def _on_thumb_diagnostic(
            self, elapsed_ms: int, metadata_ms: int, cache_hit: bool,
            file_path: str | None = None, generation: int | None = None,
            fingerprint: str = ''):
        # This is the single terminal cleanup path. Both successful and failed
        # workers emit diagnostic_finished from finally; cancelled workers do
        # too. Pause/rebuild leaves ownership intact until this callback so a
        # replacement can never run concurrently with its predecessor.
        if file_path is not None and generation is not None:
            worker = self._thumbnail_workers.get(file_path)
            locally_cancelled = worker is not None and worker.cancel_event.is_set()
            current = self._indexed_record(file_path)
            obsolete_owner = generation != self._thumbnail_generation
            fingerprint_changed = (
                bool(fingerprint) and current is not None
                and current.fingerprint != fingerprint)
            if self._finish_thumbnail_job(file_path, generation, fingerprint):
                self._thumbnail_jobs_completed += 1
                # Queue pressure must not strand a visible card until another
                # capture, scan or scroll happens. Only drain deferred work;
                # failed current jobs retain the normal scan retry cooldown.
                deferred = getattr(self, '_deferred_thumbnail_paths', set())
                if (deferred and not getattr(self, '_scrolling', False) and not self._background_paused
                        and not self._shutdown_started):
                    for path in tuple(deferred):
                        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
                            break
                        deferred.discard(path)
                        self._start_thumbnail_worker(path)
                # A cancelled old generation may have left a visible card
                # non-ready. Once its owner has actually reached the terminal
                # signal, enqueue exactly one replacement for the current
                # foreground generation.
                if obsolete_owner or fingerprint_changed:
                    self._requeue_visible_thumbnail_jobs()
                elif locally_cancelled:
                    self._start_thumbnail_worker(file_path)
        self._thumbnail_diagnostics_completed += 1
        self._thumbnail_total_elapsed_ms += max(0, int(elapsed_ms))
        self._metadata_total_elapsed_ms += max(0, int(metadata_ms))
        self._thumbnail_cache_hits += int(bool(cache_hit))
        active_processes, peak_processes = media_process_snapshot()
        if (self._thumbnail_diagnostics_completed == self._thumbnail_jobs_submitted
                or self._thumbnail_diagnostics_completed % 25 == 0):
            emit_event(
                'library', 'thumbnail_health_snapshot',
                metadata_jobs=self._thumbnail_jobs_submitted,
                thumbnail_jobs=self._thumbnail_jobs_submitted,
                completed_thumbnail_jobs=self._thumbnail_jobs_completed,
                completed_diagnostic_jobs=self._thumbnail_diagnostics_completed,
                total_thumbnail_elapsed_ms=self._thumbnail_total_elapsed_ms,
                total_metadata_probe_elapsed_ms=self._metadata_total_elapsed_ms,
                thumbnail_cache_hits=self._thumbnail_cache_hits,
                active_workers=self._thread_pool.activeThreadCount(),
                thumbnail_queue_depth=len(self._thumbnail_jobs_inflight),
                thumbnail_queue_peak=self._thumbnail_queue_peak,
                media_process_active=active_processes,
                media_process_peak=peak_processes,
                materialized_card_count=len(self._thumb_widgets),
                cache_entry_count=self._thumbnail_diagnostics_completed,
                process_memory_bytes=process_memory_bytes())
