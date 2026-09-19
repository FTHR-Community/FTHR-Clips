"""deduplication_dialog.py
Dialog for finding and losslessly merging overlapping clips to free disk space.
Includes interactive video preview to inspect and compare overlapping clips.
"""

from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QUrl, Qt, QThread, Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.clip_deduplicator import (
    OverlapPair,
    find_overlapping_pairs,
    merge_overlapping_pair,
    scan_clip_records,
)
from ui.dialogs import FthrDialog
from ui.style import (
    Colors,
    Fonts,
    Sizes,
    button_outline_qss,
    button_primary_qss,
    label_body,
    slider_qss,
)


def _format_ms(ms: int) -> str:
    s = max(0, ms // 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ClipPreviewDialog(FthrDialog):
    """Clean video preview dialog to inspect and compare overlapping clips."""

    def __init__(
        self,
        pair: OverlapPair,
        initial_clip: int = 1,
        parent: QWidget | None = None,
    ):
        super().__init__("CLIP PREVIEW & COMPARISON", parent, width=880)
        self.pair = pair
        self.active_clip_idx = initial_clip
        self._duration_ms = 0
        self._is_seeking = False

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)

        self._setup_ui()
        self._setup_events()
        self.switch_clip(self.active_clip_idx)

    def _setup_ui(self):
        # Clip switcher row
        switch_row = QHBoxLayout()
        switch_row.setSpacing(10)

        p1_name = self.pair.first.path.name
        p2_name = self.pair.second.path.name
        if len(p1_name) > 34:
            p1_name = p1_name[:32] + "…"
        if len(p2_name) > 34:
            p2_name = p2_name[:32] + "…"

        self.clip1_btn = QPushButton(f"CLIP 1: {p1_name}")
        self.clip1_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.clip1_btn.setToolTip(str(self.pair.first.path))
        self.clip1_btn.clicked.connect(lambda: self.switch_clip(1))
        switch_row.addWidget(self.clip1_btn, 1)

        self.clip2_btn = QPushButton(f"CLIP 2: {p2_name}")
        self.clip2_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.clip2_btn.setToolTip(str(self.pair.second.path))
        self.clip2_btn.clicked.connect(lambda: self.switch_clip(2))
        switch_row.addWidget(self.clip2_btn, 1)

        overlap_sec = int(self.pair.overlap_seconds)
        saved_mb = self.pair.estimated_saved_bytes / (1024 * 1024)
        info_badge = QLabel(f"OVERLAP: {overlap_sec}s  (~{saved_mb:.1f} MB)")
        info_badge.setStyleSheet(
            f"color: {Colors.ACCENT}; background: {Colors.ACCENT_SOFT}; "
            f"border: 1px solid {Colors.ACCENT_DIM}; border-radius: 4px; "
            f"padding: 6px 12px; font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_LABEL}px; "
            f"font-weight: bold; letter-spacing: 1px;"
        )
        switch_row.addWidget(info_badge)
        self.body_layout.addLayout(switch_row)
        self.body_layout.addSpacing(6)

        # Video frame
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(440)
        self.video_widget.setStyleSheet(f"background-color: #000000; border: 1px solid {Colors.BORDER};")
        self.player.setVideoOutput(self.video_widget)
        self.body_layout.addWidget(self.video_widget)
        self.body_layout.addSpacing(6)

        # Playback controls
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setSpacing(10)

        self.play_btn = QPushButton("❚❚ PAUSE")
        self.play_btn.setFixedWidth(85)
        self.play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.play_btn.setStyleSheet(button_primary_qss())
        self.play_btn.clicked.connect(self._toggle_playback)
        ctrl_bar.addWidget(self.play_btn)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setFixedWidth(115)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet(
            f"color: {Colors.TEXT}; font-family: {Fonts.DISPLAY}; "
            f"font-size: {Fonts.SIZE_BODY}px; font-weight: bold;"
        )
        ctrl_bar.addWidget(self.time_label)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setStyleSheet(slider_qss())
        self.seek_slider.setRange(0, 1000)
        ctrl_bar.addWidget(self.seek_slider, 1)

        self.ext_btn = QPushButton("OPEN EXTERNAL")
        self.ext_btn.setStyleSheet(button_outline_qss())
        self.ext_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.ext_btn.setToolTip("Open in default Windows media player")
        self.ext_btn.clicked.connect(self._open_external)
        ctrl_bar.addWidget(self.ext_btn)

        self.body_layout.addLayout(ctrl_bar)

    def _setup_events(self):
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)

    def switch_clip(self, clip_idx: int):
        self.active_clip_idx = clip_idx
        path = self.pair.first.path if clip_idx == 1 else self.pair.second.path
        if not path.exists():
            return

        if clip_idx == 1:
            self.clip1_btn.setStyleSheet(button_primary_qss())
            self.clip2_btn.setStyleSheet(button_outline_qss())
        else:
            self.clip1_btn.setStyleSheet(button_outline_qss())
            self.clip2_btn.setStyleSheet(button_primary_qss())

        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_playback_state_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_btn.setText("❚❚ PAUSE")
            self.play_btn.setStyleSheet(button_primary_qss())
        else:
            self.play_btn.setText("▶ PLAY")
            self.play_btn.setStyleSheet(button_outline_qss())

    def _on_duration_changed(self, duration_ms: int):
        self._duration_ms = max(0, duration_ms)
        self.seek_slider.setRange(0, self._duration_ms)
        pos_ms = self.player.position()
        self.time_label.setText(f"{_format_ms(pos_ms)} / {_format_ms(self._duration_ms)}")

    def _on_position_changed(self, pos_ms: int):
        if not self._is_seeking:
            self.seek_slider.setValue(pos_ms)
            self.time_label.setText(f"{_format_ms(pos_ms)} / {_format_ms(self._duration_ms)}")

    def _on_seek_moved(self, pos_ms: int):
        self._is_seeking = True
        self.time_label.setText(f"{_format_ms(pos_ms)} / {_format_ms(self._duration_ms)}")

    def _on_seek_released(self):
        self.player.setPosition(self.seek_slider.value())
        self._is_seeking = False

    def _open_external(self):
        path = self.pair.first.path if self.active_clip_idx == 1 else self.pair.second.path
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self._toggle_playback()
            event.accept()
        elif event.key() == Qt.Key.Key_Left:
            self.player.setPosition(max(0, self.player.position() - 5000))
            event.accept()
        elif event.key() == Qt.Key.Key_Right:
            self.player.setPosition(min(self._duration_ms, self.player.position() + 5000))
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _teardown(self):
        if getattr(self, '_torn_down', False):
            return
        self._torn_down = True
        try:
            if hasattr(self, 'audio_output') and self.audio_output is not None:
                self.audio_output.setMuted(True)
                self.audio_output.setVolume(0.0)
        except Exception:
            pass
        try:
            if hasattr(self, 'player') and self.player is not None:
                self.player.stop()
                self.player.setSource(QUrl())
                self.player.setVideoOutput(None)
                self.player.setAudioOutput(None)
        except Exception:
            pass

    def reject(self):
        self._teardown()
        super().reject()

    def accept(self):
        self._teardown()
        super().accept()

    def done(self, r):
        self._teardown()
        super().done(r)

    def closeEvent(self, event):  # noqa: N802
        self._teardown()
        super().closeEvent(event)


