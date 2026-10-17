// A separate test window process named valorant.exe selects the real
// anti-cheat route. The capture host keeps its own normal driver profile.
#include "capture_engine.h"
#include <winrt/base.h>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <thread>
#pragma comment(lib, "gdi32.lib")

static LRESULT CALLBACK WindowProc(HWND hwnd, UINT message, WPARAM w, LPARAM l) {
    if (message == WM_APP + 1) {
        // Let the foreground test process hand focus to the other test window.
        // A background process cannot reliably call SetForegroundWindow itself.
        return SetForegroundWindow(reinterpret_cast<HWND>(w));
    }
    if (message == WM_PAINT) {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        RECT bounds{};
        GetClientRect(hwnd, &bounds);
        const auto shade = static_cast<BYTE>(GetTickCount64() / 20 % 180 + 40);
        HBRUSH brush = CreateSolidBrush(RGB(20, shade, 110));
        FillRect(dc, &bounds, brush);
        DeleteObject(brush);
        EndPaint(hwnd, &ps);
        return 0;
    }
    return DefWindowProcW(hwnd, message, w, l);
}

static void Pump(HWND animated, int milliseconds) {
    const auto end = std::chrono::steady_clock::now()
        + std::chrono::milliseconds(milliseconds);
    while (std::chrono::steady_clock::now() < end) {
        MSG msg{};
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        InvalidateRect(animated, nullptr, FALSE);
        UpdateWindow(animated);
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
    }
}

int wmain(int argc, wchar_t** argv) {
    if (argc != 3 && argc != 2) return 2;
    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    HWND previous_foreground = GetForegroundWindow();
    WNDCLASSW wc{};
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"FthrFocusSmoke";
    RegisterClassW(&wc);
    if (argc == 2 && std::wstring(argv[1]) == L"--source") {
        HWND source = CreateWindowW(wc.lpszClassName, L"FTHR capture recovery test",
            WS_OVERLAPPEDWINDOW, 60, 60, 420, 240, nullptr, nullptr, wc.hInstance, nullptr);
        if (!source) return 3;
        ShowWindow(source, SW_SHOW);
        while (IsWindow(source)) Pump(source, 30);
        return 0;
    }
    if (argc != 3) return 2;
    HWND source = nullptr;
    for (int tick = 0; !source && tick < 100; ++tick) {
        source = FindWindowW(wc.lpszClassName, L"FTHR capture recovery test");
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }
    HWND other = CreateWindowW(wc.lpszClassName, L"FTHR background transition test",
        WS_OVERLAPPEDWINDOW, 510, 60, 420, 240, nullptr, nullptr, wc.hInstance, nullptr);
    if (!source || !other) return 3;
    ShowWindow(source, SW_SHOW);
    ShowWindow(other, SW_SHOWNOACTIVATE);
    SetForegroundWindow(source);
    Pump(source, 300);
    fthr::CaptureConfig config;
    config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
    config.target_hwnd = reinterpret_cast<uintptr_t>(source);
    config.monitor_device_path = argv[1];
    config.encoder_preference = fthr::EncoderPreference::Nvidia;
    config.buffer_seconds = 32;
    config.audio_enabled = true;
    fthr::CaptureEngine engine;
    int result = 0;
    if (!engine.Initialize(config)) result = 4;
    if (!result) Pump(source, 3000);
    for (int cycle = 1; !result && cycle <= 3; ++cycle) {
        const auto generation = engine.GetCaptureGeneration();
        SendMessageW(source, WM_APP + 1, reinterpret_cast<WPARAM>(other), 0);
        Pump(source, 250); // Allow the last in-flight foreground frame to finish.
        const auto paused_frames = engine.GetFrameCount();
        Pump(source, 4000);  // Longer than the native watchdog's stall limit.
        auto flags = engine.GetCaptureHealthFlags();
        if (GetForegroundWindow() != other
            || !(flags & fthr::CAPTURE_HEALTH_PAUSED)
            || engine.GetFrameCount() != paused_frames
            || engine.GetCaptureGeneration() != generation
            || (flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED)) {
            std::cerr << "FOCUS pause failed flags=" << flags
                      << " expected_foreground=" << other
                      << " actual_foreground=" << GetForegroundWindow()
                      << " frames_before=" << paused_frames
                      << " frames_after=" << engine.GetFrameCount()
                      << " generation_before=" << generation
                      << " generation_after=" << engine.GetCaptureGeneration() << std::endl;
            result = 5;
            break;
        }
        const auto background_frames = engine.GetFrameCount();
        SendMessageW(other, WM_APP + 1, reinterpret_cast<WPARAM>(source), 0);
        Pump(source, 1200);
        if (GetForegroundWindow() != source || engine.GetFrameCount() <= background_frames
            || engine.GetCaptureGeneration() != generation) {
            std::cerr << "FOCUS replay-preserving resume failed" << std::endl;
            result = 6;
            break;
        }
        const auto clip = std::filesystem::path(argv[2])
            / (L"focus-" + std::to_wstring(cycle) + L".mp4");
        fthr::SharedMemoryLayout response{};
        if (!engine.SaveClip(clip.c_str(), 30, &response)) {
            std::wcerr << L"FOCUS save rejected: " << response.engine_string << std::endl;
            result = 7;
            break;
        }
        for (int tick = 0; tick < 100 && response.engine_response == fthr::ResponseType::NONE; ++tick)
            Pump(source, 50);
        if (response.engine_response != fthr::ResponseType::CLIP_SAVED) {
            result = 8;
            break;
        }
        std::cout << "FOCUS cycle=" << cycle << " saved=true generation="
                  << engine.GetCaptureGeneration() << std::endl;
    }
    engine.Shutdown();
    DestroyWindow(other);
    PostMessageW(source, WM_CLOSE, 0, 0);
    if (IsWindow(previous_foreground)) SetForegroundWindow(previous_foreground);
    return result;
}
