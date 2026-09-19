"""deduplication_dialog.py
Dialog for finding and losslessly merging overlapping clips to free disk space.
Includes interactive video preview to inspect and compare overlapping clips.
"""

from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QUrl, Qt, QThread, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices
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
    QWidget,
)

from core.clip_deduplicator import (
    OverlapPair,
    find_overlapping_clusters,
    merge_clip_cluster,
    merge_overlapping_pair,
    scan_clip_records,
)
from ui.dialogs import FthrDialog, FthrMessageDialog
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

        self.clip_buttons: list[QPushButton] = []
        clips = self.pair.clips or [self.pair.first, self.pair.second]
        for idx, clip in enumerate(clips, start=1):
            name = clip.path.name
            if len(name) > 30:
                name = name[:28] + "…"
            btn = QPushButton(f"CLIP {idx}: {name}")
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setToolTip(str(clip.path))
            btn.clicked.connect(lambda _, i=idx: self.switch_clip(i))
            switch_row.addWidget(btn, 1)
            self.clip_buttons.append(btn)

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
        self.ext_btn.setToolTip("Open in default media player")
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
        clips = self.pair.clips or [self.pair.first, self.pair.second]
        if not (1 <= clip_idx <= len(clips)):
            return
        self.active_clip_idx = clip_idx
        path = clips[clip_idx - 1].path
        if not path.exists():
            return

        for idx, btn in enumerate(self.clip_buttons, start=1):
            if idx == clip_idx:
                btn.setStyleSheet(button_primary_qss())
            else:
                btn.setStyleSheet(button_outline_qss())

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
        clips = self.pair.clips or [self.pair.first, self.pair.second]
        if 1 <= self.active_clip_idx <= len(clips):
            path = clips[self.active_clip_idx - 1].path
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
            pairs = find_overlapping_clusters(records)
            self.finished.emit(pairs)
        else:
            self.finished.emit([])

    def cancel(self):
        self.cancel_event.set()


