"""Contract tests for immutable game capture profiles."""

from __future__ import annotations

import json

import pytest

from core.game_profiles import GameProfileCatalog
from core.game_profiles import ProfileDataError
from core.game_profiles import ProfileValidationError
from core.game_profiles import MAX_DOCUMENT_BYTES
from core.game_profiles import canonical_exe_path
from core.game_profiles import create_profile
from core.game_profiles import delete_profile
from core.game_profiles import empty_catalog
from core.game_profiles import find_profile
from core.game_profiles import rename_profile
from core.game_profiles import reset_override
from core.game_profiles import resolve_capture_settings
from core.game_profiles import update_profile
from core.settings_manager import SettingsManager
from core.capture_settings import audio_plan_from_settings


def _profile(name='Game', exe=r'C:\Games\Game\Game.exe', **overrides):
    catalog = create_profile(empty_catalog(), name, exe, overrides or {})
    return catalog, catalog.profiles[0]


def test_windows_exe_identity_is_lexical_and_exact():
    assert canonical_exe_path(r'c:/Games/./Game/Game.exe') == (
        r'c:\games\game\game.exe')
    assert canonical_exe_path(r'\\SERVER\Share\Game\Game.EXE') == (
        r'\\server\share\game\game.exe')
    assert canonical_exe_path(r'C:\Games\Game\..\Game\Game.exe') == (
        r'c:\games\game\game.exe')
    assert canonical_exe_path(r'C:\Games\Game\Game.exe') != canonical_exe_path(
        r'C:\Other\Game\Game.exe')

    for invalid in (
            r'Game.exe', r'C:Games\Game.exe', r'\Games\Game.exe',
            r'\\server\share', r'\\server\share.exe',
            r'\\?\C:\Games\Game.exe', r'C:\Games\Game.exe:ads',
            r'C:\Games.\Game.exe', r'C:\Games\Game. exe',
            r'C:\Games\Game<bad>.exe', r'C:\Games\Game?.exe',
            '\u00c5:\\Games\\Game.exe'):
        with pytest.raises(ProfileDataError):
            canonical_exe_path(invalid)


def test_valid_document_roundtrip_and_single_line_unicode_validation():
    catalog, profile = _profile(name='Valid profile')
    raw = catalog.to_dict()
    assert GameProfileCatalog.from_dict(raw).to_dict() == raw
    for separator in ('\x85', '\u2028', '\u2029'):
        with pytest.raises(ProfileValidationError):
            _profile(name=f'bad{separator}name')


def test_sparse_resolution_preserves_false_zero_and_null_and_detaches():
    # Invalid profile values are rejected at the edit boundary.
    with pytest.raises(ProfileValidationError):
        _profile(clips={'clip_length': 0})

    catalog, profile = _profile(
        audio={
            'audio_capture_enabled': False,
            'mic_volume': 0,
            'audio_source_mode': None,
        },
        camera={'camera_enabled': False, 'camera_device_index': 0},
    )
    globals_a = {
        'audio_capture_enabled': True,
        'mic_volume': 100,
        'audio_source_mode': 'device',
        'camera_enabled': True,
        'camera_device_index': 4,
    }
    resolved_a = resolve_capture_settings(catalog, profile.exe_path, globals_a)
    assert resolved_a['audio_capture_enabled'] is False
    assert resolved_a['mic_volume'] == 0
    assert resolved_a['audio_source_mode'] is None
    assert resolved_a['camera_device_index'] == 0
    globals_a['mic_volume'] = 200
    assert resolved_a['mic_volume'] == 0


@pytest.mark.parametrize('group,key,value', [
    ('clips', 'framerate', True),
    ('audio', 'audio_capture_mode', []),
    ('audio', 'audio_source_mode', {}),
    ('audio', 'mic_volume', True),
    ('camera', 'camera_device_index', True),
])
def test_profile_leaf_validation_keeps_error_contract(group, key, value):
    with pytest.raises(ProfileValidationError):
        _profile(**{group: {key: value}})


