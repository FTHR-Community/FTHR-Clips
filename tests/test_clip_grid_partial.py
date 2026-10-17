"""AUDIT-028 library and thumbnail integration guards."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))
pytest.importorskip('PySide6.QtCore')

from ui import clip_grid


@pytest.fixture(autouse=True)
def _cleanup_clip_grids(
    monkeypatch: pytest.MonkeyPatch, qapp
) -> None:
    """Finish each test's library workers before its monkeypatches expire."""
    from PySide6.QtCore import QCoreApplication, QEvent

    grids: list[clip_grid.ClipGrid] = []
    original_init = clip_grid.ClipGrid.__init__

    def tracked_init(grid: clip_grid.ClipGrid, *args, **kwargs) -> None:
        original_init(grid, *args, **kwargs)
        grids.append(grid)

    monkeypatch.setattr(clip_grid.ClipGrid, '__init__', tracked_init)
    yield

    for grid in reversed(grids):
        grid.shutdown()
        grid.deleteLater()

    # Deferred destruction is delivered while this fixture (and therefore
    # every test's monkeypatch fixture) is still active.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_responsive_grid_fills_viewport_without_phantom_gap() -> None:
    viewport = 1920
    columns, widths = clip_grid.ClipGrid._layout_metrics(viewport)
    usable = viewport - clip_grid.ClipGrid._H_MARGIN

    assert columns == len(widths)
    assert all(clip_grid._CARD_MIN_W <= width <= clip_grid._CARD_MAX_W
               for width in widths)
    assert (sum(widths)
            + clip_grid.ClipGrid._GRID_SPACING * (columns - 1)) == usable


def test_responsive_grid_can_reduce_columns_on_narrow_viewports() -> None:
    columns, widths = clip_grid.ClipGrid._layout_metrics(560)

    assert columns >= 1
    assert all(width >= clip_grid._CARD_MIN_W for width in widths)


def test_show_in_file_manager_uses_explorer_select_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'FTHR Clips' / 'clip with spaces.mp4'
    calls: list[object] = []

    monkeypatch.setattr(clip_grid.subprocess, 'Popen', calls.append)

    clip_grid._show_in_file_manager(str(target))

    expected_path = os.path.abspath(os.path.normpath(str(target)))
    assert calls == [f'explorer.exe /select,"{expected_path}"']


def test_portrait_thumbnail_is_fitted_without_distortion(qapp, tmp_path: Path) -> None:
    from PySide6.QtGui import QImage

    source = tmp_path / 'portrait.png'
    image = QImage(900, 1600, QImage.Format.Format_RGB32)
    assert image.save(str(source))

    card = clip_grid.ClipThumbnail(
        str(source), is_video=False, card_width=320)
    rendered = card.thumb_label.pixmap()

    assert rendered is not None
    assert rendered.height() == 180
    assert abs(rendered.width() / rendered.height() - 16 / 9) < 0.01


def test_editor_open_uses_source_thumbnail_not_card_scaled_pixmap(
    qapp, tmp_path: Path
) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPixmap

    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'placeholder')
    card = clip_grid.ClipThumbnail(str(source), is_video=True, card_width=320)
    card._thumbnail_ready = True
    card._thumb_pixmap = QPixmap(640, 360)
    card.thumb_label.setPixmap(QPixmap(320, 180))

    opened: list[tuple[str, QPixmap, QRect]] = []
    card.opened.connect(lambda path, pixmap, rect: opened.append((path, pixmap, rect)))
    card._emit_opened()

    assert len(opened) == 1
    assert opened[0][1].size() == card._thumb_pixmap.size()


