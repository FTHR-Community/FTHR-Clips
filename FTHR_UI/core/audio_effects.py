"""Optional FFmpeg microphone effects supplied by trusted feature plugins.

Factories must return a filter chain (or an empty string). Referenced assets
must outlive the plugin: open clips retain their graph until reopened.
"""
from threading import RLock

_lock = RLock()
_effects = {}


def register_microphone_effect(effect_id, filter_factory, *, owner):
    if not isinstance(effect_id, str) or not effect_id.strip():
        raise ValueError('effect_id must be a nonempty string')
    if not callable(filter_factory):
        raise TypeError('filter_factory must be callable')
    with _lock:
        previous = _effects.get(effect_id)
        if previous is not None and previous[0] != owner:
            raise ValueError(f'microphone effect already registered: {effect_id}')
        _effects[effect_id] = (owner, filter_factory)


def unregister_microphone_effect(effect_id, *, owner):
    with _lock:
        if effect_id in _effects and _effects[effect_id][0] == owner:
            del _effects[effect_id]


def unregister_microphone_effects(owner):
    """Release all registrations, including those left by a failed start/stop."""
    with _lock:
        for effect_id in list(_effects):
            if _effects[effect_id][0] == owner:
                del _effects[effect_id]


def microphone_effect_filter():
    """Resolve the current settings once when opening a clip."""
    with _lock:
        factories = [entry[1] for entry in _effects.values()]
    stages = []
    for factory in factories:
        stage = factory()
        if not isinstance(stage, str):
            raise TypeError('microphone effect factory must return a string')
        if stage.strip():
            stages.append(stage.strip())
    return ','.join(stages)