def test_sparse_resolution_valid_override_masks_invalid_inherited_global():
    catalog, profile = _profile(clips={'framerate': 60})
    invalid_globals = {'framerate': 'broken'}
    resolved = resolve_capture_settings(
        catalog, profile.exe_path, invalid_globals)
    assert resolved['framerate'] == 60
    with pytest.raises(ProfileDataError):
        resolve_capture_settings(catalog, r'C:\Other\Other.exe', invalid_globals)


def test_sparse_resolution_tracks_omitted_global_changes_and_reset_inheritance():
    catalog, profile = _profile(clips={'framerate': 60}, audio={
        'audio_capture_enabled': False,
    })
    globals_a = {'framerate': 30, 'mic_volume': 10,
                 'audio_capture_enabled': True}
    first = resolve_capture_settings(catalog, profile.exe_path, globals_a)
    globals_a['mic_volume'] = 20
    second = resolve_capture_settings(catalog, profile.exe_path, globals_a)
    assert first['framerate'] == second['framerate'] == 60
    assert first['mic_volume'] == 10
    assert second['mic_volume'] == 20
    reset = reset_override(catalog, profile.id, 'clips', 'framerate')
    inherited = resolve_capture_settings(reset, profile.exe_path, globals_a)
    assert inherited['framerate'] == 30
    group_reset = reset_override(catalog, profile.id, 'audio')
    audio_inherited = resolve_capture_settings(
        group_reset, profile.exe_path, globals_a)
    assert audio_inherited['audio_capture_enabled'] is True


def test_resolved_settings_are_immutable_and_detached_compatibility_views():
    catalog, profile = _profile(
        audio={'audio_device_tracks': [{'id': 'mic-1', 'name': 'Mic'}]},
        camera={'camera_overlay_rect': {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}},
    )
    resolved = resolve_capture_settings(catalog, profile.exe_path, {})
    assert len(resolved) >= 2
    tracks = resolved['audio_device_tracks']
    tracks[0]['name'] = 'changed'
    rect = resolved['camera_overlay_rect']
    rect['x'] = 0.9
    assert resolved['audio_device_tracks'][0]['name'] == 'Mic'
    assert resolved['camera_overlay_rect']['x'] == 0.1
    assert audio_plan_from_settings(resolved) == (
        True, False, [{'id': 'mic-1', 'name': 'Mic'}])
    with pytest.raises(TypeError):
        resolved['framerate'] = 30


def test_resolved_settings_reject_attribute_mutation_and_deletion():
    catalog, profile = _profile()
    resolved = resolve_capture_settings(catalog, profile.exe_path, {})
    with pytest.raises(AttributeError):
        resolved._values = {}
    with pytest.raises(AttributeError):
        resolved.extra = 'changed'
    with pytest.raises(AttributeError):
        del resolved._values
    with pytest.raises(AttributeError):
        del resolved.extra


def test_mutations_keep_stable_ids_and_revisions():
    catalog, profile = _profile()
    assert catalog.revision == 1
    renamed = rename_profile(catalog, profile.id, 'Renamed')
    assert renamed.profiles[0].id == profile.id
    assert renamed.profiles[0].revision == 2
    assert renamed.revision == 2
    no_op = rename_profile(renamed, profile.id, 'Renamed')
    assert no_op == renamed
    updated = update_profile(
        renamed, profile.id,
        overrides={'clips': {'framerate': 144}},
    )
    assert updated.profiles[0].revision == 3
    reset = reset_override(updated, profile.id, 'clips', 'framerate')
    assert reset.profiles[0].overrides == {}
    deleted = delete_profile(reset, profile.id)
    assert deleted.profiles == ()
    assert deleted.revision == reset.revision + 1


def test_catalog_rejects_duplicates_unknown_fields_and_future_versions():
    first, profile = _profile()
    raw = first.to_dict()
    raw['profiles'].append(dict(raw['profiles'][0]))
    with pytest.raises(ProfileDataError):
        GameProfileCatalog.from_dict(raw)
    raw = first.to_dict()
    raw['profiles'][0]['unexpected'] = True
    with pytest.raises(ProfileDataError):
        GameProfileCatalog.from_dict(raw)
    with pytest.raises(ProfileDataError):
        GameProfileCatalog.from_dict({
            'version': 2, 'revision': 1, 'profiles': [],
        })
    raw = first.to_dict()
    raw['profiles'][0]['overrides'] = {1: {}}
    with pytest.raises(ProfileDataError):
        GameProfileCatalog.from_dict(raw)


