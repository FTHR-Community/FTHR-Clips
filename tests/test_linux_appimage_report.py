from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tools/linux_appimage_report.sh'

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32',
    reason='AppImage report shell script requires POSIX environment',
)


def run_report(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['bash', str(REPORT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, 'HOME': '/home/test-user'},
        check=False,
    )


def test_report_rejects_missing_appimage():
    result = run_report('/tmp/does-not-exist.appimage')
    assert result.returncode == 2
    assert 'not found' in result.stderr.lower()


def test_report_identifies_architecture_and_runtime_prerequisites(tmp_path):
    image = tmp_path / 'FTHRClips-test.AppImage'
    image.write_bytes(b'not-a-real-appimage')

    result = run_report(str(image))

    assert result.returncode == 1
    assert 'sha256:' in result.stdout.lower()
    assert 'architecture:' in result.stdout.lower()
    assert 'fuse' in result.stdout.lower()
    assert 'appimage_extract_and_run=1' in result.stdout.lower()
    assert 'not a valid' in result.stdout.lower()


def test_report_can_write_a_machine_readable_checksum_file(tmp_path):
    image = tmp_path / 'FTHRClips-test.AppImage'
    image.write_bytes(b'not-a-real-appimage')
    checksum = tmp_path / 'SHA256SUMS'

    result = run_report(str(image), '--write-checksum', str(checksum))

    assert result.returncode == 1
    assert checksum.is_file()
    assert image.name in checksum.read_text()
