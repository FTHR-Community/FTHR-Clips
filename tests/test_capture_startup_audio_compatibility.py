"""Regression for default audio options rejected by legacy native parsers."""
import os

from core.engine_discovery import find_source_engine, isolated_source_engines
from core.native_audio_capabilities import native_audio_policy_arguments


def test_default_programs_never_sends_unknown_optional_flags():
    assert native_audio_policy_arguments() == []
    assert native_audio_policy_arguments(exclusions='', keep_backup=True) == []


def test_device_and_audio_off_modes_do_not_send_inactive_saved_policies():
    assert native_audio_policy_arguments(programs_enabled=False,
        exclusions='discord', keep_backup=False) == []
    assert native_audio_policy_arguments(audio_enabled=False,
        exclusions='discord', keep_backup=False) == []


def test_only_active_nondefault_program_policies_are_sent():
    assert native_audio_policy_arguments(exclusions='discord') == [
        '--audio-exclusions', 'discord']
    assert native_audio_policy_arguments(keep_backup=False) == [
        '--keep-system-audio-backup', '0']
    assert native_audio_policy_arguments(exclusions='discord', keep_backup=False) == [
        '--audio-exclusions', 'discord', '--keep-system-audio-backup', '0']


def test_source_discovery_includes_isolated_engine_but_not_packaged_or_test_binaries(tmp_path):
    selected = tmp_path / 'build/system-audio-backup/engine/FTHRclips.exe'
    excluded = [tmp_path / 'build/system-audio-backup/tests/FTHRclips_tests.exe',
                tmp_path / 'build/package/dist/_internal/engine/FTHRclips.exe']
    for path in [selected, *excluded]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    assert isolated_source_engines(tmp_path) == [selected]


def test_source_discovery_prefers_newest_native_output(tmp_path):
    capture_root = tmp_path / 'FTHRcapture'
    stale_direct = capture_root / 'FTHRclips/x64/CFRReleaseFinal/FTHRclips.exe'
    solution = capture_root / 'x64/Release/FTHRClips.exe'
    isolated = tmp_path / 'build/current/engine/FTHRclips.exe'
    portable = tmp_path / 'dist/FTHRClips/_internal/engine/FTHRclips.exe'
    assert find_source_engine(tmp_path) is None
    for position, path in enumerate([stale_direct, isolated, solution, portable]):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture')
        timestamp = 1_700_000_000 + position * 10
        os.utime(path, (timestamp, timestamp))
    assert find_source_engine(tmp_path) == solution

    newest_time = 1_700_000_050
    os.utime(isolated, (newest_time, newest_time))
    assert find_source_engine(tmp_path) == isolated

    os.utime(solution, (newest_time, newest_time))
    assert find_source_engine(tmp_path) == isolated
