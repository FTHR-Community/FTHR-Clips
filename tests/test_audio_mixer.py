import sys, os, struct, tempfile
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')


def _make_wav(path: str, n_samples: int = 48000):
    """Write a 1-second silent float32 stereo WAV."""
    with open(path, 'wb') as f:
        data_bytes = n_samples * 2 * 4  # stereo float32
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_bytes))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<IHHIIHH', 16, 3, 2, 48000, 48000*2*4, 8, 32))
        f.write(b'data')
        f.write(struct.pack('<I', data_bytes))
        f.write(bytes(data_bytes))


def test_mix_empty_wavs_returns_false(tmp_path):
    from core.audio_mixer import mix_multiband_clip
    result = mix_multiband_clip(
        clip_path=str(tmp_path / 'nonexistent.mp4'),
        category_wavs={},
        volumes={},
        ffmpeg_exe='/usr/bin/ffmpeg',
    )
    assert result is False


def test_mix_missing_wav_skipped(tmp_path):
    """A category_wavs entry pointing to a nonexistent file is skipped."""
    from core.audio_mixer import mix_multiband_clip
    result = mix_multiband_clip(
        clip_path=str(tmp_path / 'nonexistent.mp4'),
        category_wavs={'Game': str(tmp_path / 'missing.wav')},
        volumes={'Game': 1.0},
        ffmpeg_exe='/usr/bin/ffmpeg',
    )
    assert result is False  # no valid inputs → False


def test_mix_runs_with_ffmpeg(tmp_path):
    """Integration: mix two silent WAVs into a WAV 'clip'."""
    clip = str(tmp_path / 'clip.wav')
    _make_wav(clip)
    game_wav = str(tmp_path / 'game.wav')
    disc_wav = str(tmp_path / 'disc.wav')
    _make_wav(game_wav)
    _make_wav(disc_wav)

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        import pytest; pytest.skip('imageio_ffmpeg / ffmpeg not available')

    from core.audio_mixer import mix_multiband_clip
    result = mix_multiband_clip(
        clip_path=clip,
        category_wavs={'Game': game_wav, 'Discord': disc_wav},
        volumes={'Game': 1.0, 'Discord': 0.7},
        ffmpeg_exe=ffmpeg,
    )
    assert result is True
    assert os.path.exists(clip)


def test_mix_subprocess_has_timeout(tmp_path, monkeypatch):
    """BUG-03: mix_multiband_clip must pass timeout= to subprocess.run() to prevent
    permanent hang when ffmpeg stalls on a large or corrupt file."""
    import subprocess
    import unittest.mock as mock

    captured_kwargs = []

    def mock_run(cmd, **kwargs):
        captured_kwargs.append(kwargs)
        m = mock.MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr(subprocess, 'run', mock_run)

    clip = str(tmp_path / 'clip.wav')
    _make_wav(clip)
    game_wav = str(tmp_path / 'game.wav')
    _make_wav(game_wav)

    from core.audio_mixer import mix_multiband_clip
    mix_multiband_clip(clip, {'Game': game_wav}, {'Game': 1.0}, '/usr/bin/ffmpeg')

    assert len(captured_kwargs) > 0, "subprocess.run was never called"
    for call_kw in captured_kwargs:
        assert 'timeout' in call_kw, (
            f"subprocess.run called without timeout= — can hang permanently. "
            f"kwargs were: {call_kw}"
        )
