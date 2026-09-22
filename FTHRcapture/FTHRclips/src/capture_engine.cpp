// FTHR Capture Engine - Implementation

// Must be defined before ANY include that pulls in windows.h (including winrt/base.h)
// to prevent the min/max macros from being defined and stomping std::min/std::max.
#ifndef NOMINMAX
#define NOMINMAX
#endif
// WGC/DXGI capture feeds a compressed replay ring; the save worker muxes
// packet snapshots. WASAPI feeds persistent AAC encoders and audio rings.
// See capture_engine.h for backend selection and thread ownership.

// Windows Graphics Capture (WGC) includes
// Must come before other Windows headers to avoid redefinition conflicts.
// Requires C++17 (/std:c++17) and windowsapp.lib.
#pragma comment(lib, "windowsapp")

#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <Windows.Graphics.Capture.Interop.h>

#include "capture_engine.h"
#include "capture_focus_policy.h"
#include "wgc_frame_lease.h"
#include "hardware_encoder.h"
#include "encoded_video_config_ffmpeg.h"
#include "video_encoder.h"
#include "save_clip_task.h"
#include "shared_memory.h"
#include "audio_capture.h"
#include "audio_ring_buffer.h"
#include "audio_encoder.h"
#include "clip_audio_manifest.h"
#include "transactional_save.h"
#include "windows_capture_border_policy.h"
#include "windows_dxgi_recovery.h"
#include "windows_native_error.h"
#include "replay_interval.h"
#include "replay_health.h"
#include "frame_rate_scheduler.h"
#include "capture_scale_geometry.h"
#include <iostream>
#include <chrono>
#include <cstring>
#include <cwctype>
#include <algorithm>
#include <cmath>
#include <numeric>
#include <sstream>
#include <filesystem>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/dict.h>
#include <libavutil/mathematics.h>
#include <libavutil/opt.h>
#include <libavutil/imgutils.h>
}


namespace fthr {

    // WGCState — WinRT types confined here so the header stays WinRT-free

    struct CaptureEngine::WGCState {
        winrt::Windows::Graphics::Capture::GraphicsCaptureItem          item{ nullptr };
        winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool   frame_pool{ nullptr };
        winrt::Windows::Graphics::Capture::GraphicsCaptureSession        session{ nullptr };
        winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice  winrt_device{ nullptr };
        winrt::event_token                                                item_closed_token{};
        bool                                                              item_closed_registered = false;
        bool                                                              monitor_item = false;
    };

    namespace {

        std::string AdapterLuidJson(const monitor::AdapterLuid& luid) {
            std::ostringstream value;
            value << "{\"high_part\":" << luid.high_part
                  << ",\"low_part\":" << luid.low_part << '}';
            return value.str();
        }

