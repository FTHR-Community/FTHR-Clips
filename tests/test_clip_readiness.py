from __future__ import annotations

from core.clip_readiness import ClipReadinessRegistry, ClipReadinessState


def test_engine_commit_is_not_final_ready_while_processing_is_pending(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'base')
    registry = ClipReadinessRegistry()

    handle = registry.engine_committed(str(clip), needs_finalization=True)

    assert handle.state is ClipReadinessState.FINALIZING
    assert not handle.event.is_set()
    assert not registry.can_access(str(clip))


def test_success_sets_real_ready_state_and_unblocks_waiters(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'final')
    registry = ClipReadinessRegistry()
    handle = registry.engine_committed(str(clip), needs_finalization=True)

    registry.ready(str(clip))

    assert handle.event.is_set()
    assert registry.state(str(clip)) is ClipReadinessState.READY
    assert registry.can_access(str(clip))


def test_optional_failure_retains_valid_base_as_ready_with_warning(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'valid base')
    registry = ClipReadinessRegistry()
    handle = registry.engine_committed(str(clip), needs_finalization=True)

    registry.finalization_failed(
        str(clip), 'microphone mix failed', base_clip_usable=True)

    assert handle.event.is_set()
    assert registry.state(str(clip)) is ClipReadinessState.READY_WITH_WARNING
    assert registry.can_access(str(clip))
    assert registry.warnings(str(clip)) == ('microphone mix failed',)


def test_unusable_finalization_failure_never_becomes_accessible(tmp_path):
    clip = tmp_path / 'clip.mp4'
    registry = ClipReadinessRegistry()
    handle = registry.engine_committed(str(clip), needs_finalization=True)

    registry.finalization_failed(str(clip), 'file missing', base_clip_usable=False)

    assert handle.event.is_set()
    assert registry.state(str(clip)) is ClipReadinessState.FINALIZATION_FAILED
    assert not registry.can_access(str(clip))


def test_existing_untracked_clip_is_ready_by_default(tmp_path):
    clip = tmp_path / 'old.mp4'
    clip.write_bytes(b'old complete clip')
    registry = ClipReadinessRegistry()

    assert registry.can_access(str(clip))
    assert registry.state(str(clip)) is ClipReadinessState.READY


def test_recorded_optional_warning_is_published_at_completion(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'valid base')
    registry = ClipReadinessRegistry()
    registry.engine_committed(str(clip), needs_finalization=True)

    registry.record_warning(str(clip), 'watermark failed')
    registry.complete(str(clip))

    assert registry.state(str(clip)) is ClipReadinessState.READY_WITH_WARNING
    assert registry.warnings(str(clip)) == ('watermark failed',)


def test_startup_and_recovery_gates_override_already_signalled_ready_event(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'complete')
    registry = ClipReadinessRegistry()
    handle = registry.engine_committed(clip, needs_finalization=False)
    assert handle.event.is_set()
    registry.set_initializing(True)
    registry.set_recovery_paths([clip])
    registry.ready(clip)

    assert registry.state(clip) is ClipReadinessState.INITIALIZING
    assert not registry.can_access(clip)
    registry.set_initializing(False)
    assert registry.state(clip) is ClipReadinessState.RECOVERY_REQUIRED
    assert not registry.can_access(clip)
    registry.set_recovery_paths([])
    assert registry.can_access(clip)
    assert registry.event_for(clip) is handle.event


def test_canonical_alias_and_overlapping_block_reasons_require_all_releases(tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'complete')
    (tmp_path / 'sub').mkdir()
    alias = str(tmp_path / 'sub' / '..' / clip.name)
    registry = ClipReadinessRegistry()
    registry.block_paths([clip], reason='rename')
    registry.set_recovery_paths([alias])
    registry.unblock_paths([alias], reason='rename')

    assert not registry.can_access(clip)
    registry.block_paths([alias], reason='second-operation')
    registry.set_recovery_paths([])
    assert not registry.can_access(clip)
    registry.unblock_paths([clip], reason='second-operation')
    assert registry.can_access(alias)


def test_committed_rename_moves_readiness_handle_and_warnings(tmp_path):
    source, destination = tmp_path / 'source.mp4', tmp_path / 'destination.mp4'
    source.write_bytes(b'complete')
    registry = ClipReadinessRegistry()
    handle = registry.engine_committed(source, needs_finalization=True)
    registry.record_warning(source, 'history update requires retry')
    source.rename(destination)
    registry.rename_committed(source, destination)
    registry.complete(destination)

    assert not registry.is_tracked(source)
    assert registry.event_for(destination) is handle.event
    assert registry.state(destination) is ClipReadinessState.READY_WITH_WARNING
    assert registry.warnings(destination) == ('history update requires retry',)


def test_frozen_readiness_keys_release_exact_owner_after_path_resolution_changes(tmp_path, monkeypatch):
    from core import clip_readiness
    from core.library_index import canonical_media_path

    source, target = tmp_path / 'source.mp4', tmp_path / 'foreign.mp4'
    source.write_bytes(b'complete source')
    target.write_bytes(b'complete target')
    source_keys = frozenset({canonical_media_path(source)})
    target_keys = frozenset({canonical_media_path(target)})
    registry = ClipReadinessRegistry()
    resolved = []

    def redirected(path):
        resolved.append(str(path))
        return canonical_media_path(target if str(path) == str(source) else path)

    with monkeypatch.context() as boundary:
        boundary.setattr(clip_readiness, 'canonical_media_path', redirected)
        registry.block_keys(source_keys, reason='source-owner')
        registry.block_keys(target_keys, reason='foreign-owner')
        registry.unblock_keys(source_keys, reason='source-owner')
        registry.unblock_keys(source_keys, reason='source-owner')
        registry.unblock_keys(target_keys, reason='wrong-owner')
        assert resolved == [], 'captured-key APIs must never resolve paths again'
        assert not registry.can_access(source)  # Now points at the still-blocked target.
        assert not registry.can_access(target)
    assert registry.can_access(source)
    assert not registry.can_access(target)
    registry.unblock_keys(target_keys, reason='foreign-owner')
    assert registry.can_access(target)
