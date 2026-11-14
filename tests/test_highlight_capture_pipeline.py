from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

pytest.importorskip('PySide6.QtCore')
import main as main_module  # noqa: E402
from main import MainWindow  # noqa: E402


def _identity(path: Path) -> dict[str, int]:
    info = path.stat()
    return {
        'st_dev': int(info.st_dev), 'st_ino': int(info.st_ino),
        'size': int(info.st_size), 'mtime_ns': int(info.st_mtime_ns),
    }


def test_final_publication_refuses_replaced_path_before_grid_or_storage(
        tmp_path, monkeypatch):
    clip = tmp_path / 'saved.mp4'
    clip.write_bytes(b'committed')
    old = _identity(clip)
    clip.write_bytes(b'replacement')
    failures = []

    class _Readiness:
        @staticmethod
        def finalization_failed(path, message, **kwargs):
            failures.append((path, message, kwargs))

    host = SimpleNamespace(
        _finalized_clip_identities={},
        _failed_final_clips=set(),
        _clip_readiness=_Readiness(),
    )
    monkeypatch.setattr(main_module, 'emit_event', lambda *args, **kwargs: None)

    MainWindow._publish_final_clip(host, str(clip), 30, expected_identity=old)

    assert failures
    assert 'source replacement' in failures[0][1]


def test_fallback_error_yields_to_new_sql_presence_and_fresh_identity(
        monkeypatch, tmp_path):
    from ui import clip_viewer as viewer_module

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    identity = _identity(clip)
    refreshed = []
    host = SimpleNamespace(
        _closing=False,
        _highlight_generation=1,
        clip_path=str(clip),
        _highlight_worker=object(),
        _details_identity=identity,
        _mm=SimpleNamespace(
            get_highlight_state_snapshot=lambda _path, **_kwargs:
            {'library_highlights': {'version': 1, 'markers': []}}),
        _refresh_highlight_markers=lambda: refreshed.append(True),
    )
    monkeypatch.setattr(viewer_module, 'media_identity', lambda _path: identity)

    viewer_module.ClipViewer._on_embedded_highlights_ready(
        host, (1, str(clip), identity, None, 'late worker error'))

    assert refreshed == [True]


def test_fallback_result_is_discarded_after_same_path_replacement(
        monkeypatch, tmp_path):
    from ui import clip_viewer as viewer_module

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    old = _identity(clip)
    current = dict(old, size=old['size'] + 1)
    host = SimpleNamespace(
        _closing=False,
        _highlight_generation=1,
        clip_path=str(clip),
        _highlight_worker=object(),
        _details_identity=old,
        _mm=SimpleNamespace(
            get_highlight_state_snapshot=lambda *args, **kwargs: pytest.fail(
                'Replaced media must be refused before querying highlight SQL')),
    )
    monkeypatch.setattr(viewer_module, 'media_identity', lambda _path: current)

    viewer_module.ClipViewer._on_embedded_highlights_ready(
        host, (1, str(clip), old, (), None))

    assert host._highlight_worker is None


@pytest.mark.parametrize('suffix', ['.mkv', '.avi'])
def test_sql_absent_non_mp4_viewer_does_not_start_mp4_fallback(
        tmp_path, suffix):
    from ui import clip_viewer as viewer_module

    class Slider:
        def set_highlight_markers(self, markers):
            self.markers = markers

    class Panel:
        def clear(self):
            self.cleared = True

        def set_error(self, _error):
            raise AssertionError('non-MP4 SQL absence is not an error')

    clip = tmp_path / f'clip{suffix}'
    clip.write_bytes(b'library video')
    slider = Slider()
    panel = Panel()
    snapshot_calls = []

    def snapshot_reader(path, **kwargs):
        snapshot_calls.append((path, kwargs))
        return {}

    host = SimpleNamespace(
        _highlight_panel=panel,
        trim_slider=slider,
        _mm=SimpleNamespace(
            get_highlight_markers=lambda _path, **_kwargs: (),
            get_highlight_state_snapshot=snapshot_reader),
        clip_path=str(clip),
        _details_identity=_identity(clip),
        _highlight_error=None,
        _highlight_markers=[],
        _start_highlight_fallback=lambda: pytest.fail(
            'non-MP4 SQL absence must not start the MP4 fallback'),
    )

    viewer_module.ClipViewer._refresh_highlight_markers(host)

    assert panel.cleared is True
    assert slider.markers == ()
    assert snapshot_calls


def test_valid_empty_marker_payload_reaches_sql_setter(
        monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    expected = _identity(clip)
    calls = []
    manager = SimpleNamespace(
        set_highlight_markers=lambda path, markers, **kwargs:
        calls.append((path, markers, kwargs)),
    )
    host = SimpleNamespace(
        clip_metadata_manager=manager,
        _clip_readiness=SimpleNamespace(finalization_failed=lambda *a, **k: None),
        _record_finalization_warning=lambda *a, **k: None,
    )
    monkeypatch.setattr(main_module, 'write_capture_highlights',
                        lambda _path, _markers, expected_identity=None:
                        dict(expected_identity))

    MainWindow._persist_capture_highlights_worker(
        host, str(clip), {
            'markers': (),
            'owned_identity': expected,
            'expected_clip_id': 'clip-id',
            'initial_sql_presence': True,
        })

    assert calls and calls[0][1] == ()


def test_unregistered_empty_marker_payload_still_calls_sql_setter(
        monkeypatch, tmp_path):
    clip = tmp_path / 'new.mp4'
    clip.write_bytes(b'clip')
    expected = _identity(clip)
    calls = []
    manager = SimpleNamespace(
        set_highlight_markers=lambda path, markers, **kwargs:
        calls.append((path, markers, kwargs)),
    )
    host = SimpleNamespace(
        clip_metadata_manager=manager,
        _clip_readiness=SimpleNamespace(finalization_failed=lambda *a, **k: None),
        _record_finalization_warning=lambda *a, **k: None,
    )
    monkeypatch.setattr(main_module, 'write_capture_highlights',
                        lambda _path, _markers, expected_identity=None:
                        dict(expected_identity))

    MainWindow._persist_capture_highlights_worker(
        host, str(clip), {
            'markers': (),
            'owned_identity': expected,
            'expected_clip_id': None,
        })

    assert calls and calls[0][1] == ()
    assert calls[0][2]['expected_clip_id'] is None
