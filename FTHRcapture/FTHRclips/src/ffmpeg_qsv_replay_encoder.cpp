#include "ffmpeg_qsv_replay_encoder.h"
#include "capture_scale_geometry.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi.h>

#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable: 4244)
#endif
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavcodec/packet.h>
#include <libavutil/avutil.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_d3d11va.h>
}
#ifdef _MSC_VER
#pragma warning(pop)
#endif

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <iostream>
#include <sstream>
#include <utility>

namespace fthr {
namespace {

using NalSpan = std::pair<const uint8_t*, int>;

constexpr uint32_t kQsvPoolSize = 8;
constexpr uint32_t kQsvAsyncDepth = 4;

const QsvCodecSelection kH264Qsv{
    VideoCodec::H264,
    "h264_qsv",
    EncodedPacketFormat::LengthPrefixedNalUnits,
    AV_PROFILE_H264_MAIN};

const QsvCodecSelection kHevcQsv{
    VideoCodec::HEVC,
    "hevc_qsv",
    EncodedPacketFormat::AnnexBNalUnits,
    AV_PROFILE_HEVC_MAIN};

const QsvCodecSelection kAv1Qsv{
    VideoCodec::AV1,
    "av1_qsv",
    EncodedPacketFormat::LowOverheadObu,
    AV_PROFILE_AV1_MAIN};

std::string AvErrorString(int error_code) {
    char buffer[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(error_code, buffer, sizeof(buffer));
    return buffer;
}

std::string HResultString(const char* operation, HRESULT result) {
    std::ostringstream stream;
    stream << operation << " failed: 0x"
           << std::hex << static_cast<uint32_t>(result);
    return stream.str();
}

bool SameComObject(IUnknown* left, IUnknown* right) noexcept {
    if (!left || !right) return false;
    IUnknown* left_identity = nullptr;
    IUnknown* right_identity = nullptr;
    const HRESULT left_hr = left->QueryInterface(
        __uuidof(IUnknown), reinterpret_cast<void**>(&left_identity));
    const HRESULT right_hr = right->QueryInterface(
        __uuidof(IUnknown), reinterpret_cast<void**>(&right_identity));
    const bool same = SUCCEEDED(left_hr)
        && SUCCEEDED(right_hr)
        && left_identity == right_identity;
    if (left_identity) left_identity->Release();
    if (right_identity) right_identity->Release();
    return same;
}

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

class FfmpegQsvCodecSession final : public detail::IQsvCodecSession {
public:
    ~FfmpegQsvCodecSession() override {
        Reset();
    }

    bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        const QsvCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) override {
        Reset();
        selection_ = &selection;

        if (!shared_device || !shared_context) {
            error = "QSV requires a D3D11 device and immediate context";
            return false;
        }
        if (QueryD3D11DeviceVendor(shared_device) != EncoderVendor::Intel) {
            error = "selected D3D11 device is not Intel; refusing cross-adapter QSV";
            return false;
        }

        ID3D11Device* context_device = nullptr;
        shared_context->GetDevice(&context_device);
        const bool context_matches = SameComObject(shared_device, context_device);
        if (context_device) context_device->Release();
        if (!context_matches) {
            error = "D3D11 immediate context belongs to a different device";
            return false;
        }
        if (!detail::EnableQsvMultithreadProtection(shared_context, error)) {
            return false;
        }

        hdr10_ = config.hdr10;
        source_width_ = config.src_width;
        source_height_ = config.src_height;
        output_width_ = config.enc_width > 0
            ? config.enc_width : config.src_width;
        output_height_ = config.enc_height > 0
            ? config.enc_height : config.src_height;
        if (source_width_ == 0 || source_height_ == 0
            || output_width_ == 0 || output_height_ == 0
            || config.fps == 0 || config.bitrate_kbps == 0
            || (output_width_ & 1U) != 0 || (output_height_ & 1U) != 0) {
            error = "invalid QSV encoder dimensions, frame rate, or bitrate";
            return false;
        }

        const AVCodec* codec = avcodec_find_encoder_by_name(selection.encoder_name);
        if (!codec || !codec->name
            || std::strcmp(codec->name, selection.encoder_name) != 0) {
            error = std::string(selection.encoder_name)
                + " not found in the pinned FFmpeg build";
            return false;
        }

        if (hdr10_ && (selection.codec != VideoCodec::HEVC
            || source_width_ != output_width_ || source_height_ != output_height_)) {
            error = "QSV HDR requires target-sized P010 HEVC input";
            return false;
        }
        if (!CreateHardwareContexts(shared_device, error)
            || !CreateConversionPipeline(
                shared_device, shared_context, config.fps,
                config.scaling_mode == 1, error)) {
            Reset();
            return false;
        }

        codec_context_ = avcodec_alloc_context3(codec);
        if (!codec_context_) {
            error = "could not allocate QSV codec context";
            Reset();
            return false;
        }
        codec_context_->codec_id = codec->id;
        codec_context_->codec_type = AVMEDIA_TYPE_VIDEO;
        codec_context_->width = static_cast<int>(output_width_);
        codec_context_->height = static_cast<int>(output_height_);
        codec_context_->pix_fmt = AV_PIX_FMT_QSV;
        codec_context_->sw_pix_fmt = hdr10_ ? AV_PIX_FMT_P010 : AV_PIX_FMT_NV12;
        codec_context_->color_range = AVCOL_RANGE_MPEG;
        codec_context_->color_primaries = hdr10_ ? AVCOL_PRI_BT2020 : AVCOL_PRI_BT709;
        codec_context_->color_trc = hdr10_ ? AVCOL_TRC_SMPTE2084 : AVCOL_TRC_BT709;
        codec_context_->colorspace = hdr10_ ? AVCOL_SPC_BT2020_NCL : AVCOL_SPC_BT709;
        codec_context_->chroma_sample_location = AVCHROMA_LOC_LEFT;
        codec_context_->time_base = AVRational{1, static_cast<int>(config.fps)};
        codec_context_->framerate = AVRational{static_cast<int>(config.fps), 1};
        codec_context_->bit_rate = static_cast<int64_t>(config.bitrate_kbps) * 1000;
        codec_context_->rc_min_rate = codec_context_->bit_rate;
        codec_context_->rc_max_rate = codec_context_->bit_rate;
        codec_context_->rc_buffer_size = static_cast<int>(codec_context_->bit_rate);
        codec_context_->gop_size = static_cast<int>(config.fps);
        codec_context_->max_b_frames = 0;
        codec_context_->profile = hdr10_ ? AV_PROFILE_HEVC_MAIN_10 : selection.profile;
        codec_context_->flags |= AV_CODEC_FLAG_LOW_DELAY
            | AV_CODEC_FLAG_GLOBAL_HEADER
            | AV_CODEC_FLAG_CLOSED_GOP;
        codec_context_->hw_device_ctx = av_buffer_ref(qsv_device_ctx_);
        codec_context_->hw_frames_ctx = av_buffer_ref(qsv_frames_ctx_);
        if (!codec_context_->hw_device_ctx || !codec_context_->hw_frames_ctx) {
            error = "could not retain FFmpeg QSV hardware contexts";
            Reset();
            return false;
        }

        AVDictionary* options = nullptr;
        av_dict_set_int(&options, "async_depth", kQsvAsyncDepth, 0);
        av_dict_set(&options, "preset", "veryfast", 0);
        av_dict_set(&options, "forced_idr", "1", 0);
        av_dict_set(&options, "look_ahead_depth", "0", 0);
        int result = avcodec_open2(codec_context_, codec, &options);
        if (result < 0) {
            error = std::string("could not open ") + selection.encoder_name
                + " through oneVPL/QSV: " + AvErrorString(result);
            av_dict_free(&options);
            Reset();
            return false;
        }
        if (options) {
            const AVDictionaryEntry* option = av_dict_get(
                options, "", nullptr, AV_DICT_IGNORE_SUFFIX);
            error = std::string("pinned ") + selection.encoder_name
                + " did not accept option "
                + (option && option->key ? option->key : "unknown");
            av_dict_free(&options);
            Reset();
            return false;
        }

        d3d_frame_ = av_frame_alloc();
        qsv_frame_ = av_frame_alloc();
        packet_ = av_packet_alloc();
        if (!d3d_frame_ || !qsv_frame_ || !packet_) {
            error = "could not allocate reusable FFmpeg QSV frame/packet";
            Reset();
            return false;
        }
        if (!PrepareInputFrame(error)) {
            Reset();
            return false;
        }

        std::vector<uint8_t> extradata;
        if (selection.codec == VideoCodec::H264) {
            extradata = BuildAvccExtradata(
                codec_context_->extradata,
                codec_context_->extradata_size,
                nals_scratch_);
        } else if (codec_context_->extradata
            && codec_context_->extradata_size > 0) {
            extradata.assign(
                codec_context_->extradata,
                codec_context_->extradata + codec_context_->extradata_size);
        }
        if (selection.codec != VideoCodec::AV1 && extradata.empty()) {
            error = std::string(selection.encoder_name)
                + " did not provide usable decoder configuration";
            Reset();
            return false;
        }

        video_config = BuildQsvVideoConfig(
            selection.codec,
            output_width_,
            output_height_,
            config.fps,
            config.bitrate_kbps,
            extradata);
        video_config.hdr10 = hdr10_;
        if (!IsValidEncodedVideoConfig(video_config)) {
            error = "QSV produced an invalid encoded video configuration";
            Reset();
            return false;
        }

        initialized_ = true;
        std::cout << "[QSV] Opened " << selection.encoder_name
                  << (hdr10_ ? " on the selected Intel D3D11 adapter (P010 -> QSV)"
                             : " on the selected Intel D3D11 adapter (BGRA -> NV12 -> QSV)")
                  << std::endl;
        return true;
    }

    bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource,
        std::string& error) override {
        prepared_ = false;
        if (!initialized_ || !source || !copy_context_ || !d3d_frame_) {
            error = "QSV conversion resources are unavailable";
            return false;
        }
        if (!d3d_frame_->data[0]) {
            error = prepare_error_.empty()
                ? "QSV has no prepared D3D11 pool surface"
                : prepare_error_;
            return false;
        }

        ID3D11Device* source_device = nullptr;
        source->GetDevice(&source_device);
        const bool same_device = SameComObject(source_device, d3d11_device_);
        if (source_device) source_device->Release();
        if (!same_device) {
            error = "QSV source texture belongs to a different D3D11 device";
            return false;
        }

        D3D11_TEXTURE2D_DESC source_description{};
        source->GetDesc(&source_description);
        const uint32_t mip_levels = std::max(1U, source_description.MipLevels);
        const uint32_t subresource_count = mip_levels
            * std::max(1U, source_description.ArraySize);
        if (source_description.Width != source_width_
            || source_description.Height != source_height_
            || source_description.Format != (hdr10_ ? DXGI_FORMAT_P010 : DXGI_FORMAT_B8G8R8A8_UNORM)
            || source_description.SampleDesc.Count != 1
            || source_subresource >= subresource_count) {
            error = "QSV source texture geometry, format, or subresource is invalid";
            return false;
        }

        if (hdr10_) return CopyEncoderInput(source, source_subresource, error);

        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
        input_description.FourCC = 0;
        input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        input_description.Texture2D.MipSlice = source_subresource % mip_levels;
        input_description.Texture2D.ArraySlice = source_subresource / mip_levels;
        ID3D11VideoProcessorInputView* input_view = nullptr;
        HRESULT result = video_device_->CreateVideoProcessorInputView(
            source,
            video_processor_enumerator_,
            &input_description,
            &input_view);
        if (FAILED(result) || !input_view) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessorInputView", result);
            return false;
        }

        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.OutputIndex = 0;
        stream.InputFrameOrField = 0;
        stream.PastFrames = 0;
        stream.FutureFrames = 0;
        stream.pInputSurface = input_view;
        result = video_context_->VideoProcessorBlt(
            video_processor_, converter_output_view_, 0, 1, &stream);
        input_view->Release();
        if (FAILED(result)) {
            error = HResultString("ID3D11VideoContext::VideoProcessorBlt", result);
            return false;
        }