def test_duplicate_canonical_executable_and_duplicate_uuid_are_rejected():
    catalog, profile = _profile()
    with pytest.raises(ProfileValidationError):
        create_profile(catalog, 'Second', r'C:/Games/Game/./Game.exe')
    with pytest.raises(ProfileValidationError):
        create_profile(catalog, 'Second', r'C:\Other\Other.exe',
                       profile_id=profile.id)
    reused = create_profile(empty_catalog(), 'Second', r'C:\Other\Other.exe',
                             profile_id=profile.id)
    assert reused.profiles[0].id == profile.id


def test_detached_catalog_views_cannot_mutate_internal_values():
    catalog, profile = _profile(
        audio={'audio_device_tracks': [{'id': 'mic-1', 'name': 'Mic'}]},
        camera={'camera_overlay_rect': {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}},
    )
    exported = catalog.to_dict()
    exported['profiles'][0]['overrides']['audio']['audio_device_tracks'][0]['name'] = 'changed'
    exported['profiles'][0]['overrides']['camera']['camera_overlay_rect']['x'] = 0.9
    assert catalog.profiles[0].overrides['audio']['audio_device_tracks'][0]['name'] == 'Mic'
    assert catalog.profiles[0].overrides['camera']['camera_overlay_rect']['x'] == 0.1


@pytest.mark.parametrize('tracks', [
    [{'id': 'a;bad', 'name': 'Mic'}],
    [{'id': 'a', 'name': ''}],
    [{'id': 'a', 'name': 'x' * 81}],
    [{'id': 'a', 'name': 'bad\nname'}],
    [{'id': 'a', 'name': 123}],
    [{'id': 'a', 'name': 'Mic'}, {'id': 'a', 'name': 'Other'}],
])
def test_profile_audio_track_abi_validation_is_strict(tracks):
    with pytest.raises(ProfileValidationError):
        _profile(audio={'audio_device_tracks': tracks})


def test_profile_audio_track_names_normalize_raw_friendly_text():
    catalog, profile = _profile(audio={'audio_device_tracks': [
        {'id': 'mic-1', 'name': 'Mic-\u00e9'},
        {'id': 'game-1', 'name': 'L' * 80},
    ]})
    tracks = catalog.profiles[0].to_dict()['overrides']['audio']['audio_device_tracks']
    assert tracks == [
        {'id': 'mic-1', 'name': 'Mic-'},
        {'id': 'game-1', 'name': 'L' * 40},
    ]
    assert profile.id == catalog.profiles[0].id


def test_camera_rectangle_huge_integer_is_a_profile_validation_error():
    with pytest.raises(ProfileValidationError):
        _profile(camera={'camera_overlay_rect': {
            'x': 10 ** 400, 'y': 0, 'w': 1, 'h': 1,
        }})


def test_profile_lookup_uses_canonical_identity_not_basename():
    catalog, profile = _profile(exe=r'C:\Games\Game\Game.exe')
    assert find_profile(catalog, r'c:/Games/Game/./Game.exe') == profile
    assert find_profile(catalog, r'C:\Other\Game\Game.exe') is None
    assert find_profile(catalog, r'not-an-executable') is None
    with pytest.raises(ProfileDataError):
        find_profile({'version': 99, 'revision': 0, 'profiles': []},
                     profile.exe_path)


