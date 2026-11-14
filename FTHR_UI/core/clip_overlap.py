"""Proven capture-timeline overlap and non-destructive, cancellable merging.

Wall-clock filenames are deliberately not evidence. Only a versioned native
capture-generation tag whose interval agrees with playback is accepted.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
import heapq
from itertools import islice
import json
import math
from pathlib import Path
import threading
from uuid import UUID, uuid4

from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
from core.ffmpeg_tools import _NO_WINDOW
from core.media_identity import file_identity
from core.media_process import run_media_process

TIMELINE_TAG = 'fthr_capture_timeline'
_FIELDS = {'version', 'generation_id', 'start_tick', 'end_tick',
           'time_base_num', 'time_base_den'}
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class CaptureTimeline:
    generation_id: str
    start_tick: int
    end_tick: int
    time_base_num: int
    time_base_den: int

    @property
    def time_base(self):
        return Fraction(self.time_base_num, self.time_base_den)

    @property
    def start(self):
        return self.start_tick * self.time_base

    @property
    def end(self):
        return self.end_tick * self.time_base

    @property
    def duration(self):
        return self.end - self.start

    def to_json(self):
        return json.dumps({'version': 1, **self.__dict__}, separators=(',', ':'))


def parse_timeline(value) -> CaptureTimeline | None:
    try:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != _FIELDS or type(data['version']) is not int or data['version'] != 1:
            return None
        generation = data['generation_id']
        if (not isinstance(generation, str) or str(UUID(generation)) != generation
                or UUID(generation).int == 0):
            return None
        if any(type(data[key]) is not int for key in ('start_tick', 'end_tick', 'time_base_num', 'time_base_den')):
            return None
        if not (0 <= data['start_tick'] < data['end_tick'] < 2 ** 63
                and 0 < data['time_base_num'] <= 1_000_000_000
                and 0 < data['time_base_den'] <= 1_000_000_000):
            return None
        return CaptureTimeline(**{key: value for key, value in data.items() if key != 'version'})
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class AudioTrack:
    index: int
    key: str
    handler: str
    title: str


@dataclass(frozen=True)
class ClipInfo:
    path: str
    identity: tuple
    timeline: CaptureTimeline
    width: int
    height: int
    fps: Fraction
    audio: tuple[AudioTrack, ...] = ()
    color: tuple = ()

    @property
    def group(self):
        return (self.timeline.generation_id, self.width, self.height,
                self.fps, self.color)


def _probe(path, cancel):
    result = run_media_process(
        [get_ffprobe_exe(), '-v', 'error', '-show_streams', '-show_format',
         '-of', 'json', str(path)], cancel, timeout_seconds=8, background=True)
    if result is None or result[0] or len(result[1]) > 1024 * 1024:
        raise ValueError('Could not read clip timing. Refresh the library and try again.')
    return json.loads(result[1])


def _clip_from_probe(path, identity, document):
    from core.audio_processing import parse_source_handler
    timeline = parse_timeline(document.get('format', {}).get('tags', {}).get(TIMELINE_TAG))
    if timeline is None:
        return None
    streams = document.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    if len(videos) != 1:
        return None
    video = videos[0]
    fps = Fraction(document.get('format', {}).get('tags', {}).get(
        'fthr_frame_rate', video.get('avg_frame_rate', '0/1')))
    duration = float(video.get('duration', document.get('format', {}).get('duration', 0)))
    origin = float(video.get('start_time', 0))
    width, height = int(video['width']), int(video['height'])
    if (not 0 < fps <= 1000 or not 0 < width <= 16384 or not 0 < height <= 16384
            or not math.isfinite(duration) or not math.isfinite(origin) or abs(origin) > .0001
            or abs(duration - float(timeline.duration)) > max(.05, float(2 / fps))):
        return None
    audio, seen = [], set()
    for stream in streams:
        if stream.get('codec_type') != 'audio':
            continue
        tags = stream.get('tags', {})
        handler = str(tags.get('handler_name', ''))
        title = str(tags.get('title', handler or f'Audio {len(audio) + 1}'))
        # Drop FTHR3's old clip-local sample bounds after padding/re-encoding.
        # Retain dry-source processing, but never apply one clip's gain to a
        # different clip's samples when settings changed between saves.
        source = parse_source_handler(handler)
        if source:
            key = f"{source['source_id']}:{source['gain_percent']}:{source['mono']}"
            handler = (f"FTHR2|{source['source_id']}|{source['source_type']}|"
                       f"{source['gain_percent']}|{int(source['mono'])}|{source['display_name']}")
            title = source['display_name']
        else:
            key = f'{len(audio)}:{title}'
        if key in seen or len(handler) > 1024 or len(title) > 256 or len(audio) >= 38:
            return None
        seen.add(key)
        audio.append(AudioTrack(int(stream['index']), key, handler, title))
    color = tuple(str(video.get(key, '')) for key in
                  ('color_transfer', 'color_primaries', 'color_space', 'color_range'))
    return ClipInfo(str(path), identity, timeline, width, height, fps, tuple(audio), color)


def probe_clip(path, cancel=None, *, refresh=False) -> ClipInfo | None:
    """Background-only probe; negative results are cached by file identity."""
    cancel = cancel or threading.Event()
    try:
        path = str(Path(path).absolute())
        if Path(path).suffix.lower() != '.mp4' or cancel.is_set():
            return None
        identity = file_identity(path)
        with _cache_lock:
            if not refresh and identity in _cache:
                _cache.move_to_end(identity)
                return _cache[identity]
        info = _clip_from_probe(path, identity, _probe(path, cancel))
        if cancel.is_set() or file_identity(path) != identity:
            return None
        with _cache_lock:
            _cache[identity] = info
            _cache.move_to_end(identity)
            while len(_cache) > 4096:
                _cache.popitem(last=False)
        return info
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, RuntimeError):
        return None


def find_overlaps(infos) -> dict[str, tuple[str, ...]]:
    """Sweep intervals; bound suggestions even for thousands of duplicate saves.

    Every eligible overlap gets a suggestion, with up to 16 partners per card.
    Adjacent intervals and different generations never count as overlaps.
    """
    groups, matches = {}, {}
    for info in infos:
        if info is not None:
            groups.setdefault(info.group, {})[info.path] = info
    for group in groups.values():
        active, ends = {}, []
        for info in sorted(group.values(), key=lambda x: (x.timeline.start, x.path)):
            start = info.timeline.start
            while ends and ends[0][0] <= start:
                _, path = heapq.heappop(ends)
                active.pop(path, None)
            for other in islice(reversed(active), 8):
                for a, b in ((info.path, other), (other, info.path)):
                    values = matches.setdefault(a, [])
                    if b not in values and len(values) < 16:
                        values.append(b)
            active[info.path] = info
            heapq.heappush(ends, (info.timeline.end, info.path))
    return {path: tuple(values) for path, values in matches.items()}


@dataclass(frozen=True)
class MergeSegment:
    clip: ClipInfo
    offset: Fraction
    duration: Fraction


@dataclass(frozen=True)
class MergePlan:
    clips: tuple[ClipInfo, ...]
    segments: tuple[MergeSegment, ...]
    timeline: CaptureTimeline

    @property
    def duration(self):
        return self.timeline.duration


def plan_merge(infos) -> MergePlan:
    clips = tuple(sorted(infos, key=lambda x: (x.timeline.start, -x.timeline.end, x.path)))
    if len(clips) < 2 or len(clips) > 16 or len({c.path for c in clips}) != len(clips):
        raise ValueError('Choose between two and sixteen different overlapping clips.')
    first = clips[0]
    if any(c.group != first.group for c in clips):
        raise ValueError('These clips do not share the same capture timeline and video format.')
    end = first.timeline.start
    segments = []
    for index, clip in enumerate(clips):
        if index and clip.timeline.start >= end:
            raise ValueError('The clips do not overlap. No footage has been changed.')
        if clip.timeline.end > end:
            segments.append(MergeSegment(clip, end - clip.timeline.start, clip.timeline.end - end))
            end = clip.timeline.end
    # Express the union exactly even if equivalent time bases differ.
    base = first.timeline.time_base
    end_tick = end / base
    if end_tick.denominator != 1:
        raise ValueError('The capture timelines use incompatible frame boundaries.')
    timeline = CaptureTimeline(first.timeline.generation_id, first.timeline.start_tick,
                               int(end_tick), first.timeline.time_base_num, first.timeline.time_base_den)
    return MergePlan(clips, tuple(segments), timeline)


def _seconds(value):
    return f'{float(value):.12f}'


def build_merge_command(plan, staged, video_args):
    """Re-encode exact presentation slices; preserve independent audio tracks."""
    command = [get_ffmpeg_exe(), '-hide_banner', '-v', 'error', '-y',
               '-threads', '2', '-filter_complex_threads', '2']
    tracks = {}
    for segment in plan.segments:
        command += ['-i', segment.clip.path]
        for track in segment.clip.audio:
            tracks.setdefault(track.key, track)
    if len(tracks) > 38:
        raise ValueError('The combined clip contains too many independent audio tracks.')
    filters, concat_inputs = [], []
    for index, segment in enumerate(plan.segments):
        start, duration = _seconds(segment.offset), _seconds(segment.duration)
        filters.append(f'[{index}:v:0]trim=start={start}:duration={duration},'
                       f'setpts=PTS-{start}/TB,setsar=1[v{index}]')
        labels = f'[v{index}]'
        audio = {track.key: track for track in segment.clip.audio}
        for position, key in enumerate(tracks):
            label = f'a{index}_{position}'
            track = audio.get(key)
            if track:
                filters.append(f'[{index}:{track.index}]atrim=start={start}:duration={duration},'
                               f'asetpts=PTS-{start}/TB,aresample=48000:async=1:first_pts=0,'
                               f'aformat=sample_fmts=fltp:channel_layouts=stereo,'
                               f'apad,atrim=duration={duration}[{label}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration}[{label}]')
            labels += f'[{label}]'
        concat_inputs.append(labels)
    outputs = '[video]' + ''.join(f'[audio{i}]' for i in range(len(tracks)))
    filters.append(''.join(concat_inputs) + f'concat=n={len(plan.segments)}:v=1:a={len(tracks)}' + outputs)
    command += ['-filter_complex', ';'.join(filters), '-map', '[video]']
    for index in range(len(tracks)):
        command += ['-map', f'[audio{index}]']
    command += [*video_args, '-threads:v', '2']
    command += ['-r', str(plan.clips[0].fps), '-fps_mode', 'cfr', '-c:a', 'aac', '-b:a', '192k',
                '-map_metadata', '-1', '-map_chapters', '-1', '-movflags', '+faststart+use_metadata_tags',
                '-metadata', f'{TIMELINE_TAG}={plan.timeline.to_json()}',
                '-metadata', f'fthr_frame_rate={plan.clips[0].fps}',
                '-metadata', 'comment=fthr-audio-mode=' + ('separated' if len(tracks) > 1 else 'combined')]
    for index, track in enumerate(tracks.values()):
        command += [f'-metadata:s:a:{index}', f'handler_name={track.handler or track.title}',
                    f'-metadata:s:a:{index}', f'title={track.title}']
    return command + [str(staged)]


def _check_sources(plan):
    for clip in plan.clips:
        if file_identity(clip.path) != clip.identity:
            raise OSError('A source clip changed during merging. Refresh and try again.')


def execute_merge(paths, output, cancel, *, progress=None, status=None):
    from core.clip_mutations import rename_no_replace
    from core.export_encoding import choose_hdr_video_args, choose_video_args, software_args
    from core.export_lifecycle import ExportJob, ExportResult, ExportState
    from core.hdr_color import probe_hdr
    from core.transactional_output import create_staged_output_path, discard_staged_output

    if cancel.is_set():
        return ExportResult(ExportState.CANCELLED, 'Merge cancelled')
    clips = [probe_clip(path, cancel, refresh=True) for path in paths]
    if cancel.is_set():
        return ExportResult(ExportState.CANCELLED, 'Merge cancelled')
    if any(clip is None for clip in clips):
        raise ValueError('A clip has no verified capture timeline or is no longer available.')
    plan = plan_merge(clips)
    ffmpeg = get_ffmpeg_exe()
    color = probe_hdr(plan.clips[0].path, cancel)
    if color is not None:
        args = choose_hdr_video_args(ffmpeg, color, cancel=cancel)
        if args is None:
            raise ValueError('Merging these HDR clips needs a compatible HEVC encoder. Originals are unchanged.')
    else:
        args = choose_video_args(ffmpeg, cancel=cancel)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    staged = create_staged_output_path(output)
    expected_audio = len({t.key for s in plan.segments for t in s.clip.audio})

    def validate(path):
        _check_sources(plan)
        doc = _probe(path, cancel)
        merged = _clip_from_probe(path, file_identity(path), doc)
        if (merged is None or merged.timeline != plan.timeline
                or (merged.width, merged.height) != (plan.clips[0].width, plan.clips[0].height)
                or len(merged.audio) != expected_audio):
            raise ValueError('The merged video did not pass timing and audio validation.')
        return True

    def commit(source, destination):
        _check_sources(plan)
        if cancel.is_set():
            raise InterruptedError('Merge cancelled')
        rename_no_replace(source, destination)

    try:
        hardware = any(a in args for a in ('h264_nvenc', 'h264_qsv', 'h264_amf'))
        for attempt in range(2 if hardware else 1):
            _check_sources(plan)
            if status:
                status('Merging clips' if not attempt else 'Merging clips · software fallback')
            if attempt:
                args = software_args('quality', ffmpeg)
            result = ExportJob(build_merge_command(plan, staged, args), staged, output,
                               cancel_event=cancel, validate_output=validate, commit_output=commit,
                               popen_kwargs=_NO_WINDOW,
                               progress_callback=progress).run(duration=float(plan.duration))
            if result.state != ExportState.FAILED or cancel.is_set():
                break
        return result
    finally:
        discard_staged_output(staged)


def submit_merge(paths, directory):
    """Submit an application-owned job; all metadata/encoding stays off Qt."""
    from core.export_queue import submit_work

    paths = tuple(str(Path(p).absolute()) for p in paths)
    if not 2 <= len(paths) <= 16 or len(set(paths)) != len(paths):
        raise ValueError('Choose different overlapping clips to merge.')
    output = Path(directory).absolute() / f'{Path(paths[0]).stem}_merged_{uuid4().hex[:12]}.mp4'
    return submit_work(paths, output, 'merge',
                       lambda task: execute_merge(paths, output, task.cancel,
                                                  progress=lambda value: setattr(task, 'progress', value),
                                                  status=lambda value: setattr(task, 'status', value)),
                       label='Merge overlapping clips')
