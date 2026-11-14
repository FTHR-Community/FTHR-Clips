"""Bounded Qt model and sidebar panel for clip highlight markers."""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QAbstractItemView,
    QListView,
    QVBoxLayout,
)

from core.highlight_markers import HighlightMarker, normalize_highlight_markers
from ui.style import Colors, Fonts


def format_marker_time(position_100ns: int) -> str:
    """Format original presentation time without converting through a float."""

    milliseconds = int(position_100ns) // 10_000
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}'
    return f'{minutes}:{seconds:02d}.{millis:03d}'


class HighlightMarkersModel(QAbstractListModel):
    """One bounded list model; marker rows never become individual widgets."""

    MarkerRole = Qt.ItemDataRole.UserRole + 1
    AvailableRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: tuple[HighlightMarker, ...] = ()
        self._available: frozenset[str] = frozenset()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._markers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._markers):
            return None
        marker = self._markers[index.row()]
        available = marker.marker_id in self._available
        if role == Qt.ItemDataRole.DisplayRole:
            label = marker.label or f'Highlight {index.row() + 1}'
            return f'{label}  ·  {format_marker_time(marker.position_100ns)}'
        if role == self.MarkerRole:
            return marker.as_dict()
        if role == self.AvailableRole:
            return available
        if role == Qt.ItemDataRole.ToolTipRole:
            return ('Original source position: '
                    f'{format_marker_time(marker.position_100ns)}')
        if role == Qt.ItemDataRole.ForegroundRole and not available:
            return QBrush(QColor(Colors.TEXT_MUTED))
        return None

    def set_markers(self, markers: Iterable[Mapping | HighlightMarker],
                    available_ids: Iterable[str] | None = None):
        normalized = normalize_highlight_markers(list(markers))
        available = frozenset(available_ids or ())
        self.beginResetModel()
        self._markers = normalized
        self._available = frozenset(
            marker.marker_id for marker in normalized
            if marker.marker_id in available)
        self.endResetModel()

    def marker_at(self, row: int):
        if not 0 <= int(row) < len(self._markers):
            return None
        return self._markers[int(row)].as_dict()

    def available(self, row: int) -> bool:
        marker = self.marker_at(row)
        return marker is not None and marker['marker_id'] in self._available


# Singular spelling retained as a small compatibility convenience for tests
# and integrations that refer to the model by the feature's noun.
HighlightMarkerModel = HighlightMarkersModel


class HighlightMarkersPanel(QFrame):
    """Compact HIGHLIGHTS sidebar panel backed by one virtualized list view."""

    marker_activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('highlightMarkersPanel')
        self.model = HighlightMarkersModel(self)
        self.title_label = QLabel('HIGHLIGHTS')
        self.title_label.setObjectName('highlightMarkersTitle')
        self.title_label.setStyleSheet(
            f'color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY};'
            f' font-size: {Fonts.SIZE_MICRO}px; font-weight: 700;')
        self.error_label = QLabel()
        self.error_label.setObjectName('highlightMarkersError')
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setVisible(False)
        self.error_label.setStyleSheet(
            f'color: {Colors.TEXT_MUTED}; font-family: {Fonts.BODY};')
        self.view = QListView()
        self.view.setObjectName('highlightMarkersList')
        self.view.setModel(self.model)
        self.view.setUniformItemSizes(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.clicked.connect(self._activate)
        self.view.setMinimumHeight(48)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 4, 14, 12)
        layout.setSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.view)

    def _activate(self, index: QModelIndex):
        marker = self.model.marker_at(index.row())
        if marker is not None and self.model.available(index.row()):
            self.marker_activated.emit(marker)

    def set_markers(self, markers, available_ids=None):
        self.error_label.clear()
        self.error_label.setToolTip('')
        self.error_label.setVisible(False)
        self.view.setVisible(True)
        self.model.set_markers(markers, available_ids)
        self.setVisible(bool(self.model.rowCount()))

    def set_error(self, message: str):
        self.model.set_markers(())
        self.error_label.setText('Highlights could not be loaded for this clip.')
        self.error_label.setToolTip(f'<qt>{html.escape(str(message))}</qt>')
        self.error_label.setVisible(True)
        self.view.setVisible(False)
        self.setVisible(True)

    def clear(self):
        self.model.set_markers(())
        self.error_label.clear()
        self.error_label.setToolTip('')
        self.error_label.setVisible(False)
        self.view.setVisible(False)
        self.setVisible(False)
