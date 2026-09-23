from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import verify_windows_installer_lifecycle as lifecycle


ROOT = Path(__file__).resolve().parents[1]


def test_windows_installer_lifecycle_source_contract_passes():
    completed = subprocess.run(
        [sys.executable, 'tools/verify_windows_installer_lifecycle.py'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'stable existing AppId is retained' in completed.stdout
    assert 'installer never names the user clip root as a deletion target' in completed.stdout


@pytest.mark.parametrize('separator', [' ', '   ', '\t', ' \t '])
def test_string_define_accepts_horizontal_whitespace(separator):
    report = lifecycle.Report()
    lifecycle.require_string_define(
        report, f'  #define{separator}MyAppVersion{separator}"1.1.2-alpha" \t\r\n',
        'MyAppVersion', '1.1.2-alpha', 'version matches',
    )
    assert not report.failed


@pytest.mark.parametrize('source', [
    '',
    '#define MyAppVersion "1.1.3-alpha"',
    '#define MyAppVersion "1x1x2-alpha"',
    '; #define MyAppVersion "1.1.2-alpha"',
    '#define MyAppVersionExtra "1.1.2-alpha"',
    '#define MyAppVersion\n"1.1.2-alpha"',
    '#define MyAppVersion "1.1.2-alpha" + "extra"',
    '#define MyAppVersion "1.1.2-alpha"\n#define MyAppVersion "1.1.2-alpha"',
    '#define MyAppVersion "1.1.2-alpha"\n#define MyAppVersion "1.1.3-alpha"',
])
def test_string_define_rejects_missing_wrong_or_duplicate_values(source):
    report = lifecycle.Report()
    lifecycle.require_string_define(
        report, source, 'MyAppVersion', '1.1.2-alpha', 'version matches',
    )
    assert report.failed
