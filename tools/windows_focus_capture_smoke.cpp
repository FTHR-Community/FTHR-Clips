// The source process is named valorant.exe to exercise protected WGC routing.
#include "capture_engine.h"

#include <winrt/base.h>
#include <mmsystem.h>

#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <filesystem>
#include <iostream>
#include <string_view>
#include <thread>

#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "winmm.lib")

namespace {
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr wchar_t kWindowClass[] = L"FthrFocusSmoke";
constexpr UINT kFocus = WM_APP + 1;
constexpr UINT kScene = WM_APP + 2;
enum Scene : LONG_PTR { Before = 1, After, OffFocus };

struct Tone {
    uint32_t riff = 0x46464952;
    uint32_t riff_size = 36 + 2 * 4800;
    uint32_t wave = 0x45564157;
    uint32_t fmt = 0x20746d66;
    uint32_t fmt_size = 16;
    uint16_t format = 1;
    uint16_t channels = 1;
    uint32_t sample_rate = 48000;
    uint32_t byte_rate = 96000;
    uint16_t block_align = 2;
    uint16_t bits = 16;
    uint32_t data = 0x61746164;
    uint32_t data_size = 2 * 4800;
    std::array<int16_t, 4800> samples{};
};
static_assert(offsetof(Tone, samples) == 44);

std::wstring SourceTitle(DWORD pid) {
    return L"FTHR synthetic focus source " + std::to_wstring(pid);
}

void Fill(HDC dc, const RECT& bounds, COLORREF color) {
    const auto brush = CreateSolidBrush(color);
    FillRect(dc, &bounds, brush);
    DeleteObject(brush);
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case kFocus:
        return SetForegroundWindow(reinterpret_cast<HWND>(wparam));
    case kScene:
        SetWindowLongPtrW(window, GWLP_USERDATA, static_cast<LONG_PTR>(wparam));
        InvalidateRect(window, nullptr, FALSE);
        UpdateWindow(window);
        return TRUE;
    case WM_TIMER:
        InvalidateRect(window, nullptr, FALSE);
        return 0;
    case WM_PAINT: {
        PAINTSTRUCT paint{};
        const HDC dc = BeginPaint(window, &paint);
        RECT bounds{};
        GetClientRect(window, &bounds);
        const auto scene = GetWindowLongPtrW(window, GWLP_USERDATA);
        const auto color = scene == Before ? RGB(203, 26, 26)
            : scene == After ? RGB(26, 205, 26) : RGB(255, 0, 255);
        Fill(dc, bounds, color);
        if (scene != OffFocus) {
            const int x = bounds.right / 8
                + static_cast<int>(GetTickCount64() / 15 % 64) * bounds.right / 96;
            Fill(dc, RECT{x, bounds.bottom / 3, x + bounds.right / 10,
                         bounds.bottom / 2}, RGB(246, 246, 246));
        }
        EndPaint(window, &paint);
        return 0;
    }
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

HWND CreateSceneWindow(const wchar_t* title, Scene scene, const RECT& monitor) {
    const HWND window = CreateWindowExW(WS_EX_TOPMOST, kWindowClass, title, WS_POPUP,
        monitor.left, monitor.top, monitor.right - monitor.left, monitor.bottom - monitor.top,
        nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (window) SetWindowLongPtrW(window, GWLP_USERDATA, scene);
    return window;
}

bool Pump(HWND foreground, std::chrono::milliseconds duration) {
    RECT bounds{};
    if (!GetWindowRect(foreground, &bounds)) return false;
    const POINT center{(bounds.left + bounds.right) / 2, (bounds.top + bounds.bottom) / 2};
    const auto end = Clock::now() + duration;
    while (Clock::now() < end) {
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            DispatchMessageW(&message);
        }
        if (GetForegroundWindow() != foreground || WindowFromPoint(center) != foreground) {
            std::cerr << "FOCUS_ERROR stage=unexpected_foreground expected=" << foreground
                      << " actual=" << GetForegroundWindow()
                      << " visible_at_center=" << WindowFromPoint(center) << std::endl;
            return false;
        }
        std::this_thread::sleep_for(15ms);
    }
    return true;
}

bool Request(HWND window, UINT message, WPARAM value) {
    DWORD_PTR result = 0;
    return SendMessageTimeoutW(window, message, value, 0, SMTO_ABORTIFHUNG, 1500, &result)
        && result;
}

int RunSource(const RECT& monitor, bool audio) {
    const auto title = SourceTitle(GetCurrentProcessId());
    const HWND source = CreateSceneWindow(title.c_str(), Before, monitor);
    if (!source) return 3;
    ShowWindow(source, SW_SHOW);
    SetForegroundWindow(source);
    SetTimer(source, 1, 15, nullptr);
    Tone tone;
    if (audio) {
        for (size_t index = 0; index < tone.samples.size(); ++index) {
            tone.samples[index] = static_cast<int16_t>(1500 * std::sin(
                6.28318530718 * 440 * index / tone.sample_rate));
        }
        if (!PlaySoundW(reinterpret_cast<LPCWSTR>(&tone), nullptr,
                        SND_MEMORY | SND_ASYNC | SND_LOOP | SND_NODEFAULT)) return 3;
    }
    while (IsWindow(source)) {
        MSG message{};
        if (GetMessageW(&message, nullptr, 0, 0) <= 0) break;
        DispatchMessageW(&message);
    }
    if (audio) PlaySoundW(nullptr, nullptr, 0);
    return 0;
}

int RunCapture(const wchar_t* monitor_path, const RECT& monitor, HWND source,
               const std::filesystem::path& output, bool audio) {
    const HWND other = CreateSceneWindow(L"FTHR synthetic off-focus marker", OffFocus, monitor);
    if (!other) return 3;
    // Show the decoy behind the source. Moving the decoy to the foreground
    // raises it above the source in the topmost band.
    if (!SetWindowPos(other, source, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        || !Pump(source, 250ms)) return 3;

    fthr::SharedMemoryLayout response{};
    fthr::CaptureConfig config;
    config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
    config.target_hwnd = reinterpret_cast<uintptr_t>(source);
    config.monitor_device_path = monitor_path;
    config.encoder_preference = fthr::EncoderPreference::Auto;
    config.capture_cursor = false;
    config.buffer_seconds = 32;
    config.audio_enabled = audio;
    config.microphone_endpoint_id = L"FTHR synthetic smoke microphone disabled";
    config.allow_replay_gaps = false;
    fthr::CaptureEngine engine;
    if (!engine.Initialize(config) || engine.GetStartupCaptureBackend() != "WGC_MONITOR") return 4;
    const auto original_generation = engine.GetCaptureGeneration();
    if (!Pump(source, 4000ms) || !Request(source, kFocus, reinterpret_cast<WPARAM>(other))
        || !Pump(other, 250ms)) return 5;

    const auto paused_frames = engine.GetFrameCount();
    if (!Request(source, kScene, After) || !Pump(other, 4000ms)) return 5;
    const auto flags = engine.GetCaptureHealthFlags();
    if (!(flags & fthr::CAPTURE_HEALTH_PAUSED) || (flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED)
        || engine.GetFrameCount() != paused_frames
        || engine.GetCaptureGeneration() != original_generation) {
        std::cerr << "FOCUS_ERROR stage=pause flags=" << flags << std::endl;
        return 5;
    }

    const auto expected_generation = original_generation + (audio ? 1 : 0);
    if (!Request(other, kFocus, reinterpret_cast<WPARAM>(source)) || !Pump(source, 3500ms)
        || engine.GetFrameCount() <= paused_frames
        || engine.GetCaptureGeneration() != expected_generation
        || (engine.GetCaptureHealthFlags() & fthr::CAPTURE_HEALTH_BACKEND_FAILED)) {
        std::cerr << "FOCUS_ERROR stage=resume expected_generation=" << expected_generation
                  << " actual_generation=" << engine.GetCaptureGeneration() << std::endl;
        return 6;
    }

    const auto clip = output / (audio ? L"focus-audio.mp4" : L"focus-video.mp4");
    if (!engine.SaveClip(clip.c_str(), 30, &response)) {
        std::wcerr << L"FOCUS_ERROR stage=save_rejected detail=" << response.engine_string << std::endl;
        return 7;
    }
    for (int tick = 0; tick < 100 && response.engine_response == fthr::ResponseType::NONE; ++tick) {
        if (!Pump(source, 50ms)) return 8;
    }
    if (response.engine_response != fthr::ResponseType::CLIP_SAVED) return 8;
    std::cout << "FOCUS saved=true audio=" << audio << " generation_before=" << original_generation
              << " generation_after=" << engine.GetCaptureGeneration() << std::endl;
    engine.Shutdown();
    DestroyWindow(other);
    return 0;
}

} // namespace

int wmain(int argc, wchar_t** argv) {
    const bool is_source = argc == 4 && std::wstring_view(argv[1]) == L"--source";
    if (!is_source && argc != 5) return 2;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    WNDCLASSW window_class{};
    window_class.lpfnWndProc = WindowProc;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpszClassName = kWindowClass;
    if (!RegisterClassW(&window_class)) return 2;

    const auto monitor_path = argv[is_source ? 2 : 1];
    fthr::monitor::WindowsMonitorTopologySource topology;
    const auto selection = fthr::monitor::MonitorResolver(topology).Resolve(monitor_path);
    if (!selection.ok()) return 2;
    MONITORINFO info{sizeof(info)};
    if (!GetMonitorInfoW(reinterpret_cast<HMONITOR>(selection.monitor.hmonitor), &info)) return 2;
    const bool audio = std::wstring_view(argv[argc - 1]) == L"audio";
    if (is_source) return RunSource(info.rcMonitor, audio);

    const DWORD pid = wcstoul(argv[3], nullptr, 10);
    const auto title = SourceTitle(pid);
    const auto deadline = Clock::now() + 10s;
    HWND source = nullptr;
    while (Clock::now() < deadline) {
        source = FindWindowW(kWindowClass, title.c_str());
        DWORD owner = 0;
        RECT bounds{};
        if (source && GetWindowThreadProcessId(source, &owner) && owner == pid
            && GetForegroundWindow() == source && GetWindowRect(source, &bounds)
            && EqualRect(&bounds, &info.rcMonitor)) {
            const int result = RunCapture(monitor_path, info.rcMonitor, source, argv[2], audio);
            PostMessageW(source, WM_CLOSE, 0, 0);
            return result;
        }
        std::this_thread::sleep_for(25ms);
    }
    std::cerr << "FOCUS_ERROR stage=fullscreen_source source=" << source
              << " foreground=" << GetForegroundWindow() << std::endl;
    return 3;
}
