"""
audio_mixer.py — mix per-category WAV files into a video clip using ffmpeg.

Called after clip save when multiband audio is active. Mirrors the mic-mux
pattern in main.py but handles N category tracks with independent volumes.
"""
import os
import subprocess
import tempfile


def mix_multiband_clip(
    clip_path: str,
    category_wavs: dict,   # {"Game": "/tmp/clip_fthr_game.wav", ...}
    volumes: dict,          # {"Game": 1.0, "Discord": 0.7, ...}
    ffmpeg_exe: str,
) -> bool:
    """
    Mix category WAV files into clip_path with the given per-category volumes.
    Overwrites clip_path in-place on success.
    Returns True on success, False on any failure.
    """
    if not category_wavs:
        return False

    # Build list of (category, wav_path) for existing files only
    valid = [(cat, wav) for cat, wav in category_wavs.items()
             if os.path.exists(wav)]
    if not valid:
        return False

    # Build ffmpeg filter: volume-adjust each input then amix all
    inputs = []
    filter_parts = []
    for i, (cat, wav_path) in enumerate(valid):
        vol = volumes.get(cat, 1.0)
        inputs.extend(['-i', wav_path])
        filter_parts.append(f'[{i+1}:a]volume={vol:.3f}[a{i}]')

    n = len(filter_parts)
    mix_inputs = ''.join(f'[a{i}]' for i in range(n))
    filter_complex = (
        ';'.join(filter_parts)
        + f';{mix_inputs}amix=inputs={n}:duration=first:dropout_transition=0[aout]'
    )

    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, 'mixed.mp4')
        cmd = (
            [ffmpeg_exe, '-y', '-i', clip_path]
            + inputs
            + ['-filter_complex', filter_complex,
               '-map', '0:v?', '-map', '[aout]',
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-shortest', out_path]
        )
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            print('[AudioMixer] ffmpeg timed out after 120s — skipping mix')
            return False
        except Exception as e:
            print(f'[AudioMixer] ffmpeg error: {e}')
            return False

        if result.returncode != 0:
            err = result.stderr.decode(errors='replace').strip().splitlines()
            print(f'[AudioMixer] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
            return False

        try:
            os.replace(out_path, clip_path)
        except OSError as e:
            print(f'[AudioMixer] Could not replace clip: {e}')
            return False

    print(f'[AudioMixer] Mixed {n} tracks into {os.path.basename(clip_path)}')
    return True
