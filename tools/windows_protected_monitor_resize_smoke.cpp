// Opt-in physical mode changes with a synthetic foreground source. The
// source alone is named valorant.exe to exercise production protected routing.
#include "capture_engine.h"

#include <winrt/base.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <mmsystem.h>
#include <wrl/client.h>

#include <array>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <string_view>
#include <thread>
#include <vector>

#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "winmm.lib")

namespace {
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr wchar_t kSourceClass[] = L"FthrProtectedMonitorResizeSmoke";
constexpr UINT kSceneMessage = WM_APP + 1;
constexpr UINT kPrepareSceneMessage = WM_APP + 2;
constexpr std::array<COLORREF, 4> kColors{
    RGB(32, 32, 32), RGB(208, 32, 32), RGB(32, 208, 32), RGB(32, 32, 208)};
constexpr std::array<int, 3> kToneHz{440, 660, 880};
constexpr double kTau = 6.28318530718;

uint64_t Qpc100ns() {
    LARGE_INTEGER ticks{}, frequency{};
    QueryPerformanceCounter(&ticks);
    QueryPerformanceFrequency(&frequency);
    const int64_t seconds = ticks.QuadPart / frequency.QuadPart;
    const int64_t remainder = ticks.QuadPart % frequency.QuadPart;
    return static_cast<uint64_t>(seconds * 10'000'000 + remainder * 10'000'000 / frequency.QuadPart);
}

class TonePlayer {
public:
    ~TonePlayer() {
        if (!output_) return;
        waveOutReset(output_);
        for (auto& chunk : chunks_) {
            if (chunk.header.dwFlags & WHDR_PREPARED) {
                waveOutUnprepareHeader(output_, &chunk.header, sizeof(chunk.header));
            }
        }
        waveOutClose(output_);
        timeEndPeriod(1);
    }

    bool Start() {
        WAVEFORMATEX format{};
        format.wFormatTag = WAVE_FORMAT_PCM;
        format.nChannels = 1;
        format.nSamplesPerSec = kSampleRate;
        format.nAvgBytesPerSec = kSampleRate * sizeof(int16_t);
        format.nBlockAlign = sizeof(int16_t);
        format.wBitsPerSample = 16;
        if (waveOutOpen(&output_, WAVE_MAPPER, &format, 0, 0, CALLBACK_NULL) != MMSYSERR_NOERROR) {
            return false;
        }
        timeBeginPeriod(1);
        for (auto& chunk : chunks_) {
            chunk.header.lpData = reinterpret_cast<LPSTR>(chunk.samples.data());
            chunk.header.dwBufferLength = sizeof(chunk.samples);
            if (waveOutPrepareHeader(output_, &chunk.header, sizeof(chunk.header)) != MMSYSERR_NOERROR) {
                return false;
            }
        }
        return Pump();
    }

    uint64_t Schedule(int frequency) {
        tone_hz_ = frequency;
        tone_start_ = queued_frames_;
        return tone_start_;
    }

