// Shared-memory layout, command codes, and native mapping ownership.
// Keep the layout and enums aligned with core/capture_bridge.py and the
// Linux header. Commands and responses each have one producer; publish
// payload fields before their command or response code.

#pragma once
#ifndef FTHR_SHARED_MEMORY_H
#define FTHR_SHARED_MEMORY_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <cstdint>


namespace fthr {


    // Python publishes these command IDs; the native command loop consumes them.
    // Keep retired SET_* values reserved for ABI compatibility. Configuration
    // is passed at startup rather than through those retired commands.
    enum class CommandType : uint32_t {
        NONE = 0,
        START_RECORDING = 1,
        STOP_RECORDING = 2,
        SAVE_CLIP = 3,

        // Deprecated: settings now passed via command-line args at startup.
        // Kept for ABI compatibility - command loop ignores them gracefully.
        SET_RESOLUTION = 4,
        SET_QUALITY = 5,
        SET_FRAMERATE = 6,
        SET_HOTKEY = 7,
        SET_TARGET_WINDOW = 8,

        GET_STATUS = 9,

        // Reserved runtime reconfiguration command; currently stubbed.
        // Configuration changes requiring a new generation use startup arguments.
        RECONFIGURE_ENCODER = 10,

        // Requests a clean process exit without changing the v4 layout.
        SHUTDOWN = 11,
    };


    // Native responses consumed by Python. SAVE_STARTED acknowledges queueing;
    // CLIP_SAVED confirms the save worker has completed the file.
    enum class ResponseType : uint32_t {
        NONE = 0,
        RECORDING_STARTED = 1,
        RECORDING_STOPPED = 2,
        CLIP_SAVED = 3,
        STATUS_UPDATE = 4,
        ERROR_OCCURRED = 5,
        SAVE_STARTED = 6,   // Save queued; completion arrives separately
        MANUAL_RECORDING_ERROR = 7,
    };

    // v4 capture-health status bits.  These are continuous status, not command
    // responses: publishing them must never consume or overwrite the save
    // response slot owned by the UI.
    enum CaptureHealthFlags : uint32_t {
        CAPTURE_HEALTH_NONE            = 0,
        CAPTURE_HEALTH_ACTIVE          = 1u << 0,
        CAPTURE_HEALTH_RECOVERING      = 1u << 1,
        CAPTURE_HEALTH_BACKEND_FAILED  = 1u << 2,
        CAPTURE_HEALTH_CONTENT_SUSPECT = 1u << 3,
        CAPTURE_HEALTH_PAUSED          = 1u << 4,
        CAPTURE_HEALTH_RECONFIGURE_REQUIRED = 1u << 5,
    };


    // Match capture_bridge.py byte for byte; bump the mapping version on changes.
    // Python writes ui_* and cfg_* fields; the engine reads configuration on
    // RECONFIGURE_ENCODER and publishes engine_* and status fields.
    struct SharedMemoryLayout {

        // Command channel (Python -> C++)
        volatile CommandType ui_command;
        volatile uint32_t    ui_param1;       // General purpose param (e.g. duration_seconds)
        volatile uint32_t    ui_param2;
        volatile uint32_t    ui_param3;
        wchar_t              ui_string[256];  // General purpose string (e.g. output path)

        // Write engine_string/engine_param* before publishing engine_response.
        // Only the UI consumes a response by clearing it to NONE. Use SetEngineError
        // and SetEngineString for bounded payload writes. The UI follows the same
        // payload-first ordering when publishing ui_command.
        volatile ResponseType engine_response;
        volatile uint32_t     engine_param1;
        volatile uint32_t     engine_param2;
        volatile float        engine_param3;
        wchar_t               engine_string[512];

        // Status fields (C++ -> Python, updated every command loop tick)
        volatile bool     is_recording;
        volatile bool     is_initialized;
        volatile uint64_t frames_captured;
        volatile uint64_t bytes_written;

        // Python sets these before RECONFIGURE_ENCODER. Zero means no change.
        volatile uint32_t cfg_bitrate_kbps;   // New encoder bitrate in kbps (0 = no change)
        volatile uint32_t cfg_target_width;   // New output width  in pixels (0 = no change)
        volatile uint32_t cfg_target_height;  // New output height in pixels (0 = no change)

        // Hardware encoding status (C++ -> Python, set once at init)
        volatile bool     nvenc_active;       // true if NVENC initialized successfully

        // v2 fields: encoder reconfiguration (Python -> C++)
        volatile uint32_t cfg_codec_pref;     // 0=auto 1=h264 2=hevc 3=av1
        volatile uint32_t cfg_preset;         // 1-7
        char              active_codec[64];   // null-terminated UTF-8 codec name (C++ -> Python)

        // v3 fields: multiband audio (C++ -> Python, set at init)
        volatile bool     multiband_enabled;
        char              active_audio_mappings[1024]; // JSON blob: {app: category}

        // v4 fields: capture/content health (C++ -> Python)
        volatile uint32_t capture_health_flags;
        volatile uint32_t capture_generation;       // increments after backend reset
        volatile uint32_t content_sample_sequence;  // ~1 Hz; wraps harmlessly
        volatile uint32_t content_suspicious_streak;
        volatile float    content_luma_mean;        // derived metric only, no pixels
        volatile float    content_luma_variance;    // derived metric only, no pixels
    };


    // Bound response text to 511 wchar_t characters plus the terminator.
    // Paths included in errors may exceed the shared buffer.

    //: Write `text` into engine_string: always NUL-terminated, always inside
    //: the buffer, truncated if necessary. Does NOT publish a response.
    inline void SetEngineString(SharedMemoryLayout* layout, const wchar_t* text) {
        if (!layout) return;
        const size_t cap = sizeof(layout->engine_string) / sizeof(wchar_t); // 512
        if (!text) { layout->engine_string[0] = L'\0'; return; }
        size_t n = 0;
        while (n < cap - 1 && text[n] != L'\0') {
            layout->engine_string[n] = text[n];
            ++n;
        }
        // wcsncpy would not terminate on truncation — that is the failure mode
        // this exists to prevent, so the terminator is written explicitly.
        layout->engine_string[n] = L'\0';
    }

    //: Publish a failure: message FIRST, response LAST. This is the only
    //: sanctioned way for the engine to report ERROR_OCCURRED — writing
    //: engine_response directly on an error path skips the diagnostic and
    //: breaks the publication order the UI relies on.
    inline void SetEngineError(SharedMemoryLayout* layout, const wchar_t* message) {
        if (!layout) return;
        SetEngineString(layout, message);
        layout->engine_response = ResponseType::ERROR_OCCURRED;
    }

    // SharedMemory
    //
    // C++-side owner of the shared memory mapping.
    // Created by the C++ engine at startup. Python opens it read/write.
    class SharedMemory {
    public:
        SharedMemory();
        ~SharedMemory();

        // Creates the named file mapping and maps it into the process.
        // Must be called before GetLayout().
        bool Initialize(const wchar_t* name);

        // Unmaps the view and closes the file mapping handle.
        void Shutdown();

        // Returns a pointer to the mapped layout. Nullptr if not initialized.
        SharedMemoryLayout* GetLayout() { return layout_; }


    private:
        HANDLE              file_mapping_;
        SharedMemoryLayout* layout_;
    };


} // namespace fthr


#endif // FTHR_SHARED_MEMORY_H
