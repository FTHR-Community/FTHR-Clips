"""Library-only admission and scheduling for the optional warm editor player."""

import time

from PySide6.QtCore import QObject, QTimer


class EditorPreloadPolicy(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._loader = None
        self._candidate = None
        self._last_requested = None
        self._last_requested_at = 0.0
        self._suspended_until = 0.0
        self._closed = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._prepare_candidate)
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._tick)
        self.refresh()

    def _enabled(self):
        sm = self.owner.settings_manager
        return (not self._closed and bool(sm.get('editor_fast_open_enabled', False))
                and bool(sm.get('clip_editor_enabled', True)))

    def _eligible(self):
        owner = self.owner
        save = getattr(owner, '_save_state', None)
        grid = owner.clip_grid
        return (self._enabled() and time.monotonic() >= self._suspended_until
                and owner.isVisible() and not owner.isMinimized()
                and not getattr(owner, '_background_ui_paused', False)
                and not getattr(owner, '_ultra_performance', False)
                and not getattr(owner, '_shutdown_requested', False)
                and getattr(owner, '_file_operations_ready', False)
                and getattr(owner, '_manual_record_state', 'idle') == 'idle'
                and not (save is not None and save.is_busy())
                and getattr(owner, '_active_clip_viewer', None) is None
                and owner.main_stack.currentIndex() == 0
                and not getattr(grid, '_background_paused', False)
                and not getattr(grid, '_shutdown_started', False)
                and getattr(grid, '_bulk_job', None) is None)

    def refresh(self):
        if self._enabled():
            if not self._poll.isActive():
                self._poll.start()
            if not self._eligible():
                self.clear()
            elif hasattr(self.owner, 'clip_scroll_area'):
                self._tick()
        else:
            self._poll.stop()
            self.clear()

    def request(self, path):
        if not self._eligible():
            return
        # A hover takes priority over the automatically chosen visible card.
        if (path == self._candidate or (path == self._last_requested
                and time.monotonic() - self._last_requested_at < 5.0)):
            return
        self._candidate = path
        self._debounce.start()

    def _tick(self):
        if not self._eligible():
            self.clear()
            return
        if self._candidate is not None:
            return
        viewport = self.owner.clip_scroll_area.viewport()
        visible = []
        for path, card in tuple(self.owner.clip_grid._thumb_widgets.items()):
            if (card.is_video and card.ready and not card._mutation_busy
                    and card.isVisible()
                    and viewport.rect().intersects(card.rect().translated(
                        card.mapTo(viewport, card.rect().topLeft())))):
                visible.append(path)
        # Keep a visible candidate warm without reopening it every 30 seconds.
        # The loader still expires when library polling stops, and failed
        # preparation retries with a bounded backoff rather than staying stale.
        if visible and self._last_requested not in visible:
            self.request(visible[0])
        elif visible and self._loader is not None:
            if not self._loader.keep_alive(self._last_requested):
                self.request(self._last_requested)

    def _prepare_candidate(self):
        path, self._candidate = self._candidate, None
        if not path or not self._eligible():
            return
        if not self.owner._clip_readiness.can_access(path):
            return
        if self._loader is None:
            from ui.editor_preloader import EditorPreloader
            self._loader = EditorPreloader(self)
        self._last_requested = path
        self._last_requested_at = time.monotonic()
        self._loader.request(path, preview_quality=self.owner.settings_manager.get(
            'editor_preview_quality', 'auto'))

    def take(self, path):
        self._debounce.stop()
        self._candidate = None
        prepared = None
        if self._enabled() and self._loader is not None:
            prepared = self._loader.take(path)
        self.clear()
        return prepared

    def clear(self, *, suspend=False):
        self._debounce.stop()
        self._candidate = None
        self._last_requested = None
        if suspend:
            self._suspended_until = time.monotonic() + 3.0
        if self._loader is not None:
            self._loader.clear()

    def shutdown(self):
        self._closed = True
        self._poll.stop()
        self.clear()
