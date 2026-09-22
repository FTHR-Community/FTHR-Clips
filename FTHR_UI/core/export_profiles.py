"""Persistent export presets and content-aware FFmpeg planning.

This module contains no Qt code.  The editor, uploader, and tests all use the
same probe/planning contract so a platform limit is enforced in one place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import deque
import json
import math
import os
from pathlib import Path
import subprocess
import threading
from typing import Callable, Iterable

from core.ffmpeg_tools import (
    get_ffmpeg_exe,
    get_ffprobe_exe,
    size_constrained_video_args,
)
from core.transactional_output import (
    commit_staged_output,
    create_staged_output_path,
    discard_staged_output,
)


MIB = 1024 * 1024
DEFAULT_SAFETY_MARGIN = 0.94


@dataclass(frozen=True)
class MediaInfo:
    path: str
    size_bytes: int
    duration_s: float
    width: int
    height: int
    fps: float
    video_codec: str
    has_audio: bool
    audio_codec: str = ''
    audio_bitrate_kbps: int = 0
    video_bitrate_kbps: int = 0

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height > 0 else 16 / 9


@dataclass(frozen=True)
class ExportPreset:
    preset_id: str
    name: str
    target_size_mb: float
    target_width: int | None = None
    target_height: int | None = None
    target_fps: float | None = None
    video_codec: str = 'h264'
    audio_bitrate_kbps: int = 128
    built_in: bool = False
    safety_margin: float = DEFAULT_SAFETY_MARGIN

    def validated(self) -> 'ExportPreset':
        name = str(self.name or '').strip()
        if not name:
            raise ValueError('Preset name is required')
        size = float(self.target_size_mb)
        if not math.isfinite(size) or not 1 <= size <= 10_000:
            raise ValueError('Target file size must be between 1 MB and 10,000 MB')
        width = _optional_dimension(self.target_width, 'width')
        height = _optional_dimension(self.target_height, 'height')
        if (width is None) != (height is None):
            raise ValueError('Target width and height must be set together')
        fps = None if self.target_fps in (None, 0, '') else float(self.target_fps)
        if fps is not None and (not math.isfinite(fps) or not 1 <= fps <= 240):
            raise ValueError('Target FPS must be between 1 and 240')
        codec = str(self.video_codec or 'h264').lower()
        # Keep the codec field for future presets; this runtime supports H.264 exports.
        if codec not in {'h264', 'source'}:
            raise ValueError('This installation supports H.264 export only')
        audio = int(self.audio_bitrate_kbps)
        if not 32 <= audio <= 512:
            raise ValueError('Audio bitrate must be between 32 and 512 kbps')
        margin = float(self.safety_margin)
        if not 0.80 <= margin <= 0.99:
            raise ValueError('Safety margin must be between 80% and 99%')
        return ExportPreset(
            preset_id=str(self.preset_id or '').strip() or _slug(name),
            name=name,
            target_size_mb=size,
            target_width=width,
            target_height=height,
            target_fps=fps,
            video_codec=codec,
            audio_bitrate_kbps=audio,
            built_in=bool(self.built_in),
            safety_margin=margin,
        )


DISCORD_PRESET = ExportPreset(
    preset_id='discord',
    name='Discord · 10 MB',
    target_size_mb=10.0,
    target_fps=60.0,
    video_codec='h264',
    audio_bitrate_kbps=96,
    built_in=True,
    safety_margin=0.93,
)


@dataclass(frozen=True)
class ExportPlan:
    preset: ExportPreset
    source: MediaInfo
    duration_s: float
    target_bytes: int
    target_width: int
    target_height: int
    target_fps: float
    video_bitrate_kbps: int
    audio_bitrate_kbps: int
    reencode_video: bool
    reencode_audio: bool
    changes: tuple[str, ...]

    @property
    def can_stream_copy(self) -> bool:
        return not self.reencode_video and not self.reencode_audio

    @property
    def summary(self) -> str:
        if not self.changes:
            return 'Original already fits; no re-encoding needed.'
        return ' · '.join(self.changes)


class ExportPresetManager:
    """Atomic JSON storage for user-created presets."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path.home() / '.fthr' / 'export_presets.json'

    def all(self) -> tuple[ExportPreset, ...]:
        return (DISCORD_PRESET, *self.custom())

    def custom(self) -> tuple[ExportPreset, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return ()
        if not isinstance(raw, list):
            return ()
        presets: list[ExportPreset] = []
        seen = {'discord'}
        for item in raw:
            try:
                if not isinstance(item, dict):
                    continue
                preset = ExportPreset(**item).validated()
                if preset.preset_id in seen:
                    continue
                seen.add(preset.preset_id)
                presets.append(ExportPreset(**{**asdict(preset), 'built_in': False}))
            except (TypeError, ValueError):
                # User-edited invalid entries are skipped; valid presets remain usable.
                continue
        return tuple(presets)

    def get(self, preset_id: str) -> ExportPreset | None:
        return next((preset for preset in self.all()
                     if preset.preset_id == preset_id), None)

    def save(self, preset: ExportPreset) -> ExportPreset:
        preset = preset.validated()
        if preset.preset_id == DISCORD_PRESET.preset_id or preset.built_in:
            raise ValueError('Built-in presets cannot be replaced')
        values = list(self.custom())
        values = [value for value in values if value.preset_id != preset.preset_id]
        values.append(ExportPreset(**{**asdict(preset), 'built_in': False}))
        self._write(values)
        return preset

    def delete(self, preset_id: str) -> bool:
        if preset_id == DISCORD_PRESET.preset_id:
            return False
        values = list(self.custom())
        remaining = [value for value in values if value.preset_id != preset_id]
        if len(remaining) == len(values):
            return False
        self._write(remaining)
        return True

    def _write(self, presets: Iterable[ExportPreset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        payload = [asdict(preset) for preset in presets]
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
            os.replace(str(temporary), str(self.path))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Atomic replacement may already have consumed the temporary file.
                pass


def probe_media(path: str | Path, ffprobe: str | None = None) -> MediaInfo:
    """Read stream metadata with the bundled FFprobe."""

    media_path = Path(path)
    if not media_path.is_file():
        raise FileNotFoundError(str(media_path))
    probe = ffprobe or get_ffprobe_exe()
    completed = subprocess.run(
        [probe, '-v', 'error', '-show_streams', '-show_format',
         '-of', 'json', str(media_path)],
        capture_output=True, text=True, timeout=45,
        **_no_window(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or '').strip() or 'FFprobe could not read the file'
        raise RuntimeError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('FFprobe returned invalid metadata') from exc
    streams = payload.get('streams') if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError('No media streams were found')
    video = next((stream for stream in streams
                  if stream.get('codec_type') == 'video'), None)
    audio = next((stream for stream in streams
                  if stream.get('codec_type') == 'audio'), None)
    if not isinstance(video, dict):
        raise RuntimeError('The selected file has no video stream')
    format_info = payload.get('format', {})
    duration = _positive_float(video.get('duration')) or _positive_float(
        format_info.get('duration'))
    fps = _fraction(video.get('avg_frame_rate')) or _fraction(
        video.get('r_frame_rate'))
    size = media_path.stat().st_size
    overall_kbps = int(size * 8 / max(duration, 0.001) / 1000)
    audio_kbps = int(_positive_float((audio or {}).get('bit_rate')) / 1000)
    video_kbps = int(_positive_float(video.get('bit_rate')) / 1000)
    if not video_kbps:
        video_kbps = max(1, overall_kbps - audio_kbps)
    return MediaInfo(
        path=str(media_path),
        size_bytes=size,
        duration_s=max(duration, 0.001),
        width=max(1, int(video.get('width') or 0)),
        height=max(1, int(video.get('height') or 0)),
        fps=max(fps, 1.0),
        video_codec=str(video.get('codec_name') or ''),
        has_audio=audio is not None,
        audio_codec=str((audio or {}).get('codec_name') or ''),
        audio_bitrate_kbps=audio_kbps,
        video_bitrate_kbps=video_kbps,
    )


def plan_export(source: MediaInfo, preset: ExportPreset,
                duration_s: float | None = None) -> ExportPlan:
    """Plan FPS, bitrate, and only-when-needed resolution changes."""

    preset = preset.validated()
    duration = max(0.1, float(duration_s or source.duration_s))
    target_bytes = int(preset.target_size_mb * MIB * preset.safety_margin)
    requested_fps = preset.target_fps or source.fps
    target_fps = min(source.fps, requested_fps)

    requested_width = preset.target_width or source.width
    requested_height = preset.target_height or source.height
    target_width, target_height = _fit_inside(
        source.width, source.height, requested_width, requested_height)

    audio_kbps = preset.audio_bitrate_kbps if source.has_audio else 0
    overhead_kbps = 12
    budget_kbps = max(64, int(target_bytes * 8 / duration / 1000))
    video_kbps = max(120, budget_kbps - audio_kbps - overhead_kbps)

    # Resolution is the last lever.  FPS is reduced first, then bitrate is
    # compared with a conservative motion-video bits-per-pixel floor.
    if preset.target_width is None and source.size_bytes > target_bytes:
        ladder = (2160, 1440, 1080, 900, 720, 540, 480, 360)
        current_w, current_h = source.width, source.height
        for height in ladder:
            if height >= current_h:
                continue
            minimum = _minimum_video_kbps(current_w, current_h, target_fps)
            if video_kbps >= minimum:
                break
            next_w, next_h = _fit_inside(
                source.width, source.height,
                max(2, int(height * source.aspect_ratio)), height)
            current_w, current_h = next_w, next_h
        target_width, target_height = current_w, current_h

    codec_change = preset.video_codec not in {'source', source.video_codec.lower()}
    fps_change = target_fps < source.fps - 0.01
    size_change = source.size_bytes > target_bytes
    scale_change = (target_width, target_height) != (source.width, source.height)
    reencode_video = bool(codec_change or fps_change or size_change or scale_change)
    # Avoid an encode when every actual constraint is already satisfied.
    if (source.size_bytes <= target_bytes and not fps_change and not scale_change
            and preset.video_codec in {'source', source.video_codec.lower(), 'h264'}
            and source.video_codec.lower() in {'h264', 'avc1'}):
        reencode_video = False
    reencode_audio = bool(
        source.has_audio and reencode_video
        and (source.audio_codec.lower() != 'aac'
             or source.audio_bitrate_kbps > audio_kbps + 8))

    changes: list[str] = []
    if fps_change:
        changes.append(f'{source.fps:.0f}→{target_fps:.0f} FPS')
    if scale_change:
        changes.append(
            f'{source.width}×{source.height}→{target_width}×{target_height}')
    if size_change:
        changes.append(
            f'{source.size_bytes / MIB:.1f} MB→≤{target_bytes / MIB:.1f} MB')
    if reencode_video and not size_change:
        changes.append('H.264 video')
    if reencode_audio:
        changes.append(f'AAC {audio_kbps} kbps audio')

    return ExportPlan(
        preset=preset,
        source=source,
        duration_s=duration,
        target_bytes=target_bytes,
        target_width=target_width,
        target_height=target_height,
        target_fps=target_fps,
        video_bitrate_kbps=video_kbps,
        audio_bitrate_kbps=audio_kbps,
        reencode_video=reencode_video,
        reencode_audio=reencode_audio,
        changes=tuple(changes),
    )


def plan_video_filters(plan: ExportPlan) -> list[str]:
    filters: list[str] = []
    if plan.target_fps < plan.source.fps - 0.01:
        filters.append(f'fps={plan.target_fps:g}')
    if (plan.target_width, plan.target_height) != (plan.source.width, plan.source.height):
        filters.append(
            f'scale={plan.target_width}:{plan.target_height}:'
            'force_original_aspect_ratio=decrease')
    filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')
    return filters


def compress_media(source_path: str | Path, output_path: str | Path,
                   preset: ExportPreset, *,
                   progress: Callable[[int, str], None] | None = None,
                   cancel: Callable[[], bool] | None = None) -> tuple[Path, ExportPlan]:
    """Compress transactionally and verify the real configured size limit."""

    source = probe_media(source_path)
    plan = plan_export(source, preset)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if plan.can_stream_copy:
        # Upload preflight can use the original directly.  Export callers that
        # requested a distinct destination receive an independent copy.
        staged = create_staged_output_path(output)
        try:
            import shutil
            shutil.copy2(source.path, staged)
            commit_staged_output(staged, output)
        except Exception:
            discard_staged_output(staged)
            raise
        if progress:
            progress(100, plan.summary)
        return output, plan

    ffmpeg = get_ffmpeg_exe()
    staged = create_staged_output_path(output)
    filters = plan_video_filters(plan)
    cmd = [ffmpeg, '-y', '-i', source.path]
    if filters:
        cmd += ['-vf', ','.join(filters)]
    cmd += [
        *size_constrained_video_args(plan.video_bitrate_kbps, ffmpeg),
        '-maxrate', f'{max(plan.video_bitrate_kbps, int(plan.video_bitrate_kbps * 1.15))}k',
        '-bufsize', f'{plan.video_bitrate_kbps * 2}k',
    ]
    if source.has_audio:
        cmd += ['-c:a', 'aac', '-b:a', f'{plan.audio_bitrate_kbps}k']
    else:
        cmd += ['-an']
    cmd += ['-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(staged)]
    process = None
    stderr_thread = None
    stderr_tail: deque[str] = deque(maxlen=80)
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', **_no_window())
        assert process.stdout is not None
        assert process.stderr is not None

        def _drain_stderr() -> None:
            # FFmpeg can emit enough encoder diagnostics to fill the Windows
            # pipe before progress reaches stdout. Drain concurrently so a
            # valid encode cannot deadlock at zero bytes.
            assert process is not None and process.stderr is not None
            for stderr_line in process.stderr:
                stderr_tail.append(stderr_line.rstrip())

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True, name='fthr-compress-stderr')
        stderr_thread.start()
        for line in process.stdout:
            if cancel and cancel():
                process.terminate()
                raise RuntimeError('Compression cancelled')
            key, _, value = line.strip().partition('=')
            if key in {'out_time_ms', 'out_time_us'}:
                try:
                    # FFmpeg historically reports microseconds under both keys.
                    seconds = int(value) / 1_000_000
                    percent = min(99, int(seconds / plan.duration_s * 100))
                    if progress:
                        progress(percent, plan.summary)
                except ValueError:
                    # Ignore malformed progress telemetry; it doesn't describe export success.
                    pass
        code = process.wait()
        stderr_thread.join(timeout=2.0)
        if code != 0:
            detail = next((line for line in reversed(stderr_tail) if line.strip()), '')
            raise RuntimeError(detail or f'FFmpeg exited with code {code}')
        if not Path(staged).is_file() or Path(staged).stat().st_size <= 0:
            raise RuntimeError('Compression produced no usable output')
        if Path(staged).stat().st_size > int(preset.target_size_mb * MIB):
            raise RuntimeError(
                f'Compressed file is {Path(staged).stat().st_size / MIB:.1f} MB, '
                f'above the {preset.target_size_mb:g} MB limit')
        # Probe the staged result before publishing it; a truncated MP4 can be
        # non-empty but still invalid and must never reach the uploader.
        probe_media(staged)
        commit_staged_output(staged, output)
        if progress:
            progress(100, plan.summary)
        return output, plan
    except Exception:
        if process is not None and process.poll() is None:
            process.kill()
        discard_staged_output(staged)
        raise


def provider_limit_mb(provider: str) -> int | None:
    return {'lustful': 100, 'catbox': 200, 'discord_webhook': 10, 'discord': 10}.get(str(provider or '').lower())


def provider_compression_preset(provider: str) -> ExportPreset:
    limit = provider_limit_mb(provider)
    if limit is None:
        raise ValueError(f'Unknown upload provider: {provider}')
    return ExportPreset(
        preset_id=f'upload-{provider}',
        name=f'{provider.title()} upload',
        target_size_mb=float(limit),
        target_fps=60.0,
        video_codec='h264',
        audio_bitrate_kbps=128,
        safety_margin=0.94,
    )


def _optional_dimension(value, label: str) -> int | None:
    if value in (None, 0, ''):
        return None
    number = int(value)
    if not 16 <= number <= 8192:
        raise ValueError(f'Target {label} must be between 16 and 8192 pixels')
    return number & ~1


def _slug(value: str) -> str:
    text = ''.join(char.lower() if char.isalnum() else '-' for char in value)
    return '-'.join(part for part in text.split('-') if part)[:64] or 'preset'


def _positive_float(value) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError):
        # Missing/non-numeric probe fields are represented as zero for fallback logic.
        return 0.0


def _fraction(value) -> float:
    try:
        numerator, denominator = str(value or '0/1').split('/', 1)
        return _positive_float(numerator) / max(_positive_float(denominator), 1.0)
    except (TypeError, ValueError):
        # Invalid FFprobe rationals fall back to the alternate frame-rate field.
        return 0.0


def _fit_inside(source_w: int, source_h: int,
                box_w: int, box_h: int) -> tuple[int, int]:
    scale = min(1.0, box_w / max(source_w, 1), box_h / max(source_h, 1))
    return max(2, int(source_w * scale) & ~1), max(2, int(source_h * scale) & ~1)


def _minimum_video_kbps(width: int, height: int, fps: float) -> int:
    # 0.035 bits/pixel/frame is a pragmatic lower bound for high-motion game
    # footage. Below it a resolution reduction usually looks better than
    # retaining nominal pixels with severe block artifacts.
    return max(180, int(width * height * min(fps, 60.0) * 0.035 / 1000))


def _no_window() -> dict:
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}


__all__ = [
    'DEFAULT_SAFETY_MARGIN', 'DISCORD_PRESET', 'ExportPlan', 'ExportPreset',
    'ExportPresetManager', 'MIB', 'MediaInfo', 'compress_media',
    'plan_export', 'plan_video_filters', 'probe_media',
    'provider_compression_preset', 'provider_limit_mb',
]
