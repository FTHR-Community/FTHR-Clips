// main.cpp
// FTHR Capture Engine - Entry point and command loop
//
// Encoded ring buffer update:
//   NVENC is now initialized inside CaptureEngine::Initialize().
//   Standalone detection here is informational only (GPU name / caps).
//   The PART A TEST block has been removed (old signature, superseded).
//
// Responsibilities:
//   - Parse startup configuration from command-line arguments
//   - Initialize shared memory IPC channel
//   - Initialize and own the CaptureEngine instance
//   - Run the command loop (poll shared memory, dispatch to engine)
//
// argv contract (all provided by Python main.py start_engine()):
//   argv[1]  fps            Capture framerate (1-360). Default: 60
//   argv[2]  buffer_sec     Ring buffer duration in seconds (1-300). Default: 30
//   argv[3]  target_width   Output width in pixels. 0 = native resolution. Default: 0
//   argv[4]  target_height  Output height in pixels. 0 = native resolution. Default: 0
//   argv[5]  bitrate_kbps   Encoder bitrate in kbps (500-60000). Default: 16000
//   argv[6]  max_buffer_mb  Memory ceiling for raw frame pool (x264 fallback only).
//            Not used when NVENC is active - encoded ring buffer has no raw frame budget.
//   argv[7]  capture_mode   0 = desktop (default), 1 = window
//   argv[8]  target_hwnd    64-bit decimal HWND when capture_mode == 1, else 0
//   argv[9]  scaling_mode   0 = stretch (default), 1 = fit (letterbox/pillarbox).
//            Only meaningful when target_width/height are non-zero AND differ in
//            aspect ratio from the captured source.
//
// Threading:
//   This file runs entirely on the main thread.
//   Heavy work (capture, encode, disk I/O) lives in CaptureEngine's threads.

#include "shared_memory.h"
#include "capture_engine.h"
#include "hardware_encoder.h"  // DetectNVENC() for pre-init logging
#include <iostream>
#include <Windows.h>


// ---------------------------------------------------------------------------
// Argument parsing helpers
// ---------------------------------------------------------------------------

static uint32_t ParseArgU32(int argc, char* argv[], int index, uint32_t default_val) {
    if (index >= argc) return default_val;
    int val = std::atoi(argv[index]);
    if (val < 0) return default_val;
    return static_cast<uint32_t>(val);
}

// Parse a 64-bit unsigned integer from argv (used for HWND on 64-bit Windows).
static uintptr_t ParseArgU64(int argc, char* argv[], int index, uintptr_t default_val) {
    if (index >= argc) return default_val;
    return static_cast<uintptr_t>(std::strtoull(argv[index], nullptr, 10));
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
    std::cout << "  Max pool     : " << cfg.max_buffer_mb << " MB (x264 fallback only)" << std::endl;

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
}


// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

