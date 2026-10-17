#include "engine_args.h"

#include "capture_config_limits.h"

#include <algorithm>
#include <cstring>
#include <string_view>

namespace fthr {

namespace {

constexpr std::string_view kListPlayback = "--list-playback-devices";
constexpr std::string_view kListMicrophones = "--list-microphones";

const char* Arg(int argc, const char* const* argv, int index) {
    if (index >= argc || !argv[index] || argv[index][0] == '\0') return nullptr;
    return argv[index];
}

// Unsigned decimal through the shared ParseU32: no sign, locale or overflow
// surprises. A missing or empty argument is the default; anything else that
// does not parse is the default plus a warning.
uint32_t U32(int argc, const char* const* argv, int index, uint32_t def,
             const char* name, std::vector<std::string>& warnings) {
    const char* raw = Arg(argc, argv, index);
    if (!raw) return def;
    const auto parsed = capture_config_limits::ParseU32(raw);
    if (parsed.valid) return parsed.value;
    warnings.push_back("argv[" + std::to_string(index) + "] " + name + "='" +
                       raw + "' is not an unsigned integer; using " +
                       std::to_string(def));
    return def;
}

std::string Str(int argc, const char* const* argv, int index) {
    const char* raw = Arg(argc, argv, index);
    return raw ? std::string(raw) : std::string();
}

uint32_t Clamp(uint32_t value, uint32_t lo, uint32_t hi, const char* name,
               std::vector<std::string>& warnings) {
    const uint32_t clamped = std::clamp(value, lo, hi);
    if (clamped != value)
        warnings.push_back(std::string(name) + " " + std::to_string(value) +
                           " is outside " + std::to_string(lo) + ".." +
                           std::to_string(hi) + "; using " +
                           std::to_string(clamped));
    return clamped;
}

} // namespace

CliDecision ClassifyCommandLine(int argc, const char* const* argv) {
    for (int i = 1; i < argc; ++i) {
        if (!argv[i] || std::strncmp(argv[i], "--", 2) != 0) continue;
        const std::string_view flag(argv[i]);
        const bool known = flag == kListPlayback || flag == kListMicrophones;
        if (!known)
            return {CliMode::UsageError,
                    "unknown option '" + std::string(flag) + "'"};
        if (i != 1 || argc != 2)
            return {CliMode::UsageError,
                    "'" + std::string(flag) + "' takes no other arguments"};
        return {flag == kListPlayback ? CliMode::ListPlaybackDevices
                                      : CliMode::ListMicrophones, {}};
    }
    return {};
}

EngineArgs ParseEngineArgs(int argc, const char* const* argv) {
    EngineArgs a;
    auto& w = a.warnings;

    a.fps = U32(argc, argv, 1, kDefaultFps, "fps", w);
    if (a.fps == 0) {
        w.push_back("fps 0 is invalid; using " + std::to_string(kDefaultFps));
        a.fps = kDefaultFps;
    }
    a.fps = Clamp(a.fps, 1, kMaxFps, "fps", w);

    a.buffer_seconds = U32(argc, argv, 2, kDefaultBufferSeconds,
                           "buffer_seconds", w);
    if (a.buffer_seconds == 0) {
        w.push_back("buffer_seconds 0 is invalid; using " +
                    std::to_string(kDefaultBufferSeconds));
        a.buffer_seconds = kDefaultBufferSeconds;
    }
    a.buffer_seconds = Clamp(a.buffer_seconds, 1, kMaxBufferSeconds,
                             "buffer_seconds", w);

    a.target_width = U32(argc, argv, 3, 0, "target_width", w);
    a.target_height = U32(argc, argv, 4, 0, "target_height", w);
    if ((a.target_width == 0) != (a.target_height == 0) ||
            a.target_width > 7680 || a.target_height > 4320) {
        w.push_back("target resolution " + std::to_string(a.target_width) +
                    "x" + std::to_string(a.target_height) +
                    " is unusable; using native");
        a.target_width = a.target_height = 0;
    }

    a.bitrate_kbps = Clamp(
        U32(argc, argv, 5, kDefaultBitrateKbps, "bitrate_kbps", w),
        capture_config_limits::kMinBitrateKbps,
        capture_config_limits::kMaxBitrateKbps, "bitrate_kbps", w);

    a.max_buffer_mb = U32(argc, argv, 6, 0, "max_buffer_mb", w);
    if (a.max_buffer_mb != 0)
        a.max_buffer_mb = Clamp(a.max_buffer_mb, kMinBufferBudgetMb,
                                kMaxBufferBudgetMb, "max_buffer_mb", w);

    a.scaling_mode = U32(argc, argv, 9, 0, "scaling_mode", w) == 1 ? 1 : 0;
    a.target_output = Str(argc, argv, 10);

    const uint32_t codec = U32(argc, argv, 11, 0, "codec_pref", w);
    if (codec <= static_cast<uint32_t>(CodecPref::AV1)) {
        a.codec_pref = static_cast<CodecPref>(codec);
    } else {
        w.push_back("codec_pref " + std::to_string(codec) +
                    " is unknown; using Auto");
    }
    a.preset = static_cast<int>(
        Clamp(U32(argc, argv, 12, 4, "preset", w), 1, 7, "preset", w));

    a.audio_enabled = U32(argc, argv, 14, 1, "audio_enabled", w) != 0;

    const uint32_t encoder = U32(argc, argv, 17, 0, "encoder_pref", w);
    if (encoder <= static_cast<uint32_t>(EncoderPref::Software)) {
        a.encoder_pref = static_cast<EncoderPref>(encoder);
    } else {
        // Never Software by accident: an unknown value must not opt into a
        // software encoder the user did not choose.
        w.push_back("encoder_pref " + std::to_string(encoder) +
                    " is unknown; using Auto");
    }

    a.separate_audio_enabled =
        U32(argc, argv, 23, 0, "separate_audio", w) != 0;
    a.playback_endpoint_id = Str(argc, argv, 24);
    a.microphone_mono = U32(argc, argv, 25, 0, "microphone_mono", w) != 0;
    return a;
}

} // namespace fthr