def test_settings_profile_cas_publishes_only_after_atomic_success(tmp_path, monkeypatch):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {'unrelated': {'sentinel': 1}}
    current = manager.read_game_capture_profiles()
    candidate = create_profile(current, 'Game', r'C:\Games\Game.exe')
    assert manager.save_game_capture_profiles(candidate, expected_revision=0)
    assert manager.read_game_capture_profiles() == candidate
    assert json.loads(manager.config_file.read_text())['game_capture_profiles'] == candidate.to_dict()
    assert not manager.save_game_capture_profiles(candidate, expected_revision=0)

    previous = manager.settings['game_capture_profiles']
    monkeypatch.setattr(manager, '_persist_settings_candidate', lambda _candidate: False)
    changed = rename_profile(candidate, candidate.profiles[0].id, 'Other')
    assert not manager.save_game_capture_profiles(changed, expected_revision=candidate.revision)
    assert manager.settings['game_capture_profiles'] == previous


@pytest.mark.parametrize('writer_mode', ['false', 'raise'])
def test_absent_catalog_writer_failure_preserves_ram_key_and_disk(
        tmp_path, monkeypatch, writer_mode):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    original_bytes = b'{"unrelated":{"sentinel":1}}\n'
    manager.config_file.write_bytes(original_bytes)
    manager.settings = {'unrelated': {'sentinel': 1}}
    before = dict(manager.settings)
    candidate = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')

    if writer_mode == 'false':
        monkeypatch.setattr(manager, '_persist_settings_candidate',
                            lambda _candidate: False)
        assert not manager.save_game_capture_profiles(candidate,
                                                      expected_revision=0)
    else:
        def fail_writer(_candidate):
            raise OSError('write failed')

        monkeypatch.setattr(manager, '_persist_settings_candidate', fail_writer)
        with pytest.raises(OSError):
            manager.save_game_capture_profiles(candidate, expected_revision=0)

    assert manager.settings == before
    assert 'game_capture_profiles' not in manager.settings
    assert manager.config_file.read_bytes() == original_bytes


def test_semantic_noop_has_no_writer_call(tmp_path, monkeypatch):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {}
    candidate = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    assert manager.save_game_capture_profiles(candidate, expected_revision=0)
    calls = []
    monkeypatch.setattr(manager, '_persist_settings_candidate',
                        lambda staged: calls.append(staged) or True)
    assert manager.save_game_capture_profiles(candidate, expected_revision=1)
    assert calls == []


def test_os_replace_failure_preserves_disk_and_ram(tmp_path, monkeypatch):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {}
    current = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    assert manager.save_game_capture_profiles(current, expected_revision=0)
    old_bytes = manager.config_file.read_bytes()
    old_ram = dict(manager.settings['game_capture_profiles'])
    changed = rename_profile(current, current.profiles[0].id, 'Changed')

    def fail_replace(*_args):
        raise OSError('replace failed')

    monkeypatch.setattr('core.settings_manager.os.replace', fail_replace)
    assert not manager.save_game_capture_profiles(changed, expected_revision=1)
    assert manager.config_file.read_bytes() == old_bytes
    assert manager.settings['game_capture_profiles'] == old_ram


@pytest.mark.parametrize('expected_revision', [None, True, -1, 1.5])
def test_profile_save_requires_explicit_strict_expected_revision(
        expected_revision):
    manager = object.__new__(SettingsManager)
    manager.config_file = None
    manager.settings = {}
    candidate = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    with pytest.raises((TypeError, ValueError)):
        manager.save_game_capture_profiles(
            candidate, expected_revision=expected_revision)
    assert 'game_capture_profiles' not in manager.settings


def test_profile_writer_observes_old_ram_before_commit_and_rolls_back_replace(
        tmp_path, monkeypatch):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {}
    candidate = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    observed = []
    original_writer = manager._persist_settings_candidate

    def writer(staged):
        observed.append(manager.settings.copy())
        return original_writer(staged)

    monkeypatch.setattr(manager, '_persist_settings_candidate', writer)
    assert manager.save_game_capture_profiles(candidate, expected_revision=0)
    assert 'game_capture_profiles' not in observed[0]
    previous = dict(manager.settings['game_capture_profiles'])
    old_bytes = manager.config_file.read_bytes()
    changed = rename_profile(candidate, candidate.profiles[0].id, 'Other')

    def fail_writer(_staged):
        raise OSError('replace')

    monkeypatch.setattr(manager, '_persist_settings_candidate', fail_writer)
    with pytest.raises(OSError):
        manager.save_game_capture_profiles(changed, expected_revision=1)
    assert manager.settings['game_capture_profiles'] == previous
    assert manager.config_file.read_bytes() == old_bytes


