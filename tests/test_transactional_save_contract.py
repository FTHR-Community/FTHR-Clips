"""AUDIT-028 cross-platform engine transaction contract."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / 'FTHRcapture_common'
WINDOWS_ENGINE = ROOT / 'FTHRcapture' / 'FTHRclips' / 'src' / 'capture_engine.cpp'



def test_windows_engine_runs_the_shared_transaction_contract() -> None:
    windows = WINDOWS_ENGINE.read_text(encoding='utf-8')

    assert 'transactional_save::Run(' in windows
    assert '"mp4"' in windows


def test_windows_success_is_after_transaction_completion() -> None:
    source = WINDOWS_ENGINE.read_text(encoding='utf-8')
    transaction = source.index('const auto result = transactional_save::Run(')
    assert source.index('if (result.success) return true;', transaction) > transaction

    worker = source.index('const bool ok = ProcessSaveClipTask(task)')
    success_guard = source.index('if (ok && task.shared_memory)', worker)
    response = source.index('ResponseType::CLIP_SAVED', success_guard)

    assert worker < success_guard < response




def test_native_fault_injection_contract(tmp_path: Path) -> None:
    compiler = shutil.which('g++') or shutil.which('c++')
    if compiler is None:
        pytest.skip('native C++ compiler not installed')
    source = COMMON / 'tests' / 'transactional_save_test.cpp'
    output = tmp_path / 'transactional_save_test'
    subprocess.run(
        [
            compiler,
            '-std=c++20',
            '-Wall',
            '-Wextra',
            '-Wpedantic',
            f'-I{COMMON}',
            str(source),
            '-o',
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run([str(output)], check=True, timeout=10)
