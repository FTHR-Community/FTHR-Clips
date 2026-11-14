"""Regression contract for retaining native app stems through visual edits."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = (ROOT / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8')


def _method_source(name: str, next_name: str) -> str:
    return MAIN_SOURCE.split(f'    def {name}', 1)[1].split(f'    def {next_name}', 1)[0]


def test_visual_postprocessors_map_all_audio_and_rebind_source_manifest():
    import main
    from core.audio_manifest import rebind_manifest_after_media_replace

    assert main.rebind_manifest_after_media_replace is rebind_manifest_after_media_replace
    adapter = _method_source('_rebind_manifest_after_owned_replace', '_persist_capture_highlights_worker')
    assert 'rebound = rebind_manifest_after_media_replace(clip_path)' in adapter
    methods = (
        _method_source('_apply_image_overlay', '_apply_camera_overlay'),
        _method_source('_apply_camera_overlay', '_apply_crop'),
        _method_source('_apply_crop', '_finalize_clip'),
    )
    for method in methods:
        assert "'-map', '0:a?'" in method
        assert "'-map_metadata', '0'" in method
        assert "'-c:a', 'copy'" in method
        publication = method.index('self._replace_finalization_output(')
        rebind = method.index('self._rebind_manifest_after_owned_replace(clip_path)')
        assert publication < rebind
