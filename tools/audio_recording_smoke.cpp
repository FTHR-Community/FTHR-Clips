#include "continuous_recording_writer.h"
#include <cstdlib>
#include <cmath>
#include <iostream>
#include <thread>
#include <chrono>
extern "C" {
#include <libavformat/avformat.h>
}

#define CHECK(expression) do { if (!(expression)) { \
    std::cerr << "FAILED line " << __LINE__ << ": " << #expression << '\n'; \
    std::exit(1); } } while (false)

int main(int argc, char** argv) {
    CHECK(argc == 3 || argc == 4);
    const std::string mode = argc == 4 ? argv[3] : "";
    AVFormatContext* input = nullptr;
    CHECK(avformat_open_input(&input, argv[1], nullptr, nullptr) >= 0);
    CHECK(avformat_find_stream_info(input, nullptr) >= 0);
    const int video = av_find_best_stream(input, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    CHECK(video >= 0);
    auto* stream = input->streams[video];
    fthr::EncodedVideoConfig config;
    config.width = stream->codecpar->width; config.height = stream->codecpar->height;
    config.frame_rate = {30, 1}; config.time_base = {1, 30};
    config.bitrate_kbps = 500; config.max_keyframe_interval_frames = 30;
    config.codec_extradata.assign(stream->codecpar->extradata,
                                  stream->codecpar->extradata + stream->codecpar->extradata_size);
    fthr::ContinuousRecordingWriter writer;
    CHECK(writer.Start(argv[2], config, {}, 0, true));
    fthr::AudioSourceMetadata system, mic, app;
    system.identity = {fthr::AudioSourceId{"11111111-1111-4111-8111-111111111111"},
        fthr::AudioSourceType::System, "default-mix", "Default Mix", "system-audio"};
    mic.identity = {fthr::AudioSourceId{"22222222-2222-4222-8222-222222222222"},
        fthr::AudioSourceType::Microphone, "microphone", "Microphone", "microphone"};
    mic.input_gain_percent = 175; mic.mono = true;
    app.identity = {fthr::AudioSourceId{"33333333-3333-4333-8333-333333333333"},
        fthr::AudioSourceType::Application, "game", "Game", "generic-app"};
    AVPacket* packet = av_packet_alloc();
    int frame = 0;
    while (av_read_frame(input, packet) >= 0) {
        if (packet->stream_index == video) {
            const int64_t timestamp = 1000000000LL + static_cast<int64_t>(frame) * 10000000 / 30;
            CHECK(writer.PushVideo(packet->data, packet->size, frame,
                (packet->flags & AV_PKT_FLAG_KEY) != 0, 0, timestamp));
            std::vector<float> pcm(1600 * 2);
            for (int sample = 0; sample < 1600; ++sample) {
                const float value = static_cast<float>(std::sin((frame * 1600 + sample) * 0.05) * .125);
                pcm[sample * 2] = value; pcm[sample * 2 + 1] = -value;
            }
            CHECK(writer.PushSourceAudio(system, reinterpret_cast<uint8_t*>(pcm.data()),
                static_cast<uint32_t>(pcm.size() * 4), timestamp, 1600));
            if (frame >= 9 && (mode != "long" || frame < 15))
                CHECK(writer.PushSourceAudio(mic, reinterpret_cast<uint8_t*>(pcm.data()),
                    static_cast<uint32_t>(pcm.size() * 4), timestamp, 1600));
            if (frame >= (mode == "long" ? 300 : 15))
                CHECK(writer.PushSourceAudio(app, reinterpret_cast<uint8_t*>(pcm.data()),
                    static_cast<uint32_t>(pcm.size() * 4), timestamp, 1600));
            ++frame;
        }
        av_packet_unref(packet);
    }
    av_packet_free(&packet); avformat_close_input(&input);
    CHECK(frame >= 30);
    if (mode == "crash") {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        std::_Exit(0); // Intentionally omit Stop/destructors: test committed fragments.
    }
    if (!writer.Stop()) { std::cerr << writer.LastError() << '\n'; return 1; }
    AVFormatContext* check = nullptr;
    CHECK(avformat_open_input(&check, argv[2], nullptr, nullptr) >= 0);
    CHECK(avformat_find_stream_info(check, nullptr) >= 0);
    CHECK(check->nb_streams == 4);
    for (unsigned index = 1; index < 4; ++index)
        CHECK(check->streams[index]->codecpar->codec_id == AV_CODEC_ID_PCM_F32LE);
    avformat_close_input(&check);
    std::cout << "Independent live recording with late microphone/application streams passed.\n";
}