        bool QueryD3D11DeviceAdapterLuid(
            ID3D11Device* device,
            monitor::AdapterLuid& luid,
            std::string& diagnostic) {
            if (!device) {
                diagnostic = "D3D11 device is null";
                return false;
            }
            IDXGIDevice* dxgi_device = nullptr;
            HRESULT hr = device->QueryInterface(
                __uuidof(IDXGIDevice),
                reinterpret_cast<void**>(&dxgi_device));
            if (FAILED(hr) || !dxgi_device) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "ID3D11Device::QueryInterface(IDXGIDevice)", hr);
                return false;
            }
            IDXGIAdapter* adapter = nullptr;
            hr = dxgi_device->GetAdapter(&adapter);
            dxgi_device->Release();
            if (FAILED(hr) || !adapter) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIDevice::GetAdapter", hr);
                return false;
            }
            DXGI_ADAPTER_DESC description{};
            hr = adapter->GetDesc(&description);
            adapter->Release();
            if (FAILED(hr)) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIAdapter::GetDesc", hr);
                return false;
            }
            luid = {description.AdapterLuid.LowPart,
                    description.AdapterLuid.HighPart};
            return true;
        }

        bool CreateD3D11DeviceForVendor(
            EncoderVendor vendor,
            ID3D11Device** device,
            ID3D11DeviceContext** context,
            std::string& diagnostic) {
            if (!device || !context) return false;
            *device = nullptr;
            *context = nullptr;

            IDXGIFactory1* factory = nullptr;
            const HRESULT factory_status = CreateDXGIFactory1(
                __uuidof(IDXGIFactory1),
                reinterpret_cast<void**>(&factory));
            if (FAILED(factory_status) || !factory) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "CreateDXGIFactory1(encoder adapter search)",
                    factory_status);
                return false;
            }

            IDXGIAdapter1* matched_adapter = nullptr;
            UINT matching_adapters = 0;
            bool enumeration_failed = false;
            for (UINT index = 0; ; ++index) {
                IDXGIAdapter1* adapter = nullptr;
                const HRESULT enumerated = factory->EnumAdapters1(
                    index, &adapter);
                if (enumerated == DXGI_ERROR_NOT_FOUND) {
                    break;
                }
                if (FAILED(enumerated) || !adapter) {
                    diagnostic = diagnostics::FormatHResultFailure(
                        "IDXGIFactory1::EnumAdapters1(encoder adapter search)",
                        enumerated);
                    enumeration_failed = true;
                    if (adapter) adapter->Release();
                    break;
                }
                DXGI_ADAPTER_DESC1 description{};
                const HRESULT description_status = adapter->GetDesc1(
                    &description);
                if (FAILED(description_status)) {
                    diagnostic = diagnostics::FormatHResultFailure(
                        "IDXGIAdapter1::GetDesc1(encoder adapter search)",
                        description_status);
                    enumeration_failed = true;
                    adapter->Release();
                    break;
                }
                if (!(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                    && EncoderVendorFromPciVendorId(description.VendorId)
                        == vendor) {
                    ++matching_adapters;
                    if (matching_adapters == 1) {
                        matched_adapter = adapter;
                        adapter = nullptr;
                    }
                }
                if (adapter) adapter->Release();
            }
            factory->Release();
            if (matching_adapters > 1) {
                if (matched_adapter) matched_adapter->Release();
                diagnostic = "multiple matching hardware encoder adapters were "
                    "found; explicit adapter identity is required";
                return false;
            }
            if (enumeration_failed || matching_adapters == 0 || !matched_adapter) {
                if (matched_adapter) matched_adapter->Release();
                if (diagnostic.empty()) {
                    diagnostic = "No matching hardware encoder adapter was found";
                }
                return false;
            }

            D3D_FEATURE_LEVEL feature_level{};
            const HRESULT device_status = D3D11CreateDevice(
                matched_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
                nullptr, 0, D3D11_SDK_VERSION,
                device, &feature_level, context);
            matched_adapter->Release();
            if (FAILED(device_status)) {
                if (*device) {
                    (*device)->Release();
                    *device = nullptr;
                }
                if (*context) {
                    (*context)->Release();
                    *context = nullptr;
                }
                diagnostic = diagnostics::FormatHResultFailure(
                    "D3D11CreateDevice(encoder adapter)", device_status);
                return false;
            }
            if (!*device || !*context) {
                if (*device) {
                    (*device)->Release();
                    *device = nullptr;
                }
                if (*context) {
                    (*context)->Release();
                    *context = nullptr;
                }
                diagnostic = "D3D11CreateDevice(encoder adapter) returned no device/context";
                return false;
            }
            return true;
        }

    }  // namespace


    // Use executable basenames to select monitor capture for known protected games.
    // Query with PROCESS_QUERY_LIMITED_INFORMATION; module enumeration may be
    // blocked. Keep matching case-insensitive and add only confirmed titles.
    static bool IsAntiCheatProtected(HWND hwnd) {
        if (!hwnd) return false;

        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        if (!pid) return false;

        HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (!h) return false;

        wchar_t path[MAX_PATH] = {};
        DWORD len = MAX_PATH;
        BOOL ok = QueryFullProcessImageNameW(h, 0, path, &len);
        CloseHandle(h);
        if (!ok) return false;

        // Extract basename and lowercase it.
        const wchar_t* slash = wcsrchr(path, L'\\');
        std::wstring exe = slash ? slash + 1 : path;
        std::transform(exe.begin(), exe.end(), exe.begin(),
                       [](wchar_t c) { return static_cast<wchar_t>(towlower(c)); });

        // Known kernel-AC titles. All are matched lowercase.
        static const wchar_t* kProtected[] = {
            L"valorant.exe",                       // Vanguard
            L"valorant-win64-shipping.exe",        // Vanguard (UE shipping name)
            L"r5apex.exe",                         // Apex Legends — EAC
            L"fortniteclient-win64-shipping.exe",  // Fortnite — EAC
            L"rainbowsix.exe",                     // R6 Siege — BattlEye
            L"rainbowsix_be.exe",                  // R6 Siege — BattlEye launcher
            L"escapefromtarkov.exe",               // EFT — BattlEye
            L"destiny2.exe",                       // Destiny 2 — BattlEye
            L"tslgame.exe",                        // PUBG — BattlEye
            L"thefinals.exe",                      // The Finals — EAC
            L"deltaforceclient-win64-shipping.exe",// Delta Force — AC
        };
        for (const wchar_t* name : kProtected) {
            if (exe == name) return true;
        }
        return false;
    }



    void FramePool::Allocate(size_t frame_count, size_t bytes_per_frame) {
        frame_count_ = frame_count;
        bytes_per_frame_ = bytes_per_frame;
        storage_.resize(frame_count * bytes_per_frame);
        std::cout << "[FramePool] Allocated " << frame_count << " slots x "
            << bytes_per_frame << " bytes = "
            << (storage_.size() / 1024 / 1024) << " MB" << std::endl;
    }

    uint8_t* FramePool::GetSlot(size_t index) noexcept {
        return storage_.data() + (index * bytes_per_frame_);
    }


    // Constructor / Destructor

    CaptureEngine::CaptureEngine()
        : capture_thread_(nullptr)
        , stall_watchdog_thread_(nullptr)
        , save_clip_thread_(nullptr)
        , running_(false)
        , is_recording_(false)
        , device_(nullptr)
        , context_(nullptr)
        , duplication_(nullptr)
        , staging_texture_(nullptr)
        , crop_texture_(nullptr)
        , health_staging_texture_(nullptr)
        , nvenc_device_(nullptr)
        , nvenc_context_(nullptr)
        , wgc_active_(false)
        , width_(0)
        , height_(0)
        , crop_enabled_(false)
        , crop_x_(0)
        , crop_y_(0)
        , crop_width_(0)
        , crop_height_(0)
        , target_hwnd_(0)
        , focus_gated_(false)
        , fps_(60)
        , buffer_seconds_(30)
        , target_width_(0)
        , target_height_(0)
        , bitrate_kbps_(16000)
        , scaling_mode_(0)
        , separate_audio_enabled_(false)
        , monitor_resolver_(monitor_topology_source_)
        , nvenc_active_(false)
        , nvidia_device_(false)
        , replay_encoder_cpu_input_(false)
        , capture_adapter_vendor_(EncoderVendor::Software)
        , max_frames_(0)
        , frames_captured_(0)
        , frames_dropped_(0)
        , audio_active_(false)
    {
    }

    CaptureEngine::~CaptureEngine() {
        Shutdown();
    }

    void CaptureEngine::SetCaptureFailure(std::string detail) {
        last_capture_failure_detail_ = std::move(detail);
    }

    void CaptureEngine::EmitRecentDxgiEvidence(const char* reason) const {
        // Snapshot is intentionally taken only at a watchdog/recovery
        // boundary.  It copies at most the fixed evidence capacity and keeps
        // all AcquireNextFrame calls free of diagnostic I/O.
        const auto events = dxgi_recent_evidence_.Snapshot();
        monitor::MonitorTopologyEntry resolved_monitor;
        monitor::DxgiOutputIdentity resolved_output;
        monitor::AdapterLuid capture_luid;
        monitor::AdapterLuid encoder_luid;
        bool capture_luid_available = false;
        bool encoder_luid_available = false;
        RECT desktop_coordinates{};
        bool desktop_coordinates_available = false;
        {
            std::lock_guard<std::mutex> lock(dxgi_context_mutex_);
            resolved_monitor = resolved_monitor_;
            resolved_output = resolved_dxgi_output_;
            capture_luid = capture_device_adapter_luid_;
            encoder_luid = encoder_adapter_luid_;
            capture_luid_available = capture_device_adapter_luid_available_;
            encoder_luid_available = encoder_adapter_luid_available_;
            desktop_coordinates = resolved_desktop_coordinates_;
            desktop_coordinates_available =
                resolved_desktop_coordinates_available_;
        }
        std::ostringstream json;
        json << "{\"subsystem\":\"capture\","
             << "\"event\":\"dxgi_recent_evidence\","
             << "\"reason\":\""
             << diagnostics::JsonEscape(reason ? reason : "unknown")
             << "\",\"event_count\":" << events.size()
             << ",\"monitor_id\":\""
             << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                    monitor_device_path_))
             << "\",\"windows_display\":\""
             << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                    resolved_output.source_gdi_name))
             << "\",\"dxgi_output\":{\"index\":"
             << resolved_output.output_index
             << "},\"monitor_adapter_luid\":";
        if (!resolved_monitor.monitor_device_path.empty()) {
            json << AdapterLuidJson(resolved_monitor.adapter_luid);
        } else {
            json << "\"unavailable:not_resolved\"";
        }
        json << ",\"capture_device_luid\":"
             << (capture_luid_available
                 ? AdapterLuidJson(capture_luid)
                 : "\"unavailable:not_resolved\"")
             << ",\"encoder_adapter_luid\":"
             << (encoder_luid_available
                 ? AdapterLuidJson(encoder_luid)
                 : "\"unavailable:not_resolved\"")
             << ",\"capture_generation\":"
             << capture_generation_.load(std::memory_order_relaxed)
             << ",\"desktop_coordinates\":";
        if (desktop_coordinates_available) {
            const RECT& bounds = desktop_coordinates;
            json << "{\"left\":" << bounds.left
                 << ",\"top\":" << bounds.top
                 << ",\"right\":" << bounds.right
                 << ",\"bottom\":" << bounds.bottom << '}';
        } else {
            json << "\"unavailable:not_resolved\"";
        }
        json << ",\"events\":[";
        for (size_t index = 0; index < events.size(); ++index) {
            if (index != 0) json << ',';
            const auto& event = events[index];
            json << "{\"acquire_api\":\""
                 << "IDXGIOutputDuplication::AcquireNextFrame\","
                 << "\"acquire_start_qpc\":" << event.acquire_start_qpc
                 << ",\"acquire_end_qpc\":" << event.acquire_end_qpc
                 << ",\"last_present_qpc\":" << event.last_present_qpc
                 << ",\"presentation_gap_qpc\":"
                 << event.presentation_gap_qpc
                 << ",\"acquire_hresult\":"
                 << event.acquire_hresult
                 << ",\"acquire_hresult_hex\":\"0x"
                 << std::hex << static_cast<uint32_t>(event.acquire_hresult)
                 << std::dec << "\",\"acquired\":"
                 << (event.acquired ? "true" : "false")
                 << ",\"timed_out\":"
                 << (event.timed_out ? "true" : "false")
                 << ",\"release_attempted\":"
                 << (event.release_attempted ? "true" : "false")
                 << ",\"release_api\":\""
                 << "IDXGIOutputDuplication::ReleaseFrame\","
                 << "\"release_hresult\":" << event.release_hresult
                 << ",\"release_hresult_hex\":\"0x"
                 << std::hex << static_cast<uint32_t>(event.release_hresult)
                 << std::dec << "\",\"resource_token\":"
                 << event.resource_token
                 << ",\"generation\":" << event.generation
                 << ",\"width\":" << event.width
                 << ",\"height\":" << event.height
                 << ",\"format\":" << event.format
                 << ",\"output_index\":" << event.output_index
                 << ",\"pointer_only\":"
                 << (event.pointer_only ? "true" : "false") << '}';
        }
        json << "]}";
        std::cout << "FTHR_DIAGNOSTIC_EVENT " << json.str() << std::endl;
    }

    std::string CaptureEngine::BuildStartupDiagnosticContext() const {
        std::string selected_monitor = diagnostics::WideToUtf8(
            monitor_device_path_);
        if (selected_monitor.empty()) {
            selected_monitor = "unavailable:not_configured";
        }
        std::ostringstream context;
        context << "startup_context={\"selected_monitor_id\":\""
                << diagnostics::JsonEscape(selected_monitor)
                << "\",\"dxgi_output\":";
        if (resolved_dxgi_output_.output_index != UINT32_MAX) {
            context << "{\"index\":" << resolved_dxgi_output_.output_index
                    << ",\"source_gdi_name\":\""
                    << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                           resolved_dxgi_output_.source_gdi_name))
                    << "\"}";
        } else {
            context << "\"unavailable:not_resolved\"";
        }
        context << ",\"owning_adapter_luid\":";
        if (!resolved_monitor_.monitor_device_path.empty()) {
            context << AdapterLuidJson(resolved_monitor_.adapter_luid);
        } else {
            context << "\"unavailable:not_resolved\"";
        }
        context << ",\"capture_device_adapter_luid\":";
        if (capture_device_adapter_luid_available_) {
            context << AdapterLuidJson(capture_device_adapter_luid_);
        } else {
            context << "\"unavailable:not_reached_or_resolved\"";
        }
        context << ",\"encoder_adapter_luid\":";
        if (encoder_adapter_luid_available_) {
            context << AdapterLuidJson(encoder_adapter_luid_);
        } else {
            context << "\"unavailable:not_reached_or_resolved\"";
        }
        context << ",\"capture_backend\":\""
                << diagnostics::JsonEscape(startup_capture_backend_)
                << "\",\"encoder_backend\":\""
                << diagnostics::JsonEscape(startup_encoder_backend_)
                << "\",\"codec\":\""
                << diagnostics::JsonEscape(startup_codec_) << "\"}";
        return context.str();
    }

    bool CaptureEngine::FailStartup(
        ReplayStartupError code, std::string detail) {
        if (!last_capture_failure_detail_.empty()
            && detail.find(last_capture_failure_detail_) == std::string::npos) {
            detail += " ";
            detail += last_capture_failure_detail_;
        }
        detail += " ";
        detail += BuildStartupDiagnosticContext();
        last_startup_error_code_ = code;
        last_startup_error_ = std::move(detail);
        const char* diagnostic_error = "ENGINE_START_FAILED";
        switch (code) {
        case ReplayStartupError::CaptureAdapterUnsupported:
            if (startup_capture_backend_ == "WGC_MONITOR"
                || startup_capture_backend_ == "WGC_WINDOW") {
                diagnostic_error = "CAPTURE_WGC_INIT_FAILED";
            } else if (startup_capture_backend_ == "DXGI_OUTPUT_DUPLICATION"
                       && (last_startup_error_.find("CreateDXGIFactory")
                               != std::string::npos
                           || last_startup_error_.find("D3D11CreateDevice")
                               != std::string::npos
                           || last_startup_error_.find("DuplicateOutput")
                               != std::string::npos
                           || last_startup_error_.find("QueryInterface")
                               != std::string::npos)) {
                diagnostic_error = "CAPTURE_DXGI_INIT_FAILED";
            } else {
                diagnostic_error = "CAPTURE_OUTPUT_OPEN_FAILED";
            }
            break;
        case ReplayStartupError::CrossAdapterPathUnavailable:
            diagnostic_error = "ENCODER_ADAPTER_MISMATCH";
            break;
        case ReplayStartupError::EncoderInitFailed:
            if (startup_encoder_backend_ == "native-nvenc") {
                diagnostic_error = "ENCODER_NVENC_INIT_FAILED";
            } else if (startup_encoder_backend_ == "ffmpeg-amf") {
                diagnostic_error = "ENCODER_AMF_INIT_FAILED";
            } else if (startup_encoder_backend_ == "ffmpeg-qsv") {
                diagnostic_error = "ENCODER_QSV_INIT_FAILED";
            } else {
                diagnostic_error = "ENCODER_INIT_FAILED";
            }
            break;
        case ReplayStartupError::RequestedCodecUnsupported:
        case ReplayStartupError::HardwareEncoderUnavailable:
        case ReplayStartupError::ReplayCapacityLimited:
            diagnostic_error = "ENCODER_INIT_FAILED";
            break;
        case ReplayStartupError::None:
            break;
        }
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"engine\","
                  << "\"event\":\"startup_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"" << diagnostic_error << "\","
                  << "\"native_startup_code\":\""
                  << ReplayStartupErrorName(code) << "\","
                  << "\"detail\":\""
                  << diagnostics::JsonEscape(last_startup_error_) << "\"}"
                  << std::endl;
        std::cerr << "FTHR_STARTUP_ERROR: "
                  << ReplayStartupErrorName(code) << ": "
                  << last_startup_error_ << std::endl;
        return false;
    }


    // Initialize capture and a hardware encoder on the selected adapter.
    // Refuse startup if the requested codec/backend cannot initialize.

    bool CaptureEngine::Initialize(const CaptureConfig& config) {
        last_startup_error_code_ = ReplayStartupError::None;
        last_startup_error_.clear();
        last_capture_failure_detail_.clear();
        active_replay_capability_ = {};
        resolved_monitor_ = {};
        resolved_dxgi_output_ = {};
        resolved_desktop_coordinates_ = {};
        resolved_desktop_coordinates_available_ = false;
        capture_device_adapter_luid_ = {};
        encoder_adapter_luid_ = {};
        capture_device_adapter_luid_available_ = false;
        encoder_adapter_luid_available_ = false;
        startup_capture_backend_ = "unavailable:not_reached";
        startup_encoder_backend_ = "unavailable:not_reached";
        startup_codec_ = VideoCodecName(config.video_codec);
        fps_ = config.framerate;
        buffer_seconds_ = config.buffer_seconds;
        target_width_ = config.target_width;
        target_height_ = config.target_height;
        bitrate_kbps_ = config.bitrate_kbps;
        scaling_mode_ = (config.scaling_mode == CaptureConfig::ScalingModeEnum::FIT) ? 1u : 0u;
        separate_audio_enabled_ = config.separate_audio_enabled;
        monitor_device_path_ = monitor::NormalizeMonitorDevicePath(
            config.monitor_device_path);
        capture_loop_iterations_.store(0);
        capture_acquire_attempts_.store(0);
        capture_acquire_successes_.store(0);
        capture_timeouts_.store(0);
        capture_frames_released_.store(0);
        pointer_only_frames_.store(0);
        source_textures_received_.store(0);
        conversion_submissions_.store(0);
        conversion_completions_.store(0);
        video_packets_produced_.store(0);
        video_ring_insertions_.store(0);
        capture_thread_stage_.store(0);
        last_capture_hresult_.store(0);
        capture_restart_count_.store(0);
        capture_recovery_attempts_.store(0);
        capture_recovery_failures_.store(0);

        std::cout << "[CaptureEngine] Initializing..." << std::endl;
        std::cout << "  FPS        : " << fps_ << std::endl;
        std::cout << "  Buffer     : " << buffer_seconds_ << "s" << std::endl;
        std::cout << "  Target res : ";
        if (target_width_ == 0 || target_height_ == 0)
            std::cout << "Native" << std::endl;
        else
            std::cout << target_width_ << "x" << target_height_ << std::endl;
        std::cout << "  Bitrate    : " << bitrate_kbps_ << " kbps" << std::endl;
        std::cout << "  Codec      : " << VideoCodecName(config.video_codec)
                  << std::endl;

        bool use_disk_spool = false;
        std::wstring spool_dir;
        wchar_t env_spool_buf[MAX_PATH] = {0};
        if (GetEnvironmentVariableW(L"FTHR_REPLAY_TEMP_DIR", env_spool_buf, MAX_PATH) > 0) {
            spool_dir = env_spool_buf;
        }
        wchar_t env_flag_buf[16] = {0};
        if (GetEnvironmentVariableW(L"FTHR_REPLAY_DISK_SPOOL", env_flag_buf, 16) > 0) {
            if (env_flag_buf[0] == L'1' || env_flag_buf[0] == L't' || env_flag_buf[0] == L'T') {
                use_disk_spool = true;
            }
        }
        if (buffer_seconds_ >= 600) {
            use_disk_spool = true;
        }
        if (use_disk_spool && spool_dir.empty()) {
            std::error_code ec;
            spool_dir = (std::filesystem::temp_directory_path(ec) / L"fthr_replay_spool").wstring();
        }

        if (use_disk_spool && !spool_dir.empty()) {
            std::wcout << L"[CaptureEngine] Enabling disk-backed replay spooler at "
                       << spool_dir << L" (retention: " << buffer_seconds_ << L"s)" << std::endl;
            std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
            replay_disk_spooler_ = std::make_unique<ReplayDiskSpooler>(
                std::filesystem::path(spool_dir), 600, buffer_seconds_);
        }

        // Prefer borderless WGC, falling back to DXGI if its border remains required.
        // Regular windows try CreateForWindow first, then monitor capture. Known
        // protected games use monitor capture with a foreground gate to avoid
        // recording the desktop after focus leaves the selected game.
        target_hwnd_ = config.target_hwnd;
        focus_gated_ = false;
        const auto report_wgc_fallback = [this](
            const char* from_backend, const char* to_backend) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"backend_fallback\","
                      << "\"state\":\"DEGRADED\","
                      << "\"error_code\":\"CAPTURE_WGC_INIT_FAILED\","
                      << "\"from_backend\":\"" << from_backend << "\","
                      << "\"to_backend\":\"" << to_backend << "\","
                      << "\"detail\":\""
                      << diagnostics::JsonEscape(last_capture_failure_detail_)
                      << "\"}" << std::endl;
        };

        if (config.capture_mode == CaptureConfig::CaptureModeEnum::WINDOW
            && target_hwnd_ != 0
            && IsAntiCheatProtected(reinterpret_cast<HWND>(target_hwnd_)))
        {
            std::cout << "[CaptureEngine] Anti-cheat protected game detected — "
                      << "using WGC monitor capture (focus-gated)" << std::endl;
            wgc_active_ = true;
            focus_gated_ = true;
            if (!InitializeWGC()) {
                report_wgc_fallback("WGC_MONITOR", "DXGI_OUTPUT_DUPLICATION");
                std::cerr << "[CaptureEngine] WGC unavailable — "
                          << "falling back to DXGI desktop capture" << std::endl;
                wgc_active_ = false;
                focus_gated_ = false;
                if (!InitializeD3D11()) {
                    std::cerr << "[CaptureEngine] D3D11 initialization failed" << std::endl;
                    return FailStartup(
                        ReplayStartupError::CaptureAdapterUnsupported,
                        "The selected monitor could not be opened on its exact "
                        "display adapter.");
                }
            }
        }
        else if (config.capture_mode == CaptureConfig::CaptureModeEnum::WINDOW
            && target_hwnd_ != 0)
        {
            std::cout << "[CaptureEngine] Mode: Window Capture  HWND=0x"
                      << std::hex << target_hwnd_ << std::dec << std::endl;
            wgc_active_ = true;
            if (!InitializeWindowCapture()) {
                report_wgc_fallback("WGC_WINDOW", "WGC_MONITOR");
                std::cerr << "[CaptureEngine] Window capture init failed — "
                          << "falling back to WGC monitor capture" << std::endl;
                target_hwnd_ = 0;
                if (!InitializeWGC()) {
                    report_wgc_fallback(
                        "WGC_MONITOR", "DXGI_OUTPUT_DUPLICATION");
                    std::cerr << "[CaptureEngine] WGC unavailable — falling back to DXGI" << std::endl;
                    wgc_active_ = false;
                    if (!InitializeD3D11()) {
                        std::cerr << "[CaptureEngine] D3D11 initialization failed" << std::endl;
                        return FailStartup(
                            ReplayStartupError::CaptureAdapterUnsupported,
                            "The selected capture source could not be opened on "
                            "a supported display adapter.");
                    }
                }
            }
        }
        else {
            std::cout << "[CaptureEngine] Mode: Desktop Capture (WGC monitor)" << std::endl;
            wgc_active_ = true;
            if (!InitializeWGC()) {
                report_wgc_fallback("WGC_MONITOR", "DXGI_OUTPUT_DUPLICATION");
                std::cerr << "[CaptureEngine] WGC unavailable — falling back to DXGI" << std::endl;
                wgc_active_ = false;
                if (!InitializeD3D11()) {
                    std::cerr << "[CaptureEngine] D3D11 initialization failed" << std::endl;
                    return FailStartup(
                        ReplayStartupError::CaptureAdapterUnsupported,
                        "The selected monitor could not be opened on its exact "
                        "display adapter.");
                }
            }
        }

        // Select the replay encoder on the capture adapter. Clear any earlier WGC
        // fallback error so it cannot be reported as the cause of an encoder failure.
        last_capture_failure_detail_.clear();
        ConfigureCrop(config);
        CaptureScaleGeometry scale_geometry;
        if (!BuildCaptureScaleGeometry(
                crop_width_, crop_height_, target_width_, target_height_,
                scaling_mode_ == 1, scale_geometry)) {
            return FailStartup(
                ReplayStartupError::EncoderInitFailed,
                "The selected capture and target resolutions cannot form a valid "
                "even hardware encoder geometry.");
        }
        target_width_ = scale_geometry.target_width;
        target_height_ = scale_geometry.target_height;
        std::cout << "[CaptureEngine] Attempting hardware replay initialization "
                  << "for selected " << EncoderVendorName(capture_adapter_vendor_)
                  << " adapter..." << std::endl;

        EncoderConfig hw_cfg;
        hw_cfg.src_width = crop_width_;
        hw_cfg.src_height = crop_height_;
        hw_cfg.enc_width = target_width_;
        hw_cfg.enc_height = target_height_;
        hw_cfg.fps = fps_;
        hw_cfg.bitrate_kbps = bitrate_kbps_;
        hw_cfg.hardware_preset = config.encoder_preset;
        hw_cfg.scaling_mode = scaling_mode_;

        const auto selection = SelectWindowsReplayPolicy(
            capture_adapter_vendor_, config.encoder_preference,
            config.video_codec);
        startup_encoder_backend_ = ReplayEncoderBackendName(selection.backend);
        if (!selection.allowed) {
            return FailStartup(selection.error,
                "The requested encoder is unavailable for the selected capture "
                "source and codec. Cross-adapter AMD/Intel and raw replay "
                "fallbacks are disabled.");
        }
        if (!selection.same_adapter
            && scaling_mode_ == 1
            && (scale_geometry.destination.width != target_width_
                || scale_geometry.destination.height != target_height_)) {
            return FailStartup(
                ReplayStartupError::CrossAdapterPathUnavailable,
                "error_code=ENCODER_ADAPTER_MISMATCH: explicit NVIDIA "
                "cross-adapter CPU input does not implement FIT letterboxing; "
                "use STRETCH or select a same-adapter NVIDIA encoder.");
        }
        std::cout << "[ReplayCapability] requested="
                  << VideoCodecName(config.video_codec)
                  << " capture_adapter="
                  << EncoderVendorName(selection.capture_vendor)
                  << " encoder_adapter="
                  << EncoderVendorName(selection.encoder_vendor)
                  << " backend="
                  << ReplayEncoderBackendName(selection.backend)
                  << " adapter_policy="
                  << (selection.same_adapter ? "same-adapter"
                                             : "explicit-cross-adapter")
                  << std::endl;

        replay_encoder_ = CreateProductionReplayEncoder(
            selection.encoder_vendor, config.video_codec);
        if (!replay_encoder_) {
            return FailStartup(
                ReplayStartupError::HardwareEncoderUnavailable,
                "The approved same-adapter hardware encoder backend is not "
                "available in this build.");
        }

        replay_config_publish_failed_.store(false);
        auto packet_callback = [this](const uint8_t* data, uint32_t size,
                                      int64_t pts, bool is_keyframe,
                                      int64_t wall_qpc) {
            if (!encoded_ring_ || !replay_encoder_) {
                replay_config_publish_failed_.store(true);
                return;
            }
            if (!encoded_ring_->HasVideoConfig()) {
                const auto config = replay_encoder_->GetVideoConfig();
                if (!replay_encoder_->IsVideoConfigReady()
                    || !encoded_ring_->SetVideoConfig(config)) {
                    replay_config_publish_failed_.store(true);
                    return;
                }
            }
            const bool ring_inserted = encoded_ring_->Push(
                data, size, pts, is_keyframe, wall_qpc);
            video_packets_produced_.fetch_add(1, std::memory_order_relaxed);
            if (ring_inserted) {
                video_ring_insertions_.fetch_add(1, std::memory_order_relaxed);
            }

            std::shared_ptr<ContinuousRecordingWriter> writer;
            {
                std::lock_guard<std::mutex> lock(record_writer_mutex_);
                writer = record_writer_;
            }
            if (writer && !writer->PushVideo(data, size, pts, is_keyframe)) {
                is_recording_.store(false, std::memory_order_release);
            }

            {
                std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
                if (replay_disk_spooler_) {
                    if (!replay_disk_spooler_->IsActive()) {
                        ContinuousRecordingAudioConfig audio_config;
                        if (audio_active_ && default_mix_audio_encoder_.IsInitialized()) {
                            audio_config.sample_rate = default_mix_audio_encoder_.GetSampleRate();
                            audio_config.channels = default_mix_audio_encoder_.GetChannels();
                            audio_config.codec_extradata = default_mix_audio_encoder_.GetExtradata();
                        }
                        const auto video_config = replay_encoder_->GetVideoConfig();
                        if (replay_encoder_->IsVideoConfigReady() && !video_config.codec_extradata.empty()) {
                            replay_disk_spooler_->Start(video_config, audio_config);
                        }
                    }
                    if (replay_disk_spooler_->IsActive()) {
                        replay_disk_spooler_->PushVideo(data, size, pts, is_keyframe);
                    }
                }
            }
        };

        ID3D11Device* encoder_device = device_;
        ID3D11DeviceContext* encoder_context = context_;
        replay_encoder_cpu_input_ = !selection.same_adapter;
        if (replay_encoder_cpu_input_) {
            std::string encoder_device_diagnostic;
            if (!CreateD3D11DeviceForVendor(
                    EncoderVendor::Nvidia,
                    &nvenc_device_, &nvenc_context_,
                    encoder_device_diagnostic)) {
                return FailStartup(
                    ReplayStartupError::HardwareEncoderUnavailable,
                    "NVIDIA was selected, but a usable NVIDIA D3D11 device "
                    "could not be created. " + encoder_device_diagnostic);
            }
            if (!EnsureStagingTexture()) {
                return FailStartup(
                    ReplayStartupError::CrossAdapterPathUnavailable,
                    "NVIDIA was selected across adapters, but the capture "
                    "readback texture could not be created.");
            }
            encoder_device = nvenc_device_;
            encoder_context = nvenc_context_;
            std::cout << "[ReplayCapability] Explicit hybrid NVIDIA path: "
                      << "capture readback -> NVENC CPU input" << std::endl;
        }
        std::string encoder_luid_diagnostic;
        encoder_adapter_luid_available_ = QueryD3D11DeviceAdapterLuid(
            encoder_device, encoder_adapter_luid_, encoder_luid_diagnostic);
        if (!encoder_adapter_luid_available_) {
            std::cerr << "[ReplayCapability] Could not resolve encoder adapter LUID: "
                      << encoder_luid_diagnostic << std::endl;
        }
        nvenc_active_ = replay_encoder_->Initialize(
            hw_cfg, encoder_device, encoder_context, packet_callback,
            replay_encoder_cpu_input_);
        if (!nvenc_active_) {
            std::cerr << "[CaptureEngine] "
                      << EncoderVendorName(selection.encoder_vendor) << ' '
                      << VideoCodecName(config.video_codec)
                      << " hardware initialization failed";
            const std::string detail = replay_encoder_->GetLastError();
            if (!detail.empty()) std::cerr << ": " << detail;
            std::cerr << std::endl;
            const auto raw_capacity = CalculateRawReplayCapacity(
                crop_width_, crop_height_, 4, fps_, buffer_seconds_,
                config.max_buffer_mb);
            std::ostringstream reason;
            if (!detail.empty()) reason << detail << ". ";
            reason << "The requested " << VideoCodecName(config.video_codec)
                   << " encoder on the requested "
                   << EncoderVendorName(selection.encoder_vendor)
                   << " adapter did not initialize. Automatic codec, "
                      "cross-adapter, and raw replay fallbacks are disabled";
            if (!raw_capacity.meets_requested_duration) {
                reason << "; the legacy raw budget would hold only "
                       << raw_capacity.capacity_milliseconds << " ms of the "
                       << (static_cast<uint64_t>(buffer_seconds_) * 1000ULL)
                       << " ms requested";
            }
            reason << '.';
            return FailStartup(
                ClassifyReplayInitializationFailure(detail), reason.str());
        }

        active_replay_capability_ = EvaluateActiveReplayCapability(
            selection, replay_encoder_->GetActiveEncoderInfo(), true,
            static_cast<uint64_t>(capture_generation_.load()) + 1ULL);
        if (!active_replay_capability_.initialized) {
            return FailStartup(active_replay_capability_.error,
                "The initialized encoder did not match the requested codec, "
                "backend, or selected capture adapter.");
        }

        {
            // Retain requested history plus the encoder's maximum four-second
            // GOP pre-roll and one second for asynchronous publication jitter.
            // If disk spooling is active, clamp in-memory ring to 60 seconds to save RAM.
            const uint32_t ring_seconds = (replay_disk_spooler_)
                ? std::min<uint32_t>(buffer_seconds_, 60)
                : buffer_seconds_;
            const size_t capacity = CalculateEncodedReplaySlotCapacity(
                ring_seconds, fps_);

            LARGE_INTEGER qpc_freq;
            QueryPerformanceFrequency(&qpc_freq);
            encoded_ring_ = std::make_unique<EncodedRingBuffer>(capacity, fps_, qpc_freq.QuadPart);

            // Publish codec, geometry, timing, packet format and decoder
            // configuration as one immutable stream description.
            const auto video_config = replay_encoder_->GetVideoConfig();
            if (replay_encoder_->IsVideoConfigReady()) {
                if (!encoded_ring_->SetVideoConfig(video_config)) {
                    std::cerr << "[CaptureEngine] Could not publish immutable encoded "
                                 "stream configuration" << std::endl;
                    return false;
                }
            } else {
                std::cout << "[CaptureEngine] Decoder configuration will be "
                             "published with the first encoded packet" << std::endl;
            }

            // max_frames_ used for stats - set to time-based count.
            // ring_head_ / ring_count_ not used on NVENC path.
            max_frames_ = static_cast<size_t>(ring_seconds) * fps_;

            const auto active = replay_encoder_->GetActiveEncoderInfo();
            std::cout << "[CaptureEngine] " << active.name
                << " active. Encoded ring: " << capacity
                << " slots (" << ring_seconds << "s). Raw FramePool: skipped." << std::endl;
        }

        const uint32_t audio_ring_seconds = (replay_disk_spooler_)
            ? std::min<uint32_t>(buffer_seconds_, 60)
            : buffer_seconds_;

        // Initialize WASAPI -> AAC encoders -> bounded packet rings before video.
        // Compressed audio keeps long replay windows bounded; saves snapshot packets.
        // If optional audio initialization fails, continue with video only.
        if (!config.audio_enabled) {
            std::cout << "[CaptureEngine] Audio capture disabled by user settings." << std::endl;
        }
        else {
        do {
            AudioCaptureConfig audio_cfg;
            audio_cfg.bitrate_kbps = 128;

            if (!audio_capture_.Initialize(nullptr, audio_cfg)) {
                std::cerr << "[CaptureEngine] AudioCapture init failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                break;
            }

            std::string default_mix_uuid;
            std::string default_mix_uuid_error;
            if (!CreateAudioManifestTransactionId(
                    &default_mix_uuid, &default_mix_uuid_error)) {
                std::cerr << "[CaptureEngine] Could not create Default Mix source UUID: "
                    << default_mix_uuid_error << std::endl;
                audio_capture_.Shutdown();
                break;
            }
            AudioSourceId default_mix_id{default_mix_uuid};
            default_mix_audio_source_ = {};
            default_mix_audio_source_.identity.id = default_mix_id;
            default_mix_audio_source_.identity.type = AudioSourceType::System;
            default_mix_audio_source_.identity.persistent_identity = "default-mix";
            default_mix_audio_source_.identity.display_name = "Default Mix";
            default_mix_audio_source_.identity.icon_reference = "system-audio";
            default_mix_audio_source_.format = {
                audio_capture_.GetSampleRate(), audio_capture_.GetChannels(), "fltp"};
            default_mix_audio_source_.state.health = AudioSourceHealth::Active;
            default_mix_audio_source_.state.admitted = true;
            default_mix_audio_source_.state.active_in_generation = true;
            default_mix_audio_ring_ = std::make_unique<EncodedAudioPacketRing>(
                default_mix_id,
                capture_generation_.load(std::memory_order_relaxed) + 1,
                AudioSourceFormat{audio_capture_.GetSampleRate(),
                                  audio_capture_.GetChannels(), "fltp"},
                audio_ring_seconds);
            if (!default_mix_audio_encoder_.Initialize(
                    audio_capture_.GetSampleRate(), audio_capture_.GetChannels(),
                    audio_cfg.bitrate_kbps,
                    [this](const uint8_t* data, uint32_t size, int64_t pts) {
                        if (default_mix_audio_ring_) {
                            default_mix_audio_ring_->Push(
                                {std::vector<uint8_t>(data, data + size), pts, 1024});
                        }

                        std::shared_ptr<ContinuousRecordingWriter> writer;
                        {
                            std::lock_guard<std::mutex> lock(record_writer_mutex_);
                            writer = record_writer_;
                        }
                        if (writer && !writer->PushAudio(data, size, pts, 1024)) {
                            is_recording_.store(false, std::memory_order_release);
                        }

                        {
                            std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
                            if (replay_disk_spooler_ && replay_disk_spooler_->IsActive()) {
                                replay_disk_spooler_->PushAudio(data, size, pts, 1024);
                            }
                        }
                    })) {
                std::cerr << "[CaptureEngine] Persistent AAC encoder init failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                default_mix_audio_ring_.reset();
                break;
            }
            default_mix_audio_ring_->SetCodecExtradata(
                default_mix_audio_encoder_.GetExtradata());
            audio_capture_.SetEncoder(&default_mix_audio_encoder_);

            if (!audio_capture_.Start()) {
                std::cerr << "[CaptureEngine] AudioCapture start failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                default_mix_audio_encoder_.Finalize();
                default_mix_audio_ring_.reset();
                break;
            }

            audio_active_ = true;
            std::cout << "[CaptureEngine] Audio capture active ("
                << audio_capture_.GetSampleRate() << "Hz, "
                << audio_capture_.GetChannels() << "ch, "
                << "persistent AAC packet replay)"
                << std::endl;

        } while (false);

            std::string microphone_uuid;
            std::string microphone_uuid_error;
            if (!CreateAudioManifestTransactionId(&microphone_uuid, &microphone_uuid_error)) {
                std::cerr << "[CaptureEngine] Could not create Microphone source UUID: "
                    << microphone_uuid_error << std::endl;
            } else {
                microphone_audio_metadata_ = {};
                microphone_audio_metadata_.identity.id = AudioSourceId{microphone_uuid};
                microphone_audio_metadata_.identity.type = AudioSourceType::Microphone;
                microphone_audio_metadata_.identity.persistent_identity = "microphone";
                microphone_audio_metadata_.identity.display_name = "Microphone";
                microphone_audio_metadata_.identity.icon_reference = "microphone";
                microphone_audio_metadata_.format = {
                    kCanonicalAudioSampleRate, kCanonicalAudioChannels, "fltp"};
                microphone_audio_metadata_.state.health = AudioSourceHealth::Discovered;

                WindowsMicrophoneAudioProviderConfig microphone_config;
                microphone_config.generation = capture_generation_.load(
                    std::memory_order_relaxed) + 1;
                microphone_config.endpoint_id = config.microphone_endpoint_id;
                microphone_config.use_default_endpoint = config.microphone_endpoint_id.empty();
                microphone_config.retention_seconds = audio_ring_seconds;
                microphone_config.bitrate_kbps = 96;
                microphone_config.input_gain = std::clamp(
                    static_cast<float>(config.microphone_gain_percent) / 100.0f,
                    0.0f, 2.0f);
                microphone_config.source = microphone_audio_metadata_;
                microphone_audio_source_ = std::make_unique<WindowsMicrophoneAudioProvider>(
                    std::move(microphone_config));
                if (!microphone_audio_source_->Start()) {
                    const std::string microphone_error =
                        microphone_audio_source_->last_error();
                    std::cerr << "[CaptureEngine] Native microphone provider did not start: "
                        << microphone_error << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"microphone_initialized\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"MIC_INIT_FAILED\","
                              << "\"endpoint\":"
                              << "\"unavailable:provider_start_failed\","
                              << "\"detail\":\""
                              << diagnostics::JsonEscape(microphone_error)
                              << "\"}" << std::endl;
                    microphone_audio_source_.reset();
                } else {
                    std::cout << "[CaptureEngine] Native microphone provider starting ("
                        << (config.microphone_endpoint_id.empty() ? "Default microphone"
                                                                   : "explicit endpoint")
                        << ")." << std::endl;
                }
            }

            if (separate_audio_enabled_) {
                application_audio_source_manager_ =
                    std::make_unique<WindowsApplicationAudioSourceManager>(
                        capture_generation_.load(std::memory_order_relaxed) + 1,
                        audio_ring_seconds);
                if (!application_audio_source_manager_->Start()) {
                    const auto capability = application_audio_source_manager_->capability();
                    const std::string detail = application_audio_source_manager_->last_error();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_initialized\","
                              << "\"state\":\"UNAVAILABLE\","
                              << "\"error_code\":"
                              << (capability.api_build_supported
                                  ? "\"PROCESS_AUDIO_INIT_FAILED\""
                                  : "\"AUDIO_PROCESS_LOOPBACK_UNSUPPORTED\"")
                              << ",\"os_build\":" << capability.os_build
                              << ",\"detail\":\""
                              << diagnostics::JsonEscape(detail.empty()
                                  ? "Windows 11 process loopback is unavailable"
                                  : detail)
                              << "\"}" << std::endl;
                    application_audio_source_manager_.reset();
                } else {
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_initialized\","
                              << "\"state\":\"STARTING\","
                              << "\"error_code\":null}"
                              << std::endl;
                }
            }
            std::cout << "[CaptureEngine] Audio routing: system mix + microphone"
                      << (application_audio_source_manager_
                          ? " + application stems."
                          : (separate_audio_enabled_
                              ? " (application stems unavailable)."
                              : "."))
                      << std::endl;

        }

        if (!audio_active_ && !microphone_audio_source_
                && !application_audio_source_manager_) {
            std::cout << "[CaptureEngine] Running in video-only mode." << std::endl;
        }

        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"adapter_topology_resolved\","
                  << "\"monitor_id\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         monitor_device_path_)) << "\","
                  << "\"windows_display\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         resolved_dxgi_output_.source_gdi_name)) << "\","
                  << "\"dxgi_output\":";
        if (resolved_dxgi_output_.output_index != UINT32_MAX) {
            std::cout << "{\"index\":" << resolved_dxgi_output_.output_index << "},";
        } else {
            std::cout << "\"unavailable:not_resolved\",";
        }
        std::cout
                  << "\"monitor_adapter\":\""
                  << diagnostics::JsonEscape(EncoderVendorName(
                         selection.capture_vendor)) << "\","
                  << "\"monitor_adapter_luid\":"
                  << AdapterLuidJson(resolved_monitor_.adapter_luid) << ','
                  << "\"capture_d3d11_device\":\"selected-monitor-adapter\","
                  << "\"capture_device_luid\":"
                  << (capture_device_adapter_luid_available_
                      ? AdapterLuidJson(capture_device_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << ','
                  << "\"encoder_adapter\":\""
                  << diagnostics::JsonEscape(EncoderVendorName(
                         selection.encoder_vendor)) << "\","
                  << "\"encoder_adapter_luid\":"
                  << (encoder_adapter_luid_available_
                      ? AdapterLuidJson(encoder_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << ','
                  << "\"capture_backend\":\""
                  << diagnostics::JsonEscape(startup_capture_backend_) << "\","
                  << "\"encoder_backend\":\""
                  << diagnostics::JsonEscape(startup_encoder_backend_) << "\","
                  << "\"codec\":\""
                  << diagnostics::JsonEscape(startup_codec_) << "\","
                  << "\"capture_width\":" << width_ << ','
                  << "\"capture_height\":" << height_ << ','
                  << "\"encoder_width\":"
                  << (target_width_ == 0 ? crop_width_ : target_width_) << ','
                  << "\"encoder_height\":"
                  << (target_height_ == 0 ? crop_height_ : target_height_) << "}"
                  << std::endl;

        if (config.audio_enabled) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                      << "\"event\":\"system_audio_initialized\","
                      << "\"state\":\"" << (audio_active_ ? "READY" : "FAILED")
                      << "\",\"error_code\":"
                       << (audio_active_ ? "null" : "\"SYSTEM_AUDIO_INIT_FAILED\"")
                      << ",\"endpoint\":\""
                      << diagnostics::JsonEscape(audio_capture_.GetFriendlyName())
                      << "\",\"requested_sample_rate\":"
                      << audio_capture_.GetRequestedSampleRate()
                       << ",\"actual_sample_rate\":" << audio_capture_.GetInputSampleRate()
                      << ",\"requested_channels\":"
                      << audio_capture_.GetRequestedChannels()
                       << ",\"actual_channels\":" << audio_capture_.GetInputChannels()
                       << ",\"sample_format\":\""
                       << diagnostics::JsonEscape(audio_active_
                           ? audio_capture_.GetInputSampleFormat()
                           : "unavailable:initialization_failed") << "\""
                      << "}"
                      << std::endl;
            if (microphone_audio_source_) {
                const auto microphone_info = microphone_audio_source_->runtime_info();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                          << "\"event\":\"microphone_initialized\","
                          << "\"state\":\"STARTING\",\"endpoint\":\""
                          << diagnostics::JsonEscape(
                                 microphone_info.active_display_name.empty()
                                     ? "unavailable:provider_starting"
                                     : microphone_info.active_display_name)
                          << "\",\"requested_sample_rate\":48000,"
                          << "\"requested_channels\":2}"
                          << std::endl;
            }
        }

        // Start video capture and save-clip threads.
        // Audio is already running so both clocks start together.
        running_.store(true);
        capture_generation_.fetch_add(1);
        capture_health_flags_.store(CAPTURE_HEALTH_ACTIVE);

        // Select capture loop: WGC image sampling or DXGI acquisition.
        if (wgc_active_) {
            capture_thread_ = new std::thread(&CaptureEngine::CaptureThreadWGC, this);
        } else {
            capture_thread_ = new std::thread(&CaptureEngine::CaptureThread, this);
        }
        // NORMAL priority keeps us off-contention with game render threads.
        SetThreadPriority(capture_thread_->native_handle(), THREAD_PRIORITY_NORMAL);

        save_clip_thread_ = new std::thread(&CaptureEngine::SaveClipThread, this);
        stall_watchdog_thread_ = new std::thread(
            &CaptureEngine::ReplayStallWatchdogThread, this);

        std::cout << "[CaptureEngine] Running." << std::endl;
        return true;
    }



    void CaptureEngine::Shutdown() {
        const bool has_resources = capture_thread_ || stall_watchdog_thread_
            || save_clip_thread_
            || device_ || context_ || wgc_state_
            || replay_encoder_ || audio_active_ || nvenc_device_
            || nvenc_context_ || record_writer_ || application_audio_source_manager_
            || replay_disk_spooler_;
        if (!has_resources) return;

        std::cout << "[CaptureEngine] Shutting down..." << std::endl;

        bool has_recording_writer = false;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            has_recording_writer = static_cast<bool>(record_writer_);
        }
        if (is_recording_.load() || has_recording_writer) StopRecording();

        running_.store(false);

        // If CaptureThread is blocked waiting for a WGC frame, wake it so it exits.
        wgc_frame_cv_.notify_all();

        save_clip_queue_.Shutdown();

        if (stall_watchdog_thread_) {
            stall_watchdog_thread_->join();
            delete stall_watchdog_thread_;
            stall_watchdog_thread_ = nullptr;
        }

        if (capture_thread_) {
            capture_thread_->join();
            delete capture_thread_;
            capture_thread_ = nullptr;
        }

        if (save_clip_thread_) {
            save_clip_thread_->join();
            delete save_clip_thread_;
            save_clip_thread_ = nullptr;
        }

        {
            std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
            if (replay_disk_spooler_) {
                replay_disk_spooler_->Stop();
                replay_disk_spooler_->Cleanup();
                replay_disk_spooler_.reset();
            }
        }

        // Finalize NVENC encoder after CaptureThread has exited
        // (guarantees no EncodeFrame call is in flight)
        if (nvenc_active_) {
            replay_encoder_->Shutdown();
        }
        nvenc_active_ = false;
        replay_encoder_cpu_input_ = false;

        // Stop audio pipeline. Order matters:
        //   1. Stop WASAPI thread (no more EncodeSamples calls after this)
        //   2. Finalize encoder (flushes partial AAC frame)
        //   3. compressed packet ring is released with the generation
        if (application_audio_source_manager_) {
            // Application providers can still be submitting AAC packets. Stop
            // and join their monitor/providers before touching any audio state.
            application_audio_source_manager_->Stop();
            application_audio_source_manager_.reset();
        }
        if (audio_active_) {
            microphone_audio_source_.reset();
            audio_capture_.Stop();
            if (!default_mix_audio_encoder_.Finalize()) {
                std::cerr << "[CaptureEngine] Default Mix AAC finalization failed"
                          << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                             "\"event\":\"encoder_finalize_failed\","
                             "\"state\":\"FAILED\","
                             "\"error_code\":\"AUDIO_ENCODER_FAILED\","
                             "\"source\":\"system_audio\"}" << std::endl;
            }
            audio_capture_.Shutdown();
            default_mix_audio_ring_.reset();
            default_mix_audio_source_ = {};
            microphone_audio_metadata_ = {};
            audio_active_ = false;
            std::cout << "[CaptureEngine] Audio pipeline stopped." << std::endl;
        }
        else {
            microphone_audio_source_.reset();
        }

        ring_head_.store(0, std::memory_order_relaxed);
        ring_count_.store(0, std::memory_order_relaxed);

        // WGC session must be closed before releasing the D3D11 device it references.
        if (wgc_active_) {
            ShutdownWGC();
        }

        ShutdownD3D11();

        // Release the Optimus NVENC device after D3D11 and NVENC are both shut down.
        if (nvenc_context_) { nvenc_context_->Release(); nvenc_context_ = nullptr; }
        if (nvenc_device_)  { nvenc_device_->Release();  nvenc_device_ = nullptr; }
        replay_encoder_.reset();
        encoded_ring_.reset();

        std::cout << "[CaptureEngine] Shutdown complete. Frames captured: "
            << frames_captured_.load() << std::endl;
    }

    void CaptureEngine::ReplayStallWatchdogThread() {
        using clock = std::chrono::steady_clock;
        uint64_t previous_packets = 0;
        uint64_t previous_acquire_attempts = 0;
        uint64_t previous_acquired = 0;
        uint64_t previous_submissions = 0;
        auto last_output_progress = clock::now();
        auto last_stall_progress = last_output_progress;
        auto last_acquire_progress = clock::now();
        auto last_acquired_progress = clock::now();
        auto last_submission_progress = clock::now();
        auto last_health_snapshot = clock::now();
        bool snapshot_emitted = false;

        while (running_.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
            if (ReplayWatchdogSuspended(
                    capture_health_flags_.load(std::memory_order_relaxed))) {
                // The capture thread owns DXGI recovery. The watchdog must not
                // cancel its bounded backoff or diagnose the intentional gap
                // as a second, unrelated pipeline stall. A focus-gated game
                // also intentionally stops submitting while tabbed out.
                const auto recovering_now = clock::now();
                last_stall_progress = recovering_now;
                last_acquire_progress = recovering_now;
                last_acquired_progress = recovering_now;
                last_submission_progress = recovering_now;
                continue;
            }
            const uint64_t packets = video_packets_produced_.load(
                std::memory_order_relaxed);
            const uint64_t acquire_attempts = capture_acquire_attempts_.load(
                std::memory_order_relaxed);
            const uint64_t acquired = source_textures_received_.load(
                std::memory_order_relaxed);
            const uint64_t submissions = frames_captured_.load(
                std::memory_order_relaxed);
            const auto now = clock::now();
            if (acquire_attempts != previous_acquire_attempts) {
                previous_acquire_attempts = acquire_attempts;
                last_acquire_progress = now;
            }
            if (acquired != previous_acquired) {
                previous_acquired = acquired;
                last_acquired_progress = now;
            }
            if (submissions != previous_submissions) {
                previous_submissions = submissions;
                last_submission_progress = now;
            }
            if (now - last_health_snapshot >= std::chrono::seconds(10)) {
                last_health_snapshot = now;
                const auto acquired_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(now - last_acquired_progress).count();
                const auto submission_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(now - last_submission_progress).count();
                const auto output_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(
                        now - last_output_progress).count();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"capture_health_snapshot\","
                          << "\"frames_acquired\":" << acquired << ','
                          << "\"frames_submitted\":" << submissions << ','
                          << "\"frames_dropped\":" << frames_dropped_.load() << ','
                          << "\"stale_frame_observations\":"
                          << content_suspicious_streak_.load() << ','
                          << "\"duplicate_frame_observations\":"
                          << "\"unavailable:not_measured\","
                          << "\"pointer_only_frames\":"
                          << pointer_only_frames_.load() << ','
                          << "\"encoder_submissions\":" << submissions << ','
                          << "\"encoded_packets\":" << packets << ','
                          << "\"last_acquired_age_ms\":" << acquired_age << ','
                          << "\"last_encoder_submission_age_ms\":"
                          << submission_age << ','
                          << "\"last_encoded_output_age_ms\":" << output_age << ','
                          << "\"capture_restart_count\":"
                          << capture_restart_count_.load() << ','
                          << "\"capture_recovery_attempts\":"
                          << capture_recovery_attempts_.load() << ','
                          << "\"capture_recovery_failures\":"
                          << capture_recovery_failures_.load() << ','
                          << "\"encoder_restart_count\":0}"
                          << std::endl;

                if (audio_active_) {
                    const uint64_t system_packets = audio_capture_.GetPacketCount();
                    const uint64_t system_discontinuities =
                        audio_capture_.GetDiscontinuityCount();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"audio_health_snapshot\","
                              << "\"source\":\"system_audio\","
                              << "\"state\":\""
                              << (system_packets == 0 ? "NO_PACKETS" : "ACTIVE") << "\","
                              << "\"error_code\":"
                              << (system_packets == 0
                                  ? "\"SYSTEM_AUDIO_NO_PACKETS\""
                                  : (system_discontinuities > 0
                                      ? "\"AUDIO_PACKET_DISCONTINUITY\"" : "null")) << ','
                              << "\"endpoint\":\""
                              << diagnostics::JsonEscape(audio_capture_.GetFriendlyName())
                              << "\",\"actual_sample_rate\":"
                              << audio_capture_.GetInputSampleRate()
                              << ",\"actual_channels\":"
                              << audio_capture_.GetInputChannels()
                              << ",\"actual_sample_format\":\""
                              << diagnostics::JsonEscape(audio_capture_.GetInputSampleFormat())
                              << "\",\"internal_sample_rate\":"
                              << audio_capture_.GetSampleRate()
                              << ",\"internal_channels\":"
                              << audio_capture_.GetChannels()
                              << ",\"internal_sample_format\":\"float32\","
                              << "\"packet_count\":" << system_packets << ','
                              << "\"discontinuity_count\":"
                              << system_discontinuities << ','
                              << "\"silent_packet_count\":"
                              << audio_capture_.GetSilentPacketCount() << ','
                              << "\"no_packet_intervals\":"
                              << audio_capture_.GetNoPacketIntervalCount() << ','
                              << "\"converted_frames\":"
                              << audio_capture_.GetConvertedFrameCount() << ','
                              << "\"submitted_frames\":"
                              << audio_capture_.GetSubmittedFrameCount() << ','
                              << "\"encoded_packets\":"
                              << audio_capture_.GetEncodedPacketCount() << ','
                              << "\"encoder_errors\":"
                              << audio_capture_.GetEncoderErrorCount() << ','
                              << "\"largest_no_packet_gap_100ns\":"
                              << audio_capture_.GetLargestNoPacketGap100ns() << ','
                              << "\"max_drift_samples\":"
                              << audio_capture_.GetMaxObservedDriftSamples() << ','
                              << "\"restart_count\":"
                              << audio_capture_.GetRestartCount() << ','
                              << "\"underrun_count\":"
                              << "\"unavailable:not_exposed_by_wasapi_capture\","
                              << "\"first_packet_timestamp_100ns\":"
                              << audio_capture_.GetFirstPacketQpc100ns() << ','
                              << "\"last_packet_timestamp_100ns\":"
                              << audio_capture_.GetLastPacketQpc100ns() << "}"
                              << std::endl;
                }
                if (microphone_audio_source_) {
                    const auto microphone = microphone_audio_source_->runtime_info();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"audio_health_snapshot\","
                              << "\"source\":\"microphone\","
                              << "\"state\":\""
                              << (microphone.failed ? "FAILED"
                                  : (microphone.packet_count == 0
                                      ? "NO_PACKETS" : "ACTIVE")) << "\","
                              << "\"error_code\":"
                              << (microphone.failed
                                  ? "\"MIC_INIT_FAILED\""
                                  : (microphone.packet_count == 0
                                      ? "\"MIC_NO_PACKETS\""
                                      : (microphone.discontinuity_count > 0
                                          ? "\"AUDIO_PACKET_DISCONTINUITY\""
                                          : "null"))) << ','
                              << "\"endpoint\":\""
                              << diagnostics::JsonEscape(
                                     microphone.active_display_name.empty()
                                         ? "unavailable:provider_starting"
                                         : microphone.active_display_name) << "\","
                              << "\"requested_sample_rate\":48000,"
                              << "\"actual_sample_rate\":"
                              << microphone.input_format.sample_rate << ','
                              << "\"requested_channels\":2,"
                              << "\"actual_channels\":"
                              << microphone.input_format.channels << ','
                              << "\"sample_format\":\""
                              << diagnostics::JsonEscape(
                                     microphone.input_format.sample_format)
                              << "\","
                              << "\"packet_count\":" << microphone.packet_count << ','
                              << "\"converted_frames\":"
                              << microphone.converted_frame_count << ','
                              << "\"encoded_packets\":"
                              << microphone.encoded_packet_count << ','
                              << "\"encoder_errors\":"
                              << microphone.encoder_error_count << ','
                              << "\"rejected_packets\":"
                              << microphone.rejected_packet_count << ','
                              << "\"discontinuity_count\":"
                              << microphone.discontinuity_count << ','
                              << "\"largest_no_packet_gap_100ns\":"
                              << microphone.largest_no_packet_gap_100ns << ','
                              << "\"underrun_count\":"
                              << "\"unavailable:not_exposed_by_wasapi_capture\","
                              << "\"first_packet_timestamp_100ns\":"
                              << microphone.first_packet_qpc_100ns << ','
                              << "\"last_packet_timestamp_100ns\":"
                              << microphone.last_packet_qpc_100ns << ','
                              << "\"max_drift_samples\":"
                              << microphone.max_observed_drift_samples << ','
                              << "\"failed\":"
                              << (microphone.failed ? "true" : "false") << "}"
                              << std::endl;
                }
            }
            if (packets != previous_packets) {
                previous_packets = packets;
                last_output_progress = now;
                last_stall_progress = now;
                snapshot_emitted = false;
                continue;
            }
            const bool awaiting_first_packet = packets == 0;
            const auto stall_limit = awaiting_first_packet
                ? std::chrono::seconds(8)
                : std::chrono::seconds(2);
            if (snapshot_emitted || now - last_stall_progress <= stall_limit) {
                continue;
            }

            const auto encoder = replay_encoder_
                ? replay_encoder_->GetDiagnostics()
                : ReplayEncoderDiagnostics{};
            const bool dxgi_is_alive_but_desktop_is_static =
                !awaiting_first_packet
                && static_cast<HRESULT>(last_capture_hresult_.load(
                    std::memory_order_relaxed)) == DXGI_ERROR_WAIT_TIMEOUT
                && now - last_acquire_progress < std::chrono::seconds(1)
                && encoder.pending_resources == 0
                && encoder.queued_outputs == 0
                && encoder.submit_stage == 0
                && encoder.drain_stage == 0;
            if (dxgi_is_alive_but_desktop_is_static) {
                // Desktop Duplication reports only changed frames. Repeated
                // WAIT_TIMEOUT with a live acquire loop and an empty encoder
                // is expected and must not be diagnosed as a replay stall.
                last_stall_progress = now;
                continue;
            }
            const HRESULT removed_reason = device_
                ? device_->GetDeviceRemovedReason()
                : E_POINTER;
            std::cerr
                << "[ReplayStall] no encoded packet for >2s"
                << " capture_stage=" << capture_thread_stage_.load()
                << " loops=" << capture_loop_iterations_.load()
                << " acquire_attempts=" << capture_acquire_attempts_.load()
                << " acquired=" << capture_acquire_successes_.load()
                << " timeouts=" << capture_timeouts_.load()
                << " released=" << capture_frames_released_.load()
                << " owned=" << (capture_acquire_successes_.load()
                    - capture_frames_released_.load())
                << " textures=" << source_textures_received_.load()
                << " conversion_submit=" << conversion_submissions_.load()
                << " conversion_complete=" << conversion_completions_.load()
                << " frames=" << frames_captured_.load()
                << " packets=" << video_packets_produced_.load()
                << " ring_push=" << video_ring_insertions_.load()
                << " last_hr=0x" << std::hex
                << static_cast<uint32_t>(last_capture_hresult_.load())
                << " removed_reason=0x"
                << static_cast<uint32_t>(removed_reason) << std::dec
                << " encoder_submit_stage=" << encoder.submit_stage
                << " encoder_drain_stage=" << encoder.drain_stage
                << " slots_acquired=" << encoder.input_slots_acquired
                << " map=" << encoder.maps_succeeded << '/'
                << encoder.map_attempts
                << " mapped_now=" << encoder.mapped_resources
                << " encode_return=" << encoder.encode_returns << '/'
                << encoder.encode_attempts
                << " encode_ok=" << encoder.encode_successes
                << " drain=" << encoder.drain_dequeues
                << " completion=" << encoder.completion_events
                << " lock=" << encoder.bitstream_locks << '/'
                << encoder.bitstream_lock_attempts
                << " locked_now=" << encoder.locked_bitstreams
                << " unlock=" << encoder.bitstream_unlocks
                << " unmap=" << encoder.resources_unmapped
                << " recycled=" << encoder.slots_recycled
                << " pending=" << encoder.pending_resources
                << " queue=" << encoder.queued_outputs
                << " pool=" << encoder.pool_capacity
                << " registered=" << encoder.registered_resources
                << " nvenc_status=" << encoder.last_nvenc_status
                << std::endl;
            const bool capture_stalled =
                now - last_acquired_progress > std::chrono::seconds(2);
            const bool encoder_received_recent_submission =
                submissions > 0
                && now - last_submission_progress <= std::chrono::seconds(2);
            const bool encoder_has_pending_work =
                encoder.pending_resources > 0
                || encoder.queued_outputs > 0
                || encoder.locked_bitstreams > 0
                || encoder.drain_stage != 0;
            const auto stall_boundary = dxgi::ClassifyPipelineStall(
                capture_stalled, acquired > 0,
                encoder_received_recent_submission,
                encoder_has_pending_work);
            const char* diagnostic_code = dxgi::PipelineStallCode(
                stall_boundary);
            EmitRecentDxgiEvidence("replay_stall_detected");
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"pipeline_stall_detected\","
                      << "\"state\":\"FAILED\",\"error_code\":\""
                      << diagnostic_code << "\","
                      << "\"frames_acquired\":" << acquired << ','
                      << "\"encoder_submissions\":" << submissions << ','
                      << "\"encoded_packets\":" << packets << ','
                      << "\"capture_stage\":" << capture_thread_stage_.load() << ','
                      << "\"encoder_submit_stage\":" << encoder.submit_stage << ','
                      << "\"encoder_drain_stage\":" << encoder.drain_stage << "}"
                      << std::endl;
            snapshot_emitted = true;
            SetCaptureFailure(std::string(diagnostic_code)
                + ": the replay pipeline stopped making bounded progress");
            ClearReplayForRecovery();
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
            running_.store(false);
            break;
        }
    }


    // StartRecording / StopRecording

    bool CaptureEngine::StartRecording(const wchar_t* path) {
        if (is_recording_.load(std::memory_order_acquire) || !running_.load()
            || !path || !*path || !replay_encoder_ || !encoded_ring_) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            last_recording_error_ =
                "Replay capture is not ready for a manual recording.";
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            if (record_writer_) {
                last_recording_error_ = record_writer_->HasFailed()
                    ? record_writer_->LastError()
                    : "A manual recording is already active.";
                return false;
            }
        }

        // Some hardware codecs publish decoder configuration with their first
        // packet. Read the synchronized ring copy rather than racing that
        // publication or rejecting a start immediately after a quality switch.
        EncodedVideoConfig video_config;
        if (!encoded_ring_->WaitForVideoConfig(
                video_config, std::chrono::seconds(3)) || !running_.load()) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            last_recording_error_ =
                "The hardware video stream is still starting. Try again in a moment.";
            return false;
        }

        ContinuousRecordingAudioConfig audio_config;
        if (audio_active_ && default_mix_audio_encoder_.IsInitialized()) {
            audio_config.sample_rate = default_mix_audio_encoder_.GetSampleRate();
            audio_config.channels = default_mix_audio_encoder_.GetChannels();
            audio_config.codec_extradata =
                default_mix_audio_encoder_.GetExtradata();
        }

        auto writer = std::make_shared<ContinuousRecordingWriter>();
        if (!writer->Start(std::filesystem::path(path), video_config, audio_config)) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            last_recording_error_ = writer->LastError();
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            record_writer_ = std::move(writer);
            last_recording_error_.clear();
        }
        is_recording_.store(true, std::memory_order_release);
        replay_encoder_->RequestKeyframe();
        std::cout << "[CaptureEngine] Packet-stream recording started." << std::endl;
        return true;
    }

    bool CaptureEngine::StopRecording() {
        is_recording_.store(false, std::memory_order_release);
        std::shared_ptr<ContinuousRecordingWriter> writer;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            writer = std::move(record_writer_);
        }
        if (!writer) return true;

        const bool saved = writer->Stop();
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            last_recording_error_ = saved ? std::string{} : writer->LastError();
            if (!saved && last_recording_error_.empty()) {
                last_recording_error_ =
                    "The recording contained no complete video fragment.";
            }
        }
        std::cout << "[CaptureEngine] Packet-stream recording "
                  << (saved ? "stopped." : "closed with a recoverable error.")
                  << std::endl;
        return saved;
    }

    bool CaptureEngine::IsRecording() const {
        if (!is_recording_.load(std::memory_order_acquire)) return false;
        std::lock_guard<std::mutex> lock(record_writer_mutex_);
        return record_writer_ && record_writer_->IsRunning();
    }

    std::string CaptureEngine::GetLastRecordingError() const {
        std::lock_guard<std::mutex> lock(record_writer_mutex_);
        if (record_writer_ && record_writer_->HasFailed()) {
            const std::string writer_error = record_writer_->LastError();
            if (!writer_error.empty()) return writer_error;
        }
        return last_recording_error_;
    }

    uint64_t CaptureEngine::GetFrameCount() const {
        return frames_captured_.load(std::memory_order_relaxed);
    }


    // SaveClip
    //
    // NVENC path:  TakeSnapshot from encoded ring -> queue encoded task
    // x264 path:   Snapshot raw ring indices -> queue raw task (unchanged)

    bool CaptureEngine::SaveClip(const wchar_t* path, uint32_t duration_seconds,
        SharedMemoryLayout* shared_memory) {

        const auto save_request_started = std::chrono::steady_clock::now();

        const uint32_t health = capture_health_flags_.load();
        if (health & CAPTURE_HEALTH_PAUSED) {
            SetEngineError(shared_memory,
                L"Game capture is paused while the game is in the background. "
                L"Return to the game before saving.");
            return false;
        }
        if (!running_.load() ||
            (health & (CAPTURE_HEALTH_BACKEND_FAILED |
                       CAPTURE_HEALTH_RECOVERING))) {
            SetEngineError(shared_memory,
                L"Capture is not receiving new frames. Restart capture before saving.");
            return false;
        }
        std::cout << "[CaptureEngine] SaveClip: " << duration_seconds << "s" << std::endl;

        LARGE_INTEGER save_qpc{};
        LARGE_INTEGER save_qpc_frequency{};
        QueryPerformanceCounter(&save_qpc);
        QueryPerformanceFrequency(&save_qpc_frequency);
        const double save_qpc_s = save_qpc_frequency.QuadPart > 0
            ? static_cast<double>(save_qpc.QuadPart)
                / static_cast<double>(save_qpc_frequency.QuadPart)
            : 0.0;

        // Disk-spooled replay path: used when the requested duration exceeds the in-memory ring buffer (> 60s)
        {
            std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
            if (duration_seconds > 60 && replay_disk_spooler_ && replay_disk_spooler_->IsActive()) {
                SaveClipTask task;
                task.output_path = path;
                task.duration_seconds = duration_seconds;
                task.use_spooler = true;
                task.shared_memory = shared_memory;
                task.task_id = next_task_id_.fetch_add(1);

                if (!save_clip_queue_.Push(std::move(task))) {
                    SetEngineError(shared_memory,
                        L"The clip save queue is full or shutting down. Wait for "
                        L"the current save to finish, then try again.");
                    return false;
                }
                std::wcout << L"[SaveClip] Disk-spooled task queued: " << path << std::endl;
                return true;
            }
        }

        // NVENC path - mux only, no encoding
        if (nvenc_active_) {
            const auto publish_timeout = std::chrono::milliseconds(
                std::max<uint32_t>(100, 3000 / std::max<uint32_t>(fps_, 1)));
            if (!encoded_ring_->WaitUntilPublished(
                    save_qpc.QuadPart, publish_timeout)) {
                std::cerr << "[SaveClip] Encoder publication did not reach the "
                             "save boundary within "
                          << publish_timeout.count() << "ms; using the latest "
                             "published packet" << std::endl;
            }
            EncodedRingSnapshot snapshot = encoded_ring_->TakeSnapshotByTime(
                duration_seconds, save_qpc.QuadPart);

            if (snapshot.packets.empty()) {
                SetEngineError(shared_memory,
                    L"The replay buffer is rebuilding after a capture transition. "
                    L"Let the game capture a moment of footage, then try again.");
                std::cerr << "[SaveClip] Encoded ring buffer empty - nothing to save" << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"ring_selection_failed\","
                          << "\"state\":\"FAILED\","
                          << "\"error_code\":\"CLIP_SAVE_FAILED\","
                          << "\"packet_count\":0}"
                          << std::endl;
                return false;
            }

            // The encoded ring is selected by time, so a non-empty snapshot is
            // not sufficient proof that it contains a continuous replay. Check
            // the actual packet timeline before handing it to the mux worker;
            // otherwise a handful of stale packets can be expanded into a
            // misleading multi-second clip by downstream CFR processing.
            const auto replay_config = snapshot.video_config;
            replay_health::Input replay_health_input;
            replay_health_input.requested_duration_seconds = duration_seconds;
            replay_health_input.expected_fps =
                replay_config.frame_rate.denominator > 0
                ? static_cast<double>(replay_config.frame_rate.numerator)
                    / static_cast<double>(replay_config.frame_rate.denominator)
                : static_cast<double>(fps_);
            replay_health_input.wall_qpc_frequency = save_qpc_frequency.QuadPart;
            replay_health_input.target_end_wall_qpc = save_qpc.QuadPart;
            replay_health_input.pts_per_second = replay_config.time_base.numerator > 0
                ? static_cast<double>(replay_config.time_base.denominator)
                    / static_cast<double>(replay_config.time_base.numerator)
                : static_cast<double>(std::llround(
                      replay_health_input.expected_fps));
            replay_health_input.full_history = snapshot.full_history;
            replay_health_input.packets.reserve(snapshot.packets.size());
            for (const auto& packet : snapshot.packets) {
                // Decoder keyframe pre-roll is intentionally part of the mux
                // snapshot, but it is not presentation coverage. Excluding it
                // keeps the health decision tied to the requested interval.
                if (packet.pts < snapshot.presentation_start_pts) {
                    continue;
                }
                replay_health_input.packets.push_back({
                    packet.wall_qpc, packet.pts, packet.generation});
            }
            const auto replay_health_report = replay_health::Evaluate(
                replay_health_input);
            if (!replay_health_report.accepted) {
                std::cerr << "[SaveClip] Replay video rejected: "
                          << replay_health::FailureName(
                                 replay_health_report.failure)
                          << ", packets=" << replay_health_report.packet_count
                          << ", coverage=" << replay_health_report.actual_coverage_s
                          << "s, largest_gap=" << replay_health_report.largest_gap_s
                          << "s, rate=" << replay_health_report.effective_packet_rate
                          << " fps" << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"replay_health_rejected\","
                          << "\"state\":\"FAILED\","
                          << "\"error_code\":\"CLIP_REPLAY_VIDEO_INSUFFICIENT\","
                          << "\"reason\":\""
                          << replay_health::FailureName(replay_health_report.failure)
                          << "\",\"packet_count\":"
                          << replay_health_report.packet_count
                          << ",\"requested_duration_seconds\":"
                          << duration_seconds << ",\"actual_coverage_seconds\":"
                          << replay_health_report.actual_coverage_s
                          << ",\"largest_gap_seconds\":"
                          << replay_health_report.largest_gap_s
                          << ",\"effective_packet_rate\":"
                          << replay_health_report.effective_packet_rate
                          << ",\"full_history\":"
                          << (replay_health_report.full_history ? "true" : "false")
                          << ",\"generation_continuous\":"
                          << (replay_health_report.generation_continuous
                              ? "true" : "false") << "}" << std::endl;
                SetEngineError(shared_memory,
                    L"Replay video history is insufficient or discontinuous; "
                    L"the clip was not written. Keep capture running and try again.");
                return false;
            }

            SaveClipTask task;
            task.output_path = path;
            task.duration_seconds = duration_seconds;
            task.use_encoded_path = true;
            task.encoded_snapshot = std::move(snapshot);
            task.enc_width = (target_width_ > 0) ? target_width_ : crop_width_;
            task.enc_height = (target_height_ > 0) ? target_height_ : crop_height_;
            task.fps = fps_;
            task.separate_audio_enabled = separate_audio_enabled_;
            task.shared_memory = shared_memory;
            task.task_id = next_task_id_.fetch_add(1);

            // Populate the QPC epoch so MuxEncodedClip can convert video PTS
            // to wall-clock seconds and align audio to it exactly.
            replay_encoder_->GetEncodeEpoch(
                task.video_qpc_epoch, task.video_qpc_freq);

            // Snapshot the persistent AAC packet ring against the exact video
            // presentation interval. Every provider maps that interval through
            // its own QPC origin, so a missing Default Mix never suppresses a
            // valid microphone or process-loopback stem.
            const double start_qpc_s = task.encoded_snapshot.presentation_start_qpc_s;
            const double end_qpc_s = task.encoded_snapshot.presentation_end_qpc_s;
            if (audio_active_ && default_mix_audio_ring_
                    && !audio_capture_.IsDeviceLost()) {
                const uint64_t origin_qpc = audio_capture_.GetTimelineOriginQpc100ns();
                const uint32_t sample_rate = audio_capture_.GetSampleRate();
                if (origin_qpc > 0 && start_qpc_s > 0.0 && end_qpc_s > start_qpc_s
                        && sample_rate > 0) {
                    const auto range = MapAudioSourcePresentationRange(
                        start_qpc_s, end_qpc_s, origin_qpc, sample_rate);
                    const int64_t start_pts = std::max<int64_t>(0, range.start_pts_samples);
                    const int64_t end_pts = std::max(start_pts + 1, range.end_pts_samples);
                    task.encoded_audio_snapshot = default_mix_audio_ring_->TakeSnapshot(
                        start_pts, end_pts);
                    task.audio_presentation_start_pts_samples = start_pts;
                    task.has_encoded_audio = task.encoded_audio_snapshot.valid();
                    if (task.has_encoded_audio) {
                        EncodedAudioTrack default_mix_track;
                        default_mix_track.source = default_mix_audio_source_;
                        default_mix_track.snapshot = task.encoded_audio_snapshot;
                        default_mix_track.presentation_start_pts_samples = start_pts;
                        // The source interval reflects the packet timeline that
                        // actually survived in this clip, not a guessed requested
                        // duration.  Default Mix is a real aggregate loopback
                        // stream and therefore remains present even for silence.
                        default_mix_track.source.state.first_active_100ns =
                            AudioSamplePositionToTimeline100ns(
                                origin_qpc,
                                default_mix_track.snapshot.first_pts_samples,
                                sample_rate);
                        default_mix_track.source.state.last_active_100ns =
                            AudioSamplePositionToTimeline100ns(
                                origin_qpc,
                                default_mix_track.snapshot.last_pts_samples,
                                sample_rate);
                        task.encoded_audio_tracks.push_back(std::move(default_mix_track));
                        std::cout << "[SaveClip] Persistent AAC snapshot: "
                            << task.encoded_audio_snapshot.packets.size()
                            << " packets, PTS " << start_pts << " - " << end_pts
                            << std::endl;
                    } else {
                        std::cerr << "[SaveClip] Default Mix AAC ring had no packets for "
                            << "presentation PTS " << start_pts << " - " << end_pts
                            << std::endl;
                    }
                } else {
                    std::cerr << "[SaveClip] Default Mix timeline is not ready; origin="
                        << origin_qpc << ", video interval=" << start_qpc_s << " - "
                        << end_qpc_s << ", sample_rate=" << sample_rate << std::endl;
                }
            }
            else if (audio_active_ && audio_capture_.IsDeviceLost()) {
                std::cerr << "[SaveClip] Audio device lost - saving clip without audio" << std::endl;
            }

            if (start_qpc_s > 0.0 && end_qpc_s > start_qpc_s) {
                if (microphone_audio_source_) {
                    if (const auto microphone_track =
                            microphone_audio_source_->TakeTrackForInterval(
                                start_qpc_s, end_qpc_s, microphone_audio_metadata_)) {
                        task.encoded_audio_tracks.push_back(*microphone_track);
                    } else {
                        const std::string detail = microphone_audio_source_->last_error();
                        if (!detail.empty()) {
                            std::cerr << "[SaveClip] Native microphone unavailable: "
                                      << detail << std::endl;
                        }
                    }
                }
                if (separate_audio_enabled_ && application_audio_source_manager_) {
                    const auto application_tracks =
                        application_audio_source_manager_->TakeTracksForInterval(
                            start_qpc_s, end_qpc_s);
                    for (const auto& application_track : application_tracks)
                        task.encoded_audio_tracks.push_back(application_track);
                    const auto application_info =
                        application_audio_source_manager_->runtime_info();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_snapshot\","
                              << "\"state\":\"SNAPSHOTTED\","
                              << "\"track_count\":" << application_tracks.size()
                              << ",\"candidate_count\":"
                              << application_info.candidate_count
                              << ",\"packet_count\":" << application_info.packet_count
                              << ",\"admitted_source_count\":"
                              << application_info.admitted_source_count
                              << ",\"encoded_packets\":"
                              << application_info.encoded_packets
                              << ",\"encode_failures\":"
                              << application_info.encode_failures
                              << ",\"finalize_failures\":"
                              << application_info.finalize_failures
                              << ",\"dropped_blocks\":"
                              << application_info.dropped_blocks
                              << ",\"no_packet_intervals\":"
                              << application_info.no_packet_intervals
                              << ",\"largest_no_packet_gap_100ns\":"
                              << application_info.largest_no_packet_gap_100ns
                              << ",\"discontinuities\":"
                              << application_info.discontinuity_count
                              << ",\"source_limit_rejections\":"
                              << application_info.source_limit_rejections
                              << ",\"largest_gap_100ns\":"
                              << application_info.largest_gap_100ns << "}"
                              << std::endl;
                }
            }

            const uint32_t diagnostic_task_id = task.task_id;
            const size_t diagnostic_video_packets =
                task.encoded_snapshot.packets.size();
            const size_t diagnostic_audio_tracks =
                task.encoded_audio_tracks.size();
            if (!save_clip_queue_.Push(std::move(task))) {
                SetEngineError(shared_memory,
                    L"The clip save queue is full or shutting down. Wait for "
                    L"the current save to finish, then try again.");
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"save_queue_rejected\","
                          << "\"state\":\"FAILED\","
                          << "\"error_code\":\"CLIP_SAVE_FAILED\","
                          << "\"queue_depth\":"
                          << save_clip_queue_.GetQueueDepth() << ','
                          << "\"queue_capacity\":"
                          << save_clip_queue_.GetCapacity() << "}"
                          << std::endl;
                return false;
            }
            const auto selection_ms = std::chrono::duration_cast<
                std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - save_request_started).count();
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"ring_selection_completed\","
                      << "\"state\":\"QUEUED\","
                      << "\"task_id\":" << diagnostic_task_id << ','
                      << "\"requested_duration_seconds\":" << duration_seconds << ','
                      << "\"video_packet_count\":"
                      << diagnostic_video_packets << ','
                      << "\"audio_track_count\":"
                      << diagnostic_audio_tracks << ','
                      << "\"elapsed_ms\":" << selection_ms << "}"
                      << std::endl;
            std::wcout << L"[SaveClip] Encoded task queued: " << path << std::endl;
            return true;
        }

        // Legacy raw replay: snapshot frame-pool indices.
        const size_t needed = static_cast<size_t>(duration_seconds) * fps_;

        size_t snap_head = 0;
        size_t snap_count = 0;
        {
            std::lock_guard<std::mutex> lock(ring_mutex_);
            snap_head = ring_head_.load(std::memory_order_relaxed);
            snap_count = ring_count_.load(std::memory_order_relaxed);
        }

        if (snap_count == 0) {
            std::cerr << "[SaveClip] Ring buffer is empty" << std::endl;
            return false;
        }

        const size_t safety_frames = fps_;
        const auto raw_selection = replay_interval::SelectFixedRateFrames(
            snap_head, snap_count, needed, safety_frames);
        const size_t frames_to_encode = raw_selection.frame_count;
        const size_t start_pos = static_cast<size_t>(raw_selection.decode_start);

        if (frames_to_encode < needed) {
            std::cerr << "[SaveClip] Only " << frames_to_encode << " frames available "
                << "(requested " << needed << ")" << std::endl;
        }

        SaveClipTask task;
        task.output_path = path;
        task.duration_seconds = duration_seconds;
        task.use_encoded_path = false;
        task.start_frame_idx = start_pos % max_frames_;
        task.frame_count = frames_to_encode;
        task.src_width = crop_width_;
        task.src_height = crop_height_;
        task.enc_width = target_width_;
        task.enc_height = target_height_;
        task.fps = fps_;
        task.bitrate_kbps = bitrate_kbps_;
        task.scaling_mode = scaling_mode_;
        task.separate_audio_enabled = separate_audio_enabled_;
        task.shared_memory = shared_memory;
        task.task_id = next_task_id_.fetch_add(1);

        // The public alpha does not automatically select the raw-video path.
        // Preserve its video-only behavior instead of reintroducing a long
        // raw PCM ring solely for a non-production fallback.
        if (audio_active_ && audio_capture_.IsDeviceLost()) {
            std::cerr << "[SaveClip] Audio device lost - saving clip without audio" << std::endl;
        }

        if (!save_clip_queue_.Push(std::move(task))) {
            SetEngineError(shared_memory,
                L"The clip save queue is full or shutting down. Wait for the "
                L"current save to finish, then try again.");
            return false;
        }
        std::wcout << L"[SaveClip] Raw task queued: " << path << std::endl;
        return true;
    }


    // SaveClipThread - unchanged structure, ProcessSaveClipTask branches internally

    void CaptureEngine::SaveClipThread() {
        std::cout << "[SaveClipThread] Started." << std::endl;

        while (true) {
            SaveClipTask task;
            if (!save_clip_queue_.Pop(task)) break;

            std::wcout << L"[SaveClipThread] Processing task " << task.task_id
                << L": " << task.output_path << std::endl;

            const bool ok = ProcessSaveClipTask(task);

            // Failures already publish SetEngineError(). Publish success only after
            // ProcessSaveClipTask confirms the file was written.
            if (ok && task.shared_memory) {
                // Clear the message channel before announcing success so a clip
                // that follows a failed one cannot carry the old error text.
                SetEngineString(task.shared_memory, L"");
                task.shared_memory->engine_response = ResponseType::CLIP_SAVED;
            }
        }

        std::cout << "[SaveClipThread] Exiting." << std::endl;
    }


    // ProcessSaveClipTask - transactional wrapper around both media writers

    bool CaptureEngine::ProcessSaveClipTask(const SaveClipTask& task) {
        if (task.use_spooler) {
            std::wcout << L"[ProcessSaveClipTask] Merging disk-spooled replay segments to: "
                       << task.output_path << std::endl;
            const auto result = transactional_save::Run(
                task.output_path,
                [this, &task](const std::filesystem::path& temporary_path,
                              std::string& writer_error) {
                    bool ok = false;
                    ReplayDiskSpooler* spooler = nullptr;
                    {
                        std::lock_guard<std::mutex> lock(replay_disk_spooler_mutex_);
                        spooler = replay_disk_spooler_.get();
                    }
                    if (spooler) {
                        ok = spooler->SaveClip(
                            temporary_path.wstring(), task.duration_seconds);
                    }
                    if (!ok) {
                        writer_error = "The disk-spooled replay segments could not be merged.";
                        return false;
                    }
                    return true;
                });
            if (!result.success) {
                const std::string detail = transactional_save::DescribeFailure(result);
                std::cerr << "[ProcessSaveClipTask] Spooler save failed: " << detail << std::endl;
                SetEngineError(task.shared_memory, L"Failed to merge replay segments.");
                return false;
            }
            std::wcout << L"[ProcessSaveClipTask] Disk-spooled replay clip successfully saved." << std::endl;
            return true;
        }

        if (task.use_encoded_path && !task.encoded_audio_tracks.empty()) {
            std::string transaction_id;
            std::string transaction_error;
            if (!CreateAudioManifestTransactionId(&transaction_id, &transaction_error)) {
                SetEngineError(task.shared_memory,
                    L"Could not prepare the audio manifest transaction for this clip.");
                std::cerr << "[SaveClip] " << transaction_error << std::endl;
                return false;
            }
            const auto manifest_path = AudioManifestPathFor(task.output_path);
            const auto result = transactional_save::RunPair(
                task.output_path,
                manifest_path,
                [this, &task, &transaction_id](
                    const std::filesystem::path& media_temporary_path,
                    const std::filesystem::path& manifest_temporary_path,
                    std::string& writer_error) {
                    if (!MuxEncodedClip(task, media_temporary_path.wstring())) {
                        writer_error = "The temporary clip could not be muxed.";
                        return false;
                    }
                    return WriteClipAudioManifest(
                        task.output_path,
                        media_temporary_path,
                        manifest_temporary_path,
                        transaction_id,
                        task.encoded_audio_tracks,
                        &writer_error);
                });
            if (result.cleanup_error) {
                std::wcerr << L"[SaveClip] Secondary paired cleanup failure for "
                    << result.media_temporary_path.wstring() << L": "
                    << result.cleanup_error.value() << std::endl;
            }
            if (result.success) return true;
            if (result.failure != transactional_save::PairFailure::Writer
                    || !result.writer_error.empty()) {
                const std::string detail = transactional_save::DescribeFailure(result);
                const int required = MultiByteToWideChar(
                    CP_UTF8, 0, detail.c_str(), -1, nullptr, 0);
                std::wstring wide_detail;
                if (required > 0) {
                    wide_detail.resize(static_cast<size_t>(required));
                    MultiByteToWideChar(CP_UTF8, 0, detail.c_str(), -1,
                        wide_detail.data(), required);
                }
                SetEngineError(task.shared_memory,
                    wide_detail.empty()
                        ? L"The clip and its audio manifest could not be finalized."
                        : wide_detail.c_str());
            }
            return false;
        }

        const auto result = transactional_save::Run(
            task.output_path,
            [this, &task](const std::filesystem::path& temporary_path,
                          std::string&) {
                const std::wstring output_path = temporary_path.wstring();
                return task.use_encoded_path
                    ? MuxEncodedClip(task, output_path)
                    : EncodeRawClip(task, output_path);
            });

        if (result.cleanup_error) {
            std::wcerr << L"[SaveClip] Secondary cleanup failure for "
                << result.temporary_path.wstring() << L": "
                << result.cleanup_error.value() << std::endl;
        }

        if (result.success) return true;

        // Normal writer failures already published their precise FFmpeg/encoder
        // diagnostic. Preflight/rename failures happen outside the writer and
        // therefore need a message here. A thrown writer exception also carries
        // text in writer_error and is published here.
        if (result.failure != transactional_save::Failure::Writer
                || !result.writer_error.empty()) {
            const std::string detail = transactional_save::DescribeFailure(result);
            const int required = MultiByteToWideChar(
                CP_UTF8, 0, detail.c_str(), -1, nullptr, 0);
            std::wstring wide_detail;
            if (required > 0) {
                wide_detail.resize(static_cast<size_t>(required));
                MultiByteToWideChar(
                    CP_UTF8, 0, detail.c_str(), -1,
                    wide_detail.data(), required);
            }
            SetEngineError(
                task.shared_memory,
                wide_detail.empty()
                    ? L"The temporary clip could not be finalized."
                    : wide_detail.c_str());
        }
        return false;
    }


    // Mux replay packets and their codec configuration directly into MP4.
    // Rescale packet timestamps to the output stream timebase without re-encoding.

    bool CaptureEngine::MuxEncodedClip(
        const SaveClipTask& task, const std::wstring& output_path) {
        struct MuxDiagnosticSpan {
            uint32_t task_id;
            std::chrono::steady_clock::time_point started;
            bool success = false;
            ~MuxDiagnosticSpan() {
                const auto elapsed = std::chrono::duration_cast<
                    std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - started).count();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"native_mux_completed\","
                          << "\"state\":\"" << (success ? "COMPLETED" : "FAILED")
                          << "\",\"error_code\":"
                          << (success ? "null" : "\"CLIP_SAVE_FAILED\"") << ','
                          << "\"task_id\":" << task_id << ','
                          << "\"elapsed_ms\":" << elapsed << "}"
                          << std::endl;
            }
        } mux_diagnostic{task.task_id, std::chrono::steady_clock::now()};
        const auto& snap = task.encoded_snapshot;
        const auto& video_config = snap.video_config;

        if (!IsValidEncodedVideoConfig(video_config)
            || ToAvCodecId(video_config.codec) == AV_CODEC_ID_NONE) {
            std::cerr << "[MuxEncodedClip] Invalid encoded video config" << std::endl;
            SetEngineError(task.shared_memory,
                L"The replay stream configuration is invalid; the clip was not written.");
            return false;
        }
        const double video_tick_seconds =
            static_cast<double>(video_config.time_base.numerator)
            / static_cast<double>(video_config.time_base.denominator);
        const double video_fps =
            static_cast<double>(video_config.frame_rate.numerator)
            / static_cast<double>(video_config.frame_rate.denominator);
        const double video_frame_seconds = 1.0 / video_fps;

        if (snap.packets.empty()) {
            std::cerr << "[MuxEncodedClip] No packets in snapshot" << std::endl;
            SetEngineError(task.shared_memory,
                L"Nothing to save: the replay buffer held no video frames. "
                L"Let the engine capture for a few seconds before saving.");
            return false;
        }

        // Require a keyframe at the decode start, including for legacy snapshots.
        size_t keyframe_start = snap.packets.size();  // sentinel = not found
        for (size_t i = 0; i < snap.packets.size(); i++) {
            if (!snap.packets[i].data.empty() && snap.packets[i].is_keyframe) {
                keyframe_start = i;
                break;
            }
        }

        if (keyframe_start == snap.packets.size()) {
            std::cerr << "[MuxEncodedClip] WARNING: no keyframe found - "
                << "writing all packets (clip may not decode correctly)" << std::endl;
            keyframe_start = 0;
        }

        // Timestamp-aware snapshots already retain the keyframe before the requested
        // start. Only legacy snapshots need duration trimming by PTS span; packet
        // count does not measure elapsed time when capture cadence varies.
        if (!snap.packets.empty() && snap.presentation_start_qpc_s <= 0.0) {
            const int64_t max_pts_span = DurationInVideoTicks(
                video_config, task.duration_seconds);
            const int64_t newest_pts = snap.packets.back().pts;
            const int64_t cutoff_pts = newest_pts - max_pts_span;

            if (snap.packets[keyframe_start].pts < cutoff_pts) {
                // Current start is outside the time budget. Advance to the
                // earliest keyframe at or after cutoff_pts.
                size_t trimmed = snap.packets.size();  // sentinel
                for (size_t i = keyframe_start + 1; i < snap.packets.size(); i++) {
                    if (snap.packets[i].pts >= cutoff_pts && snap.packets[i].is_keyframe) {
                        trimmed = i;
                        break;
                    }
                }
                if (trimmed < snap.packets.size()) {
                    const size_t frames_discarded = trimmed - keyframe_start;
                    keyframe_start = trimmed;
                    std::cout << "[MuxEncodedClip] PTS trim: discarded "
                        << frames_discarded << " leading frames, "
                        << "keeping last " << task.duration_seconds
                        << "s of footage" << std::endl;
                }
            }
        }

        // Only legacy snapshots trim video to raw-audio coverage using the shared
        // QPC clock. Timestamp-aware saves retain the full video interval and allow
        // shorter or late audio instead of discarding valid footage.
        if (snap.presentation_start_qpc_s <= 0.0
            && task.audio_snapshot.valid
            && task.audio_snapshot.qpc_start_s > 0.0
            && keyframe_start < snap.packets.size()
            && (snap.qpc_start_s > 0.0 || (task.video_qpc_epoch != 0 && task.video_qpc_freq != 0))) {

            const double T_epoch_s = (task.video_qpc_freq != 0)
                ? static_cast<double>(task.video_qpc_epoch)
                  / static_cast<double>(task.video_qpc_freq)
                : 0.0;

            // Wall-clock time of the current video start (keyframe_start).
            // Prefer per-packet QPC if available, fall back to epoch + PTS.
            double video_start_wall_s = 0.0;
            if (snap.packets[keyframe_start].wall_qpc > 0 && task.video_qpc_freq > 0) {
                video_start_wall_s = static_cast<double>(snap.packets[keyframe_start].wall_qpc)
                    / static_cast<double>(task.video_qpc_freq);
            } else {
                video_start_wall_s = T_epoch_s
                    + static_cast<double>(snap.packets[keyframe_start].pts)
                    * video_tick_seconds;
            }

            if (video_start_wall_s < task.audio_snapshot.qpc_start_s) {
                // Video reaches further back than audio. Advance keyframe_start
                // to the first keyframe whose wall-clock time >= audio start.
                const double audio_start_wall_s = task.audio_snapshot.qpc_start_s;
                const int64_t trim_pts = static_cast<int64_t>(
                    (audio_start_wall_s - T_epoch_s)
                    / video_tick_seconds + 0.5);

                size_t new_kf = snap.packets.size(); // sentinel = not found
                for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                    if (snap.packets[i].pts >= trim_pts && snap.packets[i].is_keyframe) {
                        new_kf = i;
                        break;
                    }
                }
                if (new_kf < snap.packets.size()) {
                    const size_t trimmed_frames = new_kf - keyframe_start;
                    keyframe_start = new_kf;
                    std::cout << "[MuxEncodedClip] Audio-range trim: discarded "
                        << trimmed_frames << " leading video frames ("
                        << (static_cast<double>(trimmed_frames) / video_fps) << "s) "
                        << "— audio ring only covers from "
                        << (audio_start_wall_s - T_epoch_s) << "s" << std::endl;
                }
                else {
                    std::cerr << "[MuxEncodedClip] Audio-range trim: no keyframe found "
                        "after audio start (" << (audio_start_wall_s - T_epoch_s)
                        << "s into recording) — clip may have audio gap at start"
                        << std::endl;
                }
            }
        }

        // Subtract the presentation boundary, preserving negative PTS for decoder
        // pre-roll. The MP4 edit list hides that pre-roll without re-encoding.

        // Legacy raw PCM remains only for the non-production compatibility path.
        // The normal AUDIT-050 contract carries one Default Mix plus actual
        // source tracks in their own persistent AAC packet snapshots.
        const bool write_legacy_audio = task.has_audio
            && task.audio_snapshot.valid
            && !task.audio_snapshot.samples.empty();
        const bool has_contract_audio_tracks = !task.encoded_audio_tracks.empty();
        const bool write_packet_audio = has_contract_audio_tracks
            || (task.has_encoded_audio && task.encoded_audio_snapshot.valid());

        const int64_t pts_offset = snap.presentation_start_pts;

        const size_t usable_count = snap.packets.size() - keyframe_start;

        // Trimmed video clip duration in seconds.
        // Keep the source wall-clock span. A sparse capture must never be
        // shortened by converting its packet count directly into duration.
        const int64_t newest_video_pts     = snap.packets.back().pts;
        const int64_t video_clip_pts_span  = newest_video_pts - pts_offset;
        const double video_clip_duration_s =
            static_cast<double>(video_clip_pts_span + 1) * video_tick_seconds;

        // Intersect the PCM snapshot with the video presentation interval using QPC.
        // If QPC timing is unavailable, align video_duration seconds from the audio end.

        int64_t audio_aligned_start_sample = 0;
        int64_t audio_output_pts_offset = 0;

        if (write_legacy_audio) {
            const int64_t total_snap_frames = static_cast<int64_t>(
                task.audio_snapshot.samples.size())
                / static_cast<int64_t>(task.audio_snapshot.channels);

            bool qpc_aligned = false;

            // Both timestamps share the QPC clock: video uses raw ticks / qpc_freq;
            // WASAPI uses 100 ns units / 10,000,000. Match the first video packet
            // after trimming to the corresponding audio sample.

            // Presentation starts at the requested cutoff, not at the older
            // keyframe retained solely for decoder pre-roll.
            double video_start_wall_s = 0.0;
            bool have_video_wall_time = false;

            if (snap.presentation_start_qpc_s > 0.0) {
                video_start_wall_s = snap.presentation_start_qpc_s;
                have_video_wall_time = true;
            }
            // Legacy fallback: use per-packet QPC from the physical first packet.
            else if (snap.qpc_start_s > 0.0) {
                // snap.qpc_start_s is from the FULL snapshot. We need the QPC of
                // the first packet after keyframe_start trimming.
                for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                    if (!snap.packets[i].data.empty() && snap.packets[i].wall_qpc > 0) {
                        // Convert raw QPC ticks to seconds using the same freq
                        // that was used in the snapshot (stored in qpc_start_s derivation)
                        if (task.video_qpc_freq > 0) {
                            video_start_wall_s = static_cast<double>(snap.packets[i].wall_qpc)
                                / static_cast<double>(task.video_qpc_freq);
                        } else {
                            // qpc_freq not available from encoder, derive from snapshot ratio
                            // snap.qpc_start_s was computed by EncodedRingBuffer using its qpc_freq_
                            // Use the first packet's wall_qpc with the snapshot's known conversion
                            video_start_wall_s = snap.qpc_start_s;
                        }
                        have_video_wall_time = true;
                        break;
                    }
                }
            }

            // Method 2: Fall back to epoch + PTS derivation
            if (!have_video_wall_time
                && task.video_qpc_epoch != 0 && task.video_qpc_freq != 0) {
                video_start_wall_s =
                    static_cast<double>(task.video_qpc_epoch)
                        / static_cast<double>(task.video_qpc_freq)
                    + static_cast<double>(pts_offset)
                        * video_tick_seconds;
                have_video_wall_time = true;
            }

            // Compute video end wall time (for overlap calculation)
            double video_end_wall_s = 0.0;
            if (have_video_wall_time) {
                video_end_wall_s = video_start_wall_s + video_clip_duration_s;
            }

            if (have_video_wall_time && task.audio_snapshot.qpc_start_s > 0.0) {
                const double overlap_start_s = std::max(
                    video_start_wall_s, task.audio_snapshot.qpc_start_s);
                const double overlap_end_s = std::min(
                    video_end_wall_s, task.audio_snapshot.qpc_end_s);

                if (overlap_start_s < overlap_end_s) {
                    audio_aligned_start_sample = static_cast<int64_t>(
                        (overlap_start_s - task.audio_snapshot.qpc_start_s)
                        * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);
                    audio_output_pts_offset = static_cast<int64_t>(
                        (overlap_start_s - video_start_wall_s)
                        * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);
                    audio_aligned_start_sample = std::clamp<int64_t>(
                        audio_aligned_start_sample, 0, total_snap_frames);
                    qpc_aligned = true;

                    std::cout << "[MuxEncodedClip] A/V sync (QPC overlap):" << std::endl;
                    std::cout << "  Video start wall time    : " << video_start_wall_s << "s" << std::endl;
                    std::cout << "  Video end wall time      : " << video_end_wall_s << "s" << std::endl;
                    std::cout << "  Audio snap QPC start     : " << task.audio_snapshot.qpc_start_s << "s" << std::endl;
                    std::cout << "  Audio snap QPC end       : " << task.audio_snapshot.qpc_end_s << "s" << std::endl;
                    std::cout << "  Audio overlap start      : " << overlap_start_s << "s" << std::endl;
                    std::cout << "  Audio snap frames        : " << total_snap_frames << std::endl;
                    std::cout << "  Audio start sample       : " << audio_aligned_start_sample
                        << " (skip " << (static_cast<double>(audio_aligned_start_sample)
                            / task.audio_snapshot.sample_rate) << "s)" << std::endl;
                    std::cout << "  Audio timeline offset    : " << audio_output_pts_offset
                        << " samples" << std::endl;
                }
                else {
                    audio_aligned_start_sample = total_snap_frames;
                    qpc_aligned = true;
                    std::cerr << "[MuxEncodedClip] Audio has no overlap with the "
                        "requested video interval; writing video-only" << std::endl;
                }
            }

            // Fallback: end-aligned duration-based
            // Used only when QPC data is completely unavailable.
            // Takes video_duration worth of audio from the end of the snapshot.
            if (!qpc_aligned) {
                const int64_t frames_needed = static_cast<int64_t>(
                    video_clip_duration_s
                    * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);

                audio_aligned_start_sample = total_snap_frames - frames_needed;
                if (audio_aligned_start_sample < 0)
                    audio_aligned_start_sample = 0;
                if (audio_aligned_start_sample > total_snap_frames)
                    audio_aligned_start_sample = total_snap_frames;

                std::cout << "[MuxEncodedClip] A/V sync (end-aligned fallback):" << std::endl;
                std::cout << "  Video clip duration      : " << video_clip_duration_s << "s" << std::endl;
                std::cout << "  Audio snap frames        : " << total_snap_frames << std::endl;
                std::cout << "  Audio start frame        : " << audio_aligned_start_sample
                    << " (skip " << (static_cast<double>(audio_aligned_start_sample)
                        / task.audio_snapshot.sample_rate) << "s)" << std::endl;
            }
        }

        // Convert output path to UTF-8.
        // UTF-16 -> UTF-8 worst-case expansion is 3x; MAX_PATH is 260 chars but
        // long-path-aware builds can exceed that. 1024 bytes covers typical paths.
        char output_utf8[1024] = {};
        WideCharToMultiByte(CP_UTF8, 0, output_path.c_str(), -1,
            output_utf8, sizeof(output_utf8) - 1, nullptr, nullptr);

        AVFormatContext* fmt_ctx = nullptr;
        avformat_alloc_output_context2(&fmt_ctx, nullptr, "mp4", output_utf8);
        if (!fmt_ctx) {
            std::cerr << "[MuxEncodedClip] avformat_alloc_output_context2 failed" << std::endl;
            SetEngineError(task.shared_memory,
                L"Failed to create the output file container. The clip path may "
                L"be invalid or on an unsupported filesystem.");
            return false;
        }
        fmt_ctx->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;

        AVStream* video_stream = avformat_new_stream(fmt_ctx, nullptr);
        if (!video_stream) {
            std::cerr << "[MuxEncodedClip] avformat_new_stream (video) failed" << std::endl;
            avformat_free_context(fmt_ctx);
            SetEngineError(task.shared_memory,
                L"Failed to create the video stream in the output file.");
            return false;
        }

        video_stream->codecpar->codec_type = AVMEDIA_TYPE_VIDEO;
        video_stream->codecpar->codec_id = ToAvCodecId(video_config.codec);
        video_stream->codecpar->width = static_cast<int>(video_config.width);
        video_stream->codecpar->height = static_cast<int>(video_config.height);
        video_stream->codecpar->format = AV_PIX_FMT_YUV420P;
        ApplySdrBt709ColorMetadata(video_stream->codecpar);

        // High-resolution MP4 stream timebase; packet timestamps are rescaled
        // from the encoder-provided time base below.
        video_stream->time_base = AVRational{ 1, 90000 };
        ApplyConfiguredVideoMetadata(fmt_ctx, video_stream, video_config);

        // Copy the codec's decoder configuration record (avcC/hvcC/av1C).
        if (!video_config.codec_extradata.empty()) {
            video_stream->codecpar->extradata = static_cast<uint8_t*>(
                av_malloc(video_config.codec_extradata.size()
                    + AV_INPUT_BUFFER_PADDING_SIZE));
            memcpy(video_stream->codecpar->extradata,
                video_config.codec_extradata.data(),
                video_config.codec_extradata.size());
            memset(video_stream->codecpar->extradata
                    + video_config.codec_extradata.size(),
                0, AV_INPUT_BUFFER_PADDING_SIZE);
            video_stream->codecpar->extradata_size =
                static_cast<int>(video_config.codec_extradata.size());
        }
        else {
            std::cerr << "[MuxEncodedClip] WARNING: no video extradata - "
                << "file may not play everywhere" << std::endl;
        }

        // Legacy raw-PCM saves encode aligned audio to AAC on the save thread.
        // Add the audio stream and encoder ASC before avformat_write_header().
        struct MuxAudioTrack {
            AudioSourceMetadata metadata;
            AVStream* stream = nullptr;
            std::vector<std::vector<uint8_t>> packets;
            std::vector<int64_t> packet_pts;
            std::vector<int64_t> packet_durations;
            std::vector<uint8_t> extradata;
            uint32_t sample_rate = 0;
            uint32_t channels = 0;
            int64_t output_pts_offset = 0;
            bool is_default_mix = false;
        };
        std::vector<MuxAudioTrack> mux_audio_tracks;

        const auto append_persistent_track = [&](const EncodedAudioTrack& contract,
                                                  bool is_default_mix) {
            MuxAudioTrack output;
            output.metadata = contract.source;
            output.sample_rate = contract.snapshot.format.sample_rate;
            output.channels = contract.snapshot.format.channels;
            output.extradata = contract.snapshot.codec_extradata;
            output.output_pts_offset = -contract.presentation_start_pts_samples;
            output.is_default_mix = is_default_mix;
            for (const auto& packet : contract.snapshot.packets) {
                if (packet.data.empty() || packet.duration_samples <= 0) continue;
                output.packets.push_back(packet.data);
                output.packet_pts.push_back(packet.pts_samples);
                output.packet_durations.push_back(packet.duration_samples);
            }
            if (!output.packets.empty()) mux_audio_tracks.push_back(std::move(output));
        };

        if (has_contract_audio_tracks) {
            if (task.encoded_audio_tracks.size() > kMaxClipAudioTracks) {
                SetEngineError(task.shared_memory,
                    L"The clip contains more audio tracks than the supported alpha limit.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            for (size_t index = 0; index < task.encoded_audio_tracks.size(); ++index) {
                const auto& contract = task.encoded_audio_tracks[index];
                if (!contract.valid()) {
                    SetEngineError(task.shared_memory,
                        L"An audio track no longer matches this capture generation.");
                    avformat_free_context(fmt_ctx);
                    return false;
                }
                const bool is_default_mix = index == 0;
                if (is_default_mix
                        && (contract.source.identity.type != AudioSourceType::System
                            || contract.source.identity.persistent_identity != "default-mix"
                            || contract.source.identity.display_name != "Default Mix")) {
                    SetEngineError(task.shared_memory,
                        L"The Default Mix track is missing from the audio capture contract.");
                    avformat_free_context(fmt_ctx);
                    return false;
                }
                append_persistent_track(contract, is_default_mix);
            }
            if (mux_audio_tracks.empty()) {
                SetEngineError(task.shared_memory,
                    L"No valid AAC packets were available for the captured audio tracks.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            std::cout << "[MuxEncodedClip] Using persistent AAC replay: "
                << mux_audio_tracks.size() << " track(s)" << std::endl;
        }
        else if (write_packet_audio) {
            // Compatibility for a queued task created before the structured
            // contract was introduced. New saves always use the branch above.
            EncodedAudioTrack legacy;
            legacy.snapshot = task.encoded_audio_snapshot;
            legacy.presentation_start_pts_samples = task.audio_presentation_start_pts_samples;
            legacy.source.identity.id = legacy.snapshot.source_id;
            legacy.source.identity.type = AudioSourceType::System;
            legacy.source.identity.persistent_identity = "default-mix";
            legacy.source.identity.display_name = "Default Mix";
            legacy.source.identity.icon_reference = "system-audio";
            legacy.source.format = legacy.snapshot.format;
            legacy.source.state.admitted = true;
            legacy.source.state.first_active_100ns = 0;
            legacy.source.state.last_active_100ns = 0;
            append_persistent_track(legacy, true);
        }
        else if (write_legacy_audio) {
            MuxAudioTrack output;
            const uint32_t sr = task.audio_snapshot.sample_rate;
            const uint32_t ch = task.audio_snapshot.channels;
            output.sample_rate = sr;
            output.channels = ch;
            output.is_default_mix = true;
            AudioEncoder aac_enc;
            bool enc_ok = aac_enc.Initialize(
                sr, ch, task.audio_bitrate_kbps,
                [&](const uint8_t* data, uint32_t size, int64_t pts) {
                    output.packets.push_back(std::vector<uint8_t>(data, data + size));
                    output.packet_pts.push_back(pts);
                    output.packet_durations.push_back(1024);
                });
            if (enc_ok) {
                const int64_t total_frames = static_cast<int64_t>(
                    task.audio_snapshot.samples.size()) / static_cast<int64_t>(ch);
                const int64_t frames_to_encode = total_frames - audio_aligned_start_sample;
                if (frames_to_encode > 0) {
                    const float* pcm_start = task.audio_snapshot.samples.data()
                        + static_cast<size_t>(audio_aligned_start_sample) * ch;
                    aac_enc.EncodeSamples(pcm_start,
                        static_cast<uint32_t>(frames_to_encode) * ch);
                }
                aac_enc.Finalize();
                output.extradata = aac_enc.GetExtradata();
                output.output_pts_offset = audio_output_pts_offset;
                if (!output.packets.empty()) mux_audio_tracks.push_back(std::move(output));
            }
        }

        for (auto& audio : mux_audio_tracks) {
            audio.stream = avformat_new_stream(fmt_ctx, nullptr);
            if (!audio.stream) {
                SetEngineError(task.shared_memory,
                    L"Could not create the clip audio stream.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            audio.stream->codecpar->codec_type = AVMEDIA_TYPE_AUDIO;
            audio.stream->codecpar->codec_id = AV_CODEC_ID_AAC;
            audio.stream->codecpar->sample_rate = static_cast<int>(audio.sample_rate);
            audio.stream->codecpar->ch_layout.nb_channels = static_cast<int>(audio.channels);
            audio.stream->codecpar->ch_layout.order = AV_CHANNEL_ORDER_UNSPEC;
            audio.stream->codecpar->frame_size = 1024;
            audio.stream->codecpar->format = AV_SAMPLE_FMT_FLTP;
            audio.stream->time_base = AVRational{1, static_cast<int>(audio.sample_rate)};
            if (audio.is_default_mix) audio.stream->disposition |= AV_DISPOSITION_DEFAULT;
            const std::string name = audio.is_default_mix
                ? "Default Mix" : audio.metadata.identity.display_name;
            av_dict_set(&audio.stream->metadata, "handler_name", name.c_str(), 0);
            av_dict_set(&audio.stream->metadata, "title", name.c_str(), 0);
            if (!audio.extradata.empty()) {
                audio.stream->codecpar->extradata = static_cast<uint8_t*>(
                    av_malloc(audio.extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
                if (!audio.stream->codecpar->extradata) {
                    SetEngineError(task.shared_memory,
                        L"Could not allocate clip audio stream metadata.");
                    avformat_free_context(fmt_ctx);
                    return false;
                }
                memcpy(audio.stream->codecpar->extradata,
                    audio.extradata.data(), audio.extradata.size());
                memset(audio.stream->codecpar->extradata + audio.extradata.size(),
                    0, AV_INPUT_BUFFER_PADDING_SIZE);
                audio.stream->codecpar->extradata_size = static_cast<int>(audio.extradata.size());
            }
        }

        // The native publication is source-preserving. Combined mode is
        // collapsed by the UI after the file is committed; this marker keeps
        // an interrupted/failing finalization truthful about the current MP4
        // topology so the editor never invents a single-track interpretation.
        const bool native_audio_is_separated = task.separate_audio_enabled
            || mux_audio_tracks.size() > 1;
        av_dict_set(&fmt_ctx->metadata, "comment",
            native_audio_is_separated
                ? "fthr-audio-mode=separated"
                : "fthr-audio-mode=combined", 0);

        int ret = avio_open(&fmt_ctx->pb, output_utf8, AVIO_FLAG_WRITE);
        if (ret < 0) {
            std::cerr << "[MuxEncodedClip] avio_open failed: " << ret << std::endl;
            avformat_free_context(fmt_ctx);
            {
                // avio_open is the disk-full / read-only / path-missing case —
                // the single most common real failure. Carry the errno-style
                // code so a bug report can name it.
                wchar_t msg[512];
                _snwprintf_s(msg, _TRUNCATE,
                    L"Could not open the clip file for writing (error %d). "
                    L"Check free disk space and folder permissions.", ret);
                SetEngineError(task.shared_memory, msg);
            }
            return false;
        }

        AVDictionary* output_options = nullptr;
        av_dict_set(&output_options, "movflags", "use_metadata_tags", 0);
        ret = avformat_write_header(fmt_ctx, &output_options);
        av_dict_free(&output_options);
        if (ret < 0) {
            std::cerr << "[MuxEncodedClip] avformat_write_header failed: " << ret << std::endl;
            avio_closep(&fmt_ctx->pb);
            avformat_free_context(fmt_ctx);
            {
                wchar_t msg[512];
                _snwprintf_s(msg, _TRUNCATE,
                    L"Failed to write the clip file header (error %d).", ret);
                SetEngineError(task.shared_memory, msg);
            }
            return false;
        }

        const AVRational encode_tb = {
            video_config.time_base.numerator,
            video_config.time_base.denominator};

        std::cout << "\n========================================" << std::endl;
        std::cout << "[MuxEncodedClip] DIAGNOSTIC INFO" << std::endl;
        std::cout << "========================================" << std::endl;
        std::cout << "Total packets in snapshot : " << snap.packets.size() << std::endl;
        std::cout << "Keyframe start index      : " << keyframe_start << std::endl;
        std::cout << "Usable packets            : " << usable_count << std::endl;
        std::cout << "Presentation PTS boundary : " << pts_offset
            << " (" << (static_cast<double>(pts_offset) * video_tick_seconds)
            << "s absolute)" << std::endl;
        std::cout << "History classification   : "
            << (snap.full_history ? "full" : "partial") << std::endl;
        std::cout << "Video codec               : "
            << VideoCodecName(video_config.codec) << std::endl;
        std::cout << "Task FPS                  : " << video_fps << std::endl;
        std::cout << "Task duration             : " << task.duration_seconds << " seconds" << std::endl;
        std::cout << "Expected frame count      : "
            << (static_cast<double>(task.duration_seconds) * video_fps)
            << std::endl;
        std::cout << "Audio stream              : "
            << (mux_audio_tracks.empty() ? "NO" : "YES")
            << " (" << mux_audio_tracks.size() << " track(s))" << std::endl;

        // Compute average PTS delta to verify actual capture rate.
        {
            int64_t prev_raw_pts = -1;
            int64_t total_delta = 0;
            int64_t delta_count = 0;
            for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                if (snap.packets[i].data.empty()) continue;
                if (prev_raw_pts >= 0) {
                    total_delta += snap.packets[i].pts - prev_raw_pts;
                    ++delta_count;
                }
                prev_raw_pts = snap.packets[i].pts;
            }
            if (delta_count > 0) {
                double avg_delta = static_cast<double>(total_delta) / delta_count;
                double actual_fps = (avg_delta > 0.0)
                    ? (1.0 / (avg_delta * video_tick_seconds))
                    : 0.0;
                std::cout << "Avg PTS delta             : " << avg_delta
                    << " (actual capture rate ~" << actual_fps << " fps)" << std::endl;
            }
        }
        std::cout << "\nTimebase configuration:" << std::endl;
        std::cout << "  Encode timebase : " << encode_tb.num << "/" << encode_tb.den
            << " (each tick = " << (1.0 / encode_tb.den) << " sec)" << std::endl;
        std::cout << "  Stream timebase : " << video_stream->time_base.num
            << "/" << video_stream->time_base.den
            << " (each tick = " << (1.0 / video_stream->time_base.den) << " sec)" << std::endl;
        std::cout << "  avg_frame_rate  : " << video_stream->avg_frame_rate.num << "/"
            << video_stream->avg_frame_rate.den << " = "
            << (static_cast<float>(video_stream->avg_frame_rate.num)
                / video_stream->avg_frame_rate.den)
            << " fps" << std::endl;
        std::cout << "========================================\n" << std::endl;

        int     video_packet_count = 0;
        int64_t last_video_pts = -1;

        // Allocate one AVPacket and reuse it across the entire video loop.
        // av_interleaved_write_frame takes ownership of the packet's buffer
        // each iteration, so av_new_packet on the next pass allocates a
        // fresh buffer. The savings here are the AVPacket struct alloc/free
        // (~600/save at 60fps over 10s) which keeps the muxer hot path
        // closer to one heap allocation per frame instead of three.
        AVPacket* av_pkt = av_packet_alloc();
        if (!av_pkt) {
            std::cerr << "[MuxEncodedClip] av_packet_alloc failed" << std::endl;
            avio_closep(&fmt_ctx->pb);
            avformat_free_context(fmt_ctx);
            SetEngineError(task.shared_memory,
                L"Out of memory while preparing the clip for writing.");
            return false;
        }

        auto fail_media_write = [&](const wchar_t* operation, int error_code) {
            av_packet_free(&av_pkt);
            const int close_error = avio_closep(&fmt_ctx->pb);
            if (close_error < 0) {
                std::cerr << "[MuxEncodedClip] Secondary close failure: "
                    << close_error << std::endl;
            }
            avformat_free_context(fmt_ctx);
            wchar_t message[512];
            _snwprintf_s(
                message,
                _TRUNCATE,
                L"Failed while %ls the temporary clip (error %d). "
                L"The incomplete file was not published.",
                operation,
                error_code);
            SetEngineError(task.shared_memory, message);
            return false;
        };

        for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
            const auto& pkt = snap.packets[i];
            if (pkt.data.empty()) continue;

            ret = av_new_packet(av_pkt, static_cast<int>(pkt.data.size()));
            if (ret < 0)
                return fail_media_write(L"allocating a video packet for", ret);

            memcpy(av_pkt->data, pkt.data.data(), pkt.data.size());

            // Keep decoder pre-roll negative; MP4 presents from the logical
            // replay cutoff at t=0 via an edit list.
            av_pkt->pts = pkt.pts - pts_offset;
            av_pkt->dts = pkt.pts - pts_offset;
            av_pkt->duration = 1;
            av_pkt->stream_index = video_stream->index;
            av_pkt->flags = pkt.is_keyframe ? AV_PKT_FLAG_KEY : 0;

            // Log first 3 packets before rescaling.
            if (video_packet_count < 3) {
                std::cout << "[VPkt #" << video_packet_count << " BEFORE rescale] "
                    << "PTS=" << av_pkt->pts
                    << " DTS=" << av_pkt->dts
                    << " duration=" << av_pkt->duration
                    << (pkt.is_keyframe ? " [KEYFRAME]" : "")
                    << std::endl;
            }

            av_packet_rescale_ts(av_pkt, encode_tb, video_stream->time_base);
            if (video_packet_count < 3) {
                std::cout << "[VPkt #" << video_packet_count << " AFTER rescale]  "
                    << "PTS=" << av_pkt->pts
                    << " DTS=" << av_pkt->dts
                    << " duration=" << av_pkt->duration
                    << " (time="
                    << (static_cast<double>(av_pkt->pts) / video_stream->time_base.den)
                    << "s)" << std::endl;
            }

            last_video_pts = av_pkt->pts;
            ret = av_interleaved_write_frame(fmt_ctx, av_pkt);
            // av_interleaved_write_frame transfers buffer ownership to the
            // muxer. av_packet_unref clears any residual state on av_pkt so
            // the next iteration can call av_new_packet on a clean struct.
            av_packet_unref(av_pkt);
            if (ret < 0)
                return fail_media_write(L"writing video data to", ret);
            video_packet_count++;
        }

        if (video_packet_count == 0)
            return fail_media_write(L"writing video data to", -1);

        // Step 4b: Write each pre-encoded AAC source track. Every source uses
        // its own sample-rate timebase and preserves its AAC packet duration;
        // there is no cross-source downmix or re-encode in the save path.
        int audio_packet_count = 0;

        for (const auto& audio : mux_audio_tracks) {
            if (!audio.stream || audio.packets.empty()) continue;
            int track_packet_count = 0;
            int64_t total_track_samples = 0;
            for (size_t i = 0; i < audio.packets.size(); i++) {
                const auto& pkt_data = audio.packets[i];
                if (pkt_data.empty() || i >= audio.packet_pts.size()
                        || i >= audio.packet_durations.size()) continue;

                ret = av_new_packet(av_pkt, static_cast<int>(pkt_data.size()));
                if (ret < 0)
                    return fail_media_write(L"allocating an audio packet for", ret);

                memcpy(av_pkt->data, pkt_data.data(), pkt_data.size());
                av_pkt->pts = audio.packet_pts[i] + audio.output_pts_offset;
                av_pkt->dts = audio.packet_pts[i] + audio.output_pts_offset;
                av_pkt->duration = audio.packet_durations[i];
                av_pkt->stream_index = audio.stream->index;
                av_pkt->flags = 0;

                ret = av_interleaved_write_frame(fmt_ctx, av_pkt);
                av_packet_unref(av_pkt);
                if (ret < 0)
                    return fail_media_write(L"writing audio data to", ret);
                audio_packet_count++;
                track_packet_count++;
                total_track_samples += audio.packet_durations[i];
            }

            const double audio_written_s = audio.sample_rate > 0
                ? static_cast<double>(total_track_samples) / audio.sample_rate : 0.0;
            std::cout << "[MuxEncodedClip] Audio track '"
                << (audio.is_default_mix ? "Default Mix" : audio.metadata.identity.display_name)
                << "': " << track_packet_count << " packets ("
                << audio_written_s << "s)" << std::endl;
        }

        av_packet_free(&av_pkt);

        // Flush muxer's internal interleave buffer
        ret = av_interleaved_write_frame(fmt_ctx, nullptr);
        if (ret < 0)
            return fail_media_write(L"flushing interleaved data for", ret);

        const double actual_duration_s = (last_video_pts >= 0)
            ? (static_cast<double>(last_video_pts) / 90000.0)
                + video_frame_seconds
            : 0.0;

        std::cout << "\n[MuxEncodedClip] Summary:" << std::endl;
        std::cout << "  Video packets written   : " << video_packet_count << std::endl;
        std::cout << "  Audio packets written   : " << audio_packet_count << std::endl;
        std::cout << "  Last video PTS          : " << last_video_pts
            << " (" << (static_cast<double>(last_video_pts)
                / video_stream->time_base.den) << "s)" << std::endl;
        std::cout << "  Calculated clip duration: " << actual_duration_s << "s" << std::endl;
        std::cout << "  Requested duration      : " << task.duration_seconds << "s" << std::endl;
        if (!snap.full_history)
            std::cout << "  NOTE: partial history after startup/recovery; saved "
                "all decodable media currently available." << std::endl;
        std::cout << "========================================\n" << std::endl;

        ret = av_write_trailer(fmt_ctx);
        if (ret < 0)
            return fail_media_write(L"finalizing the container for", ret);

        ret = avio_closep(&fmt_ctx->pb);
        if (ret < 0) {
            avformat_free_context(fmt_ctx);
            wchar_t message[512];
            _snwprintf_s(
                message,
                _TRUNCATE,
                L"Failed while closing the temporary clip (error %d). "
                L"The incomplete file was not published.",
                ret);
            SetEngineError(task.shared_memory, message);
            return false;
        }
        avformat_free_context(fmt_ctx);

        std::wcout << L"[MuxEncodedClip] Done: " << output_path
            << L" (" << video_packet_count << L" video, "
            << audio_packet_count << L" audio, "
            << actual_duration_s << L"s)" << std::endl;
        mux_diagnostic.success = true;
        return true;
    }


    // Encode and mux a legacy raw-frame snapshot.

    bool CaptureEngine::EncodeRawClip(
        const SaveClipTask& task, const std::wstring& output_path) {
        EncoderConfig enc_cfg;
        enc_cfg.src_width = task.src_width;
        enc_cfg.src_height = task.src_height;
        enc_cfg.enc_width = task.enc_width;
        enc_cfg.enc_height = task.enc_height;
        enc_cfg.fps = task.fps;
        enc_cfg.bitrate_kbps = task.bitrate_kbps;
        enc_cfg.preset = "superfast";
        enc_cfg.tune = nullptr;
        enc_cfg.scaling_mode = task.scaling_mode;

        VideoEncoder encoder;

        // Feed audio snapshot so it gets muxed alongside the video.
        // SetAudioData must be called before Initialize() so the audio stream
        // and its AAC extradata can be registered before avformat_write_header.
        if (task.has_audio && task.audio_snapshot.valid
            && !task.audio_snapshot.samples.empty())
        {
            // Use the tail of the snapshot matching the actual clip duration.
            // x264 frames have no per-frame QPC timestamps, so we take the last
            // (frame_count / fps) seconds of audio, aligned to the clip end.
            const uint32_t sr = task.audio_snapshot.sample_rate;
            const uint32_t ch = task.audio_snapshot.channels;
            const double   clip_duration_s =
                (task.fps > 0)
                ? static_cast<double>(task.frame_count) / static_cast<double>(task.fps)
                : static_cast<double>(task.duration_seconds);
            const int64_t total_frames =
                static_cast<int64_t>(task.audio_snapshot.samples.size())
                / static_cast<int64_t>(ch);
            const int64_t frames_needed =
                static_cast<int64_t>(clip_duration_s * static_cast<double>(sr) + 0.5);
            const int64_t start_frame =
                std::max<int64_t>(0, total_frames - frames_needed);
            const size_t  skip_samples =
                static_cast<size_t>(start_frame) * static_cast<size_t>(ch);

            std::vector<float> aligned_pcm(
                task.audio_snapshot.samples.begin() + static_cast<ptrdiff_t>(skip_samples),
                task.audio_snapshot.samples.end());

            if (!aligned_pcm.empty()) {
                encoder.SetAudioData(std::move(aligned_pcm), sr, ch, task.audio_bitrate_kbps);
                std::cout << "[EncodeRawClip] Audio: "
                          << frames_needed << " frames from snapshot (clip="
                          << clip_duration_s << "s)" << std::endl;
            }
        }

        if (!encoder.Initialize(output_path.c_str(), enc_cfg)) {
            std::cerr << "[EncodeRawClip] Encoder initialization failed" << std::endl;
            SetEngineError(task.shared_memory,
                L"Failed to initialise the video encoder. The selected codec "
                L"may be unavailable on this GPU.");
            return false;
        }

        bool encode_ok = true;
        for (size_t i = 0; i < task.frame_count; i++) {
            size_t slot_idx = (task.start_frame_idx + i) % max_frames_;
            if (!encoder.EncodeFrame(frame_pool_.GetSlot(slot_idx))) {
                encode_ok = false;
                break;
            }
        }

        const bool finalize_ok = encoder.Finalize();
        if (!encode_ok) {
            SetEngineError(task.shared_memory,
                L"The software encoder failed while writing video frames. "
                L"The incomplete clip was not published.");
            return false;
        }
        if (!finalize_ok) {
            SetEngineError(task.shared_memory,
                L"The software encoder could not finalize or close the clip. "
                L"The incomplete clip was not published.");
            return false;
        }
        std::wcout << L"[EncodeRawClip] Done: " << output_path << std::endl;
        return true;
    }



    CaptureEngine::Stats CaptureEngine::GetStats() const {
        Stats s{};
        s.frames_captured = frames_captured_.load(std::memory_order_relaxed);
        s.frames_dropped = frames_dropped_.load(std::memory_order_relaxed);
        s.capture_fps = static_cast<float>(fps_);

        if (nvenc_active_ && encoded_ring_) {
            s.ring_used_frames = encoded_ring_->GetCount();
            s.ring_max_frames = encoded_ring_->GetCapacity();
            s.pool_memory_mb = 0;  // no raw frame pool
            s.effective_buffer_seconds = (fps_ > 0)
                ? static_cast<float>(s.ring_used_frames) / static_cast<float>(fps_)
                : 0.0f;
        }
        else {
            s.ring_used_frames = ring_count_.load(std::memory_order_relaxed);
            s.ring_max_frames = max_frames_;
            s.pool_memory_mb = frame_pool_.TotalBytes() / (1024 * 1024);
            s.effective_buffer_seconds = (fps_ > 0)
                ? static_cast<float>(max_frames_) / static_cast<float>(fps_)
                : 0.0f;
        }

        return s;
    }

    void CaptureEngine::PublishContentMetrics(
        uint64_t sum, uint64_t sum_sq, uint32_t count) {
        if (count == 0) return;
        const float mean = static_cast<float>(sum) / count;
        const float variance = std::max(
            0.0f, static_cast<float>(sum_sq) / count - mean * mean);
        const bool suspicious_sample =
            (mean <= 8.0f && variance <= 6.0f) || variance <= 2.0f;
        const uint32_t streak = suspicious_sample
            ? content_suspicious_streak_.fetch_add(1) + 1
            : 0;
        if (!suspicious_sample) content_suspicious_streak_.store(0);
        content_luma_mean_.store(mean);
        content_luma_variance_.store(variance);
        content_sample_sequence_.fetch_add(1);
        if (streak >= 12)
            capture_health_flags_.fetch_or(CAPTURE_HEALTH_CONTENT_SUSPECT);
        else
            capture_health_flags_.fetch_and(~CAPTURE_HEALTH_CONTENT_SUSPECT);
    }

    void CaptureEngine::SampleContentBGRA(
        const uint8_t* data, uint32_t stride, uint32_t width,
        uint32_t height, uint64_t produced_frame) {
        if (!data || width == 0 || height == 0 || stride < width * 4 ||
            produced_frame % std::max<uint32_t>(1, fps_) != 0)
            return;
        constexpr uint32_t kColumns = 16;
        constexpr uint32_t kRows = 9;
        uint64_t sum = 0;
        uint64_t sum_sq = 0;
        for (uint32_t row = 0; row < kRows; ++row) {
            const uint32_t y = std::min(
                height - 1, ((2 * row + 1) * height) / (2 * kRows));
            for (uint32_t column = 0; column < kColumns; ++column) {
                const uint32_t x = std::min(
                    width - 1, ((2 * column + 1) * width) / (2 * kColumns));
                const uint8_t* pixel = data + static_cast<size_t>(y) * stride + x * 4;
                const uint32_t luma =
                    (19u * pixel[0] + 183u * pixel[1] + 54u * pixel[2]) >> 8;
                sum += luma;
                sum_sq += luma * luma;
            }
        }
        PublishContentMetrics(sum, sum_sq, kColumns * kRows);
    }

    void CaptureEngine::SampleContentTexture(
        ID3D11Texture2D* texture, uint64_t produced_frame) {
        if (!texture || produced_frame % std::max<uint32_t>(1, fps_) != 0)
            return;
        D3D11_TEXTURE2D_DESC source{};
        texture->GetDesc(&source);
        if (source.Width == 0 || source.Height == 0) return;

        constexpr UINT kColumns = 16;
        constexpr UINT kRows = 9;

        if (!health_staging_texture_) {
            D3D11_TEXTURE2D_DESC desc{};
            desc.Width = kColumns;
            desc.Height = kRows;
            desc.MipLevels = 1;
            desc.ArraySize = 1;
            desc.Format = source.Format;
            desc.SampleDesc.Count = 1;
            desc.Usage = D3D11_USAGE_STAGING;
            desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            if (FAILED(device_->CreateTexture2D(&desc, nullptr,
                                                &health_staging_texture_)))
                return;
        }

        // Copy 144 distributed pixels into one tiny staging texture, then map
        // once. A single centre strip falsely classified dark wallpapers and
        // letterboxed scenes as uniform; the full sparse grid matches the CPU
        // path without a full-frame GPU readback.
        for (UINT row = 0; row < kRows; ++row) {
            const UINT y = std::min(
                source.Height - 1, ((2 * row + 1) * source.Height) / (2 * kRows));
            for (UINT column = 0; column < kColumns; ++column) {
                const UINT x = std::min(
                    source.Width - 1,
                    ((2 * column + 1) * source.Width) / (2 * kColumns));
                D3D11_BOX box{x, y, 0, x + 1, y + 1, 1};
                context_->CopySubresourceRegion(
                    health_staging_texture_, 0, column, row, 0, texture, 0, &box);
            }
        }
        D3D11_MAPPED_SUBRESOURCE mapped{};
        if (FAILED(context_->Map(
                health_staging_texture_, 0, D3D11_MAP_READ, 0, &mapped)))
            return;
        uint64_t sum = 0;
        uint64_t sum_sq = 0;
        const uint8_t* data = static_cast<const uint8_t*>(mapped.pData);
        for (UINT row = 0; row < kRows; ++row) {
            const uint8_t* pixels = data + static_cast<size_t>(row) * mapped.RowPitch;
            for (UINT column = 0; column < kColumns; ++column) {
                const uint8_t* pixel = pixels + column * 4;
                const uint32_t luma =
                    (19u * pixel[0] + 183u * pixel[1] + 54u * pixel[2]) >> 8;
                sum += luma;
                sum_sq += luma * luma;
            }
        }
        context_->Unmap(health_staging_texture_, 0);
        PublishContentMetrics(sum, sum_sq, kColumns * kRows);
    }

    void CaptureEngine::ClearReplayForRecovery(int64_t recovery_cutoff_qpc) {
        if (recovery_cutoff_qpc <= 0) {
            LARGE_INTEGER now{};
            if (QueryPerformanceCounter(&now)) {
                recovery_cutoff_qpc = now.QuadPart;
            }
        }
        if (encoded_ring_) encoded_ring_->Clear(recovery_cutoff_qpc);
        replay_config_publish_failed_.store(false);
        ring_head_.store(0, std::memory_order_release);
        ring_count_.store(0, std::memory_order_release);
        content_suspicious_streak_.store(0);
        capture_health_flags_.fetch_and(~CAPTURE_HEALTH_CONTENT_SUSPECT);
    }

    bool CaptureEngine::WaitForDxgiRecoveryBackoff(
        uint32_t delay_ms) const {
        uint32_t remaining = delay_ms;
        while (remaining > 0) {
            if (!running_.load(std::memory_order_relaxed)) return false;
            const uint32_t slice = std::min<uint32_t>(remaining, 10);
            std::this_thread::sleep_for(std::chrono::milliseconds(slice));
            remaining -= slice;
        }
        return running_.load(std::memory_order_relaxed);
    }

    dxgi::RecoveryAttemptResult CaptureEngine::TryRecreateDxgiDuplication(
        uint32_t attempt, std::string& detail) {
        if (!running_.load(std::memory_order_relaxed)) {
            detail = "shutdown requested";
            return dxgi::RecoveryAttemptResult::StopRequested;
        }
        if (!device_ || !capture_device_adapter_luid_available_) {
            detail = "live capture device identity is unavailable";
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        const HRESULT removed_reason = device_->GetDeviceRemovedReason();
        if (removed_reason != S_OK) {
            detail = diagnostics::FormatHResultFailure(
                "ID3D11Device::GetDeviceRemovedReason", removed_reason);
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        const auto current_monitor = monitor_resolver_.Resolve(
            monitor_device_path_);
        if (!current_monitor.ok()) {
            detail = std::string(monitor::ToString(current_monitor.error))
                + ": " + current_monitor.diagnostic;
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        IDXGIFactory1* factory = nullptr;
        IDXGIAdapter1* adapter = nullptr;
        IDXGIOutput* output = nullptr;
        IDXGIOutput1* output1 = nullptr;
        IDXGIOutputDuplication* candidate = nullptr;
        const auto cleanup = [&] {
            if (candidate) { candidate->Release(); candidate = nullptr; }
            if (output1) { output1->Release(); output1 = nullptr; }
            if (output) { output->Release(); output = nullptr; }
            if (adapter) { adapter->Release(); adapter = nullptr; }
            if (factory) { factory->Release(); factory = nullptr; }
        };

        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory));
        if (FAILED(hr) || !factory) {
            detail = diagnostics::FormatHResultFailure(
                "CreateDXGIFactory1(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        monitor::DxgiOutputIdentity output_identity;
        std::string output_diagnostic;
        if (!monitor::OpenSelectedDxgiOutput(
                factory, current_monitor.monitor, &adapter, &output,
                &output_identity, output_diagnostic)) {
            detail = output_diagnostic.empty()
                ? "selected monitor output is temporarily unavailable"
                : output_diagnostic;
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        DXGI_OUTPUT_DESC output_desc{};
        hr = output->GetDesc(&output_desc);
        if (FAILED(hr)) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput::GetDesc(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        const uint32_t current_width = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.right
            - output_desc.DesktopCoordinates.left);
        const uint32_t current_height = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.bottom
            - output_desc.DesktopCoordinates.top);

        const dxgi::RecoveryIdentity active_identity{
            monitor_device_path_, capture_device_adapter_luid_, width_, height_};
        const dxgi::RecoveryObservation observation{
            running_.load(std::memory_order_relaxed),
            true,
            {current_monitor.monitor.monitor_device_path,
             current_monitor.monitor.adapter_luid,
             current_width,
             current_height},
            removed_reason};
        const auto decision = dxgi::EvaluateRecovery(
            active_identity, observation);
        std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                  << "\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_dimensions_observed\","
                  << "\"attempt\":" << attempt << ','
                  << "\"decision\":\""
                  << dxgi::RecoveryDecisionName(decision) << "\","
                  << "\"active_width\":" << active_identity.width << ','
                  << "\"active_height\":" << active_identity.height << ','
                  << "\"current_width\":" << current_width << ','
                  << "\"current_height\":" << current_height << "}"
                  << std::endl;
        if (decision != dxgi::RecoveryDecision::RecreateDuplication) {
            detail = std::string("recovery contract rejected: ")
                + dxgi::RecoveryDecisionName(decision);
            cleanup();
            return dxgi::RecoveryAttemptForDecision(decision);
        }

        DXGI_ADAPTER_DESC1 adapter_desc{};
        hr = adapter->GetDesc1(&adapter_desc);
        if (FAILED(hr)) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::GetDesc1(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        const monitor::AdapterLuid reopened_luid{
            adapter_desc.AdapterLuid.LowPart,
            adapter_desc.AdapterLuid.HighPart};
        if (reopened_luid != capture_device_adapter_luid_) {
            detail = "reopened output adapter LUID differs from capture device";
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        hr = output->QueryInterface(
            __uuidof(IDXGIOutput1), reinterpret_cast<void**>(&output1));
        if (FAILED(hr) || !output1) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput::QueryInterface(IDXGIOutput1,recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        hr = output1->DuplicateOutput(device_, &candidate);
        if (FAILED(hr) || !candidate) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput1::DuplicateOutput(recovery)", hr);
            const bool fatal = dxgi::IsFatalDuplicationRecreateFailure(hr);
            cleanup();
            return fatal
                ? dxgi::RecoveryAttemptResult::FatalFailure
                : dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        if (!running_.load(std::memory_order_relaxed)) {
            detail = "shutdown requested after duplication recreation";
            cleanup();
            return dxgi::RecoveryAttemptResult::StopRequested;
        }

        duplication_ = candidate;
        candidate = nullptr;
        {
            std::lock_guard<std::mutex> lock(dxgi_context_mutex_);
            resolved_monitor_ = current_monitor.monitor;
            resolved_dxgi_output_ = output_identity;
            resolved_desktop_coordinates_ = output_desc.DesktopCoordinates;
            resolved_desktop_coordinates_available_ = true;
        }
        detail = "duplication recreated on attempt " + std::to_string(attempt);
        cleanup();
        return dxgi::RecoveryAttemptResult::Recovered;
    }

    bool CaptureEngine::RecoverDxgiDuplication(
        const char* api_call, HRESULT trigger) {
        // Mark the boundary before clearing the ring. A delayed encoder
        // callback must not be mistaken for current history while recovery is
        // in progress; ClearReplayForRecovery also records this QPC cutoff.
        capture_health_flags_.store(CAPTURE_HEALTH_RECOVERING);
        LARGE_INTEGER recovery_start_qpc{};
        QueryPerformanceCounter(&recovery_start_qpc);
        EmitRecentDxgiEvidence("dxgi_recovery_started");
        // A duplication recovery starts a new replay history. Clear before
        // releasing/recreating DXGI so a save racing this boundary can never
        // publish packets from two device/output generations.
        ClearReplayForRecovery(recovery_start_qpc.QuadPart);
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_started\","
                  << "\"state\":\"RECOVERING\","
                  << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                  << "\"native_failure\":"
                  << diagnostics::HResultFailureJson(
                         api_call, trigger)
                  << ",\"monitor_id\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         monitor_device_path_)) << "\","
                  << "\"capture_device_luid\":"
                  << (capture_device_adapter_luid_available_
                      ? AdapterLuidJson(capture_device_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << "}"
                  << std::endl;

        // Any outstanding frame has already been released by the caller. The
        // invalid interface must be released before DuplicateOutput is tried.
        if (duplication_) {
            duplication_->Release();
            duplication_ = nullptr;
        }

        std::string last_detail;
        const auto result = dxgi::RunBoundedRecovery(
            [this] {
                return running_.load(std::memory_order_relaxed);
            },
            [this](uint32_t delay_ms) {
                return WaitForDxgiRecoveryBackoff(delay_ms);
            },
            [this, &last_detail](uint32_t attempt) {
                const auto attempt_result = TryRecreateDxgiDuplication(
                    attempt, last_detail);
                if (attempt_result != dxgi::RecoveryAttemptResult::Recovered) {
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                              << "\"subsystem\":\"capture\","
                              << "\"event\":\"recovery_attempt_failed\","
                              << "\"attempt\":" << attempt << ','
                              << "\"detail\":\""
                              << diagnostics::JsonEscape(last_detail)
                              << "\"}" << std::endl;
                }
                return attempt_result;
            });
        capture_recovery_attempts_.fetch_add(
            result.attempts, std::memory_order_relaxed);

        if (result.outcome == dxgi::RecoveryOutcome::Recovered) {
            const uint32_t generation = capture_generation_.fetch_add(
                1, std::memory_order_acq_rel) + 1;
            const uint32_t restart = capture_restart_count_.fetch_add(
                1, std::memory_order_relaxed) + 1;
            last_capture_hresult_.store(S_OK, std::memory_order_relaxed);
            capture_health_flags_.store(CAPTURE_HEALTH_ACTIVE);
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"recovery_completed\","
                      << "\"state\":\"ACTIVE\","
                      << "\"attempts\":" << result.attempts << ','
                      << "\"capture_restart_count\":" << restart << ','
                      << "\"encoder_restart_count\":0,"
                      << "\"stream_generation_preserved\":false,"
                      << "\"replay_ring_reset\":true,"
                      << "\"capture_generation\":" << generation << ','
                      << "\"monitor_id\":\""
                      << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                             monitor_device_path_)) << "\","
                      << "\"dxgi_output\":{\"index\":"
                      << resolved_dxgi_output_.output_index << "},"
                      << "\"capture_device_luid\":"
                      << AdapterLuidJson(capture_device_adapter_luid_) << "}"
                      << std::endl;
            return true;
        }

        if (result.outcome == dxgi::RecoveryOutcome::StopRequested) {
            return false;
        }

        capture_recovery_failures_.fetch_add(1, std::memory_order_relaxed);
        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
        SetCaptureFailure(last_detail.empty()
            ? "DXGI duplication recovery failed without backend detail"
            : last_detail);
        std::cerr << "[CaptureThread] DXGI recovery failed: "
                  << dxgi::RecoveryOutcomeName(result.outcome)
                  << " attempts=" << result.attempts
                  << " detail=" << last_capture_failure_detail_ << std::endl;
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                  << "\"outcome\":\""
                  << dxgi::RecoveryOutcomeName(result.outcome) << "\","
                  << "\"attempts\":" << result.attempts << ','
                  << "\"detail\":\""
                  << diagnostics::JsonEscape(last_capture_failure_detail_)
                  << "\",\"native_failure\":"
                  << diagnostics::HResultFailureJson(
                         api_call, trigger)
                  << "}" << std::endl;
        return false;
    }

    bool CaptureEngine::ValidateCaptureTexture(
        ID3D11Texture2D* texture, const char* backend_name) {
        if (!texture) {
            SetCaptureFailure("capture backend returned a null D3D11 texture");
            return false;
        }
        D3D11_TEXTURE2D_DESC description{};
        texture->GetDesc(&description);
        const dxgi::TextureContract expected{
            width_, height_,
            static_cast<uint32_t>(DXGI_FORMAT_B8G8R8A8_UNORM), 1};
        const dxgi::TextureContract actual{
            description.Width, description.Height,
            static_cast<uint32_t>(description.Format),
            description.SampleDesc.Count};
        const bool valid = dxgi::IsCompatibleTexture(expected, actual);
        if (valid) return true;

        std::ostringstream failure;
        failure << "capture texture contract mismatch expected="
                << width_ << 'x' << height_
                << "/BGRA8/sample1 actual="
                << description.Width << 'x' << description.Height
                << "/format" << static_cast<uint32_t>(description.Format)
                << "/sample" << description.SampleDesc.Count;
        SetCaptureFailure(failure.str());
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"texture_contract_rejected\","
                  << "\"state\":\"FAILED\",\"error_code\":\""
                  << (backend_name && std::strcmp(backend_name, "WGC") == 0
                      ? "CAPTURE_WGC_RUNTIME_FAILED"
                      : "CAPTURE_DXGI_RUNTIME_FAILED") << "\","
                  << "\"backend\":\""
                  << diagnostics::JsonEscape(
                         backend_name ? backend_name : "unknown") << "\","
                  << "\"expected_width\":" << width_ << ','
                  << "\"expected_height\":" << height_ << ','
                  << "\"actual_width\":" << description.Width << ','
                  << "\"actual_height\":" << description.Height << ','
                  << "\"actual_format\":"
                  << static_cast<uint32_t>(description.Format) << ','
                  << "\"actual_sample_count\":"
                  << description.SampleDesc.Count << "}" << std::endl;
        return false;
    }

    bool CaptureEngine::ValidateMappedCaptureRowPitch(
        uint32_t row_pitch, const char* backend_name) {
        if (dxgi::IsValidBgraRowPitch(width_, row_pitch)) return true;
        std::ostringstream failure;
        failure << "capture row pitch is smaller than BGRA frame width: "
                << row_pitch << " < " << (static_cast<uint64_t>(width_) * 4u);
        SetCaptureFailure(failure.str());
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"texture_contract_rejected\","
                  << "\"state\":\"FAILED\",\"error_code\":\""
                  << (backend_name && std::strcmp(backend_name, "WGC") == 0
                      ? "CAPTURE_WGC_RUNTIME_FAILED"
                      : "CAPTURE_DXGI_RUNTIME_FAILED") << "\","
                  << "\"backend\":\""
                  << diagnostics::JsonEscape(
                         backend_name ? backend_name : "unknown") << "\","
                  << "\"expected_minimum_row_pitch\":"
                  << (static_cast<uint64_t>(width_) * 4u) << ','
                  << "\"actual_row_pitch\":" << row_pitch << "}"
                  << std::endl;
        return false;
    }


    // Capture DXGI frames at the QPC-driven cadence and submit to the replay
    // encoder. Its callback publishes packets to EncodedRingBuffer; only the
    // legacy raw path updates frame_pool_ and its indices.

    void CaptureEngine::CaptureThread() {
        // WGC disabled — DXGI-only path. No WinRT apartment needed.

        std::cout << "[CaptureThread] Started (DXGI path)." << std::endl;

        LARGE_INTEGER qpc_freq;
        QueryPerformanceFrequency(&qpc_freq);

        const double  target_ms = 1000.0 / static_cast<double>(fps_);
        const int64_t target_qpc = static_cast<int64_t>(
            (target_ms / 1000.0) * static_cast<double>(qpc_freq.QuadPart));

        FrameRateScheduler frame_scheduler(target_qpc);

        std::cout << "[CaptureThread] " << fps_ << " fps ("
            << target_ms << " ms/frame)  "
            << (nvenc_active_ ? GetActiveEncoderName() : "software")
            << " path" << std::endl;

        uint32_t consecutive_acquire_errors = 0;
        int64_t previous_present_qpc = 0;
        uint32_t previous_generation = capture_generation_.load(
            std::memory_order_relaxed);
        if (replay_encoder_) {
            replay_encoder_->SetCaptureGeneration(previous_generation);
        }
        IDXGIResource* previous_resource_identity = nullptr;
        uint64_t next_resource_token = 0;

        while (running_.load(std::memory_order_relaxed)) {

            const uint32_t generation = capture_generation_.load(
                std::memory_order_relaxed);
            if (generation != previous_generation) {
                // A recovery starts a fresh history. Do not report a
                // presentation gap across the old and new duplication.
                previous_generation = generation;
                previous_present_qpc = 0;
                previous_resource_identity = nullptr;
                if (replay_encoder_) {
                    replay_encoder_->SetCaptureGeneration(generation);
                }
            }

            capture_loop_iterations_.fetch_add(1, std::memory_order_relaxed);

            DXGI_OUTDUPL_FRAME_INFO info{};
            IDXGIResource* resource = nullptr;

            capture_thread_stage_.store(1, std::memory_order_relaxed);
            capture_acquire_attempts_.fetch_add(1, std::memory_order_relaxed);
            if (!duplication_) {
                SetCaptureFailure(
                    "DXGI duplication is unavailable outside recovery");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }
            LARGE_INTEGER acquire_start_qpc{};
            QueryPerformanceCounter(&acquire_start_qpc);
            HRESULT hr = duplication_->AcquireNextFrame(33, &info, &resource);
            LARGE_INTEGER acquire_end_qpc{};
            QueryPerformanceCounter(&acquire_end_qpc);
            last_capture_hresult_.store(
                static_cast<int32_t>(hr), std::memory_order_relaxed);

            const int64_t present_qpc = info.LastPresentTime.QuadPart;
            int64_t presentation_gap_qpc = 0;
            if (present_qpc > 0 && previous_present_qpc > 0) {
                presentation_gap_qpc = present_qpc - previous_present_qpc;
            }
            if (present_qpc > previous_present_qpc) {
                previous_present_qpc = present_qpc;
            }
            dxgi::RecentDxgiEvent recent_event{};
            recent_event.acquire_start_qpc = acquire_start_qpc.QuadPart;
            recent_event.acquire_end_qpc = acquire_end_qpc.QuadPart;
            recent_event.last_present_qpc = present_qpc;
            recent_event.presentation_gap_qpc = presentation_gap_qpc;
            recent_event.acquire_hresult = static_cast<int32_t>(hr);
            if (resource && resource != previous_resource_identity) {
                previous_resource_identity = resource;
                ++next_resource_token;
            }
            recent_event.resource_token = resource ? next_resource_token : 0;
            recent_event.generation = generation;
            recent_event.width = width_;
            recent_event.height = height_;
            recent_event.format = static_cast<uint32_t>(
                DXGI_FORMAT_B8G8R8A8_UNORM);
            recent_event.output_index = resolved_dxgi_output_.output_index;
            recent_event.acquired = SUCCEEDED(hr);
            recent_event.timed_out = hr == DXGI_ERROR_WAIT_TIMEOUT;
            // A successful acquire is recorded once, after ReleaseFrame has
            // completed, so one event contains the complete ownership
            // boundary. Timeouts/errors have no ReleaseFrame ownership and
            // are recorded immediately.
            if (FAILED(hr)) {
                dxgi_recent_evidence_.Record(recent_event);
            }

            if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
                capture_timeouts_.fetch_add(1, std::memory_order_relaxed);
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                continue;
            }

            if (hr == DXGI_ERROR_ACCESS_LOST) {
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (RecoverDxgiDuplication(
                        "IDXGIOutputDuplication::AcquireNextFrame", hr)) {
                    consecutive_acquire_errors = 0;
                    continue;
                }
                running_.store(false);
                break;
            }

            if (FAILED(hr)) {
                std::cerr << "[CaptureThread] AcquireNextFrame failed: 0x"
                    << std::hex << hr << std::dec << std::endl;
                if (++consecutive_acquire_errors >= 100) {
                    std::cerr << "[CaptureThread] Too many consecutive acquisition errors"
                              << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     "IDXGIOutputDuplication::AcquireNextFrame", hr)
                              << ",\"consecutive_failures\":"
                              << consecutive_acquire_errors << "}"
                              << std::endl;
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            consecutive_acquire_errors = 0;
            capture_acquire_successes_.fetch_add(1, std::memory_order_relaxed);
            capture_thread_stage_.store(2, std::memory_order_relaxed);
            dxgi::FrameLease<IDXGIResource, IDXGIOutputDuplication> frame(
                resource, duplication_);
            const auto finish_frame = [this, &frame, &recent_event]() {
                frame.ReleaseResource();
                const bool owned = frame.owns_frame();
                const HRESULT release = frame.ReleaseFrame();
                recent_event.release_attempted = true;
                recent_event.release_hresult = static_cast<int32_t>(release);
                dxgi_recent_evidence_.Record(recent_event);
                if (owned) {
                    capture_frames_released_.fetch_add(
                        1, std::memory_order_relaxed);
                }
                if (release == DXGI_ERROR_ACCESS_LOST) {
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    return RecoverDxgiDuplication(
                        "IDXGIOutputDuplication::ReleaseFrame", release);
                }
                if (FAILED(release)) {
                    SetCaptureFailure(diagnostics::FormatHResultFailure(
                        "IDXGIOutputDuplication::ReleaseFrame", release));
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                              << "\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     "IDXGIOutputDuplication::ReleaseFrame",
                                     release) << "}" << std::endl;
                    ClearReplayForRecovery();
                    capture_health_flags_.store(
                        CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    return false;
                }
                return true;
            };

            if (!resource) {
                SetCaptureFailure(
                    "AcquireNextFrame succeeded without a desktop resource");
                finish_frame();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }

            // Pointer-only updates carry no newly presented desktop image.
            // Encoding the returned old surface as a new frame creates a false
            // freeze and introduces a zero presentation timestamp.
            if (!dxgi::HasNewDesktopImage(info.LastPresentTime.QuadPart)) {
                recent_event.pointer_only = true;
                pointer_only_frames_.fetch_add(1, std::memory_order_relaxed);
                frames_dropped_.fetch_add(1, std::memory_order_relaxed);
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!finish_frame()) break;
                continue;
            }

            // Frame rate limiting BEFORE QueryInterface.
            // At high game FPS (e.g. 300fps, 60fps target) most frames are dropped.
            // QueryInterface is a COM call with real overhead; paying it on every
            // dropped frame wastes ~240 roundtrips/sec. Checking QPC first means
            // dropped frames cost only resource->Release() + ReleaseFrame().
            LARGE_INTEGER now;
            QueryPerformanceCounter(&now);
            if (!frame_scheduler.ShouldCapture(now.QuadPart)) {
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                frames_dropped_.fetch_add(1, std::memory_order_relaxed);
                if (!finish_frame()) break;
                continue;
            }

            ID3D11Texture2D* tex = nullptr;
            hr = frame.resource()->QueryInterface(__uuidof(ID3D11Texture2D),
                reinterpret_cast<void**>(&tex));
            frame.ReleaseResource();

            if (FAILED(hr) || !tex) {
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!finish_frame()) break;
                continue;
            }
            D3D11_TEXTURE2D_DESC recent_texture_desc{};
            tex->GetDesc(&recent_texture_desc);
            recent_event.width = recent_texture_desc.Width;
            recent_event.height = recent_texture_desc.Height;
            recent_event.format = static_cast<uint32_t>(
                recent_texture_desc.Format);
            if (!ValidateCaptureTexture(tex, "DXGI")) {
                tex->Release();
                finish_frame();
                ClearReplayForRecovery();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }
            source_textures_received_.fetch_add(1, std::memory_order_relaxed);
            capture_thread_stage_.store(3, std::memory_order_relaxed);

            if (nvenc_active_ && !replay_encoder_cpu_input_) {
                // Same-adapter compressed replay path (native NVENC or AMF).
                // CopyResource is a pure GPU op; there is no full-frame CPU
                // readback or upload on the normal AMD path.
                capture_thread_stage_.store(4, std::memory_order_relaxed);
                conversion_submissions_.fetch_add(1, std::memory_order_relaxed);
                ID3D11Texture2D* encode_texture = PrepareEncodeTexture(tex);
                if (encode_texture)
                    conversion_completions_.fetch_add(1, std::memory_order_relaxed);
                capture_thread_stage_.store(5, std::memory_order_relaxed);
                const bool encoded = encode_texture && EncodeGpuReplayTexture(
                    encode_texture,
                    info.LastPresentTime.QuadPart,
                    frames_captured_.load(std::memory_order_relaxed) + 1);
                tex->Release();
                const bool frame_finished = finish_frame();
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!encoded || !frame_finished) break;
            }
            else if (nvenc_active_ && replay_encoder_cpu_input_) {
                // Legacy cross-adapter NVENC input: read back the Intel staging texture
                // for CPU transfer to NVIDIA. Map must wait for the GPU copy; DO_NOT_WAIT
                // can reject every frame when called immediately after CopyResource.
                context_->CopyResource(staging_texture_, tex);
                tex->Release();
                capture_thread_stage_.store(4, std::memory_order_relaxed);

                D3D11_MAPPED_SUBRESOURCE mapped{};
                hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                if (FAILED(hr)) {
                    std::cerr << "[CaptureThread] Texture map failed (Optimus): 0x"
                        << std::hex << hr << std::dec << std::endl;
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    if (!finish_frame()) break;
                    continue;
                }
                if (!ValidateMappedCaptureRowPitch(mapped.RowPitch, "DXGI")) {
                    context_->Unmap(staging_texture_, 0);
                    finish_frame();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }

                SampleContentBGRA(
                    static_cast<const uint8_t*>(mapped.pData), mapped.RowPitch,
                    width_, height_, frames_captured_.load(std::memory_order_relaxed) + 1);

                const uint8_t* encode_data = CropMappedData(
                    static_cast<const uint8_t*>(mapped.pData), mapped.RowPitch);
                bool encoded = false;
                for (uint32_t attempt = 1; attempt <= 3 && !encoded; ++attempt) {
                    encoded = replay_encoder_->EncodeFrameCPU(
                        encode_data,
                        mapped.RowPitch,
                        info.LastPresentTime.QuadPart);
                    if (!encoded && attempt < 3) {
                        std::cerr << "[CaptureThread] Transient hybrid encoder "
                                  << "submission failure; retrying (" << attempt
                                  << "/3)." << std::endl;
                        std::this_thread::sleep_for(std::chrono::milliseconds(2));
                    }
                }
                context_->Unmap(staging_texture_, 0);
                conversion_submissions_.fetch_add(1, std::memory_order_relaxed);
                conversion_completions_.fetch_add(1, std::memory_order_relaxed);
                const bool frame_finished = finish_frame();
                if (!encoded) {
                    FailReplayEncoder("hybrid NVENC CPU-input submission");
                    break;
                }
                if (!frame_finished) break;
            }
            else {
                // Legacy raw path: copy to staging, then map and copy to the frame pool.
                context_->CopyResource(staging_texture_, tex);
                tex->Release();
                capture_thread_stage_.store(4, std::memory_order_relaxed);

                D3D11_MAPPED_SUBRESOURCE mapped{};
                hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                if (FAILED(hr)) {
                    std::cerr << "[CaptureThread] Texture map failed: 0x"
                        << std::hex << hr << std::dec << std::endl;
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    if (!finish_frame()) break;
                    continue;
                }
                if (!ValidateMappedCaptureRowPitch(mapped.RowPitch, "DXGI")) {
                    context_->Unmap(staging_texture_, 0);
                    finish_frame();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }

                const uint8_t* full_src = static_cast<const uint8_t*>(mapped.pData);
                const uint8_t* src     = CropMappedData(full_src, mapped.RowPitch);
                const size_t   row     = static_cast<size_t>(crop_width_) * 4;
                const bool     pitched = (mapped.RowPitch != static_cast<UINT>(row));

                const size_t write_pos = ring_head_.load(std::memory_order_relaxed);
                const size_t slot_idx  = write_pos % max_frames_;
                uint8_t*     dst       = frame_pool_.GetSlot(slot_idx);

                SampleContentBGRA(
                    full_src, mapped.RowPitch, width_, height_,
                    frames_captured_.load(std::memory_order_relaxed) + 1);

                if (!pitched) {
                    std::memcpy(dst, src, row * crop_height_);
                }
                else {
                    for (uint32_t y = 0; y < crop_height_; y++) {
                        std::memcpy(dst + y * row, src + y * mapped.RowPitch, row);
                    }
                }

                context_->Unmap(staging_texture_, 0);

                ring_head_.fetch_add(1, std::memory_order_release);
                size_t prev = ring_count_.load(std::memory_order_relaxed);
                if (prev < max_frames_)
                    ring_count_.fetch_add(1, std::memory_order_relaxed);
                if (!finish_frame()) break;
            }

            uint64_t fc = frames_captured_.fetch_add(1, std::memory_order_relaxed) + 1;
            capture_thread_stage_.store(0, std::memory_order_relaxed);
            if (fc == 1 || fc == 10 || fc == 100 || (fc % 500 == 0)) {
                std::cout << "[CaptureThread] Frames captured: " << fc << std::endl;
            }
        }

        std::cout << "[CaptureThread] Stopped. Total frames: "
                  << frames_captured_.load() << std::endl;
        if (capture_health_flags_.load() != CAPTURE_HEALTH_BACKEND_FAILED)
            capture_health_flags_.store(CAPTURE_HEALTH_NONE);
    }

    bool CaptureEngine::ConfigureCrop(const CaptureConfig& config) {
        crop_enabled_ = false;
        crop_x_ = 0;
        crop_y_ = 0;
        crop_width_ = width_;
        crop_height_ = height_;
        if (!config.crop_enabled) return true;

        const bool normalized = std::isfinite(config.crop_x)
            && std::isfinite(config.crop_y)
            && std::isfinite(config.crop_width)
            && std::isfinite(config.crop_height)
            && config.crop_x >= 0.0 && config.crop_y >= 0.0
            && config.crop_width > 0.0 && config.crop_height > 0.0
            && config.crop_x + config.crop_width <= 1.000001
            && config.crop_y + config.crop_height <= 1.000001;
        if (!normalized || width_ < 2 || height_ < 2) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_PROFILE_INVALID: "
                         "The saved crop was outside the source frame; the full "
                         "frame is being captured." << std::endl;
            return true;
        }

        const uint32_t left = std::min<uint32_t>(
            width_ - 1, static_cast<uint32_t>(std::floor(config.crop_x * width_)));
        const uint32_t top = std::min<uint32_t>(
            height_ - 1, static_cast<uint32_t>(std::floor(config.crop_y * height_)));
        const uint32_t right = std::min<uint32_t>(
            width_, static_cast<uint32_t>(std::ceil(
                (config.crop_x + config.crop_width) * width_)));
        const uint32_t bottom = std::min<uint32_t>(
            height_, static_cast<uint32_t>(std::ceil(
                (config.crop_y + config.crop_height) * height_)));
        const uint32_t crop_width = (right > left) ? ((right - left) & ~1u) : 0;
        const uint32_t crop_height = (bottom > top) ? ((bottom - top) & ~1u) : 0;
        if (crop_width < 2 || crop_height < 2
            || left + crop_width > width_ || top + crop_height > height_) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_PROFILE_INVALID: "
                         "The saved crop became too small at this resolution; "
                         "the full frame is being captured." << std::endl;
            return true;
        }
        if (left == 0 && top == 0
            && crop_width == width_ && crop_height == height_) return true;

        D3D11_TEXTURE2D_DESC description{};
        description.Width = crop_width;
        description.Height = crop_height;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_DEFAULT;
        const HRESULT result = device_->CreateTexture2D(
            &description, nullptr, &crop_texture_);
        if (FAILED(result)) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_GPU_UNAVAILABLE: "
                         "The crop texture could not be created; the full frame "
                         "is being captured." << std::endl;
            return true;
        }

        crop_enabled_ = true;
        crop_x_ = left;
        crop_y_ = top;
        crop_width_ = crop_width;
        crop_height_ = crop_height;

        // A resolution preset is a bounding box. Cropped sources retain their
        // own aspect ratio instead of being stretched back to the old preset.
        if (target_width_ > 0 && target_height_ > 0) {
            const double scale = std::min(
                static_cast<double>(target_width_) / crop_width_,
                static_cast<double>(target_height_) / crop_height_);
            target_width_ = std::max<uint32_t>(2,
                static_cast<uint32_t>(std::floor(crop_width_ * scale)) & ~1u);
            target_height_ = std::max<uint32_t>(2,
                static_cast<uint32_t>(std::floor(crop_height_ * scale)) & ~1u);
        }
        std::cout << "[Crop] Encoder input " << crop_width_ << 'x'
                  << crop_height_ << " at " << crop_x_ << ',' << crop_y_
                  << " (normalized per-game profile)" << std::endl;
        return true;
    }

    ID3D11Texture2D* CaptureEngine::PrepareEncodeTexture(
        ID3D11Texture2D* source) {
        if (!crop_enabled_) return source;
        if (!source || !crop_texture_ || !context_) return nullptr;
        const D3D11_BOX box{
            crop_x_, crop_y_, 0,
            crop_x_ + crop_width_, crop_y_ + crop_height_, 1};
        context_->CopySubresourceRegion(
            crop_texture_, 0, 0, 0, 0, source, 0, &box);
        return crop_texture_;
    }

    const uint8_t* CaptureEngine::CropMappedData(
        const uint8_t* data, uint32_t stride) const {
        if (!data || !crop_enabled_) return data;
        return data + static_cast<size_t>(crop_y_) * stride
            + static_cast<size_t>(crop_x_) * 4;
    }

    bool CaptureEngine::EnsureStagingTexture() {
        if (!device_ || width_ == 0 || height_ == 0) return false;

        D3D11_TEXTURE2D_DESC description{};
        description.Width = width_;
        description.Height = height_;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_STAGING;
        description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (!staging_texture_) {
            const HRESULT result = device_->CreateTexture2D(
                &description, nullptr, &staging_texture_);
            if (FAILED(result)) {
                std::cerr << "[CaptureEngine] D3D11 readback texture creation failed: 0x"
                          << std::hex << result << std::dec << std::endl;
                return false;
            }
        }
        return true;
    }

    bool CaptureEngine::EncodeGpuReplayTexture(
        ID3D11Texture2D* source,
        int64_t present_qpc,
        uint64_t produced_frame) {
        if (!source || !replay_encoder_ || !context_) {
            FailReplayEncoder("D3D11 replay input setup");
            return false;
        }
        SampleContentTexture(source, produced_frame);
        if (replay_encoder_->RequiresBackendGpuPreparation()) {
            if (!replay_encoder_->PrepareGpuFrame(source, 0)) {
                FailReplayEncoder("backend GPU conversion");
                return false;
            }
        } else {
            ID3D11Texture2D* input = replay_encoder_->GetCurrentInputTexture();
            if (!input) {
                FailReplayEncoder("D3D11 replay input acquisition");
                return false;
            }

            D3D11_TEXTURE2D_DESC source_description{};
            D3D11_TEXTURE2D_DESC input_description{};
            source->GetDesc(&source_description);
            input->GetDesc(&input_description);
            if (source_description.Width != input_description.Width
                || source_description.Height != input_description.Height
                || source_description.Format != input_description.Format) {
                FailReplayEncoder("D3D11 replay texture compatibility check");
                return false;
            }
            context_->CopySubresourceRegion(
                input,
                replay_encoder_->GetCurrentInputSubresource(),
                0, 0, 0,
                source,
                0,
                nullptr);
        }
        if (!replay_encoder_->EncodeFrame(present_qpc)) {
            FailReplayEncoder("hardware frame submission");
            return false;
        }
        if (replay_config_publish_failed_.load()) {
            FailReplayEncoder("encoded stream configuration publication");
            return false;
        }
        return true;
    }

    void CaptureEngine::FailReplayEncoder(const char* operation) {
        ClearReplayForRecovery();
        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
        running_.store(false);
        const std::string detail = replay_encoder_
            ? replay_encoder_->GetLastError() : std::string{};
        std::cerr << "[CaptureEngine] " << operation << " failed";
        if (!detail.empty()) std::cerr << ": " << detail;
        std::cerr << "; a fresh capture generation is required" << std::endl;
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"encoder\","
                  << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"ENCODER_SUBMIT_FAILED\","
                  << "\"operation\":\""
                  << diagnostics::JsonEscape(operation ? operation : "unknown")
                  << "\",\"detail\":\""
                  << diagnostics::JsonEscape(
                         detail.empty() ? "unavailable:no_backend_detail" : detail)
                  << "\",\"encoder_backend\":\""
                  << diagnostics::JsonEscape(startup_encoder_backend_) << "\","
                  << "\"codec\":\"" << diagnostics::JsonEscape(startup_codec_)
                  << "\",\"encoder_adapter_luid\":"
                  << (encoder_adapter_luid_available_
                      ? AdapterLuidJson(encoder_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << "}"
                  << std::endl;
    }


    bool CaptureEngine::ResolveSelectedMonitor(const char* backend_name) {
        const auto result = monitor_resolver_.Resolve(monitor_device_path_);
        if (!result.ok()) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(result.error) << ": "
                      << result.diagnostic << std::endl;
            SetCaptureFailure(result.diagnostic);
            return false;
        }

        if (!resolved_monitor_.monitor_device_path.empty()
            && !(resolved_monitor_ == result.monitor)) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::MonitorTopologyChanged)
                      << ": selected monitor transient mapping changed; "
                         "starting a fresh capture generation" << std::endl;
        }
        resolved_monitor_ = result.monitor;
        std::cout << '[' << backend_name << "] Monitor resolved: ";
        std::wcout << resolved_monitor_.friendly_name << L"  "
                   << resolved_monitor_.source_gdi_name << L"  "
                   << resolved_monitor_.monitor_device_path << std::endl;
        std::cout << '[' << backend_name << "] Topology: LUID="
                  << resolved_monitor_.adapter_luid.high_part << ':'
                  << resolved_monitor_.adapter_luid.low_part
                  << " source=" << resolved_monitor_.source_id
                  << " target=" << resolved_monitor_.target_id
                  << " generation=" << resolved_monitor_.topology_generation
                  << std::endl;
        return true;
    }

    bool CaptureEngine::InitializeMonitorCaptureDevice(
        const char* backend_name, IDXGIOutput** selected_output) {
        if (!selected_output) return false;
        *selected_output = nullptr;
        if (!ResolveSelectedMonitor(backend_name)) return false;

        IDXGIFactory1* factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory));
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "CreateDXGIFactory1", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            return false;
        }

        IDXGIAdapter1* capture_adapter = nullptr;
        std::string output_diagnostic;
        if (!monitor::OpenSelectedDxgiOutput(
                factory, resolved_monitor_, &capture_adapter, selected_output,
                &resolved_dxgi_output_,
                output_diagnostic)) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::OutputResolutionFailed)
                      << ": " << output_diagnostic << std::endl;
            SetCaptureFailure(output_diagnostic);
            factory->Release();
            return false;
        }

        DXGI_ADAPTER_DESC1 capture_desc{};
        hr = capture_adapter->GetDesc1(&capture_desc);
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::GetDesc1(selected capture adapter)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            (*selected_output)->Release();
            *selected_output = nullptr;
            capture_adapter->Release();
            factory->Release();
            return false;
        }
        D3D_FEATURE_LEVEL feature_level{};
        hr = D3D11CreateDevice(
            capture_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
            nullptr, 0, D3D11_SDK_VERSION,
            &device_, &feature_level, &context_);
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "D3D11CreateDevice(selected monitor adapter)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            (*selected_output)->Release();
            *selected_output = nullptr;
            capture_adapter->Release();
            factory->Release();
            return false;
        }

        capture_adapter_vendor_ = EncoderVendorFromPciVendorId(
            capture_desc.VendorId);
        capture_device_adapter_luid_ = {
            capture_desc.AdapterLuid.LowPart,
            capture_desc.AdapterLuid.HighPart};
        capture_device_adapter_luid_available_ = true;
        nvidia_device_ = capture_adapter_vendor_ == EncoderVendor::Nvidia;
        DXGI_OUTPUT_DESC output_desc{};
        hr = (*selected_output)->GetDesc(&output_desc);
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIOutput::GetDesc(selected output)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            (*selected_output)->Release();
            *selected_output = nullptr;
            capture_adapter->Release();
            factory->Release();
            return false;
        }
        width_ = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.right
            - output_desc.DesktopCoordinates.left);
        height_ = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.bottom
            - output_desc.DesktopCoordinates.top);
        resolved_desktop_coordinates_ = output_desc.DesktopCoordinates;
        resolved_desktop_coordinates_available_ = true;

        capture_adapter->Release();
        factory->Release();
        std::cout << '[' << backend_name << "] Selected output ready: "
                  << width_ << 'x' << height_
                  << " [" << EncoderVendorName(capture_adapter_vendor_)
                  << " display-owning adapter]"
                  << std::endl;
        return true;
    }


    // Create WGC monitor capture and D3D11 on the selected monitor adapter.
    // Set the source dimensions without DXGI duplication or a staging texture.

    bool CaptureEngine::InitializeWGC() {
        startup_capture_backend_ = "WGC_MONITOR";
        // WGC requires Windows 10 1903+ (build 18362)
        try {
            if (!winrt::Windows::Graphics::Capture::GraphicsCaptureSession::IsSupported()) {
                std::cerr << "[WGC] GraphicsCaptureSession not supported on this system" << std::endl;
                SetCaptureFailure(
                    "api_call=GraphicsCaptureSession::IsSupported result=false");
                return false;
            }
        } catch (winrt::hresult_error const& error) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::IsSupported", error.code().value);
            SetCaptureFailure(failure);
            std::cerr << "[WGC] " << failure << std::endl;
            return false;
        } catch (...) {
            SetCaptureFailure(
                "api_call=GraphicsCaptureSession::IsSupported exception=unknown");
            std::cerr << "[WGC] IsSupported() threw — WGC unavailable" << std::endl;
            return false;
        }

        // Resolve the persistent monitor device path into the current topology,
        // then create D3D11 on the adapter that actually owns that monitor.
        IDXGIOutput* selected_output = nullptr;
        if (!InitializeMonitorCaptureDevice("WGC", &selected_output)) {
            return false;
        }
        selected_output->Release();
        selected_output = nullptr;
        last_capture_failure_detail_.clear();
        const HMONITOR hmonitor = reinterpret_cast<HMONITOR>(
            resolved_monitor_.hmonitor);
        HRESULT hr = S_OK;

        // WinRT session setup
        wgc_state_ = std::make_unique<WGCState>();
        wgc_state_->monitor_item = true;
        monitor_source_invalidated_.store(false, std::memory_order_release);
        const char* current_api = "ID3D11Device::QueryInterface(IDXGIDevice)";
        try {
            // 4a. Wrap ID3D11Device as WinRT IDirect3DDevice
            winrt::com_ptr<IDXGIDevice> dxgi_dev;
            winrt::check_hresult(device_->QueryInterface(dxgi_dev.put()));

            winrt::com_ptr<IInspectable> insp;
            current_api = "CreateDirect3D11DeviceFromDXGIDevice";
            winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev.get(), insp.put()));

            current_api = "IInspectable::as(IDirect3DDevice)";
            wgc_state_->winrt_device =
                insp.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();

            current_api = "RoGetActivationFactory(GraphicsCaptureItem)";
            auto item_interop = winrt::get_activation_factory<
                winrt::Windows::Graphics::Capture::GraphicsCaptureItem,
                IGraphicsCaptureItemInterop>();

            current_api = "IGraphicsCaptureItemInterop::CreateForMonitor";
            winrt::check_hresult(item_interop->CreateForMonitor(
                hmonitor,
                winrt::guid_of<winrt::Windows::Graphics::Capture::GraphicsCaptureItem>(),
                winrt::put_abi(wgc_state_->item)));

            current_api = "GraphicsCaptureItem::Closed(add handler)";
            wgc_state_->item_closed_token = wgc_state_->item.Closed(
                [this](auto&, auto&) {
                    monitor_source_invalidated_.store(
                        true, std::memory_order_release);
                    wgc_frame_cv_.notify_one();
                });
            wgc_state_->item_closed_registered = true;

            // Poll a two-slot free-threaded pool at video cadence; no dispatcher or
            // message pump is required.
            current_api = "GraphicsCaptureItem::Size";
            auto sz = wgc_state_->item.Size();
            current_api = "Direct3D11CaptureFramePool::CreateFreeThreaded";
            wgc_state_->frame_pool =
                winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
                    wgc_state_->winrt_device,
                    winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized,
                    2,
                    sz);

            // Apply border policy before StartCapture; reject WGC if suppression fails
            // so Initialize can select DXGI.
            current_api = "Direct3D11CaptureFramePool::CreateCaptureSession";
            wgc_state_->session =
                wgc_state_->frame_pool.CreateCaptureSession(wgc_state_->item);
            if (!ApplyCaptureBorderPolicy("monitor")) {
                std::cerr << "[WGC] Windows privacy border would remain visible; "
                             "falling back to border-free DXGI capture."
                          << std::endl;
                ShutdownWGC();
                ShutdownD3D11();
                return false;
            }


            current_api = "GraphicsCaptureSession::StartCapture";
            wgc_state_->session.StartCapture();

        } catch (winrt::hresult_error const& e) {
            std::string failure = diagnostics::FormatHResultFailure(
                current_api, e.code().value);
            const std::string winrt_message = winrt::to_string(e.message());
            if (!winrt_message.empty()) {
                failure += " winrt_message=\"";
                failure += diagnostics::JsonEscape(winrt_message);
                failure += '\"';
            }
            SetCaptureFailure(failure);
            std::cerr << "[WGC] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::CaptureItemCreationFailed)
                      << ": " << failure << std::endl;
            wgc_state_.reset();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_)  { device_->Release();  device_  = nullptr; }
            nvidia_device_ = false;
            capture_adapter_vendor_ = EncoderVendor::Software;
            return false;
        }

        std::cout << "[WGC] Ready. Capture started ("
                  << width_ << "x" << height_ << ", "
                  << EncoderVendorName(capture_adapter_vendor_)
                  << " capture adapter"
                  << ")" << std::endl;
        last_capture_failure_detail_.clear();
        return true;
    }


    // Request border suppression before StartCapture and verify the live session.
    // If the runtime still requires a border, callers fall back to DXGI.

    bool CaptureEngine::ApplyCaptureBorderPolicy(const char* capture_target) {
        if (!wgc_state_ || !wgc_state_->session) return false;

        CaptureBorderPolicyInput input;
        input.session_interface_checked = true;
        try {
            auto session3 = wgc_state_->session.try_as<
                winrt::Windows::Graphics::Capture::IGraphicsCaptureSession3>();
            input.session_interface_available = static_cast<bool>(session3);
            if (session3) {
                input.property_attempted = true;
                try {
                    // This is the same session-level opt-out used by the old
                    // build and must happen before StartCapture().
                    session3.IsBorderRequired(false);
                    input.property_set_succeeded = true;
                    input.border_required_after_attempt =
                        session3.IsBorderRequired();
                } catch (winrt::hresult_error const& error) {
                    SetCaptureFailure(diagnostics::FormatHResultFailure(
                        "IGraphicsCaptureSession3::IsBorderRequired",
                        error.code().value));
                    input.property_set_succeeded = false;
                } catch (...) {
                    SetCaptureFailure(
                        "api_call=IGraphicsCaptureSession3::IsBorderRequired exception=unknown");
                    input.property_set_succeeded = false;
                }
            }
        } catch (winrt::hresult_error const& error) {
            SetCaptureFailure(diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::QueryInterface(IGraphicsCaptureSession3)",
                error.code().value));
            input.session_interface_available = false;
        } catch (...) {
            SetCaptureFailure(
                "api_call=GraphicsCaptureSession::QueryInterface(IGraphicsCaptureSession3) exception=unknown");
            input.session_interface_available = false;
        }
        const auto decision = EvaluateCaptureBorderPolicy(input);

        std::cout << "[CaptureBorderPolicy] target=" << capture_target
                  << " session_interface="
                  << (input.session_interface_available ? "available" : "unavailable")
                  << " property_attempted=" << (input.property_attempted ? "true" : "false")
                  << " property_set=" << (input.property_set_succeeded ? "true" : "false")
                  << " border_required="
                  << (input.border_required_after_attempt ? "true" : "false")
                  << " effective=" << (decision.effective_borderless ? "true" : "false")
                  << " reason=" << CaptureBorderPolicyReasonName(decision.reason)
                  << std::endl;

        if (!decision.effective_borderless
            && last_capture_failure_detail_.empty()) {
            SetCaptureFailure(
                std::string("api_call=IGraphicsCaptureSession3::IsBorderRequired result=")
                + CaptureBorderPolicyReasonName(decision.reason));
        }

        return decision.effective_borderless;
    }



    void CaptureEngine::ShutdownWGC() {
        if (!wgc_state_) return;
        try {
            if (wgc_state_->item && wgc_state_->item_closed_registered) {
                wgc_state_->item.Closed(wgc_state_->item_closed_token);
                wgc_state_->item_closed_registered = false;
            }
            if (wgc_state_->session)    wgc_state_->session.Close();
            if (wgc_state_->frame_pool) {
                wgc_state_->frame_pool.Close();
            }
        } catch (...) {}
        wgc_state_.reset();
        std::cout << "[WGC] Shutdown complete." << std::endl;
    }


    // Sample the newest WGC image at video cadence and copy it before releasing
    // the borrowed frame. Reuse the owned image on idle ticks so video timing
    // does not depend on desktop change notifications.

    void CaptureEngine::CaptureThreadWGC() {
        // WinRT calls (TryGetNextFrame, surface access) require the thread to be
        // in a COM apartment. Multi-threaded apartment is correct here — we have
        // no message pump and don't need the STA marshaling overhead.
        winrt::init_apartment(winrt::apartment_type::multi_threaded);

        std::cout << "[CaptureThread/WGC] Started ("
                  << fps_ << " fps, "
                  << (nvenc_active_ ? GetActiveEncoderName() : "software")
                  << " path, "
                  << (replay_encoder_cpu_input_ ? "hybrid CPU-input"
                      : (nvenc_active_ ? "same-adapter GPU input" : "readback"))
                  << ")" << std::endl;

        // WGC publishes changes, not a continuous video clock. Keep one
        // owned image and sample it at the encoder cadence, including an idle
        // desktop. Never retain a surface borrowed from the WGC frame pool.
        const auto interval = std::chrono::microseconds(1000000 / fps_);
        auto next_tick = std::chrono::steady_clock::now();
        winrt::com_ptr<ID3D11Texture2D> latest_texture;
        uint64_t repeated_frames = 0;
        if (replay_encoder_) {
            replay_encoder_->SetCaptureGeneration(
                capture_generation_.load(std::memory_order_relaxed));
        }
        uint32_t consecutive_frame_errors = 0;
        CaptureFocusPolicy focus_policy;

        while (running_.load(std::memory_order_relaxed)) {

            next_tick += interval;
            {
                std::unique_lock<std::mutex> lk(wgc_frame_mutex_);
                wgc_frame_cv_.wait_until(lk, next_tick, [this] {
                    return monitor_source_invalidated_.load(std::memory_order_acquire)
                        || !running_.load(std::memory_order_relaxed);
                });
            }
            const auto tick_now = std::chrono::steady_clock::now();
            if (tick_now > next_tick + interval) next_tick = tick_now;

            if (!running_.load(std::memory_order_relaxed)) break;
            if (monitor_source_invalidated_.load(std::memory_order_acquire)) {
                const char* source_kind = wgc_state_->monitor_item
                    ? "monitor" : "window";
                std::cerr << "[CaptureThread/WGC] "
                          << monitor::ToString(
                                 monitor::MonitorResolveError::MonitorDisconnected)
                          << ": selected " << source_kind
                          << " capture item closed" << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                          << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                          << "\"api_call\":\"GraphicsCaptureItem::Closed\","
                          << "\"source_kind\":\"" << source_kind << "\","
                          << "\"monitor_device_path\":\""
                          << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                                 resolved_monitor_.monitor_device_path))
                          << "\",\"monitor_adapter_luid\":"
                          << AdapterLuidJson(resolved_monitor_.adapter_luid) << ','
                          << "\"detail\":\"selected capture item closed\"}"
                          << std::endl;
                SetCaptureFailure(std::string("selected WGC ") + source_kind
                    + " capture item closed");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                ClearReplayForRecovery();
                running_.store(false);
                break;
            }

            const char* current_frame_api =
                "Direct3D11CaptureFramePool::TryGetNextFrame";
            try {
                current_frame_api = "ID3D11Device::GetDeviceRemovedReason";
                winrt::check_hresult(device_->GetDeviceRemovedReason());
                const auto focus = focus_policy.Observe(focus_gated_ && target_hwnd_ != 0
                    && GetForegroundWindow() != reinterpret_cast<HWND>(target_hwnd_));
                if (focus.paused) {
                    latest_texture = nullptr;
                    capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                } else if (focus.resumed) {
                    // Start fresh history after a focus gap. Joining pre-tab
                    // and post-tab packets creates a sparse timeline that the
                    // save validator correctly rejects. Do not reuse an image
                    // from the desktop while waiting for a new game frame.
                    latest_texture = nullptr;
                    ClearReplayForRecovery();
                    capture_generation_.fetch_add(1, std::memory_order_acq_rel);
                    if (replay_encoder_) replay_encoder_->SetCaptureGeneration(
                        capture_generation_.load(std::memory_order_acquire));
                    if (replay_encoder_) replay_encoder_->RequestKeyframe();
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_PAUSED);
                    std::cout << "[CaptureThread/WGC] Game focus restored; rebuilding replay history."
                              << std::endl;
                }
                bool new_image = false;
                // Drain both slots at each tick. Notifications can coalesce;
                // TryGetNextFrame is the sole authority for queued images.
                for (int slot = 0; slot < 2; ++slot) {
                    auto frame = wgc_state_->frame_pool.TryGetNextFrame();
                    if (!frame) break;
                    wgc::FrameLease frame_lease(frame);
                    if (focus.discard_queued_frames) continue;
                    current_frame_api = "Direct3D11CaptureFrame::ContentSize";
                    const auto content_size = frame.ContentSize();
                    if (content_size.Width != static_cast<int32_t>(width_)
                        || content_size.Height != static_cast<int32_t>(height_)) {
                        const char* source_kind = wgc_state_->monitor_item
                            ? "monitor" : "window";
                        std::cerr << "[CaptureThread/WGC] capture " << source_kind
                                  << " dimensions changed" << std::endl;
                        std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                                  << "\"subsystem\":\"capture\","
                                  << "\"event\":\"runtime_failed\","
                                  << "\"state\":\"FAILED\","
                                  << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                                  << "\"api_call\":\"Direct3D11CaptureFrame::ContentSize\","
                                  << "\"source_kind\":\"" << source_kind << "\","
                                  << "\"expected_width\":" << width_ << ','
                                  << "\"expected_height\":" << height_ << ','
                                  << "\"actual_width\":" << content_size.Width << ','
                                  << "\"actual_height\":" << content_size.Height << "}"
                                  << std::endl;
                        SetCaptureFailure(std::string("WGC ") + source_kind
                            + " dimensions changed; a fresh capture generation is required");
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        ClearReplayForRecovery();
                        running_.store(false);
                        break;
                    }

                    // Extract ID3D11Texture2D from the WGC surface
                    current_frame_api = "Direct3D11CaptureFrame::Surface";
                    auto surface = frame.Surface();

                    // IDirect3DDxgiInterfaceAccess is a COM interface in
                    // Windows::Graphics::DirectX::Direct3D11 — not a WinRT-projected
                    // type, so winrt::as<>() cannot be used. QueryInterface directly.
                    using DxgiAccess = Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess;
                    winrt::com_ptr<DxgiAccess> interop;
                    current_frame_api =
                        "IDirect3DSurface::QueryInterface(IDirect3DDxgiInterfaceAccess)";
                    winrt::check_hresult(
                        reinterpret_cast<IUnknown*>(winrt::get_abi(surface))->QueryInterface(
                            __uuidof(DxgiAccess), reinterpret_cast<void**>(interop.put())));

                    winrt::com_ptr<ID3D11Texture2D> tex;
                    current_frame_api =
                        "IDirect3DDxgiInterfaceAccess::GetInterface(ID3D11Texture2D)";
                    winrt::check_hresult(interop->GetInterface(IID_PPV_ARGS(tex.put())));
                    if (!ValidateCaptureTexture(tex.get(), "WGC")) {
                        ClearReplayForRecovery();
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                        break;
                    }
                    if (!latest_texture) {
                        D3D11_TEXTURE2D_DESC desc{};
                        tex->GetDesc(&desc);
                        desc.Usage = D3D11_USAGE_DEFAULT;
                        desc.BindFlags = 0;
                        desc.CPUAccessFlags = 0;
                        desc.MiscFlags = 0;
                        current_frame_api = "ID3D11Device::CreateTexture2D(latest WGC image)";
                        winrt::check_hresult(device_->CreateTexture2D(
                            &desc, nullptr, latest_texture.put()));
                    }
                    context_->CopyResource(latest_texture.get(), tex.get());
                    source_textures_received_.fetch_add(1, std::memory_order_relaxed);
                    new_image = true;
                } // Close every borrowed frame before encoding the owned copy.
                if (!running_.load(std::memory_order_relaxed)) break;
                if (!latest_texture) continue;

                // Do not publish a cached image after the item changes size.
                current_frame_api = "GraphicsCaptureItem::Size";
                const auto live_size = wgc_state_->item.Size();
                if (live_size.Width != static_cast<int32_t>(width_)
                    || live_size.Height != static_cast<int32_t>(height_)) {
                    SetCaptureFailure("WGC source dimensions changed; restart required");
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    ClearReplayForRecovery();
                    running_.store(false);
                    break;
                }
                capture_health_flags_.fetch_or(CAPTURE_HEALTH_ACTIVE);
                if (!new_image) ++repeated_frames;
                const auto& tex = latest_texture;
                LARGE_INTEGER now;
                QueryPerformanceCounter(&now);

                if (nvenc_active_ && !replay_encoder_cpu_input_) {
                    // Same-adapter compressed replay (native NVENC or AMF).
                    // A GPU CopyResource feeds the encoder-owned texture.
                    ID3D11Texture2D* encode_texture = PrepareEncodeTexture(tex.get());
                    if (!encode_texture || !EncodeGpuReplayTexture(
                            encode_texture, now.QuadPart,
                            frames_captured_.load(std::memory_order_relaxed) + 1)) {
                        break;
                    }
                }
                else if (nvenc_active_ && replay_encoder_cpu_input_
                    && staging_texture_) {
                    // NVENC CPU-input path (Optimus).
                    // WGC on Intel adapter; NVENC on separate NVIDIA device.
                    // Map the staging texture on the Intel device and memcpy
                    // into the NVENC system-memory input buffer.
                    context_->CopyResource(staging_texture_, tex.get());

                    D3D11_MAPPED_SUBRESOURCE mapped{};
                    HRESULT hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                    if (FAILED(hr)) {
                        std::cerr << "[CaptureThread/WGC] Texture map failed (Optimus): 0x"
                                  << std::hex << hr << std::dec << std::endl;
                        continue;
                    }
                    if (!ValidateMappedCaptureRowPitch(mapped.RowPitch, "WGC")) {
                        context_->Unmap(staging_texture_, 0);
                        ClearReplayForRecovery();
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                        break;
                    }

                    SampleContentBGRA(
                        static_cast<const uint8_t*>(mapped.pData), mapped.RowPitch,
                        width_, height_, frames_captured_.load(std::memory_order_relaxed) + 1);

                    const uint8_t* encode_data = CropMappedData(
                        static_cast<const uint8_t*>(mapped.pData), mapped.RowPitch);
                    bool encoded = false;
                    for (uint32_t attempt = 1; attempt <= 3 && !encoded; ++attempt) {
                        encoded = replay_encoder_->EncodeFrameCPU(
                            encode_data,
                            mapped.RowPitch, now.QuadPart);
                        if (!encoded && attempt < 3) {
                            std::cerr << "[CaptureThread/WGC] Transient hybrid encoder "
                                      << "submission failure; retrying (" << attempt
                                      << "/3)." << std::endl;
                            std::this_thread::sleep_for(std::chrono::milliseconds(2));
                        }
                    }
                    context_->Unmap(staging_texture_, 0);
                    if (!encoded) {
                        FailReplayEncoder("hybrid NVENC CPU-input submission");
                        break;
                    }
                }
                else {
                    // Legacy raw-frame readback
                    context_->CopyResource(staging_texture_, tex.get());

                    D3D11_MAPPED_SUBRESOURCE mapped{};
                    HRESULT hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                    if (FAILED(hr)) {
                        std::cerr << "[CaptureThread/WGC] Texture map failed: 0x"
                                  << std::hex << hr << std::dec << std::endl;
                        continue;
                    }
                    if (!ValidateMappedCaptureRowPitch(mapped.RowPitch, "WGC")) {
                        context_->Unmap(staging_texture_, 0);
                        ClearReplayForRecovery();
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                        break;
                    }

                    const uint8_t* full_src = static_cast<const uint8_t*>(mapped.pData);
                    const uint8_t* src = CropMappedData(full_src, mapped.RowPitch);
                    const size_t   row = static_cast<size_t>(crop_width_) * 4;
                    const size_t   write_pos = ring_head_.load(std::memory_order_relaxed);
                    const size_t   slot_idx  = write_pos % max_frames_;
                    uint8_t*       dst = frame_pool_.GetSlot(slot_idx);

                    SampleContentBGRA(
                        full_src, mapped.RowPitch, width_, height_,
                        frames_captured_.load(std::memory_order_relaxed) + 1);

                    if (mapped.RowPitch == static_cast<UINT>(row)) {
                        std::memcpy(dst, src, row * crop_height_);
                    } else {
                        for (uint32_t y = 0; y < crop_height_; y++) {
                            std::memcpy(dst + y * row, src + y * mapped.RowPitch, row);
                        }
                    }

                    context_->Unmap(staging_texture_, 0);

                    ring_head_.fetch_add(1, std::memory_order_release);
                    size_t prev = ring_count_.load(std::memory_order_relaxed);
                    if (prev < max_frames_)
                        ring_count_.fetch_add(1, std::memory_order_relaxed);
                }

                frames_captured_.fetch_add(1, std::memory_order_relaxed);
                consecutive_frame_errors = 0;

            } catch (winrt::hresult_error const& e) {
                std::cerr << "[CaptureThread/WGC] Frame error: 0x"
                          << std::hex << e.code().value << std::dec << std::endl;
                if (++consecutive_frame_errors >= 30) {
                    std::cerr << "[CaptureThread/WGC] Too many consecutive frame errors"
                              << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     current_frame_api, e.code().value)
                              << ",\"consecutive_failures\":"
                              << consecutive_frame_errors << "}"
                              << std::endl;
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                }
            }
        }

        std::cout << "[CaptureThread/WGC] Stopped. Frames captured: "
                  << frames_captured_.load() << " repeated idle frames: "
                  << repeated_frames << std::endl;
        if (capture_health_flags_.load() != CAPTURE_HEALTH_BACKEND_FAILED)
            capture_health_flags_.store(CAPTURE_HEALTH_NONE);

        winrt::uninit_apartment();
    }


    // Create WGC capture for target_hwnd_ using the window content dimensions.
    // The engine must restart if resizing changes the encoder input dimensions.

    bool CaptureEngine::InitializeWindowCapture() {
        startup_capture_backend_ = "WGC_WINDOW";
        try {
            if (!winrt::Windows::Graphics::Capture::GraphicsCaptureSession::IsSupported()) {
                std::cerr << "[WinCapture] GraphicsCaptureSession not supported" << std::endl;
                SetCaptureFailure(
                    "api_call=GraphicsCaptureSession::IsSupported result=false");
                return false;
            }
        } catch (winrt::hresult_error const& error) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::IsSupported", error.code().value);
            SetCaptureFailure(failure);
            std::cerr << "[WinCapture] " << failure << std::endl;
            return false;
        } catch (...) {
            SetCaptureFailure(
                "api_call=GraphicsCaptureSession::IsSupported exception=unknown");
            std::cerr << "[WinCapture] IsSupported() threw — WGC unavailable" << std::endl;
            return false;
        }

        HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
        if (!IsWindow(hwnd)) {
            SetCaptureFailure("api_call=IsWindow result=false");
            std::cerr << "[WinCapture] HWND 0x" << std::hex << target_hwnd_
                      << std::dec << " is not a valid window" << std::endl;
            return false;
        }

        // A window can move between GPUs on hybrid systems. Resolve the
        // monitor Windows currently assigns to this HWND and bind the WGC
        // D3D11 device to that monitor's adapter LUID. Never use adapter 0 or
        // the primary display as an implicit fallback.
        const HMONITOR window_monitor = MonitorFromWindow(
            hwnd, MONITOR_DEFAULTTONULL);
        if (!window_monitor) {
            SetCaptureFailure("api_call=MonitorFromWindow result=null");
            return false;
        }
        const auto topology = monitor_topology_source_.QueryActiveTopology();
        const auto window_monitor_it = std::find_if(
            topology.monitors.begin(), topology.monitors.end(),
            [window_monitor](const monitor::MonitorTopologyEntry& entry) {
                return entry.hmonitor == reinterpret_cast<uintptr_t>(window_monitor);
            });
        if (!topology.ok() || window_monitor_it == topology.monitors.end()) {
            SetCaptureFailure(
                "api_call=WindowsMonitorTopologySource::QueryActiveTopology "
                "reason=window_monitor_not_resolved");
            return false;
        }
        resolved_monitor_ = *window_monitor_it;
        // The HWND's monitor is authoritative for this capture generation.
        // Persist its identity before WGC/DXGI fallback setup so every later
        // backend resolves the same physical display instead of the originally
        // configured or primary monitor.
        monitor_device_path_ = monitor::NormalizeMonitorDevicePath(
            resolved_monitor_.monitor_device_path);

        // Create one WGC device and keep replay encoding on that exact adapter.
        // Do not enumerate another vendor merely because it exists elsewhere in
        // the machine; cross-adapter window capture is not alpha-qualified.
        D3D_FEATURE_LEVEL feature_level{};
        IDXGIFactory1* window_factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1),
            reinterpret_cast<void**>(&window_factory));
        IDXGIAdapter1* window_adapter = nullptr;
        if (SUCCEEDED(hr) && window_factory) {
            for (UINT index = 0; ; ++index) {
                IDXGIAdapter1* candidate = nullptr;
                const HRESULT enumerated = window_factory->EnumAdapters1(index, &candidate);
                if (enumerated == DXGI_ERROR_NOT_FOUND) break;
                if (FAILED(enumerated) || !candidate) {
                    hr = enumerated;
                    if (candidate) candidate->Release();
                    break;
                }
                DXGI_ADAPTER_DESC1 description{};
                const HRESULT described = candidate->GetDesc1(&description);
                if (SUCCEEDED(described)
                    && !(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                    && description.AdapterLuid.LowPart
                        == static_cast<LONG>(resolved_monitor_.adapter_luid.low_part)
                    && description.AdapterLuid.HighPart
                        == resolved_monitor_.adapter_luid.high_part) {
                    window_adapter = candidate;
                    break;
                }
                candidate->Release();
            }
        }
        if (!window_adapter) {
            if (window_factory) window_factory->Release();
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIFactory1::EnumAdapters1(window monitor adapter)",
                FAILED(hr) ? hr : DXGI_ERROR_NOT_FOUND);
            SetCaptureFailure(failure + " reason=window_monitor_adapter_not_found");
            return false;
        }
        hr = D3D11CreateDevice(
            window_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
            nullptr, 0, D3D11_SDK_VERSION,
            &device_, &feature_level, &context_);
        window_adapter->Release();
        window_factory->Release();
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "D3D11CreateDevice(window capture)", hr);
            SetCaptureFailure(failure);
            std::cerr << "[WinCapture] " << failure << std::endl;
            return false;
        }
        capture_adapter_vendor_ = QueryD3D11DeviceVendor(device_);
        nvidia_device_ = capture_adapter_vendor_ == EncoderVendor::Nvidia;
        std::string capture_luid_diagnostic;
        capture_device_adapter_luid_available_ = QueryD3D11DeviceAdapterLuid(
            device_, capture_device_adapter_luid_, capture_luid_diagnostic);
        if (!capture_device_adapter_luid_available_) {
            std::cerr << "[WinCapture] Could not resolve capture device adapter LUID: "
                      << capture_luid_diagnostic << std::endl;
        }

        // Steps 3-8: WinRT capture session (exception-safe)
        wgc_state_ = std::make_unique<WGCState>();
        const char* current_api = "ID3D11Device::QueryInterface(IDXGIDevice)";
        try {
            winrt::com_ptr<IDXGIDevice> dxgi_dev;
            winrt::check_hresult(device_->QueryInterface(dxgi_dev.put()));
            winrt::com_ptr<IInspectable> insp;
            current_api = "CreateDirect3D11DeviceFromDXGIDevice";
            winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev.get(), insp.put()));
            current_api = "IInspectable::as(IDirect3DDevice)";
            wgc_state_->winrt_device =
                insp.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();

            current_api = "RoGetActivationFactory(GraphicsCaptureItem)";
            auto item_interop = winrt::get_activation_factory<
                winrt::Windows::Graphics::Capture::GraphicsCaptureItem,
                IGraphicsCaptureItemInterop>();
            current_api = "IGraphicsCaptureItemInterop::CreateForWindow";
            winrt::check_hresult(item_interop->CreateForWindow(
                hwnd,
                winrt::guid_of<winrt::Windows::Graphics::Capture::GraphicsCaptureItem>(),
                winrt::put_abi(wgc_state_->item)));
            current_api = "GraphicsCaptureItem::Closed(add handler)";
            monitor_source_invalidated_.store(false, std::memory_order_release);
            wgc_state_->item_closed_token = wgc_state_->item.Closed(
                [this](auto&, auto&) {
                    monitor_source_invalidated_.store(
                        true, std::memory_order_release);
                    wgc_frame_cv_.notify_one();
                });
            wgc_state_->item_closed_registered = true;

            current_api = "GraphicsCaptureItem::Size";
            auto sz = wgc_state_->item.Size();
            width_  = static_cast<uint32_t>(sz.Width);
            height_ = static_cast<uint32_t>(sz.Height);
            std::cout << "[WinCapture] Window content size: " << width_ << "x" << height_ << std::endl;

            // Use the same free-threaded, two-slot pool as monitor capture.
            current_api = "Direct3D11CaptureFramePool::CreateFreeThreaded";
            wgc_state_->frame_pool =
                winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
                    wgc_state_->winrt_device,
                    winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized,
                    2,
                    sz);

            // Apply monitor capture border policy; return false for DXGI fallback
            // if the runtime requires a border.
            current_api = "Direct3D11CaptureFramePool::CreateCaptureSession";
            wgc_state_->session =
                wgc_state_->frame_pool.CreateCaptureSession(wgc_state_->item);
            if (!ApplyCaptureBorderPolicy("window")) {
                std::cerr << "[WinCapture] Windows privacy border would remain visible; "
                             "falling back to border-free monitor capture."
                          << std::endl;
                ShutdownWGC();
                ShutdownD3D11();
                return false;
            }


            current_api = "GraphicsCaptureSession::StartCapture";
            wgc_state_->session.StartCapture();

        } catch (winrt::hresult_error const& e) {
            std::string failure = diagnostics::FormatHResultFailure(
                current_api, e.code().value);
            const std::string winrt_message = winrt::to_string(e.message());
            if (!winrt_message.empty()) {
                failure += " winrt_message=\"";
                failure += diagnostics::JsonEscape(winrt_message);
                failure += '\"';
            }
            SetCaptureFailure(failure);
            std::cerr << "[WinCapture] " << failure << std::endl;
            wgc_state_.reset();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_)  { device_->Release();  device_  = nullptr; }
            nvidia_device_ = false;
            capture_adapter_vendor_ = EncoderVendor::Software;
            return false;
        }

        std::cout << "[WinCapture] Ready  "
                  << width_ << "x" << height_ << "  "
                  << EncoderVendorName(capture_adapter_vendor_)
                  << " capture adapter"
                  << std::endl;
        last_capture_failure_detail_.clear();
        return true;
    }


    // InitializeD3D11 - exact selected adapter/output fallback

    bool CaptureEngine::InitializeD3D11() {
        startup_capture_backend_ = "DXGI_OUTPUT_DUPLICATION";
        nvidia_device_ = false;
        capture_adapter_vendor_ = EncoderVendor::Software;
        IDXGIOutput* selected_output = nullptr;
        if (!InitializeMonitorCaptureDevice("DXGI", &selected_output)) {
            return false;
        }
        last_capture_failure_detail_.clear();

        IDXGIOutput1* output1 = nullptr;
        HRESULT hr = selected_output->QueryInterface(
            __uuidof(IDXGIOutput1), reinterpret_cast<void**>(&output1));
        if (FAILED(hr) || !output1) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIOutput::QueryInterface(IDXGIOutput1)", hr);
            SetCaptureFailure(failure);
            std::cerr << "[D3D11] " << failure << std::endl;
            selected_output->Release();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            return false;
        }
        hr = output1->DuplicateOutput(device_, &duplication_);
        output1->Release();
        selected_output->Release();
        if (FAILED(hr) || !duplication_) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIOutput1::DuplicateOutput", hr);
            SetCaptureFailure(failure);
            std::cerr << "[D3D11] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::OutputResolutionFailed)
                      << ": " << failure << std::endl;
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            return false;
        }

        std::cout << "[D3D11] Ready - " << width_ << "x" << height_
                  << (nvidia_device_
                      ? "  [selected NVIDIA adapter - GPU zero-copy enabled]"
                      : "  [selected display-owning adapter]")
                  << std::endl;
        std::cout << "[D3D11] Duplication: " << (duplication_ ? "OK" : "NULL") << std::endl;
        std::cout << "[D3D11] Readback texture: deferred until fallback policy"
                  << std::endl;
        last_capture_failure_detail_.clear();
        return true;
    }


    // ShutdownD3D11 - unchanged

    void CaptureEngine::ShutdownD3D11() {
        if (health_staging_texture_) {
            health_staging_texture_->Release();
            health_staging_texture_ = nullptr;
        }
        if (crop_texture_) { crop_texture_->Release(); crop_texture_ = nullptr; }
        if (staging_texture_) { staging_texture_->Release(); staging_texture_ = nullptr; }
        if (duplication_)     { duplication_->Release();     duplication_ = nullptr; }
        if (context_)         { context_->Release();         context_ = nullptr; }
        if (device_)          { device_->Release();          device_ = nullptr; }
        nvidia_device_ = false;
        capture_adapter_vendor_ = EncoderVendor::Software;
        // nvenc_device_ / nvenc_context_ are intentionally NOT released here.
        // They must outlive the NVENC encoder session (which is finalized in Shutdown()
        // before this is called). On ACCESS_LOST reinit they stay valid.
    }


} // namespace fthr