    bool Pump() {
        while (!(chunks_[next_chunk_].header.dwFlags & WHDR_INQUEUE)) {
            auto& chunk = chunks_[next_chunk_];
            for (size_t i = 0; i < chunk.samples.size(); ++i) {
                const uint64_t sample = queued_frames_ + i;
                const double offset = static_cast<double>(sample - tone_start_);
                chunk.samples[i] = tone_hz_ != 0 && sample >= tone_start_ && offset < kSampleRate / 2
                    ? static_cast<int16_t>(8000 * std::sin(kTau * tone_hz_ * offset / kSampleRate))
                    : 0;
            }
            if (waveOutWrite(output_, &chunk.header, sizeof(chunk.header)) != MMSYSERR_NOERROR) return false;
            queued_frames_ += chunk.samples.size();
            next_chunk_ = (next_chunk_ + 1) % chunks_.size();
        }
        return true;
    }

private:
    static constexpr DWORD kSampleRate = 48000;
    struct Chunk {
        std::array<int16_t, kSampleRate / 100> samples{};
        WAVEHDR header{};
    };
    std::array<Chunk, 8> chunks_{};
    HWAVEOUT output_ = nullptr;
    size_t next_chunk_ = 0;
    uint64_t queued_frames_ = 0;
    uint64_t tone_start_ = 0;
    int tone_hz_ = 0;
};

struct ToneReference {
    uint64_t sample_qpc_100ns = 0;
    uint64_t confirmed_qpc_100ns = 0;
    double packet_ms = 0;
};

// Listen independently of CaptureEngine. The playback endpoint can timestamp a
// loopback sample for presentation long after waveOut reports its queue position.
// Analyze only the requested tone in-place and release every packet immediately.
class LoopbackToneClock {
public:
    bool Start() {
        using Microsoft::WRL::ComPtr;
        ComPtr<IMMDeviceEnumerator> enumerator;
        ComPtr<IMMDevice> endpoint;
        WAVEFORMATEX* raw_format = nullptr;
        if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                    IID_PPV_ARGS(&enumerator)))
            || FAILED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &endpoint))
            || FAILED(endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                    reinterpret_cast<void**>(client_.GetAddressOf())))
            || FAILED(client_->GetMixFormat(&raw_format))) return false;
        std::unique_ptr<WAVEFORMATEX, decltype(&CoTaskMemFree)> format(raw_format, &CoTaskMemFree);
        const WORD sample_format = format->wFormatTag != WAVE_FORMAT_EXTENSIBLE ? format->wFormatTag
            : static_cast<WORD>(reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(format.get())->SubFormat.Data1);
        if (sample_format == WAVE_FORMAT_IEEE_FLOAT && format->wBitsPerSample == 32) floating_ = true;
        else if (sample_format != WAVE_FORMAT_PCM || format->wBitsPerSample != 16) return false;
        channels_ = format->nChannels;
        sample_rate_ = format->nSamplesPerSec;
        return channels_ != 0 && sample_rate_ != 0
            && SUCCEEDED(client_->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                1'000'000, 0, format.get(), nullptr))
            && SUCCEEDED(client_->GetService(IID_PPV_ARGS(&packets_)))
            && SUCCEEDED(client_->Start());
    }

    void Watch(int frequency) {
        frequency_ = frequency;
        matching_packets_ = 0;
    }

    bool Read(std::optional<ToneReference>& reference) {
        for (;;) {
            UINT32 available = 0;
            if (FAILED(packets_->GetNextPacketSize(&available))) return false;
            if (available == 0) return true;
            BYTE* data = nullptr;
            DWORD flags = 0;
            UINT32 frames = 0;
            UINT64 qpc = 0;
            if (FAILED(packets_->GetBuffer(&data, &frames, &flags, nullptr, &qpc))) return false;
            if (frequency_ != 0) {
                if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) matching_packets_ = 0;
                if (!(flags & (AUDCLNT_BUFFERFLAGS_SILENT | AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR))
                    && qpc > 0 && Matches(data, frames)) {
                    // Three matching packets distinguish a sustained test tone
                    // from noise. Keep the first packet's presentation time.
                    if (matching_packets_++ == 0) {
                        first_match_ = {qpc, 0, 1000.0 * frames / sample_rate_};
                    }
                    if (matching_packets_ == 3) {
                        first_match_.confirmed_qpc_100ns = Qpc100ns();
                        reference = first_match_;
                        frequency_ = 0;
                    }
                } else {
                    matching_packets_ = 0;
                }
            }
            if (FAILED(packets_->ReleaseBuffer(frames))) return false;
        }
    }

private:
    bool Matches(const BYTE* data, uint32_t frames) const {
        double real = 0, imaginary = 0, energy = 0;
        for (uint32_t frame = 0; frame < frames; ++frame) {
            const size_t index = static_cast<size_t>(frame) * channels_;
            const double value = floating_ ? reinterpret_cast<const float*>(data)[index]
                : reinterpret_cast<const int16_t*>(data)[index] / 32768.0;
            const double angle = kTau * frequency_ * frame / sample_rate_;
            real += value * std::cos(angle);
            imaginary += value * std::sin(angle);
            energy += value * value;
        }
        return energy > 0.0001 * frames && 2 * (real * real + imaginary * imaginary) > 0.72 * frames * energy;
    }

    Microsoft::WRL::ComPtr<IAudioClient> client_;
    Microsoft::WRL::ComPtr<IAudioCaptureClient> packets_;
    bool floating_ = false;
    uint32_t channels_ = 0;
    uint32_t sample_rate_ = 0;
    int frequency_ = 0;
    int matching_packets_ = 0;
    ToneReference first_match_;
};

