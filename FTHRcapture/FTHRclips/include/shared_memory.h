// shared_memory.h
// FTHR Capture Engine - Shared memory IPC channel
//
// Responsibilities:
//   - Define the SharedMemoryLayout struct that Python and C++ both map
//   - Define the CommandType and ResponseType enums used over the channel
//   - Provide SharedMemory: the C++-side owner that creates the mapping
//
// Layout contract:
//   Python (capture_bridge.py) opens the mapping read/write.
//   C++ (main.cpp command loop) polls ui_command and writes engine_response.
//   Both sides treat the layout as a single shared struct.
//
// Threading:
//   The layout is accessed from main.cpp's command loop (main thread) and
//   read by Python's polling loop. The 10ms Sleep() in the command loop
//   provides sufficient visibility without explicit synchronisation for
//   the use patterns here (one writer per field at a time).
//
// What does NOT live here:
//   - Encoding configuration   ->  video_encoder.h (EncoderConfig)
//   - Capture configuration    ->  capture_engine.h (CaptureConfig)
//   - Python bridge logic      ->  capture_bridge.py

#pragma once
#ifndef FTHR_SHARED_MEMORY_H
#define FTHR_SHARED_MEMORY_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <cstdint>


namespace fthr {


    // ---------------------------------------------------------------------------
    // CommandType
    //
    // Written by Python (ui_command field), read by C++ command loop.
    // Values are explicit to prevent silent renumbering if new commands
    // are inserted between existing ones.
    //
    // Deprecated commands (SET_*) remain in the enum for ABI stability.
    // They are accepted by the command loop and silently ignored.
    // The new settings pipeline passes all configuration via argv at
    // engine startup rather than through these runtime commands.
    // ---------------------------------------------------------------------------
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

        // Phase 6: runtime encoder reconfiguration without process restart.
        // When sent, the engine reads cfg_bitrate_kbps / cfg_target_width /
        // cfg_target_height from SharedMemoryLayout and applies them to the
        // next SaveClip call. Currently stubbed in the command loop.
        RECONFIGURE_ENCODER = 10,
    };


    // ---------------------------------------------------------------------------
    // ResponseType
    //
    // Written by C++ command loop (engine_response field), read by Python.
    //
    // Phase 3 addition: SAVE_STARTED
    //   Sent immediately when main thread queues a clip save task.
    //   Python no longer needs to wait for encoding to complete.
    //   CLIP_SAVED is sent later by SaveClipThread when encoding finishes.
    // ---------------------------------------------------------------------------
    enum class ResponseType : uint32_t {
        NONE = 0,
        RECORDING_STARTED = 1,
        RECORDING_STOPPED = 2,
        CLIP_SAVED = 3,
        STATUS_UPDATE = 4,
        ERROR_OCCURRED = 5,
        SAVE_STARTED = 6,   // Phase 3: async SaveClip queued successfully
    };


    // ---------------------------------------------------------------------------
    // SharedMemoryLayout
    //
    // The exact binary layout that both Python and C++ map into memory.
    // Both sides must agree on this struct. Any change here requires a
    // matching change in capture_bridge.py's SharedMemoryLayout ctypes struct.
    //
    // Field ownership:
    //   UI fields (ui_*)    - written by Python, read by C++
    //   Engine fields (engine_*) - written by C++, read by Python
    //   Status fields       - written by C++, read by Python
    //   Config fields (cfg_*) - written by Python, read by C++ on RECONFIGURE_ENCODER
    // ---------------------------------------------------------------------------
    struct SharedMemoryLayout {

        // ------------------------------------------------------------------
        // Command channel (Python -> C++)
        // ------------------------------------------------------------------
        volatile CommandType ui_command;
        volatile uint32_t    ui_param1;       // General purpose param (e.g. duration_seconds)
        volatile uint32_t    ui_param2;
        volatile uint32_t    ui_param3;
        wchar_t              ui_string[256];  // General purpose string (e.g. output path)

        // ------------------------------------------------------------------
        // Response channel (C++ -> Python)
        // ------------------------------------------------------------------
        volatile ResponseType engine_response;
        volatile uint32_t     engine_param1;
        volatile uint32_t     engine_param2;
        volatile float        engine_param3;
        wchar_t               engine_string[512];

        // ------------------------------------------------------------------
        // Status fields (C++ -> Python, updated every command loop tick)
        // ------------------------------------------------------------------
        volatile bool     is_recording;
        volatile bool     is_initialized;
        volatile uint64_t frames_captured;
        volatile uint64_t bytes_written;

        // ------------------------------------------------------------------
        // Phase 6: reconfiguration fields (Python -> C++)
        //
        // Set by Python before sending RECONFIGURE_ENCODER.
        // Zero means "no change" for that field.
        // C++ command loop reads these when handling RECONFIGURE_ENCODER.
        // ------------------------------------------------------------------
        volatile uint32_t cfg_bitrate_kbps;   // New encoder bitrate in kbps (0 = no change)
        volatile uint32_t cfg_target_width;   // New output width  in pixels (0 = no change)
        volatile uint32_t cfg_target_height;  // New output height in pixels (0 = no change)

        // ------------------------------------------------------------------
        // Hardware encoding status (C++ -> Python, set once at init)
        // ------------------------------------------------------------------
        volatile bool     nvenc_active;       // true if NVENC initialized successfully
    };


    // ---------------------------------------------------------------------------
    // SharedMemory
    //
    // C++-side owner of the shared memory mapping.
    // Created by the C++ engine at startup. Python opens it read/write.
    // ---------------------------------------------------------------------------
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

        // Convenience helpers used by the C++ engine side.
        void SendCommand(CommandType cmd,
            uint32_t p1 = 0, uint32_t p2 = 0, uint32_t p3 = 0);
        bool WaitForResponse(ResponseType expected, DWORD timeout_ms = 5000);

    private:
        HANDLE              file_mapping_;
        SharedMemoryLayout* layout_;
    };


} // namespace fthr


#endif // FTHR_SHARED_MEMORY_H