int main(int argc, char* argv[]) {
    std::cout << "FTHR Capture Engine starting..." << std::endl;

    // ------------------------------------------------------------------
    // 1. Parse command-line arguments into CaptureConfig
    // ------------------------------------------------------------------
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

    // ------------------------------------------------------------------
    // 2. Validate all parameters - clamp to safe ranges
    // ------------------------------------------------------------------
    if (config.framerate == 0 || config.framerate > 360) {
        std::cerr << "[Config] Invalid framerate " << config.framerate
            << " - clamping to 60" << std::endl;
        config.framerate = 60;
    }

    if (config.buffer_seconds == 0 || config.buffer_seconds > 300) {
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

    // ------------------------------------------------------------------
    // 2.5. NVENC Detection (informational)
    //
    // Logs GPU name and capabilities before engine init.
    // CaptureEngine::Initialize() will attempt NVENC independently and
    // fall back to x264 if it fails - this block does not affect that.
    // ------------------------------------------------------------------
    std::cout << "\n=== Hardware Encoding Detection ===" << std::endl;

    fthr::NVENCDetectionResult nvenc_info = fthr::DetectNVENC();

    if (nvenc_info.available) {
        std::cout << "[NVENC] Available" << std::endl;
        std::cout << "  GPU           : " << nvenc_info.gpu_name << std::endl;
        std::cout << "  H.264         : " << (nvenc_info.h264_supported ? "Yes" : "No") << std::endl;
        std::cout << "  HEVC          : " << (nvenc_info.hevc_supported ? "Yes" : "No") << std::endl;
        std::cout << "  Max res       : " << nvenc_info.max_encode_width
            << "x" << nvenc_info.max_encode_height << std::endl;
    }
    else {
        std::cout << "[NVENC] Not available - " << nvenc_info.error_message << std::endl;
        std::cout << "[NVENC] Fallback: x264 software encoding" << std::endl;
    }

    std::cout << "====================================\n" << std::endl;

    // ------------------------------------------------------------------
    // 3. Initialise shared memory IPC channel
    // ------------------------------------------------------------------
    fthr::SharedMemory memory;
    if (!memory.Initialize(L"FTHR_SharedMemory_v1")) {
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

    std::cout << "Shared memory ready (is_initialized = false until engine starts)." << std::endl;

    // ------------------------------------------------------------------
    // 4. Initialise the capture engine
    //
    // CaptureEngine::Initialize() will attempt NVENC first.
    // On success: encoded ring buffer active, FramePool skipped.
    // On failure: x264 fallback with raw BGRA FramePool.
    //
    // is_initialized is set to true ONLY after this succeeds. Python's
    // CaptureBridge.initialize() checks this flag before connecting.
    // ------------------------------------------------------------------
    std::cout << "Initializing capture engine..." << std::endl;

    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        std::cerr << "[Fatal] Capture engine init failed (DXGI unavailable?)" << std::endl;
        layout->is_initialized = false;
        std::cout << "Press Enter to exit...";
        std::cin.get();
        return 1;
    }

    // NOW the engine is fully running — signal Python it's safe to connect.
    layout->is_initialized = true;
    layout->nvenc_active = engine.IsNvencActive();
    std::cout << "=== FTHR Capture Engine RUNNING ===" << std::endl;
    std::cout << "  is_initialized = true" << std::endl;
    std::cout << "  nvenc_active   = " << (layout->nvenc_active ? "true" : "false") << std::endl;

    // ------------------------------------------------------------------
    // 5. Command loop - polls shared memory for UI commands
    // ------------------------------------------------------------------
    while (true) {

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
                    layout->engine_response = fthr::ResponseType::ERROR_OCCURRED;
                }
                break;

            case fthr::CommandType::STOP_RECORDING:
                std::cout << "[Cmd] STOP_RECORDING" << std::endl;
                engine.StopRecording();
                layout->engine_response = fthr::ResponseType::RECORDING_STOPPED;
                break;

            case fthr::CommandType::SAVE_CLIP:
                std::wcout << L"[Cmd] SAVE_CLIP -> " << cmd_string
                    << L" (" << cmd_param1 << L"s)" << std::endl;
                if (engine.SaveClip(cmd_string, cmd_param1, layout)) {
                    layout->engine_response = fthr::ResponseType::SAVE_STARTED;
                    std::wcout << L"[Cmd] SAVE_CLIP queued" << std::endl;
                }
                else {
                    layout->engine_response = fthr::ResponseType::ERROR_OCCURRED;
                    std::cerr << "[Cmd] SAVE_CLIP failed to queue" << std::endl;
                }
                break;

            case fthr::CommandType::RECONFIGURE_ENCODER:
                std::cout << "[Cmd] RECONFIGURE_ENCODER (stub - restart required)" << std::endl;
                layout->engine_response = fthr::ResponseType::STATUS_UPDATE;
                break;

            default:
                std::cout << "[Cmd] Unknown command: " << static_cast<int>(current_cmd)
                    << std::endl;
                break;
            }
        }

        layout->is_recording = engine.IsRecording();
        layout->frames_captured = engine.GetFrameCount();

        // 20ms poll interval halves the main-thread wakeup rate compared to the
        // old 10ms while keeping SAVE_CLIP latency well below human perception
        // (~10ms average, 20ms worst case from hotkey to engine acting on it).
        Sleep(20);
    }

    // Unreachable - Python calls TerminateProcess() on shutdown.
    engine.Shutdown();
    memory.Shutdown();

    return 0;
}