        return CopyEncoderInput(converter_texture_, 0, error);
    }

    bool CopyEncoderInput(ID3D11Texture2D* texture, uint32_t subresource,
                          std::string& error) {
        copy_context_->CopySubresourceRegion(
            reinterpret_cast<ID3D11Texture2D*>(d3d_frame_->data[0]),
            static_cast<UINT>(reinterpret_cast<uintptr_t>(d3d_frame_->data[1])),
            0, 0, 0,
            texture,
            subresource,
            nullptr);
        const HRESULT removed = d3d11_device_->GetDeviceRemovedReason();
        if (FAILED(removed)) {
            error = HResultString("Intel D3D11 device", removed);
            return false;
        }
        prepared_ = true;
        return true;
    }

    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override {
        if (!initialized_ || !d3d_frame_ || !d3d_frame_->data[0]) return nullptr;
        return reinterpret_cast<ID3D11Texture2D*>(d3d_frame_->data[0]);
    }

    uint32_t GetCurrentInputSubresource() const noexcept override {
        if (!initialized_ || !d3d_frame_) return 0;
        return static_cast<uint32_t>(
            reinterpret_cast<uintptr_t>(d3d_frame_->data[1]));
    }

    detail::QsvSubmitStatus SubmitFrame(
        int64_t pts,
        bool force_keyframe,
        std::string& error) override {
        if (!initialized_ || !codec_context_ || !qsv_frame_) {
            error = "QSV frame submitted before initialization";
            return detail::QsvSubmitStatus::Error;
        }
        if (!prepared_) {
            error = "QSV frame submitted before GPU conversion completed";
            return detail::QsvSubmitStatus::Error;
        }

        const HRESULT removed = d3d11_device_->GetDeviceRemovedReason();
        if (FAILED(removed)) {
            error = HResultString("Intel D3D11 device", removed);
            return detail::QsvSubmitStatus::Error;
        }

        qsv_frame_->pts = pts;
        qsv_frame_->pict_type = force_keyframe
            ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        const int result = avcodec_send_frame(codec_context_, qsv_frame_);
        if (result == AVERROR(EAGAIN)) {
            return detail::QsvSubmitStatus::NeedDrain;
        }
        if (result < 0) {
            error = std::string("avcodec_send_frame failed: ")
                + AvErrorString(result);
            return detail::QsvSubmitStatus::Error;
        }

        prepared_ = false;
        av_frame_unref(qsv_frame_);
        av_frame_unref(d3d_frame_);
        std::string prepare_error;
        if (!PrepareInputFrame(prepare_error)) {
            prepare_error_ = std::move(prepare_error);
        } else {
            prepare_error_.clear();
        }
        return detail::QsvSubmitStatus::Accepted;
    }

