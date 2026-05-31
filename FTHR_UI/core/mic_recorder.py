"""
mic_recorder.py — continuous microphone capture into a rolling in-memory ring.

Why this exists (a.k.a. why mic is recorded separately from desktop audio)
--------------------------------------------------------------------------
The C++ engine only grabs system loopback — i.e. everything you HEAR. It has
no idea your mic exists, so out of the box your clips are missing your voice,
which kind of defeats the point of a hype-moment clipper. Properly teaching the
engine to mix in a second audio device is a real architecture project. Nobody
has time for that this sprint.

So we cheat: Python keeps its own always-on mic recording in a ring buffer, and
when you save a clip we yank out the matching time slice and ffmpeg-mux it onto
the video the engine just wrote. Two separate captures, glued together after the
fact. The hard part is timing alignment (see the frame-count clock below), which
is why this file is way more paranoid about timestamps than it looks like it
should be. This is fine.

How it works
------------
- A `sounddevice.InputStream` runs at 48 kHz mono. Each chunk is appended
  to a deque of (monotonic_timestamp, samples).
- The deque is trimmed to keep only the last `keep_seconds` of audio
  (default 90 s — comfortably more than any clip length the UI exposes).
- `extract_segment(end_time, duration)` returns the slice of mic samples
  that aligns with the time window the clip covers.

Threading
---------
- `sounddevice` runs the callback on its own audio thread. The deque is
  guarded by a lock; callers from the UI thread can read safely.
- Singleton pattern so the rest of the app shares one capture stream.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

try:
    import numpy as _np
    import sounddevice as _sd
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False


# 48kHz mono. 48k because that's what basically every modern audio stack runs
# at internally, so we dodge a resample. Mono because it's a mic, not an orchestra.
SAMPLE_RATE = 48000
BLOCK_SIZE  = 1024
# Keep 90s of mic in RAM. That's comfortably longer than any clip length the UI
# offers, with slack. Costs ~17MB of float32. cope, you have 16 gigs.
KEEP_SECONDS = 90


class MicRecorder:
    """Continuous mic capture singleton. Always-on while a device is selected."""

    _instance: Optional['MicRecorder'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self._stream = None
        self._device_index = None
        self._gain = 1.0
        self._lock = threading.Lock()
        self._rms_listeners: list = []
        # deque of (mono_time_at_block_end, np.ndarray[float32, mono samples])
        self._chunks: deque = deque()
        # Frame-count-based timing: avoids GIL-delayed callback timestamps.
        # _stream_start_time is time.monotonic() when the stream started.
        # _total_frames is the cumulative sample count delivered by PortAudio.
        # t_end per chunk = _stream_start_time + _total_frames / SAMPLE_RATE
        # This is accurate regardless of when the Python callback fires.
        self._stream_start_time: float = 0.0
        self._total_frames: int = 0

    @staticmethod
    def is_available() -> bool:
        return _AVAILABLE

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self, device_index, gain: float = 1.0) -> bool:
        """
        Start (or restart) recording from the given input device.
        device_index=None uses the system default mic.
        Returns True if a stream is running afterwards.
        """
        if not _AVAILABLE:
            return False
        self.stop()
        self._device_index = device_index
        self._gain = max(0.0, gain)
        try:
            self._stream = _sd.InputStream(
                device=device_index,
                channels=1,
                dtype='float32',
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                callback=self._on_audio,
            )
            self._stream.start()
            # Record the reference point AFTER start() so the clock is running.
            # All chunk timestamps will be stream_start_time + frames/SR.
            with self._lock:
                self._stream_start_time = time.monotonic()
                self._total_frames = 0
            return True
        except Exception as e:
            print(f'[MicRecorder] Start failed: {e}')
            self._stream = None
            return False

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._chunks.clear()

    def is_running(self) -> bool:
        return self._stream is not None

    def set_gain(self, gain: float):
        self._gain = max(0.0, gain)

    def add_rms_listener(self, fn):
        """Register a callable(rms: float) called from the audio thread."""
        with self._lock:
            if fn not in self._rms_listeners:
                self._rms_listeners.append(fn)

    def remove_rms_listener(self, fn):
        with self._lock:
            try:
                self._rms_listeners.remove(fn)
            except ValueError:
                pass

    # ── Audio thread callback ────────────────────────────────────────────

    def _on_audio(self, indata, frames, time_info, status):
        try:
            arr = indata if indata.ndim == 1 else indata[:, 0]
            # When the gain is 1.0 (default — most users), skip the
            # multiply-and-copy path entirely. The buffer sounddevice hands
            # us is reused on the next callback, so we still need a copy
            # before stashing it; np.array() with copy=True is cheaper than
            # `.copy() * gain` when no scaling is needed.
            gain = self._gain
            if gain == 1.0:
                samples = _np.array(arr, dtype=_np.float32, copy=True)
            else:
                samples = _np.asarray(arr, dtype=_np.float32) * gain
            with self._lock:
                # Use frame-count-based time, NOT time.monotonic().
                #
                # time.monotonic() inside the callback reflects when Python
                # scheduled this call, which can be seconds late if the GIL
                # is held by the UI thread during a heavy game frame. That
                # delay shifts every chunk's timestamp forward, making
                # extract_segment() pull audio from the wrong window.
                #
                # Instead: t_end = stream_start + frames_delivered / SR.
                # PortAudio delivers frames in lockstep with the hardware
                # clock, so this is accurate to within a few microseconds
                # regardless of Python scheduling latency.
                self._total_frames += frames
                t_end = self._stream_start_time + self._total_frames / SAMPLE_RATE
                self._chunks.append((t_end, samples))
                # Trim history older than KEEP_SECONDS
                cutoff = t_end - KEEP_SECONDS
                while self._chunks and self._chunks[0][0] < cutoff:
                    self._chunks.popleft()
                # Notify RMS listeners (e.g. level meter) without a second stream
                if self._rms_listeners:
                    import numpy as _np_local
                    rms = float(_np_local.sqrt(_np_local.mean(_np_local.square(samples))))
                    for fn in list(self._rms_listeners):
                        try:
                            fn(rms)
                        except Exception:
                            pass
        except Exception:
            # Never EVER raise from the audio callback. sounddevice/PortAudio
            # will quietly kill the stream and you'll spend an afternoon
            # wondering why the mic "randomly stopped working." swallow it.
            pass

    # ── Extraction for save_clip ─────────────────────────────────────────

    def extract_segment(self, end_time: float, duration: float):
        """
        Return mono float32 samples covering [end_time - duration, end_time].
        Returns None if mic isn't running or no samples are available.
        Times are `time.monotonic()` references.
        """
        if not self.is_running():
            return None

        start_time = end_time - duration
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return None

        # Each chunk's timestamp is the time the BLOCK ENDED. Each block has
        # BLOCK_SIZE samples, so the block covers [t - BLOCK_SIZE/SR, t].
        block_dur = BLOCK_SIZE / SAMPLE_RATE
        out = []
        for t_end, samples in chunks:
            t_start = t_end - block_dur
            if t_end < start_time:
                continue
            if t_start > end_time:
                break
            # Trim partial blocks at edges
            if t_start < start_time:
                skip = int((start_time - t_start) * SAMPLE_RATE)
                samples = samples[skip:]
            if t_end > end_time:
                trim = int((t_end - end_time) * SAMPLE_RATE)
                if trim > 0:
                    samples = samples[:-trim]
            if samples.size:
                out.append(samples)

        if not out:
            return None
        return _np.concatenate(out)


def write_wav(path: str, samples) -> bool:
    """Write a mono float32 numpy array to a 16-bit PCM WAV at SAMPLE_RATE."""
    try:
        import wave
        # Convert float32 [-1, 1] to int16
        clipped = _np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(_np.int16)
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return True
    except Exception as e:
        print(f'[MicRecorder] write_wav failed: {e}')
        return False
