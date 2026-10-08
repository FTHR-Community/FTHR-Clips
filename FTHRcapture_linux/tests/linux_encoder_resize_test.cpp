#include "encoder.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

// An output mode or scale change hands the encoder frames of a new size
// mid-generation. The encoder must keep its configured output size and
// rescale, never read the new buffer with the old dimensions.
int main() {
    constexpr uint32_t width = 160;
    constexpr uint32_t height = 90;
    constexpr uint32_t fps = 30;
    constexpr int64_t frame_ns = 1'000'000'000LL / fps;

    fthr::EncoderConfig config{};
    config.src_width = width;
    config.src_height = height;
    config.enc_width = width;
    config.enc_height = height;
    config.fps = fps;
    config.bitrate_kbps = 300;
    config.codec_pref = fthr::CodecPref::H264;
    config.encoder_pref = fthr::EncoderPref::Software;
    config.preset = 1;

    fthr::Encoder encoder;
    std::string codec_name;
    assert(encoder.Open(config, codec_name));

    std::vector<fthr::EncodedPacket> output;
    auto push = [&](fthr::EncodedPacket packet) { output.push_back(std::move(packet)); };

    struct Size { uint32_t w, h, stride_padding; };
    // Original size, larger (with row padding), smaller, then back again.
    const Size sizes[] = {{width, height, 0}, {320, 180, 64}, {96, 54, 0},
                          {width, height, 0}};
    int64_t wall_time = 1'000'000'000LL;
    for (const Size& size : sizes) {
        const uint32_t stride = size.w * 4 + size.stride_padding;
        // Exactly the bytes this size needs; scaling it with the stale
        // 160x90 dimensions would read past the end of the smaller buffer.
        std::vector<uint8_t> bgra(static_cast<size_t>(stride) * size.h, 0x60);
        for (int frame = 0; frame < 15; ++frame) {
            wall_time += frame_ns;
            assert(encoder.EncodeFrame(bgra.data(), stride, size.w, size.h,
                                       wall_time, push));
        }
    }
    assert(encoder.GetWidth() == width);
    assert(encoder.GetHeight() == height);
    assert(output.size() >= 30);

    // A stride smaller than the row is rejected rather than over-read.
    std::vector<uint8_t> tiny(64 * 4 * 8, 0);
    assert(!encoder.EncodeFrame(tiny.data(), 64 * 4 - 4, 64, 8,
                                wall_time + frame_ns, push));

    std::cout << "encoder resize test passed (" << codec_name << ", "
              << output.size() << " packets)" << std::endl;
    return 0;
}
