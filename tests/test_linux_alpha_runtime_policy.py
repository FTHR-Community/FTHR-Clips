from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AUDIO_CPP = (ROOT / 'FTHRcapture_linux/src/audio_capture.cpp').read_text()
AUDIO_H = (ROOT / 'FTHRcapture_linux/src/audio_capture.h').read_text()
BACKEND_CPP = (ROOT / 'FTHRcapture_linux/src/capture_backend.cpp').read_text()
X11_CPP = (ROOT / 'FTHRcapture_linux/src/backend_x11.cpp').read_text()
CMAKE = (ROOT / 'FTHRcapture_linux/CMakeLists.txt').read_text()
MAIN_CPP = (ROOT / 'FTHRcapture_linux/src/main.cpp').read_text()
UI_MAIN = (ROOT / 'FTHR_UI/main.py').read_text(encoding='utf-8')
SYSTEM_REPORT = (ROOT / 'tools/linux_system_report.sh').read_text()


def test_auto_desktop_audio_resolves_default_sink_monitor():
    assert 'pa_context_get_server_info' in AUDIO_CPP
    assert 'pa_context_get_sink_info_by_name' in AUDIO_CPP
    assert 'monitor_source_name' in AUDIO_CPP
    assert 'source device (nullptr = default)' not in AUDIO_CPP
    assert 'source = nullptr' not in AUDIO_CPP


def test_audio_open_failure_is_synchronous_and_video_only_diagnostic_exists():
    start = AUDIO_CPP[AUDIO_CPP.index('bool AudioCapture::Start'):]
    assert start.index('pa_simple_new') < start.index('std::thread')
    engine = (ROOT / 'FTHRcapture_linux/src/capture_engine.cpp').read_text()
    assert 'FTHR_STARTUP_WARNING: DESKTOP_AUDIO_UNAVAILABLE' in engine


def test_audio_history_matches_maximum_replay_duration():
    assert 'kMaxSeconds  = 1800' in AUDIO_H


def test_multiband_request_is_ignored_at_native_boundary():
    assert 'cfg.multiband_enabled = false;' in MAIN_CPP
    assert 'arg_u32(argv, 13, 0) == 1' not in MAIN_CPP


def test_native_x11_is_enabled_but_never_used_as_xwayland_fallback():
    assert 'FTHR_X11_CAPTURE' in CMAKE
    option = CMAKE[CMAKE.index('option(FTHR_X11_CAPTURE'):]
    assert ' ON)' in option[:160]
    assert '#if FTHR_X11_CAPTURE' in BACKEND_CPP
    wayland_failure = BACKEND_CPP.index(
        'refusing XWayland/x11grab fallback')
    assert BACKEND_CPP.index('return nullptr;', wayland_failure) < \
        BACKEND_CPP.index('auto x11', wayland_failure)


def test_x11_shutdown_has_callback_and_process_escalation_boundaries():
    assert 'interrupt_callback.callback' in X11_CPP
    assert 'case fthr::CommandType::SHUTDOWN' in MAIN_CPP
    assert 'process.wait(timeout=2.0)' in UI_MAIN
    assert 'process.terminate()' in UI_MAIN
    assert 'process.kill()' in UI_MAIN
    assert 'process.wait(timeout=0.5)' in UI_MAIN


def test_linux_report_describes_native_x11_without_xwayland_claim():
    assert 'then x11grab' not in SYSTEM_REPORT
    assert 'native X11 x11grab' in SYSTEM_REPORT
    assert 'XWayland fallback is refused' in SYSTEM_REPORT
