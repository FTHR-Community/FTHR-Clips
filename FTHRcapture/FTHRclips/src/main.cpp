// Native Windows startup and shared-memory command dispatch.
// CaptureEngine owns capture, encoding, and save workers.
// The positional startup contract is documented in docs/engine-startup.md.

#include "shared_memory.h"
#include "capture_engine.h"
#include "hardware_encoder.h"  // DetectNVENC() for pre-init logging
#include "windows_microphone_audio_provider.h"
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <iostream>
#include <Windows.h>


// Argument parsing helpers

static uint32_t ParseArgU32(int argc, char* argv[], int index, uint32_t default_val) {
    if (index >= argc) return default_val;
    int val = std::atoi(argv[index]);
    if (val < 0) return default_val;
    return static_cast<uint32_t>(val);
}

static double ParseArgDouble(
    int argc, char* argv[], int index, double default_val) {
    if (index >= argc || !argv[index]) return default_val;
    errno = 0;
    char* end = nullptr;
    const double value = std::strtod(argv[index], &end);
    if (errno != 0 || end == argv[index] || (end && *end != '\0')
        || !std::isfinite(value)) return default_val;
    return value;
}

// Parse a 64-bit unsigned integer from argv (used for HWND on 64-bit Windows).
static uintptr_t ParseArgU64(int argc, char* argv[], int index, uintptr_t default_val) {
    if (index >= argc) return default_val;
    return static_cast<uintptr_t>(std::strtoull(argv[index], nullptr, 10));
}

static std::wstring ParseArgUtf8(int argc, char* argv[], int index) {
    if (index >= argc || !argv[index] || argv[index][0] == '\0') return {};
    const int length = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, argv[index], -1, nullptr, 0);
    if (length <= 1) return {};
    std::wstring value(static_cast<size_t>(length), L'\0');
    if (MultiByteToWideChar(
            CP_UTF8, MB_ERR_INVALID_CHARS, argv[index], -1,
            value.data(), length) == 0) {
        return {};
    }
    value.pop_back();
    return value;
}

static std::string Utf8FromWide(const std::wstring& value) {
    if (value.empty()) return {};
    const int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
        value.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (length <= 1) return {};
    std::string result(static_cast<size_t>(length), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.c_str(), -1,
            result.data(), length, nullptr, nullptr) != length) {
        return {};
    }
    result.pop_back();
    return result;
}

static std::wstring WideFromUtf8(const std::string& value) {
    if (value.empty()) return {};
    const int length = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.c_str(), -1, nullptr, 0);
    if (length <= 1) return {};
    std::wstring result(static_cast<size_t>(length), L'\0');
    if (MultiByteToWideChar(
            CP_UTF8, MB_ERR_INVALID_CHARS, value.c_str(), -1,
            result.data(), length) != length) {
        return {};
    }
    result.pop_back();
    return result;
}

static std::string EscapeJson(const std::string& value) {
    std::string result;
    result.reserve(value.size() + 8);
    for (const unsigned char character : value) {
        switch (character) {
        case '\\': result += "\\\\"; break;
        case '"': result += "\\\""; break;
        case '\n': result += "\\n"; break;
        case '\r': result += "\\r"; break;
        case '\t': result += "\\t"; break;
        default:
            if (character >= 0x20) result.push_back(static_cast<char>(character));
            break;
        }
    }
    return result;
}

static int ListMicrophones() {
    std::vector<fthr::WindowsMicrophoneEndpoint> endpoints;
    std::string error;
    if (!fthr::EnumerateWindowsMicrophoneEndpoints(&endpoints, &error)) {
        std::cerr << "{\"schema_version\":1,\"error\":\""
                  << EscapeJson(error) << "\"}" << std::endl;
        return 1;
    }
    std::cout << "{\"schema_version\":1,\"microphones\":[";
    for (size_t index = 0; index < endpoints.size(); ++index) {
        const auto& endpoint = endpoints[index];
        if (index) std::cout << ',';
        std::cout << "{\"endpoint_id\":\"" << EscapeJson(Utf8FromWide(endpoint.endpoint_id))
                  << "\",\"display_name\":\"" << EscapeJson(endpoint.display_name)
                  << "\",\"device_state\":" << endpoint.device_state
                  << ",\"is_default\":" << (endpoint.is_default ? "true" : "false")
                  << '}';
    }
    std::cout << "]}" << std::endl;
    return 0;
}

