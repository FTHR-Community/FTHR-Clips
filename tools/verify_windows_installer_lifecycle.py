#!/usr/bin/env python3
"""Verify installer lifecycle rules and optional built release inputs.

Use --bundle, --vcredist, and --installer to check artifacts. These checks
supplement manual install, update, and uninstall qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / 'installer_windows.iss'
APP_ID = '{{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}'
APP_UNINSTALL_KEY = (
    r'Software\Microsoft\Windows\CurrentVersion\Uninstall' '\\'
    r'{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1'
)


def windows_powershell() -> str:
    """Use inbox Windows PowerShell for its native Authenticode provider."""
    candidate = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
        'System32/WindowsPowerShell/v1.0/powershell.exe')
    return str(candidate) if candidate.is_file() else 'powershell.exe'


def windows_powershell_module_path() -> str:
    system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    program_files = Path(os.environ.get('ProgramFiles', r'C:\Program Files'))
    return ';'.join((
        str(program_files / 'WindowsPowerShell' / 'Modules'),
        str(system_root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'Modules'),
    ))


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f'  [ OK ] {message}')

    def fail(self, message: str) -> None:
        self.failed = True
        print(f'  [FAIL] {message}')

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f'  [WARN] {message}')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require(report: Report, source: str, fragment: str, description: str) -> None:
    if fragment in source:
        report.ok(description)
    else:
        report.fail(f'{description}: missing {fragment!r}')


def require_string_define(report: Report, source: str, name: str, value: str,
                          description: str) -> None:
    """Check one exact string definition without depending on column alignment."""
    definitions = re.findall(
        rf'^[ \t]*#define[ \t]+{re.escape(name)}(?=[ \t\r\n]|$)([^\r\n]*)',
        source, re.MULTILINE,
    )
    if len(definitions) == 1 and definitions[0].strip(' \t') == f'"{value}"':
        report.ok(description)
    else:
        report.fail(f'{description}: expected exactly one #define {name} "{value}"')


def check_source(report: Report) -> None:
    if not INSTALLER.is_file():
        report.fail('installer_windows.iss is missing')
        return
    source = INSTALLER.read_text(encoding='utf-8')
    sys.path.insert(0, str(ROOT / 'FTHR_UI'))
    from version import (  # noqa: PLC0415
        APP_NAME, PUBLISHER, __version__, windows_file_version,
    )

    require_string_define(report, source, 'MyAppName', APP_NAME,
                          'installer name matches the product source of truth')
    require_string_define(report, source, 'MyAppVersion', __version__,
                          'installer version matches the product source of truth')
    require_string_define(report, source, 'MyAppPublisher', PUBLISHER,
                          'installer publisher matches the product source of truth')
    require_string_define(report, source, 'MyAppId', APP_ID,
                          'stable existing AppId is retained')
    require(report, source, 'AppId={#MyAppId}',
            'AppId is used as the install/update identity')
    require(report, source, 'UninstallDisplayIcon={app}\\{#MyAppExeName}',
            'Installed Apps entry uses the FTHR executable icon')
    require(report, source, 'UsePreviousAppDir=yes',
            'upgrades retain the previous app directory')
    require(report, source, 'UsePreviousGroup=yes',
            'upgrades retain the previous Start Menu group')
    require(report, source, 'DisableWelcomePage=yes',
            'the policy acknowledgement is the first interactive page')
    require(report, source, 'DisableFinishedPage=yes',
            'the stock Windows finish page is replaced by the branded close page')
    require(report, source, 'WizardForm.BorderStyle := bsNone;',
            'the installer uses the app-style frameless popup shell')
    require(report, source, 'SetupIconFile=FTHR_UI\\assets\\favicon.ico',
            'the installer uses the supplied application favicon')
    require(report, source, 'Source: "FTHR_UI\\assets\\icons\\close.png"',
            'the popup close control uses the existing application icon')
    require(report, source, 'policies.fthrclips.com',
            'installer links to the hosted Privacy Policy')
    require(report, source, "ConsentCheck.Checked := False",
            'policy acknowledgement defaults to unchecked')
    require(report, source, 'procedure CreateLocationPage;',
            'installer presents install and clip locations together')
    require(report, source, "InstallLabel.Caption := 'INSTALL FOLDER';",
            'installer exposes the application install folder')
    require(report, source, "ClipLabel.Caption := 'CLIP FOLDER';",
            'installer exposes the separate clip-library folder')
    require(report, source, 'function UserProfileDirectory: String;',
            'installer derives the per-user profile path with supported constants')
    require(report, source, "ExpandConstant('{userappdata}')",
            'installer uses a supported AppData constant')
    if "ExpandConstant('{userprofile}')" in source:
        report.fail('installer uses unsupported {userprofile} constant')
    else:
        report.ok('installer has no unsupported {userprofile} constant')
    require(report, source, 'clips_directory',
            'selected clip-library directory is handed to the app settings')
    require(report, source, 'CreateFinishPage',
            'installer has a branded completion page')
    if re.search(r'(?m)^Wizard(?:Image|SmallImage)File=', source):
        report.fail('installer still embeds unrelated wizard banner artwork')
    else:
        report.ok('installer embeds no unrelated wizard banner artwork')
    require(report, source, 'Source: "FTHR_UI\\assets\\fonts\\Oswald-Bold.ttf"',
            'installer carries the application display font')
    require(report, source, 'OutputBaseFilename=FTHRClips-Setup-{#MyAppVersion}-x64',
            'installer artifact filename carries the product version')
    numeric = '.'.join(str(part) for part in windows_file_version())
    require(report, source, f'VersionInfoVersion={numeric}',
            'installer numeric file version matches the source version tuple')
    require(report, source, 'CloseApplications=yes',
            'Restart Manager is enabled for safe in-use application closure')
    require(report, source, 'CloseApplicationsFilter=FTHRClips.exe',
            'in-use closure is limited to FTHR executables')
    require(report, source, 'RestartApplications=no',
            'installer never silently relaunches a capture session')
    require(report, source, 'function PrepareToInstall(var NeedsRestart: Boolean): String;',
            'downgrade decision is explicit')
    require(report, source, "'/ALLOWDOWNGRADE'", 'explicit downgrade override is documented in code')
    require(report, source, f"ProductUninstallKey = '{APP_UNINSTALL_KEY}';",
            'existing product uninstall identity is queried')
    require(report, source, 'function NeedsVCRedist: Boolean;',
            'VC++ prerequisite has an installed-version guard')
    require(report, source, "'SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64'",
            'VC++ prerequisite checks the official x64 runtime location')
    require(report, source, 'Check: NeedsVCRedist',
            'VC++ prerequisite runs only when the guard requires it')
    require(report, source,
            'Type: filesandordirs; Name: "{app}\\_internal"',
            'upgrade replaces the complete installer-owned runtime tree')
    require(report, source, 'function HasOwnedAutostart: Boolean;',
            'upgrade detects product-owned autostart state')
    require(report, source, 'RegWriteStringValue(HKCU, RunKey, RunValueName,',
            'upgrade rewrites preserved autostart with the installed path')
    require(report, source, 'RegDeleteValue(HKCU, RunKey, RunValueName);',
            'uninstall removes the exact product autostart value')
    require(report, source, 'function LegacyInstallPresent: Boolean;',
            'legacy per-user installation detection is explicit')
    require(report, source, 'Remove the obsolete per-user FTHR test installation',
            'legacy removal is an explicit opt-in choice')
    require(report, source, "'/REMOVELEGACY'",
            'headless legacy removal requires an explicit maintenance switch')
    require(report, source, 'if DelTree(LegacyDir, True, True, True) then begin',
            'legacy removal is limited to the verified legacy install directory')
    require(report, source,
            'Name: "{code:UserProfilePath}\\.fthr"; Check: ShouldRemoveSettingsAndCache',
            'settings/cache removal is opt-in')
    require(report, source,
            'Name: "{localappdata}\\FTHR Clips\\plugins"',
            'uninstall removes separately activated optional packages')

    delete_sections = '\n'.join(re.findall(
        r'(?ms)^\[(?:InstallDelete|UninstallDelete)\]\s*(.*?)(?=^\[|\Z)', source))
    if re.search(r'(?mi)^\s*Type:\s*filesandordirs;\s*Name:\s*"\{app\}"\s*$',
                 delete_sections):
        report.fail('installer broadly deletes the application root during upgrade')
    else:
        report.ok('upgrade cleanup is bounded below the application root')
    if re.search(r'(?mi)^\s*Type:.*\{(?:userprofile|userappdata|localappdata)\}\\FTHR_Clips',
                 delete_sections):
        report.fail('installer names the user clip root as a deletion target')
    else:
        report.ok('installer never names the user clip root as a deletion target')
    if 'Name: "{userappdata}\\fthr"' in source:
        report.fail('obsolete and incorrect %APPDATA%\\fthr deletion remains')
    else:
        report.ok('obsolete %APPDATA%\\fthr deletion is absent')
    for unsafe in ('taskkill', 'Stop-Process', 'Remove-Item', 'rmdir /s /q'):
        if unsafe.lower() in source.lower():
            report.fail(f'unsafe implicit process/file removal command remains: {unsafe}')
        else:
            report.ok(f'no implicit {unsafe} command in installer')


def authenticode(path: Path) -> dict[str, str] | None:
    """Read Authenticode and PE version metadata without parsing PE ourselves."""
    if os.name != 'nt':
        return None
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        "$target = $env:FTHR_ARTIFACT_VERIFY_PATH; "
        "$p = Get-AuthenticodeSignature -LiteralPath $target; "
        "$i = Get-Item -LiteralPath $target; "
        "[PSCustomObject]@{status=[string]$p.Status; "
        "subject=if ($p.SignerCertificate) {[string]$p.SignerCertificate.Subject} else {''}; "
        "file_version=[string]$i.VersionInfo.FileVersion; "
        "product_version=[string]$i.VersionInfo.ProductVersion} | ConvertTo-Json -Compress"
    )
    env = os.environ.copy()
    env['PSModulePath'] = windows_powershell_module_path()
    env['FTHR_ARTIFACT_VERIFY_PATH'] = str(path.resolve())
    completed = subprocess.run([windows_powershell(), '-NoProfile', '-Command', command],
                               capture_output=True, text=True, timeout=30, env=env)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or 'PowerShell metadata query failed')
    return json.loads(completed.stdout)


def check_authenticode(report: Report, path: Path, *, microsoft: bool,
                       require_signed: bool) -> None:
    if not path.is_file():
        report.fail(f'artifact missing: {path}')
        return
    report.ok(f'{path.name}: SHA-256 {sha256(path)}')
    try:
        details = authenticode(path)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        report.fail(f'{path.name}: could not inspect Authenticode metadata ({exc})')
        return
    if details is None:
        report.warn(f'{path.name}: Authenticode was not inspected off Windows')
        return
    status = details.get('status', '')
    subject = details.get('subject', '')
    if microsoft:
        if status == 'Valid' and 'Microsoft Corporation' in subject:
            report.ok(f'{path.name}: valid Microsoft Authenticode signature ({subject})')
        else:
            report.fail(f'{path.name}: expected valid Microsoft signature; status={status!r}, subject={subject!r}')
    elif status == 'Valid':
        report.ok(f'{path.name}: valid Authenticode signature ({subject})')
    elif require_signed:
        report.fail(f'{path.name}: release signing required; status={status!r}')
    else:
        report.warn(f'{path.name}: unsigned/untrusted build artifact (status={status!r}); do not publish')
    if details.get('file_version') or details.get('product_version'):
        report.ok(
            f'{path.name}: FileVersion={details.get("file_version")!r}, '
            f'ProductVersion={details.get("product_version")!r}')


def check_bundle(report: Report, bundle: Path, *, require_signed: bool) -> None:
    if not bundle.is_dir():
        report.fail(f'PyInstaller bundle missing: {bundle}')
        return
    required = (
        bundle / 'FTHRClips.exe',
        bundle / '_internal' / 'engine' / 'FTHRClips.exe',
        bundle / '_internal' / 'engine' / 'FTHRPlaybackMixer.dll',
    )
    for path in required:
        if path.exists():
            report.ok(f'bundle contains {path.relative_to(bundle)}')
        else:
            report.fail(f'bundle missing {path.relative_to(bundle)}')
    # PyInstaller puts data files under _internal in an onedir build. The
    # Inno installer separately places the end-user notices at {app}; accept
    # either onedir layout while still requiring the package inputs to exist.
    for rel in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses'):
        locations = (bundle / rel, bundle / '_internal' / rel)
        found = next((path for path in locations if path.exists()), None)
        if found is None:
            report.fail(f'bundle missing {rel} at root or _internal')
        else:
            report.ok(f'bundle contains {found.relative_to(bundle)}')

    sys.path.insert(0, str(ROOT / 'FTHR_UI'))
    from core.uploader_bundle_manifest import (  # noqa: PLC0415
        EXPECTED_HARDWARE_BUNDLE_SHA256,
        EXPECTED_UPLOADER_BUNDLE_SHA256,
        HARDWARE_PLUGIN_ID,
        UPLOADER_PLUGIN_ID,
    )
    optional_packages = (
        ('FTHR-Uploader.fthrplugin', EXPECTED_UPLOADER_BUNDLE_SHA256,
         UPLOADER_PLUGIN_ID, 'FTHR Uploader.exe'),
        ('FTHR-Hardware-Identity.fthrplugin', EXPECTED_HARDWARE_BUNDLE_SHA256,
         HARDWARE_PLUGIN_ID, 'FTHR Hardware Identity.exe'),
    )
    package_root = bundle / '_internal' / 'plugin-packages'
    for name, expected_hash, plugin_id, entrypoint in optional_packages:
        package = package_root / name
        if not package.is_file():
            report.fail(f'bundle missing dormant optional package {name}')
            continue
        if sha256(package).lower() != expected_hash.lower():
            report.fail(f'{name}: hash does not match the Core release binding')
            continue
        try:
            with zipfile.ZipFile(package) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                names = set(archive.namelist())
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            report.fail(f'{name}: invalid dormant package ({exc})')
            continue
        expected_names = {
            'manifest.json', 'TERMS_OF_SERVICE.txt', 'PRIVACY_POLICY.txt',
            f'payload/{entrypoint}',
        }
        if (manifest.get('plugin_id') != plugin_id
                or manifest.get('entrypoint') != entrypoint
                or names != expected_names):
            report.fail(f'{name}: manifest or exact-content boundary is invalid')
        else:
            report.ok(f'bundle contains verified dormant package {name}')

    direct_optional_executables = [
        path for path in bundle.rglob('*')
        if path.is_file()
        and path.name in {'FTHR Uploader.exe', 'FTHR Hardware Identity.exe'}
    ]
    if direct_optional_executables:
        report.fail('optional executables were extracted into the Core bundle')
    else:
        report.ok('optional executables remain sealed in dormant packages')
    debug_files = [path for path in bundle.rglob('*')
                   if path.is_file() and path.suffix.lower() in {'.pdb', '.ilk', '.iobj', '.ipdb'}]
    if debug_files:
        report.fail('bundle contains debug artifacts: ' + ', '.join(
            str(path.relative_to(bundle)) for path in debug_files[:5]))
    else:
        report.ok('bundle contains no PDB/ILK/IPDB debug artifacts')
    foreign_icu = [
        path for path in bundle.rglob('*.dll')
        if path.name.casefold() == 'icuuc.dll'
        or path.name.casefold().startswith(('icudt', 'icuin'))
    ]
    if foreign_icu:
        report.fail(
            'bundle contains foreign ICU DLLs which can break QtWidgets: '
            + ', '.join(str(path.relative_to(bundle)) for path in foreign_icu[:5]))
    else:
        report.ok('bundle contains no foreign ICU DLLs')
    exe = bundle / 'FTHRClips.exe'
    if exe.is_file():
        check_authenticode(report, exe, microsoft=False, require_signed=require_signed)
    engine = bundle / '_internal' / 'engine' / 'FTHRClips.exe'
    if engine.is_file():
        check_authenticode(report, engine, microsoft=False, require_signed=require_signed)
    playback_mixer = bundle / '_internal' / 'engine' / 'FTHRPlaybackMixer.dll'
    if playback_mixer.is_file():
        check_authenticode(report, playback_mixer, microsoft=False,
                           require_signed=require_signed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, help='PyInstaller onedir bundle')
    parser.add_argument('--vcredist', type=Path, help='bundled Microsoft VC++ redistributable')
    parser.add_argument('--installer', type=Path, help='compiled Inno Setup executable')
    parser.add_argument('--require-signed', action='store_true',
                        help='fail if FTHR executable/installer lacks a valid Authenticode signature')
    args = parser.parse_args()

    report = Report()
    print('Windows installer lifecycle source contract:')
    check_source(report)
    if args.bundle:
        print('\nPyInstaller bundle:')
        check_bundle(report, args.bundle, require_signed=args.require_signed)
    if args.vcredist:
        print('\nVC++ redistributable:')
        check_authenticode(report, args.vcredist, microsoft=True, require_signed=True)
    if args.installer:
        print('\nCompiled installer:')
        check_authenticode(report, args.installer, microsoft=False,
                           require_signed=args.require_signed)

    print()
    if report.failed:
        print('RESULT: Windows installer lifecycle gate FAILED.')
        return 1
    if report.warnings:
        print('RESULT: Windows installer lifecycle contract passed with release-blocking warnings.')
    else:
        print('RESULT: Windows installer lifecycle contract passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