struct Phase {
    size_t color = 0;
    int width = 1920;
    int height = 1080;
};

struct Scene {
    Phase visible;
    Phase pending;
    bool audio = false;
    std::optional<ToneReference> reference;
    uint64_t pending_since_100ns = 0;
    uint64_t scheduled_sample = 0;
    TonePlayer tone;
    LoopbackToneClock tone_clock;
};

std::wstring SourceTitle(DWORD pid) {
    return L"FTHR protected monitor resize smoke " + std::to_wstring(pid);
}

std::wstring MonitorSelection(const fthr::monitor::MonitorTopologyEntry& monitor) {
    if (monitor.monitor_device_path.empty()) return fthr::monitor::DesktopMonitorIdentity(monitor);
    return monitor.monitor_device_path;
}

void Fill(HDC dc, const RECT& rectangle, COLORREF color) {
    const HBRUSH brush = CreateSolidBrush(color);
    FillRect(dc, &rectangle, brush);
    DeleteObject(brush);
}

LRESULT CALLBACK SourceProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* scene = reinterpret_cast<Scene*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    switch (message) {
    case WM_NCCREATE:
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(
            reinterpret_cast<const CREATESTRUCTW*>(lparam)->lpCreateParams));
        return DefWindowProcW(window, message, wparam, lparam);
    case kPrepareSceneMessage:
        if (!scene || scene->pending_since_100ns != 0) return FALSE;
        scene->visible = {0, LOWORD(lparam), HIWORD(lparam)};
        InvalidateRect(window, nullptr, FALSE);
        UpdateWindow(window);
        return TRUE;
    case kSceneMessage:
        if (!scene || wparam == 0 || wparam >= kColors.size()) return FALSE;
        scene->pending = {wparam, LOWORD(lparam), HIWORD(lparam)};
        if (scene->audio) {
            if (scene->pending_since_100ns != 0) return FALSE;
            const int frequency = kToneHz[wparam - 1];
            scene->pending_since_100ns = Qpc100ns();
            scene->scheduled_sample = scene->tone.Schedule(frequency);
            scene->tone_clock.Watch(frequency);
            return TRUE;
        }
        scene->visible = scene->pending;
        InvalidateRect(window, nullptr, FALSE);
        UpdateWindow(window);
        return TRUE;
    case WM_TIMER:
        if (scene->audio && (!scene->tone.Pump() || !scene->tone_clock.Read(scene->reference))) {
            std::cerr << "RESIZE_SOURCE_ERROR playback or independent WASAPI loopback read" << std::endl;
            DestroyWindow(window);
            return 0;
        }
        if (scene->pending_since_100ns) {
            const auto now = Qpc100ns();
            if (now - scene->pending_since_100ns > 15'000'000) {
                std::cerr << "RESIZE_SOURCE_ERROR no timely WASAPI tone presentation timestamp" << std::endl;
                DestroyWindow(window);
                return 0;
            }
            if (scene->reference && now >= scene->reference->sample_qpc_100ns) {
                scene->visible = scene->pending;
                InvalidateRect(window, nullptr, FALSE);
                UpdateWindow(window);
                const auto painted = Qpc100ns();
                const auto& reference = *scene->reference;
                std::cout << "RESIZE_TONE scene=" << scene->visible.color
                          << " scheduled_sample=" << scene->scheduled_sample
                          << " estimated_onset_qpc_100ns=" << reference.sample_qpc_100ns
                          << " confirmed_qpc_100ns=" << reference.confirmed_qpc_100ns
                          << " paint_issued_qpc_100ns=" << now
                          << " paint_completed_qpc_100ns=" << painted
                          << " onset_packet_span_ms=" << reference.packet_ms
                          << " paint_after_estimate_ms=" << (now - reference.sample_qpc_100ns) / 10000.0
                          << std::endl;
                scene->reference.reset();
                scene->pending_since_100ns = 0;
                return 0;
            }
        }
        InvalidateRect(window, nullptr, FALSE);
        UpdateWindow(window);
        return 0;
    case WM_PAINT: {
        PAINTSTRUCT paint{};
        const HDC dc = BeginPaint(window, &paint);
        RECT bounds{};
        GetClientRect(window, &bounds);
        Fill(dc, bounds, kColors[scene->visible.color]);
        const int width = scene->visible.width;
        const int height = scene->visible.height;
        const int box_x = width / 8 + static_cast<int>(GetTickCount64() / 10 % 64) * width / 96;
        const COLORREF white = RGB(248, 248, 248);
        Fill(dc, RECT{box_x, height * 4 / 9, box_x + width / 12, height * 5 / 9}, white);
        // Four fixed anchors make incorrect crop or a partial 900p blit visible.
        for (const int x : {width / 20, width * 18 / 20}) {
            for (const int y : {height / 20, height * 18 / 20}) {
                Fill(dc, RECT{x, y, x + width / 20, y + height / 20}, white);
            }
        }
        EndPaint(window, &paint);
        return 0;
    }
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

