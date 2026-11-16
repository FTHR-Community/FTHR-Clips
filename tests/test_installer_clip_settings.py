"""Run the installer's actual Pascal settings helpers without installing the app."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_installer_paths_round_trip_through_app_settings(tmp_path, monkeypatch):
    if os.name != 'nt':
        pytest.skip('Inno Setup runtime requires Windows')
    from tools.build_windows_installer import find_iscc
    from core.settings_manager import SettingsManager

    try:
        compiler = find_iscc(None)
    except FileNotFoundError:
        pytest.skip('Inno Setup compiler is not installed')

    source = (ROOT / 'installer_windows.iss').read_text(encoding='utf-8')
    helpers = source[source.index('function JsonEscape('):
                     source.index('function UserProfileDirectory')]
    helpers += source[source.index('function SaveSettingsJson('):
                      source.index('function ExistingClipDirectory')]

    def pascal(value):
        return "'" + value.replace("'", "''") + "'"

    cases = [r'D:\My Clips', 'D:\\trailing\\', r'\\server\share\Clips',
             'D:\\Clips Üñicode', 'D:\\test "quote"']
    statements = []
    for index, path in enumerate(cases):
        old = json.dumps({'clips_directory': 'C:\\Old\\',
                          'recording_directory': 'C:\\Old\\Recordings',
                          'keep': {'volume': 37}})
        output = tmp_path / f'{index}.json'
        statements.extend([
            f'  Json := UpsertJsonString({pascal(old)}, \'clips_directory\', {pascal(path)});',
            f'  if JsonStringValue(Json, \'clips_directory\') <> {pascal(path)} then RaiseException(\'Path mismatch\');',
            f'  if not SaveSettingsJson({pascal(str(output))}, Json) then RaiseException(\'Save failed\');',
        ])
    script = tmp_path / 'probe.iss'
    script.write_text(
        '[Setup]\nAppName=Settings probe\nAppVersion=1\n'
        'DefaultDirName={tmp}\\FTHRSettingsProbe\nPrivilegesRequired=lowest\n'
        'Uninstallable=no\nCreateAppDir=no\nOutputBaseFilename=probe\n'
        f'OutputDir={tmp_path}\n[Code]\n' + helpers +
        '\nfunction InitializeSetup: Boolean;\nvar Json: String;\nbegin\n' +
        '\n'.join(statements) + '\n  Result := False;\nend;\n',
        encoding='utf-8-sig')
    compiled = subprocess.run([str(compiler), str(script)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    subprocess.run([str(tmp_path / 'probe.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES'],
                   timeout=30, check=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    settings_path = tmp_path / '.fthr' / 'settings.json'
    settings_path.parent.mkdir()
    for index, path in enumerate(cases):
        data = (tmp_path / f'{index}.json').read_bytes()
        assert json.loads(data)['keep'] == {'volume': 37}
        settings_path.write_bytes(data)
        manager = SettingsManager()
        assert manager.settings['clips_directory'] == path
        assert manager.settings['recording_directory'] == 'C:\\Old\\Recordings'