    detail::QsvReceiveStatus ReceivePacket(
        detail::QsvPacketView& output,
        std::string& error) override {
        if (!codec_context_ || !packet_) {
            error = "QSV receive called without a codec context";
            return detail::QsvReceiveStatus::Error;
        }

        while (true) {
            av_packet_unref(packet_);
            const int result = avcodec_receive_packet(codec_context_, packet_);
            if (result == AVERROR(EAGAIN)) {
                return detail::QsvReceiveStatus::NeedMoreInput;
            }
            if (result == AVERROR_EOF) {
                return detail::QsvReceiveStatus::EndOfStream;
            }
            if (result < 0) {
                error = std::string("avcodec_receive_packet failed: ")
                    + AvErrorString(result);
                return detail::QsvReceiveStatus::Error;
            }

            const uint8_t* packet_data = packet_->data;
            int packet_size = packet_->size;
            if (selection_ && selection_->codec == VideoCodec::H264) {
                if (ConvertAnnexBPacketToAvcc(
                        packet_->data,
                        packet_->size,
                        nals_scratch_,
                        normalized_packet_)) {
                    packet_data = normalized_packet_.data();
                    packet_size = static_cast<int>(normalized_packet_.size());
                    if (packet_size == 0) continue;
                }
            }

            if (!packet_data || packet_size <= 0) continue;
            if (packet_->pts == AV_NOPTS_VALUE) {
                error = "QSV returned an encoded packet without a PTS";
                return detail::QsvReceiveStatus::Error;
            }
            output = {};
            output.data = packet_data;
            output.size = static_cast<uint32_t>(packet_size);
            output.pts = packet_->pts;
            output.dts = packet_->dts == AV_NOPTS_VALUE
                ? packet_->pts : packet_->dts;
            output.keyframe = (packet_->flags & AV_PKT_FLAG_KEY) != 0;
            size_t extradata_size = 0;
            output.codec_extradata = av_packet_get_side_data(
                packet_, AV_PKT_DATA_NEW_EXTRADATA, &extradata_size);
            output.codec_extradata_size = static_cast<uint32_t>(extradata_size);
            return detail::QsvReceiveStatus::Packet;
        }
    }

