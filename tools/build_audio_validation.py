"""Build the native playback bridge and portable audio validation tools on Windows.

Developer prerequisite: ``python -m pip install ziglang==0.14.1``. FFmpeg comes
from ``python tools/fetch_third_party.py --ffmpeg``. This is not a replacement
for the MSVC/Windows SDK build of the complete capture engine.
"""

from pathlib import Path
import argparse
import hashlib
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / 'FTHRcapture/FTHRclips/third_party/ffmpeg'
OUTPUT = ROOT / '.audio-validation'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-providers', action='store_true',
                        help='Also compile the three Windows audio provider/registry translation units.')
    options = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    bridge = ROOT / 'FTHRcapture/FTHRPlaybackMixer/x64/Release/FTHRPlaybackMixer.dll'
    bridge.parent.mkdir(parents=True, exist_ok=True)
    common = [sys.executable, '-m', 'ziglang', 'c++', '-std=c++17', '-O2',
              '-target', 'x86_64-windows-gnu', '-I', str(ROOT / 'FTHRcapture_common'),
              '-I', str(ROOT / 'FTHRcapture/FTHRclips/include'),
              '-I', str(FFMPEG / 'include'), '-L', str(FFMPEG / 'lib')]
    jobs = [
        (bridge, ['-shared', 'FTHRcapture_common/ffmpeg_playback_bridge.cpp',
                  '-lavformat', '-lavcodec', '-lavutil', '-lavfilter', '-lswresample']),
        (OUTPUT / 'audio_quality_smoke.exe', ['tools/audio_quality_smoke.cpp',
            'FTHRcapture/FTHRclips/src/audio_encoder.cpp',
            'FTHRcapture/FTHRclips/src/audio_packet_ring.cpp', '-lavcodec', '-lavutil']),
        (OUTPUT / 'audio_recording_smoke.exe', ['tools/audio_recording_smoke.cpp',
            'FTHRcapture/FTHRclips/src/continuous_recording_writer.cpp',
            'FTHRcapture/FTHRclips/src/encoded_video_config.cpp',
            'FTHRcapture/FTHRclips/src/audio_source_model.cpp', '-lavformat', '-lavcodec', '-lavutil']),
        (OUTPUT / 'audio_regression.exe', ['tools/audio_regression_main.cpp',
            'FTHRcapture/FTHRclips_tests/tests/continuous_recording_writer_test.cpp',
            'FTHRcapture/FTHRclips_tests/tests/audio_source_model_test.cpp',
            'FTHRcapture/FTHRclips_tests/tests/audio_format_converter_test.cpp',
            'FTHRcapture/FTHRclips/src/continuous_recording_writer.cpp',
            'FTHRcapture/FTHRclips/src/encoded_video_config.cpp',
            'FTHRcapture/FTHRclips/src/audio_source_model.cpp',
            'FTHRcapture/FTHRclips/src/audio_packet_ring.cpp',
            'FTHRcapture/FTHRclips/src/audio_encoder.cpp',
            'FTHRcapture/FTHRclips/src/clip_audio_manifest.cpp',
            'FTHRcapture/FTHRclips/src/audio_timeline.cpp',
            'FTHRcapture/FTHRclips/src/audio_format_converter.cpp',
            'FTHRcapture/FTHRclips/src/save_clip_task.cpp',
            '-lavformat', '-lavcodec', '-lavutil', '-lswresample', '-lbcrypt', '-lole32']),
    ]
    for target, arguments in jobs:
        subprocess.run([*common, *arguments, '-o', str(target)], cwd=ROOT, check=True)
        print(target.relative_to(ROOT))
    if options.check_providers:
        sdk = OUTPUT / 'sdk-headers'
        sdk.mkdir(exist_ok=True)
        header = sdk / 'audioclientactivationparams.h'
        digest = 'c23b597d816793082dcbe505cf8a54d9a50213016a0a6b6d772aebb64dc8b560'
        if not header.exists():
            url = ('https://raw.githubusercontent.com/microsoft/win32metadata/'
                   '9cf94833626504b330c2f616ce71a2d3497cdc76/'
                   'generation/WinSDK/RecompiledIdlHeaders/um/audioclientactivationparams.h')
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = response.read(65536)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError('Windows SDK activation-header checksum mismatch')
            header.write_bytes(payload)
        if hashlib.sha256(header.read_bytes()).hexdigest() != digest:
            raise ValueError('Cached Windows SDK activation-header checksum mismatch')
        for name in ('windows_microphone_audio_provider', 'windows_audio_session_registry',
                     'windows_process_loopback_audio_provider'):
            target = OUTPUT / f'{name}.obj'
            subprocess.run([*common, '-DNTDDI_VERSION=0x0A00000A', '-I', str(sdk),
                            '-c', f'FTHRcapture/FTHRclips/src/{name}.cpp', '-o', str(target)],
                           cwd=ROOT, check=True)
            print(target.relative_to(ROOT))


if __name__ == '__main__':
    main()
