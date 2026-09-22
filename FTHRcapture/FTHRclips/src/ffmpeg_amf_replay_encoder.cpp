#include "ffmpeg_amf_replay_encoder.h"
#include "capture_scale_geometry.h"
#include "windows_native_error.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>

#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable: 4244)
#endif
extern "C" {
#include <libavcodec/avcodec.h>
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
#include <limits>
#include <sstream>
#include <utility>

namespace fthr {
namespace {

using NalSpan = std::pair<const uint8_t*, int>;

const AmfCodecSelection kH264Amf{
    VideoCodec::H264,
    "h264_amf",
    EncodedPacketFormat::LengthPrefixedNalUnits,
    AV_PROFILE_H264_MAIN};

const AmfCodecSelection kHevcAmf{
    VideoCodec::HEVC,
    "hevc_amf",
    EncodedPacketFormat::AnnexBNalUnits,
    AV_PROFILE_HEVC_MAIN};

const AmfCodecSelection kAv1Amf{
    VideoCodec::AV1,
    "av1_amf",
    EncodedPacketFormat::LowOverheadObu,
    AV_PROFILE_AV1_MAIN};

std::string AvErrorString(int error_code) {
    char buffer[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(error_code, buffer, sizeof(buffer));
    return buffer;
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

class FfmpegAmfCodecSession final : public detail::IAmfCodecSession {
public:
    ~FfmpegAmfCodecSession() override {
        Reset();
    }

    bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        const AmfCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) override {
        Reset();
        selection_ = &selection;

        if (!shared_device || !shared_context) {
            error = "AMF requires a D3D11 device and immediate context";
            return false;
        }
        if (QueryD3D11DeviceVendor(shared_device) != EncoderVendor::Amd) {
            error = "selected D3D11 device is not AMD; refusing cross-adapter AMF";
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

        CaptureScaleGeometry geometry;
        if (!BuildCaptureScaleGeometry(
                config.src_width, config.src_height,
                config.enc_width, config.enc_height,
                config.scaling_mode == 1, geometry)
            || config.fps == 0 || config.bitrate_kbps == 0) {
            error = "invalid AMF encoder dimensions, frame rate, or bitrate";
            return false;
        }
        source_width_ = geometry.source_width;
        source_height_ = geometry.source_height;
        output_width_ = geometry.target_width;
        output_height_ = geometry.target_height;
        destination_ = geometry.destination;

        const AVCodec* codec = avcodec_find_encoder_by_name(selection.encoder_name);
        if (!codec || !codec->name
            || std::strcmp(codec->name, selection.encoder_name) != 0) {
            error = std::string(selection.encoder_name)
                + " not found in the pinned FFmpeg build";
            return false;
        }

        hw_device_ctx_ = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
        if (!hw_device_ctx_) {
            error = "could not allocate FFmpeg D3D11 hardware device context";
            return false;
        }
        auto* device_context = reinterpret_cast<AVHWDeviceContext*>(
            hw_device_ctx_->data);
        auto* d3d11_context = reinterpret_cast<AVD3D11VADeviceContext*>(
            device_context->hwctx);
        shared_device->AddRef();
        d3d11_context->device = shared_device;
        int result = av_hwdevice_ctx_init(hw_device_ctx_);
        if (result < 0) {
            error = "could not initialize FFmpeg D3D11 hardware device context: "
                + AvErrorString(result);
            Reset();
            return false;
        }

        hw_frames_ctx_ = av_hwframe_ctx_alloc(hw_device_ctx_);
        if (!hw_frames_ctx_) {
            error = "could not allocate FFmpeg D3D11 hardware frame context";
            Reset();
            return false;
        }
        auto* frames_context = reinterpret_cast<AVHWFramesContext*>(
            hw_frames_ctx_->data);
        frames_context->format = AV_PIX_FMT_D3D11;
        frames_context->sw_format = AV_PIX_FMT_NV12;
        frames_context->width = static_cast<int>(output_width_);
        frames_context->height = static_cast<int>(output_height_);
        // This pinned D3D11 hwcontext requires a non-zero fixed pool. Eight
        // slices cover AMF async_depth=4 plus the prepared input and transient
        // references. Each submitted AVFrame retains its array slice until AMF
        // releases it; capture never maps this GPU-only texture array.
        frames_context->initial_pool_size = 8;
        auto* d3d_frames = reinterpret_cast<AVD3D11VAFramesContext*>(
            frames_context->hwctx);
        d3d_frames->BindFlags = D3D11_BIND_DECODER | D3D11_BIND_VIDEO_ENCODER;
        result = av_hwframe_ctx_init(hw_frames_ctx_);
        if (result < 0) {
            error = "could not initialize FFmpeg D3D11 hardware frame context: "
                + AvErrorString(result);
            Reset();
            return false;
        }

        codec_context_ = avcodec_alloc_context3(codec);
        if (!codec_context_) {
            error = "could not allocate AMF codec context";
            Reset();
            return false;
        }
        codec_context_->codec_id = codec->id;
        codec_context_->codec_type = AVMEDIA_TYPE_VIDEO;
        codec_context_->width = static_cast<int>(output_width_);
        codec_context_->height = static_cast<int>(output_height_);
        codec_context_->pix_fmt = AV_PIX_FMT_D3D11;
        codec_context_->color_range = AVCOL_RANGE_MPEG;
        codec_context_->color_primaries = AVCOL_PRI_BT709;
        codec_context_->color_trc = AVCOL_TRC_BT709;
        codec_context_->colorspace = AVCOL_SPC_BT709;
        codec_context_->chroma_sample_location = AVCHROMA_LOC_LEFT;
        codec_context_->time_base = AVRational{1, static_cast<int>(config.fps)};
        codec_context_->framerate = AVRational{static_cast<int>(config.fps), 1};
        codec_context_->bit_rate = static_cast<int64_t>(config.bitrate_kbps) * 1000;
        codec_context_->rc_max_rate = codec_context_->bit_rate;
        codec_context_->gop_size = static_cast<int>(config.fps);
        codec_context_->max_b_frames = 0;
        codec_context_->profile = selection.profile;
        codec_context_->flags |= AV_CODEC_FLAG_LOW_DELAY | AV_CODEC_FLAG_GLOBAL_HEADER;
        codec_context_->hw_device_ctx = av_buffer_ref(hw_device_ctx_);
        codec_context_->hw_frames_ctx = av_buffer_ref(hw_frames_ctx_);
        if (!codec_context_->hw_device_ctx || !codec_context_->hw_frames_ctx) {
            error = "could not retain FFmpeg D3D11 hardware contexts";
            Reset();
            return false;
        }

        AVDictionary* options = nullptr;
        av_dict_set(&options, "usage", "ultralowlatency", 0);
        av_dict_set(&options, "quality", "speed", 0);
        av_dict_set(&options, "rc", "cbr", 0);
        av_dict_set(&options, "forced_idr", "1", 0);
        av_dict_set(&options, "async_depth", "4", 0);
        if (selection.codec == VideoCodec::HEVC) {
            av_dict_set(&options, "bitdepth", "8", 0);
            av_dict_set(&options, "header_insertion_mode", "gop", 0);
            av_dict_set(&options, "gops_per_idr", "1", 0);
        } else if (selection.codec == VideoCodec::AV1) {
            av_dict_set(&options, "bitdepth", "8", 0);
            av_dict_set(&options, "header_insertion_mode", "gop", 0);
            av_dict_set(&options, "latency", "lowest_latency", 0);
        }

        result = avcodec_open2(codec_context_, codec, &options);
        if (result < 0) {
            error = std::string("could not open ") + selection.encoder_name
                + ": " + AvErrorString(result);
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

        frame_ = av_frame_alloc();
        packet_ = av_packet_alloc();
        if (!frame_ || !packet_) {
            error = "could not allocate reusable FFmpeg AMF frame/packet";
            Reset();
            return false;
        }
        d3d11_device_ = shared_device;
        if (!CreateConversionPipeline(shared_device, shared_context, config.fps, error)
            || !PrepareInputFrame(error)) {
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
        if (extradata.empty()) {
            error = std::string(selection.encoder_name)
                + " did not provide usable decoder configuration";
            Reset();
            return false;
        }

        video_config = BuildAmfVideoConfig(
            selection.codec,
            output_width_,
            output_height_,
            config.fps,
            config.bitrate_kbps,
            extradata);
        if (!IsValidEncodedVideoConfig(video_config)) {
            error = "AMF produced an invalid encoded video configuration";
            Reset();
            return false;
        }

        d3d11_device_ = shared_device;
        initialized_ = true;
        std::cout << "[AMF] Opened " << selection.encoder_name
                  << " on the selected AMD D3D11 adapter (NV12 hardware frames)"
                  << std::endl;
        return true;
    }

    bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource,
        std::string& error) override {
        prepared_ = false;
        if (!initialized_ || !source || !copy_context_ || !video_device_
            || !video_context_ || !video_processor_ || !video_processor_enumerator_
            || !converter_texture_ || !converter_output_view_ || !frame_
            || !frame_->data[0]) {
            error = "AMF conversion resources are unavailable";
            return false;
        }
        ID3D11Device* source_device = nullptr;
        source->GetDevice(&source_device);
        const bool same_device = SameComObject(source_device, d3d11_device_);
        if (source_device) source_device->Release();
        if (!same_device) {
            error = "AMF source texture belongs to a different D3D11 device";
            return false;
        }
        D3D11_TEXTURE2D_DESC source_description{};
        source->GetDesc(&source_description);
        const uint32_t mip_levels = std::max(1U, source_description.MipLevels);
        const uint32_t subresource_count = mip_levels
            * std::max(1U, source_description.ArraySize);
        if (source_description.Width != source_width_
            || source_description.Height != source_height_
            || source_description.Format != DXGI_FORMAT_B8G8R8A8_UNORM
            || source_subresource >= subresource_count) {
            error = "AMF source texture geometry, format, or subresource is invalid";
            return false;
        }
        ID3D11Texture2D* destination_texture =
            reinterpret_cast<ID3D11Texture2D*>(frame_->data[0]);
        const uint32_t destination_subresource = static_cast<uint32_t>(
            reinterpret_cast<uintptr_t>(frame_->data[1]));
        D3D11_TEXTURE2D_DESC destination_description{};
        destination_texture->GetDesc(&destination_description);
        const uint32_t destination_mips = std::max(
            1U, destination_description.MipLevels);
        const uint32_t destination_arrays = std::max(
            1U, destination_description.ArraySize);
        if (destination_description.Width != output_width_
            || destination_description.Height != output_height_
            || destination_description.Format != DXGI_FORMAT_NV12
            || destination_subresource >= destination_mips * destination_arrays) {
            error = "AMF hardware frame texture geometry, format, or subresource is invalid";
            return false;
        }
        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
        input_description.FourCC = 0;
        input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        input_description.Texture2D.MipSlice = source_subresource % mip_levels;
        input_description.Texture2D.ArraySlice = source_subresource / mip_levels;
        ID3D11VideoProcessorInputView* input_view = nullptr;
        HRESULT result = video_device_->CreateVideoProcessorInputView(
            source, video_processor_enumerator_, &input_description, &input_view);
        if (FAILED(result) || !input_view) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessorInputView", result);
            return false;
        }
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.OutputIndex = 0;
        stream.pInputSurface = input_view;
        result = video_context_->VideoProcessorBlt(
            video_processor_, converter_output_view_, 0, 1, &stream);
        input_view->Release();
        if (FAILED(result)) {
            error = HResultString("ID3D11VideoContext::VideoProcessorBlt", result);
            return false;
        }
        copy_context_->CopySubresourceRegion(
            destination_texture,
            destination_subresource,
            0, 0, 0, converter_texture_, 0, nullptr);
        const HRESULT removed = d3d11_device_->GetDeviceRemovedReason();
        if (FAILED(removed)) {
            error = HResultString("AMD D3D11 device", removed);
            return false;
        }
        prepared_ = true;
        return true;
    }

    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override {
        if (!initialized_ || !frame_ || !frame_->data[0]) return nullptr;
        return reinterpret_cast<ID3D11Texture2D*>(frame_->data[0]);
    }

    uint32_t GetCurrentInputSubresource() const noexcept override {
        if (!initialized_ || !frame_) return 0;
        return static_cast<uint32_t>(
            reinterpret_cast<uintptr_t>(frame_->data[1]));
    }

    detail::AmfSubmitStatus SubmitFrame(
        int64_t pts,
        bool force_keyframe,
        std::string& error) override {
        if (!initialized_ || !codec_context_ || !frame_) {
            error = "AMF frame submitted before initialization";
            return detail::AmfSubmitStatus::Error;
        }
        if (!prepared_ || !frame_->data[0]) {
            error = prepare_error_.empty()
                ? "AMF has no prepared D3D11 input texture"
                : prepare_error_;
            return detail::AmfSubmitStatus::Error;
        }
        if (d3d11_device_) {
            const HRESULT removed = d3d11_device_->GetDeviceRemovedReason();
            if (FAILED(removed)) {
                error = diagnostics::FormatHResultFailure(
                    "ID3D11Device::GetDeviceRemovedReason(AMF)", removed);
                return detail::AmfSubmitStatus::Error;
            }
        }

        frame_->pts = pts;
        frame_->pict_type = force_keyframe ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        const int result = avcodec_send_frame(codec_context_, frame_);
        if (result == AVERROR(EAGAIN)) {
            return detail::AmfSubmitStatus::NeedDrain;
        }
        if (result < 0) {
            error = std::string("avcodec_send_frame failed: ")
                + AvErrorString(result);
            return detail::AmfSubmitStatus::Error;
        }

        prepared_ = false;
        av_frame_unref(frame_);
        std::string prepare_error;
        if (!PrepareInputFrame(prepare_error)) {
            // The submitted frame remains valid inside FFmpeg/AMF. Preserve its
            // output, but make the next input attempt fail deterministically.
            prepare_error_ = std::move(prepare_error);
        } else {
            prepare_error_.clear();
        }
        return detail::AmfSubmitStatus::Accepted;
    }

    detail::AmfReceiveStatus ReceivePacket(
        detail::AmfPacketView& output,
        std::string& error) override {
        if (!codec_context_ || !packet_) {
            error = "AMF receive called without a codec context";
            return detail::AmfReceiveStatus::Error;
        }

        while (true) {
            av_packet_unref(packet_);
            const int result = avcodec_receive_packet(codec_context_, packet_);
            if (result == AVERROR(EAGAIN)) {
                return detail::AmfReceiveStatus::NeedMoreInput;
            }
            if (result == AVERROR_EOF) {
                return detail::AmfReceiveStatus::EndOfStream;
            }
            if (result < 0) {
                error = std::string("avcodec_receive_packet failed: ")
                    + AvErrorString(result);
                return detail::AmfReceiveStatus::Error;
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
                error = "AMF returned an encoded packet without a PTS";
                return detail::AmfReceiveStatus::Error;
            }
            output.data = packet_data;
            output.size = static_cast<uint32_t>(packet_size);
            output.pts = packet_->pts;
            output.dts = packet_->dts == AV_NOPTS_VALUE
                ? packet_->pts : packet_->dts;
            output.keyframe = (packet_->flags & AV_PKT_FLAG_KEY) != 0;
            return detail::AmfReceiveStatus::Packet;
        }
    }

    detail::AmfSubmitStatus SendEndOfStream(std::string& error) override {
        if (!codec_context_) return detail::AmfSubmitStatus::Accepted;
        const int result = avcodec_send_frame(codec_context_, nullptr);
        if (result == AVERROR(EAGAIN)) {
            return detail::AmfSubmitStatus::NeedDrain;
        }
        if (result == AVERROR_EOF) {
            return detail::AmfSubmitStatus::Accepted;
        }
        if (result < 0) {
            error = std::string("AMF flush submission failed: ")
                + AvErrorString(result);
            return detail::AmfSubmitStatus::Error;
        }
        return detail::AmfSubmitStatus::Accepted;
    }

    void Reset() noexcept override {
        initialized_ = false;
        prepared_ = false;
        d3d11_device_ = nullptr;
        prepare_error_.clear();
        normalized_packet_.clear();
        nals_scratch_.clear();
        if (frame_) av_frame_free(&frame_);
        if (packet_) av_packet_free(&packet_);
        if (codec_context_) avcodec_free_context(&codec_context_);
        if (hw_frames_ctx_) av_buffer_unref(&hw_frames_ctx_);
        if (hw_device_ctx_) av_buffer_unref(&hw_device_ctx_);
        if (converter_output_view_) converter_output_view_->Release();
        if (converter_texture_) converter_texture_->Release();
        if (video_processor_) video_processor_->Release();
        if (video_processor_enumerator_) video_processor_enumerator_->Release();
        if (video_context_) video_context_->Release();
        if (video_device_) video_device_->Release();
        if (copy_context_) copy_context_->Release();
        converter_output_view_ = nullptr;
        converter_texture_ = nullptr;
        video_processor_ = nullptr;
        video_processor_enumerator_ = nullptr;
        video_context_ = nullptr;
        video_device_ = nullptr;
        copy_context_ = nullptr;
        source_width_ = source_height_ = output_width_ = output_height_ = 0;
        destination_ = {};
        selection_ = nullptr;
    }

private:
    bool CreateConversionPipeline(
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        uint32_t fps,
        std::string& error) {
        HRESULT result = shared_device->QueryInterface(
            __uuidof(ID3D11VideoDevice),
            reinterpret_cast<void**>(&video_device_));
        if (FAILED(result) || !video_device_) {
            error = HResultString("AMD D3D11 video-device query", result);
            return false;
        }
        result = shared_context->QueryInterface(
            __uuidof(ID3D11VideoContext),
            reinterpret_cast<void**>(&video_context_));
        if (FAILED(result) || !video_context_) {
            error = HResultString("AMD D3D11 video-context query", result);
            return false;
        }
        copy_context_ = shared_context;
        copy_context_->AddRef();
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
        if (FAILED(result)) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,input)",
                result);
            return false;
        }
        if ((input_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT) == 0) {
            error = "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,input) reports unsupported format";
            return false;
        }
        result = video_processor_enumerator_->CheckVideoProcessorFormat(
            DXGI_FORMAT_NV12, &output_support);
        if (FAILED(result)) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(NV12,output)",
                result);
            return false;
        }
        if ((output_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT) == 0) {
            error = "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(NV12,output) reports unsupported format";
            return false;
        }
        result = video_device_->CreateVideoProcessor(
            video_processor_enumerator_, 0, &video_processor_);
        if (FAILED(result) || !video_processor_) {
            error = HResultString("ID3D11VideoDevice::CreateVideoProcessor", result);
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
            error = HResultString("AMF NV12 converter texture creation", result);
            return false;
        }
        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
        output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
        output_description.Texture2D.MipSlice = 0;
        result = video_device_->CreateVideoProcessorOutputView(
            converter_texture_, video_processor_enumerator_,
            &output_description, &converter_output_view_);
        if (FAILED(result) || !converter_output_view_) {
            error = HResultString(
                "ID3D11VideoDevice::CreateVideoProcessorOutputView", result);
            return false;
        }
        RECT source_rect{0, 0, static_cast<LONG>(source_width_),
            static_cast<LONG>(source_height_)};
        RECT destination_rect{
            static_cast<LONG>(destination_.left),
            static_cast<LONG>(destination_.top),
            static_cast<LONG>(destination_.left + destination_.width),
            static_cast<LONG>(destination_.top + destination_.height)};
        RECT target_rect{0, 0, static_cast<LONG>(output_width_),
            static_cast<LONG>(output_height_)};
        video_context_->VideoProcessorSetStreamFrameFormat(
            video_processor_, 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        video_context_->VideoProcessorSetStreamSourceRect(
            video_processor_, 0, TRUE, &source_rect);
        video_context_->VideoProcessorSetStreamDestRect(
            video_processor_, 0, TRUE, &destination_rect);
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

    static std::string HResultString(const char* operation, HRESULT result) {
        return diagnostics::FormatHResultFailure(operation, result);
    }

    bool PrepareInputFrame(std::string& error) {
        if (!frame_ || !hw_frames_ctx_) {
            error = "AMF input frame or hardware pool is unavailable";
            return false;
        }
        av_frame_unref(frame_);
        const int result = av_hwframe_get_buffer(hw_frames_ctx_, frame_, 0);
        if (result < 0) {
            error = std::string("could not acquire AMD D3D11 input texture: ")
                + AvErrorString(result);
            return false;
        }
        if (frame_->format != AV_PIX_FMT_D3D11 || !frame_->data[0]) {
            error = "FFmpeg hardware pool did not return a D3D11 texture";
            av_frame_unref(frame_);
            return false;
        }
        frame_->color_range = AVCOL_RANGE_MPEG;
        frame_->color_primaries = AVCOL_PRI_BT709;
        frame_->color_trc = AVCOL_TRC_BT709;
        frame_->colorspace = AVCOL_SPC_BT709;
        return true;
    }

    const AmfCodecSelection* selection_ = nullptr;
    AVBufferRef* hw_device_ctx_ = nullptr;
    AVBufferRef* hw_frames_ctx_ = nullptr;
    AVCodecContext* codec_context_ = nullptr;
    AVFrame* frame_ = nullptr;
    AVPacket* packet_ = nullptr;
    ID3D11Device* d3d11_device_ = nullptr;
    bool initialized_ = false;
    bool prepared_ = false;
    uint32_t source_width_ = 0;
    uint32_t source_height_ = 0;
    uint32_t output_width_ = 0;
    uint32_t output_height_ = 0;
    CaptureScaleRect destination_{};
    ID3D11VideoDevice* video_device_ = nullptr;
    ID3D11VideoContext* video_context_ = nullptr;
    ID3D11VideoProcessorEnumerator* video_processor_enumerator_ = nullptr;
    ID3D11VideoProcessor* video_processor_ = nullptr;
    ID3D11Texture2D* converter_texture_ = nullptr;
    ID3D11VideoProcessorOutputView* converter_output_view_ = nullptr;
    ID3D11DeviceContext* copy_context_ = nullptr;
    std::string prepare_error_;
    std::vector<NalSpan> nals_scratch_;
    std::vector<uint8_t> normalized_packet_;
};

} // namespace

const AmfCodecSelection* GetAmfCodecSelection(VideoCodec codec) noexcept {
    switch (codec) {
    case VideoCodec::H264: return &kH264Amf;
    case VideoCodec::HEVC: return &kHevcAmf;
    case VideoCodec::AV1: return &kAv1Amf;
    }
    return nullptr;
}

EncodedVideoConfig BuildAmfVideoConfig(
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
    const auto* selection = GetAmfCodecSelection(codec);
    if (selection) config.packet_format = selection->packet_format;
    config.codec_extradata = codec_extradata;
    return config;
}

namespace detail {

std::unique_ptr<IAmfCodecSession> CreateProductionAmfCodecSession() {
    return std::make_unique<FfmpegAmfCodecSession>();
}

} // namespace detail

FfmpegAmfReplayEncoder::FfmpegAmfReplayEncoder(VideoCodec codec)
    : FfmpegAmfReplayEncoder(
          codec, detail::CreateProductionAmfCodecSession()) {}

FfmpegAmfReplayEncoder::FfmpegAmfReplayEncoder(
    VideoCodec codec,
    std::unique_ptr<detail::IAmfCodecSession> session)
    : codec_(codec), session_(std::move(session)) {}

FfmpegAmfReplayEncoder::~FfmpegAmfReplayEncoder() {
    Shutdown();
}

bool FfmpegAmfReplayEncoder::Initialize(
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
        SetError("AMF codec session is unavailable");
        return false;
    }
    session_->Reset();
    if (cpu_input_mode) {
        SetError("AMD AMF supports only the same-adapter D3D11 hardware path");
        return false;
    }
    if (!callback) {
        SetError("AMF packet callback must not be null");
        return false;
    }
    const auto* selection = GetAmfCodecSelection(codec_);
    if (!selection) {
        SetError("unsupported AMF codec selection");
        return false;
    }

    LARGE_INTEGER frequency{};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) {
        SetError("QueryPerformanceFrequency failed for AMF timestamps");
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
        SetError(error.empty() ? "AMF initialization failed" : std::move(error));
        session_->Reset();
        return false;
    }
    if (candidate_config.codec != codec_
        || candidate_config.packet_format != selection->packet_format
        || !IsValidEncodedVideoConfig(candidate_config)) {
        SetError("AMF active codec/config does not match the requested codec");
        session_->Reset();
        return false;
    }

    video_config_ = std::move(candidate_config);
    packet_callback_ = std::move(callback);
    initialized_ = true;
    return true;
}

