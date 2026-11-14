#include "capture_engine.h"

#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_5.h>
#include <winrt/base.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;

constexpr wchar_t kSourceClass[] = L"FTHRPerformanceSourceD3D11";
constexpr wchar_t kSourceTitle[] = L"FTHR FPS Performance Source";

class Arguments {
public:
    Arguments(int argc, wchar_t** argv) {
        for (int index = 2; index < argc; index += 2) {
            if (index + 1 == argc || std::wstring_view(argv[index]).substr(0, 2) != L"--"
                || !values_.emplace(argv[index], argv[index + 1]).second) {
                throw std::invalid_argument("Expected distinct --option value pairs");
            }
        }
    }

    std::wstring Get(const wchar_t* key, const wchar_t* fallback = L"") const {
        const auto value = values_.find(key);
        return value == values_.end() ? fallback : value->second;
    }

    int Number(const wchar_t* key, int fallback, int low, int high) const {
        const auto value = Get(key);
        if (value.empty()) return fallback;
        size_t used = 0;
        const auto result = std::stoll(value, &used);
        if (used != value.size() || result < low || result > high) {
            throw std::invalid_argument("Numeric option out of range");
        }
        return static_cast<int>(result);
    }

private:
    std::map<std::wstring, std::wstring> values_;
};

bool StopRequested(const fs::path& path) {
    std::error_code error;
    return !path.empty() && fs::exists(path, error);
}

bool PumpMessages() {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        if (message.message == WM_QUIT) return false;
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return true;
}

LRESULT CALLBACK SourceWindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_DESTROY) {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

struct TimerCloser {
    void operator()(void* handle) const { CloseHandle(handle); }
};

void WaitForSourceDeadline(HANDLE timer, Clock::time_point deadline) {
    const auto remaining = std::chrono::ceil<std::chrono::duration<int64_t, std::ratio<1, 10'000'000>>>(
        deadline - Clock::now()).count();
    if (remaining <= 0) return;
    LARGE_INTEGER due{};
    due.QuadPart = -remaining;
    if (!SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE)
        || MsgWaitForMultipleObjectsEx(1, &timer, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE)
            == WAIT_FAILED) {
        winrt::throw_last_error();
    }
}

uint8_t FrameChecksum(uint16_t id) {
    uint8_t crc = 0;
    for (const unsigned shift : {0u, 8u}) {
        crc ^= static_cast<uint8_t>(id >> shift);
        for (int bit = 0; bit < 8; ++bit) {
            crc = static_cast<uint8_t>((crc << 1) ^ ((crc & 0x80) ? 0x07 : 0));
        }
    }
    return crc;
}

constexpr char kShader[] = R"HLSL(
cbuffer Animation : register(b0) { uint marker; uint frame; uint cell; uint padding; }
float4 vertex(uint vertex_id : SV_VertexID) : SV_Position {
    float2 position = float2((vertex_id << 1) & 2, vertex_id & 2);
    return float4(position * float2(2, -2) + float2(-1, 1), 0, 1);
}
float4 pixel(float4 position : SV_Position) : SV_Target {
    uint x = uint(position.x);
    uint y = uint(position.y);
    if (x >= cell && x < 29 * cell && y >= cell && y < 3 * cell) {
        uint bit = (marker >> ((x - cell) / cell)) & 1;
        return float4(bit, bit, bit, 1);
    }
    uint shift = (frame * 7) & 127;
    uint tiles = (((x + shift) / 48) ^ ((y + (frame & 63)) / 48)) & 1;
    float gradient = ((x + frame * 5 + 2 * y) & 255) / 255.0;
    return float4(0.10 + 0.16 * tiles, 0.18 + 0.36 * gradient, 0.40 + 0.18 * tiles, 1);
}
)HLSL";

winrt::com_ptr<ID3DBlob> CompileShader(const char* entry, const char* target) {
    winrt::com_ptr<ID3DBlob> shader, errors;
    const HRESULT result = D3DCompile(kShader, sizeof(kShader) - 1, "fps-source", nullptr,
        nullptr, entry, target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, shader.put(), errors.put());
    if (FAILED(result) && errors) {
        std::cerr << static_cast<const char*>(errors->GetBufferPointer()) << '\n';
    }
    winrt::check_hresult(result);
    return shader;
}

