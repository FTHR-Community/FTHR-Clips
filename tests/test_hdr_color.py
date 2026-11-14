import json
from pathlib import Path
import subprocess
import threading

import pytest

from core import hdr_color
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe, software_video_args
from core import export_encoding


@pytest.fixture
def hdr(monkeypatch):
    color = hdr_color.HdrColor('smpte2084')
    monkeypatch.setattr(hdr_color, 'probe_hdr', lambda *args: color)
    return color


def test_copy_and_lossless_do_not_probe_or_convert(monkeypatch):
    monkeypatch.setattr(hdr_color, 'probe_hdr', lambda *_: pytest.fail('must not probe'))
    for codec in ('copy', 'ffv1'):
        cmd = ['ffmpeg', '-i', 'source', '-c:v', codec, 'out']
        assert hdr_color.sdr_command(cmd) == cmd


def test_hdr_preserving_encode_is_not_tonemapped(hdr):
    command = ['ffmpeg', '-i', 'source', '-c:v', 'hevc_nvenc',
               '-color_trc', 'smpte2084', 'out']
    assert hdr_color.sdr_command(command) == command


@pytest.mark.parametrize('graph,safe', [
    ('[0:v]crop=100:100:0:0[vout]', True),
    ('[0:v]trim=end=1,setpts=PTS-STARTPTS[a];[0:v]trim=start=2[b];[a][b]concat=n=2[v]', True),
    ('[0:v]curves=all=0/0 1/1[v]', False),
    ('[0:v][1:v]overlay=0:0[v]', False),
    ('[0:v]format=yuv420p[v]', False),
    ('[0:v]unknown_filter[v]', False),
])
def test_hdr_export_rejects_sdr_only_effects(graph, safe):
    assert export_encoding.hdr_safe_template(['ffmpeg', '-filter_complex', graph, 'out']) is safe


def test_hdr_probe_failure_allows_sdr_fallback(monkeypatch):
    from core import media_process
    monkeypatch.setattr(export_encoding, '_working', {})
    commands = []
    def run(cmd, *args, **kwargs):
        commands.append(cmd)
        return 1, b'', b'unsupported'
    monkeypatch.setattr(media_process, 'run_media_process', run)
    assert export_encoding.choose_hdr_video_args('ffmpeg', hdr_color.HdrColor('smpte2084')) is None
    assert len(commands) == 3
    assert all('p010le' in command and 'main10' in command for command in commands)


def test_failed_hdr_encode_retries_with_tonemapped_sdr(hdr, monkeypatch, tmp_path):
    from core import export_queue
    from core.export_lifecycle import ExportResult, ExportState
    monkeypatch.setattr(export_queue, 'probe_hdr', lambda *args: hdr)
    monkeypatch.setattr(export_queue, 'choose_hdr_video_args', lambda *args:
                        ['-c:v', 'hevc_qsv', '-color_trc', 'smpte2084'])
    monkeypatch.setattr(export_queue, 'software_args', lambda *args: ['-c:v', 'libopenh264'])
    commands = []
    class Job:
        def __init__(self, cmd, *args, **kwargs):
            commands.append(cmd)
        def run(self, **kwargs):
            return ExportResult(ExportState.FAILED if len(commands) == 1 else ExportState.COMPLETED)
    monkeypatch.setattr(export_queue, 'ExportJob', Job)
    result = export_queue.run_export(
        ['ffmpeg', '-i', 'original.mp4', '-vf', 'crop=128:64:0:0',
         *export_encoding.VIDEO_PLACEHOLDER, str(tmp_path / 'pending.mp4')],
        tmp_path / 'pending.mp4', tmp_path / 'out.mp4', quality='quality', duration=1,
        cancel=threading.Event(), validate=None)
    assert result.succeeded and len(commands) == 2
    assert 'tonemap' not in ' '.join(commands[0])
    assert 'tonemap' in ' '.join(commands[1])


def test_sdr_unchanged(monkeypatch):
    monkeypatch.setattr(hdr_color, 'probe_hdr', lambda *_: None)
    cmd = ['ffmpeg', '-i', 'source', '-vf', 'scale=64:32', '-c:v', 'h264', 'out']
    assert hdr_color.sdr_command(cmd) == cmd


def test_conversion_precedes_effects(hdr):
    cmd = ['ffmpeg', '-i', 'source', '-vf', 'eq=brightness=0.1', '-c:v', 'h264', 'out']
    result = hdr_color.sdr_command(cmd)
    assert result[result.index('-vf') + 1] == hdr_color.sdr_tonemap_filter(hdr) + ',eq=brightness=0.1'
    assert cmd[4] == 'eq=brightness=0.1'


