import threading

import pytest

from core import playback_proxy
from core.media_identity import file_identity
from ui import clip_viewer, editor_preloader
from ui.editor_preloader import EditorPreloader, PendingPreparedMedia


def _inspection(clip_path, playback_path):
    return editor_preloader._Inspection(
        editor_preloader._clip_identity(clip_path), str(playback_path),
        file_identity(playback_path), ())


@pytest.fixture
def foreground(monkeypatch):
    compatibility, discovery, media, sources = [], [], [], []

    def prepare(path, _cancelled):
        compatibility.append(path)
        return path

    def discover(path, _cancelled):
        discovery.append(path)
        return ()

    monkeypatch.setattr(playback_proxy, 'prepare_compatibility_path', prepare)
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', discover)
    monkeypatch.setattr(clip_viewer.ClipViewer, '_start_metadata_probe', lambda _self: None)
    monkeypatch.setattr(clip_viewer.ClipViewer, '_set_media_source',
                        lambda self, **_: media.append((self.clip_path, self._playback_path)))
    monkeypatch.setattr(clip_viewer.ClipViewer, '_on_audio_sources_discovered',
                        lambda self, *_: sources.append(self.clip_path))
    return compatibility, discovery, media, sources


@pytest.mark.parametrize('completed_before_bind', [False, True])
def test_opening_same_clip_keeps_active_compatibility_and_discovery(
        qtbot, monkeypatch, tmp_path, foreground, completed_before_bind):
    clip, preview = (str(tmp_path / name) for name in ('clip.mp4', 'compatible.mp4'))
    for path in ('clip.mp4', 'compatible.mp4'):
        (tmp_path / path).write_bytes(b'clip')
    started, release = threading.Event(), threading.Event()
    requests = []

    def inspect(request):
        requests.append(request)
        started.set()
        assert release.wait(5)
        return _inspection(clip, preview)

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    loader = EditorPreloader()
    loader.request(clip)
    compatibility, discovery, media, sources = foreground
    try:
        qtbot.waitUntil(started.is_set)
        deadline = requests[0].deadline
        assert loader.keep_alive(clip)
        assert requests[0].deadline > deadline
        assert not loader.keep_alive(preview)
        pending = loader.take(clip)
        assert isinstance(pending, PendingPreparedMedia)
        loader.clear()
        if completed_before_bind:
            release.set()
            qtbot.waitUntil(lambda: loader._active is None)
        viewer = clip_viewer.ClipViewer(clip, None, preloaded_media=pending)
        qtbot.addWidget(viewer)
        pending.dispose()
        assert viewer._pending_editor_media is pending
        assert not requests[0].cancelled.is_set()
        assert not media and not compatibility and not discovery
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(media))
    assert media == [(clip, preview)]
    assert sources == [clip]
    assert not compatibility and not discovery
    assert len(requests) == 1 and loader._active is None
    viewer._teardown_player()


def test_opening_other_clip_cancels_speculative_inspection(qtbot, monkeypatch, tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    def inspect(request):
        started.set()
        if request.cancelled.wait(5):
            cancelled.set()

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    loader = EditorPreloader()
    loader.request(tmp_path / 'preloaded.mp4')
    qtbot.waitUntil(started.is_set)
    assert loader.take(tmp_path / 'other.mp4') is None
    qtbot.waitUntil(cancelled.is_set)
    qtbot.waitUntil(lambda: loader._active is None)


def test_opening_queued_clip_transfers_latest_inspection(qtbot, monkeypatch, tmp_path):
    active, target = (str(tmp_path / name) for name in ('active.mp4', 'target.mp4'))
    (tmp_path / 'target.mp4').write_bytes(b'target')
    started, release = threading.Event(), threading.Event()
    requests, results = [], []

    def inspect(request):
        requests.append(request.path)
        if request.path == active:
            started.set()
            assert release.wait(5)
            return None
        return _inspection(target, target)

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    loader = EditorPreloader()
    loader.request(active)
    try:
        qtbot.waitUntil(started.is_set)
        loader.request(target)
        pending = loader.take(target)
        assert isinstance(pending, PendingPreparedMedia)
        assert pending.bind(target, 'auto', results.append)
        pending.dispose()
        assert requests == [active]
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(results))
    assert requests == [active, target]
    assert results[0].playback_path == target


def test_preload_joins_parallel_compatibility_and_audio_inspection(qtbot, monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    audio_started, compatibility_started = threading.Event(), threading.Event()
    finish_audio, finish_compatibility = threading.Event(), threading.Event()
    compatibility_finished = threading.Event()
    completed = []

    def compat(path, _cancelled):
        compatibility_started.set()
        assert finish_compatibility.wait(5)
        compatibility_finished.set()
        return path

    def audio(_path, _cancelled):
        audio_started.set()
        assert finish_audio.wait(5)
        return ()

    monkeypatch.setattr(editor_preloader, 'prepare_compatibility_path', compat)
    monkeypatch.setattr(editor_preloader, 'discover_playback_sources', audio)
    loader = EditorPreloader()
    loader.request(clip)
    try:
        qtbot.waitUntil(lambda: audio_started.is_set() and compatibility_started.is_set())
        pending = loader.take(clip)
        assert pending.bind(clip, 'auto', completed.append)
        finish_compatibility.set()
        qtbot.waitUntil(compatibility_finished.is_set)
        assert not completed
    finally:
        finish_compatibility.set()
        finish_audio.set()
    qtbot.waitUntil(lambda: bool(completed))
    assert completed[0].audio_sources == ()
    assert completed[0].playback_path == str(clip)
    assert loader._active is None


@pytest.mark.parametrize('change', ['close', 'rebind', 'replace file', 'failed inspection'])
def test_pending_completion_obeys_viewer_and_file_lifetime(
        qtbot, monkeypatch, tmp_path, foreground, change):
    original, other, preview = (tmp_path / name for name in (
        'original.mp4', 'other.mp4', 'compatible.mp4'))
    for path in (original, other, preview):
        path.write_bytes(b'clip')
    clip, destination = str(original), str(other)
    started, release = threading.Event(), threading.Event()
    requests = []

    def inspect(request):
        requests.append(request)
        result = _inspection(clip, preview)
        started.set()
        assert release.wait(5)
        return result if change != 'failed inspection' else None

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    loader = EditorPreloader()
    loader.request(clip)
    compatibility, discovery, media, sources = foreground
    try:
        qtbot.waitUntil(started.is_set)
        pending = loader.take(clip)
        viewer = clip_viewer.ClipViewer(clip, None, embedded=True, preloaded_media=pending)
        qtbot.addWidget(viewer)
        pending.dispose()
        if change == 'close':
            viewer._teardown_player()
        elif change == 'rebind':
            viewer.load_clip(destination)
        elif change == 'replace file':
            original.write_bytes(b'replaced')
        assert requests[0].cancelled.is_set() == (change in {'close', 'rebind'})
    finally:
        release.set()
    qtbot.waitUntil(lambda: loader._active is None)
    if change == 'close':
        assert not media and not sources and not compatibility and not discovery
    else:
        expected = destination if change == 'rebind' else clip
        qtbot.waitUntil(lambda: bool(media and sources))
        assert media == [(expected, expected)]
        assert sources == compatibility == discovery == [expected]
        viewer._teardown_player()
