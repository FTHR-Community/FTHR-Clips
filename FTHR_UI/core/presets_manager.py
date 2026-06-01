import json
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
        with open(self._path, 'w') as f:
            json.dump(data, f, indent=2)

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
