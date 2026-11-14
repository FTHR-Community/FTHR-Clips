#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include "capture_engine.h"
#include "wgc_window_lifecycle.h"

#include <algorithm>
#include <objbase.h>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

#pragma comment(lib, "gdi32.lib")

namespace {

constexpr wchar_t kWindowClass[] = L"FTHR_WgcResizeSmoke_Window";
uint32_t g_paint_phase = 0;
constexpr uint32_t kFailureMask = fthr::CAPTURE_HEALTH_BACKEND_FAILED
    | fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED;

void LogApartmentState(const char* phase) {
    APTTYPE type{};
    APTTYPEQUALIFIER qualifier{};
    const HRESULT result = CoGetApartmentType(&type, &qualifier);
    std::cout << "WGC_SMOKE_APARTMENT {\"phase\":\"" << phase
              << "\",\"co_get_apartment_hr\":\"0x"
              << std::hex << static_cast<uint32_t>(result) << std::dec << '"';
    if (SUCCEEDED(result)) {
        std::cout << ",\"apartment_type\":" << static_cast<int>(type)
                  << ",\"apartment_qualifier\":"
                  << static_cast<int>(qualifier);
    }
    std::cout << '}'
              << std::endl;
}

bool ExplicitWinRtApartmentProbeEnabled() {
    wchar_t value[8]{};
    const DWORD length = GetEnvironmentVariableW(
        L"FTHR_WGC_SMOKE_OWNED_APARTMENT", value,
        static_cast<DWORD>(sizeof(value) / sizeof(value[0])));
    return length == 1 && value[0] == L'1';
}

LRESULT CALLBACK SmokeWindowProc(HWND hwnd, UINT message,
                                 WPARAM wparam, LPARAM lparam) {
    if (message == WM_PAINT) {
        PAINTSTRUCT paint{};
        HDC dc = BeginPaint(hwnd, &paint);
        RECT client{};
        GetClientRect(hwnd, &client);
        const COLORREF base = RGB(25 + (g_paint_phase * 29) % 150,
                                  65 + (g_paint_phase * 47) % 150,
                                  40 + (g_paint_phase * 61) % 150);
        HBRUSH brush = CreateSolidBrush(base);
        FillRect(dc, &client, brush);
        DeleteObject(brush);
        const LONG marker_width = (std::max)(20L, client.right / 5);
        const LONG marker_left = static_cast<LONG>(
            (g_paint_phase * 37) % (std::max)(1L, client.right - marker_width));
        RECT marker{marker_left, client.bottom / 4,
                    marker_left + marker_width, client.bottom * 3 / 4};
        brush = CreateSolidBrush(RGB(245, 190, 30));
        FillRect(dc, &marker, brush);
        DeleteObject(brush);
        EndPaint(hwnd, &paint);
        return 0;
    }
    if (message == WM_CREATE) {
        SetTimer(hwnd, 1, 100, nullptr);
        return 0;
    }
    if (message == WM_TIMER) {
        ++g_paint_phase;
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    }
    if (message == WM_DESTROY) {
        KillTimer(hwnd, 1);
        return 0;
    }
    if (message == WM_ERASEBKGND) return 1;
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

void PumpMessages() {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

bool WaitForState(fthr::CaptureEngine& engine, HWND hwnd,
                  uint32_t required, uint32_t forbidden,
                  std::chrono::seconds timeout, const char* label) {
    const auto started = std::chrono::steady_clock::now();
    const auto initial_frames = engine.GetFrameCount();
    const auto deadline = started + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
        PumpMessages();
        const uint32_t health = engine.GetCaptureHealthFlags();
        std::cout << "WGC_SMOKE {\"phase\":\"" << label
                  << "\",\"health\":" << health
                  << ",\"frames\":" << engine.GetFrameCount() << '}'
                  << std::endl;
        if (health & kFailureMask) return false;
        const auto elapsed = std::chrono::steady_clock::now() - started;
        if ((health & required) == required && (health & forbidden) == 0
            && engine.GetFrameCount() >= initial_frames + 15
            && elapsed >= std::chrono::seconds(1)) return true;
        if (!IsWindow(hwnd)) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    return false;
}

bool WaitForPausedAndStable(fthr::CaptureEngine& engine, HWND hwnd) {
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(10);
    while (std::chrono::steady_clock::now() < deadline) {
        PumpMessages();
        const uint32_t health = engine.GetCaptureHealthFlags();
        const auto frames_before = engine.GetFrameCount();
        if (health & kFailureMask) return false;
        if (health & fthr::CAPTURE_HEALTH_PAUSED) {
            const auto settled = std::chrono::steady_clock::now()
                + std::chrono::seconds(1);
            while (std::chrono::steady_clock::now() < settled) {
                PumpMessages();
                if (engine.GetCaptureHealthFlags() & kFailureMask) return false;
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            return engine.GetFrameCount() == frames_before && IsWindow(hwnd);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

bool WaitForClosedFailure(fthr::CaptureEngine& engine) {
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(10);
    while (std::chrono::steady_clock::now() < deadline) {
        PumpMessages();
        const uint32_t health = engine.GetCaptureHealthFlags();
        if (health & fthr::CAPTURE_HEALTH_BACKEND_FAILED) {
            return (health & fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED) == 0;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

std::wstring UniqueOutputPath() {
    wchar_t directory[MAX_PATH]{};
    if (!GetTempPathW(MAX_PATH, directory)) return L"wgc_resize_smoke.mp4";
    GUID id{};
    CoCreateGuid(&id);
    wchar_t suffix[64]{};
    swprintf_s(suffix, L"wgc_resize_smoke_%08lx%04x.mp4",
        id.Data1, id.Data2);
    return std::wstring(directory) + suffix;
}

bool SaveClipWithDuration(fthr::CaptureEngine& engine,
                          const std::wstring& output,
                          uint32_t duration_seconds) {
    fthr::SharedMemoryLayout shared_memory{};
    const bool accepted = engine.SaveClip(
        output.c_str(), duration_seconds, &shared_memory);
    if (accepted) {
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(30);
        while (std::chrono::steady_clock::now() < deadline
            && shared_memory.engine_response != fthr::ResponseType::CLIP_SAVED
            && shared_memory.engine_response != fthr::ResponseType::ERROR_OCCURRED) {
            PumpMessages();
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }
    return accepted
        && shared_memory.engine_response == fthr::ResponseType::CLIP_SAVED
        && std::filesystem::exists(output)
        && std::filesystem::file_size(output) > 0;
}

std::wstring PartialOutputPath(const std::wstring& full_output) {
    std::filesystem::path partial(full_output);
    partial.replace_filename(partial.stem().wstring() + L"-partial.mp4");
    if (std::filesystem::exists(partial)) return {};
    return partial.wstring();
}

bool WaitForReconfigureFailure(fthr::CaptureEngine& engine, HWND hwnd) {
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(15);
    while (std::chrono::steady_clock::now() < deadline) {
        PumpMessages();
        const auto health = engine.GetCaptureHealthFlags();
        if ((health & fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED)
            && (health & fthr::CAPTURE_HEALTH_BACKEND_FAILED)) return true;
        if (!IsWindow(hwnd)) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    const std::wstring mode = argc > 1 ? argv[1] : L"--resize-minimize-restore";
    if (mode != L"--resize-minimize-restore" && mode != L"--initial-minimized"
        && mode != L"--static-lifecycle" && mode != L"--native-source-startup"
        && mode != L"--resize-reconfigure-minimize-restore") {
        std::wcerr << L"Usage: windows_wgc_resize_smoke.exe "
                      L"--resize-minimize-restore [duration-seconds] | "
                      L"--initial-minimized | --static-lifecycle [--output fresh.mp4] | "
                      L"--native-source-startup [--output fresh.mp4] | "
                      L"--resize-reconfigure-minimize-restore [--output fresh.mp4]\n";
        return 2;
    }
    uint32_t duration_seconds = 30;
    std::wstring output_path;
    if (argc > 2) {
        if (std::wstring(argv[2]) == L"--output" && argc > 3) {
            output_path = argv[3];
        } else {
            const long parsed = wcstol(argv[2], nullptr, 10);
            if (parsed < 12 || parsed > 180) {
                std::wcerr << L"duration-seconds must be between 12 and 180\n";
                return 2;
            }
            duration_seconds = static_cast<uint32_t>(parsed);
        }
    }
    if (output_path.empty() && argc > 3) {
        if (std::wstring(argv[3]) != L"--output" || argc <= 4) return 2;
        output_path = argv[4];
    }
    if (!output_path.empty()) {
        const std::filesystem::path requested(output_path);
        if (!requested.is_absolute() || requested.extension() != L".mp4"
            || std::filesystem::exists(requested)) {
            std::wcerr << L"--output must be a fresh absolute .mp4 path\n";
            return 2;
        }
    }

    // This opt-in harness probe initializes the caller thread before any WGC
    // WinRT activation and keeps the apartment alive through all engine/object
    // destruction. Leave it disabled by default to retain the failing baseline.
    const bool owned_apartment_enabled = ExplicitWinRtApartmentProbeEnabled();
    LogApartmentState("before_optional_ro_initialize");
    // This remains opt-in so the historical uninitialized-apartment control
    // can still reproduce the old boundary. When enabled, exercise the exact
    // production RAII guard and keep it alive through all engine destruction.
    std::unique_ptr<fthr::wgc::WinRtApartmentGuard> apartment_guard;
    if (owned_apartment_enabled) {
        apartment_guard = std::make_unique<fthr::wgc::WinRtApartmentGuard>();
        std::cout << "WGC_SMOKE_APARTMENT {\"phase\":\"ro_initialize\","
                  << "\"hr\":\"0x" << std::hex
                  << static_cast<uint32_t>(apartment_guard->Result()) << std::dec
                  << "\",\"enabled\":true}" << std::endl;
        if (!apartment_guard->Succeeded()) {
            std::cerr << "RoInitialize(RO_INIT_MULTITHREADED) failed with HRESULT 0x"
                      << std::hex
                      << static_cast<uint32_t>(apartment_guard->Result())
                      << std::dec << "; probe aborted" << std::endl;
            return 6;
        }
    } else {
        std::cout << "WGC_SMOKE_APARTMENT {\"phase\":\"ro_initialize\","
                     "\"attempted\":false,\"enabled\":false}"
                  << std::endl;
    }
    LogApartmentState("after_optional_ro_initialize");

    HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSW window_class{};
    window_class.lpfnWndProc = SmokeWindowProc;
    window_class.hInstance = instance;
    window_class.lpszClassName = kWindowClass;
    if (!RegisterClassW(&window_class)
        && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
        std::cerr << "RegisterClassW failed\n";
        return 3;
    }
    HWND hwnd = CreateWindowExW(0, kWindowClass, L"FTHR WGC lifecycle smoke",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT,
        656, 399, nullptr, nullptr, instance, nullptr);
    if (!hwnd) {
        std::cerr << "CreateWindowExW failed\n";
        return 3;
    }
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    if (mode == L"--initial-minimized") {
        ShowWindow(hwnd, SW_MINIMIZE);
        PumpMessages();
    } else if (mode == L"--static-lifecycle") {
        // Keep the window's initial painted image static for the complete
        // capture. This verifies startup owns one valid frame rather than
        // relying on a later WM_TIMER repaint to begin encoding.
        KillTimer(hwnd, 1);
    }

    fthr::CaptureConfig config;
    config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
    config.target_hwnd = reinterpret_cast<uintptr_t>(hwnd);
    config.capture_backend_preference =
        fthr::WindowsCaptureBackendPreference::Wgc;
    config.framerate = 60;
    config.buffer_seconds = (std::min)(duration_seconds, 30u);
    config.target_width = mode == L"--native-source-startup" ? 0 : 640;
    config.target_height = mode == L"--native-source-startup" ? 0 : 360;
    config.video_codec = fthr::VideoCodec::H264;
    config.encoder_preference = fthr::EncoderPreference::Nvidia;
    config.scaling_mode = fthr::CaptureConfig::ScalingModeEnum::STRETCH;
    config.audio_enabled = false;

    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        const auto error = engine.GetLastStartupError();
        std::cerr << "CaptureEngine initialization failed: " << error << '\n';
        const bool expected = mode == L"--initial-minimized"
            && error.find("restore it and retry") != std::string::npos;
        ShowWindow(hwnd, SW_RESTORE);
        DestroyWindow(hwnd);
        std::cout << "WGC_SMOKE {\"result\":\""
                  << (expected ? "PASS_INITIAL_MINIMIZED_REJECTED" : "FAIL")
                  << "\"}" << std::endl;
        return expected ? 0 : 4;
    }
    if (!engine.IsNvencActive()) {
        engine.Shutdown();
        DestroyWindow(hwnd);
        std::cerr << "Requested NVIDIA H.264 encoder did not initialize\n";
        return 5;
    }
    if (mode == L"--initial-minimized") {
        engine.Shutdown();
        ShowWindow(hwnd, SW_RESTORE);
        DestroyWindow(hwnd);
        std::cerr << "Capture unexpectedly initialized a minimized window\n";
        return 1;
    }

    if (mode == L"--static-lifecycle" || mode == L"--native-source-startup") {
        bool passed = WaitForState(engine, hwnd, fthr::CAPTURE_HEALTH_ACTIVE,
            fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "initial");
        const auto output = output_path.empty() ? UniqueOutputPath() : output_path;
        if (passed) passed = SaveClipWithDuration(engine, output, 1);
        std::wcout << L"WGC_SMOKE_OUTPUT " << output << std::endl;
        if (passed && mode == L"--native-source-startup") {
            SetWindowPos(hwnd, nullptr, 0, 0, 816, 489,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
            InvalidateRect(hwnd, nullptr, TRUE);
            passed = WaitForReconfigureFailure(engine, hwnd);
        }
        engine.Shutdown();
        if (IsWindow(hwnd)) DestroyWindow(hwnd);
        UnregisterClassW(kWindowClass, instance);
        std::cout << "WGC_SMOKE {\"result\":\""
                  << (passed ? "PASS" : "FAIL") << "\"}" << std::endl;
        return passed ? 0 : 1;
    }

    if (mode == L"--resize-reconfigure-minimize-restore") {
        std::unique_ptr<fthr::CaptureEngine> restarted_engine;
        bool passed = WaitForState(engine, hwnd, fthr::CAPTURE_HEALTH_ACTIVE,
            fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "initial");
        if (passed) {
            SetWindowPos(hwnd, nullptr, 0, 0, 816, 489,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
            InvalidateRect(hwnd, nullptr, TRUE);
            passed = WaitForReconfigureFailure(engine, hwnd);
            std::cout << "WGC_SMOKE {\"phase\":\"resize_reconfigure_required\","
                      << "\"observed\":" << (passed ? "true" : "false")
                      << "}\n";
        }
        if (passed) {
            // Exercise the same public lifecycle boundary the UI uses, but
            // only once and only after the runtime explicitly requests a new
            // generation. Production restarts the native process, so use a
            // distinct engine instance rather than reusing terminal queues.
            std::cout << "WGC_SMOKE {\"phase\":\"explicit_fresh_instance_restart\","
                         "\"restart_count\":1}\n";
            LogApartmentState("before_first_engine_shutdown");
            engine.Shutdown();
            restarted_engine = std::make_unique<fthr::CaptureEngine>();
            passed = restarted_engine->Initialize(config)
                && restarted_engine->IsNvencActive();
            if (passed) {
                passed = WaitForState(*restarted_engine, hwnd,
                    fthr::CAPTURE_HEALTH_ACTIVE,
                    fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15),
                    "fresh_generation");
            }
        }
        if (passed) {
            ShowWindow(hwnd, SW_MINIMIZE);
            passed = WaitForPausedAndStable(*restarted_engine, hwnd);
        }
        if (passed) {
            ShowWindow(hwnd, SW_RESTORE);
            SetForegroundWindow(hwnd);
            InvalidateRect(hwnd, nullptr, TRUE);
            passed = WaitForState(*restarted_engine, hwnd,
                fthr::CAPTURE_HEALTH_ACTIVE,
                fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "restored");
        }
        if (passed) {
            const auto dwell = std::chrono::steady_clock::now()
                + std::chrono::seconds(5);
            const auto starting_frames = restarted_engine->GetFrameCount();
            while (std::chrono::steady_clock::now() < dwell) {
                PumpMessages();
                if (restarted_engine->GetCaptureHealthFlags() & kFailureMask) {
                    passed = false;
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            passed = passed && restarted_engine->GetFrameCount()
                >= starting_frames + 60;
        }
        if (passed) {
            const auto output = output_path.empty()
                ? UniqueOutputPath() : output_path;
            const bool saved = SaveClipWithDuration(*restarted_engine, output, 1);
            std::cout << "WGC_SMOKE {\"phase\":\"save_after_restart\","
                      << "\"completed\":" << (saved ? "true" : "false")
                      << "}\n";
            std::wcout << L"WGC_SMOKE_OUTPUT " << output << std::endl;
            passed = saved;
            if (passed) {
                const auto partial_output = PartialOutputPath(output);
                const bool partial_saved = !partial_output.empty()
                    && SaveClipWithDuration(*restarted_engine, partial_output, 30);
                std::cout << "WGC_SMOKE {\"phase\":\"partial_save_after_restart\","
                          << "\"completed\":"
                          << (partial_saved ? "true" : "false") << "}\n";
                if (partial_saved) {
                    std::wcout << L"WGC_SMOKE_PARTIAL_OUTPUT "
                               << partial_output << std::endl;
                }
                passed = partial_saved;
            }
        }
        if (passed) {
            DestroyWindow(hwnd);
            passed = WaitForClosedFailure(*restarted_engine);
        }
        LogApartmentState("before_final_shutdown");
        if (restarted_engine) restarted_engine->Shutdown();
        else engine.Shutdown();
        if (IsWindow(hwnd)) DestroyWindow(hwnd);
        UnregisterClassW(kWindowClass, instance);
        std::cout << "WGC_SMOKE {\"mode\":\"resize-reconfigure-minimize-restore\","
                  << "\"result\":\"" << (passed ? "PASS" : "FAIL")
                  << "\"}\n";
        return passed ? 0 : 1;
    }

    bool passed = WaitForState(engine, hwnd, fthr::CAPTURE_HEALTH_ACTIVE,
        fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "initial");
    if (passed) {
        SetWindowPos(hwnd, nullptr, 0, 0, 816, 489,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
        InvalidateRect(hwnd, nullptr, TRUE);
        passed = WaitForState(engine, hwnd, fthr::CAPTURE_HEALTH_ACTIVE,
            fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "resized");
    }
    if (passed) {
        ShowWindow(hwnd, SW_MINIMIZE);
        passed = WaitForPausedAndStable(engine, hwnd);
    }
    if (passed) {
        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
        InvalidateRect(hwnd, nullptr, TRUE);
        passed = WaitForState(engine, hwnd, fthr::CAPTURE_HEALTH_ACTIVE,
            fthr::CAPTURE_HEALTH_PAUSED, std::chrono::seconds(15), "restored");
    }
    if (passed) {
        const auto dwell = std::chrono::steady_clock::now()
            + std::chrono::seconds(5);
        const auto starting_frames = engine.GetFrameCount();
        while (std::chrono::steady_clock::now() < dwell) {
            PumpMessages();
            if (engine.GetCaptureHealthFlags() & kFailureMask) {
                passed = false;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        passed = passed && engine.GetFrameCount() >= starting_frames + 60;
    }
    if (passed) {
        const auto output = output_path.empty()
            ? UniqueOutputPath() : output_path;
        const bool saved = SaveClipWithDuration(engine, output, 1);
        std::cout << "WGC_SMOKE {\"phase\":\"save\",\"completed\":"
                  << (saved ? "true" : "false") << "}" << std::endl;
        std::wcout << L"WGC_SMOKE_OUTPUT " << output << std::endl;
        passed = saved;
        if (passed) {
            const auto partial_output = PartialOutputPath(output);
            const bool partial_saved = !partial_output.empty()
                && SaveClipWithDuration(engine, partial_output, 30);
            std::cout << "WGC_SMOKE {\"phase\":\"partial_save\",\"completed\":"
                      << (partial_saved ? "true" : "false") << "}"
                      << std::endl;
            if (partial_saved) {
                std::wcout << L"WGC_SMOKE_PARTIAL_OUTPUT "
                           << partial_output << std::endl;
            }
            passed = partial_saved;
        }
    }
    if (passed) {
        DestroyWindow(hwnd);
        passed = WaitForClosedFailure(engine);
    }

    engine.Shutdown();
    if (IsWindow(hwnd)) DestroyWindow(hwnd);
    UnregisterClassW(kWindowClass, instance);
    std::cout << "WGC_SMOKE {\"result\":\""
              << (passed ? "PASS" : "FAIL") << "\"}" << std::endl;
    return passed ? 0 : 1;
}
