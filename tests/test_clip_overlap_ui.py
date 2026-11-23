"""Overlap affordance, async discovery ownership and full editor handoff."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtGui import QContextMenuEvent, QPixmap
from PySide6.QtWidgets import QMenu, QWidget, QMessageBox

from ui.clip_grid import ClipGrid, ClipThumbnail
from ui.clip_overlap import OverlapDiscovery


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


def _record(path, size=10, inode=1):
    return SimpleNamespace(kind='video', path=path, st_dev=1, st_ino=inode,
                           size=size, mtime_ns=20)


def test_discovery_rejects_stale_and_coalesces(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('ui.clip_overlap._RESUME_DELAY_MS', 1)
    discovery = OverlapDiscovery(cache_path=tmp_path / 'overlap.sqlite3')
    starts, results = [], []
    monkeypatch.setattr(discovery.pool, 'start', starts.append)
    discovery.changed.connect(results.append)
    records = [_record('a.mp4')]
    discovery.update(records)
    qtbot.waitUntil(lambda: len(starts) == 1)
    first = discovery.active
    discovery.update(records)
    assert not first.cancel.is_set() and discovery.active is first
    discovery.update([_record('a.mp4', size=11), _record('b.mp4')])
    discovery.update([_record('c.mp4'), _record('d.mp4')])
    assert first.cancel.is_set()
    assert len(starts) == 1
    discovery._finished(first.generation, {'a.mp4': None}, [], {'stale': ('b',)})
    qtbot.waitUntil(lambda: len(starts) == 2)
    assert discovery.active.generation == discovery.generation
    assert [path for path, _ in discovery.active.records] == ['c.mp4', 'd.mp4']
    assert not results and not discovery.cache
    discovery._finished(discovery.generation, {'c.mp4': None, 'd.mp4': None}, [], None)
    assert set(discovery.cache) == {'c.mp4', 'd.mp4'}
    discovery.shutdown()


def test_discovery_prioritizes_visible_work_and_resumes_after_cancellation(qtbot, tmp_path, monkeypatch):
    import core.clip_overlap as core
    monkeypatch.setattr('ui.clip_overlap._RESUME_DELAY_MS', 1)
    monkeypatch.setattr('ui.clip_overlap._PROBE_SPACING_MS', 1)
    calls, worker_ids = [], []

    def inspect(path, cancel, **_options):
        calls.append(path)
        worker_ids.append(threading.get_ident())
        if len(calls) == 1:
            assert cancel.wait(5)
            return core.ClipInspection(probed=True)
        return core.ClipInspection(complete=True, probed=True)

    monkeypatch.setattr(core, 'inspect_clip', inspect)
    discovery = OverlapDiscovery(cache_path=tmp_path / 'overlap.sqlite3')
    records = [_record(f'{index}.mp4', inode=index) for index in range(3)]
    discovery.set_paused(True)
    discovery.update(records)
    discovery.prioritize(['2.mp4'])
    try:
        qtbot.wait(20)
        assert not calls
        discovery.set_paused(False)
        qtbot.waitUntil(lambda: len(calls) == 1)
        recovery = discovery.active
        discovery.set_scrolling(True)
        qtbot.waitUntil(lambda: discovery.active is None)
        assert recovery.cancel.is_set() and calls == ['2.mp4'] and not discovery.cache
        discovery.set_scrolling(False)
        qtbot.waitUntil(lambda: len(discovery.cache) == 3)
        assert calls == ['2.mp4', '2.mp4', '0.mp4', '1.mp4']
        assert all(ident != threading.get_ident() for ident in worker_ids)
        discovery.update(records)
        qtbot.wait(20)
        assert len(calls) == 4
    finally:
        discovery.shutdown()
        discovery.pool.waitForDone(1000)


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


def test_changed_identity_clears_badges_and_failed_inspection_retries(qtbot, tmp_path, monkeypatch):
    import core.clip_overlap as core
    monkeypatch.setattr('ui.clip_overlap._RESUME_DELAY_MS', 1)
    monkeypatch.setattr('ui.clip_overlap._PROBE_SPACING_MS', 1)
    monkeypatch.setattr('ui.clip_overlap._RETRY_SECONDS', .05)
    calls, results = [], []

    def inspect(path, _cancel, **_options):
        calls.append(path)
        if calls == ['first.mp4']:
            return core.ClipInspection(probed=True)
        info = test_clip_overlap.clip(path, 0, 10) if calls.count(path) <= 2 else None
        return core.ClipInspection(info, complete=True, probed=True)

    monkeypatch.setattr(core, 'inspect_clip', inspect)
    discovery = OverlapDiscovery(cache_path=tmp_path / 'overlap.sqlite3')
    discovery.changed.connect(results.append)
    try:
        discovery.update([_record('first.mp4'), _record('second.mp4')])
        qtbot.waitUntil(lambda: len(discovery.cache) == 2)
        assert calls == ['first.mp4', 'second.mp4', 'first.mp4']
        assert results[-1] == {'first.mp4': ('second.mp4',), 'second.mp4': ('first.mp4',)}
        discovery.update([_record('first.mp4', inode=2), _record('second.mp4')])
        assert results[-1] == {}
        qtbot.waitUntil(lambda: len(discovery.cache) == 2)
        assert discovery.cache['first.mp4'] is None
        assert calls == ['first.mp4', 'second.mp4', 'first.mp4', 'first.mp4']
    finally:
        discovery.shutdown()
        discovery.pool.waitForDone(1000)


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
