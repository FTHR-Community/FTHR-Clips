#pragma once
#include "ring_buffer.h"
#include <functional>
#include <string>
#include <vector>
#include <cstdint>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

namespace fthr {

struct EncoderConfig {
    uint32_t src_width;
    uint32_t src_height;
    uint32_t enc_width;
    uint32_t enc_height;
    uint32_t fps;
    uint32_t bitrate_kbps;
};

class Encoder {
public:
    Encoder() = default;
    ~Encoder() { Close(); }

    // Opens a hardware or software encoder. Returns the codec name used.
    bool Open(const EncoderConfig& cfg, std::string& codec_used_out);
    void Close();

    // Encode one BGRA frame. On success, calls push_fn for each output packet.
    using PushFn = std::function<void(EncodedPacket)>;
    bool EncodeFrame(const uint8_t* bgra, uint32_t stride,
                     int64_t wall_time_ns, PushFn push_fn);

    // Returns codec extradata (SPS/PPS) needed to write MP4 headers.
    std::vector<uint8_t> GetExtradata() const;

    bool IsOpen() const { return codec_ctx_ != nullptr; }

private:
    bool TryOpen(const char* codec_name, const EncoderConfig& cfg);

    AVCodecContext* codec_ctx_ = nullptr;
    SwsContext*     sws_ctx_   = nullptr;
    AVFrame*        yuv_frame_ = nullptr;
    AVPacket*       pkt_       = nullptr;
    int64_t         next_pts_  = 0;
    EncoderConfig   cfg_       = {};
};

} // namespace fthr
