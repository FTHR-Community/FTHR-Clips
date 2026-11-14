#pragma once
#include "encoded_video_config.h"
#include <vector>

namespace fthr {

// Kept in the executable as well as in stdout. The UI checks this marker before
// launching: older engines interpret unknown flags as normal capture startup.
inline constexpr char kEncoderProbeMarker[] = "FTHR_GPU_ENCODER_PROBE_V1";
int RunNativeEncoderProbe(int argc, char* argv[]);

namespace detail {
struct EncoderProbePacket { std::vector<uint8_t> bytes; int64_t pts = 0; };
bool ValidateEncoderProbePackets(const std::vector<EncoderProbePacket>& packets,
    const EncodedVideoConfig& config, uint32_t& decoded);
}

} // namespace fthr
