"""Versioned audio DSP descriptions consumed by native preview and FFmpeg export.

Capture masters stay dry. Never put user-supplied FFmpeg text into this graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


DSP_VERSION = 1
MASTER_CEILING = 0.98


def parse_source_handler(value: object) -> dict | None:
    """Read bounded native track metadata; never interpret FFmpeg text."""
    if not isinstance(value, str) or len(value) > 512 or not value.startswith(('FTHR2|', 'FTHR3|')):
        return None
    timed = value.startswith('FTHR3|')
    fields = value.rstrip(' \x00').split('|', 6 if timed else 5)
    if len(fields) != (7 if timed else 6):
        return None
    _version, source_id, source_type, gain, mono = fields[:5]
    name = fields[-1]
    try:
        source_id = str(UUID(source_id))
        processing = SourceProcessing(int(gain), mono == '1')
        if timed:
            timing = fields[5].split(':')
            if (len(timing) not in {1, 2} or not 0 <= int(timing[0]) <= 2**63 - 1
                    or len(timing) == 2 and not int(timing[0]) <= int(timing[1]) <= 2**63 - 1):
                return None
    except (ValueError, TypeError):
        return None
    if (source_type not in {'system', 'microphone', 'application'} or mono not in {'0', '1'}
            or not name or len(name) > 128 or any(ord(char) < 32 for char in name)):
        return None
    return {'source_id': source_id, 'source_type': source_type, 'display_name': name,
            'gain_percent': processing.gain_percent, 'mono': processing.mono}


def master_filter(enabled: bool = True) -> str:
    """Transparent below the ceiling; compensate and drain the 5 ms lookahead."""
    if not enabled:
        return ('aeval=clip(val(0)\\,-1\\,1)|clip(val(1)\\,-1\\,1):c=stereo,'
                'aformat=sample_rates=48000:channel_layouts=stereo')
    return ('alimiter=limit=0.98:level=disabled:latency=1:attack=5:release=50,'
            'aformat=sample_rates=48000:channel_layouts=stereo')


def mixed_output_metadata() -> list[str]:
    """A rendered mix must not retain dry-source processing instructions."""
    return ['-metadata:s:a:0', 'handler_name=Mixed Audio',
            '-metadata:s:a:0', 'title=Mixed Audio',
            '-metadata', 'fthr_capture_audio=',
            '-metadata', 'comment=fthr-audio-mode=combined']


@dataclass(frozen=True)
class SourceProcessing:
    gain_percent: int = 100
    mono: bool = False

    def __post_init__(self):
        if type(self.gain_percent) is not int or not 0 <= self.gain_percent <= 200:
            raise ValueError('input gain must be between 0 and 200')
        if not isinstance(self.mono, bool):
            raise ValueError('mono must be boolean')

    def filter(self) -> str:
        stages = []
        if self.mono:
            stages.append('pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1')
        if self.gain_percent != 100:
            stages.append(f'volume={self.gain_percent / 100:.8f}')
        return ','.join(stages) or 'anull'