    detail::QsvSubmitStatus SendEndOfStream(std::string& error) override {
        if (!codec_context_) return detail::QsvSubmitStatus::Accepted;
        const int result = avcodec_send_frame(codec_context_, nullptr);
        if (result == AVERROR(EAGAIN)) {
            return detail::QsvSubmitStatus::NeedDrain;
        }
        if (result == AVERROR_EOF) {
            return detail::QsvSubmitStatus::Accepted;
        }
        if (result < 0) {
            error = std::string("QSV flush submission failed: ")
                + AvErrorString(result);
            return detail::QsvSubmitStatus::Error;
        }
        return detail::QsvSubmitStatus::Accepted;
    }

    void Reset() noexcept override {
        initialized_ = false;
        prepared_ = false;
        prepare_error_.clear();
        normalized_packet_.clear();
        nals_scratch_.clear();
        if (d3d_frame_) av_frame_free(&d3d_frame_);
        if (qsv_frame_) av_frame_free(&qsv_frame_);
        if (packet_) av_packet_free(&packet_);
        if (codec_context_) avcodec_free_context(&codec_context_);
        if (qsv_frames_ctx_) av_buffer_unref(&qsv_frames_ctx_);
        if (d3d_frames_ctx_) av_buffer_unref(&d3d_frames_ctx_);
        if (qsv_device_ctx_) av_buffer_unref(&qsv_device_ctx_);
        if (d3d_device_ctx_) av_buffer_unref(&d3d_device_ctx_);
        if (converter_output_view_) {
            converter_output_view_->Release();
            converter_output_view_ = nullptr;
        }
        if (converter_texture_) {
            converter_texture_->Release();
            converter_texture_ = nullptr;
        }
        if (video_processor_) {
            video_processor_->Release();
            video_processor_ = nullptr;
        }
        if (video_processor_enumerator_) {
            video_processor_enumerator_->Release();
            video_processor_enumerator_ = nullptr;
        }
        if (video_context_) {
            video_context_->Release();
            video_context_ = nullptr;
        }
        if (video_device_) {
            video_device_->Release();
            video_device_ = nullptr;
        }
        if (copy_context_) {
            copy_context_->Release();
            copy_context_ = nullptr;
        }
        d3d11_device_ = nullptr;
        source_width_ = 0;
        source_height_ = 0;
        output_width_ = 0;
        output_height_ = 0;
        selection_ = nullptr;
    }

private:
    bool CreateHardwareContexts(
        ID3D11Device* shared_device,
        std::string& error) {
        d3d_device_ctx_ = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
        if (!d3d_device_ctx_) {
            error = "could not allocate FFmpeg D3D11 hardware device context";
            return false;
        }
        auto* device_context = reinterpret_cast<AVHWDeviceContext*>(
            d3d_device_ctx_->data);
        auto* d3d11_context = reinterpret_cast<AVD3D11VADeviceContext*>(
            device_context->hwctx);
        shared_device->AddRef();
        d3d11_context->device = shared_device;
        int result = av_hwdevice_ctx_init(d3d_device_ctx_);
        if (result < 0) {
            error = "could not initialize FFmpeg D3D11 hardware device context: "
                + AvErrorString(result);
            return false;
        }

        result = av_hwdevice_ctx_create_derived(
            &qsv_device_ctx_,
            AV_HWDEVICE_TYPE_QSV,
            d3d_device_ctx_,
            0);
        if (result < 0) {
            error = "could not derive oneVPL/QSV from the selected Intel "
                "D3D11 device: " + AvErrorString(result);
            return false;
        }

        d3d_frames_ctx_ = av_hwframe_ctx_alloc(d3d_device_ctx_);
        if (!d3d_frames_ctx_) {
            error = "could not allocate FFmpeg Intel D3D11 frame context";
            return false;
        }
        auto* frames_context = reinterpret_cast<AVHWFramesContext*>(
            d3d_frames_ctx_->data);
        frames_context->format = AV_PIX_FMT_D3D11;
        frames_context->sw_format = hdr10_ ? AV_PIX_FMT_P010 : AV_PIX_FMT_NV12;
        frames_context->width = static_cast<int>(output_width_);
        frames_context->height = static_cast<int>(output_height_);
        frames_context->initial_pool_size = kQsvPoolSize;
        auto* d3d_frames = reinterpret_cast<AVD3D11VAFramesContext*>(
            frames_context->hwctx);
        // Keep the encoder pool separate from the render-target converter.
        // Render-target pools lose array-slice identity in the pinned QSV map.
        d3d_frames->BindFlags = D3D11_BIND_DECODER | D3D11_BIND_VIDEO_ENCODER;
        result = av_hwframe_ctx_init(d3d_frames_ctx_);
        if (result < 0) {
            error = "could not initialize Intel D3D11 frame pool: "
                + AvErrorString(result);
            return false;
        }

        result = av_hwframe_ctx_create_derived(
            &qsv_frames_ctx_,
            AV_PIX_FMT_QSV,
            qsv_device_ctx_,
            d3d_frames_ctx_,
            AV_HWFRAME_MAP_READ);
        if (result < 0) {
            error = "could not derive QSV frames from Intel D3D11 pool: "
                + AvErrorString(result);
            return false;
        }
        d3d11_device_ = shared_device;
        return true;
    }

