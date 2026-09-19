#include "capture_engine.h"
#include "shared_memory.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <unistd.h>

// Signal handling for clean shutdown

static volatile bool g_quit = false;
static void on_signal(int) { g_quit = true; }

// Argv parsing helpers

static uint32_t arg_u32(char** argv, int idx, uint32_t def) {
    if (!argv[idx] || argv[idx][0] == '\0') return def;
    long v = strtol(argv[idx], nullptr, 10);
    return (v < 0) ? def : static_cast<uint32_t>(v);
}


int main(int argc, char* argv[]) {
    // Keep positional arguments aligned with the UI and Windows engine.
    // See docs/engine-startup.md for platform differences and reserved slots.

    fthr::CaptureConfig cfg{};
    cfg.fps            = (argc > 1) ? arg_u32(argv, 1, 60)     : 60;
    cfg.buffer_seconds = (argc > 2) ? arg_u32(argv, 2, 30)     : 30;
    cfg.target_width   = (argc > 3) ? arg_u32(argv, 3, 0)      : 0;
    cfg.target_height  = (argc > 4) ? arg_u32(argv, 4, 0)      : 0;
    cfg.bitrate_kbps   = (argc > 5) ? arg_u32(argv, 5, 16000)  : 16000;
    // argv[6] = max_buffer_mb — ignored
    // argv[7] = capture_mode  — ignored (always desktop)
    // argv[8] = target_hwnd   — ignored
    cfg.scaling_mode   = (argc > 9) ? arg_u32(argv, 9, 0)      : 0;
    cfg.target_output  = (argc > 10 && argv[10] && argv[10][0]) ? argv[10] : "";
    cfg.codec_pref = static_cast<fthr::CodecPref>(
        (argc > 11) ? arg_u32(argv, 11, 0) : 0);
    cfg.preset     = (argc > 12) ? static_cast<int>(arg_u32(argv, 12, 4)) : 4;
    if (cfg.preset < 1) cfg.preset = 1;
    if (cfg.preset > 7) cfg.preset = 7;
    cfg.multiband_enabled = false;
    cfg.separate_audio_enabled = (argc > 23) && (arg_u32(argv, 23, 0) != 0);
    cfg.audio_enabled = !((argc > 14) && (arg_u32(argv, 14, 1) == 0));
    cfg.encoder_pref = static_cast<fthr::EncoderPref>(
        (argc > 17) ? std::min<uint32_t>(arg_u32(argv, 17, 0), 4) : 0);

    if (cfg.fps            < 1)     cfg.fps            = 1;
    if (cfg.fps            > 360)   cfg.fps            = 360;
    if (cfg.buffer_seconds < 1)     cfg.buffer_seconds = 1;
    if (cfg.buffer_seconds > 300)   cfg.buffer_seconds = 300;
    if (cfg.bitrate_kbps   < 500)   cfg.bitrate_kbps   = 500;
    if (cfg.bitrate_kbps   > 60000) cfg.bitrate_kbps   = 60000;

    std::cout << "[FTHR] Linux capture engine starting" << std::endl;
    std::cout << "[FTHR] fps=" << cfg.fps
              << " buffer=" << cfg.buffer_seconds << "s"
              << " enc=" << cfg.target_width << "x" << cfg.target_height
              << " bitrate=" << cfg.bitrate_kbps << "kbps"
              << " scaling=" << cfg.scaling_mode << std::endl;

    // Shared memory
    fthr::SharedMemory shm;
    if (!shm.Initialize("FTHR_SharedMemory_v4")) {
        std::cerr << "[FTHR] Shared memory init failed — exiting" << std::endl;
        return 1;
    }
    fthr::SharedMemoryLayout* layout = shm.GetLayout();

    // Capture engine
    fthr::CaptureEngine engine;
    if (!engine.Initialize(cfg)) {
        std::cerr << "[FTHR] CaptureEngine init failed — exiting" << std::endl;
        return 1;
    }

    // Signal the UI that we are ready
    layout->is_initialized  = true;
    layout->is_recording     = false;
    layout->cfg_bitrate_kbps = cfg.bitrate_kbps;
    layout->cfg_target_width = cfg.target_width;
    layout->cfg_target_height= cfg.target_height;
    layout->nvenc_active     = engine.IsNvencActive();
    memset(layout->active_codec, 0, sizeof(layout->active_codec));
    layout->multiband_enabled = false;
    memset(layout->active_audio_mappings, 0, sizeof(layout->active_audio_mappings));
    layout->cfg_codec_pref = static_cast<uint32_t>(cfg.codec_pref);
    layout->cfg_preset     = static_cast<uint32_t>(cfg.preset);
    layout->capture_health_flags = engine.GetCaptureHealthFlags();
    layout->capture_generation = engine.GetCaptureGeneration();
    layout->content_sample_sequence = 0;
    layout->content_suspicious_streak = 0;
    layout->content_luma_mean = 0.0f;
    layout->content_luma_variance = 0.0f;

    std::cout << "[FTHR] Ready. Waiting for commands..." << std::endl;

    // Signal handlers for clean exit
    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);

    // After a terminal capture failure the process lingers briefly so the
    // UI's 500 ms status poll can observe BACKEND_FAILED and its reason
    // before the exit path reports a stopped engine instead.
    constexpr auto kFailurePublishGrace = std::chrono::milliseconds(2000);
    std::chrono::steady_clock::time_point capture_stopped_at{};
    bool failure_reason_published = false;

    // Command poll loop — same 20ms cadence as Windows version
    while (!g_quit) {
        auto cmd = static_cast<fthr::CommandType>(layout->ui_command);

        if (cmd != fthr::CommandType::NONE) {
            layout->ui_command = static_cast<uint32_t>(fthr::CommandType::NONE);

            switch (cmd) {
            case fthr::CommandType::SAVE_CLIP: {
                // Clear the previous save detail before queueing this request so a later
                // success cannot carry stale error text.
                fthr::set_engine_string(layout, "");

                // Acknowledge queueing immediately. SAVE_STARTED has no payload, and
                // engine_string was cleared before submission.
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::SAVE_STARTED);

                std::string out_path(layout->ui_string);
                uint32_t duration_sec = layout->ui_param1;
                if (duration_sec == 0) duration_sec = cfg.buffer_seconds;

                std::cout << "[FTHR] SAVE_CLIP -> " << out_path
                          << " (" << duration_sec << "s)" << std::endl;

                std::string save_error;
                bool ok = engine.SaveClip(
                    out_path, duration_sec, layout, &save_error);

                // Write the error detail before publishing ERROR_OCCURRED.
                if (!ok)
                    fthr::set_engine_string(
                        layout, save_error.empty()
                            ? "SaveClip failed: " + out_path
                            : save_error);
                layout->engine_response = ok
                    ? static_cast<uint32_t>(fthr::ResponseType::CLIP_SAVED)
                    : static_cast<uint32_t>(fthr::ResponseType::ERROR_OCCURRED);
                break;
            }

            case fthr::CommandType::GET_STATUS:
                // Write engine_param1 before publishing its response.
                layout->engine_param1 = engine.IsNvencActive() ? 1 : 0;
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;

            case fthr::CommandType::RECONFIGURE_ENCODER: {
                uint32_t new_codec_pref = layout->cfg_codec_pref;
                int      new_preset     = static_cast<int>(layout->cfg_preset);
                if (new_preset < 1) new_preset = 1;
                if (new_preset > 7) new_preset = 7;

                std::cout << "[FTHR] RECONFIGURE_ENCODER  codec_pref="
                          << new_codec_pref << "  preset=" << new_preset << std::endl;

                engine.Reconfigure(new_codec_pref, new_preset);

                memset(layout->active_codec, 0, sizeof(layout->active_codec));
                layout->nvenc_active    = engine.IsNvencActive();
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;
            }

            case fthr::CommandType::SET_RESOLUTION:
                layout->cfg_target_width  = layout->ui_param1;
                layout->cfg_target_height = layout->ui_param2;
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;

            case fthr::CommandType::SET_QUALITY:
                layout->cfg_bitrate_kbps = layout->ui_param1;
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;

            case fthr::CommandType::STOP_RECORDING:
                engine.SetPaused(true);
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STOPPED);
                break;

            case fthr::CommandType::START_RECORDING:
                engine.SetPaused(false);
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STARTED);
                break;

            case fthr::CommandType::SHUTDOWN:
                // Normal X11 shutdown first uses AVIOInterruptCB through
                // CaptureEngine::Shutdown(). If an XCB reply ignores that
                // callback, the UI's existing fixed process deadline still
                // terminates, kills and reaps this isolated engine process.
                std::cout << "[FTHR] SHUTDOWN requested" << std::endl;
                g_quit = 1;
                break;

            default:
                break;
            }
        }

        layout->frames_captured = engine.GetFrameCount();
        layout->nvenc_active    = engine.IsNvencActive();
        uint32_t health_flags = engine.GetCaptureHealthFlags();
        if ((health_flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED) &&
                !failure_reason_published) {
            // Payload first: the UI reads engine_string as soon as it sees
            // the BACKEND_FAILED flag. engine_string is shared with save and
            // recording results, so wait until the UI has consumed any
            // pending response and hold the flag back until then; no
            // engine_response is published for the capture failure itself.
            if (layout->engine_response ==
                    static_cast<uint32_t>(fthr::ResponseType::NONE)) {
                fthr::set_engine_string(layout, engine.GetCaptureFailureReason());
                failure_reason_published = true;
            } else {
                health_flags &= ~fthr::CAPTURE_HEALTH_BACKEND_FAILED;
            }
        } else if (!(health_flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED)) {
            // A RECONFIGURE_ENCODER restart may fail again later.
            failure_reason_published = false;
        }
        layout->capture_health_flags = health_flags;
        layout->capture_generation = engine.GetCaptureGeneration();
        layout->content_sample_sequence = engine.GetContentSampleSequence();
        layout->content_suspicious_streak = engine.GetContentSuspiciousStreak();
        layout->content_luma_mean = engine.GetContentLumaMean();
        layout->content_luma_variance = engine.GetContentLumaVariance();
        if (!engine.IsCapturing()) {
            const auto now = std::chrono::steady_clock::now();
            if (capture_stopped_at == std::chrono::steady_clock::time_point{})
                capture_stopped_at = now;
            if (now - capture_stopped_at >= kFailurePublishGrace) {
                std::cerr << "[FTHR] Capture backend stopped; exiting engine"
                          << std::endl;
                break;
            }
        } else {
            capture_stopped_at = {};
        }
        // Keep active_codec in shared memory up to date
        const std::string& ac = engine.GetActiveCodec();
        if (!ac.empty()) {
            strncpy(layout->active_codec, ac.c_str(),
                    sizeof(layout->active_codec) - 1);
            layout->active_codec[sizeof(layout->active_codec) - 1] = '\0';
        }
        // Linux captures continuously — no discrete recording state
        layout->is_recording    = false;

        usleep(20000);  // 20ms poll, identical to Windows
    }

    std::cout << "[FTHR] Shutting down..." << std::endl;
    layout->is_initialized = false;
    engine.Shutdown();

    return 0;
}