int RunSource(int left, int top, bool audio) {
    Scene scene;
    scene.audio = audio;
    if (audio && (!scene.tone_clock.Start() || !scene.tone.Start())) {
        std::cerr << "RESIZE_SOURCE_ERROR default WASAPI loopback or waveOut startup" << std::endl;
        return 2;
    }
    WNDCLASSW window_class{};
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpfnWndProc = SourceProc;
    window_class.lpszClassName = kSourceClass;
    if (!RegisterClassW(&window_class)) return 2;
    const HWND window = CreateWindowExW(WS_EX_TOPMOST, kSourceClass,
        SourceTitle(GetCurrentProcessId()).c_str(), WS_POPUP,
        left, top, 1920, 1080, nullptr, nullptr, window_class.hInstance, &scene);
    if (!window) return 2;
    ShowWindow(window, SW_SHOW);
    const bool activated = SetForegroundWindow(window) != FALSE;
    RECT bounds{};
    GetWindowRect(window, &bounds);
    std::cout << "RESIZE_SOURCE pid=" << GetCurrentProcessId() << " window=" << window
              << " activated=" << activated << " foreground=" << GetForegroundWindow()
              << " bounds=" << bounds.left << ',' << bounds.top << ',' << bounds.right
              << ',' << bounds.bottom << std::endl;
    SetTimer(window, 1, 10, nullptr);
    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) DispatchMessageW(&message);
    return 0;
}

struct DisplayMode {
    wchar_t device[CCHDEVICENAME]{};
    DEVMODEW mode{};
};

bool QueryMode(const wchar_t* device, DWORD index, DEVMODEW& mode) {
    mode = {};
    mode.dmSize = sizeof(mode);
    return EnumDisplaySettingsExW(device, index, &mode, 0) != FALSE;
}

bool SameMode(const DEVMODEW& left, const DEVMODEW& right) {
    return left.dmPelsWidth == right.dmPelsWidth && left.dmPelsHeight == right.dmPelsHeight
        && left.dmDisplayFrequency == right.dmDisplayFrequency
        && left.dmBitsPerPel == right.dmBitsPerPel
        && left.dmDisplayOrientation == right.dmDisplayOrientation
        && left.dmDisplayFlags == right.dmDisplayFlags
        && left.dmPosition.x == right.dmPosition.x && left.dmPosition.y == right.dmPosition.y;
}

bool ApplyMode(const DisplayMode& display, const char* stage) {
    DEVMODEW current{};
    if (QueryMode(display.device, ENUM_CURRENT_SETTINGS, current)
        && SameMode(current, display.mode)) return true;
    DEVMODEW requested = display.mode;
    const LONG result = ChangeDisplaySettingsExW(display.device, &requested,
        nullptr, CDS_FULLSCREEN, nullptr);
    if (result != DISP_CHANGE_SUCCESSFUL
        || !QueryMode(display.device, ENUM_CURRENT_SETTINGS, current)
        || !SameMode(current, display.mode)) {
        std::cerr << "RESIZE_ERROR stage=" << stage << " ChangeDisplaySettingsExW="
                  << result << " actual=" << current.dmPelsWidth << 'x' << current.dmPelsHeight
                  << '@' << current.dmDisplayFrequency << std::endl;
        return false;
    }
    return true;
}

