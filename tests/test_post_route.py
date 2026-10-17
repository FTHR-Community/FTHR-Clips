"""Each CLIP_SAVED selects one route that owns clip_ready through finalization.

Concurrent routes could release an uploader while another still writes.
Import the route selector directly; importing main.py creates no window.
"""

import itertools
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

pytest.importorskip('PySide6.QtCore')
from main import MainWindow, select_post_route
import main as main_module

ROUTES = {'mic', 'finalize'}
FLAGS = ('audio_on', 'multiband_enabled', 'mic_running',
         'watermark', 'manual_crop', 'camera')


@pytest.mark.parametrize('combo', list(itertools.product([False, True], repeat=6)))
def test_exactly_one_route_for_every_settings_combination(combo):
    """All 64 settings combinations. Never zero routes, never two."""
    # strict=True: if FLAGS and the parametrised tuple width ever drift apart,
    # zip() would silently drop a flag and the combination would not be tested.
    kwargs = dict(zip(FLAGS, combo, strict=True))
    route, has_async = select_post_route(**kwargs)

    assert route in ROUTES, f'unknown route {route!r} for {kwargs}'
    assert isinstance(has_async, bool)


def test_retired_multiband_request_cannot_own_the_post_mix_route():
    route, _ = select_post_route(
        audio_on=True, multiband_enabled=True, mic_running=True,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'mic'


def test_mic_route_only_when_mic_is_actually_running():
    route, _ = select_post_route(
        audio_on=True, multiband_enabled=False, mic_running=False,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'finalize'

    route, _ = select_post_route(
        audio_on=True, multiband_enabled=False, mic_running=True,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'mic'


def test_audio_disabled_disables_both_mux_routes():
    for multiband, mic in itertools.product([False, True], repeat=2):
        route, _ = select_post_route(
            audio_on=False, multiband_enabled=multiband, mic_running=mic,
            watermark=False, manual_crop=False, camera=False)
        assert route == 'finalize', (
            'audio off must not select an audio mux route')


def test_plain_clip_waits_for_cfr_validation():
    """Even a plain clip waits until its physical sample timing is verified."""
    route, has_async = select_post_route(
        audio_on=False, multiband_enabled=False, mic_running=False,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'finalize'
    assert has_async is True


@pytest.mark.parametrize('flag', ['manual_crop', 'camera', 'keyboard'])
def test_finalize_defers_upload_when_it_will_rewrite_the_file(flag):
    kwargs = dict(audio_on=False, multiband_enabled=False, mic_running=False,
                  watermark=False, manual_crop=False, camera=False)
    kwargs[flag] = True
    route, has_async = select_post_route(**kwargs)
    assert route == 'finalize'
    assert has_async is True, (
        f'{flag} rewrites the clip — the upload must wait for clip_ready')


def test_keyboard_only_finalization_starts_the_completion_worker():
    class _Settings:
        def get(self, key, default=None):
            if key == 'third_party_keyboard':
                return {'enabled': True, 'hwnd': 42}
            return default

    scheduled = []
    worker = object()
    host = SimpleNamespace(
        settings_manager=_Settings(),
        _allow_completed_clip_pipeline=lambda _path, _ready=None: True,
        _finalize_clip_worker=worker,
        _spawn_mux_thread=lambda target, args: scheduled.append((target, args)),
    )

    MainWindow._finalize_clip(
        host, 'keyboard-only.mp4', 30, 100.0, threading.Event(), None)

    assert len(scheduled) == 1
    assert scheduled[0][0] is worker


def test_keyboard_compositor_cannot_deadlock_on_an_unread_error_pipe():
    source = Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
    text = source.read_text(encoding='utf-8', errors='replace')
    body = text[
        text.index('def _apply_keyboard_overlay'):
        text.index('def _apply_camera_overlay')
    ]

    assert 'stderr=subprocess.PIPE' not in body
    assert 'TemporaryFile()' in body
    assert 'FTHR-KeyboardCompositorFeed' in body


def test_export_watermark_does_not_rewrite_source_clip():
    route, has_async = select_post_route(
        audio_on=False, multiband_enabled=False, mic_running=False,
        watermark=True, manual_crop=False, camera=False)

    assert route == 'finalize'
    assert has_async is True


def test_mux_routes_always_defer_the_upload():
    route, has_async = select_post_route(
        audio_on=True, multiband_enabled=False, mic_running=True,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'mic'
    assert has_async is True


def test_native_combined_audio_defers_for_the_audio_collapse_pass():
    route, has_async = select_post_route(
        audio_on=True, multiband_enabled=False, mic_running=False,
        watermark=False, manual_crop=False, camera=False,
        audio_capture_mode='combined', native_audio=True)
    assert (route, has_async) == ('mic', True)


def test_native_separated_audio_still_schedules_cfr_finalization():
    route, has_async = select_post_route(
        audio_on=True, multiband_enabled=False, mic_running=False,
        watermark=False, manual_crop=False, camera=False,
        audio_capture_mode='separated', native_audio=True)
    assert (route, has_async) == ('finalize', True)


def test_retired_multiband_request_creates_no_post_mix_work():
    route, has_async = select_post_route(
        audio_on=True, multiband_enabled=True, mic_running=False,
        watermark=False, manual_crop=False, camera=False)
    assert route == 'finalize'
    assert has_async is True


def test_completion_handler_dispatches_one_route_only():
    """Source-level guard on the one if/else post-processing dispatch."""
    src = (Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
           ).read_text(encoding='utf-8', errors='replace')
    start = src.index('def _on_clip_written')
    body = src[start:src.index('def _record_finalization_warning', start)]
    dispatch = body[body.index('args = ('):]

    assert dispatch.count('self._mux_mic_into_clip(') == 1
    assert 'self._mux_mic_into_clip(*args, audio_mode=audio_mode)' in dispatch
    assert 'self._mux_multiband_into_clip(' not in dispatch
    assert dispatch.count('self._finalize_clip(') == 1
    assert dispatch.index("if route == 'mic'") < dispatch.index('else:')


def test_readiness_is_registered_before_incremental_library_update():
    src = (Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
           ).read_text(encoding='utf-8', errors='replace')
    body = src[src.index('def _on_clip_written'):]
    notify = body.index('self.upload_manager.notify_clip_saved(')
    incremental_update = body.index('self.clip_grid.upsert_saved_clip(')
    assert notify < incremental_update, (
        'the grid must see FINALIZING readiness before the saved card is added')


def test_save_completion_never_forces_a_full_library_rebuild():
    src = (Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
           ).read_text(encoding='utf-8', errors='replace')
    start = src.index('def _on_clip_written')
    end = src.index('def _open_clips_folder', start)
    save_completion = src[start:end]

    assert 'self.clip_grid._known_files = None' not in save_completion
    assert 'self.clip_grid._load_clips()' not in save_completion
    assert save_completion.count('self.clip_grid.upsert_saved_clip(') == 2


def test_library_update_failure_cannot_turn_a_valid_clip_into_save_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / 'saved.mp4'
    clip.write_bytes(b'valid-final-payload')
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    statuses: list[str] = []
    published: list[str] = []

    class _Readiness:
        @staticmethod
        def can_access(_path):
            return True

        @staticmethod
        def warnings(_path):
            return ()

    class _FailingGrid:
        @staticmethod
        def upsert_saved_clip(_path, *, ready):
            raise RuntimeError('injected library update failure')

    host = SimpleNamespace(
        _published_final_clips=set(),
        _clip_readiness=_Readiness(),
        clip_grid=_FailingGrid(),
        clip_saved=SimpleNamespace(emit=published.append),
        _clip_diagnostic_started_by_path={},
        _set_status=lambda status, _qss: statuses.append(status),
        push_error=lambda *args, **kwargs: pytest.fail(
            f'valid clip was reported as failed: {args}, {kwargs}'),
        _update_status=lambda: None,
    )
    monkeypatch.setattr(
        main_module, 'emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(
        main_module.QTimer, 'singleShot',
        staticmethod(lambda _delay, _callback: None))

    MainWindow._publish_final_clip(host, str(clip), 30)

    assert published == [str(clip)]
    assert statuses == ['SAVED']
    assert any(
        args == ('library', 'saved_clip_upsert_failed')
        for args, _fields in events)


def test_duplicate_final_publication_is_ignored_after_first_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / 'saved.mp4'
    clip.write_bytes(b'valid-final-payload')
    published: list[str] = []
    upserts: list[tuple[str, bool]] = []
    statuses: list[str] = []

    class _Readiness:
        @staticmethod
        def can_access(_path):
            return True

        @staticmethod
        def warnings(_path):
            return ()

    class _Grid:
        @staticmethod
        def upsert_saved_clip(path, *, ready):
            upserts.append((path, ready))

    host = SimpleNamespace(
        _published_final_clips=set(),
        _clip_readiness=_Readiness(),
        clip_grid=_Grid(),
        clip_saved=SimpleNamespace(emit=published.append),
        _clip_diagnostic_started_by_path={},
        _set_status=lambda status, _qss: statuses.append(status),
        _update_status=lambda: None,
    )
    monkeypatch.setattr(
        main_module.QTimer, 'singleShot',
        staticmethod(lambda _delay, _callback: None))

    MainWindow._publish_final_clip(host, str(clip), 30)
    MainWindow._publish_final_clip(host, str(clip), 30)

    assert published == [str(clip)]
    assert upserts == [(str(clip), True)]
    assert statuses == ['SAVED']


def test_finalizer_cfr_gate_normalizes_once_and_releases_ready_event(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # This fixture exercises finalizer ordering, not packet probing of its
    # synthetic bytes. Source-timeline rejection is covered separately below.
    monkeypatch.setattr(MainWindow, '_source_timeline_is_safe', lambda *_: True)
    clip_path = str(tmp_path / 'finalizer-once.mp4')
    Path(clip_path).write_bytes(b'valid-final-payload')
    normalized: list[str] = []
    published: list[str] = []
    completed: list[str] = []

    class _Readiness:
        @staticmethod
        def state(_path):
            from core.clip_readiness import ClipReadinessState
            return ClipReadinessState.READY

        @staticmethod
        def complete(path):
            completed.append(path)

        @staticmethod
        def finalization_failed(*_args, **_kwargs):
            pytest.fail('the synthetic finalizer must not fail')

    class _UiCall:
        @staticmethod
        def emit(callback):
            callback()

    ready = threading.Event()
    host = SimpleNamespace(
        _mux_threads=[],
        _clip_readiness=_Readiness(),
        _ui_call=_UiCall(),
        _normalize_clip_to_cfr=lambda path: normalized.append(path),
        _publish_final_clip=lambda path, _duration: published.append(path),
        _record_finalization_warning=lambda *_args: None,
    )
    host._complete_clip_finalization = lambda path, duration, **kwargs: (
        MainWindow._complete_clip_finalization(
            host, path, duration, **kwargs))

    def _worker(_path, _duration, _end_time, clip_ready, _crop):
        clip_ready.set()

    worker = MainWindow._spawn_mux_thread(
        host, _worker, (clip_path, 30, 0.0, ready, None))
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert ready.is_set()
    assert normalized == [clip_path], (
        'the finalizer must not normalize the same clip twice')
    assert completed == [clip_path]
    assert published == [clip_path]

    no_ready_clip = str(tmp_path / 'finalizer-no-ready.mp4')
    Path(no_ready_clip).write_bytes(b'valid-final-payload')
    no_ready_worker = MainWindow._spawn_mux_thread(
        host, lambda *_args: None,
        (no_ready_clip, 30, 0.0, None, None))
    no_ready_worker.join(timeout=2)

    assert not no_ready_worker.is_alive()
    assert normalized == [clip_path, no_ready_clip], (
        'the no-ready finalizer path must also normalize exactly once')
    assert completed == [clip_path, no_ready_clip]
    assert published == [clip_path, no_ready_clip]


def test_sparse_source_gate_runs_before_overlay_or_crop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A proven source PTS hole must not reach a timestamp-rewriting worker."""
    clip_path = str(tmp_path / 'sparse-source.mp4')
    Path(clip_path).write_bytes(b'source')
    overlay_calls: list[str] = []
    completed: list[str] = []
    failures: list[str] = []
    ready = threading.Event()

    def _worker(_path, _duration, _end_time, _clip_ready, _crop):
        overlay_calls.append('crop')

    monkeypatch.setattr(
        main_module, 'probe_video_metadata',
        lambda _path: SimpleNamespace(average_fps=60.0, fps_source="fthr_frame_rate"))
    monkeypatch.setattr(
        main_module, 'probe_video_cfr_evidence',
        lambda _path, _fps: SimpleNamespace(
            physical_timeline_bounded=False))
    host = SimpleNamespace(
        _mux_threads=[],
        _clip_readiness=SimpleNamespace(
            finalization_failed=lambda path, _message, **_kwargs:
                failures.append(path)),
        _record_finalization_warning=lambda *_args: None,
        _complete_clip_finalization=lambda path, _duration, **_kwargs:
            completed.append(path),
    )
    host._source_timeline_is_safe = MainWindow._source_timeline_is_safe.__get__(
        host, MainWindow)
    worker = MainWindow._spawn_mux_thread(
        host, _worker, (clip_path, 30, 0.0, ready, None))
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert ready.is_set()
    assert overlay_calls == []
    assert failures == [clip_path]
    assert completed == [clip_path]


def test_source_probe_exception_fails_closed_before_worker_or_cfr(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clip_path = str(tmp_path / 'probe-error.mp4')
    Path(clip_path).write_bytes(b'source')
    worker_calls: list[str] = []
    normalized: list[str] = []
    failures: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    completions: list[tuple[str, dict[str, object]]] = []
    ready = threading.Event()

    def _probe(_path):
        raise RuntimeError('synthetic ffprobe failure')

    monkeypatch.setattr(main_module, 'probe_video_metadata', _probe)
    host = SimpleNamespace(
        _mux_threads=[],
        _clip_readiness=SimpleNamespace(
            finalization_failed=lambda path, message, **_kwargs:
                failures.append((path, message))),
        _normalize_clip_to_cfr=lambda path: normalized.append(path),
        _record_finalization_warning=lambda path, message:
            warnings.append((path, message)),
        _complete_clip_finalization=lambda path, _duration, **kwargs:
            completions.append((path, kwargs)),
    )

    def _worker(*_args):
        worker_calls.append('called')

    worker = MainWindow._spawn_mux_thread(
        host, _worker, (clip_path, 30, 0.0, ready, None))
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert worker_calls == []
    assert normalized == []
    assert ready.is_set()
    assert len(failures) == 1
    assert failures[0][0] == clip_path
    assert 'validation failed' in failures[0][1]
    assert warnings
    assert completions == [(clip_path, {'already_normalized': True})]


def test_inconclusive_cfr_repair_uses_bounded_background_ffmpeg_invocation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clip_path = tmp_path / 'vfr.mp4'
    clip_path.write_bytes(b'vfr-source')
    commands: list[tuple[list[str], dict[str, object]]] = []

    metadata = SimpleNamespace(
        average_fps=60.0,
        video_bitrate_bps=16_000_000,
        duration_seconds=60.0,
    )

    class _Readiness:
        @staticmethod
        def finalization_failed(*_args, **_kwargs):
            pytest.fail('the synthetic CFR repair must succeed')

    def _run(command, **kwargs):
        commands.append((command, kwargs))
        Path(command[-1]).write_bytes(b'cfr-output')
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(main_module, 'probe_video_metadata',
                        lambda _path: metadata)
    monkeypatch.setattr(
        main_module, 'probe_video_cfr_evidence',
        lambda _path, _fps: SimpleNamespace(
            cfr=False, physical_timeline_bounded=None))
    monkeypatch.setattr(main_module, 'postprocess_video_args',
                        lambda *_args, **_kwargs: [
                            '-c:v', 'h264_nvenc', '-preset', 'p1',
                            '-b:v', '16000k', '-pix_fmt', 'yuv420p'])
    monkeypatch.setattr(main_module, 'software_video_args',
                        lambda *_args, **_kwargs: [
                            '-c:v', 'libx264', '-preset', 'veryfast',
                            '-b:v', '16000k', '-pix_fmt', 'yuv420p'])
    monkeypatch.setattr(main_module, 'rebind_manifest_after_media_replace',
                        lambda _path: None)
    monkeypatch.setattr(main_module.subprocess, 'run', _run)

    host = SimpleNamespace(_clip_readiness=_Readiness())
    assert MainWindow._normalize_clip_to_cfr(host, str(clip_path))

    assert len(commands) == 1
    command, options = commands[0]
    assert command[command.index('-threads') + 1] == '2'
    assert command[command.index('-filter_threads') + 1] == '2'
    assert command.index('-threads') < command.index('-i')
    assert ['-fps_mode', 'cfr', '-r', '60'] == command[
        command.index('-fps_mode'):command.index('-fps_mode') + 4]
    assert ['-c:v', 'h264_nvenc', '-preset', 'p1'] == command[
        command.index('-c:v'):command.index('-c:v') + 4]
    assert command[command.index('-threads:v') + 1] == '2'
    assert command[-2:] == ['-c:a', 'copy'] or command[-3:-1] == [
        '-c:a', 'copy']
    if sys.platform == 'win32':
        assert options['creationflags'] == main_module._BACKGROUND_NO_WINDOW[
            'creationflags']


def test_only_one_call_site_starts_post_processing():
    """Post-processing must be started from the completion handler and nowhere
    else — not from the submit path, which is where it used to live."""
    src = (Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
           ).read_text(encoding='utf-8', errors='replace')
    for call in ('self._mux_mic_into_clip(', 'self._finalize_clip('):
        assert src.count(call) == 1, (
            f'{call} is invoked from more than one place — a clip could be '
            f'post-processed twice')
    assert src.count('self.upload_manager.notify_clip_saved(') == 1


def test_partial_file_cannot_enter_post_processing():
    ready = threading.Event()

    assert not MainWindow._allow_completed_clip_pipeline(
        '/clips/foo.mp4.partial', ready)
    assert ready.is_set()
    assert MainWindow._allow_completed_clip_pipeline('/clips/foo.mp4')


def test_every_post_processing_entrypoint_has_the_completed_clip_guard():
    src = (Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
           ).read_text(encoding='utf-8', errors='replace')
    entrypoints = ('_mux_mic_into_clip', '_finalize_clip')
    for name in entrypoints:
        start = src.index(f'def {name}')
        body = src[start:start + 1200]
        assert '_allow_completed_clip_pipeline' in body


@pytest.mark.parametrize('bounded, expected', [(True, True), (False, False)])
def test_valid_vfr_is_preserved_and_sparse_replay_is_rejected(
        tmp_path, monkeypatch, bounded, expected):
    clip = tmp_path / 'native.mp4'
    clip.write_bytes(b'original-video-and-audio')
    failures = []
    monkeypatch.setattr(main_module, 'probe_video_metadata',
                        lambda _: SimpleNamespace(average_fps=60.0))
    monkeypatch.setattr(main_module, 'probe_video_cfr_evidence',
                        lambda *_: SimpleNamespace(
                            cfr=False, physical_timeline_bounded=bounded))
    monkeypatch.setattr(main_module.subprocess, 'run',
                        lambda *_args, **_kwargs: pytest.fail('unexpected transcode'))
    host = SimpleNamespace(_clip_readiness=SimpleNamespace(
        finalization_failed=lambda *args, **kwargs: failures.append((args, kwargs))))
    assert MainWindow._normalize_clip_to_cfr(host, str(clip)) is expected
    assert bool(failures) is (not expected)
    assert clip.read_bytes() == b'original-video-and-audio'
