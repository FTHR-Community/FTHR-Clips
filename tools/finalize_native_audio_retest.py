"""Run the production native-audio finalizer on a fresh copy of an MP4.

This records local metadata and finalizer execution only. It does not inspect
content markers or make a physical A/V synchronization claim. Visual overlays
are disabled through no-op host hooks; source selection and the production
FFmpeg/filter path remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / 'FTHR_UI'
if str(UI_ROOT) not in sys.path:
    sys.path.insert(0, str(UI_ROOT))

from core.audio_manifest import (  # noqa: E402
    AudioManifestError,
    manifest_path_for,
    validate_manifest,
)
from core.combined_audio_policy import (  # noqa: E402
    descriptors_from_ffprobe_streams,
    select_combined_audio_streams,
)
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe  # noqa: E402


EVIDENCE_NAME = 'native_audio_retest_evidence.json'
_LABEL_TYPES = {
    'system audio': 'system', 'default mix': 'system', 'system': 'system',
    'microphone': 'microphone', 'mic': 'microphone', 'mic audio': 'microphone',
}
_PRODUCTION_SOURCES = (
    'FTHR_UI/main.py',
    'FTHR_UI/core/combined_audio_policy.py',
    'FTHR_UI/core/audio_manifest.py',
    'FTHR_UI/core/ffmpeg_tools.py',
    'FTHR_UI/core/av_sync_diagnostics.py',
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _probe_streams(ffprobe: str, media: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(media)],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or '').strip().splitlines()
        raise RuntimeError(detail[-1] if detail else 'ffprobe could not inspect input')
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError('ffprobe returned invalid stream metadata') from error
    streams = payload.get('streams') if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError('ffprobe stream metadata is unavailable')
    return streams


def _load_manifest(media: Path, sidecar: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    if not sidecar.exists() and not sidecar.is_symlink():
        return None, None
    try:
        raw = sidecar.read_bytes()
        manifest = json.loads(raw.decode('utf-8'))
        validate_manifest(manifest, media_path=media)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            AudioManifestError, TypeError, ValueError) as error:
        raise AudioManifestError(
            f'invalid present audio manifest: {error}') from error
    return manifest, raw


def _label_type(stream: dict[str, Any]) -> tuple[str | None, list[str]]:
    tags = stream.get('tags')
    tags = tags if isinstance(tags, dict) else {}
    labels = [tags[key].strip() for key in ('title', 'handler_name')
              if isinstance(tags.get(key), str) and tags[key].strip()]
    types = {_LABEL_TYPES[label.casefold()] for label in labels
             if label.casefold() in _LABEL_TYPES}
    if len(types) > 1:
        raise ValueError(
            f"conflicting recognized source labels for stream {stream.get('index')}")
    return (next(iter(types)) if types else None), labels


def identify_audio_sources(
        streams: list[dict[str, Any]],
        manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Describe audio tracks without assigning semantics to unlabeled tracks."""
    audio_streams = [stream for stream in streams
                     if isinstance(stream, dict)
                     and stream.get('codec_type') == 'audio'
                     and isinstance(stream.get('index'), int)]
    audio_indexes = {stream['index'] for stream in audio_streams}
    manifest_sources = {}
    if manifest is not None:
        manifest_sources = {
            source['stream_index']: source for source in manifest.get('sources', [])
        }
        invalid_indexes = set(manifest_sources) - audio_indexes
        if invalid_indexes:
            bad_index = min(invalid_indexes)
            raise AudioManifestError(
                f'manifest references nonexistent/non-audio stream index {bad_index}')

    tracks = []
    for audio_ordinal, stream in enumerate(audio_streams):
        container_index = stream['index']
        source = manifest_sources.get(container_index)
        label_type, labels = _label_type(stream)
        if source is not None:
            source_type = source['source_type']
            provenance = 'verified_manifest'
            display_name = source['display_name']
        elif label_type is not None:
            source_type = label_type
            provenance = 'explicit_label'
            display_name = next(
                (label for label in labels
                 if _LABEL_TYPES.get(label.casefold()) == label_type), None)
        else:
            source_type = 'unknown'
            provenance = 'unknown'
            display_name = None
        tracks.append({
            'container_index': container_index,
            'audio_ordinal': audio_ordinal,
            'codec_name': stream.get('codec_name'),
            'source_type': source_type,
            'display_name': display_name,
            'provenance': provenance,
            'recognized_labels': labels,
        })
    return tracks