class _ScanWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(list)

    def __init__(self, directory: Path):
        super().__init__()
        self.directory = directory
        self.cancel_event = threading.Event()

    def run(self):
        records = scan_clip_records(
            self.directory,
            cancel_event=self.cancel_event,
            progress_cb=lambda cur, tot, name: self.progress.emit(cur, tot, name),
        )
        if not self.cancel_event.is_set():
            pairs = find_overlapping_pairs(records)
            self.finished.emit(pairs)
        else:
            self.finished.emit([])

    def cancel(self):
        self.cancel_event.set()


class _MergeWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(int, int)  # success_count, total_bytes_saved

    def __init__(self, pairs: list[OverlapPair], remove_originals: bool):
        super().__init__()
        self.pairs = pairs
        self.remove_originals = remove_originals
        self.cancel_event = threading.Event()

    def run(self):
        success = 0
        bytes_saved = 0
        total = len(self.pairs)
        for idx, pair in enumerate(self.pairs):
            if self.cancel_event.is_set():
                break
            self.progress.emit(idx, total, f"Merging {pair.first.path.name}...")
            try:
                merge_overlapping_pair(
                    pair,
                    remove_originals=self.remove_originals,
                    cancel_event=self.cancel_event,
                )
                success += 1
                bytes_saved += pair.estimated_saved_bytes
            except Exception as e:
                print(f"[Deduplication] Failed to merge {pair.first.path.name} and {pair.second.path.name}: {e}")
                continue

        self.finished.emit(success, bytes_saved)

    def cancel(self):
        self.cancel_event.set()