class SourceRenderer {
public:
    SourceRenderer(HWND window, UINT width, UINT height, UINT cell) : cell_(cell) {
        winrt::com_ptr<ID3D11Device> device;
        winrt::check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION, device.put(),
            nullptr, context_.put()));
        winrt::com_ptr<IDXGIAdapter> adapter;
        winrt::check_hresult(device.as<IDXGIDevice>()->GetAdapter(adapter.put()));
        winrt::com_ptr<IDXGIFactory2> factory;
        winrt::check_hresult(adapter->GetParent(IID_PPV_ARGS(factory.put())));
        BOOL tearing_supported = FALSE;
        if (const auto factory5 = factory.try_as<IDXGIFactory5>()) {
            if (SUCCEEDED(factory5->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING,
                    &tearing_supported, sizeof(tearing_supported))) && tearing_supported) {
                present_flags_ = DXGI_PRESENT_ALLOW_TEARING;
            }
        }
        DXGI_SWAP_CHAIN_DESC1 description{};
        description.Width = width;
        description.Height = height;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        description.BufferCount = 3;
        description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        description.Flags = present_flags_ ? DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING : 0;
        winrt::check_hresult(factory->CreateSwapChainForHwnd(device.get(), window, &description,
            nullptr, nullptr, swap_chain_.put()));
        winrt::check_hresult(factory->MakeWindowAssociation(window, DXGI_MWA_NO_ALT_ENTER));
        winrt::com_ptr<ID3D11Texture2D> back_buffer;
        winrt::check_hresult(swap_chain_->GetBuffer(0, IID_PPV_ARGS(back_buffer.put())));
        winrt::check_hresult(device->CreateRenderTargetView(back_buffer.get(), nullptr, target_.put()));
        const auto vertex = CompileShader("vertex", "vs_4_0");
        const auto pixel = CompileShader("pixel", "ps_4_0");
        winrt::check_hresult(device->CreateVertexShader(vertex->GetBufferPointer(),
            vertex->GetBufferSize(), nullptr, vertex_.put()));
        winrt::check_hresult(device->CreatePixelShader(pixel->GetBufferPointer(),
            pixel->GetBufferSize(), nullptr, pixel_.put()));
        D3D11_BUFFER_DESC buffer{};
        buffer.ByteWidth = sizeof(Constants);
        buffer.Usage = D3D11_USAGE_DEFAULT;
        buffer.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        winrt::check_hresult(device->CreateBuffer(&buffer, nullptr, constants_.put()));
        const D3D11_VIEWPORT viewport{0, 0, static_cast<float>(width), static_cast<float>(height), 0, 1};
        context_->RSSetViewports(1, &viewport);
        context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context_->VSSetShader(vertex_.get(), nullptr, 0);
        context_->PSSetShader(pixel_.get(), nullptr, 0);
        auto* constants = constants_.get();
        context_->PSSetConstantBuffers(0, 1, &constants);
    }

    bool AllowsTearing() const { return present_flags_ != 0; }

    bool Render(uint32_t frame) {
        const auto id = static_cast<uint16_t>(frame);
        const uint32_t marker = 0x5u | (static_cast<uint32_t>(id) << 4)
            | (static_cast<uint32_t>(FrameChecksum(id)) << 20);
        const Constants constants{marker, frame, cell_, 0};
        context_->UpdateSubresource(constants_.get(), 0, nullptr, &constants, 0, 0);
        auto* target = target_.get();
        context_->OMSetRenderTargets(1, &target, nullptr);
        context_->Draw(3, 0);
        const HRESULT result = swap_chain_->Present(0, present_flags_);
        winrt::check_hresult(result);
        return result == S_OK;
    }

private:
    struct Constants { uint32_t marker, frame, cell, padding; };
    winrt::com_ptr<ID3D11DeviceContext> context_;
    winrt::com_ptr<IDXGISwapChain1> swap_chain_;
    winrt::com_ptr<ID3D11RenderTargetView> target_;
    winrt::com_ptr<ID3D11VertexShader> vertex_;
    winrt::com_ptr<ID3D11PixelShader> pixel_;
    winrt::com_ptr<ID3D11Buffer> constants_;
    UINT cell_;
    UINT present_flags_ = 0;
};