static void PrintConfig(const fthr::CaptureConfig& cfg) {
    std::cout << "  Framerate    : " << cfg.framerate << " fps" << std::endl;
    std::cout << "  Buffer       : " << cfg.buffer_seconds << " sec" << std::endl;
    std::cout << "  Target res   : ";
    if (cfg.target_width == 0 || cfg.target_height == 0)
        std::cout << "Native" << std::endl;
    else
        std::cout << cfg.target_width << "x" << cfg.target_height << std::endl;
    std::cout << "  Bitrate      : " << cfg.bitrate_kbps << " kbps" << std::endl;
    std::cout << "  Video codec  : " << fthr::VideoCodecName(cfg.video_codec)
              << std::endl;
    std::cout << "  Encoder pref : "
              << static_cast<uint32_t>(cfg.encoder_preference) << std::endl;
    std::cout << "  NVENC preset : P" << cfg.encoder_preset << std::endl;
    std::cout << "  Raw budget   : " << cfg.max_buffer_mb
              << " MB (diagnostic only; automatic fallback disabled)" << std::endl;

    using Mode = fthr::CaptureConfig::CaptureModeEnum;
    if (cfg.capture_mode == Mode::WINDOW && cfg.target_hwnd != 0) {
        std::cout << "  Capture mode : Window  (HWND=0x" << std::hex << cfg.target_hwnd
                  << std::dec << ")" << std::endl;
    } else {
        std::cout << "  Capture mode : Desktop" << std::endl;
    }

    using Scl = fthr::CaptureConfig::ScalingModeEnum;
    std::cout << "  Scaling mode : "
              << (cfg.scaling_mode == Scl::FIT ? "Fit (letterbox)" : "Stretch")
              << std::endl;
    std::cout << "  Audio        : "
              << (cfg.audio_enabled ? "Enabled" : "Disabled (user setting)")
              << std::endl;
    std::cout << "  Audio mode   : "
              << (cfg.separate_audio_enabled ? "Separated tracks" : "Combined")
              << std::endl;
    if (cfg.audio_enabled) {
        std::cout << "  Microphone   : "
                  << (cfg.microphone_endpoint_id.empty() ? "Default microphone"
                                                         : "Explicit native endpoint")
                  << std::endl;
    }
    if (!cfg.monitor_device_path.empty()) {
        std::wcout << L"  Monitor path : " << cfg.monitor_device_path << std::endl;
    }
}


// Entry point