    bool CreateConversionPipeline(
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        uint32_t fps,
        bool fit,
        std::string& error) {
        HRESULT result = shared_device->QueryInterface(
            __uuidof(ID3D11VideoDevice),
            reinterpret_cast<void**>(&video_device_));
        if (FAILED(result) || !video_device_) {
            error = HResultString("Intel D3D11 video-device query", result);
            return false;
        }
        result = shared_context->QueryInterface(
            __uuidof(ID3D11VideoContext),
            reinterpret_cast<void**>(&video_context_));
        if (FAILED(result) || !video_context_) {
            error = HResultString("Intel D3D11 video-context query", result);
            return false;
        }
        copy_context_ = shared_context;
        copy_context_->AddRef();
        if (hdr10_) return true;

        D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
        content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        content.InputFrameRate = {fps, 1};
        content.InputWidth = source_width_;
        content.InputHeight = source_height_;
        content.OutputFrameRate = {fps, 1};
        content.OutputWidth = output_width_;
        content.OutputHeight = output_height_;
        content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
        result = video_device_->CreateVideoProcessorEnumerator(
            &content, &video_processor_enumerator_);
        if (FAILED(result) || !video_processor_enumerator_) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessorEnumerator", result);
            return false;
        }

        UINT input_support = 0;
        UINT output_support = 0;
        result = video_processor_enumerator_->CheckVideoProcessorFormat(
            DXGI_FORMAT_B8G8R8A8_UNORM, &input_support);
        if (FAILED(result)
            || (input_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT) == 0) {
            error = "Intel D3D11 video processor does not accept BGRA input";
            return false;
        }
        result = video_processor_enumerator_->CheckVideoProcessorFormat(
            DXGI_FORMAT_NV12, &output_support);
        if (FAILED(result)
            || (output_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT) == 0) {
            error = "Intel D3D11 video processor does not produce NV12 output";
            return false;
        }

        result = video_device_->CreateVideoProcessor(
            video_processor_enumerator_, 0, &video_processor_);
        if (FAILED(result) || !video_processor_) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessor", result);
            return false;
        }

        D3D11_TEXTURE2D_DESC converter_description{};
        converter_description.Width = output_width_;
        converter_description.Height = output_height_;
        converter_description.MipLevels = 1;
        converter_description.ArraySize = 1;
        converter_description.Format = DXGI_FORMAT_NV12;
        converter_description.SampleDesc.Count = 1;
        converter_description.Usage = D3D11_USAGE_DEFAULT;
        converter_description.BindFlags = D3D11_BIND_RENDER_TARGET;
        result = shared_device->CreateTexture2D(
            &converter_description, nullptr, &converter_texture_);
        if (FAILED(result) || !converter_texture_) {
            error = HResultString("NV12 converter texture creation", result);
            return false;
        }

        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
        output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
        output_description.Texture2D.MipSlice = 0;
        result = video_device_->CreateVideoProcessorOutputView(
            converter_texture_,
            video_processor_enumerator_,
            &output_description,
            &converter_output_view_);
        if (FAILED(result) || !converter_output_view_) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessorOutputView", result);
            return false;
        }

        RECT source_rect{
            0, 0,
            static_cast<LONG>(source_width_),
            static_cast<LONG>(source_height_)};
        CaptureScaleGeometry geometry;
        if (!BuildCaptureScaleGeometry(
                source_width_, source_height_, output_width_, output_height_,
                fit, geometry)) {
            error = "invalid QSV source-to-output scaling geometry";
            return false;
        }
        RECT output_rect{
            static_cast<LONG>(geometry.destination.left),
            static_cast<LONG>(geometry.destination.top),
            static_cast<LONG>(geometry.destination.left
                + geometry.destination.width),
            static_cast<LONG>(geometry.destination.top
                + geometry.destination.height)};
        RECT target_rect{
            0, 0, static_cast<LONG>(output_width_),
            static_cast<LONG>(output_height_)};
        video_context_->VideoProcessorSetStreamFrameFormat(
            video_processor_, 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        video_context_->VideoProcessorSetStreamSourceRect(
            video_processor_, 0, TRUE, &source_rect);
        video_context_->VideoProcessorSetStreamDestRect(
            video_processor_, 0, TRUE, &output_rect);
        video_context_->VideoProcessorSetOutputTargetRect(
            video_processor_, TRUE, &target_rect);
        D3D11_VIDEO_COLOR background{};
        background.RGBA = {0.f, 0.f, 0.f, 1.f};
        video_context_->VideoProcessorSetOutputBackgroundColor(
            video_processor_, FALSE, &background);
        video_context_->VideoProcessorSetStreamAutoProcessingMode(
            video_processor_, 0, FALSE);

        D3D11_VIDEO_PROCESSOR_COLOR_SPACE input_color{};
        input_color.RGB_Range = 0;
        input_color.YCbCr_Matrix = 1;
        input_color.Nominal_Range = 2;
        D3D11_VIDEO_PROCESSOR_COLOR_SPACE output_color{};
        output_color.YCbCr_Matrix = 1;
        output_color.Nominal_Range = 1;
        video_context_->VideoProcessorSetStreamColorSpace(
            video_processor_, 0, &input_color);
        video_context_->VideoProcessorSetOutputColorSpace(
            video_processor_, &output_color);
        return true;
    }

    bool PrepareInputFrame(std::string& error) {
        if (!d3d_frame_ || !qsv_frame_
            || !d3d_frames_ctx_ || !qsv_frames_ctx_) {
            error = "QSV input frame or hardware pool is unavailable";
            return false;
        }
        av_frame_unref(qsv_frame_);
        av_frame_unref(d3d_frame_);
        int result = av_hwframe_get_buffer(d3d_frames_ctx_, d3d_frame_, 0);
        if (result < 0) {
            error = "could not acquire Intel D3D11 input texture: "
                + AvErrorString(result);
            return false;
        }
        if (d3d_frame_->format != AV_PIX_FMT_D3D11 || !d3d_frame_->data[0]) {
            error = "FFmpeg Intel hardware pool did not return a D3D11 texture";
            av_frame_unref(d3d_frame_);
            return false;
        }

        qsv_frame_->format = AV_PIX_FMT_QSV;
        qsv_frame_->hw_frames_ctx = av_buffer_ref(qsv_frames_ctx_);
        if (!qsv_frame_->hw_frames_ctx) {
            error = "could not retain derived QSV frame context";
            av_frame_unref(d3d_frame_);
            return false;
        }
        result = av_hwframe_map(
            qsv_frame_, d3d_frame_, AV_HWFRAME_MAP_READ);
        if (result < 0 || !qsv_frame_->data[3]) {
            error = "could not map Intel D3D11 pool slice to QSV surface: "
                + AvErrorString(result);
            av_frame_unref(qsv_frame_);
            av_frame_unref(d3d_frame_);
            return false;
        }
        return true;
    }

    const QsvCodecSelection* selection_ = nullptr;
    bool hdr10_ = false;
    AVBufferRef* d3d_device_ctx_ = nullptr;
    AVBufferRef* qsv_device_ctx_ = nullptr;
    AVBufferRef* d3d_frames_ctx_ = nullptr;
    AVBufferRef* qsv_frames_ctx_ = nullptr;
    AVCodecContext* codec_context_ = nullptr;
    AVFrame* d3d_frame_ = nullptr;
    AVFrame* qsv_frame_ = nullptr;
    AVPacket* packet_ = nullptr;

    ID3D11Device* d3d11_device_ = nullptr;
    ID3D11DeviceContext* copy_context_ = nullptr;
    ID3D11VideoDevice* video_device_ = nullptr;
    ID3D11VideoContext* video_context_ = nullptr;
    ID3D11VideoProcessorEnumerator* video_processor_enumerator_ = nullptr;
    ID3D11VideoProcessor* video_processor_ = nullptr;
    ID3D11Texture2D* converter_texture_ = nullptr;
    ID3D11VideoProcessorOutputView* converter_output_view_ = nullptr;

    uint32_t source_width_ = 0;
    uint32_t source_height_ = 0;
    uint32_t output_width_ = 0;
    uint32_t output_height_ = 0;
    bool initialized_ = false;
    bool prepared_ = false;
    std::string prepare_error_;
    std::vector<NalSpan> nals_scratch_;
    std::vector<uint8_t> normalized_packet_;
};

} // namespace

