#include "encoder.h"

#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

int main() {
    constexpr uint32_t width = 160;
    constexpr uint32_t height = 90;
    constexpr uint32_t fps = 30;
    constexpr int64_t second = 1'000'000'000LL;

    fthr::EncoderConfig config{};
    config.src_width = width;
    config.src_height = height;
    config.enc_width = width;
    config.enc_height = height;
    config.fps = fps;
    config.bitrate_kbps = 300;
    config.codec_pref = fthr::CodecPref::H264;
    // Timestamp semantics must be deterministic and independent of whichever
    // GPU happens to be installed on the test machine. Hardware encoder
    // qualification is covered by linux_vaapi_encoder_test.
    config.encoder_pref = fthr::EncoderPref::Software;
    config.preset = 1;

    fthr::Encoder encoder;
    std::string codec_name;
    assert(encoder.Open(config, codec_name));
    std::vector<uint8_t> bgra(width * height * 4, 0x40);
    std::vector<fthr::EncodedPacket> output;

    int64_t wall_time = 100 * second;
    for (int frame = 0; frame < 120; ++frame) {
        wall_time += frame % 5 == 0 ? second / 15 : second / 30;
        assert(encoder.EncodeFrame(
            bgra.data(),
            width * 4,
            wall_time,
            [&](fthr::EncodedPacket packet) {
                output.push_back(std::move(packet));
            }));
    }

    assert(output.size() >= 40);
    assert(output.front().pts == 0);
    bool observed_capture_gap = false;
    int64_t previous_keyframe_pts = output.front().is_keyframe
        ? output.front().pts
        : -1;
    int keyframes = output.front().is_keyframe ? 1 : 0;
    for (size_t i = 1; i < output.size(); ++i) {
        assert(output[i].pts > output[i - 1].pts);
        assert(output[i].wall_time_ns > output[i - 1].wall_time_ns);
        observed_capture_gap |= output[i].pts - output[i - 1].pts > 1;
        if (output[i].is_keyframe) {
            if (previous_keyframe_pts >= 0)
                assert(output[i].pts - previous_keyframe_pts <= 2 * fps + 1);
            previous_keyframe_pts = output[i].pts;
            ++keyframes;
        }
    }
    // Double-length capture gaps must remain visible in media time. A
    // frame-counter implementation would keep every PTS delta equal to one.
    assert(observed_capture_gap);
    assert(keyframes >= 2);
    const int64_t media_span_ns =
        (output.back().pts - output.front().pts) * second / fps;
    const int64_t capture_span_ns =
        output.back().wall_time_ns - output.front().wall_time_ns;
    assert(media_span_ns >= capture_span_ns - second / fps);
    assert(media_span_ns <= capture_span_ns + second / fps);
    return 0;
}
