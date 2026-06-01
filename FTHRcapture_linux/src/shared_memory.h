#pragma once
#include <cstdint>
#include <string>

namespace fthr {

// Must match Python's SharedMemoryLayout in capture_bridge.py (Linux branch)
struct SharedMemoryLayout {
    uint32_t ui_command;
    uint32_t ui_param1;
    uint32_t ui_param2;
    uint32_t ui_param3;
    char     ui_string[1024];
    uint32_t engine_response;
    uint32_t engine_param1;
    uint32_t engine_param2;
    float    engine_param3;
    char     engine_string[2048];
    bool     is_recording;
    bool     is_initialized;
    uint64_t frames_captured;
    uint64_t bytes_written;
    uint32_t cfg_bitrate_kbps;
    uint32_t cfg_target_width;
    uint32_t cfg_target_height;
    bool     nvenc_active;

    // Encoder config — written by UI before RECONFIGURE_ENCODER command.
    // active_codec is written by the engine after Open() succeeds.
    uint32_t cfg_codec_pref;     // 0=auto 1=h264 2=hevc 3=av1
    uint32_t cfg_preset;         // 1–7
    char     active_codec[64];   // e.g. "hevc_nvenc\0"
};

enum class CommandType : uint32_t {
    NONE=0, START_RECORDING=1, STOP_RECORDING=2, SAVE_CLIP=3,
    SET_RESOLUTION=4, SET_QUALITY=5, SET_FRAMERATE=6, SET_HOTKEY=7,
    SET_TARGET_WINDOW=8, GET_STATUS=9, RECONFIGURE_ENCODER=10
};

enum class ResponseType : uint32_t {
    NONE=0, RECORDING_STARTED=1, RECORDING_STOPPED=2, CLIP_SAVED=3,
    STATUS_UPDATE=4, ERROR_OCCURRED=5, SAVE_STARTED=6
};

class SharedMemory {
public:
    SharedMemory() = default;
    ~SharedMemory() { Shutdown(); }

    bool Initialize(const std::string& name);
    void Shutdown();
    SharedMemoryLayout* GetLayout() { return layout_; }

private:
    int   shm_fd_  = -1;
    void* mapping_ = nullptr;
    std::string name_;
    SharedMemoryLayout* layout_ = nullptr;
};

} // namespace fthr
