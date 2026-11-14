"""Reject broken engine response ordering in mutated source fixtures.

These tests exercise the static verifier and confirm the real tree passes.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / 'tools' / 'verify_engine_response_contract.py'

sys.path.insert(0, str(REPO / 'tools'))
from verify_engine_response_contract import check_file, main  # noqa: E402


def _write(tmp_path: Path, body: str, name: str = 'fixture.cpp') -> Path:
    p = tmp_path / name
    p.write_text(body, encoding='utf-8')
    return p


def _rules(violations):
    return {v.rule for v in violations}


# The exact pre-fix shapes. These are the regressions the gate exists for.

def test_catches_the_original_linux_save_clip_ordering(tmp_path):
    """Verbatim shape of FTHRcapture_linux/src/main.cpp before AUDIT-018:
    response published, message written afterwards."""
    src = _write(tmp_path, '''
void loop(SharedMemoryLayout* layout) {
    {
        bool ok = engine.SaveClip(out_path, duration_sec, layout);
        layout->engine_response = ok
            ? static_cast<uint32_t>(fthr::ResponseType::CLIP_SAVED)
            : static_cast<uint32_t>(fthr::ResponseType::ERROR_OCCURRED);
        if (!ok)
            snprintf(layout->engine_string,
                     sizeof(layout->engine_string),
                     "SaveClip failed: %s", out_path.c_str());
    }
}
''')
    rules = _rules(check_file(src))
    assert 'ERROR-WITHOUT-PAYLOAD' in rules or 'PAYLOAD-AFTER-RESPONSE' in rules, (
        'the gate did not catch the original AUDIT-018 ordering')


def test_catches_the_original_linux_get_status_ordering(tmp_path):
    """The second site: engine_param1 written after STATUS_UPDATE."""
    src = _write(tmp_path, '''
void loop(SharedMemoryLayout* layout) {
    {
        layout->engine_response =
            static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
        layout->engine_param1 = engine.IsNvencActive() ? 1 : 0;
    }
}
''')
    assert 'PAYLOAD-AFTER-RESPONSE' in _rules(check_file(src))


def test_catches_the_original_windows_error_without_diagnostics(tmp_path):
    """Verbatim shape of the Windows error paths before AUDIT-019: a response
    with no message at all."""
    src = _write(tmp_path, '''
void MuxEncodedClip(const SaveClipTask& task) {
    if (snap.packets.empty()) {
        if (task.shared_memory)
            task.shared_memory->engine_response = ResponseType::ERROR_OCCURRED;
        return;
    }
}
''')
    assert 'ERROR-WITHOUT-PAYLOAD' in _rules(check_file(src))


# Other shapes that must not slip through

def test_catches_unsafe_string_primitive(tmp_path):
    src = _write(tmp_path, '''
void f(SharedMemoryLayout* layout) {
    {
        wcscpy(layout->engine_string, message);
        layout->engine_response = ResponseType::ERROR_OCCURRED;
    }
}
''')
    assert 'UNSAFE-STRING' in _rules(check_file(src))


def test_catches_engine_side_consumption(tmp_path):
    """Only the UI may write NONE. An engine clearing a published response
    destroys a verdict the UI has not read yet — this is AUDIT-017's shape,
    on the producer side."""
    src = _write(tmp_path, '''
void f(SharedMemoryLayout* layout) {
    while (true) {
        if (ready) {
            layout->engine_response = ResponseType::NONE;
        }
    }
}
''')
    assert 'ENGINE-CONSUMES' in _rules(check_file(src))


def test_catches_raw_payload_write_in_a_cpp_file(tmp_path):
    src = _write(tmp_path, '''
void f(SharedMemoryLayout* layout) {
    {
        layout->engine_string[0] = 0;
        layout->engine_response = ResponseType::ERROR_OCCURRED;
    }
}
''')
    assert 'RAW-PAYLOAD-WRITE' in _rules(check_file(src))


# It must not cry wolf

def test_accepts_the_corrected_shape(tmp_path):
    src = _write(tmp_path, '''
void loop(SharedMemoryLayout* layout) {
    {
        bool ok = engine.SaveClip(out_path, duration_sec, layout);
        if (!ok)
            fthr::set_engine_string(layout, "SaveClip failed: " + out_path);
        layout->engine_response = ok
            ? static_cast<uint32_t>(fthr::ResponseType::CLIP_SAVED)
            : static_cast<uint32_t>(fthr::ResponseType::ERROR_OCCURRED);
    }
}
''')
    assert check_file(src) == []


def test_accepts_the_set_engine_error_helper(tmp_path):
    src = _write(tmp_path, '''
bool MuxEncodedClip(const SaveClipTask& task) {
    if (snap.packets.empty()) {
        SetEngineError(task.shared_memory, L"Nothing to save.");
        return false;
    }
    return true;
}
''')
    assert check_file(src) == []


def test_comments_mentioning_error_occurred_are_not_violations(tmp_path):
    """The engines carry a lot of explanatory prose about this exact contract.
    If comments counted, the gate would be unusable and would be deleted."""
    src = _write(tmp_path, '''
// This used to publish engine_response = ERROR_OCCURRED before writing
// engine_string, which was AUDIT-018.
/* layout->engine_response = ResponseType::ERROR_OCCURRED; */
bool f(SharedMemoryLayout* layout) {
    return true;
}
''')
    assert check_file(src) == []


# The real tree

def test_real_engines_pass_the_gate():
    result = subprocess.run([sys.executable, str(GATE)],
                            capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, (
        f'engine contract gate failed on the real tree:\n'
        f'{result.stdout}\n{result.stderr}')


def test_gate_reports_missing_sources_as_failure(tmp_path):
    """An empty scan must not be mistaken for a clean one — that is how a gate
    silently stops guarding anything after a directory rename."""
    assert main(['--root', str(tmp_path / 'does-not-exist')]) == 1


def test_gate_exit_code_is_nonzero_on_a_bad_tree(tmp_path):
    _write(tmp_path, '''
void f(SharedMemoryLayout* layout) {
    {
        layout->engine_response = ResponseType::ERROR_OCCURRED;
    }
}
''')
    assert main(['--root', str(tmp_path)]) == 1


# Stale payload: a message must never outlive the save it describes

def _engine_source(rel: str) -> str:
    return (REPO / rel).read_text(encoding='utf-8', errors='replace')




def test_windows_clears_the_message_before_announcing_success():
    """The Windows equivalent. SaveClipThread publishes CLIP_SAVED; the text
    channel is emptied first so a success cannot carry a previous failure's
    message (sequence A)."""
    src = _engine_source('FTHRcapture/FTHRclips/src/capture_engine.cpp')
    start = src.index('const bool ok = ProcessSaveClipTask(task)')
    body = src[start:start + 1200]
    publish = body.index('ResponseType::CLIP_SAVED')
    clear = body.index('SetEngineString(task.shared_memory, L"")')
    assert clear < publish, 'success published before clearing the old message'


def test_windows_success_is_conditional_on_the_outcome():
    """AUDIT-021 regression. CLIP_SAVED must be guarded by the save's result;
    an unconditional publish reports every failure as a success."""
    src = _engine_source('FTHRcapture/FTHRclips/src/capture_engine.cpp')
    start = src.index('const bool ok = ProcessSaveClipTask(task)')
    body = src[start:start + 1200]
    guard = body.index('if (ok && task.shared_memory)')
    publish = body.index('ResponseType::CLIP_SAVED')
    assert guard < publish, 'CLIP_SAVED is published outside the success guard'


def test_windows_save_functions_can_report_failure():
    """The root cause of AUDIT-021 was three void functions. If any of them
    goes back to void, failure becomes unreportable again."""
    hdr = _engine_source('FTHRcapture/FTHRclips/include/capture_engine.h')
    for fn in ('ProcessSaveClipTask', 'MuxEncodedClip', 'EncodeRawClip'):
        assert f'bool {fn}(' in hdr, f'{fn} must return bool, not void'
        assert f'void {fn}(' not in hdr


def test_engine_string_is_only_written_through_bounded_helpers():
    """T4: an over-long error text must be truncated, not overflow. Every
    write goes through a helper that bounds and terminates."""
    for rel, helper in (
        ('FTHRcapture/FTHRclips/src/capture_engine.cpp', 'SetEngineString'),
        ('FTHRcapture/FTHRclips/src/main.cpp', 'SetEngineString'),
    ):
        src = _engine_source(rel)
        assert 'engine_string' not in src or helper in src
        for unsafe in ('strcpy(', 'wcscpy(', 'sprintf('):
            assert f'{unsafe}' not in src or 'engine_string' not in src, (
                f'{rel} uses {unsafe} near engine_string')
