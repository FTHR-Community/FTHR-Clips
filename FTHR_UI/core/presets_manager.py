import json
import os
import tempfile
from pathlib import Path


PRESET_KEYS = [
    'clip_length', 'extended_clip_length', 'framerate',
    'resolution', 'bitrate_level', 'codec_pref', 'encoder_preset',
    'audio_capture_enabled', 'multiband_audio_enabled',
]


class PresetsManager:
    def __init__(self):
        self._path = Path.home() / '.fthr' / 'presets.json'

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: dict):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file first, then atomically replace — prevents wiping
        # all presets if json.dump raises (e.g. non-serialisable value).
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix='.json.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            raise

    def names(self) -> list[str]:
        return sorted(self._read().keys())

    def load(self, name: str) -> dict | None:
        return self._read().get(name)

    def save(self, name: str, data: dict):
        presets = self._read()
        presets[name] = data
        self._write(presets)

    def delete(self, name: str):
        presets = self._read()
        presets.pop(name, None)
        self._write(presets)
