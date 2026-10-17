"""Small, explicitly-scoped preview of the currently selected capture window."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QFontMetrics, QImage, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

from ui.style import Colors, Fonts


class WindowPreview(QFrame):
    """A lazy, non-interactive preview that follows one exact window identity.

    The preview backend is imported and started only while this widget is
    visible. Its destination is the top-level popup's client area (never this
    child widget's native handle), expressed in physical pixels.
    """

    _geometry_update = Signal()

    def __init__(self, parent=None, *, eyebrow: bool | str = True):
        super().__init__(parent)
        self._entry: dict | None = None
        self._backend = None
        self._backend_running = False
        self._image_slot = None
        self._status_slot = None
        self._backend_cleanup = {'backend': None}
        self._generation = 0
        self._last_status = 'Select a window to preview'
        self._image = None
        self._preferred_height = 180
        self.setObjectName('windowPreview')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMaximumHeight(self._preferred_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        if eyebrow:
            eyebrow_text = eyebrow if isinstance(eyebrow, str) else 'SELECTED WINDOW PREVIEW'
            self.eyebrow = QLabel(eyebrow_text)
            self.eyebrow.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-size: 9px; '
                f'font-family: {Fonts.DISPLAY}; letter-spacing: 1px;')
            layout.addWidget(self.eyebrow)
        else:
            self.eyebrow = None

        self.image_label = QLabel(self._last_status)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(100, 100)
        self.image_label.setMaximumHeight(140)
        self.image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.image_label.setStyleSheet(
            f'background: transparent; color: {Colors.TEXT_DIM}; '
            f'font-size: 10px; font-family: {Fonts.BODY}; border: none;')
        layout.addWidget(self.image_label)

        self.caption = QLabel('')
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.caption.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: 10px; font-family: {Fonts.BODY};')
        layout.addWidget(self.caption)
        self.capture_notice = None
        if sys.platform.startswith('linux'):
            self.capture_notice = QLabel(
                'Window preview only — Linux capture currently records the desktop')
            self.capture_notice.setWordWrap(True)
            self.capture_notice.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-size: 9px; '
                f'font-family: {Fonts.BODY};')
            layout.addWidget(self.capture_notice)

        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(75)
        self._geometry_timer.timeout.connect(self._update_backend_geometry)
        self._geometry_update.connect(self._schedule_geometry_update)
        cleanup_holder = self._backend_cleanup

        def cleanup_backend(_obj=None):
            backend = cleanup_holder.get('backend')
            if backend is not None:
                try:
                    dispose = getattr(backend, 'dispose', None)
                    if callable(dispose):
                        dispose()
                    else:
                        backend.stop()
                except RuntimeError:
                    # Destruction may already have invalidated the Qt wrapper;
                    # cleanup is best-effort and must not touch it again.
                    pass
                cleanup_holder['backend'] = None

        self.destroyed.connect(cleanup_backend)
        self._apply_frame_style()

    @staticmethod
    def _identity(entry: dict | None):
        if not isinstance(entry, dict):
            return None
        try:
            hwnd = int(entry.get('hwnd', 0) or 0)
        except (TypeError, ValueError):
            # Malformed native IDs are not previewable window identities.
            return None
        if not hwnd:
            return None
        pid = entry.get('pid')
        return (hwnd, pid) if pid is not None else (hwnd,)

    def set_window(self, entry: dict | None, *, force_refresh: bool = False):
        """Select by exact native identity; clear immediately for empty input."""
        normalized = entry if self._identity(entry) is not None else None
        if (not force_refresh and
                self._identity(normalized) == self._identity(self._entry)):
            # Titles may change while the same HWND remains selected.
            if normalized is not None:
                self._entry = dict(normalized)
                self._update_caption()
            return

        self._generation += 1
        self._stop_backend()
        self._entry = dict(normalized) if normalized is not None else None
        self._image = None
        self._show_status('Select a window to preview' if self._entry is None
                          else 'Preview starting…')
        self._update_caption()
        if self.isVisible():
            self._start_backend()

    def refresh_theme(self):
        self._apply_frame_style()
        if self.eyebrow is not None:
            self.eyebrow.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-size: 9px; '
                f'font-family: {Fonts.DISPLAY}; letter-spacing: 1px;')
        self.caption.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: 10px; font-family: {Fonts.BODY};')
        self.image_label.setStyleSheet(
            f'background: transparent; color: {Colors.TEXT_DIM}; '
            f'font-size: 10px; font-family: {Fonts.BODY}; border: none;')
        if self.capture_notice is not None:
            self.capture_notice.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-size: 9px; '
                f'font-family: {Fonts.BODY};')
        self._render_image_or_status()

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        top_level = self.window()
        if top_level is not self:
            top_level.installEventFilter(self)
        if self._entry is not None:
            self._start_backend()

    def hideEvent(self, event):  # noqa: N802 - Qt API
        self._geometry_timer.stop()
        self._stop_backend()
        top_level = self.window()
        if top_level is not self:
            top_level.removeEventFilter(self)
        super().hideEvent(event)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_caption()
        self._geometry_update.emit()
        self._render_image_or_status()

    def moveEvent(self, event):  # noqa: N802 - Qt API
        super().moveEvent(event)
        self._geometry_update.emit()

    def _apply_frame_style(self):
        self.setStyleSheet(
            f'QFrame#windowPreview {{ background: {Colors.SURFACE_1}; '
            f'border: 1px solid {Colors.BORDER}; border-radius: 2px; }}')

    def set_preferred_height(self, height: int):
        """Set a natural height that can be reduced for short display areas."""
        height = max(88, min(180, int(height)))
        if height == self._preferred_height:
            return
        self._preferred_height = height
        self.setMaximumHeight(height)
        self.updateGeometry()

    def set_compact_for_short_screen(self, compact: bool):
        """Allow the viewport's minimum to relax only when the popup must fit."""
        if compact:
            self.image_label.setMinimumHeight(0)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        else:
            self.image_label.setMinimumHeight(100)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802 - Qt API
        """Reserve a useful natural viewport while leaving the label shrinkable."""
        hint = super().sizeHint()
        return QSize(max(160, hint.width()),
                     min(self.maximumHeight(),
                         max(hint.height(), self._preferred_height)))

    def _update_caption(self):
        title = str((self._entry or {}).get('title', '') or '')
        self.caption.setText(QFontMetrics(self.caption.font()).elidedText(
            title, Qt.TextElideMode.ElideRight, max(180, self.caption.width())))
        self.caption.setToolTip(title)

    def _show_status(self, status: str):
        self._last_status = str(status or 'Preview unavailable')
        self._image = None
        self._render_image_or_status()

    def _render_image_or_status(self):
        if self._image is None:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(self._last_status)
            return
        image = self._image
        pixmap = QPixmap.fromImage(image).scaled(
            self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.image_label.setText('')
        self.image_label.setPixmap(pixmap)

    def _start_backend(self):
        if self._backend_running or self._entry is None or not self.isVisible():
            return
        backend = self._backend
        if backend is None:
            try:
                from core.window_preview import create_window_preview_backend
                backend = create_window_preview_backend(self)
            except Exception:
                self._show_status('Window preview unavailable')
                return
            self._backend = backend
            self._backend_cleanup['backend'] = backend
        generation = self._generation
        self._image_slot = lambda image, g=generation, b=backend: self._on_image(image, g, b)
        self._status_slot = lambda status, g=generation, b=backend: self._on_status(status, g, b)
        backend.image_ready.connect(self._image_slot)
        backend.status_changed.connect(self._status_slot)
        self._show_status('Preview starting…')
        self._backend_running = True
        try:
            backend.start(self._entry, *self._destination())
        except Exception as exc:
            self._show_status(f'Preview unavailable: {exc}')
            self._stop_backend()

    def _stop_backend(self):
        self._geometry_timer.stop()
        backend = self._backend
        if backend is not None:
            image_slot, self._image_slot = self._image_slot, None
            status_slot, self._status_slot = self._status_slot, None
            if image_slot is not None:
                try:
                    backend.image_ready.disconnect(image_slot)
                except (RuntimeError, TypeError):
                    # The Qt object may be deleted or the slot already
                    # disconnected during backend shutdown; continue cleanup.
                    pass
            if status_slot is not None:
                try:
                    backend.status_changed.disconnect(status_slot)
                except (RuntimeError, TypeError):
                    # The Qt object may be deleted or the slot already
                    # disconnected during backend shutdown; continue cleanup.
                    pass
            if self._backend_running:
                self._backend_running = False
                try:
                    backend.stop()
                except RuntimeError:
                    # A deleted C++ QObject cannot be stopped; it is already
                    # beyond the widget's control.
                    pass

    def _on_image(self, image: QImage, generation: int, backend):
        if generation != self._generation or backend is not self._backend:
            return
        if isinstance(image, QImage) and not image.isNull():
            self._image = image
            self._last_status = ''
            self._render_image_or_status()

    def _on_status(self, status: str, generation: int, backend):
        if generation != self._generation or backend is not self._backend:
            return
        if str(status).strip().casefold() == 'live window preview':
            if self._image is None:
                # Windows DWM draws directly into the top-level window; keep
                # the child viewport transparent so that native thumbnail is
                # not covered by a placeholder or status label.
                self._last_status = ''
                self.image_label.setPixmap(QPixmap())
                self.image_label.setText('')
            return
        if self._image is not None:
            self._image = None
        self._show_status(status)

    def _schedule_geometry_update(self):
        if self._backend is not None and self.isVisible():
            self._geometry_timer.start()

    def _destination(self):
        top_level = self.window()
        dpr = float(top_level.devicePixelRatioF())
        origin = top_level.mapFromGlobal(self.image_label.mapToGlobal(QPoint(0, 0)))
        rect = self.image_label.rect()
        left = round(origin.x() * dpr)
        top = round(origin.y() * dpr)
        right = round((origin.x() + rect.width()) * dpr)
        bottom = round((origin.y() + rect.height()) * dpr)
        return int(top_level.winId()), (left, top, right, bottom)

    def _update_backend_geometry(self):
        backend = self._backend
        if backend is None or self._entry is None or not self.isVisible():
            return
        try:
            backend.update_geometry(*self._destination())
        except (RuntimeError, AttributeError):
            # Ignore geometry ticks during Qt teardown or after a backend has
            # been disposed; there is no live preview surface to update.
            pass

    def event(self, event):
        result = super().event(event)
        event_type = event.type()
        if event_type in (QEvent.Type.ParentChange, QEvent.Type.WindowChangeInternal,
                          QEvent.Type.ScreenChangeInternal):
            self._geometry_update.emit()
        return result

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if watched is self.window() and event.type() in (
                QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
                QEvent.Type.ScreenChangeInternal):
            self._geometry_update.emit()
        return super().eventFilter(watched, event)
