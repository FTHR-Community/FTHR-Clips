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

enum class CodecPref : uint32_t { Auto = 0, H264 = 1, HEVC = 2, AV1 = 3 };

struct EncoderConfig {
    uint32_t  src_width;
    uint32_t  src_height;
    uint32_t  enc_width;
    uint32_t  enc_height;
    uint32_t  fps;
    uint32_t  bitrate_kbps;
    CodecPref codec_pref = CodecPref::Auto;
    int       preset     = 4;   // 1=fastest … 7=best quality
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

    uint32_t GetWidth()  const { return cfg_.enc_width;  }
    uint32_t GetHeight() const { return cfg_.enc_height; }

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

// Maps P1–P7 to the codec-specific preset string. Call after avcodec_alloc_context3,
// before avcodec_open2. Returns false if codec_name is unrecognised.
bool ApplyPreset(AVCodecContext* ctx, const char* codec_name, int preset_1_7);

} // namespace fthr
