#include "software_replay_encoder.h"
#include <windows.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/mem.h>
}
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <cmath>
#include <vector>

int RunSoftwareReplayEncoderTests() {
    auto check = [](bool ok, const char* message) {
        if (!ok) { std::cerr << "FAILED: " << message << '\n'; std::exit(1); }
    };
    const auto selection = fthr::SelectWindowsReplayPolicy(
        fthr::EncoderVendor::Intel, fthr::EncoderPreference::Software, fthr::VideoCodec::H264);
    check(selection.allowed && !selection.same_adapter,
        "explicit software works on any capture adapter");
    check(!fthr::SelectWindowsReplayPolicy(fthr::EncoderVendor::Intel,
        fthr::EncoderPreference::Software, fthr::VideoCodec::AV1).allowed,
        "unsupported software codec is rejected without substitution");
    fthr::SoftwareReplayEncoder encoder;
    fthr::EncoderConfig config;
    config.src_width = 320; config.src_height = 180;
    config.enc_width = 320; config.enc_height = 240;
    config.fps = 30; config.bitrate_kbps = 1000; config.scaling_mode = 1;
    AVCodecContext* decoder = avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_H264));
    AVFrame* decoded = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    check(decoder && decoded && packet, "allocate software replay decoder");
    LARGE_INTEGER qpc_frequency{};
    check(QueryPerformanceFrequency(&qpc_frequency)
            && qpc_frequency.QuadPart > 0,
        "query the real QPC frequency for software replay timing");
    constexpr int64_t base_qpc = 1'000'000;
    const int64_t frame_period = qpc_frequency.QuadPart / config.fps;
    const std::vector<int64_t> submitted_qpc = {
        base_qpc,
        base_qpc + frame_period,
        base_qpc + frame_period * 2 + frame_period / 2,
        base_qpc + frame_period * 5,
        base_qpc + frame_period * 5,
        base_qpc + frame_period * 9,
        base_qpc + frame_period * 10,
        base_qpc + frame_period * 11,
    };
    std::vector<int64_t> observed_pts;
    std::vector<int64_t> observed_walls;
    int packets = 0, frames = 0;
    bool requested_keyframe = false;
    auto callback = [&](const uint8_t* data, uint32_t size, int64_t pts, bool key, int64_t wall) {
        observed_pts.push_back(pts);
        observed_walls.push_back(wall);
        if (packets == 3) requested_keyframe = key;
        check(av_new_packet(packet, static_cast<int>(size)) == 0, "allocate encoded test packet");
        std::memcpy(packet->data, data, size);
        check(avcodec_send_packet(decoder, packet) == 0, "decode AVCC replay packet");
        av_packet_unref(packet);
        while (avcodec_receive_frame(decoder, decoded) == 0) {
            check(decoded->width == 320 && decoded->height == 240, "software target dimensions");
            check(decoded->data[0][0] <= 20, "FIT top padding stays black");
            check(decoded->data[0][120 * decoded->linesize[0] + 160] > 150,
                "FIT center contains converted source content");
            ++frames;
            av_frame_unref(decoded);
        }
        ++packets;
    };
    check(encoder.Initialize(config, nullptr, nullptr, callback, true),
        encoder.GetLastError().c_str());
    const auto video = encoder.GetVideoConfig();
    check(fthr::IsValidEncodedVideoConfig(video) && encoder.IsVideoConfigReady(),
        "software stream configuration is ready before capture");
    auto active = fthr::EvaluateActiveReplayCapability(selection,
        encoder.GetActiveEncoderInfo(), true, 7);
    check(active.initialized && !active.hardware && active.generation == 7,
        "software capability truth is accepted without claiming hardware");
    check(!fthr::ShouldAllocateRawReplayPool(active.active_backend, true),
        "software replay uses compressed packet storage");
    decoder->extradata_size = static_cast<int>(video.codec_extradata.size());
    decoder->extradata = static_cast<uint8_t*>(av_mallocz(
        video.codec_extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
    check(decoder->extradata != nullptr, "allocate decoder configuration");
    std::memcpy(decoder->extradata, video.codec_extradata.data(), video.codec_extradata.size());
    check(avcodec_open2(decoder, decoder->codec, nullptr) == 0, "open replay decoder");
    // Padded rows catch accidental tight-stride assumptions in capture readback.
    const uint32_t stride = 320 * 4 + 64;
    std::vector<uint8_t> source(stride * 180, 255);
    for (int i = 0; i < static_cast<int>(submitted_qpc.size()); ++i) {
        if (i == 3) encoder.RequestKeyframe();
        check(encoder.EncodeFrameCPU(source.data(), stride, submitted_qpc[i]),
            encoder.GetLastError().c_str());
    }
    int64_t epoch = 0, frequency = 0;
    check(encoder.GetEncodeEpoch(epoch, frequency) && epoch == base_qpc
            && frequency == qpc_frequency.QuadPart,
        "software replay publishes its clock epoch");
    encoder.Shutdown();
    avcodec_send_packet(decoder, nullptr);
    while (avcodec_receive_frame(decoder, decoded) == 0) ++frames;
    std::vector<int64_t> expected_pts;
    int64_t last_expected_pts = -1;
    for (const auto wall : submitted_qpc) {
        int64_t expected = wall == base_qpc
            ? 0
            : ((wall - base_qpc) * config.fps) / qpc_frequency.QuadPart;
        if (expected <= last_expected_pts) expected = last_expected_pts + 1;
        expected_pts.push_back(expected);
        last_expected_pts = expected;
    }
    check(observed_pts == expected_pts && observed_walls == submitted_qpc,
        "software replay preserves irregular QPC cadence and missed-frame gaps");
    check(packets == static_cast<int>(submitted_qpc.size())
            && frames == static_cast<int>(submitted_qpc.size()) && requested_keyframe,
        "all software packets decode and recording can request a keyframe");
    av_packet_free(&packet); av_frame_free(&decoded); avcodec_free_context(&decoder);
    std::cout << "Software replay encode/decode and policy tests passed.\n";
    return 12;
}
