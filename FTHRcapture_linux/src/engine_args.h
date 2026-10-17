#pragma once
// The engine's command line: the positional startup contract shared with the
// UI and the Windows engine (docs/engine-startup.md) and the `--` modes that
// exit without starting capture. Pure, so it is tested without a session.

#include "encoder_policy.h"

#include <cstdint>
#include <string>
#include <vector>

namespace fthr {

enum class CliMode {
    Engine,                // positional startup arguments: run the engine
    ListPlaybackDevices,   // --list-playback-devices
    ListMicrophones,       // --list-microphones
    UsageError,            // any other `--` argument: exit 2, never start
};

struct CliDecision {
    CliMode mode = CliMode::Engine;
    std::string message;   // UsageError only
};

// Any argument that starts with "--" selects a non-engine mode, wherever it
// appears, so a flag the engine does not know can never be read as a
// positional value (as `--list-playback-devices` once was, as the fps) and
// start a full engine. The listing flags must be the only argument.
CliDecision ClassifyCommandLine(int argc, const char* const* argv);

inline constexpr uint32_t kDefaultFps = 60;
inline constexpr uint32_t kMaxFps = 360;
inline constexpr uint32_t kDefaultBufferSeconds = 30;
inline constexpr uint32_t kMaxBufferSeconds = 900;
inline constexpr uint32_t kDefaultBitrateKbps = 16000;
inline constexpr uint32_t kMinBufferBudgetMb = 64;
inline constexpr uint32_t kMaxBufferBudgetMb = 16384;

struct EngineArgs {
    uint32_t    fps = kDefaultFps;                       // argv[1]
    uint32_t    buffer_seconds = kDefaultBufferSeconds;  // argv[2]
    uint32_t    target_width = 0;                        // argv[3], 0 = native
    uint32_t    target_height = 0;                       // argv[4], 0 = native
    uint32_t    bitrate_kbps = kDefaultBitrateKbps;      // argv[5]
    uint32_t    max_buffer_mb = 0;                       // argv[6], 0 = no budget
    // argv[7] capture mode and argv[8] HWND are Windows-only.
    uint32_t    scaling_mode = 0;                        // argv[9], 1 = fit
    std::string target_output;                           // argv[10], "" = first output
    CodecPref   codec_pref = CodecPref::Auto;            // argv[11]
    int         preset = 4;                              // argv[12], 1..7
    // argv[13] is the retired multiband slot.
    bool        audio_enabled = true;                    // argv[14]
    // argv[15] microphone endpoint and argv[16] gain: the Linux engine does
    // not capture a microphone (the UI records it), so they are ignored.
    EncoderPref encoder_pref = EncoderPref::Auto;        // argv[17]
    // argv[18..22] crop: Windows-only.
    bool        separate_audio_enabled = false;          // argv[23]
    std::string playback_endpoint_id;                    // argv[24], PulseAudio sink, "" = default
    bool        microphone_mono = false;                 // argv[25], parsed and ignored

    // One line per argument that was invalid or out of range, saying what
    // was used instead.
    std::vector<std::string> warnings;
};

// Missing and empty arguments mean "default" and are not warnings.
EngineArgs ParseEngineArgs(int argc, const char* const* argv);

} // namespace fthr