bool Find900p(const DisplayMode& original, DisplayMode& smaller) {
    smaller = original;
    smaller.mode = {};
    DEVMODEW candidate{};
    for (DWORD index = 0; QueryMode(original.device, index, candidate); ++index) {
        if (candidate.dmPelsWidth != 1600 || candidate.dmPelsHeight != 900
            || candidate.dmBitsPerPel != original.mode.dmBitsPerPel
            || candidate.dmDisplayOrientation != original.mode.dmDisplayOrientation
            || candidate.dmDisplayFlags != original.mode.dmDisplayFlags) continue;
        candidate.dmPosition = original.mode.dmPosition;
        if (ChangeDisplaySettingsExW(original.device, &candidate, nullptr,
            CDS_TEST, nullptr) != DISP_CHANGE_SUCCESSFUL) continue;
        if (candidate.dmDisplayFrequency == original.mode.dmDisplayFrequency) {
            smaller.mode = candidate;
            return true;
        }
        if (candidate.dmDisplayFrequency > smaller.mode.dmDisplayFrequency) {
            smaller.mode = candidate;
        }
    }
    return smaller.mode.dmPelsWidth != 0;
}

struct SourceWindow {
    DWORD pid;
    HWND window = nullptr;
};

BOOL CALLBACK FindSourceWindow(HWND window, LPARAM context) {
    auto& source = *reinterpret_cast<SourceWindow*>(context);
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    if (pid != source.pid) return TRUE;
    wchar_t window_class[64]{};
    GetClassNameW(window, window_class, static_cast<int>(std::size(window_class)));
    if (std::wstring_view(window_class) != kSourceClass) return TRUE;
    source.window = window;
    return FALSE;
}

bool HoldScene(HWND source, fthr::CaptureEngine& engine, uint32_t generation,
               std::chrono::milliseconds duration) {
    const auto beginning = engine.GetFrameCount();
    const auto deadline = Clock::now() + duration;
    while (Clock::now() < deadline) {
        const auto flags = engine.GetCaptureHealthFlags();
        if (GetForegroundWindow() != source || engine.GetCaptureGeneration() != generation
            || (flags & (fthr::CAPTURE_HEALTH_BACKEND_FAILED | fthr::CAPTURE_HEALTH_PAUSED
                        | fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED))) {
            std::cerr << "RESIZE_ERROR stage=capture flags=" << flags
                      << " generation=" << engine.GetCaptureGeneration()
                      << " foreground_match=" << (GetForegroundWindow() == source) << std::endl;
            return false;
        }
        std::this_thread::sleep_for(20ms);
    }
    return engine.GetFrameCount() > beginning;
}

bool Save(fthr::CaptureEngine& engine, HWND source, const std::filesystem::path& path,
          uint32_t seconds, fthr::SharedMemoryLayout& response) {
    if (GetForegroundWindow() != source || !engine.SaveClip(path.c_str(), seconds, &response)) {
        std::wcerr << L"RESIZE_ERROR stage=save_admission detail=" << response.engine_string << std::endl;
        return false;
    }
    const auto deadline = Clock::now() + 15s;
    while (Clock::now() < deadline && GetForegroundWindow() == source
           && response.engine_response == fthr::ResponseType::NONE) {
        std::this_thread::sleep_for(20ms);
    }
    if (response.engine_response != fthr::ResponseType::CLIP_SAVED) {
        std::wcerr << L"RESIZE_ERROR stage=save_completion detail=" << response.engine_string << std::endl;
        return false;
    }
    std::wcout << L"RESIZE_SAVED file=" << path.filename().c_str() << std::endl;
    return true;
}

