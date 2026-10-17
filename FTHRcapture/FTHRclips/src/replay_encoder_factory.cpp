#include "replay_encoder.h"

#include "ffmpeg_amf_replay_encoder.h"
#include "ffmpeg_qsv_replay_encoder.h"
#include "hardware_encoder.h"
#include "software_replay_encoder.h"

namespace fthr {

std::unique_ptr<IReplayEncoder> CreateProductionReplayEncoder(
    EncoderVendor vendor,
    VideoCodec codec) {
    switch (SelectProductionReplayBackend(vendor, codec)) {
    case ReplayEncoderBackend::NativeNvenc:
        return std::make_unique<HardwareEncoder>(codec);
    case ReplayEncoderBackend::FfmpegAmf:
        return std::make_unique<FfmpegAmfReplayEncoder>(codec);
    case ReplayEncoderBackend::FfmpegQsv:
        return std::make_unique<FfmpegQsvReplayEncoder>(codec);
    case ReplayEncoderBackend::Software:
        if (vendor == EncoderVendor::Software && codec == VideoCodec::H264)
            return std::make_unique<SoftwareReplayEncoder>();
        return nullptr;
    }
    return nullptr;
}

} // namespace fthr
