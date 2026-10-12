"""Reject Linux artifacts with unapproved FFmpeg libraries or metadata.

Fixtures use manifests, hashes, and ELF headers without requiring FFmpeg.
"""

import hashlib
import json
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import verify_release_licenses as vrl  # noqa: E402


REAL_MANIFEST = ROOT / 'tools' / 'ffmpeg_manifest_linux.json'


@pytest.fixture
def manifest_root(tmp_path):
    """A tree containing only tools/ffmpeg_manifest_linux.json."""
    (tmp_path / 'tools').mkdir()
    shutil.copy2(REAL_MANIFEST, tmp_path / 'tools' / 'ffmpeg_manifest_linux.json')
    return tmp_path


def _manifest_data():
    return json.loads(REAL_MANIFEST.read_text(encoding='utf-8'))


def _write_lib(directory: Path, name: str, payload: bytes = b'not a real elf'):
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(payload)
    return p


# The manifest itself

def test_real_manifest_passes(manifest_root):
    rep = vrl.Report()
    vrl.check_linux_manifest(manifest_root, rep)
    assert not rep.failures, rep.failures


def test_missing_manifest_fails(tmp_path):
    rep = vrl.Report()
    vrl.check_linux_manifest(tmp_path, rep)
    assert rep.failures, 'an absent Linux manifest must fail the gate'


@pytest.mark.parametrize('drop', ['version', 'license', 'source',
                                  'shipped_files_sha256'])
def test_incomplete_manifest_fails(tmp_path, drop):
    data = _manifest_data()
    del data[drop]
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools' / 'ffmpeg_manifest_linux.json').write_text(
        json.dumps(data), encoding='utf-8')
    rep = vrl.Report()
    vrl.check_linux_manifest(tmp_path, rep)
    assert rep.failures, f'manifest without "{drop}" must fail'


def test_gpl_manifest_licence_fails(tmp_path):
    data = _manifest_data()
    data['license'] = 'GPL v3 or later'
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools' / 'ffmpeg_manifest_linux.json').write_text(
        json.dumps(data), encoding='utf-8')
    rep = vrl.Report()
    vrl.check_linux_manifest(tmp_path, rep)
    assert rep.failures


# The shipped libraries

def test_documented_library_with_correct_hash_passes(manifest_root, tmp_path):
    """A library whose bytes hash to the manifest value is accepted."""
    payload = b'pretend this is libavcodec'
    digest = hashlib.sha256(payload).hexdigest()
    data = _manifest_data()
    name = 'libavcodec.so.62.28.102'
    data['shipped_files_sha256'] = {name: digest}
    (manifest_root / 'tools' / 'ffmpeg_manifest_linux.json').write_text(
        json.dumps(data), encoding='utf-8')

    art = tmp_path / 'artifact'
    _write_lib(art, name, payload)
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_hash_mismatch_fails(manifest_root, tmp_path):
    """The exact scenario a swapped-in library produces."""
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.62.28.102', b'tampered content')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures, 'a library that does not match its recorded hash must fail'


def test_system_gpl_soname_fails(manifest_root, tmp_path):
    """libavcodec.so.60 is Ubuntu's GPL build — the AUDIT-014 failure mode."""
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.60')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures


def test_undocumented_library_fails(manifest_root, tmp_path):
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.99.1.1')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures, 'an FFmpeg library absent from the manifest must fail'


def test_postproc_is_caught(manifest_root, tmp_path):
    """libpostproc only exists in GPL builds; it must never appear."""
    art = tmp_path / 'artifact'
    _write_lib(art, 'libpostproc.so.57')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures


def test_missing_manifest_blocks_library_validation(tmp_path):
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.62.28.102')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=tmp_path)
    assert rep.failures, 'libraries must not be accepted without a manifest'


