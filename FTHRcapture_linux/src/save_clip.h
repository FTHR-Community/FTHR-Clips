#pragma once
#include "ring_buffer.h"
#include "shared_memory.h"
#include <string>
#include <vector>
#include <cstdint>

extern "C" {
#include <libavcodec/avcodec.h>
}

namespace fthr {

// Write a clip file to path.
// video_packets: snapshot from EncodedRingBuffer (H.264 annexb/global-header)
// audio_pcm:     interleaved float32 samples at audio_sample_rate/audio_channels
// extradata:     SPS/PPS from avcodec_parameters (AV_CODEC_FLAG_GLOBAL_HEADER)
// shm:           optional, updated with bytes_written progress (may be nullptr)
bool save_clip_to_file(
    const std::string&               path,
    const std::vector<EncodedPacket>& video_packets,
    const std::vector<float>&        audio_pcm,
    int                              audio_sample_rate,
    int                              audio_channels,
    const std::vector<uint8_t>&      extradata,
    uint32_t                         fps,
    uint32_t                         width,
    uint32_t                         height,
    AVCodecID                        video_codec_id,
    SharedMemoryLayout*              shm
);

} // namespace fthr
