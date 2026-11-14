from __future__ import annotations

from core.capture_settings import (
    MAX_AUDIO_DEVICE_TRACKS,
    effective_audio_source_mode,
    encode_audio_device_tracks,
    normalize_audio_device_tracks,
    resolve_audio_plan,
    resolve_audio_tracks,
)


def test_device_list_drops_malformed_and_duplicate_entries():
    tracks = normalize_audio_device_tracks([
        'junk',
        {'id': '', 'name': 'No id'},
        {'id': '{a}', 'name': 'Chat'},
        {'id': '{a}', 'name': 'Duplicate'},
        {'id': '{b}', 'name': '   '},
    ])

    assert tracks == [
        {'id': '{a}', 'name': 'Chat'},
        {'id': '{b}', 'name': 'Audio Device 2'},
    ]


def test_device_list_is_capped_and_ignores_non_lists():
    many = [{'id': f'{{{i}}}', 'name': f'D{i}'} for i in range(9)]

    assert len(normalize_audio_device_tracks(many)) == MAX_AUDIO_DEVICE_TRACKS
    assert normalize_audio_device_tracks(None) == []
    assert normalize_audio_device_tracks('x') == []


def test_names_cannot_break_the_launch_argument():
    tracks = normalize_audio_device_tracks(
        [{'id': '{a}', 'name': 'Game | Chat; mix'}])

    assert tracks[0]['name'] == 'Game Chat mix'
    assert encode_audio_device_tracks(tracks) == 'Game Chat mix;{a}'


def test_encoded_argument_joins_entries_for_the_engine():
    encoded = encode_audio_device_tracks([
        {'id': '{a}', 'name': 'Chat'}, {'id': '{b}', 'name': 'Game'}])

    assert encoded == 'Chat;{a}|Game;{b}'
    assert encode_audio_device_tracks([]) == ''


def test_default_settings_keep_the_previous_track_behavior():
    assert resolve_audio_tracks('combined', [], None) == (False, False)
    assert resolve_audio_tracks('separated', [], None) == (True, True)


def test_per_program_option_is_independent_of_the_mode():
    assert resolve_audio_tracks('separated', [], False) == (True, False)
    assert resolve_audio_tracks('combined', [], True) == (True, True)


def test_device_tracks_keep_the_clip_multi_track_without_program_tracks():
    devices = [{'id': '{a}', 'name': 'Chat'}]

    assert resolve_audio_tracks('combined', devices, None) == (True, False)
    assert resolve_audio_tracks('combined', devices, False) == (True, False)


def test_names_are_limited_to_the_manifest_character_set():
    tracks = normalize_audio_device_tracks([
        {'id': '{a}', 'name': 'Chat & Spiel \u00e4'},
        {'id': '{b}', 'name': '\u00e4\u00f6'},
    ])

    assert tracks[0]['name'] == 'Chat Spiel'
    assert tracks[1]['name'] == 'Audio Device 2'


def test_saved_record_mode_wins_over_derived_defaults():
    devices = [{'id': '{a}', 'name': 'Chat'}]

    assert effective_audio_source_mode(
        'device', 'separated', devices, True) == 'device'
    assert effective_audio_source_mode(None, 'combined', [], None) == 'device'
    assert effective_audio_source_mode(None, 'separated', [], None) == 'programs'
    assert effective_audio_source_mode(None, 'combined', devices, None) == 'devices'


def test_record_mode_decides_what_the_engine_records():
    devices = [{'id': '{a}', 'name': 'Chat'}]

    assert resolve_audio_plan('device', 'combined', devices, True) == (
        False, False, [])
    assert resolve_audio_plan('programs', 'combined', devices, None) == (
        True, True, [])
    assert resolve_audio_plan('devices', 'combined', devices, None) == (
        True, False, devices)
    assert resolve_audio_plan('devices', 'combined', [], None) == (
        True, False, [])
