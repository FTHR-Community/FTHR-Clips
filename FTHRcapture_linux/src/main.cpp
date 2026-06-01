#include "capture_engine.h"
#include "shared_memory.h"
#include <iostream>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Signal handling for clean shutdown
// ---------------------------------------------------------------------------

static volatile bool g_quit = false;
static void on_signal(int) { g_quit = true; }

// ---------------------------------------------------------------------------
// Argv parsing helpers
// ---------------------------------------------------------------------------

static uint32_t arg_u32(char** argv, int idx, uint32_t def) {
    if (!argv[idx] || argv[idx][0] == '\0') return def;
    long v = strtol(argv[idx], nullptr, 10);
    return (v < 0) ? def : static_cast<uint32_t>(v);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main(int argc, char* argv[]) {
    // Argv contract (identical to Windows version):
    //   [1] fps            (1-360,   default 60)
    //   [2] buffer_sec     (1-300,   default 30)
    //   [3] target_width   (0=native)
    //   [4] target_height  (0=native)
    //   [5] bitrate_kbps   (500-60000, default 16000)
    //   [6] max_buffer_mb  (ignored on Linux)
    //   [7] capture_mode   (0=desktop, 1=window — Linux only supports 0)
    //   [8] target_hwnd    (ignored on Linux)
    //   [9] scaling_mode   (0=stretch, 1=fit)
    //   [10] target_output  (wl_output name, e.g. "DP-3" — empty = first output)
    //   [14] audio_enabled (1=on default, 0=off)

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
    cfg.multiband_enabled = (argc > 13) && (arg_u32(argv, 13, 0) == 1);
    cfg.audio_enabled = !((argc > 14) && (arg_u32(argv, 14, 1) == 0));

    // Clamp
    if (cfg.fps            < 1)     cfg.fps            = 1;
    if (cfg.fps            > 360)   cfg.fps            = 360;
    if (cfg.buffer_seconds < 1)     cfg.buffer_seconds = 1;
    if (cfg.buffer_seconds > 300)   cfg.buffer_seconds = 300;
    if (cfg.bitrate_kbps   < 500)   cfg.bitrate_kbps   = 500;
    if (cfg.bitrate_kbps   > 60000) cfg.bitrate_kbps   = 60000;

    // Load audio category config written by Python before engine start.
    // File format: one JSON object per line: {"name":"X","sink":"Y","patterns":["a","b"]}
    if (cfg.multiband_enabled) {
        const char* home = getenv("HOME");
        std::string cat_path = (home ? std::string(home) : "") + "/.fthr/audio_categories.json";
        FILE* fp = fopen(cat_path.c_str(), "r");
        if (fp) {
            char line[2048];
            while (fgets(line, sizeof(line), fp)) {
                fthr::AudioCategoryConfig c;

                // Extract quoted string value for a given key
                auto extract = [](const char* s, const char* key) -> std::string {
                    std::string k = std::string("\"") + key + "\":\"";
                    const char* p = strstr(s, k.c_str());
                    if (!p) return {};
                    p += k.size();
                    const char* e = strchr(p, '"');
                    return e ? std::string(p, e) : std::string{};
                };

                c.name      = extract(line, "name");
                c.sink_name = extract(line, "sink");
                if (c.name.empty()) continue;

                // Extract patterns array
                const char* pat_start = strstr(line, "\"patterns\":");
                if (pat_start) {
                    const char* arr     = strchr(pat_start, '[');
                    const char* arr_end = arr ? strchr(arr, ']') : nullptr;
                    if (arr && arr_end) {
                        const char* p = arr;
                        while (p < arr_end) {
                            p = strchr(p, '"');
                            if (!p || p >= arr_end) break;
                            ++p;
                            const char* e = strchr(p, '"');
                            if (!e || e >= arr_end) break;
                            c.patterns.push_back(std::string(p, e));
                            p = e + 1;
                        }
                    }
                }
                cfg.audio_categories.push_back(std::move(c));
            }
            fclose(fp);
            std::cout << "[FTHR] Loaded " << cfg.audio_categories.size()
                      << " audio categories\n";
        } else {
            std::cerr << "[FTHR] multiband enabled but " << cat_path
                      << " not found — disabling multiband\n";
            cfg.multiband_enabled = false;
        }
    }

    std::cout << "[FTHR] Linux capture engine starting" << std::endl;
    std::cout << "[FTHR] fps=" << cfg.fps
              << " buffer=" << cfg.buffer_seconds << "s"
              << " enc=" << cfg.target_width << "x" << cfg.target_height
              << " bitrate=" << cfg.bitrate_kbps << "kbps"
              << " scaling=" << cfg.scaling_mode << std::endl;

    // Shared memory
    fthr::SharedMemory shm;
    if (!shm.Initialize("FTHR_SharedMemory_v3")) {
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
    layout->multiband_enabled = cfg.multiband_enabled;
    memset(layout->active_audio_mappings, 0, sizeof(layout->active_audio_mappings));
    layout->cfg_codec_pref = static_cast<uint32_t>(cfg.codec_pref);
    layout->cfg_preset     = static_cast<uint32_t>(cfg.preset);

    std::cout << "[FTHR] Ready. Waiting for commands..." << std::endl;

    // Signal handlers for clean exit
    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);

    // Command poll loop — same 20ms cadence as Windows version
    while (!g_quit) {
        auto cmd = static_cast<fthr::CommandType>(layout->ui_command);

        if (cmd != fthr::CommandType::NONE) {
            layout->ui_command = static_cast<uint32_t>(fthr::CommandType::NONE);

            switch (cmd) {
            case fthr::CommandType::SAVE_CLIP: {
                // Immediately acknowledge so Python UI doesn't time out
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::SAVE_STARTED);

                std::string out_path(layout->ui_string);
                uint32_t duration_sec = layout->ui_param1;
                if (duration_sec == 0) duration_sec = cfg.buffer_seconds;

                std::cout << "[FTHR] SAVE_CLIP -> " << out_path
                          << " (" << duration_sec << "s)" << std::endl;

                bool ok = engine.SaveClip(out_path, duration_sec, layout);
                layout->engine_response = ok
                    ? static_cast<uint32_t>(fthr::ResponseType::CLIP_SAVED)
                    : static_cast<uint32_t>(fthr::ResponseType::ERROR_OCCURRED);
                if (!ok)
                    snprintf(layout->engine_string,
                             sizeof(layout->engine_string),
                             "SaveClip failed: %s", out_path.c_str());
                break;
            }

            case fthr::CommandType::GET_STATUS:
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                layout->engine_param1 = engine.IsNvencActive() ? 1 : 0;
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
                // Linux engine captures continuously; STOP is a no-op
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STOPPED);
                break;

            case fthr::CommandType::START_RECORDING:
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STARTED);
                break;

            default:
                break;
            }
        }

        // Update live status in shared memory
        layout->frames_captured = engine.GetFrameCount();
        layout->nvenc_active    = engine.IsNvencActive();
        // Keep active_codec in shared memory up to date
        const std::string& ac = engine.GetActiveCodec();
        if (!ac.empty()) {
            strncpy(layout->active_codec, ac.c_str(),
                    sizeof(layout->active_codec) - 1);
            layout->active_codec[sizeof(layout->active_codec) - 1] = '\0';
        }
        if (cfg.multiband_enabled) {
            std::string json = engine.GetAudioMappingsJson();
            if (json.size() < sizeof(layout->active_audio_mappings)) {
                strncpy(layout->active_audio_mappings, json.c_str(),
                        sizeof(layout->active_audio_mappings) - 1);
                layout->active_audio_mappings[sizeof(layout->active_audio_mappings) - 1] = '\0';
            }
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
