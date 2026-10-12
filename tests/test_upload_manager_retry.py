import sys, os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import time

from unittest.mock import patch, MagicMock


def test_oversized_catbox_upload_requires_compression_confirmation(
    qapp, tmp_path,
):
    from core.upload_manager import UploadManager

    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_provider': 'catbox',
    }.get(key, default)
    manager = UploadManager(sm)
    manager.set('upload_provider', 'catbox')
    manager.is_enabled = lambda: True
    clip = tmp_path / 'large.mp4'
    with clip.open('wb') as stream:
        stream.truncate(201 * 1024 * 1024)
    requested = []
    manager.compression_required.connect(
        lambda path, provider, limit: requested.append(
            (path, provider, limit)))

    manager.enqueue_upload(str(clip))

    assert requested == [(str(clip), 'catbox', 200)]
    assert manager._queue.empty()


def test_oversized_upload_auto_compresses_without_confirmation(qapp, tmp_path):
    from core.upload_manager import UploadManager

    sm = MagicMock()
    manager = UploadManager(sm)
    manager.set('upload_provider', 'lustful')
    manager.set('upload_auto_compress', True)
    manager.is_enabled = lambda: True
    clip = tmp_path / 'large.mp4'
    with clip.open('wb') as stream:
        stream.truncate(101 * 1024 * 1024)
    requested = []
    manager.compression_required.connect(
        lambda path, provider, limit: requested.append(
            (path, provider, limit)))

    manager.enqueue_upload(str(clip))

    assert requested == []
    task_kind, queued_path, _event = manager._queue.get_nowait()
    assert task_kind == 'compress_upload'
    assert queued_path == str(clip)


def test_compressed_upload_history_marks_original_and_preserves_provenance(
    qapp, tmp_path, monkeypatch,
):
    import json
    from core import upload_manager as upload_module

    history_path = tmp_path / 'history.json'
    compressed = str(tmp_path / 'compressed.mp4')
    original = str(tmp_path / 'original.mp4')
    history_path.write_text(json.dumps({
        compressed: {'status': 'ok', 'url': 'https://example.test/clip'},
    }), encoding='utf-8')
    monkeypatch.setattr(upload_module, '_HISTORY_FILE', history_path)
    manager = upload_module.UploadManager(MagicMock())

    manager._alias_compressed_upload_history(
        original, compressed, 42 * 1024 * 1024)

    history = json.loads(history_path.read_text(encoding='utf-8'))
    assert history[original]['uploaded_copy'] == 'compressed'
    assert history[original]['original_preserved'] is True
    assert history[original]['uploaded_path'] == compressed


def test_upload_error_signal_has_four_args(qapp):
    """upload_error must carry (title, detail, level, clip_path)."""
    from core.upload_manager import UploadManager
    sm = MagicMock()
    sm.get.return_value = False
    um = UploadManager(sm)

    received = []
    um.upload_error.connect(lambda t, d, l, p: received.append((t, d, l, p)))

    # Simulate UPLOAD NOT CONFIGURED (no path)
    assert um._do_single_upload.__func__ is not None  # the method exists
    um.upload_error.emit('TEST', 'detail', 'warning', '')
    assert received == [('TEST', 'detail', 'warning', '')]


def test_upload_failed_emits_path(qapp, tmp_path):
    """All-retries-exhausted path must emit clip_path in upload_error."""
    from core.upload_manager import UploadManager
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_enabled': True,
        'upload_server_url': 'http://localhost:9',
        'upload_auth_header': '',
        'upload_auto_delete': False,
    }.get(key, default)
    um = UploadManager(sm)
    um.is_enabled = lambda: True

    clip = tmp_path / 'test_clip.mp4'
    clip.write_bytes(b'\x00' * 64)

    received = []
    um.upload_error.connect(lambda t, d, l, p: received.append((t, d, l, p)))

    with patch.object(
            um, '_invoke_plugin',
            return_value={'ok': False, 'message': 'refused'}):
        um._do_single_upload(str(clip))

    failed = [r for r in received if r[0] == 'UPLOAD FAILED']
    assert failed, 'UPLOAD FAILED not emitted'
    assert failed[0][3] == str(clip), 'clip_path missing from signal'


def test_partial_file_is_rejected_by_all_upload_entrypoints(qapp, tmp_path):
    from core.upload_manager import UploadManager
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_enabled': True,
        'upload_mode': 'immediate',
        'upload_server_url': 'https://example.invalid/upload',
    }.get(key, default)
    um = UploadManager(sm)
    partial = tmp_path / 'clip.mp4.partial'
    partial.write_bytes(b'partial')

    ready = um.notify_clip_saved(str(partial))
    um.enqueue_upload(str(partial))
    success, message = um._do_single_upload(str(partial))

    assert ready.is_set()
    assert um._queue.empty()
    assert not success
    assert 'Incomplete clip' in message


def test_interval_scan_does_not_enqueue_partial_files(qapp, tmp_path, monkeypatch):
    import core.upload_manager as upload_module
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_enabled': True,
        'upload_mode': 'interval',
    }.get(key, default)
    um = upload_module.UploadManager(sm)
    partial = tmp_path / 'desktop_clip_from_11Aug2026_12-00-00.mp4.partial'
    partial.write_bytes(b'partial')
    monkeypatch.setattr(upload_module, '_CLIPS_DIR', tmp_path)

    um._interval_scan()

    assert um._queue.empty()


def test_upload_waits_for_real_final_ready_state(qapp, tmp_path):
    from core.clip_readiness import ClipReadinessRegistry
    from core.upload_manager import UploadManager

    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_enabled': True,
        'upload_mode': 'manual',
    }.get(key, default)
    registry = ClipReadinessRegistry()
    um = UploadManager(sm, registry)
    um.is_enabled = lambda: True
    um.is_uploaded = lambda _path: False
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'base')
    registry.engine_committed(str(clip), needs_finalization=True)
    uploaded = []
    um._do_single_upload = lambda path: (uploaded.append(path) or (True, 'ok'))
    um.start()
    try:
        um.enqueue_upload(str(clip))
        time.sleep(0.15)
        assert uploaded == []

        registry.complete(str(clip))
        deadline = time.monotonic() + 2
        while not uploaded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert uploaded == [str(clip)]
    finally:
        um.stop()


def test_failed_finalization_never_uploads(qapp, tmp_path):
    from core.clip_readiness import ClipReadinessRegistry
    from core.upload_manager import UploadManager

    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: {
        'upload_enabled': True,
        'upload_mode': 'manual',
    }.get(key, default)
    registry = ClipReadinessRegistry()
    um = UploadManager(sm, registry)
    um.is_enabled = lambda: True
    um.is_uploaded = lambda _path: False
    clip = tmp_path / 'clip.mp4'
    registry.engine_committed(str(clip), needs_finalization=True)
    uploaded = []
    um._do_single_upload = lambda path: (uploaded.append(path) or (True, 'ok'))
    um.start()
    try:
        um.enqueue_upload(str(clip))
        registry.finalization_failed(
            str(clip), 'missing final bytes', base_clip_usable=False)
        time.sleep(0.2)
        assert uploaded == []
    finally:
        um.stop()
