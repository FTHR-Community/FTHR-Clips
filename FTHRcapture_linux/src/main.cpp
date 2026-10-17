#include "capture_engine.h"
#include "shared_memory.h"
#include "audio_devices.h"
#include "engine_args.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <sys/prctl.h>
#include <unistd.h>

// Signal handling for clean shutdown

static volatile bool g_quit = false;
static void on_signal(int) { g_quit = true; }

static int ListPlaybackDevices() {
    std::vector<fthr::AudioEndpoint> sinks;
    std::string error;
    if (!fthr::EnumeratePlaybackSinks(&sinks, &error)) {
        std::cerr << fthr::FormatEndpointListError(error) << std::endl;
        return 1;
    }
    std::cout << fthr::FormatEndpointListJson(sinks) << std::endl;
    return 0;
}

static int ListMicrophones() {
    // The Linux engine does not capture a microphone; the UI records it
    // itself. An empty inventory is the honest answer.
    std::cout << fthr::FormatEndpointListJson({}) << std::endl;
    return 0;
}


int main(int argc, char* argv[]) {
    // `--` modes never touch shared memory or start capture.
    const fthr::CliDecision cli = fthr::ClassifyCommandLine(argc, argv);
    switch (cli.mode) {
    case fthr::CliMode::ListPlaybackDevices: return ListPlaybackDevices();
    case fthr::CliMode::ListMicrophones:     return ListMicrophones();
    case fthr::CliMode::UsageError:
        std::cerr << "FTHRclips: " << cli.message << "\n"
                  << "usage: FTHRclips [positional startup arguments]"
                     " (see docs/engine-startup.md)\n"
                  << "       FTHRclips --list-playback-devices\n"
                  << "       FTHRclips --list-microphones" << std::endl;
        return 2;
    case fthr::CliMode::Engine:
        break;
    }

    // Keep positional arguments aligned with the UI and Windows engine.
    // See docs/engine-startup.md for platform differences and reserved slots.
    const fthr::EngineArgs args = fthr::ParseEngineArgs(argc, argv);
    for (const auto& warning : args.warnings)
        std::cerr << "[Config] " << warning << std::endl;

    fthr::CaptureConfig cfg{};
    cfg.fps            = args.fps;
    cfg.buffer_seconds = args.buffer_seconds;
    cfg.target_width   = args.target_width;
    cfg.target_height  = args.target_height;
    cfg.bitrate_kbps   = args.bitrate_kbps;
    cfg.scaling_mode   = args.scaling_mode;
    cfg.target_output  = args.target_output;
    cfg.codec_pref     = args.codec_pref;
    cfg.preset         = args.preset;
    cfg.multiband_enabled = false;
    cfg.separate_audio_enabled = args.separate_audio_enabled;
    cfg.audio_enabled  = args.audio_enabled;
    cfg.encoder_pref   = args.encoder_pref;
    cfg.playback_sink  = args.playback_endpoint_id;
    cfg.max_buffer_bytes =
        static_cast<uint64_t>(args.max_buffer_mb) * 1024u * 1024u;

    std::cout << "[FTHR] Linux capture engine starting" << std::endl;
    std::cout << "[FTHR] fps=" << cfg.fps
              << " buffer=" << cfg.buffer_seconds << "s"
              << " enc=" << cfg.target_width << "x" << cfg.target_height
              << " bitrate=" << cfg.bitrate_kbps << "kbps"
              << " scaling=" << cfg.scaling_mode
              << " encoder_pref=" << static_cast<uint32_t>(cfg.encoder_pref)
              << " playback="
              << (cfg.playback_sink.empty() ? "<default>" : cfg.playback_sink)
              << std::endl;
    if (args.microphone_mono)
        std::cout << "[FTHR] microphone_mono ignored: the Linux engine does not"
                     " capture a microphone" << std::endl;

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

    // The UI normally sends SHUTDOWN and reaps the process. If the UI dies
    // without doing so (SIGKILL, a crash, a terminal closing), the engine
    // must not outlive it: an orphan keeps capturing, keeps the shared-memory
    // name, and the next UI connects to the wrong engine. Ask the kernel for
    // SIGTERM when the parent exits; if it already has, leave now.
    const pid_t launcher = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) == 0 && getppid() != launcher)
        g_quit = true;

    // After a terminal capture failure the process lingers briefly so the
    // UI's 500 ms status poll can observe BACKEND_FAILED and its reason
    // before the exit path reports a stopped engine instead.
    constexpr auto kFailurePublishGrace = std::chrono::milliseconds(2000);
    std::chrono::steady_clock::time_point capture_stopped_at{};
    bool failure_reason_published = false;
    std::string published_recovery_reason;

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

            default:
                // Kept ahead of the recording cases rather than last: the
                // response-order checker pairs each STATUS_UPDATE with the
                // next publish and would otherwise match this one against the
                // failure-reason write that follows the switch.
                // Retired or Windows-only commands (SET_FRAMERATE, SET_HOTKEY,
                // SET_TARGET_WINDOW). Acknowledge so a caller never waits.
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
            }
        }

        layout->frames_captured = engine.GetFrameCount();
        layout->nvenc_active    = engine.IsNvencActive();
        uint32_t health_flags = engine.GetCaptureHealthFlags();
        // engine_string is shared with save results, so nothing below may
        // overwrite it while the UI has yet to consume a save response.
        // Pause/resume and status acknowledgements carry no payload and have
        // no consumer on Linux, so they never hold a publication back.
        const auto pending = static_cast<fthr::ResponseType>(
            layout->engine_response);
        const bool save_result_pending =
            pending == fthr::ResponseType::SAVE_STARTED ||
            pending == fthr::ResponseType::CLIP_SAVED ||
            pending == fthr::ResponseType::ERROR_OCCURRED;
        if ((health_flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED) &&
                !failure_reason_published) {
            // Payload first: the UI reads engine_string as soon as it sees
            // the BACKEND_FAILED flag, so hold the flag back until the
            // string can be written; no engine_response is published for
            // the capture failure itself.
            if (!save_result_pending) {
                fthr::set_engine_string(layout, engine.GetCaptureFailureReason());
                failure_reason_published = true;
            } else {
                health_flags &= ~fthr::CAPTURE_HEALTH_BACKEND_FAILED;
            }
        } else if (!(health_flags & fthr::CAPTURE_HEALTH_BACKEND_FAILED)) {
            // A RECONFIGURE_ENCODER restart may fail again later.
            failure_reason_published = false;
            // While recovering, say what is being waited for; clear it again
            // once capture is back so a later failure never shows stale text.
            const std::string recovery_reason =
                (health_flags & fthr::CAPTURE_HEALTH_RECOVERING)
                    ? engine.GetCaptureFailureReason() : std::string();
            if (recovery_reason != published_recovery_reason && !save_result_pending) {
                fthr::set_engine_string(layout, recovery_reason);
                published_recovery_reason = recovery_reason;
            }
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
