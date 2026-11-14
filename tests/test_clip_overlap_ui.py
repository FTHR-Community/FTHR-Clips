"""Overlap affordance, async discovery ownership and full editor handoff."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtGui import QContextMenuEvent, QPixmap
from PySide6.QtWidgets import QMenu, QWidget, QMessageBox

from ui.clip_grid import ClipGrid, ClipThumbnail
from ui.clip_overlap import OverlapDiscovery, _Discovery


def card_for(qtbot, path='first.mp4'):
    card = ClipThumbnail(path, defer_image_load=True, card_width=240)
    qtbot.addWidget(card)
    return card


def test_badge_requires_proof_ready_and_unlocked(qtbot):
    card = card_for(qtbot)
    assert card._overlap_button.isHidden()
    calls = []
    card.overlap_requested.connect(calls.append)
    card.set_overlap_paths(('second.mp4',))
    assert not card._overlap_button.isHidden()
    assert card._overlap_button.accessibleName() == 'Merge overlapping clips'
    assert not card._overlap_button.icon().isNull()
    card._overlap_button.click()
    assert calls == ['first.mp4']
    card.set_ready(False)
    assert card._overlap_button.isHidden()
    card._request_overlap_merge()
    card.set_ready(True)
    card.set_selection_state(False, True, locked=True)
    assert not card._overlap_button.isEnabled()
    card._request_overlap_merge()
    assert calls == ['first.mp4']


def test_recycled_badge_does_not_keep_partner(qtbot):
    card = card_for(qtbot)
    card.set_overlap_paths(('second.mp4',))
    card.rebind('other.mp4', imported=False, upload_enabled=False,
                uploaded=False, upload_info=None, ready=True, card_width=240)
    assert not card._overlap_paths
    assert card._overlap_button.isHidden()


def test_context_action_only_for_proven_overlap(qtbot, monkeypatch):
    card = card_for(qtbot)
    labels = []
    calls = []
    card.overlap_requested.connect(calls.append)
    def choose(menu, *_):
        labels[:] = [a.text() for a in menu.actions()]
        return next((a for a in menu.actions() if a.text() == 'Merge overlapping clips…'), None)
    class TestMenu(QMenu):
        exec = choose
    monkeypatch.setattr('ui.clip_grid.QMenu', TestMenu)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, card.pos(), card.pos())
    card.contextMenuEvent(event)
    assert 'Merge overlapping clips…' not in labels
    card.set_overlap_paths(('second.mp4',))
    card.contextMenuEvent(event)
    assert calls == ['first.mp4']
    card.set_ready(False)
    labels.clear()
    card.contextMenuEvent(event)
    assert labels == []


def test_discovery_rejects_stale_and_coalesces(qapp, monkeypatch):
    discovery = OverlapDiscovery()
    starts, results = [], []
    monkeypatch.setattr(discovery.pool, 'start', starts.append)
    discovery.changed.connect(results.append)
    records = [SimpleNamespace(kind='video', path='a')]
    discovery.update(records)
    first = discovery.active
    discovery.update(records)
    discovery.update(records)
    assert first.cancel.is_set()
    assert len(starts) == 1
    discovery._finished(first.generation, {'stale': ('b',)}, {})
    assert len(starts) == 2
    assert discovery.active.generation == discovery.generation
    assert all('stale' not in result for result in results)
    discovery._finished(discovery.generation, {'a': ('b',)}, {'identity': None})
    assert results[-1] == {'a': ('b',)}
    discovery.shutdown()


def test_worker_probes_all_videos_off_thread_and_reuses_identity(qapp, monkeypatch):
    import core.clip_overlap as core
    calls = []
    monkeypatch.setattr(core, 'probe_clip', lambda path, cancel=None: calls.append((path, threading.get_ident())) or path)
    monkeypatch.setattr(core, 'find_overlaps', lambda infos: {'all': tuple(infos)})
    discovery = OverlapDiscovery()
    results = []
    discovery.changed.connect(results.append)
    records = [SimpleNamespace(kind='video', path=str(i), st_dev=1, st_ino=i,
                               size=10, mtime_ns=20) for i in range(3)]
    worker = _Discovery(1, records, {}, discovery.signals)
    captured = []
    worker.signals.finished.connect(lambda *args: captured.append(args))
    thread = threading.Thread(target=worker.run)
    thread.start(); thread.join(5)
    qapp.processEvents()
    assert len(calls) == 3
    assert all(ident != threading.get_ident() for _, ident in calls)
    assert captured[0][1] == {'all': ('0', '1', '2')}
    second = _Discovery(2, records, captured[0][2], discovery.signals)
    second.run()
    assert len(calls) == 3
    records[0].size = 11
    _Discovery(3, records, captured[0][2], discovery.signals).run()
    assert len(calls) == 4
    discovery.shutdown()


def main_method(name):
    # Importing main boots platform services; compile the actual method alone
    # against a fake window to verify its branch and editor-session contract.
    source = Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    method = next(node for cls in tree.body if isinstance(cls, ast.ClassDef)
                  for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    import os
    namespace = {'QPixmap': QPixmap, 'QTimer': QTimer, 'QMessageBox': QMessageBox, 'os': os}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[name]


def test_merge_completion_only_opens_success_and_refreshes(qapp):
    from core.export_lifecycle import ExportResult, ExportState
    opened, errors, refreshed = [], [], []
    window = SimpleNamespace(clip_grid=SimpleNamespace(_load_clips=lambda: refreshed.append(True)),
                             _open_merged_clip=opened.append,
                             push_error=lambda *args: errors.append(args))
    poll = main_method('_poll_overlap_merge')
    timer = SimpleNamespace(stop=lambda: None, deleteLater=lambda: None)
    for state in (ExportState.COMPLETED, ExportState.CANCELLED, ExportState.FAILED):
        task = SimpleNamespace(done=True, output='new.mp4', detail='bad media', result=ExportResult(state))
        poll(window, task, timer)
    assert opened == ['new.mp4']
    assert refreshed == [True]
    assert errors == [('MERGE FAILED', 'bad media')]


def test_merge_uses_full_editor_session(qapp, tmp_path):
    opened = []
    path = tmp_path / 'new.mp4'
    path.write_bytes(b'merged')
    window = SimpleNamespace(_editor_sessions=[], _active_clip_viewer=None,
                             _editor_trash_pending=lambda: False,
                             _add_editor_session=lambda path: ('session', path),
                             _show_editor_session=lambda session, pixmap: opened.append(session))
    main_method('_open_merged_clip')(window, str(path))
    assert opened == [('session', str(path))]


def test_render_isolated_overlap_card(qtbot, tmp_path):
    """Synthetic media only; an opt-in persistent path supports visual review."""
    import os
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtGui import QFontDatabase
    for font in (Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'assets' / 'fonts').glob('*.ttf'):
        QFontDatabase.addApplicationFont(str(font))
    card = card_for(qtbot, str(tmp_path / 'Arena replay.mp4'))
    image = QPixmap(480, 270)
    image.fill(QColor('#203b48'))
    painter = QPainter(image)
    painter.fillRect(100, 80, 220, 90, QColor('#3b6068'))
    painter.end()
    card._thumb_pixmap = image
    card.set_source_duration(42)
    card.metadata_label.setText('1920 × 1080 · 60 fps')
    card.set_overlap_paths(('partner.mp4',))
    card.resize_card(300, force=True)
    card.show()
    qtbot.wait(20)
    output = os.environ.get('FTHR_OVERLAP_SCREENSHOT', str(tmp_path / 'overlap-card.png'))
    assert card.grab().save(output)


def test_partner_picker_and_readiness(qapp, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ClipGrid, '_load_clips', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    grid = ClipGrid(settings)
    qtbot.addWidget(grid)
    first, second, third = (str(tmp_path / name) for name in ('first.mp4', 'second.mp4', 'third.mp4'))
    grid._overlaps = {first: (second, third)}
    grid._create_thumbnail(first, 240, ready_override=True)
    grid._records_by_path = {path: object() for path in (first, second, third)}
    chosen = []
    grid.overlap_merge_requested.connect(chosen.append)
    class Picker(QMenu):
        def exec(self, *_):
            return self.actions()[-1]
    monkeypatch.setattr('ui.clip_grid.QMenu', Picker)
    grid._request_overlap_merge(first)
    assert chosen == [(first, third)]
    grid._readiness_checker = lambda path: path == first
    grid._request_overlap_merge(first)
    assert chosen == [(first, third)]
    grid.shutdown()


def test_new_scan_invalidates_discovery_without_publishing_stale(qapp, monkeypatch):
    discovery = OverlapDiscovery()
    starts, results = [], []
    monkeypatch.setattr(discovery.pool, 'start', starts.append)
    discovery.changed.connect(results.append)
    discovery.update([])
    worker = discovery.active
    discovery.invalidate()
    discovery._finished(worker.generation, {'stale': ('other',)}, {})
    assert worker.cancel.is_set()
    assert results == [{}]
    discovery.shutdown()


# Reuse synthetic tagged source generation, never the user's library.
import test_clip_overlap

overlapping_media = test_clip_overlap.overlapping_media


def test_real_qt_discovery_badge_merge_and_full_editor(qapp, qtbot, overlapping_media, tmp_path, monkeypatch):
    import hashlib
    from core import clip_overlap as overlap, export_encoding
    from core.export_lifecycle import ExportState
    import ui.export_jobs
    monkeypatch.setattr(ClipGrid, '_load_clips', lambda *_: None)
    monkeypatch.setattr(ui.export_jobs, 'show_export_jobs', lambda: None)
    # Force the production software encoder; avoid machine-specific GPU setup.
    monkeypatch.setattr(export_encoding, 'choose_video_args',
                        lambda ffmpeg, **kwargs: export_encoding.software_args('quality', ffmpeg))
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    grid = ClipGrid(settings)
    qtbot.addWidget(grid)
    paths = tuple(str(p) for p in overlapping_media)
    before = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths]
    grid._replace_records(grid._records_from_paths(set(paths)))
    card = grid._create_thumbnail(paths[0], 240, ready_override=True)
    qtbot.waitUntil(lambda: bool(card._overlap_paths), timeout=20000)
    assert card._overlap_paths == (paths[1],)
    assert not card._overlap_button.isHidden()
    tasks, opened, errors = [], [], []
    real_submit = overlap.submit_merge
    def submit(paths, directory):
        task = real_submit(paths, directory)
        tasks.append(task)
        return task
    monkeypatch.setattr(overlap, 'submit_merge', submit)
    class Window(QWidget):
        _on_overlap_merge_requested = main_method('_on_overlap_merge_requested')
        _poll_overlap_merge = main_method('_poll_overlap_merge')
        _open_merged_clip = main_method('_open_merged_clip')
        def _editor_trash_pending(self):
            return False
        def _add_editor_session(self, path):
            return ('editor', path)
        def _show_editor_session(self, session, pixmap):
            opened.append(session)
        def push_error(self, *args, **kwargs):
            errors.append(args)
    window = Window()
    qtbot.addWidget(window)
    window.clip_grid = grid
    window._clip_readiness = SimpleNamespace(can_access=lambda path: True)
    window._editor_sessions = []
    window._active_clip_viewer = None
    grid.overlap_merge_requested.connect(window._on_overlap_merge_requested)
    card._overlap_button.click()
    assert len(tasks) == 1
    task = tasks[0]
    try:
        qtbot.waitUntil(lambda: bool(opened) or bool(errors), timeout=60000)
        assert not errors, errors
        assert task.done and task.result.state == ExportState.COMPLETED
        assert opened == [('editor', task.output)]
        assert task.output not in paths
        assert Path(task.output).parent == tmp_path / 'Merged'
        result = overlap.probe_clip(task.output)
        assert result is not None and result.timeline.duration == 6
        assert [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths] == before
    finally:
        task.cancel.set()
        task.thread.join(5)
        grid.shutdown()


def test_merged_editor_guards_preserve_saved_output(qapp, tmp_path):
    path = tmp_path / 'merged.mp4'
    path.write_bytes(b'merged')
    messages, refreshed, opened = [], [], []
    window = SimpleNamespace(_editor_sessions=[], _active_clip_viewer=None,
                             _ultra_performance=True,
                             _editor_trash_pending=lambda: False,
                             _add_editor_session=lambda path: opened.append(path),
                             clip_grid=SimpleNamespace(_load_clips=lambda: refreshed.append(True)),
                             push_error=lambda *args, **kwargs: messages.append(args))
    open_clip = main_method('_open_merged_clip')
    open_clip(window, str(path))
    assert messages[-1][0] == 'MERGED CLIP SAVED'
    assert path.read_bytes() == b'merged' and not opened
    path.unlink()
    open_clip(window, str(path))
    assert messages[-1][0] == 'MERGED CLIP NOT FOUND'
    assert refreshed == [True]


def test_merge_request_blocked_during_trash_or_ultra_performance(qapp):
    request = main_method('_on_overlap_merge_requested')
    # Any attempt to access queue/editor state would fail on this minimal fake.
    request(SimpleNamespace(_ultra_performance=True), ('a', 'b'))
    request(SimpleNamespace(_editor_trash_pending=lambda: True), ('a', 'b'))