def test_multiple_trim_graph_splits_converted_video(hdr):
    cmd = ['ffmpeg', '-i', 'source', '-filter_complex',
           '[0:v]trim=end=1[a];[0:v:0]trim=start=2[b];[a][b]concat=n=2[vout]',
           '-map', '[vout]', '-c:v', 'h264', 'out']
    result = hdr_color.sdr_command(cmd)
    graph = result[result.index('-filter_complex') + 1]
    assert ',split=2[fthr_hdr_sdr_0][fthr_hdr_sdr_1];' in graph
    assert '[fthr_hdr_sdr_0]trim=end=1' in graph
    assert '[fthr_hdr_sdr_1]trim=start=2' in graph


def test_overlay_converts_only_main_video(hdr):
    cmd = ['ffmpeg', '-i', 'source', '-i', 'camera', '-filter_complex',
           '[1:v]scale=32:32[cam];[0:v][cam]overlay=0:0[vout]',
           '-map', '[vout]', '-c:v', 'h264', 'out']
    result = hdr_color.sdr_command(cmd)
    graph = result[result.index('-filter_complex') + 1]
    assert '[1:v]scale=32:32[cam]' in graph
    assert '[fthr_hdr_sdr_0][cam]overlay' in graph


def test_audio_graph_maps_converted_video(hdr):
    cmd = ['ffmpeg', '-i', 'source', '-filter_complex', '[0:a]anull[aout]',
           '-map', '0:v:0', '-map', '[aout]', '-c:v', 'h264', 'out']
    result = hdr_color.sdr_command(cmd)
    assert result[result.index('-map') + 1] == '[fthr_hdr_sdr]'
    assert 'anull[aout];[0:v:0]zscale=' in result[result.index('-filter_complex') + 1]


def test_probe_cache_invalidates_replaced_media(tmp_path, monkeypatch):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'first')
    calls = []
    def probe(*args, **kwargs):
        calls.append(args)
        return 0, json.dumps({'streams': [{'color_transfer': 'smpte2084'}]}), b''
    monkeypatch.setattr(hdr_color, 'run_media_process', probe)
    assert hdr_color.probe_hdr(source).transfer == 'smpte2084'
    assert hdr_color.probe_hdr(source).transfer == 'smpte2084'
    assert len(calls) == 1
    source.write_bytes(b'replacement')
    assert hdr_color.probe_hdr(source).transfer == 'smpte2084'
    assert len(calls) == 2


@pytest.mark.parametrize('transfer', ['smpte2084', 'arib-std-b67'])
def test_real_hdr_ramp_tonemaps_and_is_tagged_sdr(tmp_path, transfer):
    ffmpeg, probe = get_ffmpeg_exe(), get_ffprobe_exe()
    source = tmp_path / 'hdr.mkv'
    out = tmp_path / 'sdr.mp4'
    ramp = ('nullsrc=s=128x64:r=1,format=gbrpf32le,'
            "geq=r='X/W*10':g='X/W*10':b='X/W*10',"
            f'zscale=pin=bt2020:tin=linear:min=gbr:rin=pc:p=bt2020:t={transfer}:'
            'm=bt2020nc:r=tv:npl=100,format=yuv420p10le')
    generated = subprocess.run(
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', ramp, '-frames:v', '1',
         '-c:v', 'ffv1', '-color_trc', transfer, '-color_primaries', 'bt2020',
         '-colorspace', 'bt2020nc', '-y', str(source)], capture_output=True, timeout=20)
    assert generated.returncode == 0, generated.stderr.decode(errors='replace')
    color = hdr_color.probe_hdr(source, threading.Event())
    assert color and color.transfer == transfer
    command = hdr_color.sdr_command(
        [ffmpeg, '-v', 'error', '-y', '-i', str(source),
         *software_video_args(2500, ffmpeg), str(out)])
    encoded = subprocess.run(command, capture_output=True, timeout=20)
    assert encoded.returncode == 0, encoded.stderr.decode(errors='replace')
    metadata = subprocess.run([probe, '-v', 'error', '-show_streams', '-of', 'json', str(out)],
                              capture_output=True, timeout=10)
    stream = json.loads(metadata.stdout)['streams'][0]
    assert stream['color_transfer'] == 'bt709'
    assert stream['color_primaries'] == 'bt709'
    assert stream['pix_fmt'] == 'yuv420p'
    pixels = subprocess.run([ffmpeg, '-v', 'error', '-i', str(out), '-frames:v', '1',
                             '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                            capture_output=True, timeout=10)
    assert pixels.returncode == 0
    row = pixels.stdout[32 * 128:33 * 128]
    assert row[10] < row[60] < row[110]  # Bright HDR values remain distinguishable.
    assert row[-1] > 150
    assert Path(source).exists()