int main(int argc, char* argv[]) {
    if (argc == 2 && std::strcmp(argv[1], "--list-microphones") == 0) {
        return ListMicrophones();
    }
    std::cout << "FTHR Capture Engine starting..." << std::endl;
    char diagnostic_session[64] = {};
    const DWORD diagnostic_length = GetEnvironmentVariableA(
        "FTHR_DIAGNOSTIC_SESSION_ID", diagnostic_session,
        static_cast<DWORD>(sizeof(diagnostic_session)));
    std::string safe_session;
    if (diagnostic_length > 0 && diagnostic_length < sizeof(diagnostic_session)) {
        for (DWORD index = 0; index < diagnostic_length; ++index) {
            const char character = diagnostic_session[index];
            if ((character >= '0' && character <= '9')
                || (character >= 'a' && character <= 'f')
                || (character >= 'A' && character <= 'F')
                || character == '-') {
                safe_session.push_back(character);
            }
        }
    }
    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"engine\","
              << "\"event\":\"session_correlated\",\"session_id\":\""
              << (safe_session.empty() ? "unavailable:not_provided" : safe_session)
              << "\"}" << std::endl;

    // 1. Parse command-line arguments into CaptureConfig
    fthr::CaptureConfig config;

    config.framerate = ParseArgU32(argc, argv, 1, 60);
    config.buffer_seconds = ParseArgU32(argc, argv, 2, 30);
    config.target_width = ParseArgU32(argc, argv, 3, 0);
    config.target_height = ParseArgU32(argc, argv, 4, 0);
    config.bitrate_kbps = ParseArgU32(argc, argv, 5, 16000);
    config.max_buffer_mb = ParseArgU32(argc, argv, 6, 2048);

    // argv[7] = capture mode  (0 = desktop, 1 = window)
    // argv[8] = target HWND   (64-bit decimal; 0 = not set)
    {
        uint32_t mode = ParseArgU32(argc, argv, 7, 0);
        config.capture_mode = (mode == 1)
            ? fthr::CaptureConfig::CaptureModeEnum::WINDOW
            : fthr::CaptureConfig::CaptureModeEnum::DESKTOP;
        config.target_hwnd = ParseArgU64(argc, argv, 8, 0);
    }

    // argv[9] = scaling mode  (0 = stretch, 1 = fit/letterbox)
    {
        uint32_t scl = ParseArgU32(argc, argv, 9, 0);
        config.scaling_mode = (scl == 1)
            ? fthr::CaptureConfig::ScalingModeEnum::FIT
            : fthr::CaptureConfig::ScalingModeEnum::STRETCH;
    }

    // argv[10] = capture_monitor  (stable Windows monitor device path)
    // argv[11] = codec_pref       (0=auto/H.264, 1=H.264, 2=HEVC, 3=AV1)
    // argv[12] = encoder_preset   (same)
    // argv[13] = retired multiband slot (ignored)
    // argv[14] = audio_enabled    (0 = disable WASAPI loopback capture)
    config.monitor_device_path = ParseArgUtf8(argc, argv, 10);
    const uint32_t codec_preference_arg = ParseArgU32(argc, argv, 11, 0);
    switch (codec_preference_arg) {
    case 0:
    case 1:
        config.video_codec = fthr::VideoCodec::H264;
        break;
    case 2:
        config.video_codec = fthr::VideoCodec::HEVC;
        break;
    case 3:
        config.video_codec = fthr::VideoCodec::AV1;
        break;
    default:
        std::cerr << "[Config] Unknown codec preference - using Auto/H.264"
                  << std::endl;
        config.video_codec = fthr::VideoCodec::H264;
        break;
    }
    config.encoder_preset = std::clamp<uint32_t>(
        ParseArgU32(argc, argv, 12, 4), 1, 7);
    config.multiband_enabled = false;
    config.separate_audio_enabled = ParseArgU32(argc, argv, 23, 0) != 0;
    config.audio_enabled = (ParseArgU32(argc, argv, 14, 1) != 0);
    config.microphone_endpoint_id = ParseArgUtf8(argc, argv, 15);
    config.microphone_gain_percent = std::min<uint32_t>(
        ParseArgU32(argc, argv, 16, 100), 200);
    switch (ParseArgU32(argc, argv, 17, 0)) {
    case 1: config.encoder_preference = fthr::EncoderPreference::Nvidia; break;
    case 2: config.encoder_preference = fthr::EncoderPreference::Amd; break;
    case 3: config.encoder_preference = fthr::EncoderPreference::Intel; break;
    case 4: config.encoder_preference = fthr::EncoderPreference::Software; break;
    default: config.encoder_preference = fthr::EncoderPreference::Auto; break;
    }
    config.crop_enabled = (ParseArgU32(argc, argv, 18, 0) != 0);
    config.crop_x = ParseArgDouble(argc, argv, 19, 0.0);
    config.crop_y = ParseArgDouble(argc, argv, 20, 0.0);
    config.crop_width = ParseArgDouble(argc, argv, 21, 1.0);
    config.crop_height = ParseArgDouble(argc, argv, 22, 1.0);

    // 2. Validate all parameters - clamp to safe ranges
    if (config.framerate == 0 || config.framerate > 360) {
        std::cerr << "[Config] Invalid framerate " << config.framerate
            << " - clamping to 60" << std::endl;
        config.framerate = 60;
    }

    if (config.buffer_seconds == 0 || config.buffer_seconds > 1800) {
        std::cerr << "[Config] Invalid buffer_seconds " << config.buffer_seconds
            << " - clamping to 30" << std::endl;
        config.buffer_seconds = 30;
    }

    if ((config.target_width == 0) != (config.target_height == 0)) {
        std::cerr << "[Config] Mismatched resolution "
            << config.target_width << "x" << config.target_height
            << " - falling back to native" << std::endl;
        config.target_width = 0;
        config.target_height = 0;
    }

    if (config.target_width > 7680 || config.target_height > 4320) {
        std::cerr << "[Config] Resolution " << config.target_width << "x"
            << config.target_height << " exceeds 8K - falling back to native" << std::endl;
        config.target_width = 0;
        config.target_height = 0;
    }

    if (config.bitrate_kbps < 500 || config.bitrate_kbps > 60000) {
        std::cerr << "[Config] Invalid bitrate_kbps " << config.bitrate_kbps
            << " - clamping to 16000" << std::endl;
        config.bitrate_kbps = 16000;
    }

    if (config.max_buffer_mb < 64) {
        std::cerr << "[Config] max_buffer_mb " << config.max_buffer_mb
            << " too small - clamping to 64 MB" << std::endl;
        config.max_buffer_mb = 64;
    }
    if (config.max_buffer_mb > 16384) {
        std::cerr << "[Config] max_buffer_mb " << config.max_buffer_mb
            << " too large - clamping to 16384 MB" << std::endl;
        config.max_buffer_mb = 16384;
    }

    std::cout << "Configuration:" << std::endl;
    PrintConfig(config);

    // Log NVENC capabilities for diagnostics. CaptureEngine independently
    // selects and initializes the requested hardware backend.
    std::cout << "\n=== Hardware Encoding Detection ===" << std::endl;

    fthr::NVENCDetectionResult nvenc_info = fthr::DetectNVENC();

    if (nvenc_info.available) {
        std::cout << "[NVENC] Available" << std::endl;
        std::cout << "  GPU           : " << nvenc_info.gpu_name << std::endl;
        std::cout << "  H.264         : " << (nvenc_info.h264_supported ? "Yes" : "No") << std::endl;
        std::cout << "  HEVC          : " << (nvenc_info.hevc_supported ? "Yes" : "No") << std::endl;
        std::cout << "  AV1           : " << (nvenc_info.av1_supported ? "Yes" : "No") << std::endl;
        std::cout << "  Max res       : " << nvenc_info.max_encode_width
            << "x" << nvenc_info.max_encode_height << std::endl;
    }
    else {
        std::cout << "[NVENC] Not available - " << nvenc_info.error_message << std::endl;
        std::cout << "[NVENC] Fallback: OpenH264 software encoding" << std::endl;
    }

    std::cout << "====================================\n" << std::endl;

    // 3. Initialise shared memory IPC channel
    fthr::SharedMemory memory;
    if (!memory.Initialize(L"FTHR_SharedMemory_v4")) {
        std::cerr << "[Fatal] Failed to create shared memory" << std::endl;
        std::cout << "Press Enter to exit...";
        std::cin.get();
        return 1;
    }

    auto* layout = memory.GetLayout();
    if (!layout) {
        std::cerr << "[Fatal] Failed to map shared memory layout" << std::endl;
        std::cout << "Press Enter to exit...";
        std::cin.get();
        return 1;
    }

    layout->is_initialized = false;  // set true AFTER engine init succeeds
    layout->is_recording = false;
    layout->frames_captured = 0;
    layout->bytes_written = 0;
    layout->ui_command = fthr::CommandType::NONE;
    layout->engine_response = fthr::ResponseType::NONE;
    layout->nvenc_active = false;
    layout->capture_health_flags = fthr::CAPTURE_HEALTH_NONE;
    layout->capture_generation = 0;
    layout->content_sample_sequence = 0;
    layout->content_suspicious_streak = 0;
    layout->content_luma_mean = 0.0f;
    layout->content_luma_variance = 0.0f;
    layout->cfg_bitrate_kbps = config.bitrate_kbps;
    layout->cfg_target_width = config.target_width;
    layout->cfg_target_height = config.target_height;
    layout->cfg_codec_pref = std::min<uint32_t>(codec_preference_arg, 3);
    layout->cfg_preset = config.encoder_preset;
    layout->multiband_enabled = false;

    std::cout << "Shared memory ready (is_initialized = false until engine starts)." << std::endl;

    // Initialize NVENC, AMF, or QSV on the capture adapter without automatic
    // cross-adapter or raw fallback. Set is_initialized only on success; the
    // Python bridge checks it before connecting.
    std::cout << "Initializing capture engine..." << std::endl;

    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        std::cerr << "[Fatal] Capture engine init failed: "
                  << fthr::ReplayStartupErrorName(
                         engine.GetLastStartupErrorCode())
                  << std::endl;
        layout->is_initialized = false;
        return 1;
    }

    // NOW the engine is fully running — signal Python it's safe to connect.
    layout->is_initialized = true;
    layout->nvenc_active = engine.IsNvencActive();
    layout->capture_health_flags = engine.GetCaptureHealthFlags();
    layout->capture_generation = engine.GetCaptureGeneration();

    // Populate v2 fields so Python get_active_codec() returns a meaningful string.
    {
        // Successful public-alpha startup always reports the exact active
        // hardware encoder. Failed startup never publishes is_initialized.
        const std::string codec_name = engine.GetActiveEncoderName();
        strncpy_s(layout->active_codec, sizeof(layout->active_codec),
                  codec_name.c_str(), _TRUNCATE);
    }

    // The retired v3 multiband field remains false for ABI compatibility.

    std::cout << "=== FTHR Capture Engine RUNNING ===" << std::endl;
    std::cout << "  is_initialized = true" << std::endl;
    std::cout << "  nvenc_active   = " << (layout->nvenc_active ? "true" : "false") << std::endl;
    std::cout << "  active_codec   = " << layout->active_codec << std::endl;
    const auto& capability = engine.GetActiveReplayCapability();
    std::cout << "  replay_policy  = "
              << (capability.same_adapter ? "same-adapter"
                                          : "explicit-cross-adapter")
              << std::endl;
    std::cout << "  capture_vendor = "
              << fthr::EncoderVendorName(capability.capture_vendor) << std::endl;
    std::cout << "  encoder_vendor = "
              << fthr::EncoderVendorName(capability.encoder_vendor) << std::endl;
    std::cout << "  encoder_backend= "
              << fthr::ReplayEncoderBackendName(capability.active_backend)
              << std::endl;
    std::cout << "  replay_capacity= " << config.buffer_seconds
              << "s configured compressed history" << std::endl;

    // 5. Command loop - polls shared memory for UI commands
    bool shutdown_requested = false;
    while (!shutdown_requested) {

        if (layout->ui_command != fthr::CommandType::NONE) {

            fthr::CommandType current_cmd = layout->ui_command;
            layout->ui_command = fthr::CommandType::NONE;

            wchar_t  cmd_string[256] = { 0 };
            wcsncpy_s(cmd_string, layout->ui_string, 255);
            uint32_t cmd_param1 = layout->ui_param1;

            switch (current_cmd) {

            case fthr::CommandType::START_RECORDING:
                std::wcout << L"[Cmd] START_RECORDING -> " << cmd_string << std::endl;
                if (engine.StartRecording(cmd_string)) {
                    layout->engine_response = fthr::ResponseType::RECORDING_STARTED;
                }
                else {
                    // Continuous-recording failures use their own response so
                    // the replay-save state machine cannot consume them.
                    std::wstring error = WideFromUtf8(
                        engine.GetLastRecordingError());
                    if (error.empty()) {
                        error = L"Could not start the recoverable recording.";
                    }
                    fthr::SetEngineString(layout, error.c_str());
                    layout->engine_response =
                        fthr::ResponseType::MANUAL_RECORDING_ERROR;
                }
                break;

            case fthr::CommandType::STOP_RECORDING:
                std::cout << "[Cmd] STOP_RECORDING" << std::endl;
                if (engine.StopRecording()) {
                    fthr::SetEngineString(layout, L"");
                    layout->engine_response =
                        fthr::ResponseType::RECORDING_STOPPED;
                } else {
                    std::wstring error = WideFromUtf8(
                        engine.GetLastRecordingError());
                    if (error.empty()) {
                        error = L"The recording closed with a recoverable error.";
                    }
                    fthr::SetEngineString(layout, error.c_str());
                    layout->engine_response =
                        fthr::ResponseType::MANUAL_RECORDING_ERROR;
                }
                break;

            case fthr::CommandType::SAVE_CLIP:
                std::wcout << L"[Cmd] SAVE_CLIP -> " << cmd_string
                    << L" (" << cmd_param1 << L"s)" << std::endl;
                // Publish acceptance before making work visible to the save
                // thread. A fast completion must never be overwritten by a
                // late SAVE_STARTED (or have its error payload cleared).
                fthr::SetEngineString(layout, L"");
                layout->engine_response = fthr::ResponseType::SAVE_STARTED;
                if (engine.SaveClip(cmd_string, cmd_param1, layout)) {
                    std::wcout << L"[Cmd] SAVE_CLIP queued" << std::endl;
                }
                else {
                    // SaveClip publishes a specific health/ring error. Preserve
                    // it instead of replacing every failure with a generic
                    // message, which hid the startup/focus-gate root cause.
                    if (layout->engine_response
                            != fthr::ResponseType::ERROR_OCCURRED) {
                        fthr::SetEngineError(layout,
                            L"The capture engine could not queue the save. The "
                            L"replay buffer may be empty or still starting up.");
                    }
                    std::cerr << "[Cmd] SAVE_CLIP failed to queue" << std::endl;
                }
                break;

            case fthr::CommandType::RECONFIGURE_ENCODER:
                std::cout << "[Cmd] RECONFIGURE_ENCODER (stub - restart required)" << std::endl;
                layout->engine_response = fthr::ResponseType::STATUS_UPDATE;
                break;

            case fthr::CommandType::SHUTDOWN:
                // Leaving this loop reaches CaptureEngine::Shutdown(), which
                // owns capture, audio and queued-save cleanup in one order.
                std::cout << "[Cmd] SHUTDOWN" << std::endl;
                shutdown_requested = true;
                break;

            default:
                std::cout << "[Cmd] Unknown command: " << static_cast<int>(current_cmd)
                    << std::endl;
                break;
            }
        }

        layout->is_recording = engine.IsRecording();
        layout->frames_captured = engine.GetFrameCount();
        layout->capture_health_flags = engine.GetCaptureHealthFlags();
        layout->capture_generation = engine.GetCaptureGeneration();
        layout->content_sample_sequence = engine.GetContentSampleSequence();
        layout->content_suspicious_streak = engine.GetContentSuspiciousStreak();
        layout->content_luma_mean = engine.GetContentLumaMean();
        layout->content_luma_variance = engine.GetContentLumaVariance();

        // 20ms poll interval halves the main-thread wakeup rate compared to the
        // old 10ms while keeping SAVE_CLIP latency well below human perception
        // (~10ms average, 20ms worst case from hotkey to engine acting on it).
        Sleep(20);
    }

    layout->is_initialized = false;
    engine.Shutdown();
    memory.Shutdown();

    return 0;
}