def test_uploaded_card_copy_and_open_link_actions(qapp, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / 'uploaded.png'
    source.write_bytes(b'not-decoded-in-this-test')
    url = 'https://files.catbox.moe/example.mp4'
    card = clip_grid.ClipThumbnail(
        str(source), is_video=False, upload_info={'url': url})

    assert not card._link_actions.isHidden()
    assert card.copy_link_btn.isEnabled()
    assert card.open_link_btn.isEnabled()

    qapp.clipboard().clear()
    card.copy_link_btn.click()
    assert qapp.clipboard().text() == url

    opened: list[str] = []
    monkeypatch.setattr(
        clip_grid.QDesktopServices,
        'openUrl',
        lambda target: opened.append(target.toString()) or True,
    )
    card.open_link_btn.click()
    assert opened == [url]


def test_unuploaded_card_disables_link_actions(qapp, tmp_path: Path) -> None:
    source = tmp_path / 'local.png'
    source.write_bytes(b'not-decoded-in-this-test')
    card = clip_grid.ClipThumbnail(str(source), is_video=False)

    assert card._link_actions.isHidden()


def test_library_worker_discovers_mp4_but_not_partial(tmp_path: Path) -> None:
    completed = tmp_path / 'foo.mp4'
    partial = tmp_path / 'foo.mp4.partial'
    completed.write_bytes(b'complete')
    partial.write_bytes(b'partial')
    received: list[tuple[set[str], object, object, object]] = []
    worker = clip_grid._FileCollectWorker(str(tmp_path), [], 'all', 'newest')
    worker.signals.finished.connect(
        lambda found, pairs, imported, subdirs: received.append(
            (found, pairs, imported, subdirs)))

    worker.run()

    assert received
    found = received[0][0]
    assert str(completed) in found
    assert str(partial) not in found


def test_library_worker_applies_each_clip_category(tmp_path: Path) -> None:
    clips_dir = tmp_path / 'clips'
    imports_dir = tmp_path / 'imports'
    clips_dir.mkdir()
    imports_dir.mkdir()
    local_clip = clips_dir / 'local.mp4'
    screenshot = clips_dir / 'screen.png'
    imported_clip = imports_dir / 'linked.mp4'
    for path in (local_clip, screenshot, imported_clip):
        path.write_bytes(b'media')

    expected = {
        'all': {str(local_clip), str(screenshot), str(imported_clip)},
        'clips': {str(local_clip), str(imported_clip)},
        'screenshots': {str(screenshot)},
        'imported': {str(imported_clip)},
    }
    for category, expected_paths in expected.items():
        received: list[object] = []
        worker = clip_grid._FileCollectWorker(
            str(clips_dir), [str(imports_dir)], category, 'newest')
        worker.signals.finished.connect(
            lambda _found, pairs, _imported, _subdirs, received=received:
                received.append({path for _mtime, path in pairs}))

        worker.run()

        assert received == [expected_paths]


def test_library_worker_ignores_fthr_post_processing_directory(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / '.fthr-finalize-123'
    temp_dir.mkdir()
    transient = temp_dir / 'output.mp4'
    transient.write_bytes(b'in-progress')

    received: list[tuple[set[str], object, object, object]] = []
    worker = clip_grid._FileCollectWorker(str(tmp_path), [], 'all', 'newest')
    worker.signals.finished.connect(
        lambda found, pairs, imported, subdirs: received.append(
            (found, pairs, imported, subdirs)))

    worker.run()

    assert received
    assert str(transient) not in received[0][0]


def test_screenshot_empty_state_names_the_hotkey(qapp, tmp_path: Path) -> None:
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path),
            'hotkeys': {'save_screenshot': 'CTRL+F11'},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)

    grid._filter = 'screenshots'
    grid._show_empty(True)

    assert grid._no_clips_lbl.text() == 'No screenshots yet'
    assert grid._empty_detail_lbl.text() == 'PRESS CTRL+F11 TO TAKE A SCREENSHOT'
    assert not grid._empty_detail_lbl.isHidden()


def test_category_results_restore_a_visible_grid_without_opacity_effect(
        qapp, tmp_path: Path) -> None:
    image_path = tmp_path / 'screenshot.png'
    from PySide6.QtGui import QImage
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    assert image.save(str(image_path))
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path), 'hotkeys': {},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    grid._filter = 'screenshots'
    grid._on_files_collected_for_transition(
        {str(image_path)}, [(image_path.stat().st_mtime, str(image_path))],
        set(), [])

    assert not grid._sections_host.isHidden()
    assert grid._sections_host.graphicsEffect() is None
    assert len(grid.thumbnails) == 1


def test_category_refresh_keeps_worker_alive_until_completion(
        qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path),
            'imported_clip_folders': [],
            'hotkeys': {},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    started: list[object] = []
    monkeypatch.setattr(grid._scan_thread_pool, 'start', started.append)

    grid._on_filter_changed(2)

    assert grid._filter == 'screenshots'
    assert len(started) == 1
    worker = started[0]
    assert worker in grid._transition_workers.values()

    worker.signals.finished.emit(set(), [], set(), [])
    assert worker not in grid._transition_workers.values()
    assert grid._all_count_label.text() == 'SCREENSHOTS'


def test_short_section_cards_are_left_aligned(qapp, tmp_path: Path) -> None:
    from PySide6.QtGui import QImage

    image_path = tmp_path / 'screenshot.png'
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    assert image.save(str(image_path))
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path), 'hotkeys': {},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    grid._clear_sections()
    grid.thumbnails.clear()
    grid._thumb_widgets.clear()
    grid._add_section('TODAY', [str(image_path)], 0)

    section = grid._sections_layout.itemAt(0).widget()
    card_grid = section.layout().itemAt(1).layout()
    alignment = card_grid.itemAtPosition(0, 0).alignment()

    assert alignment & clip_grid.Qt.AlignmentFlag.AlignLeft
    assert alignment & clip_grid.Qt.AlignmentFlag.AlignTop


