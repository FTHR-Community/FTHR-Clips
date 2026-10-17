#include "software_replay_encoder.h"
#include <windows.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}
#include <algorithm>
#include <cstring>
#include <utility>

namespace fthr {
namespace {
using NalSpan = std::pair<const uint8_t*, int>;

void ParseAnnexBNals(
    const uint8_t* data,
    int size,
    std::vector<NalSpan>& nals) {
    nals.clear();
    if (!data || size <= 0) return;

    int cursor = 0;
    while (cursor < size) {
        int nal_start = -1;
        for (; cursor < size - 2; ++cursor) {
            if (data[cursor] != 0 || data[cursor + 1] != 0) continue;
            if (data[cursor + 2] == 1) {
                nal_start = cursor + 3;
                cursor += 3;
                break;
            }
            if (cursor + 3 < size
                && data[cursor + 2] == 0
                && data[cursor + 3] == 1) {
                nal_start = cursor + 4;
                cursor += 4;
                break;
            }
        }
        if (nal_start < 0) break;

        int nal_end = size;
        for (int index = nal_start; index < size - 2; ++index) {
            if (data[index] == 0
                && data[index + 1] == 0
                && (data[index + 2] == 1
                    || (index + 3 < size
                        && data[index + 2] == 0
                        && data[index + 3] == 1))) {
                nal_end = index;
                break;
            }
        }
        if (nal_end > nal_start) {
            nals.push_back({data + nal_start, nal_end - nal_start});
        }
        cursor = nal_end;
    }
}

bool ConvertAnnexBPacketToAvcc(
    const uint8_t* data,
    int size,
    std::vector<NalSpan>& nals,
    std::vector<uint8_t>& output) {
    ParseAnnexBNals(data, size, nals);
    if (nals.empty()) return false;

    size_t output_size = 0;
    for (const auto& nal : nals) {
        if (nal.second <= 0) continue;
        const uint8_t nal_type = nal.first[0] & 0x1F;
        if (nal_type == 7 || nal_type == 8) continue;
        output_size += 4 + static_cast<size_t>(nal.second);
    }

    output.resize(output_size);
    uint8_t* destination = output.data();
    for (const auto& nal : nals) {
        if (nal.second <= 0) continue;
        const uint8_t nal_type = nal.first[0] & 0x1F;
        if (nal_type == 7 || nal_type == 8) continue;
        const uint32_t nal_size = static_cast<uint32_t>(nal.second);
        destination[0] = static_cast<uint8_t>(nal_size >> 24);
        destination[1] = static_cast<uint8_t>(nal_size >> 16);
        destination[2] = static_cast<uint8_t>(nal_size >> 8);
        destination[3] = static_cast<uint8_t>(nal_size);
        std::memcpy(destination + 4, nal.first, nal_size);
        destination += 4 + nal_size;
    }
    return true;
}

std::vector<uint8_t> BuildAvccExtradata(
    const uint8_t* data,
    int size,
    std::vector<NalSpan>& nals) {
    if (!data || size <= 0) return {};
    if (size >= 7 && data[0] == 1) {
        return {data, data + size};
    }

    ParseAnnexBNals(data, size, nals);
    const uint8_t* sps = nullptr;
    const uint8_t* pps = nullptr;
    int sps_size = 0;
    int pps_size = 0;
    for (const auto& nal : nals) {
        if (nal.second <= 0) continue;
        const uint8_t nal_type = nal.first[0] & 0x1F;
        if (nal_type == 7 && !sps) {
            sps = nal.first;
            sps_size = nal.second;
        } else if (nal_type == 8 && !pps) {
            pps = nal.first;
            pps_size = nal.second;
        }
    }
    if (!sps || sps_size < 4 || !pps || pps_size < 1) return {};

    std::vector<uint8_t> output;
    output.reserve(static_cast<size_t>(sps_size + pps_size + 11));
    output.push_back(1);
    output.push_back(sps[1]);
    output.push_back(sps[2]);
    output.push_back(sps[3]);
    output.push_back(0xFF);
    output.push_back(0xE1);
    output.push_back(static_cast<uint8_t>(sps_size >> 8));
    output.push_back(static_cast<uint8_t>(sps_size));
    output.insert(output.end(), sps, sps + sps_size);
    output.push_back(1);
    output.push_back(static_cast<uint8_t>(pps_size >> 8));
    output.push_back(static_cast<uint8_t>(pps_size));
    output.insert(output.end(), pps, pps + pps_size);
    return output;
}

}

bool SoftwareReplayEncoder::Fail(const char* operation, int result) {
    char detail[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(result, detail, sizeof(detail));
    error_ = std::string(operation) + ": " + detail;
    return false;
}

bool SoftwareReplayEncoder::Initialize(
    const EncoderConfig& config, ID3D11Device*, ID3D11DeviceContext*,
    PacketCallback callback, bool) {
    Shutdown();
    error_.clear();
    if (!callback || !config.fps || !config.bitrate_kbps
        || !BuildCaptureScaleGeometry(config.src_width, config.src_height,
            config.enc_width, config.enc_height, config.scaling_mode == 1, geometry_)) {
        error_ = "invalid software encoder configuration";
        return false;
    }
    const AVCodec* encoder = avcodec_find_encoder_by_name("libopenh264");
    if (!encoder) { error_ = "libopenh264 not found in this build"; return false; }
    codec_ = avcodec_alloc_context3(encoder);
    frame_ = av_frame_alloc();
    packet_ = av_packet_alloc();
    if (!codec_ || !frame_ || !packet_) return Fail("allocate software encoder", AVERROR(ENOMEM));
    codec_->width = geometry_.target_width;
    codec_->height = geometry_.target_height;
    codec_->pix_fmt = AV_PIX_FMT_YUV420P;
    codec_->time_base = {1, static_cast<int>(config.fps)};
    codec_->framerate = {static_cast<int>(config.fps), 1};
    codec_->bit_rate = static_cast<int64_t>(config.bitrate_kbps) * 1000;
    codec_->gop_size = static_cast<int>(config.fps * 2);
    codec_->max_b_frames = 0;
    codec_->thread_count = std::min(8u, std::max(1u, std::thread::hardware_concurrency()));
    codec_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    codec_->color_range = AVCOL_RANGE_MPEG;
    codec_->colorspace = AVCOL_SPC_BT709;
    codec_->color_primaries = AVCOL_PRI_BT709;
    codec_->color_trc = AVCOL_TRC_BT709;
    av_opt_set(codec_->priv_data, "rc_mode", "bitrate", 0);
    av_opt_set(codec_->priv_data, "allow_skip_frames", "0", 0);
    int result = avcodec_open2(codec_, encoder, nullptr);
    if (result < 0) return Fail("open libopenh264", result);
    frame_->format = codec_->pix_fmt;
    frame_->width = codec_->width;
    frame_->height = codec_->height;
    frame_->color_range = codec_->color_range;
    frame_->colorspace = codec_->colorspace;
    frame_->color_primaries = codec_->color_primaries;
    frame_->color_trc = codec_->color_trc;
    result = av_frame_get_buffer(frame_, 32);
    if (result < 0) return Fail("allocate software frame", result);
    const auto& rect = geometry_.destination;
    scaler_ = sws_getContext(config.src_width, config.src_height, AV_PIX_FMT_BGRA,
        rect.width, rect.height, AV_PIX_FMT_YUV420P, SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!scaler_) return Fail("create software scaler", AVERROR(ENOMEM));
    const int* coefficients = sws_getCoefficients(SWS_CS_ITU709);
    result = sws_setColorspaceDetails(scaler_, coefficients, 1, coefficients, 0,
        0, 1 << 16, 1 << 16);
    if (result < 0) return Fail("configure BT.709 conversion", result);
    video_ = {};
    video_.width = codec_->width;
    video_.height = codec_->height;
    video_.frame_rate = {static_cast<int32_t>(config.fps), 1};
    video_.time_base = {1, static_cast<int32_t>(config.fps)};
    video_.bitrate_kbps = config.bitrate_kbps;
    video_.max_keyframe_interval_frames = codec_->gop_size;
    std::vector<NalSpan> nals;
    video_.codec_extradata = BuildAvccExtradata(codec_->extradata, codec_->extradata_size, nals);
    if (video_.codec_extradata.empty()) {
        error_ = "libopenh264 did not publish H.264 decoder configuration";
        return false;
    }
    callback_ = std::move(callback);
    LARGE_INTEGER frequency{};
    QueryPerformanceFrequency(&frequency);
    frequency_ = frequency.QuadPart;
    initialized_ = true;
    return true;
}

bool SoftwareReplayEncoder::EncodeFrameCPU(
    const uint8_t* bgra, uint32_t stride, int64_t present_qpc) {
    if (!initialized_ || !bgra || stride < geometry_.source_width * 4) return false;
    int result = av_frame_make_writable(frame_);
    if (result < 0) return Fail("make software frame writable", result);
    // Clear the whole target before scaling into an even, centered FIT rectangle.
    const ptrdiff_t lines[4] = {frame_->linesize[0], frame_->linesize[1], frame_->linesize[2], 0};
    result = av_image_fill_black(frame_->data, lines, AV_PIX_FMT_YUV420P,
        AVCOL_RANGE_MPEG, frame_->width, frame_->height);
    if (result < 0) return Fail("clear software frame", result);
    const auto& rect = geometry_.destination;
    uint8_t* output[4] = {
        frame_->data[0] + rect.top * frame_->linesize[0] + rect.left,
        frame_->data[1] + (rect.top / 2) * frame_->linesize[1] + rect.left / 2,
        frame_->data[2] + (rect.top / 2) * frame_->linesize[2] + rect.left / 2, nullptr};
    const uint8_t* source[4] = {bgra, nullptr, nullptr, nullptr};
    const int source_lines[4] = {static_cast<int>(stride), 0, 0, 0};
    if (sws_scale(scaler_, source, source_lines, 0, geometry_.source_height,
            output, frame_->linesize) != static_cast<int>(rect.height)) {
        error_ = "software frame conversion failed";
        return false;
    }
    LARGE_INTEGER now{};
    QueryPerformanceCounter(&now);
    const int64_t wall = present_qpc > 0 ? present_qpc : now.QuadPart;
    if (!epoch_) epoch_ = wall;
    int64_t pts = 0;
    if (!first_frame_) {
        const int64_t elapsed = wall - epoch_.load(std::memory_order_relaxed);
        pts = (elapsed * static_cast<int64_t>(video_.frame_rate.numerator))
            / frequency_.load(std::memory_order_relaxed);
        if (pts <= last_pts_) pts = last_pts_ + 1;
    }
    first_frame_ = false;
    last_pts_ = pts;
    frame_->pts = pts;
    frame_->pict_type = ConsumeKeyframeRequest() ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
    result = avcodec_send_frame(codec_, frame_);
    if (result == AVERROR(EAGAIN)) {
        if (!Drain()) return false;
        result = avcodec_send_frame(codec_, frame_);
    }
    if (result < 0) return Fail("submit software frame", result);
    timing_.push_back({pts, wall});
    return Drain();
}

bool SoftwareReplayEncoder::Drain() {
    for (;;) {
        int result = avcodec_receive_packet(codec_, packet_);
        if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
        if (result < 0) return Fail("receive software packet", result);
        std::vector<NalSpan> nals;
        std::vector<uint8_t> normalized;
        if (!ConvertAnnexBPacketToAvcc(packet_->data, packet_->size, nals, normalized)
            || normalized.empty() || timing_.empty()) {
            av_packet_unref(packet_);
            error_ = "invalid software H.264 packet or missing presentation timing";
            return false;
        }
        const auto timing = timing_.front();
        timing_.pop_front();
        callback_(normalized.data(), static_cast<uint32_t>(normalized.size()),
            timing.first, (packet_->flags & AV_PKT_FLAG_KEY) != 0, timing.second);
        av_packet_unref(packet_);
    }
}

bool SoftwareReplayEncoder::GetEncodeEpoch(int64_t& start, int64_t& frequency) const {
    start = epoch_; frequency = frequency_;
    return epoch_ > 0 && frequency_ > 0;
}

void SoftwareReplayEncoder::Shutdown() {
    if (initialized_ && codec_ && callback_) {
        avcodec_send_frame(codec_, nullptr);
        Drain();
    }
    initialized_ = false;
    av_packet_free(&packet_);
    av_frame_free(&frame_);
    avcodec_free_context(&codec_);
    sws_freeContext(scaler_);
    scaler_ = nullptr;
    callback_ = {};
    timing_.clear();
    last_pts_ = -1;
    first_frame_ = true;
    epoch_ = frequency_ = 0;
    video_ = {};
}
}