int RunSource(const Arguments& args) {
    const int width = args.Number(L"--width", 1280, 320, 7680);
    const int height = args.Number(L"--height", 720, 180, 4320);
    const int fps = args.Number(L"--fps", 180, 0, 480);
    const int cell = std::min(24, width / 32);
    const fs::path stop = args.Get(L"--stop-file");
    if (stop.empty()) throw std::invalid_argument("--source requires --stop-file");
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSW window_class{};
    window_class.lpfnWndProc = SourceWindowProc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    window_class.lpszClassName = kSourceClass;
    if (!RegisterClassW(&window_class)) winrt::throw_last_error();
    const HWND window = CreateWindowExW(0, kSourceClass, kSourceTitle, WS_POPUP,
        args.Number(L"--x", 0, -32768, 32767), args.Number(L"--y", 0, -32768, 32767),
        width, height, nullptr, nullptr, instance, nullptr);
    if (!window) winrt::throw_last_error();
    std::unique_ptr<void, TimerCloser> timer(CreateWaitableTimerExW(
        nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS));
    if (!timer) winrt::throw_last_error();
    SourceRenderer renderer(window, width, height, cell);
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    const auto started = Clock::now();
    uint32_t frame = 1;
    uint64_t presented = renderer.Render(frame) ? 1 : 0;
    RECT rect{};
    if (!GetWindowRect(window, &rect)) winrt::throw_last_error();
    std::cout << "FPS_SOURCE_READY {\"hwnd\":" << reinterpret_cast<uintptr_t>(window)
              << ",\"x\":" << rect.left << ",\"y\":" << rect.top
              << ",\"width\":" << width << ",\"height\":" << height << ",\"cell\":" << cell
              << ",\"fps_requested\":" << fps << ",\"allow_tearing\":"
              << (renderer.AllowsTearing() ? "true" : "false") << "}" << std::endl;

    const auto interval = std::chrono::nanoseconds(1'000'000'000 / (std::max)(1, fps));
    auto next_frame = fps ? started + interval : Clock::time_point::max();
    auto next_check = started;
    auto next_report = started + 1s;
    while (PumpMessages()) {
        const auto now = Clock::now();
        if (now >= next_check) {
            if (StopRequested(stop)) break;
            next_check = now + 100ms;
        }
        if (now >= next_report) {
            std::cout << "FPS_SOURCE_STATS {\"elapsed_s\":" << std::chrono::duration<double>(now - started).count()
                      << ",\"attempted\":" << frame << ",\"presented\":" << presented << "}" << std::endl;
            next_report += 1s;
        }
        if (now >= next_frame) {
            if (renderer.Render(++frame)) ++presented;
            const auto finished = Clock::now();
            next_frame += interval;
            if (next_frame <= finished) next_frame += ((finished - next_frame) / interval + 1) * interval;
        }
        WaitForSourceDeadline(timer.get(), (std::min)(next_frame, next_check));
    }
    if (IsWindow(window)) DestroyWindow(window);
    std::cout << "FPS_SOURCE_DONE {\"elapsed_s\":"
              << std::chrono::duration<double>(Clock::now() - started).count()
              << ",\"attempted\":" << frame << ",\"presented\":" << presented << "}" << std::endl;
    return 0;
}

struct PendingSave {
    std::unique_ptr<fthr::SharedMemoryLayout> response = std::make_unique<fthr::SharedMemoryLayout>();
    Clock::time_point started = Clock::now();
    bool reported = false;
};

bool ObserveSaves(std::vector<PendingSave>& saves, bool& all_saved) {
    bool finished = true;
    for (size_t index = 0; index < saves.size(); ++index) {
        auto& save = saves[index];
        const auto response = save.response->engine_response;
        if (response == fthr::ResponseType::NONE) {
            finished = false;
        } else if (!save.reported) {
            const bool success = response == fthr::ResponseType::CLIP_SAVED;
            all_saved = all_saved && success;
            std::cout << "FPS_SAVE {\"index\":" << index + 1 << ",\"success\":"
                      << (success ? "true" : "false") << ",\"response\":"
                      << static_cast<uint32_t>(response) << ",\"elapsed_s\":"
                      << std::chrono::duration<double>(Clock::now() - save.started).count()
                      << "}" << std::endl;
            if (!success) std::wcerr << L"FPS_SAVE_ERROR " << save.response->engine_string << std::endl;
            save.reported = true;
        }
    }
    return finished;
}

void CaptureStats(const fthr::CaptureEngine& engine, Clock::time_point started) {
    const auto stats = engine.GetStats();
    std::cout << "FPS_CAPTURE_STATS {\"elapsed_s\":"
              << std::chrono::duration<double>(Clock::now() - started).count()
              << ",\"frames\":" << stats.frames_captured << ",\"drops\":" << stats.frames_dropped
              << ",\"ring_frames\":" << stats.ring_used_frames
              << ",\"health\":" << engine.GetCaptureHealthFlags()
              << ",\"generation\":" << engine.GetCaptureGeneration() << "}" << std::endl;
}

int RunCapture(const Arguments& args) {
    const fs::path output = args.Get(L"--output");
    const fs::path stop = args.Get(L"--stop-file");
    if (output.empty() || !fs::is_directory(output) || !fs::is_empty(output)) {
        throw std::invalid_argument("--capture requires an empty --output directory");
    }
    const auto duration = std::chrono::seconds(args.Number(L"--duration", 12, 1, 3600));
    const auto warmup = std::chrono::seconds(args.Number(L"--warmup", 3, 0, 120));
    const auto save_seconds = static_cast<uint32_t>(args.Number(L"--save-seconds", 5, 1, 120));
    const auto save_count = static_cast<size_t>(args.Number(L"--saves", 1, 1, 32));
    const bool manual = args.Number(L"--manual", 0, 0, 1) == 1;
    fthr::CaptureConfig config;
    config.monitor_device_path = args.Get(L"--monitor");
    config.target_width = args.Number(L"--width", 1280, 2, 7680);
    config.target_height = args.Number(L"--height", 720, 2, 4320);
    config.framerate = args.Number(L"--fps", 60, 1, 240);
    config.bitrate_kbps = args.Number(L"--bitrate-kbps", 12000, 500, 200000);
    config.buffer_seconds = (std::max)(10u, save_seconds + 2);
    config.audio_enabled = args.Number(L"--audio", 0, 0, 1) == 1;
    const auto scaling = args.Get(L"--scaling", L"stretch");
    if (scaling == L"fit") config.scaling_mode = fthr::CaptureConfig::ScalingModeEnum::FIT;
    else if (scaling != L"stretch") throw std::invalid_argument("--scaling must be stretch or fit");
    const auto crop = args.Get(L"--crop");
    if (!crop.empty()) {
        std::wistringstream values(crop);
        wchar_t first = 0, second = 0, third = 0;
        if (!(values >> config.crop_x >> first >> config.crop_y >> second >> config.crop_width
                >> third >> config.crop_height)
            || first != L',' || second != L',' || third != L','
            || (values >> std::ws).peek() != WEOF
            || !std::isfinite(config.crop_x + config.crop_y + config.crop_width + config.crop_height)
            || config.crop_x < 0 || config.crop_y < 0 || config.crop_width <= 0 || config.crop_height <= 0
            || config.crop_x + config.crop_width > 1 || config.crop_y + config.crop_height > 1) {
            throw std::invalid_argument("--crop requires normalized x,y,width,height within the source");
        }
        config.crop_enabled = true;
    }
    const auto backend = args.Get(L"--backend", L"dxgi");
    if (backend == L"dxgi") {
        config.capture_backend_preference = fthr::WindowsCaptureBackendPreference::Dxgi;
    } else if (backend == L"wgc" || backend == L"window") {
        config.capture_backend_preference = fthr::WindowsCaptureBackendPreference::Wgc;
    } else {
        throw std::invalid_argument("--backend must be dxgi, wgc, or window");
    }
    if (backend == L"window") {
        const auto target = args.Get(L"--hwnd");
        const HWND window = target.empty() ? FindWindowW(kSourceClass, kSourceTitle)
            : reinterpret_cast<HWND>(std::stoull(target));
        if (!window || !IsWindow(window)) throw std::invalid_argument("Capture window not found");
        config.capture_mode = fthr::CaptureConfig::CaptureModeEnum::WINDOW;
        config.target_hwnd = reinterpret_cast<uintptr_t>(window);
    } else if (config.monitor_device_path.empty()) {
        throw std::invalid_argument("Monitor capture requires --monitor");
    }
    const auto encoder = args.Get(L"--encoder", L"nvidia");
    if (encoder == L"nvidia") config.encoder_preference = fthr::EncoderPreference::Nvidia;
    else if (encoder == L"intel") config.encoder_preference = fthr::EncoderPreference::Intel;
    else if (encoder == L"software") config.encoder_preference = fthr::EncoderPreference::Software;
    else throw std::invalid_argument("--encoder must be nvidia, intel, or software");
    const auto codec = args.Get(L"--codec", L"h264");
    if (codec == L"h264") config.video_codec = fthr::VideoCodec::H264;
    else if (codec == L"hevc") config.video_codec = fthr::VideoCodec::HEVC;
    else if (codec == L"av1") config.video_codec = fthr::VideoCodec::AV1;
    else throw std::invalid_argument("--codec must be h264, hevc, or av1");
    const auto storage = args.Get(L"--storage", L"memory");
    if (storage == L"memory") config.replay_storage.mode = fthr::ReplayStorageMode::Memory;
    else if (storage == L"disk") config.replay_storage.mode = fthr::ReplayStorageMode::Disk;
    else throw std::invalid_argument("--storage must be memory or disk");
    config.replay_storage.directory = output / "replay-storage";
    if (config.replay_storage.mode == fthr::ReplayStorageMode::Disk) {
        fs::create_directories(config.replay_storage.directory);
    }
    config.replay_storage.memory_mb = 512;
    config.replay_storage.disk_mb = 1024;

    // Declared before engine so responses outlive Shutdown even on failure.
    std::vector<PendingSave> saves;
    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        std::cerr << "FPS_CAPTURE_ERROR initialize "
                  << fthr::ReplayStartupErrorName(engine.GetLastStartupErrorCode())
                  << ' ' << engine.GetLastStartupError() << std::endl;
        return 3;
    }
    std::cout << "FPS_CAPTURE_READY {\"backend\":\"" << engine.GetStartupCaptureBackend()
              << "\",\"encoder\":\"" << engine.GetActiveEncoderName() << "\",\"fps_requested\":"
              << config.framerate << "}" << std::endl;
    const auto warmup_end = Clock::now() + warmup;
    while (Clock::now() < warmup_end && !StopRequested(stop)) std::this_thread::sleep_for(25ms);
    if (StopRequested(stop)) return 4;
    if (manual && !engine.StartRecording((output / "manual.mp4").c_str())) {
        std::cerr << "FPS_CAPTURE_ERROR recording " << engine.GetLastRecordingError() << std::endl;
        return 4;
    }
    const auto started = Clock::now();
    auto next_report = started + 1s;
    CaptureStats(engine, started);
    bool all_saved = true;
    for (size_t next_save = 1; next_save <= save_count;) {
        if (StopRequested(stop)) return 4;
        const auto now = Clock::now();
        if (now >= next_report) {
            CaptureStats(engine, started);
            next_report += 1s;
        }
        const auto offset = std::chrono::duration_cast<std::chrono::milliseconds>(duration)
            * static_cast<int64_t>(next_save) / static_cast<int64_t>(save_count);
        if (now >= started + offset) {
            const auto clip = output / (L"replay-" + std::to_wstring(next_save) + L".mp4");
            auto& save = saves.emplace_back();
            if (!engine.SaveClip(clip.c_str(), save_seconds, save.response.get())) {
                std::wcerr << L"FPS_SAVE_ERROR admission " << save.response->engine_string << std::endl;
                return 5;
            }
            ++next_save;
        }
        ObserveSaves(saves, all_saved);
        std::this_thread::sleep_for(20ms);
    }
    const bool recording_saved = !manual || engine.StopRecording();
    const auto save_deadline = Clock::now() + 45s;
    while (!ObserveSaves(saves, all_saved)) {
        if (StopRequested(stop) || Clock::now() >= save_deadline) return 5;
        std::this_thread::sleep_for(20ms);
    }
    CaptureStats(engine, started);
    const auto shutdown = Clock::now();
    engine.Shutdown();
    std::cout << "FPS_CAPTURE_DONE {\"shutdown_s\":"
              << std::chrono::duration<double>(Clock::now() - shutdown).count() << "}" << std::endl;
    return all_saved && recording_saved ? 0 : 5;
}

} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc < 2 || std::wstring_view(argv[1]) == L"--help") {
        std::cout << "windows-fps-performance-smoke --source|--capture [--option value ...]" << std::endl;
        return argc < 2 ? 2 : 0;
    }
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    try {
        const Arguments args(argc, argv);
        const std::wstring_view mode = argv[1];
        if (mode == L"--source") return RunSource(args);
        if (mode == L"--capture") {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            return RunCapture(args);
        }
        throw std::invalid_argument("Mode must be --source or --capture");
    } catch (const winrt::hresult_error& error) {
        std::wcerr << L"FPS_ERROR HRESULT=0x" << std::hex << error.code().value
                   << L' ' << error.message().c_str() << std::endl;
    } catch (const std::exception& error) {
        std::cerr << "FPS_ERROR " << error.what() << std::endl;
    }
    return 2;
}
