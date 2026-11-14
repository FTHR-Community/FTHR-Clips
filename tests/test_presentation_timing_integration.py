"""Focused contracts for lossless MP4 presentation timing integration."""

from io import StringIO
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

pytest.importorskip('PySide6.QtCore')

import main as main_module  # noqa: E402
from core.export_lifecycle import ExportJob, ExportState  # noqa: E402
from main import MainWindow  # noqa: E402


class _ImmediateProcess:
    def __init__(self):
        self.stdout = StringIO('progress=end\n')
        self.stderr = StringIO()
        self.returncode = None

    def poll(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _factory(process):
    return lambda *_args, **_kwargs: process


def _readiness(failures):
    return SimpleNamespace(
        finalization_failed=lambda path, message, **_kwargs:
        failures.append((path, message)),
    )


def test_main_presentation_rewrite_rebinds_and_emits(monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'original')
    failures = []
    events = []
    rebound = []
    warnings = []

    monkeypatch.setattr(main_module.sys, 'platform', 'win32')
    monkeypatch.setattr(
        main_module, 'normalize_mp4_presentation_in_place',
        lambda path: Path(path).write_bytes(b'normalized') or True)
    monkeypatch.setattr(
        main_module, 'rebind_manifest_after_media_replace',
        lambda path: rebound.append(path) or True)
    monkeypatch.setattr(
        main_module, 'emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)))
    host = SimpleNamespace(
        _clip_readiness=_readiness(failures),
        _record_finalization_warning=lambda path, message:
        warnings.append((path, message)),
    )

    assert MainWindow._normalize_clip_presentation_timing(host, str(clip))
    assert clip.read_bytes() == b'normalized'
    assert rebound == [str(clip)]
    assert failures == []
    assert warnings == []
    assert any(args[:2] == ('clip_save', 'presentation_timing_normalized')
               for args, _fields in events)


def test_main_presentation_io_failure_blocks_readiness_without_deleting_source(
        monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    original = b'original'
    clip.write_bytes(original)
    failures = []
    warnings = []
    events = []

    monkeypatch.setattr(main_module.sys, 'platform', 'win32')
    monkeypatch.setattr(
        main_module, 'normalize_mp4_presentation_in_place',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('replace denied')))
    monkeypatch.setattr(
        main_module, 'emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)))
    host = SimpleNamespace(
        _clip_readiness=_readiness(failures),
        _record_finalization_warning=lambda path, message:
        warnings.append((path, message)),
    )

    assert not MainWindow._normalize_clip_presentation_timing(host, str(clip))
    assert clip.read_bytes() == original
    assert failures and failures[0][0] == str(clip)
    assert warnings and 'replace denied' in warnings[0][1]
    assert any(args[:2] == (
        'clip_save', 'presentation_timing_normalization_failed')
        for args, _fields in events)


def test_main_presentation_unsupported_is_a_noop(monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'unchanged')
    failures = []
    monkeypatch.setattr(main_module.sys, 'platform', 'win32')
    monkeypatch.setattr(
        main_module, 'normalize_mp4_presentation_in_place', lambda *_args: False)
    host = SimpleNamespace(_clip_readiness=_readiness(failures))

    assert not MainWindow._normalize_clip_presentation_timing(host, str(clip))
    assert clip.read_bytes() == b'unchanged'
    assert failures == []


def test_main_presentation_rewrite_requires_existing_manifest_rebind(
        monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'original')
    sidecar = Path(str(clip) + '.fthr-audio.json')
    sidecar.write_text('{}', encoding='utf-8')
    failures = []
    warnings = []
    events = []

    monkeypatch.setattr(main_module.sys, 'platform', 'win32')
    monkeypatch.setattr(
        main_module, 'normalize_mp4_presentation_in_place', lambda *_args: True)
    monkeypatch.setattr(
        main_module, 'rebind_manifest_after_media_replace', lambda *_args: False)
    monkeypatch.setattr(
        main_module, 'emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)))
    host = SimpleNamespace(
        _clip_readiness=_readiness(failures),
        _record_finalization_warning=lambda path, message:
        warnings.append((path, message)),
    )

    assert not MainWindow._normalize_clip_presentation_timing(host, str(clip))
    assert clip.exists()
    assert failures and 'manifest' in failures[0][1]
    assert warnings and 'manifest' in warnings[0][1]
    assert not any(args[:2] == ('clip_save', 'presentation_timing_normalized')
                   for args, _fields in events)


def test_ready_gate_orders_cfr_then_timing_before_ready(
        monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    order = []

    class _Readiness:
        @staticmethod
        def state(_path):
            from core.clip_readiness import ClipReadinessState
            return ClipReadinessState.READY

    class _ReadyEvent(threading.Event):
        def set(self):
            order.append('ready')
            super().set()

    monkeypatch.setattr(MainWindow, '_source_timeline_is_safe',
                        lambda *_args: True)
    ready = _ReadyEvent()
    host = SimpleNamespace(
        _mux_threads=[],
        _clip_readiness=_Readiness(),
        _normalize_clip_to_cfr=lambda _path: order.append('cfr') or True,
        _normalize_clip_presentation_timing=lambda _path:
        order.append('timing') or False,
        _record_finalization_warning=lambda *_args: None,
        _complete_clip_finalization=lambda _path, _duration, **_kwargs:
        order.append('complete'),
    )

    worker = MainWindow._spawn_mux_thread(
        host,
        lambda *_args: order.append('worker'),
        (str(clip), 2, 0.0, ready, None),
    )
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert order == ['worker', 'cfr', 'timing', 'ready', 'complete']


def test_export_job_normalizes_mp4_before_validator_and_commit(
        monkeypatch, tmp_path):
    staged = tmp_path / 'staged.mp4'
    final = tmp_path / 'final.mp4'
    staged.write_bytes(b'staged')
    order = []
    timing_cancel = []

    monkeypatch.setattr('core.export_lifecycle.sys.platform', 'win32')
    monkeypatch.setattr(
        'core.export_lifecycle.normalize_mp4_presentation_in_place',
        lambda path, cancelled=None:
        (order.append('timing'), timing_cancel.append(cancelled), True)[-1],
    )
    process = _ImmediateProcess()
    job = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(process),
        validate_output=lambda _path: order.append('validate'),
        commit_output=lambda source, target: (
            order.append('commit'), Path(target).write_bytes(Path(source).read_bytes())
        )[-1],
    )

    result = job.run()

    assert result.state is ExportState.COMPLETED
    assert order == ['timing', 'validate', 'commit']
    assert timing_cancel == [job._cancel_event]
    assert final.read_bytes() == b'staged'


def test_export_job_timing_cancel_cannot_publish(monkeypatch, tmp_path):
    staged = tmp_path / 'staged.mp4'
    final = tmp_path / 'final.mp4'
    staged.write_bytes(b'staged')
    validator_called = []
    commit_called = []

    monkeypatch.setattr('core.export_lifecycle.sys.platform', 'win32')

    def cancel_during_timing(_path, cancelled=None):
        cancelled.set()
        return False

    monkeypatch.setattr(
        'core.export_lifecycle.normalize_mp4_presentation_in_place',
        cancel_during_timing)
    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(_ImmediateProcess()),
        validate_output=lambda _path: validator_called.append(True),
        commit_output=lambda *_args: commit_called.append(True),
    ).run()

    assert result.state is ExportState.CANCELLED
    assert validator_called == []
    assert commit_called == []
    assert not final.exists()


def test_export_job_leaves_unknown_format_untouched(monkeypatch, tmp_path):
    staged = tmp_path / 'staged.mkv'
    final = tmp_path / 'final.mkv'
    staged.write_bytes(b'staged')
    calls = []
    monkeypatch.setattr('core.export_lifecycle.sys.platform', 'win32')
    monkeypatch.setattr(
        'core.export_lifecycle.normalize_mp4_presentation_in_place',
        lambda *_args, **_kwargs: calls.append(True))

    result = ExportJob(
        ['ffmpeg', str(staged)], staged, final,
        popen_factory=_factory(_ImmediateProcess()),
        validate_output=lambda _path: None,
    ).run()

    assert result.state is ExportState.COMPLETED
    assert calls == []