const QsvCodecSelection* GetQsvCodecSelection(VideoCodec codec) noexcept {
    switch (codec) {
    case VideoCodec::H264: return &kH264Qsv;
    case VideoCodec::HEVC: return &kHevcQsv;
    case VideoCodec::AV1: return &kAv1Qsv;
    }
    return nullptr;
}

EncodedVideoConfig BuildQsvVideoConfig(
    VideoCodec codec,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    uint32_t bitrate_kbps,
    const std::vector<uint8_t>& codec_extradata) {
    EncodedVideoConfig config;
    config.codec = codec;
    config.width = width;
    config.height = height;
    config.frame_rate = {static_cast<int32_t>(fps), 1};
    config.time_base = {1, static_cast<int32_t>(fps)};
    config.bitrate_kbps = bitrate_kbps;
    config.max_keyframe_interval_frames = fps;
    config.max_b_frames = 0;
    const auto* selection = GetQsvCodecSelection(codec);
    if (selection) config.packet_format = selection->packet_format;
    config.codec_extradata = codec_extradata;
    return config;
}

namespace detail {

bool EnableQsvMultithreadProtection(
    ID3D11DeviceContext* context, std::string& error) {
    if (!context || context->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE) {
        error = "QSV requires a D3D11 immediate context for multithread protection";
        return false;
    }
    ID3D11Multithread* multithread = nullptr;
    const HRESULT result = context->QueryInterface(
        __uuidof(ID3D11Multithread), reinterpret_cast<void**>(&multithread));
    if (FAILED(result) || !multithread) {
        error = HResultString("QSV ID3D11Multithread query", result);
        return false;
    }
    // The return value is the PREVIOUS state, not a success result. A fresh
    // capture context normally returns FALSE here. Leave the lock enabled
    // for the full shared-device lifetime, including oneVPL shutdown.
    multithread->SetMultithreadProtected(TRUE);
    const bool protected_context = multithread->GetMultithreadProtected() != FALSE;
    multithread->Release();
    if (!protected_context) {
        error = "could not enable QSV D3D11 multithread protection";
        return false;
    }
    return true;
}

std::unique_ptr<IQsvCodecSession> CreateProductionQsvCodecSession() {
    return std::make_unique<FfmpegQsvCodecSession>();
}

} // namespace detail

