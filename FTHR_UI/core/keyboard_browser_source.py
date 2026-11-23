"""Render an OBS browser overlay without forwarding any input."""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from core.third_party_keyboard import keyboard_browser_host_allowed


class _OverlayRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        if (url.scheme() not in {'about', 'blob', 'data'}
                and not keyboard_browser_host_allowed(url.scheme(), url.host().casefold())):
            info.block(True)


class KeyboardBrowserSource:
    """A transparent view kept off the desktop surface and sampled on demand."""

    def __init__(self, url, width, height, fps, on_frame, on_error):
        self._on_frame = on_frame
        self._on_error = on_error
        self._loaded = False
        self._closed = False
        self._view = QWebEngineView()
        self._view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self._view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._view.resize(width, height)
        self._profile = QWebEngineProfile()
        self._interceptor = _OverlayRequests(self._profile)
        self._profile.setUrlRequestInterceptor(self._interceptor)
        self._profile.downloadRequested.connect(lambda download: download.cancel())
        self._page = QWebEnginePage(self._profile, self._view)
        self._page.setBackgroundColor(QColor(0, 0, 0, 0))
        self._page.permissionRequested.connect(lambda permission: permission.deny())
        self._view.setPage(self._page)
        self._view.loadFinished.connect(self._on_loaded)
        self._view.renderProcessTerminated.connect(
            lambda *_: self._on_loaded(False))
        self._timer = QTimer(self._view)
        self._timer.setInterval(round(1000 / fps))
        self._timer.timeout.connect(self._sample)
        self._retry = QTimer(self._view)
        self._retry.setSingleShot(True)
        self._retry.setInterval(3000)
        self._retry.timeout.connect(lambda: self._view.load(QUrl(url)))
        self._view.show()
        self._view.load(QUrl(url))

    def _on_loaded(self, ok):
        if self._closed:
            return
        self._loaded = bool(ok)
        if ok:
            self._retry.stop()
            self._timer.start()
        else:
            self._timer.stop()
            self._on_error('Browser source could not be loaded. Check that the overlay is running.')
            self._retry.start()

    def _sample(self):
        if self._loaded:
            image = self._view.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
            if not image.isNull():
                self._on_frame(image)

    def close(self):
        view, profile = self._view, self._profile
        self._closed = True
        self._loaded = False
        self._timer.stop()
        self._retry.stop()
        view.stop()
        view.close()
        view.destroyed.connect(profile.deleteLater)
        view.deleteLater()
