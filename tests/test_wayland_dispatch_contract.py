"""AUDIT-024 bounded Wayland dispatch and recovery contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "FTHRcapture_linux"
SRC = LINUX / "src"


def _read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def test_wayland_backends_have_no_blocking_dispatch_or_roundtrip_calls() -> None:
    for name in ("backend_wlr.cpp", "backend_ext.cpp"):
        source = _read(name)
        assert "wl_display_dispatch(" not in source
        assert "wl_display_roundtrip(" not in source


def test_both_wayland_backends_use_shared_deadlines_and_dispatch_helper() -> None:
    for stem in ("backend_wlr", "backend_ext"):
        header = _read(f"{stem}.h")
        source = _read(f"{stem}.cpp")
        assert "kInitializationTimeout = std::chrono::seconds(5)" in header
        assert "kFrameTimeout = std::chrono::seconds(2)" in header
        assert "DispatchWaylandUntil(" in source
        assert "BoundedWaylandRoundtrip(" in source
        assert "WaylandWaitResultName(result)" in source


def test_wait_helper_follows_wayland_prepared_read_protocol() -> None:
    source = _read("wayland_dispatch.cpp")
    wait = source[source.index("WaylandWaitResult WaitForWaylandEvent("):]
    operations = [
        "dispatch_pending(display)",
        "prepare_read(display)",
        "flush(display)",
        "poll_fds(&display_fd",
        "read_events(display)",
    ]
    positions = [wait.index(operation) for operation in operations]
    assert positions == sorted(positions)
    assert "operations_.cancel_read(display_)" in source
    assert "POLLHUP | POLLERR" in source
    assert "WaylandWaitResult::Disconnected" in source


def test_engine_running_flag_cancels_waits_and_failure_enters_recovery() -> None:
    factory = _read("capture_backend.cpp")
    engine = _read("capture_engine.cpp")
    assert "std::make_unique<WlrBackend>(running)" in factory
    assert "std::make_unique<ExtBackend>(running)" in factory
    assert factory.count("if (cancelled()) return nullptr;") >= 4
    assert "CreateBestBackend(cfg_, &running_, &failure)" in engine

    capture_failure = engine.index("if (!backend_->CaptureFrame(raw))")
    generation_cleanup = engine.index("backend_->Shutdown()", capture_failure)
    recovery = engine.index("recovery.OnGenerationFailed", 0)
    assert recovery < capture_failure < generation_cleanup
    assert "CAPTURE_HEALTH_RECOVERING" in engine
    assert "CAPTURE_HEALTH_BACKEND_FAILED" in engine
    assert "running_.store(false)" in engine


def test_failed_generation_cleans_stale_backend_before_retry() -> None:
    engine = _read("capture_engine.cpp")
    generation = engine[engine.index("CaptureEngine::GenerationEnd CaptureEngine::RunCaptureGeneration()") :]
    capture_failure = generation.index("if (!backend_->CaptureFrame(raw))")
    shutdown = generation.index("backend_->Shutdown()", capture_failure)
    reset = generation.index("backend_.reset()", shutdown)
    close = generation.index("encoder_.Close()", reset)
    assert shutdown < reset < close


def test_only_completed_capture_frames_advance_progress() -> None:
    engine = _read("capture_engine.cpp")
    capture = engine.index("if (!backend_->CaptureFrame(raw))")
    progress = engine.index("frame_count_.fetch_add(1)", capture)
    assert capture < progress


def test_wlr_destroys_timed_out_requests_before_recovery() -> None:
    source = _read("backend_wlr.cpp")
    assert "void WlrBackend::DestroyPendingFrame()" in source
    assert source.count("DestroyPendingFrame();") >= 6


def test_ext_destroys_frame_after_bounded_wait() -> None:
    source = _read("backend_ext.cpp")
    wait = source.index("const bool completed = WaitUntil(")
    destroy = source.index("ext_image_copy_capture_frame_v1_destroy(frame)", wait)
    failure = source.index("if (!completed || frame_failed_ || session_stopped_)", destroy)
    assert wait < destroy < failure