FfmpegQsvReplayEncoder::FfmpegQsvReplayEncoder(VideoCodec codec)
    : FfmpegQsvReplayEncoder(
          codec, detail::CreateProductionQsvCodecSession()) {}

FfmpegQsvReplayEncoder::FfmpegQsvReplayEncoder(
    VideoCodec codec,
    std::unique_ptr<detail::IQsvCodecSession> session)
    : codec_(codec), session_(std::move(session)) {}

FfmpegQsvReplayEncoder::~FfmpegQsvReplayEncoder() {
    Shutdown();
}

bool FfmpegQsvReplayEncoder::Initialize(
    const EncoderConfig& config,
    ID3D11Device* shared_device,
    ID3D11DeviceContext* shared_context,
    PacketCallback callback,
    bool cpu_input_mode) {
    if (initialized_) Shutdown();
    last_error_.clear();
    submitted_timing_.clear();
    video_config_ = {};
    packet_callback_ = {};
    first_frame_ = true;
    flushing_ = false;
    gpu_frame_prepared_ = false;
    encode_start_qpc_ = 0;
    last_pts_ = -1;
    last_forced_keyframe_pts_ = -1;
    fps_ = config.fps;

    if (!session_) {
        SetError("QSV codec session is unavailable");
        return false;
    }
    session_->Reset();
    if (cpu_input_mode) {
        SetError("Intel QSV supports only the same-adapter D3D11 hardware path");
        return false;
    }
    if (!callback) {
        SetError("QSV packet callback must not be null");
        return false;
    }
    const auto* selection = GetQsvCodecSelection(codec_);
    if (!selection) {
        SetError("unsupported QSV codec selection");
        return false;
    }

    LARGE_INTEGER frequency{};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) {
        SetError("QueryPerformanceFrequency failed for QSV timestamps");
        return false;
    }
    qpc_frequency_ = frequency.QuadPart;

    std::string error;
    EncodedVideoConfig candidate_config;
    if (!session_->Initialize(
            config,
            shared_device,
            shared_context,
            *selection,
            candidate_config,
            error)) {
        SetError(error.empty() ? "QSV initialization failed" : std::move(error));
        session_->Reset();
        return false;
    }
    if (candidate_config.codec != codec_
        || candidate_config.packet_format != selection->packet_format
        || !IsValidEncodedVideoConfig(candidate_config)
        || (codec_ != VideoCodec::AV1
            && candidate_config.codec_extradata.empty())) {
        SetError("QSV active codec/config does not match the requested codec");
        session_->Reset();
        return false;
    }

    video_config_ = std::move(candidate_config);
    packet_callback_ = std::move(callback);
    initialized_ = true;
    return true;
}

bool FfmpegQsvReplayEncoder::RequiresBackendGpuPreparation() const noexcept {
    return true;
}

bool FfmpegQsvReplayEncoder::PrepareGpuFrame(
    ID3D11Texture2D* source,
    uint32_t source_subresource) {
    gpu_frame_prepared_ = false;
    if (!initialized_ || flushing_ || !session_) {
        SetError("QSV GPU preparation called outside an active generation");
        return false;
    }
    std::string error;
    if (!session_->PrepareGpuFrame(source, source_subresource, error)) {
        SetError(error.empty() ? "QSV GPU conversion failed" : std::move(error));
        return false;
    }
    gpu_frame_prepared_ = true;
    return true;
}

bool FfmpegQsvReplayEncoder::EncodeFrame(int64_t present_qpc) {
    if (!initialized_ || flushing_) {
        SetError("QSV EncodeFrame called outside an active generation");
        return false;
    }
    if (!gpu_frame_prepared_) {
        SetError("QSV EncodeFrame called before GPU preparation");
        return false;
    }

    const int64_t pts = ComputePts(present_qpc);
    const bool force_keyframe = ConsumeKeyframeRequest() || last_forced_keyframe_pts_ < 0
        || pts - last_forced_keyframe_pts_
            >= static_cast<int64_t>(fps_) * 4;
    if (!SubmitWithBackpressure(pts, force_keyframe)) return false;
    gpu_frame_prepared_ = false;

    LARGE_INTEGER now{};
    if (present_qpc <= 0) QueryPerformanceCounter(&now);
    const int64_t wall_qpc = present_qpc > 0 ? present_qpc : now.QuadPart;
    submitted_timing_.push_back({pts, wall_qpc});
    if (force_keyframe) last_forced_keyframe_pts_ = pts;
    return DrainAvailable(false);
}

bool FfmpegQsvReplayEncoder::EncodeFrameCPU(
    const uint8_t*, uint32_t, int64_t) {
    SetError("Intel QSV CPU full-frame input is intentionally unsupported");
    return false;
}

ID3D11Texture2D* FfmpegQsvReplayEncoder::GetCurrentInputTexture() const noexcept {
    return initialized_ && session_
        ? session_->GetCurrentInputTexture()
        : nullptr;
}

uint32_t FfmpegQsvReplayEncoder::GetCurrentInputSubresource() const noexcept {
    return initialized_ && session_
        ? session_->GetCurrentInputSubresource()
        : 0;
}

void FfmpegQsvReplayEncoder::Shutdown() {
    if (initialized_) Flush();
    initialized_ = false;
    flushing_ = false;
    gpu_frame_prepared_ = false;
    if (session_) session_->Reset();
    packet_callback_ = {};
    submitted_timing_.clear();
}

EncodedVideoConfig FfmpegQsvReplayEncoder::GetVideoConfig() const {
    return video_config_;
}