def test_unknown_future_document_is_unavailable_but_generic_save_preserves_it(tmp_path):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    future = {'version': 99, 'revision': 3, 'profiles': [], 'future': {'x': 1}}
    manager.settings = {'game_capture_profiles': future, 'sentinel': 'keep'}
    with pytest.raises(ProfileDataError):
        manager.read_game_capture_profiles()
    assert manager.save_settings()
    saved = json.loads(manager.config_file.read_text())
    assert saved['game_capture_profiles'] == future
    assert saved['sentinel'] == 'keep'


def test_future_catalog_dedicated_save_preserves_ram_and_raw_bytes(tmp_path):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    raw_bytes = b'{"version":99,"revision":3,"profiles":[],"future":{"x":1}}\n'
    manager.config_file.write_bytes(raw_bytes)
    future = {'version': 99, 'revision': 3, 'profiles': [], 'future': {'x': 1}}
    manager.settings = {'game_capture_profiles': future, 'sentinel': 'keep'}
    before = manager.settings['game_capture_profiles']
    with pytest.raises(ProfileDataError):
        manager.save_game_capture_profiles(empty_catalog(), expected_revision=3)
    assert manager.settings['game_capture_profiles'] is before
    assert manager.settings == {
        'game_capture_profiles': future, 'sentinel': 'keep'}
    assert manager.config_file.read_bytes() == raw_bytes


def _profile_document_with_path_fill(path_fill):
    profiles = []
    for index in range(128):
        profiles.append({
            'id': f'00000000-0000-0000-0000-{index + 1:012d}',
            'revision': 1,
            'name': f'Game {index}',
            'exe_path': 'C:\\Games\\' + ('g' * path_fill)
            + f'{index}.exe',
            'overrides': {},
        })
    return {'version': 1, 'revision': 0, 'profiles': profiles}


