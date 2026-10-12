#include "encoder.h"
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
}

namespace {
constexpr int kSkip = 77;

// ApplyPreset must not set a VA-API quality level: its meaning differs
// between drivers, so the unset default (-1) is the contract.
int CompressionLevelFor(const AVCodec* codec, int preset) {
    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    assert(ctx);
    assert(fthr::ApplyPreset(ctx, "h264_vaapi", preset));
    const int level = ctx->compression_level;
    avcodec_free_context(&ctx);
    return level;
}
}

int main() {
    if (const AVCodec* vaapi = avcodec_find_encoder_by_name("h264_vaapi")) {
        assert(CompressionLevelFor(vaapi, 1) == -1);
        assert(CompressionLevelFor(vaapi, 7) == -1);
    }

    fthr::EncoderConfig cfg{};
    cfg.src_width = 320;
    cfg.src_height = 180;
    cfg.enc_width = 320;
    cfg.enc_height = 180;
    cfg.fps = 30;
    cfg.bitrate_kbps = 1500;
    cfg.codec_pref = fthr::CodecPref::H264;
    cfg.encoder_pref = fthr::EncoderPref::Amd;
    cfg.preset = 4;

    fthr::Encoder encoder;
    std::string codec;
    if (!encoder.Open(cfg, codec)) {
        std::cout << "SKIP: h264_vaapi is unavailable or cannot initialize VA-API" << std::endl;
        return kSkip;
    }
    if (codec != "h264_vaapi") {
        std::cout << "SKIP: requested h264_vaapi was not selected (got "
                  << codec << ")" << std::endl;
        return kSkip;
    }

    std::vector<uint8_t> bgra(cfg.src_width * cfg.src_height * 4, 0);
    size_t packets = 0;
    for (int frame = 0; frame < 60; ++frame) {
        for (size_t i = 0; i < bgra.size(); i += 4) {
            bgra[i] = static_cast<uint8_t>(frame * 3);
            bgra[i + 1] = static_cast<uint8_t>((i / 4) % 256);
            bgra[i + 2] = 80;
            bgra[i + 3] = 255;
        }
        const bool ok = encoder.EncodeFrame(
            bgra.data(), cfg.src_width * 4,
            static_cast<int64_t>(frame) * 1'000'000'000LL / cfg.fps,
            [&](fthr::EncodedPacket packet) {
                assert(!packet.data.empty());
                ++packets;
            });
        assert(ok);
    }
    assert(packets > 0);
    std::cout << "VA-API encoder produced " << packets
              << " packets using " << codec << std::endl;
    return 0;
}