class ClipDeduplicationDialog(FthrDialog):
    """Scan and losslessly merge overlapping clips to recover disk space."""

    deduplication_completed = Signal(int, int)  # success_count, bytes_saved

    def __init__(self, clips_directory: Path, parent: QWidget | None = None):
        super().__init__("CLIP DEDUPLICATION & OVERLAP MANAGER", parent, width=860)
        self.clips_dir = Path(clips_directory)
        self.pairs: list[OverlapPair] = []
        self._scan_worker: _ScanWorker | None = None
        self._merge_worker: _MergeWorker | None = None

        self._setup_ui()
        self._start_scan()

    def _setup_ui(self):
        self.info_label = QLabel("Scanning library for overlapping clips...")
        self.info_label.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))
        self.body_layout.addWidget(self.info_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "Select", "Clip 1", "Clip 2", "Overlap & Savings", "Preview"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {Colors.SURFACE_1};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                selection-background-color: #142820;
                selection-color: {Colors.TEXT};
            }}
            QTableWidget::item:selected {{
                background-color: #142820;
                color: {Colors.TEXT};
            }}
            QTableWidget::item:hover {{
                background-color: {Colors.SURFACE_2};
            }}
            QHeaderView::section {{
                background-color: {Colors.SURFACE_2};
                color: {Colors.TEXT_MUTED};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_LABEL}px;
                padding: 6px;
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
            }}
        """)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 55)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(4, 95)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        self.body_layout.addWidget(self.table)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER};
                height: 12px;
                text-align: center;
                color: {Colors.TEXT};
                font-size: 10px;
            }}
            QProgressBar::chunk {{
                background: {Colors.ACCENT};
            }}
        """)
        self.progress_bar.setVisible(False)
        self.body_layout.addWidget(self.progress_bar)

        self.delete_checkbox = QCheckBox("Delete original overlapping clips after successful merge")
        self.delete_checkbox.setChecked(True)
        self.delete_checkbox.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
        self.body_layout.addWidget(self.delete_checkbox)

        self.disclaimer_label = QLabel("Note: Clips are merged losslessly without re-encoding, so a 1-2 second jump may appear at the stitch boundary.")
        self.disclaimer_label.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO))
        self.disclaimer_label.setWordWrap(True)
        self.body_layout.addWidget(self.disclaimer_label)

        # Actions
        self.action_layout.addStretch(1)

        self.preview_btn = QPushButton("PREVIEW SELECTED")
        self.preview_btn.setStyleSheet(button_outline_qss())
        self.preview_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self._preview_selected_row)
        self.action_layout.addWidget(self.preview_btn)

        self.scan_btn = QPushButton("RE-SCAN")
        self.scan_btn.setStyleSheet(button_outline_qss())
        self.scan_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.scan_btn.clicked.connect(self._start_scan)
        self.action_layout.addWidget(self.scan_btn)

        self.merge_btn = QPushButton("MERGE SELECTED")
        self.merge_btn.setStyleSheet(button_primary_qss())
        self.merge_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.merge_btn.setEnabled(False)
        self.merge_btn.clicked.connect(self._start_merge)
        self.action_layout.addWidget(self.merge_btn)

    def _start_scan(self):
        self.table.setRowCount(0)
        self.merge_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.info_label.setText("Scanning library for video clips...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        self._scan_worker = _ScanWorker(self.clips_dir)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_scan_progress(self, cur: int, total: int, filename: str):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(cur)
            self.info_label.setText(f"Scanning clip {cur}/{total}: {filename}")

    def _on_scan_finished(self, pairs: list[OverlapPair]):
        self.pairs = pairs
        self.progress_bar.setVisible(False)
        self.scan_btn.setEnabled(True)

        if not pairs:
            self.info_label.setText("No overlapping clips found in the library. All files are unique!")
            return

        total_saved_mb = sum(p.estimated_saved_bytes for p in pairs) / (1024 * 1024)
        self.info_label.setText(
            f"Found {len(pairs)} overlapping clip pairs. "
            f"Potential disk savings: {total_saved_mb:.1f} MB. "
            "Double-click any clip to preview."
        )
        self.merge_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)

        self.table.setRowCount(len(pairs))
        for row, pair in enumerate(pairs):
            chk = QCheckBox()
            chk.setChecked(True)
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, chk_widget)

            item1 = QTableWidgetItem(pair.first.path.name)
            item1.setToolTip(f"Clip 1: {pair.first.path}\nDouble-click to preview")
            self.table.setItem(row, 1, item1)

            item2 = QTableWidgetItem(pair.second.path.name)
            item2.setToolTip(f"Clip 2: {pair.second.path}\nDouble-click to preview")
            self.table.setItem(row, 2, item2)

            overlap_sec = int(pair.overlap_seconds)
            saved_mb = pair.estimated_saved_bytes / (1024 * 1024)
            info_text = f"{overlap_sec}s overlap (~{saved_mb:.1f} MB)"
            item_info = QTableWidgetItem(info_text)
            self.table.setItem(row, 3, item_info)

            # Preview button in row
            btn_prev = QPushButton("▶ PREVIEW")
            btn_prev.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {Colors.ACCENT};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 3px;
                    padding: 3px 6px;
                    font-family: {Fonts.DISPLAY};
                    font-size: 10px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: {Colors.ACCENT_SOFT};
                    border-color: {Colors.ACCENT};
                }}
            """)
            btn_prev.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_prev.clicked.connect(lambda _, p=pair: self._open_preview(p, initial_clip=1))

            prev_widget = QWidget()
            prev_layout = QHBoxLayout(prev_widget)
            prev_layout.addWidget(btn_prev)
            prev_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            prev_layout.setContentsMargins(2, 2, 2, 2)
            self.table.setCellWidget(row, 4, prev_widget)

    def _on_selection_changed(self):
        has_sel = len(self.table.selectionModel().selectedRows()) > 0
        self.preview_btn.setEnabled(has_sel or len(self.pairs) > 0)

    def _on_cell_double_clicked(self, row: int, col: int):
        if 0 <= row < len(self.pairs):
            initial = 2 if col == 2 else 1
            self._open_preview(self.pairs[row], initial_clip=initial)

    def _preview_selected_row(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if selected_rows:
            row = selected_rows[0].row()
            if 0 <= row < len(self.pairs):
                self._open_preview(self.pairs[row], initial_clip=1)
        elif self.pairs:
            self._open_preview(self.pairs[0], initial_clip=1)

    def _open_preview(self, pair: OverlapPair, initial_clip: int = 1):
        dlg = ClipPreviewDialog(pair, initial_clip=initial_clip, parent=self)
        try:
            dlg.exec()
        finally:
            dlg._teardown()
            dlg.deleteLater()

    def _start_merge(self):
        selected_pairs: list[OverlapPair] = []
        for row in range(self.table.rowCount()):
            widget = self.table.cellWidget(row, 0)
            if widget:
                chk = widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    selected_pairs.append(self.pairs[row])

        if not selected_pairs:
            return

        self.merge_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(selected_pairs))
        self.progress_bar.setValue(0)

        remove_orig = self.delete_checkbox.isChecked()
        self._merge_worker = _MergeWorker(selected_pairs, remove_orig)
        self._merge_worker.progress.connect(self._on_merge_progress)
        self._merge_worker.finished.connect(self._on_merge_finished)
        self._merge_worker.start()

    def _on_merge_progress(self, cur: int, total: int, status: str):
        self.progress_bar.setValue(cur)
        self.info_label.setText(status)

    def _on_merge_finished(self, success_count: int, bytes_saved: int):
        self.progress_bar.setVisible(False)
        self.scan_btn.setEnabled(True)
        saved_mb = bytes_saved / (1024 * 1024)
        if success_count > 0:
            self.info_label.setText(
                f"Successfully merged {success_count} overlapping clip pair(s)! "
                f"Recovered {saved_mb:.1f} MB disk space."
            )
            self.deduplication_completed.emit(success_count, bytes_saved)
        else:
            self.info_label.setText(
                "Merge could not be completed. Check console/logs for details."
            )
        self.table.setRowCount(0)
        self.merge_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)

    def _teardown_workers(self):
        if getattr(self, '_workers_torn_down', False):
            return
        self._workers_torn_down = True
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self._scan_worker.wait(1000)
        if self._merge_worker and self._merge_worker.isRunning():
            self._merge_worker.cancel()
            self._merge_worker.wait(1000)

    def reject(self):
        self._teardown_workers()
        super().reject()

    def accept(self):
        self._teardown_workers()
        super().accept()

    def done(self, r):
        self._teardown_workers()
        super().done(r)

    def closeEvent(self, event):  # noqa: N802
        self._teardown_workers()
        super().closeEvent(event)