def _largest_profile_document_under(byte_limit):
    low, high = 0, 12_000
    while low < high:
        middle = (low + high + 1) // 2
        raw = _profile_document_with_path_fill(middle)
        encoded_size = len(json.dumps(
            raw, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if encoded_size <= byte_limit:
            low = middle
        else:
            high = middle - 1
    return _profile_document_with_path_fill(low), low


def test_catalog_count_and_utf8_document_size_limits():
    maximum = _profile_document_with_path_fill(0)
    assert len(GameProfileCatalog.from_dict(maximum).profiles) == 128
    unicode_document = _profile_document_with_path_fill(0)
    unicode_document['profiles'][0]['name'] = '\u00e9' * 80
    unicode_json = json.dumps(
        unicode_document, ensure_ascii=False, separators=(',', ':'))
    assert len(unicode_json.encode('utf-8')) > len(unicode_json)
    GameProfileCatalog.from_dict(unicode_document)
    too_many = dict(maximum)
    too_many['profiles'] = maximum['profiles'] + [dict(maximum['profiles'][0])]
    too_many['profiles'][-1]['id'] = '00000000-0000-0000-0000-999999999999'
    too_many['profiles'][-1]['exe_path'] = r'C:\Games\extra.exe'
    with pytest.raises(ProfileValidationError):
        GameProfileCatalog.from_dict(too_many)

    near_limit, fill = _largest_profile_document_under(MAX_DOCUMENT_BYTES)
    encoded_size = len(json.dumps(
        near_limit, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    assert MAX_DOCUMENT_BYTES - 256 <= encoded_size <= MAX_DOCUMENT_BYTES
    assert len(GameProfileCatalog.from_dict(near_limit).profiles) == 128
    with pytest.raises(ProfileValidationError):
        GameProfileCatalog.from_dict(_profile_document_with_path_fill(fill + 1))

    multibyte_limit = json.loads(json.dumps(near_limit))
    multibyte_limit['profiles'][0]['exe_path'] = (
        multibyte_limit['profiles'][0]['exe_path'].replace('g', '\u00e9'))
    multibyte_json = json.dumps(
        multibyte_limit, ensure_ascii=False, separators=(',', ':'))
    assert len(multibyte_json) <= MAX_DOCUMENT_BYTES < len(multibyte_json.encode('utf-8'))
    with pytest.raises(ProfileValidationError):
        GameProfileCatalog.from_dict(multibyte_limit)


@pytest.mark.parametrize('group,key,values', [
    ('clips', 'clip_length', [5, 900, 4, 901, True]),
    ('clips', 'framerate', [15, 240, 14, 241, True]),
    ('clips', 'custom_bitrate_kbps', [500, 200000, 499, 200001, True]),
    ('audio', 'mic_volume', [0, 200, -1, 201, True]),
    ('camera', 'camera_device_index', [0, 255, -1, 256, True]),
])
def test_profile_numeric_boundaries_are_strict(group, key, values):
    for value in values[:2]:
        _profile(**{group: {key: value}})
    for value in values[2:]:
        with pytest.raises(ProfileValidationError):
            _profile(**{group: {key: value}})


def test_profile_endpoints_tracks_rectangles_and_unknown_fields_are_strict():
    _profile(audio={'mic_device_id': None, 'playback_device_id': ''})
    for endpoint in (' mic', 'mic ', 'mic;bad', 'mic|bad', 'mic\n'):
        with pytest.raises(ProfileValidationError):
            _profile(audio={'mic_device_id': endpoint})
    with pytest.raises(ProfileValidationError):
        _profile(audio={'mic_device_id': 'x' * 2049})
    tracks = [{'id': f'id-{index}', 'name': f'Mic {index}'}
              for index in range(4)]
    _profile(audio={'audio_device_tracks': tracks})
    with pytest.raises(ProfileValidationError):
        _profile(audio={'audio_device_tracks': tracks + [
            {'id': 'id-4', 'name': 'Mic 4'}]})
    with pytest.raises(ProfileValidationError):
        _profile(camera={'camera_overlay_rect': {
            'x': True, 'y': 0, 'w': 1, 'h': 1}})
    with pytest.raises(ProfileValidationError):
        _profile(camera={'camera_overlay_rect': {
            'x': float('inf'), 'y': 0, 'w': 1, 'h': 1}})
    with pytest.raises(ProfileValidationError):
        _profile(camera={'camera_overlay_rect': {
            'x': 0, 'y': 0, 'w': 1}})
    with pytest.raises(ProfileValidationError):
        _profile(clips={'unknown': 1})
    with pytest.raises(ProfileValidationError):
        _profile(**{'unknown_group': {'framerate': 60}})


@pytest.mark.parametrize('raw', [None, {}, {'version': 1}, 'future'])
def test_present_invalid_catalog_is_rejected_without_repair(raw):
    manager = object.__new__(SettingsManager)
    manager.config_file = None
    manager.settings = {'game_capture_profiles': raw}
    with pytest.raises(ProfileDataError):
        manager.read_game_capture_profiles()
    with pytest.raises(ProfileDataError):
        manager.save_game_capture_profiles(empty_catalog(), expected_revision=0)
    assert manager.settings['game_capture_profiles'] is raw


@pytest.mark.parametrize('value', [True, 1.5, -1, None])
def test_profile_catalog_rejects_strict_revision_values(value):
    with pytest.raises(ProfileDataError):
        GameProfileCatalog(version=1, revision=value, profiles=[])


def test_invalid_revision_transition_is_rejected_without_disk_mutation(tmp_path):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {}
    current = create_profile(empty_catalog(), 'Game', r'C:\Games\Game.exe')
    assert manager.save_game_capture_profiles(current, expected_revision=0)
    forged = GameProfileCatalog(
        1, current.revision + 1,
        (type(current.profiles[0])(
            current.profiles[0].id, 7, current.profiles[0].name,
            current.profiles[0].exe_path, current.profiles[0].overrides),),
    )
    with pytest.raises(ProfileValidationError):
        manager.save_game_capture_profiles(forged, expected_revision=1)
    assert manager.read_game_capture_profiles() == current