int Capture(const fthr::monitor::MonitorTopologyEntry& monitor, HWND source,
            const DisplayMode& original, const DisplayMode& smaller,
            const std::filesystem::path& output, bool audio, bool fixed, bool start_900p,
            bool reporter_length, fthr::EncoderPreference encoder) {
    if (start_900p && (GetForegroundWindow() != source || !ApplyMode(smaller, "startup_900p"))) {
        return 3;
    }
    // Responses outlive the engine so every failure path drains pending saves.
    fthr::SharedMemoryLayout down{}, round_trip{};
    fthr::CaptureConfig config;
    config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
    config.target_hwnd = reinterpret_cast<uintptr_t>(source);
    config.monitor_device_path = MonitorSelection(monitor);
    config.encoder_preference = encoder;
    config.bitrate_kbps = 25000;
    config.capture_cursor = false;
    config.buffer_seconds = reporter_length ? 47 : 25;
    const uint32_t save_seconds = reporter_length ? 45 : 20;
    config.audio_enabled = audio;
    // An exact non-existent endpoint prevents opening the default microphone.
    config.microphone_endpoint_id = L"FTHR synthetic smoke microphone disabled";
    config.allow_replay_gaps = false;
    config.allow_low_fps_clips = false;
    if (fixed) {
        config.target_width = 1280;
        config.target_height = 720;
    }
    fthr::CaptureEngine engine;
    if (!engine.Initialize(config) || engine.GetStartupCaptureBackend() != "WGC_MONITOR") {
        std::cerr << "RESIZE_ERROR stage=initialize backend=" << engine.GetStartupCaptureBackend()
                  << " detail=" << engine.GetLastStartupError() << std::endl;
        return 4;
    }
    const uint32_t generation = engine.GetCaptureGeneration();
    std::cout << "RESIZE_START backend=" << engine.GetStartupCaptureBackend()
              << " encoder=" << engine.GetActiveEncoderName() << " generation=" << generation
              << " allow_replay_gaps=false allow_low_fps_clips=false buffer_seconds="
              << config.buffer_seconds << " save_seconds=" << save_seconds << std::endl;
    if (!HoldScene(source, engine, generation, reporter_length ? 51000ms : 2200ms)) return 5;
    const auto displays = start_900p
        ? std::vector{&smaller, &original} : std::vector{&original, &smaller, &original};
    for (size_t index = 0; index < displays.size(); ++index) {
        const auto& display = *displays[index];
        if (GetForegroundWindow() != source || !ApplyMode(display, "switch")) return 5;
        const LPARAM size = MAKELPARAM(display.mode.dmPelsWidth, display.mode.dmPelsHeight);
        DWORD_PTR prepared = FALSE;
        if (!SendMessageTimeoutW(source, kPrepareSceneMessage, 0, size,
                SMTO_ABORTIFHUNG, 1000, &prepared) || !prepared) return 5;
        // Establish fresh moving source imagery in the new display mode before
        // requesting the synchronized test marker. The runner checks that WGC
        // adoption is logged before this marker and gates the whole strict save.
        if (index > 0 && !HoldScene(source, engine, generation, 500ms)) return 5;
        std::cout << "RESIZE_MARKER_REQUEST stage=" << index + 1 << std::endl;
        DWORD_PTR painted = FALSE;
        if (!SendMessageTimeoutW(source, kSceneMessage, index + 1, size,
                SMTO_ABORTIFHUNG, 1000, &painted) || !painted) return 5;
        if (!HoldScene(source, engine, generation, 2400ms)) return 5;
        const auto stats = engine.GetStats();
        std::cout << "RESIZE_PHASE stage=" << index + 1 << " display=" << display.mode.dmPelsWidth
                  << 'x' << display.mode.dmPelsHeight << '@' << display.mode.dmDisplayFrequency
                  << " frames=" << stats.frames_captured << " packets=" << stats.ring_used_frames
                  << " generation=" << generation << std::endl;
        if (!start_900p && index == 1
            && !Save(engine, source, output / L"downshift.mp4", save_seconds, down)) {
            return 6;
        }
    }
    const auto filename = start_900p ? L"growth.mp4" : L"round-trip.mp4";
    return Save(engine, source, output / filename, save_seconds, round_trip) ? 0 : 6;
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    if (argc == 5 && std::wstring_view(argv[1]) == L"--source") {
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
        return RunSource(_wtoi(argv[2]), _wtoi(argv[3]), _wtoi(argv[4]) != 0);
    }
    if (argc == 3 && std::wstring_view(argv[1]) == L"--restore") {
        DisplayMode original{};
        std::ifstream input(std::filesystem::path(argv[2]), std::ios::binary);
        if (!input.read(reinterpret_cast<char*>(&original), sizeof(original))) return 2;
        return ApplyMode(original, "restore") ? 0 : 7;
    }
    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    fthr::monitor::WindowsMonitorTopologySource topology;
    if (argc == 2 && std::wstring_view(argv[1]) == L"--list-monitors") {
        const auto monitors = topology.QueryActiveTopology();
        for (const auto& monitor : monitors.monitors) {
            if (monitor.active) {
                DisplayMode original{}, smaller{};
                wcscpy_s(original.device, monitor.source_gdi_name.c_str());
                const bool supported = QueryMode(original.device, ENUM_CURRENT_SETTINGS, original.mode)
                    && Find900p(original, smaller);
                std::wcout << monitor.source_gdi_name << L'\t' << monitor.left << L'\t' << monitor.top
                           << L'\t' << monitor.width << L'x' << monitor.height
                           << L'\t' << L"900p_available=" << supported << L'\t'
                           << MonitorSelection(monitor) << std::endl;
            }
        }
        return monitors.ok() ? 0 : 2;
    }
    if (argc < 7 || argc > 9) return 2;
    bool start_900p = false;
    bool reporter_length = false;
    for (int index = 7; index < argc; ++index) {
        const std::wstring_view argument(argv[index]);
        if (argument == L"--start-900p") start_900p = true;
        else if (argument == L"--reporter-length") reporter_length = true;
        else return 2;
    }
    const auto selected = fthr::monitor::MonitorResolver(topology).Resolve(argv[1]);
    if (!selected.ok()) return 2;
    DisplayMode original{}, smaller{};
    wcscpy_s(original.device, selected.monitor.source_gdi_name.c_str());
    if (!QueryMode(original.device, ENUM_CURRENT_SETTINGS, original.mode)
        || original.mode.dmPelsWidth != 1920 || original.mode.dmPelsHeight != 1080
        || !Find900p(original, smaller)) {
        std::cerr << "RESIZE_ERROR stage=preflight requires an active 1080p monitor with a 900p mode" << std::endl;
        return 3;
    }
    const auto output = std::filesystem::path(argv[2]);
    const auto deadline = Clock::now() + 15s;
    SourceWindow owned_window{static_cast<DWORD>(_wtoi(argv[3]))};
    HWND source = nullptr;
    RECT bounds{};
    while (Clock::now() < deadline && !source) {
        EnumWindows(FindSourceWindow, reinterpret_cast<LPARAM>(&owned_window));
        const HWND window = owned_window.window;
        if (window && GetForegroundWindow() == window && GetWindowRect(window, &bounds)
            && bounds.left == original.mode.dmPosition.x && bounds.top == original.mode.dmPosition.y
            && bounds.right - bounds.left == 1920 && bounds.bottom - bounds.top == 1080) {
            source = window;
            break;
        }
        std::this_thread::sleep_for(25ms);
    }
    if (!source) {
        GetWindowRect(owned_window.window, &bounds);
        std::cerr << "RESIZE_ERROR stage=foreground source_pid=" << owned_window.pid
                  << " expected_window=" << owned_window.window << " foreground=" << GetForegroundWindow()
                  << " expected_origin=" << original.mode.dmPosition.x << ',' << original.mode.dmPosition.y
                  << " actual_rect=" << bounds.left << ',' << bounds.top << ',' << bounds.right
                  << ',' << bounds.bottom << std::endl;
        return 3;
    }
    const auto restore_path = output / L"display-mode.bin";
    {
        std::ofstream snapshot(restore_path, std::ios::binary);
        snapshot.write(reinterpret_cast<const char*>(&original), sizeof(original));
        snapshot.close();
        if (!snapshot) return 3;
    }
    const int result = Capture(selected.monitor, source, original, smaller, output,
        _wtoi(argv[4]) != 0, _wtoi(argv[5]) != 0, start_900p, reporter_length,
        static_cast<fthr::EncoderPreference>(_wtoi(argv[6])));
    return ApplyMode(original, "restore") ? result : 7;
}