def _assert_source_unchanged(
        source: Path, source_hash: str, sidecar: Path,
        sidecar_hash: str | None, sidecar_present: bool) -> None:
    if _sha256(source) != source_hash:
        raise RuntimeError('original MP4 changed during retest')
    present_after = sidecar.exists() or sidecar.is_symlink()
    if present_after != sidecar_present:
        raise RuntimeError('original audio sidecar presence changed during retest')
    if sidecar_present and _sha256(sidecar) != sidecar_hash:
        raise RuntimeError('original audio sidecar changed during retest')


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')


def run_retest(native_path: str | Path, output_directory: str | Path) -> Path:
    """Finalize a byte-for-byte copy and return its local evidence JSON path."""
    source = Path(native_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('native input must be a file')
    if source.suffix.casefold() != '.mp4':
        raise ValueError('native input must be an MP4 file')

    output_dir = Path(output_directory).expanduser().resolve(strict=False)
    try:
        source.relative_to(output_dir)
    except ValueError:
        pass
    else:
        raise ValueError('output directory contains the input')
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError('output directory must not exist')
    if not output_dir.parent.is_dir():
        raise FileNotFoundError('output directory parent must already exist')

    sidecar = manifest_path_for(source)
    sidecar_present = sidecar.exists() or sidecar.is_symlink()
    manifest, sidecar_bytes = _load_manifest(source, sidecar)
    source_hash = _sha256(source)
    sidecar_hash = (hashlib.sha256(sidecar_bytes).hexdigest()
                    if sidecar_bytes is not None else None)
    if manifest is not None and manifest.get('media_sha256') != source_hash:
        raise AudioManifestError('input changed while validating audio manifest')

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source_streams = _probe_streams(ffprobe, source)
    if not any(stream.get('codec_type') == 'video'
               for stream in source_streams if isinstance(stream, dict)):
        raise ValueError('native input has no video stream')
    source_tracks = identify_audio_sources(source_streams, manifest)
    if not source_tracks:
        raise ValueError('native input has no audio streams')
    if not any(track['source_type'] in {'system', 'microphone'}
               for track in source_tracks):
        raise ValueError('no system or microphone source is verified or explicitly labeled')

    descriptors = descriptors_from_ffprobe_streams(source_streams)
    selected = select_combined_audio_streams(manifest, descriptors)
    if not selected:
        raise ValueError('no identified system or microphone stream is selectable')
    selected_set = set(selected)
    for track in source_tracks:
        track['selected_for_finalization'] = track['audio_ordinal'] in selected_set

    _assert_source_unchanged(
        source, source_hash, sidecar, sidecar_hash, sidecar_present)
    output_dir.mkdir()
    copied_media = output_dir / source.name
    copied_sidecar = manifest_path_for(copied_media)
    shutil.copyfile(source, copied_media)
    if _sha256(copied_media) != source_hash:
        raise RuntimeError('native copy is not byte-for-byte identical')
    if sidecar_bytes is not None:
        shutil.copyfile(sidecar, copied_sidecar)
        if copied_sidecar.read_bytes() != sidecar_bytes:
            raise RuntimeError('copied audio sidecar does not match original bytes')

    source_code_hashes = {
        relative: _sha256(REPO_ROOT / relative)
        for relative in _PRODUCTION_SOURCES
    }
    evidence_path = output_dir / EVIDENCE_NAME
    evidence: dict[str, Any] = {
        'schema_version': 1,
        'status': 'RUNNING',
        'finalizer_execution_status': 'RUNNING',
        'physical_validation': 'NOT RUN',
        'content_sync_validation': 'NOT RUN',
        'source_path': str(source),
        'source_sha256': source_hash,
        'source_sidecar_path': str(sidecar) if sidecar_present else None,
        'source_sidecar_sha256': sidecar_hash,
        'source_manifest': manifest,
        'source_streams': source_streams,
        'source_audio_tracks': source_tracks,
        'native_copy_sha256_before_finalizer': source_hash,
        'native_copy_sidecar_sha256_before_finalizer': sidecar_hash,
        'finalized_copy_path': str(copied_media),
        'selected_base_input_count': len(selected),
        'finalizer_scope': ('multi_input_alignment_candidate'
                            if len(selected) > 1 else 'single_input_path_only'),
        'production_worker': 'main.MainWindow._combine_native_audio_worker',
        'production_source_sha256': source_code_hashes,
        'ffmpeg_path': str(Path(ffmpeg).resolve()),
        'ffprobe_path': str(Path(ffprobe).resolve()),
        'warnings': [],
        'overlay_hooks': {
            'disabled': True,
            'reason': 'headless retest host uses no-op crop/image/keyboard/camera hooks',
        },
        'originals_unchanged': None,
    }
    _write_evidence(evidence_path, evidence)

    try:
        from main import MainWindow

        warnings: list[tuple[str, str]] = []
        host = SimpleNamespace(
            _record_finalization_warning=lambda path, message:
                warnings.append((path, message)),
            _apply_crop=lambda *_args: None,
            _apply_image_overlay=lambda *_args: None,
            _apply_keyboard_overlay=lambda *_args: None,
            _apply_camera_overlay=lambda *_args: None,
        )
        duration_seconds = max(
            1,
            math.ceil(max(
                float(stream.get('start_time', 0) or 0)
                + float(stream.get('duration', 0) or 0)
                for stream in source_streams
                if isinstance(stream, dict)
                and stream.get('codec_type') in {'video', 'audio'})),
        )
        MainWindow._combine_native_audio_worker(
            host, str(copied_media), duration_seconds, 0.0, None, None)
        evidence['warnings'] = [message for _path, message in warnings]
        if warnings:
            raise RuntimeError('production finalizer reported a warning')

        final_streams = _probe_streams(ffprobe, copied_media)
        final_audio = [stream for stream in final_streams
                       if isinstance(stream, dict)
                       and stream.get('codec_type') == 'audio']
        if len(final_audio) != 1:
            raise RuntimeError(
                f'production finalizer output has {len(final_audio)} audio streams, expected one')
        tags = final_audio[0].get('tags')
        combined_labels = {'title', 'name', 'handler_name'}
        if not isinstance(tags, dict) or not any(
                tags.get(label) == 'Combined Audio' for label in combined_labels):
            raise RuntimeError('production finalizer output is not labeled Combined Audio')
        evidence['final_streams'] = final_streams
        evidence['finalized_copy_sha256'] = _sha256(copied_media)
        evidence['finalizer_execution_status'] = 'PASS'
        evidence['status'] = 'FINALIZED'
        _write_evidence(evidence_path, evidence)
    except Exception as error:
        evidence['finalizer_execution_status'] = 'FAIL'
        evidence['status'] = 'FINALIZER_FAILED'
        evidence['failure'] = f'{type(error).__name__}: {error}'
        _write_evidence(evidence_path, evidence)
        raise
    finally:
        try:
            _assert_source_unchanged(
                source, source_hash, sidecar, sidecar_hash, sidecar_present)
        except Exception as error:
            evidence['originals_unchanged'] = False
            evidence['status'] = 'ORIGINAL_PRESERVATION_FAILED'
            evidence['finalizer_execution_status'] = 'FAIL'
            evidence['failure'] = f'{type(error).__name__}: {error}'
            _write_evidence(evidence_path, evidence)
            raise
        evidence['originals_unchanged'] = True
        _write_evidence(evidence_path, evidence)
    return evidence_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Run the production native-audio finalizer on an untouched copy. '
            'This does not perform physical/content A/V synchronization validation.'),
    )
    parser.add_argument('native_mp4', type=Path, help='native multi-track MP4 to retest')
    parser.add_argument('new_output_directory', type=Path,
                        help='fresh output directory; it must not already exist')
    args = parser.parse_args(argv)
    try:
        evidence_path = run_retest(args.native_mp4, args.new_output_directory)
    except Exception as error:
        parser.exit(2, f'retest failed: {error}\n')
    print(f'Finalizer result: FINALIZED; evidence: {evidence_path}')
    print('Physical/content synchronization validation: NOT RUN')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
