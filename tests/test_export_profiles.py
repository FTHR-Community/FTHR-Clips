from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))

from core.export_profiles import (  # noqa: E402
    DISCORD_PRESET,
    MIB,
    ExportPreset,
    ExportPresetManager,
    MediaInfo,
    plan_export,
    provider_limit_mb,
)


def _media(**overrides) -> MediaInfo:
    values = {
        'path': 'clip.mp4',
        'size_bytes': 250 * MIB,
        'duration_s': 120.0,
        'width': 2560,
        'height': 1440,
        'fps': 120.0,
        'video_codec': 'h264',
        'has_audio': True,
        'audio_codec': 'aac',
        'audio_bitrate_kbps': 192,
        'video_bitrate_kbps': 16_000,
    }
    values.update(overrides)
    return MediaInfo(**values)


def test_discord_plan_reduces_high_fps_before_other_changes() -> None:
    plan = plan_export(_media(), DISCORD_PRESET)

    assert plan.target_fps == 60.0
    assert plan.changes[0] == '120→60 FPS'
    assert plan.target_bytes < (20 if sys.platform == 'win32' else 10) * MIB
    assert plan.reencode_video
    assert abs(plan.target_width / plan.target_height - 16 / 9) < 0.01


def test_source_that_already_fits_is_stream_copied() -> None:
    plan = plan_export(_media(
        size_bytes=5 * MIB,
        duration_s=30,
        width=1920,
        height=1080,
        fps=60,
        audio_bitrate_kbps=96,
    ), DISCORD_PRESET)

    assert plan.can_stream_copy
    assert plan.changes == ()
    assert 'no re-encoding' in plan.summary


def test_resolution_is_reduced_when_bitrate_budget_is_too_low() -> None:
    plan = plan_export(_media(
        size_bytes=900 * MIB,
        duration_s=900,
        width=3840,
        height=2160,
        fps=60,
    ), DISCORD_PRESET)

    assert plan.target_height < 2160
    assert plan.target_width < 3840
    assert plan.target_width % 2 == 0
    assert plan.target_height % 2 == 0


def test_custom_presets_round_trip_and_can_be_deleted(tmp_path: Path) -> None:
    manager = ExportPresetManager(tmp_path / 'presets.json')
    preset = ExportPreset(
        preset_id='review',
        name='Review copy',
        target_size_mb=42,
        target_width=1280,
        target_height=720,
        target_fps=30,
        audio_bitrate_kbps=96,
    )

    manager.save(preset)
    restored = manager.get('review')
    assert restored is not None
    assert restored.name == 'Review copy'
    assert restored.target_width == 1280
    assert manager.delete('review')
    assert manager.get('review') is None
    assert not manager.delete('discord')


def test_provider_limits_are_explicit() -> None:
    assert provider_limit_mb('lustful') is None
    assert provider_limit_mb('catbox') == 200