bool FfmpegAmfReplayEncoder::RequiresBackendGpuPreparation() const noexcept {
    return true;
}

bool FfmpegAmfReplayEncoder::PrepareGpuFrame(
    ID3D11Texture2D* source,
    uint32_t source_subresource) {
    gpu_frame_prepared_ = false;
    if (!initialized_ || flushing_ || !session_) {
        SetError("AMF GPU preparation called outside an active generation");
        return false;
    }
    std::string error;
    if (!session_->PrepareGpuFrame(source, source_subresource, error)) {
        SetError(error.empty() ? "AMF GPU conversion failed" : std::move(error));
        return false;
    }
    gpu_frame_prepared_ = true;
    return true;
}

bool FfmpegAmfReplayEncoder::EncodeFrame(int64_t present_qpc) {
    if (!initialized_ || flushing_) {
        SetError("AMF EncodeFrame called outside an active generation");
        return false;
    }
    if (!gpu_frame_prepared_ || !GetCurrentInputTexture()) {
        SetError("AMF EncodeFrame called before GPU preparation");
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

bool FfmpegAmfReplayEncoder::EncodeFrameCPU(
    const uint8_t*, uint32_t, int64_t) {
    SetError("AMD AMF CPU full-frame input is intentionally unsupported");
    return false;
}

ID3D11Texture2D* FfmpegAmfReplayEncoder::GetCurrentInputTexture() const noexcept {
    return initialized_ && session_
        ? session_->GetCurrentInputTexture()
        : nullptr;
}

uint32_t FfmpegAmfReplayEncoder::GetCurrentInputSubresource() const noexcept {
    return initialized_ && session_
        ? session_->GetCurrentInputSubresource()
        : 0;
}

void FfmpegAmfReplayEncoder::Shutdown() {
    if (initialized_) {
        Flush();
    }
    initialized_ = false;
    gpu_frame_prepared_ = false;
    flushing_ = false;
    if (session_) session_->Reset();
    packet_callback_ = {};
    submitted_timing_.clear();
}

EncodedVideoConfig FfmpegAmfReplayEncoder::GetVideoConfig() const {
    return video_config_;
}

ActiveEncoderInfo FfmpegAmfReplayEncoder::GetActiveEncoderInfo() const {
    const auto* selection = GetAmfCodecSelection(codec_);
    return {
        EncoderVendor::Amd,
        ReplayEncoderBackend::FfmpegAmf,
        codec_,
        initialized_,
        selection ? selection->encoder_name : "amf_unknown"};
}

std::string FfmpegAmfReplayEncoder::GetLastError() const {
    return last_error_;
}

bool FfmpegAmfReplayEncoder::GetEncodeEpoch(
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

bool FfmpegAmfReplayEncoder::IsInitialized() const {
    return initialized_;
}

int64_t FfmpegAmfReplayEncoder::ComputePts(int64_t present_qpc) {
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

int64_t FfmpegAmfReplayEncoder::WallQpcForPts(int64_t pts) {
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

bool FfmpegAmfReplayEncoder::DrainAvailable(bool flushing) {
    while (true) {
        detail::AmfPacketView packet;
        std::string error;
        const auto status = session_->ReceivePacket(packet, error);
        switch (status) {
        case detail::AmfReceiveStatus::Packet:
            if (packet.data && packet.size > 0 && packet_callback_) {
                packet_callback_(
                    packet.data,
                    packet.size,
                    packet.pts,
                    packet.keyframe,
                    WallQpcForPts(packet.pts));
            }
            break;
        case detail::AmfReceiveStatus::NeedMoreInput:
            return true;
        case detail::AmfReceiveStatus::EndOfStream:
            return true;
        case detail::AmfReceiveStatus::Error:
            SetError(error.empty() ? "AMF packet receive failed" : std::move(error));
            return false;
        }
        if (!flushing && !initialized_) return false;
    }
}

bool FfmpegAmfReplayEncoder::SubmitWithBackpressure(
    int64_t pts,
    bool force_keyframe) {
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string error;
        const auto status = session_->SubmitFrame(pts, force_keyframe, error);
        if (status == detail::AmfSubmitStatus::Accepted) return true;
        if (status == detail::AmfSubmitStatus::Error) {
            SetError(error.empty() ? "AMF frame submission failed" : std::move(error));
            return false;
        }
        if (!DrainAvailable(false)) return false;
    }
    SetError("AMF encoder remained backpressured after draining output");
    return false;
}

bool FfmpegAmfReplayEncoder::Flush() {
    flushing_ = true;
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string error;
        const auto status = session_->SendEndOfStream(error);
        if (status == detail::AmfSubmitStatus::Accepted) {
            const bool drained = DrainAvailable(true);
            flushing_ = false;
            return drained;
        }
        if (status == detail::AmfSubmitStatus::Error) {
            SetError(error.empty() ? "AMF flush failed" : std::move(error));
            flushing_ = false;
            return false;
        }
        if (!DrainAvailable(true)) {
            flushing_ = false;
            return false;
        }
    }
    SetError("AMF flush remained backpressured after draining output");
    flushing_ = false;
    return false;
}

void FfmpegAmfReplayEncoder::SetError(std::string error) {
    last_error_ = std::move(error);
    if (!last_error_.empty()) {
        std::cerr << "[AMF] " << last_error_ << std::endl;
    }
}

} // namespace fthr
