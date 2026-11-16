"""A persistent editor window whose per-clip resources can be replaced."""

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QRegion
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton, QStackedLayout, QWidget

from ui.frameless_window_windows import apply_native_resize_style
from ui.style import Colors, Fonts, set_theme_style
from ui.editor_layout import studio_layout_metrics
from ui.icons import IconButton


class _EditorSkeleton(QWidget):
    first_paint = Signal()

    def __init__(self, title, parent):
        super().__init__(parent)
        self._title = title
        self._painted = False
        self._regions = None
        self.setAccessibleName(f'Loading clip editor: {title}')
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.close_button = IconButton('close.png', label='Close editor')
        self.close_button.setParent(self)
        self.close_button.setFixedSize(40, 40)
        self.close_button.clicked.connect(parent.reject)
        self.close_button.setToolTip('Close editor')
        set_theme_style(self.close_button, lambda: (
            'QPushButton { background: transparent; border: none; }'
            f'QPushButton:hover {{ background: {Colors.SURFACE_3}; }}'))

    def resizeEvent(self, event):
        self.close_button.move(self.width() - 40, 0)
        super().resizeEvent(event)

    def reveal_controls(self, regions):
        """Cover only unfinished content; ready controls also receive input."""
        if regions == self._regions:
            return
        self._regions = regions
        self.close_button.hide()
        mask = QRegion()
        for rect in regions.values():
            mask |= QRegion(rect)
        self.setMask(mask)
        self.setAccessibleName('Loading ' + ' and '.join(regions))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._regions is not None:
            painter.setFont(QFont(Fonts.DISPLAY_FAMILY, Fonts.SIZE_BODY))
            for name, rect in self._regions.items():
                painter.fillRect(rect, QColor(Colors.BG))
                if name == 'video':
                    painter.fillRect(rect.adjusted(4, 4, -4, -4), QColor(Colors.SURFACE_1))
                    painter.setPen(QColor(Colors.TEXT))
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, 'Loading video…')
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(Colors.SURFACE_3))
                    for row in range(max(1, min(3, rect.height() // 48))):
                        y = rect.y() + 8 + row * 48
                        painter.drawRoundedRect(QRectF(rect.x() + 8, y,
                                                      min(100, rect.width() - 16), 10), 3, 3)
                        painter.drawRoundedRect(QRectF(rect.x() + 8, y + 20,
                                                      max(1, rect.width() - 16), 7), 3, 3)
            painter.end()
            return
        painter.fillRect(self.rect(), QColor(Colors.BG))
        header = QRect(0, 0, self.width(), 40)
        painter.fillRect(header, QColor(Colors.SHELL_BG))
        painter.setPen(QColor(Colors.TEXT))
        painter.setFont(QFont(Fonts.DISPLAY_FAMILY, Fonts.SIZE_BODY))
        title_rect = header.adjusted(20, 0, -140, 0)
        title = painter.fontMetrics().elidedText(
            self._title, Qt.TextElideMode.ElideMiddle, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, title)
        width, height = self.width(), self.height()
        left, right, strip, _ = studio_layout_metrics(width, height, 16 / 9)
        timeline_y = height - strip - 76
        body_y = 90
        center_x, center_w = left + 12, width - left - right - 24

        def block(x, y, w, h, color=Colors.SURFACE_3):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, y, max(1, w), max(1, h)), 4, 4)

        def heading(x, y, text):
            painter.setPen(QColor(Colors.TEXT))
            painter.drawText(QRect(x, y, 180, 24), Qt.AlignmentFlag.AlignVCenter, text)

        # Match the real editor's toolbar, two inspectors, preview and timeline.
        # Static shapes need no animation timer or extra rendering under load.
        for x, w in ((width - 390, 164), (width - 214, 88), (width - 114, 100)):
            block(x, 50, w, 28)
        heading(20, body_y, 'Video')
        for row in range(2):
            block(20, body_y + 40 + row * 42, (left - 52) / 2, 30)
            block(32 + (left - 52) / 2, body_y + 40 + row * 42,
                  (left - 52) / 2, 30)
        heading(20, body_y + 142, 'Speed')
        block(20, body_y + 182, left - 40, 30)
        block(20, body_y + 226, left - 88, 12)
        heading(20, body_y + 270, 'Color & effects')
        if body_y + 322 < timeline_y - 8:
            block(20, body_y + 310, left - 40, 12)
        block(center_x, body_y, center_w, timeline_y - body_y - 54, Colors.SURFACE_1)
        painter.setPen(QColor(Colors.TEXT))
        painter.drawText(QRect(center_x, body_y, center_w, timeline_y - body_y - 54),
                         Qt.AlignmentFlag.AlignCenter, 'Loading clip…')
        block(center_x + 12, timeline_y - 42, 92, 28)
        block(center_x + 116, timeline_y - 34, 108, 12)
        rx = width - right + 16
        heading(rx, body_y, 'Audio mix')
        for row in range(3):
            block(rx, body_y + 40 + row * 66, min(108, right - 40), 12)
            block(rx, body_y + 64 + row * 66, right - 36, 8)
        heading(rx, body_y + 264, 'Clip details')
        if body_y + 330 < timeline_y - 8:
            block(rx, body_y + 302, right - 36, 28)
        painter.setPen(QColor(Colors.BORDER))
        painter.drawLine(0, timeline_y, width, timeline_y)
        block(20, timeline_y + 18, 56, 26)
        block(84, timeline_y + 18, 56, 26)
        block(width - 82, timeline_y + 18, 62, 26)
        tile_w = (width - 48) / 8
        for index in range(8):
            block(20 + index * (tile_w + 1), timeline_y + 64, tile_w - 5,
                  max(28, strip - 30))
        block(20, height - 20, width - 40, 6)
        painter.end()
        if not self._painted:
            self._painted = True
            from core.field_diagnostics import emit_event
            emit_event('playback', 'editor_shell_painted')
            # Start setup after Qt has flushed this paint to the backing store.
            QTimer.singleShot(0, self, self.first_paint.emit)


class ClipEditorWindow(QDialog):
    upload_requested = Signal(str)
    export_error = Signal(str, str, str)
    library_flags_changed = Signal(str)
    library_details_changed = Signal(str)
    clip_page_ready = Signal()
    clip_open_failed = Signal(str)
    clip_navigation_requested = Signal(int)
    minimize_requested = Signal()
    sidebar_navigation_requested = Signal(int)
    panel_switch_requested = Signal()

    def __init__(self, clip_path, bridge, parent=None, *, session_state=None,
                 autoplay=True, defer_initial_clip=False, **options):
        super().__init__(parent)
        self._page = None
        self._pending = None
        self._poster = None
        self._deferred_preview = None
        self._bridge = bridge
        self._options = {key: value for key, value in options.items()
                         if key != 'preloaded_media'}
        self._native_style_applied = False
        self._idle_memory_timer = QTimer(self)
        self._idle_memory_timer.setSingleShot(True)
        self._idle_memory_timer.setInterval(15_000)
        self._idle_memory_timer.timeout.connect(self._release_idle_memory)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowMaximizeButtonHint)
        self.setMinimumSize(960, 600)
        self._content = QStackedLayout(self)
        self._content.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._content.setContentsMargins(0, 0, 0, 0)
        self._content.setSpacing(0)
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(80)
        self._loading_timer.timeout.connect(self._finish_loading_if_ready)
        if defer_initial_clip:
            from ui.quick_video_preview import prepare_editor_preview
            self._deferred_preview = prepare_editor_preview(self)
            self.queue_clip_after_paint(clip_path, session_state=session_state,
                                        autoplay=autoplay, **options)
        else:
            self.replace_clip(clip_path, session_state=session_state,
                              autoplay=autoplay, **options)

    @property
    def clip_page(self):
        return self._page

    @property
    def clip_pending(self):
        return self._pending is not None

    @property
    def clip_path(self):
        if self._pending is not None:
            return self._pending[0]
        return self._page.clip_path if self._page is not None else None

    def __getattr__(self, name):
        # MainWindow's lifecycle operations act on the currently displayed
        # clip. The native window remains the same across replacements.
        page = self.__dict__.get('_page')
        if page is not None:
            return getattr(page, name)
        raise AttributeError(name)

    def capture_session_state(self):
        if self._pending is not None:
            return self._pending[1]
        return self._page.capture_session_state() if self._page is not None else None

    def queue_clip_after_paint(self, clip_path, *, session_state=None, autoplay=True, **options):
        """Keep the editor skeleton visible while controls and media initialize."""
        self._cancel_queued_clip()
        self._idle_memory_timer.stop()
        title = Path(clip_path).name
        self.setWindowTitle(f'FTHR — {title}')
        request = (clip_path, session_state, autoplay, options)
        self._pending = request
        prepared = options.get('preloaded_media')
        if prepared is not None:
            self.destroyed.connect(prepared.dispose)
        self._poster = _EditorSkeleton(title, self)
        self._poster.first_paint.connect(lambda: self._open_painted_clip(request))
        if self._page is not None:
            self._page.hide()
        self._content.addWidget(self._poster)
        self._content.setCurrentWidget(self._poster)
        self._poster.show()

    def _remove_poster(self):
        self._loading_timer.stop()
        poster, self._poster = self._poster, None
        if poster is not None:
            self._content.removeWidget(poster)
            poster.hide()
            poster.deleteLater()

    def _finish_loading_if_ready(self):
        page = self._page
        if page is None:
            self._remove_poster()
            return
        failed = getattr(getattr(page, 'player_state', None), 'value', '') == 'FAILED'
        frame_ready = (getattr(page, '_native_has_real_frame', False)
                       or getattr(page.video_widget, '_has_real_frame', False))
        if self._poster is None:
            return
        if failed:
            self._remove_poster()
            return
        regions = {}
        def region(name, widget):
            if not widget.isVisible():
                return
            origin = widget.mapTo(self, QPoint())
            regions[name] = QRect(origin, widget.size()).intersected(self._poster.rect())
        if not frame_ready:
            region('video', page._video_clip_frame)
        # Track discovery is enough to edit gains. Do not hide the panel while
        # its decoder finishes preparing, or block it on video readiness.
        if not (page._playback_sources or page._audio_preparation_ready):
            region('audio', page._audio_mix_panel)
        if regions:
            self._poster.reveal_controls(regions)
        else:
            self._remove_poster()

    def _cancel_queued_clip(self):
        request, self._pending = self._pending, None
        if request is None:
            return
        self._remove_poster()
        self._release_preload(request[3])

    def _release_preload(self, options):
        prepared = options.get('preloaded_media')
        if prepared is not None:
            self.destroyed.disconnect(prepared.dispose)
            prepared.dispose()

    def _open_painted_clip(self, request):
        if request is not self._pending:
            return
        self._pending = None
        clip_path, session_state, autoplay, options = request
        error_detail = None
        try:
            opened = self.replace_clip(clip_path, session_state=session_state,
                                       autoplay=autoplay, **options)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
            opened = False
            error_detail = str(error)
        finally:
            self._release_preload(options)
        if opened:
            self._finish_loading_if_ready()
            if self._poster is not None:
                self._loading_timer.start()
            from core.field_diagnostics import emit_event
            emit_event('playback', 'editor_controls_ready')
            self.clip_page_ready.emit()
        else:
            if error_detail is not None:
                self.clip_open_failed.emit(error_detail)
            self.reject()

    def _prepare_page_details(self, page):
        flush = getattr(page, '_flush_clip_details', None)
        if not callable(flush):
            return True
        try:
            if flush() is False:
                return False
            if any(getattr(page, name, None) is not None
                   and getattr(page, name).isModified()
                   for name in ('_tag_edit', '_desc_edit')):
                raise OSError('The clip details could not be saved')
        except (OSError, TypeError, ValueError) as error:
            warn = getattr(page, '_warn_clip_details_error', None)
            if callable(warn):
                warn(error)
            return False
        return True

    def replace_clip(self, clip_path, *, session_state=None, autoplay=True,
                     **options):
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return False
        self._idle_memory_timer.stop()
        previous = self._page
        worker = getattr(previous, '_export_thread', None)
        if worker is not None and worker.is_alive():
            QMessageBox.information(self, 'Export in progress',
                                    'Finish or cancel the export before switching clips.')
            return False
        if previous is not None and not self._prepare_page_details(previous):
            return False
        self.setUpdatesEnabled(False)
        replacement_error = None
        settings = {**self._options, **options}
        try:
            if previous is None:
                from ui.clip_viewer import ClipViewer

                page = ClipViewer(clip_path, self._bridge, self, embedded=True,
                                  autoplay=False, native_preview=self._deferred_preview, **settings)
                self._deferred_preview = None
                page.upload_requested.connect(self.upload_requested.emit)
                page.export_error.connect(self.export_error.emit)
                page.library_flags_changed.connect(self.library_flags_changed.emit)
                page.library_details_changed.connect(self.library_details_changed.emit)
                page.clip_navigation_requested.connect(self.clip_navigation_requested.emit)
                page.minimize_requested.connect(self.minimize_requested.emit)
                page.sidebar_navigation_requested.connect(self.sidebar_navigation_requested.emit)
                page.panel_switch_requested.connect(self.panel_switch_requested.emit)
                page.finished.connect(self._page_finished)
                self._page = page
                self._content.addWidget(page)
                if self._poster is not None:
                    self._content.setCurrentWidget(self._poster)
                for button in page.findChildren(QPushButton):
                    button.setAutoDefault(False)
                    button.setDefault(False)
            else:
                page = previous
                previous_path = page.clip_path
                previous_state = page.capture_session_state()
                try:
                    page.load_clip(clip_path, autoplay=False, **settings)
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    replacement_error = str(error)
                    clip_path, settings = previous_path, self._options
                    session_state, autoplay = previous_state, False
                    page.load_clip(clip_path, autoplay=False, **settings)
            self._options = {key: value for key, value in settings.items()
                             if key != 'preloaded_media'}
            self.setWindowTitle(f'FTHR — {Path(clip_path).name}')
            set_theme_style(self, lambda: self._page._theme_style_factory())
            if session_state is not None:
                page.restore_session_state(session_state)
            if self.isVisible():
                page.show()
            if autoplay:
                page.start_autoplay()
            page.play_btn.setFocus()
        finally:
            self.setUpdatesEnabled(True)
        if replacement_error is not None:
            QMessageBox.warning(self, 'Could not open clip', replacement_error)
            self.update()
            return False
        self.update()
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._content.invalidate()
        self._content.activate()
        if self._page is not None and self._pending is None:
            self._finish_loading_if_ready()

    def hideEvent(self, event):
        if not event.spontaneous():
            self._cancel_queued_clip()
        self._idle_memory_timer.start()
        super().hideEvent(event)

    def _release_idle_memory(self):
        if not self.isVisible() and self._page is not None:
            self._page.release_idle_ui_resources()

    def done(self, result):
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return
        super().done(result)

    def _page_finished(self, result):
        self.done(result)

    def _close(self):
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return False
        if self.clip_pending or self._page is None:
            return self.reject()
        return self._page._close()

    def _teardown_player(self, **options):
        self._cancel_queued_clip()
        self._remove_poster()
        if self._page is not None:
            self._page._teardown_player(**options)

    def reject(self):
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return False
        if self._page is not None and not self._prepare_page_details(self._page):
            return False
        worker = getattr(self._page, '_export_thread', None)
        if worker is not None and worker.is_alive():
            self._page._close()
            return
        self._teardown_player(wait_for_workers=False)
        super().reject()

    def accept(self):
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return False
        if self._page is not None and not self._prepare_page_details(self._page):
            return False
        self._teardown_player()
        super().accept()
        return True

    def closeEvent(self, event):
        event.ignore()
        if getattr(self._page, '_trash_pending_token', None) is not None:
            return
        if self._page is not None and not self._prepare_page_details(self._page):
            return
        worker = getattr(self._page, '_export_thread', None)
        if worker is not None and worker.is_alive():
            self._page._close()
            return
        self._teardown_player(wait_for_workers=False)
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            if self.clip_pending or self._poster is not None:
                self.reject()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        self._idle_memory_timer.stop()
        super().showEvent(event)
        if not self._native_style_applied:
            self._native_style_applied = True
            QTimer.singleShot(0, self, lambda: apply_native_resize_style(self))

    def nativeEvent(self, event_type, message):
        if self._page is not None:
            return self._page.nativeEvent(event_type, message)
        return False, 0
