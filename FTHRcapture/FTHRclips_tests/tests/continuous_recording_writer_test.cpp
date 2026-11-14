#include "continuous_recording_writer.h"
#include "audio_encoder.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <string>
#include <thread>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/dict.h>
#include <libavutil/mathematics.h>
}

namespace {

constexpr int kFps = 30;
constexpr uint8_t kHighlightUuidUserType[16] = {
    0x9a, 0x68, 0xd1, 0x74, 0x6b, 0xf0, 0x42, 0x77,
    0x9b, 0x6d, 0x8b, 0x04, 0xe3, 0x48, 0x43, 0x2a};
int recording_checks = 0;

struct EncodedPacketSample {
    std::vector<uint8_t> data;
    int64_t pts = AV_NOPTS_VALUE;
    bool is_keyframe = false;
};

void CheckRecording(bool condition, const char* message) {
    ++recording_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

bool ReceivePackets(
    AVCodecContext* encoder,
    AVPacket* packet,
    fthr::ContinuousRecordingWriter& writer,
    uint64_t generation = 0,
    std::vector<uint8_t>* keyframe_sample = nullptr,
    int64_t discard_before_keyframe_pts = AV_NOPTS_VALUE,
    bool* accepted_keyframe = nullptr,
    std::vector<EncodedPacketSample>* packet_cache = nullptr) {
    while (true) {
        const int result = avcodec_receive_packet(encoder, packet);
        if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
        if (result < 0 || packet->pts == AV_NOPTS_VALUE) return false;
        const bool is_keyframe = (packet->flags & AV_PKT_FLAG_KEY) != 0;
        if (discard_before_keyframe_pts != AV_NOPTS_VALUE
            && accepted_keyframe && !*accepted_keyframe) {
            if (!is_keyframe || packet->pts < discard_before_keyframe_pts) {
                av_packet_unref(packet);
                continue;
            }
            *accepted_keyframe = true;
        }
        if (keyframe_sample && keyframe_sample->empty() && is_keyframe)
            keyframe_sample->assign(packet->data, packet->data + packet->size);
        const bool accepted = writer.PushVideo(
            packet->data,
            static_cast<uint32_t>(packet->size),
            packet->pts,
            is_keyframe,
            generation);
        if (accepted && packet_cache != nullptr) {
            packet_cache->push_back({
                std::vector<uint8_t>(packet->data, packet->data + packet->size),
                packet->pts, is_keyframe});
        }
        av_packet_unref(packet);
        if (!accepted) return false;
    }
}

bool ProbeMedia(
    const std::filesystem::path& path,
    bool expect_audio,
    int expected_fps = 0,
    int64_t expected_bitrate_bps = 0) {
    AVFormatContext* input = nullptr;
    if (avformat_open_input(&input, path.string().c_str(), nullptr, nullptr) < 0)
        return false;
    const bool found_info = avformat_find_stream_info(input, nullptr) >= 0;
    const int video_index = found_info ? av_find_best_stream(
        input, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0) : -1;
    const int audio_index = found_info ? av_find_best_stream(
        input, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0) : -1;
    const AVCodecParameters* video_parameters = video_index >= 0
        ? input->streams[video_index]->codecpar : nullptr;
    const AVStream* video_stream = video_index >= 0
        ? input->streams[video_index] : nullptr;
    const bool correct_sdr_color = video_parameters
        && video_parameters->color_range == AVCOL_RANGE_MPEG
        && video_parameters->color_primaries == AVCOL_PRI_BT709
        && video_parameters->color_trc == AVCOL_TRC_BT709
        && video_parameters->color_space == AVCOL_SPC_BT709;
    const bool correct_declared_rate = !video_stream || expected_fps <= 0
        || (video_stream->avg_frame_rate.num == expected_fps
            && video_stream->avg_frame_rate.den == 1
            && video_stream->r_frame_rate.num == expected_fps
            && video_stream->r_frame_rate.den == 1);
    const AVDictionaryEntry* configured_fps = expected_fps > 0
        ? av_dict_get(input->metadata, "fthr_frame_rate", nullptr, 0)
        : nullptr;
    const AVDictionaryEntry* configured_bitrate = expected_bitrate_bps > 0
        ? av_dict_get(input->metadata, "fthr_video_bitrate_bps", nullptr, 0)
        : nullptr;
    const bool correct_configured_metadata =
        (expected_fps <= 0
            || (configured_fps && std::string(configured_fps->value)
                == std::to_string(expected_fps) + "/1"))
        && (expected_bitrate_bps <= 0
            || (configured_bitrate && std::string(configured_bitrate->value)
                == std::to_string(expected_bitrate_bps)));
    bool read_video = false;
    bool read_audio = false;
    bool audio_sync_samples = true;
    bool constant_sample_timing = true;
    int64_t previous_video_pts = AV_NOPTS_VALUE;
    const int64_t expected_video_delta = expected_fps > 0 && video_stream
        ? av_rescale_q(1, AVRational{1, expected_fps}, video_stream->time_base)
        : 0;
    AVPacket* packet = av_packet_alloc();
    if (packet) {
        while (av_read_frame(input, packet) >= 0) {
            if (packet->stream_index == video_index && packet->size > 0) {
                read_video = true;
                if (expected_video_delta > 0
                    && packet->pts != AV_NOPTS_VALUE) {
                    if (previous_video_pts != AV_NOPTS_VALUE
                        && packet->pts - previous_video_pts
                            != expected_video_delta) {
                        constant_sample_timing = false;
                    }
                    previous_video_pts = packet->pts;
                }
            }
            if (packet->stream_index == audio_index && packet->size > 0) {
                read_audio = true;
                if (expect_audio && (packet->flags & AV_PKT_FLAG_KEY) == 0)
                    audio_sync_samples = false;
            }
            av_packet_unref(packet);
        }
        av_packet_free(&packet);
    }
    avformat_close_input(&input);
    return video_index >= 0 && read_video && correct_sdr_color
        && correct_declared_rate && correct_configured_metadata
        && constant_sample_timing
        && (!expect_audio || (audio_index >= 0 && read_audio
            && audio_sync_samples));
}

size_t CountBox(const std::vector<uint8_t>& bytes, const char name[4]) {
    size_t count = 0;
    for (size_t index = 4; index + 4 <= bytes.size(); ++index) {
        if (std::equal(name, name + 4, bytes.begin() + index)) ++count;
    }
    return count;
}

bool HasBtrtBitrate(
    const std::vector<uint8_t>& bytes, uint32_t expected_bitrate_bps) {
    constexpr char btrt[4] = {'b', 't', 'r', 't'};
    for (size_t index = 0; index + 16 <= bytes.size(); ++index) {
        if (!std::equal(std::begin(btrt), std::end(btrt), bytes.begin() + index))
            continue;
        const auto read_u32 = [&bytes](size_t offset) {
            return (static_cast<uint32_t>(bytes[offset]) << 24)
                | (static_cast<uint32_t>(bytes[offset + 1]) << 16)
                | (static_cast<uint32_t>(bytes[offset + 2]) << 8)
                | static_cast<uint32_t>(bytes[offset + 3]);
        };
        const uint32_t max_bitrate = read_u32(index + 8);
        const uint32_t average_bitrate = read_u32(index + 12);
        if (max_bitrate == expected_bitrate_bps
            && average_bitrate == expected_bitrate_bps)
            return true;
    }
    return false;
}

std::vector<std::string> ReadHighlightUuidPayloads(
    const std::vector<uint8_t>& bytes) {
    std::vector<std::string> payloads;
    auto read_u32 = [&bytes](size_t offset) -> uint64_t {
        return (static_cast<uint64_t>(bytes[offset]) << 24)
            | (static_cast<uint64_t>(bytes[offset + 1]) << 16)
            | (static_cast<uint64_t>(bytes[offset + 2]) << 8)
            | static_cast<uint64_t>(bytes[offset + 3]);
    };
    size_t offset = 0;
    while (offset + 8 <= bytes.size()) {
        uint64_t box_size = read_u32(offset);
        size_t header_size = 8;
        if (box_size == 1) {
            if (offset + 16 > bytes.size()) return {};
            box_size = (static_cast<uint64_t>(bytes[offset + 8]) << 56)
                | (static_cast<uint64_t>(bytes[offset + 9]) << 48)
                | (static_cast<uint64_t>(bytes[offset + 10]) << 40)
                | (static_cast<uint64_t>(bytes[offset + 11]) << 32)
                | (static_cast<uint64_t>(bytes[offset + 12]) << 24)
                | (static_cast<uint64_t>(bytes[offset + 13]) << 16)
                | (static_cast<uint64_t>(bytes[offset + 14]) << 8)
                | static_cast<uint64_t>(bytes[offset + 15]);
            header_size = 16;
        } else if (box_size == 0) {
            box_size = bytes.size() - offset;
        }
        if (box_size < header_size || box_size > bytes.size() - offset)
            return {};
        if (bytes[offset + 4] == 'u' && bytes[offset + 5] == 'u'
            && bytes[offset + 6] == 'i' && bytes[offset + 7] == 'd') {
            if (box_size < header_size + 16) return {};
            if (!std::equal(
                    std::begin(kHighlightUuidUserType),
                    std::end(kHighlightUuidUserType),
                    bytes.begin() + offset + header_size)) {
                offset += static_cast<size_t>(box_size);
                continue;
            }
            payloads.emplace_back(
                bytes.begin() + offset + header_size + 16,
                bytes.begin() + offset + box_size);
        }
        offset += static_cast<size_t>(box_size);
    }
    return payloads;
}

bool HasExactHighlightUuidPayload(
    const std::vector<uint8_t>& bytes,
    const std::string& marker_id,
    int64_t expected_position_100ns) {
    const std::string expected =
        "{\"version\":1,\"markers\":[{\"marker_id\":\""
        + marker_id + "\",\"position_100ns\":"
        + std::to_string(expected_position_100ns) + ",\"label\":\"\"}]}";
    const auto payloads = ReadHighlightUuidPayloads(bytes);
    return std::find(payloads.begin(), payloads.end(), expected)
        != payloads.end();
}

bool HasHighlightUuidMarker(
    const std::vector<uint8_t>& bytes, const std::string& marker_id) {
    const std::string needle = "\"marker_id\":\"" + marker_id + "\"";
    const auto payloads = ReadHighlightUuidPayloads(bytes);
    return std::any_of(payloads.begin(), payloads.end(),
        [&needle](const std::string& payload) {
            return payload.find(needle) != std::string::npos;
        });
}

bool DecodeOneFrame(
    AVFormatContext* input,
    int stream_index,
    AVMediaType type,
    int64_t target_ms) {
    const AVStream* stream = input->streams[stream_index];
    const AVCodec* decoder = avcodec_find_decoder(stream->codecpar->codec_id);
    if (!decoder) return false;
    AVCodecContext* context = avcodec_alloc_context3(decoder);
    if (!context || avcodec_parameters_to_context(context, stream->codecpar) < 0
        || avcodec_open2(context, decoder, nullptr) < 0) {
        avcodec_free_context(&context);
        return false;
    }
    const int64_t timestamp = av_rescale_q(
        target_ms, AVRational{1, 1000}, stream->time_base);
    if (type == AVMEDIA_TYPE_AUDIO && stream->codecpar->sample_rate <= 0) {
        avcodec_free_context(&context);
        return false;
    }
    const int64_t frame_step = type == AVMEDIA_TYPE_VIDEO
        ? av_rescale_q(1, AVRational{1, kFps}, stream->time_base)
        : av_rescale_q(
            1024,
            AVRational{1, stream->codecpar->sample_rate},
            stream->time_base);
    if (frame_step <= 0) {
        avcodec_free_context(&context);
        return false;
    }
    const bool seek_ok = avformat_seek_file(
        input, stream_index, std::numeric_limits<int64_t>::min(), timestamp,
        std::numeric_limits<int64_t>::max(), AVSEEK_FLAG_BACKWARD) >= 0;
    if (!seek_ok) {
        avcodec_free_context(&context);
        return false;
    }
    avcodec_flush_buffers(context);
    AVPacket* packet = av_packet_alloc();
    AVFrame* frame = av_frame_alloc();
    bool decoded = packet && frame;
    auto receive_until_target = [&]() -> int {
        while (true) {
            const int result = avcodec_receive_frame(context, frame);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return 0;
            if (result < 0) return -1;
            const bool valid_frame = (type == AVMEDIA_TYPE_VIDEO
                    && frame->width > 0 && frame->height > 0)
                || (type == AVMEDIA_TYPE_AUDIO && frame->nb_samples > 0);
            if (!valid_frame || frame->best_effort_timestamp == AV_NOPTS_VALUE)
                continue;
            if (frame->best_effort_timestamp < timestamp) continue;
            if (frame->best_effort_timestamp > timestamp + frame_step) return -1;
            return 1;
        }
    };
    while (decoded && av_read_frame(input, packet) >= 0) {
        if (packet->stream_index == stream_index) {
            decoded = avcodec_send_packet(context, packet) >= 0;
            av_packet_unref(packet);
            if (decoded) {
                const int result = receive_until_target();
                if (result > 0) {
                    av_frame_free(&frame);
                    av_packet_free(&packet);
                    avcodec_free_context(&context);
                    return true;
                }
                if (result < 0) decoded = false;
            }
        } else {
            av_packet_unref(packet);
        }
    }
    if (decoded && avcodec_send_packet(context, nullptr) >= 0
        && receive_until_target() > 0) {
        av_frame_free(&frame);
        av_packet_free(&packet);
        avcodec_free_context(&context);
        return true;
    }
    av_frame_free(&frame);
    av_packet_free(&packet);
    avcodec_free_context(&context);
    return false;
}

bool DecodeAndSeekRecording(
    const std::filesystem::path& path, bool expect_audio) {
    AVFormatContext* input = nullptr;
    if (avformat_open_input(&input, path.string().c_str(), nullptr, nullptr) < 0)
        return false;
    if (avformat_find_stream_info(input, nullptr) < 0) {
        avformat_close_input(&input);
        return false;
    }
    const int video_index = av_find_best_stream(
        input, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    const int audio_index = av_find_best_stream(
        input, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    const bool decoded_video = video_index >= 0
        && DecodeOneFrame(input, video_index, AVMEDIA_TYPE_VIDEO, 3'500)
        && DecodeOneFrame(input, video_index, AVMEDIA_TYPE_VIDEO, 1'500)
        && DecodeOneFrame(input, video_index, AVMEDIA_TYPE_VIDEO, 4'500);
    const bool decoded_audio = !expect_audio || (audio_index >= 0
        && DecodeOneFrame(input, audio_index, AVMEDIA_TYPE_AUDIO, 1'000)
        && DecodeOneFrame(input, audio_index, AVMEDIA_TYPE_AUDIO, 8'500));
    avformat_close_input(&input);
    return decoded_video && decoded_audio;
}

void NativeHighlightSerializerContract() {
    const std::string id_a = "00000000-0000-4000-8000-000000000001";
    const std::string id_b = "00000000-0000-4000-8000-000000000002";
    std::vector<fthr::HighlightMarker> markers{
        {id_b, 20'000'000, {}},
        {id_a, 10'000'000, {}}};
    std::string payload;
    CheckRecording(fthr::SerializeHighlightMarkers(markers, payload),
        "native marker serializer accepts empty labels and exact positions");
    CheckRecording(payload.find("\"position_100ns\":10000000")
            < payload.find("\"position_100ns\":20000000"),
        "native marker serializer preserves exact integer positions in order");
    markers.push_back({id_a, 30'000'000, {}});
    CheckRecording(!fthr::SerializeHighlightMarkers(markers, payload),
        "native marker serializer rejects duplicate UUIDs independent of position");
    markers.pop_back();
    markers[0].label = "native-label-must-stay-in-python";
    CheckRecording(!fthr::SerializeHighlightMarkers(markers, payload),
        "native marker serializer rejects nonempty native labels");
}

void HighlightUuidParserRequiresTheOwnedUserType() {
    const std::string payload =
        "{\"version\":1,\"markers\":[{\"marker_id\":\""
        "00000000-0000-4000-8000-000000000001"
        "\",\"position_100ns\":0,\"label\":\"\"}]}";
    const uint32_t box_size = static_cast<uint32_t>(24 + payload.size());
    std::vector<uint8_t> foreign_box{
        static_cast<uint8_t>(box_size >> 24),
        static_cast<uint8_t>(box_size >> 16),
        static_cast<uint8_t>(box_size >> 8),
        static_cast<uint8_t>(box_size), 'u', 'u', 'i', 'd'};
    foreign_box.insert(foreign_box.end(), 16, 0);
    foreign_box.insert(foreign_box.end(), payload.begin(), payload.end());
    CheckRecording(ReadHighlightUuidPayloads(foreign_box).empty(),
        "foreign UUID user type does not satisfy the owned marker parser");
    std::vector<uint8_t> malformed_box{0, 0, 0, 23, 'u', 'u', 'i', 'd'};
    CheckRecording(ReadHighlightUuidPayloads(malformed_box).empty(),
        "malformed complete UUID boxes are rejected by the marker parser");
}

using OwnedEnvironmentValue = std::unique_ptr<char, decltype(&std::free)>;

OwnedEnvironmentValue ReadNativeArtifactDirectory() noexcept {
    char* raw = nullptr;
    size_t length = 0;
    const auto status = _dupenv_s(&raw, &length, "FTHR_NATIVE_ARTIFACT_DIR");
    OwnedEnvironmentValue directory(raw, &std::free);
    // Do not mistake a read/allocation error for an absent preservation request:
    // that could allow cleanup to delete an artifact the operator wanted kept.
    CheckRecording(status == 0, "native artifact environment value can be read");
    CheckRecording((!directory && length == 0)
            || (directory && length > 0 && directory.get()[length - 1] == '\0'),
        "native artifact environment value has a complete terminated length");
    return directory;
}

bool PreserveNativeArtifacts() noexcept {
    const auto owned_directory = ReadNativeArtifactDirectory();
    const char* directory = owned_directory.get();
    return directory && directory[0] != '\0';
}

void CleanupRecordingArtifact(const std::filesystem::path& path) {
    if (PreserveNativeArtifacts()) {
        std::error_code absolute_error;
        const auto absolute_path = std::filesystem::absolute(
            path, absolute_error);
        std::cout << "Preserving native artifact: "
                  << (absolute_error ? path : absolute_path).string()
                  << std::endl;
        return;
    }
    std::error_code ignored;
    std::filesystem::remove(path, ignored);
}

std::filesystem::path MakeRecordingPath(const char* name) {
    const auto stamp = std::chrono::high_resolution_clock::now()
        .time_since_epoch().count();
    const std::string filename = std::string("fthr-") + name + "-"
        + std::to_string(stamp) + ".mp4";
    const auto owned_directory = ReadNativeArtifactDirectory();
    const char* directory = owned_directory.get();
    if (directory && directory[0] != '\0') {
        std::filesystem::path artifact_directory(directory);
        std::error_code error;
        std::filesystem::create_directories(artifact_directory, error);
        CheckRecording(!error, "native artifact directory is available");
        return artifact_directory / filename;
    }
    return std::filesystem::temp_directory_path() / filename;
}

void FragmentedRecordingSurvivesAnUnfinishedCopy() {
    const AVCodec* codec = avcodec_find_encoder_by_name("libopenh264");
    CheckRecording(codec != nullptr, "bundled OpenH264 encoder is available");

    AVCodecContext* encoder = avcodec_alloc_context3(codec);
    CheckRecording(encoder != nullptr, "test encoder context allocated");
    encoder->width = 160;
    encoder->height = 90;
    encoder->pix_fmt = AV_PIX_FMT_YUV420P;
    encoder->time_base = {1, kFps};
    encoder->framerate = {kFps, 1};
    encoder->gop_size = kFps;
    encoder->max_b_frames = 0;
    encoder->bit_rate = 300'000;
    encoder->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    CheckRecording(avcodec_open2(encoder, codec, nullptr) >= 0,
        "test H.264 encoder opened");

    fthr::EncodedVideoConfig config;
    config.codec = fthr::VideoCodec::H264;
    config.width = static_cast<uint32_t>(encoder->width);
    config.height = static_cast<uint32_t>(encoder->height);
    config.frame_rate = {kFps, 1};
    config.time_base = {1, kFps};
    config.bitrate_kbps = 300;
    config.max_keyframe_interval_frames = kFps;
    config.packet_format = fthr::EncodedPacketFormat::LengthPrefixedNalUnits;
    config.codec_extradata.assign(
        encoder->extradata, encoder->extradata + encoder->extradata_size);
    CheckRecording(!config.codec_extradata.empty(),
        "test H.264 decoder configuration is available");

    const auto output = MakeRecordingPath("recording-complete");
    const auto interrupted = MakeRecordingPath("recording-interrupted");
    fthr::ContinuousRecordingWriter writer;
    fthr::AudioEncoder audio_encoder;
    bool audio_packets_accepted = true;
    CheckRecording(audio_encoder.Initialize(
        48'000, 2, 128,
        [&writer, &audio_packets_accepted](
            const uint8_t* data, uint32_t size, int64_t pts) {
            if (!writer.PushAudio(data, size, pts, 1024))
                audio_packets_accepted = false;
        }), "test AAC encoder starts");
    fthr::ContinuousRecordingAudioConfig audio_config;
    audio_config.sample_rate = audio_encoder.GetSampleRate();
    audio_config.channels = audio_encoder.GetChannels();
    audio_config.codec_extradata = audio_encoder.GetExtradata();
    CheckRecording(audio_config.valid(),
        "test AAC decoder configuration is available");
    CheckRecording(writer.Start(output, config, audio_config),
        "fragmented recording writer starts");
    const std::string marker_id = "00000000-0000-4000-8000-000000000001";
    const std::string second_marker_id =
        "00000000-0000-4000-8000-000000000002";
    CheckRecording(writer.PushHighlight(marker_id, 90),
        "first marker queued before its same-PTS video packet");
    CheckRecording(writer.PushHighlight(second_marker_id, 210),
        "second marker queued before its same-PTS video packet");

    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    CheckRecording(frame && packet, "test frame and packet allocated");
    frame->format = encoder->pix_fmt;
    frame->width = encoder->width;
    frame->height = encoder->height;
    CheckRecording(av_frame_get_buffer(frame, 32) >= 0,
        "test frame storage allocated");
    std::vector<float> audio_samples(1600 * 2, 0.0f);

    // Twelve seconds closes eleven independent one-second fragments while
    // still running quickly in the native release suite.
    for (int index = 0; index < 12 * kFps; ++index) {
        CheckRecording(av_frame_make_writable(frame) >= 0,
            "test frame remains writable");
        for (int row = 0; row < frame->height; ++row) {
            std::fill_n(
                frame->data[0] + row * frame->linesize[0],
                frame->width,
                static_cast<uint8_t>(16 + (index % 180)));
        }
        for (int row = 0; row < frame->height / 2; ++row) {
            std::fill_n(
                frame->data[1] + row * frame->linesize[1],
                frame->width / 2, static_cast<uint8_t>(128));
            std::fill_n(
                frame->data[2] + row * frame->linesize[2],
                frame->width / 2, static_cast<uint8_t>(128));
        }
        // Submit the normal CFR encoder timeline. Real capture timing can be
        // sparse; the UI finalizer repairs those gaps after the source-preserving
        // writer closes, while this native test verifies the writer's ordinary
        // configured-rate path.
        frame->pts = index;
        CheckRecording(avcodec_send_frame(encoder, frame) >= 0,
            "test frame submitted");
        CheckRecording(ReceivePackets(encoder, packet, writer),
            "encoded packet accepted by recording writer");
        audio_encoder.EncodeSamples(
            audio_samples.data(), static_cast<uint32_t>(audio_samples.size()));
    }
    CheckRecording(avcodec_send_frame(encoder, nullptr) >= 0,
        "test encoder flushed");
    CheckRecording(ReceivePackets(encoder, packet, writer),
        "flushed packets accepted by recording writer");
    audio_encoder.Finalize();
    CheckRecording(audio_packets_accepted,
        "AAC packets are accepted by recording writer");

    // Copy the still-open file after the worker crosses the final keyframe.
    // The copy has no trailer and represents sudden process or power loss.
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    {
        std::ifstream source(output, std::ios::binary);
        std::ofstream destination(interrupted, std::ios::binary);
        destination << source.rdbuf();
        CheckRecording(source.good() || source.eof(),
            "active fragmented recording can be copied for crash simulation");
        CheckRecording(destination.good(),
            "unfinished recording copy is written");
    }

    CheckRecording(writer.Stop(), "normal recording close succeeds");
    CheckRecording(ProbeMedia(output, true, kFps, 300'000),
        "normally closed video plus AAC recording is playable");
    CheckRecording(ProbeMedia(interrupted, true, kFps, 300'000),
        "video plus AAC recording copied before Stop is independently playable");
    CheckRecording(DecodeAndSeekRecording(output, true),
        "normally closed video and AAC streams decode after two seeks");
    CheckRecording(DecodeAndSeekRecording(interrupted, true),
        "interrupted video and AAC streams decode after two seeks");

    std::ifstream file(output, std::ios::binary);
    const std::vector<uint8_t> bytes{
        std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    const char moov[4] = {'m', 'o', 'o', 'v'};
    const char moof[4] = {'m', 'o', 'o', 'f'};
    CheckRecording(CountBox(bytes, moov) >= 1,
        "recovery metadata is present at recording start");
    CheckRecording(CountBox(bytes, moof) >= 2,
        "recording contains multiple independent media fragments");
    CheckRecording(HasBtrtBitrate(bytes, 300'000),
        "MP4 track bitrate declaration matches the configured video bitrate");
    CheckRecording(HasExactHighlightUuidPayload(bytes, marker_id, 30'000'000),
        "recording contains the first bounded highlight uuid payload");
    CheckRecording(HasExactHighlightUuidPayload(
            bytes, second_marker_id, 70'000'000),
        "recording contains the second bounded highlight uuid payload");
    {
        std::ifstream interrupted_file(interrupted, std::ios::binary);
        const std::vector<uint8_t> interrupted_bytes{
            std::istreambuf_iterator<char>(interrupted_file),
            std::istreambuf_iterator<char>()};
        CheckRecording(HasExactHighlightUuidPayload(
                interrupted_bytes, marker_id, 30'000'000),
            "interrupted copy contains a bounded first marker payload");
        CheckRecording(HasExactHighlightUuidPayload(
                interrupted_bytes, second_marker_id, 70'000'000),
            "interrupted copy contains a bounded second marker payload");
    }

    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&encoder);
    CleanupRecordingArtifact(output);
    CleanupRecordingArtifact(interrupted);
}

void NoAudioRecordingDecodesAndSeeks() {
    const AVCodec* codec = avcodec_find_encoder_by_name("libopenh264");
    CheckRecording(codec != nullptr, "software H.264 encoder is available");
    AVCodecContext* encoder = avcodec_alloc_context3(codec);
    CheckRecording(encoder != nullptr, "no-audio encoder context allocated");
    encoder->width = 160;
    encoder->height = 90;
    encoder->pix_fmt = AV_PIX_FMT_YUV420P;
    encoder->time_base = {1, kFps};
    encoder->framerate = {kFps, 1};
    encoder->gop_size = kFps;
    encoder->max_b_frames = 0;
    encoder->bit_rate = 300'000;
    encoder->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    CheckRecording(avcodec_open2(encoder, codec, nullptr) >= 0,
        "no-audio encoder opened");

    fthr::EncodedVideoConfig config;
    config.codec = fthr::VideoCodec::H264;
    config.width = static_cast<uint32_t>(encoder->width);
    config.height = static_cast<uint32_t>(encoder->height);
    config.frame_rate = {kFps, 1};
    config.time_base = {1, kFps};
    config.bitrate_kbps = 300;
    config.max_keyframe_interval_frames = kFps;
    config.packet_format = fthr::EncodedPacketFormat::LengthPrefixedNalUnits;
    config.codec_extradata.assign(
        encoder->extradata, encoder->extradata + encoder->extradata_size);
    CheckRecording(!config.codec_extradata.empty(),
        "no-audio decoder configuration is available");

    const auto output = MakeRecordingPath("recording-no-audio");
    fthr::ContinuousRecordingWriter writer;
    constexpr uint64_t generation = 7;
    CheckRecording(writer.Start(output, config, {}, generation),
        "no-audio fragmented writer starts");
    const std::string first_marker =
        "00000000-0000-4000-8000-000000000003";
    const std::string second_marker =
        "00000000-0000-4000-8000-000000000004";
    const std::string before_origin_marker =
        "00000000-0000-4000-8000-000000000005";
    const std::string origin_marker =
        "00000000-0000-4000-8000-000000000006";
    const std::string last_marker =
        "00000000-0000-4000-8000-000000000007";
    const std::string future_marker =
        "00000000-0000-4000-8000-000000000008";
    CheckRecording(!writer.PushHighlight(first_marker, 30, 0),
        "bound writer rejects the legacy zero generation");
    CheckRecording(!writer.PushHighlight(first_marker, 30, generation - 1),
        "bound writer rejects an old generation");
    CheckRecording(writer.PushHighlight(first_marker, 60, generation),
        "no-audio first marker accepted");
    CheckRecording(writer.PushHighlight(second_marker, 150, generation),
        "no-audio second marker accepted");
    CheckRecording(writer.PushHighlight(
            before_origin_marker, 29, generation),
        "no-audio before-origin marker accepted");
    CheckRecording(writer.PushHighlight(origin_marker, 30, generation),
        "no-audio origin marker accepted");
    CheckRecording(writer.PushHighlight(last_marker, 210, generation),
        "no-audio last marker accepted");
    CheckRecording(writer.PushHighlight(future_marker, 211, generation),
        "no-audio future marker accepted");

    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    CheckRecording(frame && packet, "no-audio frame and packet allocated");
    std::vector<uint8_t> keyframe_sample;
    std::vector<EncodedPacketSample> encoded_packet_cache;
    bool accepted_first_keyframe = false;
    frame->format = encoder->pix_fmt;
    frame->width = encoder->width;
    frame->height = encoder->height;
    CheckRecording(av_frame_get_buffer(frame, 32) >= 0,
        "no-audio frame storage allocated");
    // Encode a nonzero source timeline and intentionally discard packets up to
    // the first accepted keyframe at PTS 30. Marker positions must use that
    // accepted keyframe as their origin, never the discarded source frames.
    for (int index = 0; index < 7 * kFps; ++index) {
        CheckRecording(av_frame_make_writable(frame) >= 0,
            "no-audio frame remains writable");
        for (int row = 0; row < frame->height; ++row)
            std::fill_n(
                frame->data[0] + row * frame->linesize[0], frame->width,
                static_cast<uint8_t>(16 + (index % 180)));
        for (int row = 0; row < frame->height / 2; ++row) {
            std::fill_n(
                frame->data[1] + row * frame->linesize[1], frame->width / 2,
                static_cast<uint8_t>(128));
            std::fill_n(
                frame->data[2] + row * frame->linesize[2], frame->width / 2,
                static_cast<uint8_t>(128));
        }
        frame->pts = index;
        CheckRecording(avcodec_send_frame(encoder, frame) >= 0,
            "no-audio frame submitted");
        CheckRecording(ReceivePackets(
                encoder, packet, writer, generation, &keyframe_sample, 30,
                &accepted_first_keyframe, &encoded_packet_cache),
            "no-audio encoded packet accepted");
    }
    CheckRecording(avcodec_send_frame(encoder, nullptr) >= 0,
        "no-audio encoder flushed");
    CheckRecording(ReceivePackets(
            encoder, packet, writer, generation, &keyframe_sample, 30,
            &accepted_first_keyframe, &encoded_packet_cache),
        "no-audio flushed packet accepted");
    CheckRecording(accepted_first_keyframe,
        "the nonzero first accepted keyframe establishes the origin");
    CheckRecording(!keyframe_sample.empty(),
        "a valid encoded keyframe sample is available for generation checks");
    CheckRecording(!writer.PushVideo(
            keyframe_sample.data(), static_cast<uint32_t>(keyframe_sample.size()),
            240, true, 0),
        "bound writer rejects a zero-generation video packet");
    CheckRecording(!writer.PushVideo(
            keyframe_sample.data(), static_cast<uint32_t>(keyframe_sample.size()),
            240, true, generation - 1),
        "bound writer rejects an old-generation video packet");
    CheckRecording(writer.PushVideo(
            keyframe_sample.data(), static_cast<uint32_t>(keyframe_sample.size()),
            210, true, generation),
        "bound writer accepts a current-generation video packet");
    CheckRecording(!writer.HasFailed(),
        "generation rejection leaves the media writer healthy");
    CheckRecording(writer.Stop(), "no-audio recording close succeeds");
    CheckRecording(ProbeMedia(output, false, kFps, 300'000),
        "no-audio recording is playable");
    CheckRecording(DecodeAndSeekRecording(output, false),
        "no-audio recording decodes after two seeks");
    std::ifstream file(output, std::ios::binary);
    const std::vector<uint8_t> bytes{
        std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    CheckRecording(HasExactHighlightUuidPayload(bytes, first_marker, 10'000'000),
        "no-audio first marker has an exact bounded payload");
    CheckRecording(HasExactHighlightUuidPayload(
            bytes, second_marker, 40'000'000),
        "no-audio second marker has an exact bounded payload");
    CheckRecording(!HasHighlightUuidMarker(bytes, before_origin_marker),
        "marker before the accepted origin is omitted");
    CheckRecording(HasExactHighlightUuidPayload(bytes, origin_marker, 0),
        "marker at the accepted origin has exact zero position");
    CheckRecording(HasExactHighlightUuidPayload(
            bytes, last_marker, 60'000'000),
        "marker at the last accepted packet has exact bounded position");
    CheckRecording(!HasHighlightUuidMarker(bytes, future_marker),
        "marker after the accepted media is omitted");

    CheckRecording(!keyframe_sample.empty(),
        "a valid encoded keyframe sample is available for lifecycle checks");
    CheckRecording(encoded_packet_cache.size() >= 151,
        "real accepted packets cover the quota CFR timeline");
    const int64_t quota_origin_pts = encoded_packet_cache.front().pts;
    CheckRecording(quota_origin_pts == 30,
        "quota cache starts at the real accepted PTS 30");
    for (size_t index = 1; index <= 150; ++index)
        CheckRecording(
            encoded_packet_cache[index].pts
                == quota_origin_pts + static_cast<int64_t>(index),
            "cached accepted packets retain a continuous CFR PTS sequence");
    auto push_cached_packet =
        [&writer, &encoded_packet_cache, quota_origin_pts](
            size_t index, uint64_t write_generation, const char* message) {
            const auto& sample = encoded_packet_cache[index];
            CheckRecording(writer.PushVideo(
                    sample.data.data(), static_cast<uint32_t>(sample.data.size()),
                    sample.pts - quota_origin_pts, sample.is_keyframe,
                    write_generation), message);
        };
    const auto quota_output = MakeRecordingPath("recording-marker-quota");
    CheckRecording(writer.Start(quota_output, config, {}, generation + 1),
        "stopped writer restarts with a fresh generation");
    push_cached_packet(0, generation + 1,
        "fresh generation accepts the real first cached packet");
    auto quota_marker = [](uint32_t index) {
        char value[64]{};
        std::snprintf(
            value, sizeof(value), "00000000-0000-4000-8000-%012x", index);
        return std::string(value);
    };
    for (uint32_t index = 0; index < fthr::kMaxHighlightMarkers; ++index)
        CheckRecording(writer.PushHighlight(
                quota_marker(index), 0, generation + 1),
            "distinct marker remains within the lifetime quota");
    for (size_t index = 1; index <= 30; ++index)
        push_cached_packet(index, generation + 1,
            "real cached packet commits the full marker batch");
    const auto quota_payload_deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(2);
    bool quota_payload_seen = false;
    std::vector<std::string> captured_quota_payloads;
    while (std::chrono::steady_clock::now() < quota_payload_deadline) {
        std::ifstream quota_file(quota_output, std::ios::binary);
        const std::vector<uint8_t> quota_bytes{
            std::istreambuf_iterator<char>(quota_file),
            std::istreambuf_iterator<char>()};
        const auto payloads = ReadHighlightUuidPayloads(quota_bytes);
        for (const auto& payload : payloads) {
            size_t marker_count = 0;
            size_t marker_offset = 0;
            while ((marker_offset = payload.find(
                        "\"marker_id\":", marker_offset))
                    != std::string::npos) {
                ++marker_count;
                marker_offset += 12;
            }
            if (marker_count != fthr::kMaxHighlightMarkers) continue;
            bool all_ids_present = true;
            for (uint32_t index = 0;
                    index < fthr::kMaxHighlightMarkers; ++index) {
                if (payload.find(quota_marker(index)) == std::string::npos) {
                    all_ids_present = false;
                    break;
                }
            }
            quota_payload_seen = all_ids_present;
            if (quota_payload_seen)
                captured_quota_payloads = payloads;
            if (quota_payload_seen) break;
        }
        if (quota_payload_seen) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    CheckRecording(quota_payload_seen,
        "written marker batch contains all 4096 admitted UUIDs");
    CheckRecording(captured_quota_payloads.size() == 1,
        "the complete 4096-marker batch is captured as one owned payload");
    CheckRecording(writer.PushHighlight(
            quota_marker(0), 123, generation + 1),
        "duplicate marker remains idempotent without a new slot");
    CheckRecording(!writer.PushHighlight(
            quota_marker(fthr::kMaxHighlightMarkers), 0, generation + 1),
        "4097th distinct marker is refused nonfatally");
    for (size_t index = 31; index <= 150; ++index)
        push_cached_packet(index, generation + 1,
            "post-refusal media remains on the continuous CFR timeline");
    CheckRecording(writer.Stop(),
        "media remains writable and stoppable after marker quota refusal");
    CheckRecording(!writer.HasFailed(),
        "marker quota refusal does not fail the media writer");
    CheckRecording(ProbeMedia(quota_output, false, kFps, 300'000),
        "quota recording remains playable after refusal");
    CheckRecording(DecodeAndSeekRecording(quota_output, false),
        "quota recording decodes after refusal");
    {
        std::ifstream quota_file(quota_output, std::ios::binary);
        const std::vector<uint8_t> quota_bytes{
            std::istreambuf_iterator<char>(quota_file),
            std::istreambuf_iterator<char>()};
        CheckRecording(
            ReadHighlightUuidPayloads(quota_bytes) == captured_quota_payloads,
            "duplicate and quota refusal preserve the exact original UUID payload");
    }

    const auto reset_output = MakeRecordingPath("recording-marker-reset");
    CheckRecording(writer.Start(reset_output, config, {}, generation + 2),
        "stopped writer starts a third fresh generation");
    push_cached_packet(0, generation + 2,
        "third generation accepts the real first cached packet");
    CheckRecording(writer.PushHighlight(
            quota_marker(fthr::kMaxHighlightMarkers), 0, generation + 2),
        "previously refused marker is admitted in a new recording");
    for (size_t index = 1; index <= 30; ++index)
        push_cached_packet(index, generation + 2,
            "new generation flushes its first marker");
    CheckRecording(writer.Stop(), "third generation closes successfully");
    std::ifstream reset_file(reset_output, std::ios::binary);
    const std::vector<uint8_t> reset_bytes{
        std::istreambuf_iterator<char>(reset_file),
        std::istreambuf_iterator<char>()};
    CheckRecording(HasExactHighlightUuidPayload(
            reset_bytes, quota_marker(fthr::kMaxHighlightMarkers), 0),
        "reset recording commits the formerly rejected marker at position zero");

    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&encoder);
    CleanupRecordingArtifact(output);
    CleanupRecordingArtifact(quota_output);
    CleanupRecordingArtifact(reset_output);
}

void MissingDecoderConfigurationFailsBeforeCreatingAWriter() {
    fthr::EncodedVideoConfig config;
    config.codec = fthr::VideoCodec::H264;
    config.width = 1920;
    config.height = 1080;
    config.frame_rate = {60, 1};
    config.time_base = {1, 60};
    config.packet_format = fthr::EncodedPacketFormat::LengthPrefixedNalUnits;

    fthr::ContinuousRecordingWriter writer;
    const auto output = MakeRecordingPath("recording-invalid");
    CheckRecording(!writer.Start(output, config),
        "writer refuses a stream with no decoder configuration");
    CheckRecording(!std::filesystem::exists(output),
        "invalid start never creates a misleading recording file");
}

} // namespace

int RunContinuousRecordingWriterTests() {
    NativeHighlightSerializerContract();
    HighlightUuidParserRequiresTheOwnedUserType();
    FragmentedRecordingSurvivesAnUnfinishedCopy();
    NoAudioRecordingDecodesAndSeeks();
    MissingDecoderConfigurationFailsBeforeCreatingAWriter();
    std::cout << "Continuous recording writer tests: " << recording_checks
              << " checks passed" << std::endl;
    return recording_checks;
}
