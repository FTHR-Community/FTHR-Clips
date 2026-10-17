#pragma once
// Which FFmpeg encoders the engine may try, in order. Pure and free of
// FFmpeg so the policy is pinned by a test without a GPU.

#include <cstdint>
#include <vector>

namespace fthr {

enum class CodecPref : uint32_t { Auto = 0, H264 = 1, HEVC = 2, AV1 = 3 };
enum class EncoderPref : uint32_t {
    Auto = 0, Nvidia = 1, Amd = 2, Intel = 3, Software = 4
};

// Candidates for a codec and encoder preference, matching the Windows 2.0
// policy: Auto tries the hardware backends only (NVENC, then VA-API/AMF, then
// QSV) and never falls back to software on its own. Software encoders are
// tried only when the preference is Software, so a missing or broken GPU
// encoder surfaces as HARDWARE ENCODING UNAVAILABLE instead of a silent,
// CPU-heavy software replay.
std::vector<const char*> EncoderCandidates(CodecPref codec_pref,
                                           EncoderPref encoder_pref);

} // namespace fthr