def test_soname_alias_is_accepted(manifest_root, tmp_path):
    """A real .so.62 next to the versioned file is a normal alias, not a finding.

    In the real tree this is a symlink and is skipped, but PyInstaller flattens
    symlinks into copies, so the gate has to tolerate the name.
    """
    payload = b'verified alias payload'
    digest = hashlib.sha256(payload).hexdigest()
    data = _manifest_data()
    data['shipped_files_sha256']['libavcodec.so.62.28.102'] = digest
    (manifest_root / 'tools' / 'ffmpeg_manifest_linux.json').write_text(
        json.dumps(data), encoding='utf-8')
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.62', payload)
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_unversioned_linker_alias_is_hash_verified(manifest_root, tmp_path):
    payload = b'verified linker alias payload'
    digest = hashlib.sha256(payload).hexdigest()
    data = _manifest_data()
    data['shipped_files_sha256']['libavcodec.so.62.28.102'] = digest
    (manifest_root / 'tools' / 'ffmpeg_manifest_linux.json').write_text(
        json.dumps(data), encoding='utf-8')
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so', payload)
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_tampered_linker_alias_fails(manifest_root, tmp_path):
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so', b'system or tampered bytes')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures


def test_failed_ffmpeg_runtime_probe_cannot_report_clean(monkeypatch, tmp_path):
    exe = tmp_path / ('ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
    exe.write_bytes(b'not executed by this unit test')
    monkeypatch.setattr(
        vrl.subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(
            returncode=127, stdout='', stderr='loader failure'),
    )
    rep = vrl.Report()
    vrl.check_ffmpeg_executable(exe, rep)
    assert rep.failures
    assert not any('-buildconf: clean' in line for line in rep.warnings)


# Engine linkage

def test_engine_check_skips_when_absent(tmp_path):
    rep = vrl.Report()
    vrl.check_linux_engine(tmp_path / 'FTHRclips', rep)
    assert not rep.failures, 'a missing engine is a warning, not a failure'


@pytest.mark.skipif(shutil.which('readelf') is None,
                    reason='readelf not available')
def test_engine_without_rpath_fails(tmp_path, monkeypatch):
    """An engine with no RPATH would load whatever the system offers."""
    engine = tmp_path / 'FTHRclips'
    engine.write_bytes(b'\x7fELF' + b'\0' * 60)
    monkeypatch.setattr(vrl, '_elf_needed', lambda p: ['libavcodec.so.62'])
    monkeypatch.setattr(vrl, '_elf_rpath', lambda p: [])
    rep = vrl.Report()
    vrl.check_linux_engine(engine, rep)
    assert rep.failures


def test_engine_linked_against_system_ffmpeg_fails(tmp_path, monkeypatch):
    engine = tmp_path / 'FTHRclips'
    engine.write_bytes(b'\x7fELF' + b'\0' * 60)
    monkeypatch.setattr(vrl, '_elf_needed', lambda p: ['libavcodec.so.60'])
    monkeypatch.setattr(vrl, '_elf_rpath', lambda p: ['$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_engine(engine, rep)
    assert rep.failures, 'linking the distro GPL FFmpeg must fail the gate'


def test_engine_with_absolute_rpath_fails(tmp_path, monkeypatch):
    """A build-host path in RPATH is both a leak and a hijack risk."""
    engine = tmp_path / 'FTHRclips'
    engine.write_bytes(b'\x7fELF' + b'\0' * 60)
    monkeypatch.setattr(vrl, '_elf_needed', lambda p: ['libavcodec.so.62'])
    monkeypatch.setattr(vrl, '_elf_rpath',
                        lambda p: ['$ORIGIN', '/home/builder/ffmpeg/lib'])
    rep = vrl.Report()
    vrl.check_linux_engine(engine, rep)
    assert rep.failures


def test_correctly_built_engine_passes(tmp_path, monkeypatch):
    engine = tmp_path / 'FTHRclips'
    engine.write_bytes(b'\x7fELF' + b'\0' * 60)
    monkeypatch.setattr(vrl, '_elf_needed',
                        lambda p: ['libavcodec.so.62', 'libavutil.so.60'])
    monkeypatch.setattr(vrl, '_elf_rpath', lambda p: ['$ORIGIN', '$ORIGIN/lib'])
    rep = vrl.Report()
    vrl.check_linux_engine(engine, rep)
    assert not rep.failures, rep.failures


# The forbidden-flag list must stay intact

@pytest.mark.parametrize('flag', [b'--enable-gpl', b'--enable-nonfree',
                                  b'--enable-libx264', b'--enable-libx265'])
def test_forbidden_flags_still_listed(flag):
    assert flag in vrl.FORBIDDEN_FLAGS


def test_version3_is_not_forbidden():
    """--enable-version3 yields LGPLv3, not GPL.

    It is present in both the Windows and the Linux runtime. Adding it to the
    forbidden list would make the gate permanently red and people would start
    ignoring it.
    """
    assert b'--enable-version3' not in vrl.FORBIDDEN_FLAGS


def test_gpl_flag_in_a_binary_is_detected(tmp_path):
    lib = tmp_path / 'libavcodec.so.62.28.102'
    lib.write_bytes(b'padding --enable-gpl --enable-libx264 padding')
    rep = vrl.Report()
    vrl.check_binary(lib, rep)
    assert rep.failures


# Names that look like FFmpeg but are not

@pytest.mark.parametrize('name', ['libavif-cbf1e83c.so.16.3.0',
                                  'libavif.so.16',
                                  'libavc1394.so.0'])
def test_non_ffmpeg_libav_names_are_ignored(manifest_root, tmp_path, name):
    """libavif is the AV1 *image* codec; libavc1394 is FireWire.

    An over-broad 'libav*' match treated these as FFmpeg. In the gate that
    produced false failures; in build_linux.sh the same mistake deleted libavif
    and the bundle stopped starting.
    """
    art = tmp_path / 'artifact'
    _write_lib(art, name)
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, f'{name} is not FFmpeg and must be ignored'


def test_wheel_provided_ffmpeg_is_accepted_when_lgpl(manifest_root, tmp_path):
    """OpenCV's auditwheel-renamed FFmpeg is documented and LGPL."""
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec-156beeea.so.62.11.100',
               b'libavcodec license: LGPL version 2.1 or later')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_wheel_provided_ffmpeg_fails_if_it_turns_gpl(manifest_root, tmp_path):
    """If a future wheel ships a GPL FFmpeg, the gate must still catch it."""
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec-156beeea.so.62.11.100',
               b'configuration: --enable-gpl --enable-libx264')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert rep.failures, 'a GPL wheel library must not be waved through'


def test_qt_multimedia_ffmpeg_is_accepted(manifest_root, tmp_path):
    art = tmp_path / 'artifact'
    _write_lib(art, 'libavcodec.so.61',
               b'libavcodec license: LGPL version 2.1 or later')
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_libs(art, rep, 'test', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


# Bundled ffmpeg / ffprobe CLI tools

PINNED_NEEDED = [
    'libavdevice.so.62', 'libavfilter.so.11', 'libavformat.so.62',
    'libavcodec.so.62', 'libswresample.so.6', 'libswscale.so.9',
    'libavutil.so.60', 'libm.so.6', 'libpthread.so.0', 'libc.so.6',
]


def _cli_appdir(tmp_path, names=('ffmpeg', 'ffprobe')):
    d = tmp_path / 'AppDir' / '_internal'
    for n in names:
        _write_lib(d, n, b'\x7fELF' + b'\0' * 60)
    return tmp_path / 'AppDir'


def _patch_cli(monkeypatch, needed, rpath):
    monkeypatch.setattr(vrl, '_elf_needed', lambda p: list(needed))
    monkeypatch.setattr(vrl, '_elf_rpath', lambda p: list(rpath))
    monkeypatch.setattr(vrl, 'check_ffmpeg_executable',
                        lambda exe, rep: rep.ok(f'{exe.name}: stub'))


def test_cli_tools_with_pinned_needed_and_origin_rpath_pass(
        manifest_root, tmp_path, monkeypatch):
    appdir = _cli_appdir(tmp_path)
    _patch_cli(monkeypatch, PINNED_NEEDED, ['$ORIGIN/../lib', '$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_cli_tool_with_system_rpath_fails(manifest_root, tmp_path, monkeypatch):
    appdir = _cli_appdir(tmp_path)
    _patch_cli(monkeypatch, PINNED_NEEDED, ['$ORIGIN', '/usr/lib'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert any('RPATH' in f for f in rep.failures), rep.failures


def test_cli_tool_without_rpath_fails(manifest_root, tmp_path, monkeypatch):
    appdir = _cli_appdir(tmp_path)
    _patch_cli(monkeypatch, PINNED_NEEDED, [])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert any('RPATH' in f for f in rep.failures), rep.failures


def test_cli_tool_linked_to_system_ffmpeg_fails(
        manifest_root, tmp_path, monkeypatch):
    appdir = _cli_appdir(tmp_path)
    needed = [n for n in PINNED_NEEDED if n != 'libavcodec.so.62']
    _patch_cli(monkeypatch, needed + ['libavcodec.so.60'], ['$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert any('libavcodec.so.60' in f for f in rep.failures), rep.failures


def test_cli_tool_with_unlisted_ffmpeg_needed_fails(
        manifest_root, tmp_path, monkeypatch):
    appdir = _cli_appdir(tmp_path)
    _patch_cli(monkeypatch, PINNED_NEEDED + ['libavcodec.so.99'], ['$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert rep.failures


def test_appdir_without_cli_tools_fails(manifest_root, tmp_path, monkeypatch):
    appdir = tmp_path / 'AppDir'
    appdir.mkdir()
    _patch_cli(monkeypatch, PINNED_NEEDED, ['$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(appdir, rep, 'appdir', manifest_root=manifest_root)
    assert any('does not ship ffmpeg' in f for f in rep.failures), rep.failures


def test_tree_without_cli_tools_only_warns(manifest_root, tmp_path, monkeypatch):
    tree = tmp_path / 'ffmpeg'
    tree.mkdir()
    _patch_cli(monkeypatch, PINNED_NEEDED, ['$ORIGIN'])
    rep = vrl.Report()
    vrl.check_linux_ffmpeg_cli(tree, rep, 'tree', manifest_root=manifest_root,
                               required=False)
    assert not rep.failures
    assert rep.warnings


def test_manifest_records_cli_tools():
    cli = _manifest_data()['cli_tools']
    assert cli['shipped'] == ['ffmpeg', 'ffprobe']
    assert cli['rpath'] == '$ORIGIN/../lib:$ORIGIN'


def _run_all_with_recorded_appdirs(monkeypatch, tree):
    seen = []
    for name in ('check_files', 'check_project_license', 'check_manifest',
                 'check_linux_manifest', 'check_python_sources',
                 'check_qt_source_selection', 'check_asset_manifest',
                 'scan_dir', 'check_qt_artifact', 'check_asset_artifact'):
        monkeypatch.setattr(vrl, name, lambda *a, **k: None)
    monkeypatch.setattr(
        vrl, 'check_files',
        lambda path, required, rep, kind: seen.append((Path(path), kind)))
    monkeypatch.setattr(sys, 'argv', ['verify_release_licenses.py',
                                      '--all', '--tree', str(tree)])
    vrl.main()
    return [p for p, kind in seen if kind == 'appdir']


def test_all_inspects_the_appdir_the_build_script_writes(monkeypatch, tmp_path):
    (tmp_path / 'build_output' / 'AppDir').mkdir(parents=True)
    appdirs = _run_all_with_recorded_appdirs(monkeypatch, tmp_path)
    assert appdirs == [(tmp_path / 'build_output' / 'AppDir').resolve()]


def test_all_ignores_a_stale_build_appdir(monkeypatch, tmp_path):
    (tmp_path / 'build' / 'AppDir').mkdir(parents=True)
    assert _run_all_with_recorded_appdirs(monkeypatch, tmp_path) == []