bool FfmpegQsvReplayEncoder::IsVideoConfigReady() const {
    return initialized_
        && IsValidEncodedVideoConfig(video_config_)
        && !video_config_.codec_extradata.empty();
}

ActiveEncoderInfo FfmpegQsvReplayEncoder::GetActiveEncoderInfo() const {
    const auto* selection = GetQsvCodecSelection(codec_);
    return {
        EncoderVendor::Intel,
        ReplayEncoderBackend::FfmpegQsv,
        codec_,
        initialized_,
        selection ? selection->encoder_name : "qsv_unknown"};
}

std::string FfmpegQsvReplayEncoder::GetLastError() const {
    return last_error_;
}

bool FfmpegQsvReplayEncoder::GetEncodeEpoch(
    int64_t& start_qpc,
    int64_t& qpc_frequency) const {
    if (first_frame_ || encode_start_qpc_ <= 0 || qpc_frequency_ <= 0) {
        start_qpc = 0;
        qpc_frequency = 0;
        return false;
    }
    start_qpc = encode_start_qpc_;
    qpc_frequency = qpc_frequency_;
    return true;
}

bool FfmpegQsvReplayEncoder::IsInitialized() const {
    return initialized_;
}

int64_t FfmpegQsvReplayEncoder::ComputePts(int64_t present_qpc) {
    int64_t frame_qpc = present_qpc;
    if (frame_qpc <= 0) {
        LARGE_INTEGER now{};
        QueryPerformanceCounter(&now);
        frame_qpc = now.QuadPart;
    }

    int64_t pts = 0;
    if (first_frame_) {
        first_frame_ = false;
        encode_start_qpc_ = frame_qpc;
    } else {
        const int64_t elapsed = frame_qpc - encode_start_qpc_;
        pts = (elapsed * static_cast<int64_t>(fps_)) / qpc_frequency_;
        if (pts <= last_pts_) pts = last_pts_ + 1;
    }
    last_pts_ = pts;
    return pts;
}

int64_t FfmpegQsvReplayEncoder::WallQpcForPts(int64_t pts) {
    while (submitted_timing_.size() > 1
        && submitted_timing_[1].pts <= pts) {
        submitted_timing_.pop_front();
    }
    if (!submitted_timing_.empty()
        && submitted_timing_.front().pts <= pts) {
        return submitted_timing_.front().wall_qpc;
    }
    return 0;
}

bool FfmpegQsvReplayEncoder::DrainAvailable(bool flushing) {
    while (true) {
        detail::QsvPacketView packet;
        std::string error;
        const auto status = session_->ReceivePacket(packet, error);
        switch (status) {
        case detail::QsvReceiveStatus::Packet:
            if (!UpdateDeferredVideoConfig(packet)) return false;
            if (!IsVideoConfigReady()) {
                SetError("QSV emitted a packet before decoder configuration was available");
                return false;
            }
            if (packet.data && packet.size > 0 && packet_callback_) {
                packet_callback_(
                    packet.data,
                    packet.size,
                    packet.pts,
                    packet.keyframe,
                    WallQpcForPts(packet.pts));
            }
            break;
        case detail::QsvReceiveStatus::NeedMoreInput:
            return true;
        case detail::QsvReceiveStatus::EndOfStream:
            return true;
        case detail::QsvReceiveStatus::Error:
            SetError(error.empty() ? "QSV packet receive failed" : std::move(error));
            return false;
        }
        if (!flushing && !initialized_) return false;
    }
}

bool FfmpegQsvReplayEncoder::SubmitWithBackpressure(
    int64_t pts,
    bool force_keyframe) {
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string error;
        const auto status = session_->SubmitFrame(pts, force_keyframe, error);
        if (status == detail::QsvSubmitStatus::Accepted) return true;
        if (status == detail::QsvSubmitStatus::Error) {
            SetError(error.empty() ? "QSV frame submission failed" : std::move(error));
            return false;
        }
        if (!DrainAvailable(false)) return false;
    }
    SetError("QSV encoder remained backpressured after draining output");
    return false;
}

bool FfmpegQsvReplayEncoder::Flush() {
    flushing_ = true;
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string error;
        const auto status = session_->SendEndOfStream(error);
        if (status == detail::QsvSubmitStatus::Accepted) {
            const bool drained = DrainAvailable(true);
            flushing_ = false;
            return drained;
        }
        if (status == detail::QsvSubmitStatus::Error) {
            SetError(error.empty() ? "QSV flush failed" : std::move(error));
            flushing_ = false;
            return false;
        }
        if (!DrainAvailable(true)) {
            flushing_ = false;
            return false;
        }
    }
    SetError("QSV flush remained backpressured after draining output");
    flushing_ = false;
    return false;
}

bool FfmpegQsvReplayEncoder::UpdateDeferredVideoConfig(
    const detail::QsvPacketView& packet) {
    if (!packet.codec_extradata || packet.codec_extradata_size == 0) {
        return true;
    }
    const std::vector<uint8_t> packet_config(
        packet.codec_extradata,
        packet.codec_extradata + packet.codec_extradata_size);
    if (video_config_.codec_extradata.empty()) {
        video_config_.codec_extradata = packet_config;
        return true;
    }
    if (video_config_.codec_extradata != packet_config) {
        SetError("QSV decoder configuration changed within one capture generation");
        return false;
    }
    return true;
}

void FfmpegQsvReplayEncoder::SetError(std::string error) {
    last_error_ = std::move(error);
    if (!last_error_.empty()) {
        std::cerr << "[QSV] " << last_error_ << std::endl;
    }
}

} // namespace fthr