class _MergeWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(int, int, bool)  # success_count, total_bytes_saved, remove_originals

    def __init__(self, pairs: list[OverlapPair], remove_originals: bool):
        super().__init__()
        self.pairs = pairs
        self.remove_originals = remove_originals
        self.cancel_event = threading.Event()

    def run(self):
        success = 0
        bytes_saved = 0
        total = len(self.pairs)
        for idx, item in enumerate(self.pairs):
            if self.cancel_event.is_set():
                break
            name = item.first.path.name if item.first else f"Item {idx + 1}"
            clips = item.clips or [item.first, item.second]
            self.progress.emit(idx, total, f"Merging {idx + 1} of {total}: {name}...")
            try:
                if len(clips) > 2:
                    _, saved = merge_clip_cluster(
                        clips,
                        remove_originals=self.remove_originals,
                        cancel_event=self.cancel_event,
                    )
                    item.actual_saved_bytes = saved
                else:
                    merge_overlapping_pair(
                        item,
                        remove_originals=self.remove_originals,
                        cancel_event=self.cancel_event,
                    )
                success += 1
                bytes_saved += item.actual_saved_bytes if self.remove_originals else 0
            except Exception as e:
                print(f"[Deduplication] Failed to merge {name}: {e}")
                continue

        self.finished.emit(success, bytes_saved, self.remove_originals)

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

        self.delete_checkbox = QCheckBox("Move original overlapping clips to OS Recycle Bin / Trash after successful merge")
        self.delete_checkbox.setChecked(False)
        self.delete_checkbox.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
        self.delete_checkbox.clicked.connect(self._on_delete_checkbox_toggled)
        self.body_layout.addWidget(self.delete_checkbox)

        self.quarantine_hint = QLabel("Original clips are safely moved to your system Recycle Bin / Trash using send2trash.")
        self.quarantine_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_MICRO))
        self.body_layout.addWidget(self.quarantine_hint)

        self.disclaimer_label = QLabel(
            "Note: Clips are merged losslessly without re-encoding, taking only a few seconds per clip. "
            "A 1-2 second jump may appear at the stitch boundary."
        )
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

        has_chained = any(p.is_chained for p in pairs)
        has_inferred = any(p.confidence == 'LOW' for p in pairs)
        notes = []
        if has_chained:
            notes.append("chained overlaps detected")
        if has_inferred:
            notes.append("some timestamps are inferred")
        note_str = f" ({'; '.join(notes)})" if notes else ""

        total_saved_mb = sum(p.estimated_saved_bytes for p in pairs) / (1024 * 1024)
        item_word = "group(s)" if any(len(p.clips or []) > 2 for p in pairs) else "pair(s)"
        self.info_label.setText(
            f"Found {len(pairs)} overlapping clip {item_word}. "
            f"Estimated potential disk savings: {total_saved_mb:.1f} MB.{note_str} "
            "Double-click any clip to preview."
        )
        self.merge_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)

        self.table.setRowCount(len(pairs))
        for row, pair in enumerate(pairs):
            chk = QCheckBox()
            # LOW confidence items are unchecked by default to require explicit user selection
            chk.setChecked(pair.confidence != 'LOW')
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, chk_widget)

            clips = pair.clips or [pair.first, pair.second]

            item1 = QTableWidgetItem(pair.first.path.name)
            item1.setToolTip(f"Clip 1: {pair.first.path}\nConfidence: {pair.first.timestamp_confidence}\nDouble-click to preview")
            self.table.setItem(row, 1, item1)

            if len(clips) > 2:
                col2_text = f"{pair.second.path.name} (+{len(clips) - 2} more)"
                col2_tip = "\n".join(f"Clip {i}: {c.path.name}" for i, c in enumerate(clips[1:], start=2)) + "\nDouble-click to preview"
            else:
                col2_text = pair.second.path.name
                col2_tip = f"Clip 2: {pair.second.path}\nConfidence: {pair.second.timestamp_confidence}\nDouble-click to preview"

            item2 = QTableWidgetItem(col2_text)
            item2.setToolTip(col2_tip)
            self.table.setItem(row, 2, item2)

            overlap_sec = int(pair.overlap_seconds)
            saved_mb = pair.estimated_saved_bytes / (1024 * 1024)
            info_text = f"{overlap_sec}s overlap (~{saved_mb:.1f} MB)"
            if pair.confidence == 'LOW':
                info_text += " ⚠️ [Low Confidence]"
            elif pair.confidence != 'HIGH':
                info_text += " [Inferred]"
            if pair.is_chained:
                if len(clips) > 2:
                    info_text += f" [Chain: {len(clips)} clips]"
                else:
                    info_text += " [Chained]"
            item_info = QTableWidgetItem(info_text)
            if pair.confidence == 'LOW':
                item_info.setForeground(QColor(Colors.WARNING))
                item_info.setToolTip(
                    "⚠️ Low timestamp confidence!\n"
                    "File modification and creation times indicate this clip may have been copied or touched.\n"
                    "Merging requires explicit 'Force merge (Low Confidence)' confirmation.\n"
                    "Please preview before merging."
                )
            elif pair.confidence != 'HIGH':
                item_info.setForeground(QColor(Colors.WARNING))
                item_info.setToolTip(
                    "Timestamp was inferred from file modification date.\n"
                    "May be inaccurate if files were copied or touched.\n"
                    "Please preview before merging."
                )
            elif pair.is_chained:
                if len(clips) > 2:
                    item_info.setToolTip(
                        f"Overlap chain of {len(clips)} clips (e.g. A->B->C).\n"
                        "Merged seamlessly in a single FFmpeg pass."
                    )
                else:
                    item_info.setToolTip(
                        "Part of an overlap chain.\n"
                        "Merged seamlessly in a single FFmpeg pass."
                    )
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

    def _on_delete_checkbox_toggled(self, checked: bool):
        if checked:
            FthrMessageDialog.information(
                self,
                "RECYCLE BIN QUARANTINE ENABLED",
                "When enabled, original overlapping video files will be safely moved to your "
                "OS Recycle Bin / Trash rather than permanently deleted.\n\n"
                "You can restore any file from the Recycle Bin at any time."
            )

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

        remove_orig = self.delete_checkbox.isChecked()
        low_conf_pairs = [p for p in selected_pairs if p.confidence == 'LOW']

        # 1. Low confidence guard requiring explicit user confirmation
        if low_conf_pairs:
            msg = (
                f"⚠️ Warning: {len(low_conf_pairs)} selected item(s) have LOW timestamp confidence.\n\n"
                "Their modification and creation dates suggest they were copied or touched outside FTHR.\n"
                "Do you want to proceed with a Force Merge (Low Confidence)?"
            )
            if not FthrMessageDialog.question(self, "FORCE MERGE (LOW CONFIDENCE)?", msg):
                return

        # 2. Recycle Bin quarantine confirmation listing original files
        if remove_orig:
            msg_parts = [
                f"You have chosen to move original clips for {len(selected_pairs)} item(s) to the OS Recycle Bin / Trash.\n\n"
                "The following files will be safely moved:\n"
            ]
            all_files: list[Path] = []
            for item in selected_pairs:
                for c in (item.clips or [item.first, item.second]):
                    all_files.append(c.path)

            for p in all_files[:12]:
                msg_parts.append(f"  • {p.name}\n")
            if len(all_files) > 12:
                msg_parts.append(f"  ... and {len(all_files) - 12} more file(s)\n")
            msg_parts.append("\nDo you want to proceed?")
            if not FthrMessageDialog.question(self, "CONFIRM MOVE TO RECYCLE BIN", "".join(msg_parts)):
                return

        self.merge_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        if len(selected_pairs) == 1:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, len(selected_pairs))
            self.progress_bar.setValue(0)

        self._merge_worker = _MergeWorker(selected_pairs, remove_orig)
        self._merge_worker.progress.connect(self._on_merge_progress)
        self._merge_worker.finished.connect(self._on_merge_finished)
        self._merge_worker.start()

    def _on_merge_progress(self, cur: int, total: int, status: str):
        if total > 1:
            self.progress_bar.setValue(cur)
        self.info_label.setText(status)

    def _on_merge_finished(self, success_count: int, bytes_saved: int, removed: bool):
        self.progress_bar.setVisible(False)
        self.scan_btn.setEnabled(True)
        saved_mb = bytes_saved / (1024 * 1024)
        if success_count > 0:
            if removed:
                self.info_label.setText(
                    f"Successfully merged {success_count} overlapping clip item(s)! "
                    f"Confirmed {saved_mb:.1f} MB disk space recovered (originals safely moved to OS Recycle Bin / Trash)."
                )
            else:
                self.info_label.setText(
                    f"Successfully merged {success_count} overlapping clip item(s)! "
                    "Original files were preserved in place (no disk space freed)."
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
        for worker in (self._scan_worker, self._merge_worker):
            if worker and worker.isRunning():
                worker.cancel()
                if not worker.wait(2000):
                    worker.terminate()
                    worker.wait(1000)

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
