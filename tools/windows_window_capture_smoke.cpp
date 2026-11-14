#include "capture_engine.h"
#include "replay_encoder.h"

#include <winrt/base.h>

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>
#include <memory>
#include <new>
#include <vector>

#pragma comment(lib, "gdi32.lib")

namespace {

using Clock = std::chrono::steady_clock;

constexpr wchar_t kWindowClassName[] = L"FTHRClipsWindowCaptureSmokeClass";
constexpr wchar_t kWindowTitle[] = L"FTHRClips Window Capture Smoke";
constexpr int kInitialWidth = 640;
constexpr int kInitialHeight = 360;
// This deliberate growth crosses the production WGC encoder-geometry boundary.
// The lifecycle qualification observes RECONFIGURE_REQUIRED, rejects a save
// from that generation, and exercises the caller-owned engine restart path.
constexpr int kResizedWidth = 800;
constexpr int kResizedHeight = 450;
constexpr uint32_t kOutputWidth = 1280;
constexpr uint32_t kOutputHeight = 720;
constexpr uint32_t kFps = 60;
constexpr uint32_t kBufferSeconds = 8;
constexpr uint32_t kSaveSeconds = 2;
constexpr auto kInitialWarmup = std::chrono::seconds(4);
constexpr auto kResizeWarmup = std::chrono::seconds(4);
constexpr auto kMinimizedWatchdogWindow = std::chrono::seconds(5);
constexpr auto kRestoreWarmup = std::chrono::seconds(4);
constexpr auto kResizeObservation = std::chrono::seconds(15);
constexpr auto kCloseObservation = std::chrono::seconds(2);
constexpr auto kSaveTimeout = std::chrono::seconds(45);

struct WindowState {
    bool closed = false;
    unsigned int animation_frame = 0;
    // Save responses must outlive every asynchronous save worker, including
    // the bounded-timeout/error path, until CaptureEngine::Shutdown joins it.
    std::vector<std::unique_ptr<fthr::SharedMemoryLayout>> save_responses;
};

WindowState* StateFor(HWND window) noexcept {
    return reinterpret_cast<WindowState*>(GetWindowLongPtrW(
        window, GWLP_USERDATA));
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam,
                            LPARAM lparam) {
    auto* state = StateFor(window);
    switch (message) {
    case WM_NCCREATE: {
        const auto* create = reinterpret_cast<const CREATESTRUCTW*>(lparam);
        SetWindowLongPtrW(window, GWLP_USERDATA,
                          reinterpret_cast<LONG_PTR>(create->lpCreateParams));
        return TRUE;
    }
    case WM_TIMER:
        if (state) {
            ++state->animation_frame;
            InvalidateRect(window, nullptr, FALSE);
        }
        return 0;
    case WM_PAINT: {
        PAINTSTRUCT paint{};
        const HDC dc = BeginPaint(window, &paint);
        RECT client{};
        GetClientRect(window, &client);
        const int width = (std::max)(1L, client.right - client.left);
        const int height = (std::max)(1L, client.bottom - client.top);
        const unsigned int frame = state ? state->animation_frame : 0;
        const int x = static_cast<int>((frame * 11u) %
                                       static_cast<unsigned int>((std::max)(1, width - 100)));
        const int y = static_cast<int>((frame * 7u) %
                                       static_cast<unsigned int>((std::max)(1, height - 80)));
        HBRUSH background = CreateSolidBrush(RGB(19, 27, 48));
        FillRect(dc, &client, background);
        DeleteObject(background);
        RECT moving{ x, y, (std::min)(width, x + 100),
                     (std::min)(height, y + 80) };
        HBRUSH accent = CreateSolidBrush(RGB(
            40 + static_cast<BYTE>((frame * 3u) % 180u),
            120 + static_cast<BYTE>((frame * 5u) % 100u),
            210));
        FillRect(dc, &moving, accent);
        DeleteObject(accent);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(240, 245, 255));
        const std::wstring label = L"FTHR WGC WINDOW SMOKE  frame="
            + std::to_wstring(frame);
        TextOutW(dc, 18, 18, label.c_str(), static_cast<int>(label.size()));
        EndPaint(window, &paint);
        return 0;
    }
    case WM_CLOSE:
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        if (state) state->closed = true;
        KillTimer(window, 1);
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(window, message, wparam, lparam);
    }
}