def test_saved_clip_is_added_and_finalized_without_a_full_rebuild(
        qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_clip = tmp_path / 'old.mp4'
    new_clip = tmp_path / 'new.mp4'
    old_clip.write_bytes(b'old')
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path), 'hotkeys': {},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    old_card = grid._thumb_widgets[str(old_clip)]
    new_clip.write_bytes(b'new')

    monkeypatch.setattr(
        grid, '_collect_media_files',
        lambda: pytest.fail('incremental save update performed a directory scan'))
    monkeypatch.setattr(
        grid, '_clear_sections',
        lambda: pytest.fail('incremental save update rebuilt every section'))
    started_workers: list[object] = []
    monkeypatch.setattr(grid._thread_pool, 'start', started_workers.append)

    grid.upsert_saved_clip(str(new_clip), ready=False)

    new_card = grid._thumb_widgets[str(new_clip)]
    assert grid._thumb_widgets[str(old_clip)] is old_card
    assert new_card.ready is False
    assert str(new_clip) in grid._known_files
    assert started_workers == []

    grid.upsert_saved_clip(str(new_clip), ready=True)

    assert grid._thumb_widgets[str(new_clip)] is new_card
    assert new_card.ready is True
    assert new_card._finalizing_badge is None
    assert len(started_workers) == 1


def test_rapid_saved_clip_upserts_keep_existing_cards_and_order(
        qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / 'first.mp4'
    second = tmp_path / 'second.mp4'
    third = tmp_path / 'third.mp4'
    first.write_bytes(b'clip')
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            'clips_directory': str(tmp_path), 'hotkeys': {},
        }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    first_card = grid._thumb_widgets[str(first)]
    second.write_bytes(b'clip')
    third.write_bytes(b'clip')
    started_workers: list[object] = []
    monkeypatch.setattr(grid._thread_pool, 'start', started_workers.append)

    # Model two rapid native completions. Neither update may rescan or
    # recreate the card that was already visible when the saves arrived.
    grid.upsert_saved_clip(str(second), ready=False)
    second_card = grid._thumb_widgets[str(second)]
    grid.upsert_saved_clip(str(third), ready=False)
    third_card = grid._thumb_widgets[str(third)]
    grid.upsert_saved_clip(str(second), ready=True)
    grid.upsert_saved_clip(str(third), ready=True)

    assert grid._thumb_widgets[str(first)] is first_card
    assert grid._thumb_widgets[str(second)] is second_card
    assert grid._thumb_widgets[str(third)] is third_card
    assert all(grid._thumb_widgets[str(path)].ready
               for path in (second, third))
    assert len(started_workers) == 2


def test_thumbnail_worker_rejects_partial_before_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / 'foo.mp4.partial'
    partial.write_bytes(b'partial')
    decoder_called = False

    def unexpected_decoder(*_args) -> object:
        nonlocal decoder_called
        decoder_called = True
        raise AssertionError('partial file reached thumbnail decoder')

    monkeypatch.setattr(clip_grid, '_decode_thumbnail_with_owned_process', unexpected_decoder)
    received: list[tuple[str, str, int]] = []
    worker = clip_grid._ThumbnailWorker(str(partial))
    worker.signals.finished.connect(
        lambda path, cache, duration: received.append((path, cache, duration)))

    worker.run()

    assert not decoder_called
    assert received == [(str(partial), '', 0)]


def test_thumbnail_failure_does_not_invalidate_the_saved_clip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = tmp_path / 'saved.mp4'
    saved.write_bytes(b'valid-core-clip')
    monkeypatch.setattr(
        clip_grid, '_decode_thumbnail_with_owned_process',
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError('injected thumbnail decoder failure')))
    monkeypatch.setattr(clip_grid, '_probe_with_owned_process', lambda *_: None)
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path / 'cache'))
    received: list[tuple[str, str, int]] = []
    worker = clip_grid._ThumbnailWorker(str(saved))
    worker.signals.finished.connect(
        lambda path, cache, duration: received.append((path, cache, duration)))

    worker.run()

    assert saved.exists()
    assert received == [(str(saved), '', 0)]


def test_metadata_enrichment_failure_does_not_invalidate_the_saved_clip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = tmp_path / 'saved.mp4'
    saved.write_bytes(b'valid-core-clip')

    monkeypatch.setattr(
        clip_grid, '_probe_with_owned_process',
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError('injected metadata probe failure')))
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path / 'cache'))

    received: list[tuple[str, str, int]] = []
    worker = clip_grid._ThumbnailWorker(str(saved))
    worker.signals.finished.connect(
        lambda path, cache, duration: received.append((path, cache, duration)))

    worker.run()

    assert saved.exists()
    assert received == [(str(saved), '', 0)]
