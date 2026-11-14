"""Explicit colour conversion for SDR editing; original HDR media is untouched."""
from collections import OrderedDict
from dataclasses import dataclass
import json
import math
from fractions import Fraction
import re
import threading

from core.ffmpeg_tools import get_ffprobe_exe
from core.media_identity import file_identity
from core.media_process import run_media_process


@dataclass(frozen=True)
class HdrColor:
    transfer: str
    primaries: str = 'bt2020'
    matrix: str = 'bt2020nc'
    range: str = 'tv'
    peak_nits: float = 10000.0


_cache = OrderedDict()
_lock = threading.Lock()


def hdr_color_from_stream(stream):
    color = None
    if stream.get('color_transfer') in {'smpte2084', 'arib-std-b67'}:
        peak = 10000.0 if stream['color_transfer'] == 'smpte2084' else 1000.0
        for side in stream.get('side_data_list', []):
            value = side.get('max_content') or side.get('max_luminance')
            try:
                measured = float(Fraction(str(value)))
                if math.isfinite(measured) and 100 < measured <= 10000:
                    peak = measured
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        color = HdrColor(
            stream['color_transfer'],
            stream.get('color_primaries') if stream.get('color_primaries') in
            {'bt2020', 'bt709'} else 'bt2020',
            stream.get('color_space') if stream.get('color_space') in
            {'bt2020nc', 'bt709'} else 'bt2020nc',
            'pc' if stream.get('color_range') == 'pc' else 'tv', peak)
    return color


def probe_hdr(source, cancelled=None):
    """Bounded identity-keyed metadata probe, called only by background jobs."""
    cancelled = cancelled or threading.Event()
    try:
        identity = file_identity(source)
        with _lock:
            if identity in _cache:
                _cache.move_to_end(identity)
                return _cache[identity]
        result = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=color_transfer,color_primaries,color_space,color_range:stream_side_data',
             '-of', 'json', str(source)], cancelled, timeout_seconds=4)
        if result is None or result[0] or cancelled.is_set():
            return None
        streams = json.loads(result[1]).get('streams', [])
        if not streams:
            return None
        color = hdr_color_from_stream(streams[0])
        if file_identity(source) != identity:
            return None
        with _lock:
            _cache[identity] = color
            while len(_cache) > 128:
                _cache.popitem(last=False)
        return color
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, IndexError):
        return None


def sdr_tonemap_filter(color):
    """Linear-light HDR -> BT.709, before SDR effects/overlays and quantization."""
    return (
        f'zscale=pin={color.primaries}:tin={color.transfer}:min={color.matrix}:'
        f'rin={color.range}:t=linear:npl=100,format=gbrpf32le,'
        'zscale=p=bt709,setparams=colorspace=bt709,'
        f'tonemap=tonemap=mobius:param=0.3:desat=2:peak={color.peak_nits / 100:g},'
        'zscale=min=gbr:t=bt709:m=bt709:r=tv,format=yuv420p,'
        'sidedata=delete:type=MASTERING_DISPLAY_METADATA,'
        'sidedata=delete:type=CONTENT_LIGHT_LEVEL,'
        'sidedata=delete:type=DYNAMIC_HDR_PLUS,'
        'sidedata=delete:type=DYNAMIC_HDR_VIVID')


def sdr_command(command, source=None, cancelled=None):
    """Tone map an HDR source before an existing SDR transcode's video graph.

    Copy paths and colour-safe FFV1 retain source colour and depth. The input graph is
    deliberately restricted to this app's first-video-input command contract.
    """
    command = list(command)
    codecs = [command[i + 1] for i, value in enumerate(command[:-1])
              if value in {'-c:v', '-codec:v', '-vcodec', '-c'}]
    if not codecs or codecs[-1] == 'copy':
        return command
    if codecs[-1] == 'ffv1':
        from core.export_encoding import hdr_safe_template
        if hdr_safe_template(command):
            return command
    if '-color_trc' in command and command[command.index('-color_trc') + 1] in {'smpte2084', 'arib-std-b67'}:
        return command
    if source is None:
        if '-i' not in command:
            return command
        source = command[command.index('-i') + 1]
    color = probe_hdr(source, cancelled)
    if color is None:
        return command
    conversion = sdr_tonemap_filter(color)
    if '-vf' in command:
        i = command.index('-vf') + 1
        command[i] = conversion + ',' + command[i]
    elif '-filter_complex' in command:
        i = command.index('-filter_complex') + 1
        graph = command[i]
        references = list(re.finditer(r'\[0:v(?::0)?\]', graph))
        if references:
            # A filter output cannot feed multiple trims without an explicit split.
            labels = [f'fthr_hdr_sdr_{n}' for n in range(len(references))]
            iterator = iter(labels)
            graph = re.sub(r'\[0:v(?::0)?\]', lambda _: f'[{next(iterator)}]', graph)
            prefix = f'[0:v:0]{conversion}'
            if len(labels) > 1:
                prefix += f',split={len(labels)}'
            command[i] = prefix + ''.join(f'[{label}]' for label in labels) + ';' + graph
        else:
            # Audio-only complex graphs still allow the video to be mapped directly.
            command[i] = graph + f';[0:v:0]{conversion}[fthr_hdr_sdr]'
            mapped = False
            for n, value in enumerate(command[:-1]):
                if value == '-map' and command[n + 1] in {'0:v', '0:v?', '0:v:0', '0:v:0?'}:
                    command[n + 1] = '[fthr_hdr_sdr]'
                    mapped = True
            if not mapped:
                raise ValueError('HDR conversion requires an explicit video map')
    else:
        command[-1:-1] = ['-vf', conversion]
    return command