bool PumpMessages(HWND window, WindowState& state) {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        if (message.message == WM_QUIT) {
            state.closed = true;
            return false;
        }
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return IsWindow(window) != FALSE && !state.closed;
}

bool PumpFor(HWND window, WindowState& state, std::chrono::milliseconds duration) {
    const auto deadline = Clock::now() + duration;
    while (Clock::now() < deadline) {
        if (!PumpMessages(window, state)) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return PumpMessages(window, state);
}

void Emit(const std::string& line) {
    std::cout << "WINDOW_SMOKE " << line << std::endl;
}

bool Fail(const char* phase, const std::string& detail) {
    std::cerr << "WINDOW_SMOKE_ERROR phase=" << phase
              << " detail=" << detail << std::endl;
    return false;
}

std::string Narrow(const volatile wchar_t* value) {
    if (!value) return {};
    std::string result;
    for (size_t index = 0; index < 512 && value[index] != L'\0'; ++index) {
        const wchar_t character = value[index];
        result.push_back(character >= 0 && character <= 0x7f
            ? static_cast<char>(character) : '?');
    }
    return result;
}

bool ParseEncoder(const std::wstring& value, fthr::EncoderPreference& preference,
                  const char*& name) {
    if (value == L"nvidia") {
        preference = fthr::EncoderPreference::Nvidia;
        name = "nvidia";
        return true;
    }
    if (value == L"amd") {
        preference = fthr::EncoderPreference::Amd;
        name = "amd";
        return true;
    }
    if (value == L"intel") {
        preference = fthr::EncoderPreference::Intel;
        name = "intel";
        return true;
    }
    if (value == L"software") {
        preference = fthr::EncoderPreference::Software;
        name = "software";
        return true;
    }
    return false;
}

bool ResizeClient(HWND window, int width, int height) {
    RECT outer{0, 0, width, height};
    const LONG style = static_cast<LONG>(GetWindowLongW(window, GWL_STYLE));
    const LONG ex_style = static_cast<LONG>(GetWindowLongW(window, GWL_EXSTYLE));
    if (!AdjustWindowRectEx(&outer, static_cast<DWORD>(style), FALSE,
                            static_cast<DWORD>(ex_style))) {
        return false;
    }
    return SetWindowPos(window, nullptr, 0, 0,
                        outer.right - outer.left, outer.bottom - outer.top,
                        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE) != FALSE;
}

bool WaitForProgress(HWND window, WindowState& state, fthr::CaptureEngine& engine,
                     const char* phase, uint64_t before,
                     std::chrono::milliseconds duration, uint64_t& after) {
    Emit(std::string("phase=") + phase + " state=progress_start frames="
         + std::to_string(before));
    const auto deadline = Clock::now() + duration;
    after = before;
    bool observed_progress = false;
    while (Clock::now() < deadline) {
        if (!PumpMessages(window, state))
            return Fail(phase, "window_closed_during_progress");
        after = engine.GetFrameCount();
        if (after > before && !observed_progress) {
            observed_progress = true;
            Emit(std::string("phase=") + phase + " state=progress frames="
                 + std::to_string(after) + " health="
                 + std::to_string(engine.GetCaptureHealthFlags()));
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    after = engine.GetFrameCount();
    return observed_progress && after > before ||
        (Fail(phase, "no_frame_progress before=" + std::to_string(before)
              + " after=" + std::to_string(after)), false);
}

bool SavePhase(HWND window, WindowState& state, fthr::CaptureEngine& engine,
               const char* phase, const std::filesystem::path& output,
               const char* artifact_name) {
    const auto clip = output / artifact_name;
    if (std::filesystem::exists(clip))
        return Fail(phase, "refusing_to_overwrite=" + clip.string());
    state.save_responses.push_back(
        std::make_unique<fthr::SharedMemoryLayout>());
    auto& response = *state.save_responses.back();
    response.engine_response = fthr::ResponseType::NONE;
    Emit(std::string("phase=") + phase + " state=save_start artifact="
         + artifact_name + " duration_seconds=" + std::to_string(kSaveSeconds));
    if (!engine.SaveClip(clip.c_str(), kSaveSeconds, &response)) {
        return Fail(phase, "save_admission detail="
                    + Narrow(response.engine_string));
    }
    const auto deadline = Clock::now() + kSaveTimeout;
    while (Clock::now() < deadline
           && response.engine_response == fthr::ResponseType::NONE) {
        if (!PumpMessages(window, state))
            return Fail(phase, "window_closed_during_save");
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    if (response.engine_response != fthr::ResponseType::CLIP_SAVED) {
        return Fail(phase, "save_completion response="
                    + std::to_string(static_cast<uint32_t>(response.engine_response))
                    + " detail=" + Narrow(response.engine_string));
    }
    Emit(std::string("phase=") + phase + " state=save_success artifact="
         + artifact_name + " response=CLIP_SAVED");
    return true;
}

bool RunCaptureSavePhase(HWND window, WindowState& state, fthr::CaptureEngine& engine,
                         const char* phase, const std::filesystem::path& output,
                         const char* artifact_name, std::chrono::milliseconds warmup,
                         uint64_t& baseline) {
    baseline = engine.GetFrameCount();
    if (!WaitForProgress(window, state, engine, phase, baseline, warmup, baseline))
        return false;
    if (!SavePhase(window, state, engine, phase, output, artifact_name)) return false;
    Emit(std::string("phase=") + phase + " state=pass artifact=" + artifact_name
         + " frames=" + std::to_string(baseline));
    return true;
}

bool CheckMinimized(HWND window, WindowState& state, fthr::CaptureEngine& engine,
                    uint64_t& paused_frames) {
    paused_frames = engine.GetFrameCount();
    ShowWindow(window, SW_MINIMIZE);
    if (!PumpMessages(window, state)) return Fail("minimize", "window_closed_on_minimize");
    Emit(std::string("phase=minimize state=minimized frames=")
         + std::to_string(paused_frames));
    const auto pause_deadline = Clock::now() + std::chrono::seconds(2);
    uint64_t settled_frames = paused_frames;
    uint32_t settled_generation = engine.GetCaptureGeneration();
    bool observed_paused = false;
    while (Clock::now() < pause_deadline) {
        if (!PumpMessages(window, state))
            return Fail("minimize", "window_closed_while_minimized");
        const uint32_t health = engine.GetCaptureHealthFlags();
        settled_frames = engine.GetFrameCount();
        settled_generation = engine.GetCaptureGeneration();
        if ((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0)
            return Fail("minimize", "backend_failed_while_minimized");
        // One frame can already be in flight when ShowWindow takes effect.
        if ((health & fthr::CAPTURE_HEALTH_PAUSED) != 0
            && settled_frames <= paused_frames + 1) {
            observed_paused = true;
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    if (!observed_paused)
        return Fail("minimize", "capture_did_not_enter_bounded_paused_state");
    paused_frames = settled_frames;
    if (!PumpFor(window, state,
                 std::chrono::duration_cast<std::chrono::milliseconds>(
                     kMinimizedWatchdogWindow)))
        return Fail("minimize", "window_closed_while_minimized");
    const uint64_t after = engine.GetFrameCount();
    const uint32_t health = engine.GetCaptureHealthFlags();
    Emit(std::string("phase=minimize state=paused frames_before=")
         + std::to_string(settled_frames) + " frames_after=" + std::to_string(after)
         + " health=" + std::to_string(health));
    if (after != settled_frames)
        return Fail("minimize", "unexpected_frame_progress_while_minimized");
    if ((health & fthr::CAPTURE_HEALTH_PAUSED) == 0)
        return Fail("minimize", "missing_capture_health_paused");
    if (engine.GetCaptureGeneration() != settled_generation)
        return Fail("minimize", "capture_generation_changed_while_minimized");
    if ((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0)
        return Fail("minimize", "backend_failed_after_watchdog_window");
    if (engine.GetStartupCaptureBackend() != "WGC_WINDOW")
        return Fail("minimize", "capture_backend_changed="
                    + engine.GetStartupCaptureBackend());
    Emit("phase=minimize state=pass watchdog_seconds=5 backend=WGC_WINDOW");
    return true;
}

bool ReinitializeAfterGenerationFailure(
    HWND window, WindowState& state, fthr::CaptureEngine& engine,
    const fthr::CaptureConfig& config, const std::filesystem::path& output,
    const char* requested_encoder, const char* phase,
    const char* rejected_artifact);

bool RestoreWindow(HWND window, WindowState& state, fthr::CaptureEngine& engine,
                   const fthr::CaptureConfig& config,
                   const std::filesystem::path& output, uint64_t baseline,
                   const char* requested_encoder) {
    ShowWindow(window, SW_RESTORE);
    SetForegroundWindow(window);
    InvalidateRect(window, nullptr, TRUE);
    if (!PumpMessages(window, state)) return Fail("restore", "window_closed_on_restore");

    // Restore can publish one final DWM ContentSize that differs from the
    // minimized generation. Give the engine a short bounded window to report
    // that exact reconfigure contract before asking for the normal warmup.
    const auto stabilization_deadline = Clock::now() + std::chrono::seconds(3);
    while (Clock::now() < stabilization_deadline) {
        if (!PumpMessages(window, state))
            return Fail("restore", "window_closed_during_restore_stabilization");
        const uint32_t health = engine.GetCaptureHealthFlags();
        if ((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0) {
            if ((health & fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED) == 0)
                return Fail("restore", "backend_failed_during_restore health="
                            + std::to_string(health));
            if (!ReinitializeAfterGenerationFailure(
                    window, state, engine, config, output, requested_encoder,
                    "restore", "window-restore-before-restart.mp4")) {
                return false;
            }
            baseline = engine.GetFrameCount();
            break;
        }
        if (engine.GetFrameCount() > baseline)
            break;
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    uint64_t frames = baseline;
    if (!WaitForProgress(window, state, engine, "restore", baseline,
                         std::chrono::duration_cast<std::chrono::milliseconds>(
                             kRestoreWarmup), frames)) return false;
    if (!SavePhase(window, state, engine, "restore", output, "window-restore.mp4"))
        return false;
    Emit("phase=restore state=pass artifact=window-restore.mp4 frames="
         + std::to_string(frames));
    return true;
}

bool ReinitializeAfterGenerationFailure(
    HWND window, WindowState& state, fthr::CaptureEngine& engine,
    const fthr::CaptureConfig& config, const std::filesystem::path& output,
    const char* requested_encoder, const char* phase,
    const char* rejected_artifact) {
    const auto deadline = Clock::now() + kResizeObservation;
    uint32_t health = fthr::CAPTURE_HEALTH_NONE;
    while (Clock::now() < deadline) {
        if (!PumpMessages(window, state))
            return Fail(phase, "window_closed_before_reconfigure_failure");
        health = engine.GetCaptureHealthFlags();
        if ((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0
            && (health & fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED) != 0) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    if ((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) == 0
        || (health & fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED) == 0) {
        return Fail(phase, "missing_reconfigure_required health="
                    + std::to_string(health));
    }
    Emit(std::string("phase=") + phase
         + " state=reconfigure_required capture_backend=WGC_WINDOW health="
         + std::to_string(health));

    const auto stale_clip = output / rejected_artifact;
    if (std::filesystem::exists(stale_clip))
        return Fail(phase, "refusing_to_overwrite=" + stale_clip.string());
    state.save_responses.push_back(
        std::make_unique<fthr::SharedMemoryLayout>());
    auto& response = *state.save_responses.back();
    response.engine_response = fthr::ResponseType::NONE;
    if (engine.SaveClip(stale_clip.c_str(), kSaveSeconds, &response))
        return Fail(phase, "save_admission_succeeded_before_restart");
    if (response.engine_response != fthr::ResponseType::ERROR_OCCURRED)
        return Fail(phase, "save_before_restart_missing_error_response");
    Emit(std::string("phase=") + phase
         + " state=save_rejected response=ERROR_OCCURRED");

    const auto shutdown_started = Clock::now();
    engine.Shutdown();
    const auto shutdown_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        Clock::now() - shutdown_started).count();
    Emit(std::string("phase=") + phase + " state=shutdown elapsed_ms="
         + std::to_string(shutdown_ms));

    // CaptureEngine's SaveClipQueue is intentionally terminal after Shutdown;
    // caller-side restart therefore needs a fresh engine object. Keep the
    // caller's storage and WindowState intact while reusing the exact config
    // and HWND below, which models a bounded application restart.
    engine.~CaptureEngine();
    new (&engine) fthr::CaptureEngine();
    if (!engine.Initialize(config)) {
        return Fail(phase, "reinitialize_failed startup_code="
                    + std::string(fthr::ReplayStartupErrorName(
                        engine.GetLastStartupErrorCode()))
                    + " detail=" + engine.GetLastStartupError());
    }
    if (engine.GetStartupCaptureBackend() != "WGC_WINDOW") {
        return Fail(phase, "reinitialize_capture_backend="
                    + engine.GetStartupCaptureBackend());
    }
    const auto& capability = engine.GetActiveReplayCapability();
    const std::string active_backend = fthr::ReplayEncoderBackendName(
        capability.active_backend);
    Emit(std::string("phase=") + phase
         + " state=restarted capture_backend=WGC_WINDOW requested_encoder="
         + std::string(requested_encoder) + " active_encoder_backend="
         + active_backend + " target_width=1280 target_height=720");
    return true;
}

bool CloseWindowAndInvalidate(HWND window, WindowState& state,
                              fthr::CaptureEngine& engine,
                              const std::filesystem::path& output) {
    DestroyWindow(window);
    const auto deadline = Clock::now() + kCloseObservation;
    bool invalidated = false;
    while (Clock::now() < deadline) {
        PumpMessages(window, state);
        const uint32_t health = engine.GetCaptureHealthFlags();
        invalidated = (health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0;
        if (invalidated) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    const uint32_t health = engine.GetCaptureHealthFlags();
    invalidated = state.closed
        && (health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0;
    Emit(std::string("phase=close state=invalidated window_closed=")
         + (state.closed ? "true" : "false") + " backend_failed="
         + (((health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) != 0) ? "true" : "false")
         + " health=" + std::to_string(health));
    if (!invalidated) return Fail("close", "window_close_not_invalidated");
    const auto after_close_clip = output / "window-after-close.mp4";
    state.save_responses.push_back(
        std::make_unique<fthr::SharedMemoryLayout>());
    auto& response = *state.save_responses.back();
    response.engine_response = fthr::ResponseType::NONE;
    if (engine.SaveClip(after_close_clip.c_str(), kSaveSeconds, &response))
        return Fail("close", "save_admission_succeeded_after_window_close");
    if (response.engine_response != fthr::ResponseType::ERROR_OCCURRED)
        return Fail("close", "save_after_close_missing_error_response");
    Emit("phase=close state=save_rejected response=ERROR_OCCURRED");
    return true;
}

bool CreateSmokeWindow(HINSTANCE instance, WindowState& state, HWND& window) {
    WNDCLASSEXW klass{sizeof(WNDCLASSEXW)};
    klass.style = CS_HREDRAW | CS_VREDRAW;
    klass.lpfnWndProc = WindowProc;
    klass.hInstance = instance;
    klass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    klass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    klass.lpszClassName = kWindowClassName;
    if (!RegisterClassExW(&klass)
        && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return false;
    window = CreateWindowExW(
        0, kWindowClassName, kWindowTitle, WS_OVERLAPPEDWINDOW,
        60, 60, kInitialWidth, kInitialHeight, nullptr,
        nullptr, instance, &state);
    if (!window) return false;
    if (!ResizeClient(window, kInitialWidth, kInitialHeight)) {
        DestroyWindow(window);
        window = nullptr;
        return false;
    }
    SetTimer(window, 1, 16, nullptr);
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    return true;
}

void ShutdownMeasured(fthr::CaptureEngine& engine, const char* phase = "close") {
    const auto started = Clock::now();
    engine.Shutdown();
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        Clock::now() - started).count();
    Emit(std::string("phase=") + phase + " state=shutdown elapsed_ms="
         + std::to_string(elapsed));
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    std::filesystem::path output;
    std::wstring encoder_value = L"amd";
    std::wstring replay_storage;
    for (int index = 1; index < argc; ++index) {
        const std::wstring argument = argv[index];
        if (argument == L"--output" && index + 1 < argc) {
            output = argv[++index];
        } else if (argument == L"--encoder" && index + 1 < argc) {
            encoder_value = argv[++index];
        } else if (argument == L"--replay-storage" && index + 1 < argc) {
            replay_storage = argv[++index];
        } else {
            std::wcerr << L"WINDOW_SMOKE_ERROR phase=arguments detail=unknown_or_incomplete_"
                       << argument << std::endl;
            return 2;
        }
    }
    if (output.empty()) {
        std::wcerr << L"WINDOW_SMOKE_ERROR phase=arguments detail=output_required" << std::endl;
        return 2;
    }
    if (!replay_storage.empty() && replay_storage != L"memory" && replay_storage != L"disk") {
        std::wcerr << L"WINDOW_SMOKE_ERROR phase=arguments detail=invalid_replay_storage" << std::endl;
        return 2;
    }
    fthr::EncoderPreference encoder_preference{};
    const char* requested_encoder = nullptr;
    if (!ParseEncoder(encoder_value, encoder_preference, requested_encoder)) {
        std::wcerr << L"WINDOW_SMOKE_ERROR phase=arguments detail=invalid_encoder_"
                   << encoder_value << std::endl;
        return 2;
    }
    std::error_code fs_error;
    if (!std::filesystem::is_directory(output, fs_error)) {
        std::wcerr << L"WINDOW_SMOKE_ERROR phase=arguments detail=output_not_directory" << std::endl;
        return 2;
    }
    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    std::cout << std::unitbuf;
    WindowState window_state;
    HWND window = nullptr;
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    const HWND previous_foreground = GetForegroundWindow();
    if (!CreateSmokeWindow(instance, window_state, window)) {
        std::cerr << "WINDOW_SMOKE_ERROR phase=window_create detail=CreateWindowExW_failed"
                  << std::endl;
        winrt::uninit_apartment();
        return 3;
    }
    fthr::CaptureEngine engine;
    fthr::CaptureConfig config;
    config.framerate = kFps;
    config.buffer_seconds = kBufferSeconds;
    config.target_width = kOutputWidth;
    config.target_height = kOutputHeight;
    config.bitrate_kbps = 12000;
    config.video_codec = fthr::VideoCodec::H264;
    config.encoder_preference = encoder_preference;
    config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
    config.target_hwnd = reinterpret_cast<uintptr_t>(window);
    config.capture_backend_preference = fthr::WindowsCaptureBackendPreference::Wgc;
    config.audio_enabled = false;
    config.separate_audio_enabled = false;
    config.scaling_mode = fthr::CaptureConfig::ScalingModeEnum::FIT;
    if (!replay_storage.empty()) {
        config.replay_storage.mode = replay_storage == L"disk"
            ? fthr::ReplayStorageMode::Disk : fthr::ReplayStorageMode::Memory;
        config.replay_storage.directory = output / "replay-buffer";
        config.replay_storage.memory_mb = 128;
        config.replay_storage.disk_mb = 1024;
        std::filesystem::create_directories(config.replay_storage.directory);
    }
    Emit(std::string("phase=initialize state=start requested_encoder=")
         + requested_encoder + " capture_mode=WINDOW target_width="
         + std::to_string(kOutputWidth) + " target_height="
         + std::to_string(kOutputHeight) + " fps=60 audio=disabled");
    if (!engine.Initialize(config)) {
        std::cerr << "WINDOW_SMOKE_ERROR phase=initialize native_startup_code="
                  << fthr::ReplayStartupErrorName(engine.GetLastStartupErrorCode())
                  << " detail=" << engine.GetLastStartupError() << std::endl;
        engine.Shutdown();
        DestroyWindow(window);
        PumpMessages(window, window_state);
        if (previous_foreground && IsWindow(previous_foreground))
            SetForegroundWindow(previous_foreground);
        winrt::uninit_apartment();
        return 4;
    }
    const auto& capability = engine.GetActiveReplayCapability();
    const std::string active_vendor = fthr::EncoderVendorName(capability.encoder_vendor);
    const std::string active_backend = fthr::ReplayEncoderBackendName(
        capability.active_backend);
    const std::string amd_proof = active_vendor == "AMD"
        ? "not_claimed" : "unavailable_on_non_amd_adapter";
    Emit(std::string("identity schema=1 capture_mode=WINDOW capture_backend=")
         + engine.GetStartupCaptureBackend() + " requested_encoder="
         + requested_encoder + " active_encoder_backend=" + active_backend
         + " active_vendor=" + active_vendor + " video_codec=H264 fps=60"
           " target_width=1280 target_height=720 audio=disabled video_only=true"
           " amd_hardware_proof=" + amd_proof
         + (replay_storage.empty() ? "" : std::string(" replay_storage=")
            + (replay_storage == L"disk" ? "disk" : "memory")));
    Emit("phase=initialize state=pass");

    bool success = true;
    uint64_t frames = 0;
    Emit("phase=initial state=start");
    success = RunCaptureSavePhase(
        window, window_state, engine, "initial", output, "window-initial.mp4",
        std::chrono::duration_cast<std::chrono::milliseconds>(kInitialWarmup), frames);
    if (success) {
        if (!ResizeClient(window, kResizedWidth, kResizedHeight)) {
            success = Fail("resize", "SetWindowPos_failed");
        } else if (!PumpFor(window, window_state, std::chrono::milliseconds(500))) {
            success = Fail("resize", "window_closed_after_resize");
        } else {
            Emit("phase=resize state=changed width="
                 + std::to_string(kResizedWidth) + " height="
                 + std::to_string(kResizedHeight));
            success = ReinitializeAfterGenerationFailure(
                window, window_state, engine, config, output, requested_encoder,
                "resize", "window-resize-before-restart.mp4");
            if (success) {
                success = RunCaptureSavePhase(
                    window, window_state, engine, "resize", output,
                    "window-resize.mp4",
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        kResizeWarmup), frames);
            }
        }
    }
    uint64_t paused_frames = 0;
    if (success) success = CheckMinimized(window, window_state, engine, paused_frames);
    if (success) {
        success = RestoreWindow(window, window_state, engine, config, output,
                                paused_frames, requested_encoder);
    }
    if (success) {
        Emit("phase=close state=start");
        success = CloseWindowAndInvalidate(window, window_state, engine, output);
    }
    if (window && !window_state.closed) DestroyWindow(window);
    if (success) {
        ShutdownMeasured(engine);
        Emit("phase=close state=pass");
    } else {
        ShutdownMeasured(engine);
    }
    if (previous_foreground && IsWindow(previous_foreground))
        SetForegroundWindow(previous_foreground);
    winrt::uninit_apartment();
    return success ? 0 : 5;
}
