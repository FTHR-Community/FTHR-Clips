// Opt-in AMD isolation qualification helper.
//
// This helper is intentionally separate from the production executable.  It
// never creates the shared-memory mapping and never changes application/UI
// settings.  A uses the pinned FFmpeg AMF API directly with D3D11 hardware
// frames; B uses the production FfmpegAmfReplayEncoder conversion path; C
// starts CaptureEngine with an explicitly selected WGC monitor and saves a
// real replay.  Every path requires an actually enumerated AMD adapter.

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <winrt/base.h>
#include <wrl/client.h>

#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable: 4244)
#endif
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_d3d11va.h>
}
#ifdef _MSC_VER
#pragma warning(pop)
#endif

#include "capture_engine.h"
#include "continuous_recording_writer.h"
#include "ffmpeg_amf_replay_encoder.h"
#include "windows_monitor_resolver.h"

#include <algorithm>
#include <chrono>
#include <cwchar>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <limits>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

using Microsoft::WRL::ComPtr;

constexpr UINT kAmdVendorId = 0x1002;
constexpr int kExitAmdRequired = 20;
constexpr int kExitAmfUnavailable = 21;
constexpr int kExitHarnessFailure = 22;
constexpr uint32_t kMaxDimension = 4096;
constexpr uint32_t kMaxFrames = 20000;
constexpr uint32_t kMaxWaitSeconds = 3600;

struct Options {
    std::wstring mode;
    std::filesystem::path output;
    std::wstring monitor_path;
    int adapter_index = 0;
    uint32_t width = 640;
    uint32_t height = 360;
    uint32_t output_width = 960;
    uint32_t output_height = 540;
    uint32_t fps = 60;
    uint32_t frames = 120;
    uint32_t warmup_seconds = 8;
    uint32_t duration_seconds = 3;
};

struct AmdAdapter {
    ComPtr<IDXGIAdapter1> adapter;
    DXGI_ADAPTER_DESC1 description{};
    UINT enumeration_index = 0;
};

std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0,
        nullptr, nullptr);
    if (size <= 0) return {};
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(),
        size, nullptr, nullptr);
    return result;
}

std::string ErrorString(int error) {
    char buffer[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(error, buffer, sizeof(buffer));
    return buffer;
}

bool ParseU32(const std::wstring& text, uint32_t& value) {
    if (text.empty()) return false;
    wchar_t* end = nullptr;
    const unsigned long parsed = std::wcstoul(text.c_str(), &end, 10);
    if (!end || *end != L'\0' || parsed == 0
        || parsed > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    value = static_cast<uint32_t>(parsed);
    return true;
}

bool ParseInt(const std::wstring& text, int& value) {
    if (text.empty()) return false;
    wchar_t* end = nullptr;
    const long parsed = std::wcstol(text.c_str(), &end, 10);
    if (!end || *end != L'\0'
        || parsed < 0 || parsed > std::numeric_limits<int>::max()) {
        return false;
    }
    value = static_cast<int>(parsed);
    return true;
}

bool ParseOptions(int argc, wchar_t** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        const std::wstring key = argv[index];
        if (key == L"--mode" && index + 1 < argc) {
            options.mode = argv[++index];
        } else if (key == L"--output" && index + 1 < argc) {
            options.output = argv[++index];
        } else if (key == L"--monitor-path" && index + 1 < argc) {
            options.monitor_path = argv[++index];
        } else if (key == L"--adapter-index" && index + 1 < argc) {
            if (!ParseInt(argv[++index], options.adapter_index)) return false;
        } else if (key == L"--width" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.width)) return false;
        } else if (key == L"--height" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.height)) return false;
        } else if (key == L"--output-width" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.output_width)) return false;
        } else if (key == L"--output-height" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.output_height)) return false;
        } else if (key == L"--fps" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.fps)) return false;
        } else if (key == L"--frames" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.frames)) return false;
        } else if (key == L"--warmup-seconds" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.warmup_seconds)) return false;
        } else if (key == L"--duration-seconds" && index + 1 < argc) {
            if (!ParseU32(argv[++index], options.duration_seconds)) return false;
        } else {
            return false;
        }
    }
    if (options.mode.empty()
        || (options.mode != L"probe" && options.mode != L"a"
            && options.mode != L"b" && options.mode != L"c")) {
        return false;
    }
    if (options.mode != L"probe" && options.output.empty()) return false;
    if (options.mode == L"c" && options.monitor_path.empty()) return false;
    return options.adapter_index >= 0 && options.fps <= 360
        && options.width >= 64 && options.width <= kMaxDimension
        && options.height >= 64 && options.height <= kMaxDimension
        && options.output_width >= 64 && options.output_width <= kMaxDimension
        && options.output_height >= 64 && options.output_height <= kMaxDimension
        && options.frames >= 12 && options.frames <= kMaxFrames
        && options.warmup_seconds <= kMaxWaitSeconds
        && options.duration_seconds <= kMaxWaitSeconds
        && (options.width % 2 == 0) && (options.height % 2 == 0)
        && (options.output_width % 2 == 0) && (options.output_height % 2 == 0);
}

std::vector<AmdAdapter> EnumerateAmdAdapters() {
    std::vector<AmdAdapter> adapters;
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return adapters;
    for (UINT index = 0;; ++index) {
        ComPtr<IDXGIAdapter1> adapter;
        const HRESULT result = factory->EnumAdapters1(index, &adapter);
        if (result == DXGI_ERROR_NOT_FOUND) break;
        if (FAILED(result) || !adapter) continue;
        DXGI_ADAPTER_DESC1 description{};
        if (FAILED(adapter->GetDesc1(&description))) continue;
        if (description.VendorId != kAmdVendorId) continue;
        adapters.push_back({adapter, description, index});
    }
    return adapters;
}

void PrintAdapters(const std::vector<AmdAdapter>& adapters) {
    for (size_t index = 0; index < adapters.size(); ++index) {
        const auto& description = adapters[index].description;
        std::wcout << L"AMD_ADAPTER index=" << index
                   << L" dxgi_index=" << adapters[index].enumeration_index
                   << L" name=\"" << description.Description << L"\""
                   << L" vendor_id=0x" << std::hex << description.VendorId
                   << std::dec << L" luid_high=" << description.AdapterLuid.HighPart
                   << L" luid_low=" << description.AdapterLuid.LowPart
                   << std::endl;
    }
}

bool CreateAmdD3D11(
    const AmdAdapter& adapter,
    ComPtr<ID3D11Device>& device,
    ComPtr<ID3D11DeviceContext>& context,
    std::string& error) {
    const UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT
        | D3D11_CREATE_DEVICE_VIDEO_SUPPORT;
    const HRESULT result = D3D11CreateDevice(
        adapter.adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, flags, nullptr,
        0, D3D11_SDK_VERSION, &device, nullptr, &context);
    if (FAILED(result) || !device || !context) {
        error = "D3D11CreateDevice(AMD adapter) failed";
        return false;
    }
    return true;
}

std::vector<uint8_t> BuildPattern(
    uint32_t width, uint32_t height, uint32_t frame_index) {
    std::vector<uint8_t> pixels(
        static_cast<size_t>(width) * height * 4u, 0);
    const uint32_t bar_width = std::max<uint32_t>(4, width / 24);
    const uint32_t bar_x = (frame_index * std::max<uint32_t>(1, width / 18))
        % width;
    for (uint32_t y = 0; y < height; ++y) {
        for (uint32_t x = 0; x < width; ++x) {
            uint8_t b = 32, g = 32, r = 32;
            if (y < height / 2 && x < width / 2) {
                b = 220; g = 35; r = 35;
            } else if (y < height / 2) {
                b = 35; g = 220; r = 35;
            } else if (x < width / 2) {
                b = 35; g = 35; r = 220;
            } else {
                b = 175; g = 175; r = 175;
            }
            const bool in_bar = x >= bar_x && x < bar_x + bar_width;
            if (in_bar || (bar_x + bar_width > width && x < (bar_x + bar_width) % width)) {
                b = 235; g = 235; r = 235;
            }
            const size_t offset = (static_cast<size_t>(y) * width + x) * 4u;
            pixels[offset + 0] = b;
            pixels[offset + 1] = g;
            pixels[offset + 2] = r;
            pixels[offset + 3] = 255;
        }
    }
    return pixels;
}

bool CreateSourceTexture(
    ID3D11Device* device,
    uint32_t width,
    uint32_t height,
    const std::vector<uint8_t>& pixels,
    ComPtr<ID3D11Texture2D>& texture,
    std::string& error) {
    D3D11_TEXTURE2D_DESC description{};
    description.Width = width;
    description.Height = height;
    description.MipLevels = 1;
    description.ArraySize = 1;
    description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    description.SampleDesc.Count = 1;
    description.Usage = D3D11_USAGE_DEFAULT;
    description.BindFlags = D3D11_BIND_RENDER_TARGET
        | D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA initial{};
    initial.pSysMem = pixels.data();
    initial.SysMemPitch = width * 4u;
    const HRESULT result = device->CreateTexture2D(
        &description, &initial, &texture);
    if (FAILED(result) || !texture) {
        error = "CreateTexture2D(BGRA source) failed";
        return false;
    }
    return true;
}

using NalSpan = std::pair<const uint8_t*, int>;

void ParseAnnexBNals(
    const uint8_t* data, int size, std::vector<NalSpan>& nals) {
    nals.clear();
    if (!data || size <= 0) return;
    int cursor = 0;
    while (cursor < size) {
        int start = -1;
        for (; cursor < size - 2; ++cursor) {
            if (data[cursor] != 0 || data[cursor + 1] != 0) continue;
            if (data[cursor + 2] == 1) {
                start = cursor + 3;
                cursor += 3;
                break;
            }
            if (cursor + 3 < size && data[cursor + 2] == 0
                && data[cursor + 3] == 1) {
                start = cursor + 4;
                cursor += 4;
                break;
            }
        }
        if (start < 0) break;
        int end = size;
        for (int index = start; index < size - 2; ++index) {
            if (data[index] == 0 && data[index + 1] == 0
                && (data[index + 2] == 1
                    || (index + 3 < size && data[index + 2] == 0
                        && data[index + 3] == 1))) {
                end = index;
                break;
            }
        }
        if (end > start) nals.push_back({data + start, end - start});
        cursor = end;
    }
}

bool ConvertAnnexBToAvcc(
    const uint8_t* data,
    int size,
    std::vector<uint8_t>& output) {
    std::vector<NalSpan> nals;
    ParseAnnexBNals(data, size, nals);
    if (nals.empty()) return false;
    size_t output_size = 0;
    for (const auto& nal : nals) {
        if (nal.second > 0 && (nal.first[0] & 0x1Fu) != 7
            && (nal.first[0] & 0x1Fu) != 8) {
            output_size += 4u + static_cast<size_t>(nal.second);
        }
    }
    output.resize(output_size);
    uint8_t* destination = output.data();
    for (const auto& nal : nals) {
        if (nal.second <= 0 || (nal.first[0] & 0x1Fu) == 7
            || (nal.first[0] & 0x1Fu) == 8) continue;
        const uint32_t nal_size = static_cast<uint32_t>(nal.second);
        destination[0] = static_cast<uint8_t>(nal_size >> 24);
        destination[1] = static_cast<uint8_t>(nal_size >> 16);
        destination[2] = static_cast<uint8_t>(nal_size >> 8);
        destination[3] = static_cast<uint8_t>(nal_size);
        std::memcpy(destination + 4, nal.first, nal.second);
        destination += 4 + nal.second;
    }
    return !output.empty();
}

std::vector<uint8_t> BuildAvccExtradata(const AVCodecContext* codec) {
    if (!codec || !codec->extradata || codec->extradata_size <= 0) return {};
    const uint8_t* data = codec->extradata;
    const int size = codec->extradata_size;
    if (size >= 7 && data[0] == 1) {
        return {data, data + size};
    }
    std::vector<NalSpan> nals;
    ParseAnnexBNals(data, size, nals);
    const uint8_t* sps = nullptr;
    const uint8_t* pps = nullptr;
    int sps_size = 0;
    int pps_size = 0;
    for (const auto& nal : nals) {
        if (nal.second <= 0) continue;
        const uint8_t type = nal.first[0] & 0x1Fu;
        if (type == 7 && !sps) {
            sps = nal.first;
            sps_size = nal.second;
        } else if (type == 8 && !pps) {
            pps = nal.first;
            pps_size = nal.second;
        }
    }
    if (!sps || sps_size < 4 || !pps || pps_size <= 0) return {};
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

bool IsAnnexB(const AVPacket* packet) {
    return packet && packet->size >= 3 && packet->data[0] == 0
        && packet->data[1] == 0
        && (packet->data[2] == 1
            || (packet->size >= 4 && packet->data[2] == 0
                && packet->data[3] == 1));
}

bool ConfigureDirectAmf(
    ID3D11Device* device,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    AVBufferRef*& device_ctx,
    AVBufferRef*& frames_ctx,
    AVCodecContext*& codec,
    std::string& error) {
    const AVCodec* encoder = avcodec_find_encoder_by_name("h264_amf");
    if (!encoder) {
        error = "pinned FFmpeg h264_amF encoder is unavailable";
        return false;
    }
    device_ctx = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
    if (!device_ctx) {
        error = "av_hwdevice_ctx_alloc(D3D11VA) failed";
        return false;
    }
    auto* hw_device = reinterpret_cast<AVHWDeviceContext*>(device_ctx->data);
    auto* d3d11_device = reinterpret_cast<AVD3D11VADeviceContext*>(
        hw_device->hwctx);
    device->AddRef();
    d3d11_device->device = device;
    int result = av_hwdevice_ctx_init(device_ctx);
    if (result < 0) {
        error = "av_hwdevice_ctx_init failed: " + ErrorString(result);
        return false;
    }
    frames_ctx = av_hwframe_ctx_alloc(device_ctx);
    if (!frames_ctx) {
        error = "av_hwframe_ctx_alloc failed";
        return false;
    }
    auto* frames = reinterpret_cast<AVHWFramesContext*>(frames_ctx->data);
    frames->format = AV_PIX_FMT_D3D11;
    frames->sw_format = AV_PIX_FMT_BGRA;
    frames->width = static_cast<int>(width);
    frames->height = static_cast<int>(height);
    frames->initial_pool_size = 8;
    result = av_hwframe_ctx_init(frames_ctx);
    if (result < 0) {
        error = "av_hwframe_ctx_init failed: " + ErrorString(result);
        return false;
    }
    codec = avcodec_alloc_context3(encoder);
    if (!codec) {
        error = "avcodec_alloc_context3(h264_amf) failed";
        return false;
    }
    codec->codec_type = AVMEDIA_TYPE_VIDEO;
    codec->codec_id = encoder->id;
    codec->width = static_cast<int>(width);
    codec->height = static_cast<int>(height);
    codec->pix_fmt = AV_PIX_FMT_D3D11;
    codec->profile = AV_PROFILE_H264_MAIN;
    codec->time_base = AVRational{1, static_cast<int>(fps)};
    codec->framerate = AVRational{static_cast<int>(fps), 1};
    codec->bit_rate = 8'000'000;
    codec->rc_max_rate = codec->bit_rate;
    codec->gop_size = static_cast<int>(fps);
    codec->max_b_frames = 0;
    codec->color_range = AVCOL_RANGE_MPEG;
    codec->color_primaries = AVCOL_PRI_BT709;
    codec->color_trc = AVCOL_TRC_BT709;
    codec->colorspace = AVCOL_SPC_BT709;
    codec->flags |= AV_CODEC_FLAG_LOW_DELAY | AV_CODEC_FLAG_GLOBAL_HEADER;
    codec->hw_device_ctx = av_buffer_ref(device_ctx);
    codec->hw_frames_ctx = av_buffer_ref(frames_ctx);
    if (!codec->hw_device_ctx || !codec->hw_frames_ctx) {
        error = "could not retain D3D11 hardware contexts";
        return false;
    }
    AVDictionary* options = nullptr;
    av_dict_set(&options, "usage", "ultralowlatency", 0);
    av_dict_set(&options, "quality", "speed", 0);
    av_dict_set(&options, "rc", "cbr", 0);
    av_dict_set(&options, "forced_idr", "1", 0);
    av_dict_set(&options, "async_depth", "4", 0);
    result = avcodec_open2(codec, encoder, &options);
    if (result < 0) {
        error = "avcodec_open2(h264_amf) failed: " + ErrorString(result);
        av_dict_free(&options);
        return false;
    }
    if (options) {
        const AVDictionaryEntry* entry = av_dict_get(
            options, "", nullptr, AV_DICT_IGNORE_SUFFIX);
        error = std::string("h264_amf rejected option ")
            + (entry && entry->key ? entry->key : "unknown");
        av_dict_free(&options);
        return false;
    }
    return true;
}

int RunDirectA(const Options& options, const AmdAdapter& adapter) {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    std::string error;
    if (!CreateAmdD3D11(adapter, device, context, error)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=" << error << std::endl;
        return kExitHarnessFailure;
    }
    AVBufferRef* device_ctx = nullptr;
    AVBufferRef* frames_ctx = nullptr;
    AVCodecContext* codec = nullptr;
    if (!ConfigureDirectAmf(
            device.Get(), options.width, options.height, options.fps,
            device_ctx, frames_ctx, codec, error)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=" << error << std::endl;
        avcodec_free_context(&codec);
        av_buffer_unref(&frames_ctx);
        av_buffer_unref(&device_ctx);
        return error.find("encoder is unavailable") != std::string::npos
            ? kExitAmfUnavailable : kExitHarnessFailure;
    }
    const auto extradata = BuildAvccExtradata(codec);
    if (extradata.empty()) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=missing_avcC_extradata"
                  << std::endl;
        avcodec_free_context(&codec);
        av_buffer_unref(&frames_ctx);
        av_buffer_unref(&device_ctx);
        return kExitHarnessFailure;
    }
    fthr::EncodedVideoConfig video_config;
    video_config.codec = fthr::VideoCodec::H264;
    video_config.width = options.width;
    video_config.height = options.height;
    video_config.frame_rate = {static_cast<int32_t>(options.fps), 1};
    video_config.time_base = {1, static_cast<int32_t>(options.fps)};
    video_config.bitrate_kbps = 8000;
    video_config.max_keyframe_interval_frames = options.fps;
    video_config.codec_extradata = extradata;
    fthr::ContinuousRecordingWriter writer;
    if (!writer.Start(options.output, video_config)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=writer_start_failed "
                  << writer.LastError() << std::endl;
        avcodec_free_context(&codec);
        av_buffer_unref(&frames_ctx);
        av_buffer_unref(&device_ctx);
        return kExitHarnessFailure;
    }
    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    if (!frame || !packet) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=ffmpeg_alloc_failed"
                  << std::endl;
        writer.Stop();
        av_frame_free(&frame);
        av_packet_free(&packet);
        avcodec_free_context(&codec);
        av_buffer_unref(&frames_ctx);
        av_buffer_unref(&device_ctx);
        return kExitHarnessFailure;
    }
    uint64_t packet_count = 0;
    int64_t first_pts = -1;
    int64_t last_pts = -1;
    auto drain = [&]() -> bool {
        while (true) {
            const int result = avcodec_receive_packet(codec, packet);
            if (result == 0) {
                std::vector<uint8_t> converted;
                const uint8_t* payload = packet->data;
                int payload_size = packet->size;
                if (IsAnnexB(packet)) {
                    if (!ConvertAnnexBToAvcc(
                            packet->data, packet->size, converted)) {
                        error = "could not convert AMF Annex-B packet to AVCC";
                        av_packet_unref(packet);
                        return false;
                    }
                    payload = converted.data();
                    payload_size = static_cast<int>(converted.size());
                }
                if (!payload || payload_size <= 0
                    || packet->pts == AV_NOPTS_VALUE
                    || !writer.PushVideo(
                        payload, static_cast<uint32_t>(payload_size), packet->pts,
                        (packet->flags & AV_PKT_FLAG_KEY) != 0)) {
                    error = "AMF direct packet could not be queued";
                    av_packet_unref(packet);
                    return false;
                }
                if (first_pts < 0) first_pts = packet->pts;
                last_pts = packet->pts;
                ++packet_count;
                av_packet_unref(packet);
                continue;
            }
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
            error = "avcodec_receive_packet failed: " + ErrorString(result);
            return false;
        }
    };
    bool success = true;
    for (uint32_t index = 0; index < options.frames && success; ++index) {
        av_frame_unref(frame);
        const int allocation = av_hwframe_get_buffer(frames_ctx, frame, 0);
        if (allocation < 0) {
            error = "av_hwframe_get_buffer failed: " + ErrorString(allocation);
            success = false;
            break;
        }
        auto* texture = reinterpret_cast<ID3D11Texture2D*>(frame->data[0]);
        const UINT subresource = static_cast<UINT>(
            reinterpret_cast<uintptr_t>(frame->data[1]));
        const auto pixels = BuildPattern(options.width, options.height, index);
        context->UpdateSubresource(
            texture, subresource, nullptr, pixels.data(), options.width * 4u, 0);
        frame->pts = static_cast<int64_t>(index);
        frame->pict_type = index == 0 ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        int submit = avcodec_send_frame(codec, frame);
        if (submit == AVERROR(EAGAIN)) {
            success = drain();
            if (success) submit = avcodec_send_frame(codec, frame);
        }
        if (submit < 0) {
            error = "avcodec_send_frame failed: " + ErrorString(submit);
            success = false;
        } else {
            av_frame_unref(frame);
            success = drain();
        }
    }
    if (success) {
        int submit = avcodec_send_frame(codec, nullptr);
        if (submit == AVERROR(EAGAIN)) {
            success = drain();
            if (success) submit = avcodec_send_frame(codec, nullptr);
        }
        if (submit < 0) {
            error = "AMF flush failed: " + ErrorString(submit);
            success = false;
        } else {
            success = drain();
        }
    }
    av_frame_free(&frame);
    av_packet_free(&packet);
    const bool stopped = writer.Stop();
    if (!success || !stopped || packet_count < options.frames
        || first_pts < 0 || last_pts < static_cast<int64_t>(options.frames - 1)) {
        if (success && stopped && packet_count < options.frames) {
            error = "AMF direct packet coverage below submitted frame count";
        } else if (success && stopped && (first_pts < 0
                   || last_pts < static_cast<int64_t>(options.frames - 1))) {
            error = "AMF direct PTS coverage did not reach submitted frame range";
        }
        if (!stopped && error.empty()) error = writer.LastError();
        std::cerr << "AMD_ISOLATION_FAILURE stage=A reason=" << error << std::endl;
        avcodec_free_context(&codec);
        av_buffer_unref(&frames_ctx);
        av_buffer_unref(&device_ctx);
        return kExitHarnessFailure;
    }
    const auto& description = adapter.description;
    std::wcout << L"AMD_ISOLATION stage=A status=pass vendor=AMD backend=ffmpeg-amf"
               << L" codec=h264_amf fps=" << options.fps
               << L" mode=direct_gpu adapter=\"" << description.Description
                 << L"\" adapter_luid_high=" << description.AdapterLuid.HighPart
                 << L" adapter_luid_low=" << description.AdapterLuid.LowPart
                 << L" output=\"" << options.output.wstring() << L"\""
               << L" width=" << options.width << L" height=" << options.height
               << L" frames=" << options.frames << L" packets=" << packet_count
               << L" first_pts=" << first_pts << L" last_pts=" << last_pts
               << std::endl;
    avcodec_free_context(&codec);
    av_buffer_unref(&frames_ctx);
    av_buffer_unref(&device_ctx);
    return 0;
}

int RunProductionB(const Options& options, const AmdAdapter& adapter) {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    std::string error;
    if (!CreateAmdD3D11(adapter, device, context, error)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=B reason=" << error << std::endl;
        return kExitHarnessFailure;
    }
    fthr::ContinuousRecordingWriter writer;
    fthr::FfmpegAmfReplayEncoder encoder(fthr::VideoCodec::H264);
    fthr::EncoderConfig config;
    config.src_width = options.width;
    config.src_height = options.height;
    config.enc_width = options.output_width;
    config.enc_height = options.output_height;
    config.fps = options.fps;
    config.bitrate_kbps = 8000;
    config.scaling_mode = 0;
    uint64_t packet_count = 0;
    int64_t first_pts = -1;
    int64_t last_pts = -1;
    bool writer_push_failed = false;
    if (!encoder.Initialize(
            config, device.Get(), context.Get(),
            [&](const uint8_t* data, uint32_t size, int64_t pts, bool key, int64_t) {
                if (first_pts < 0) first_pts = pts;
                last_pts = pts;
                if (!writer.PushVideo(data, size, pts, key)) {
                    writer_push_failed = true;
                    return;
                }
                ++packet_count;
            }, false)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=B reason="
                  << encoder.GetLastError() << std::endl;
        return kExitHarnessFailure;
    }
    if (!writer.Start(options.output, encoder.GetVideoConfig())) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=B reason=writer_start_failed "
                  << writer.LastError() << std::endl;
        encoder.Shutdown();
        return kExitHarnessFailure;
    }
    const auto first_pattern = BuildPattern(options.width, options.height, 0);
    ComPtr<ID3D11Texture2D> source;
    if (!CreateSourceTexture(
            device.Get(), options.width, options.height, first_pattern,
            source, error)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=B reason=" << error << std::endl;
        encoder.Shutdown();
        writer.Stop();
        return kExitHarnessFailure;
    }
    LARGE_INTEGER frequency{};
    LARGE_INTEGER start{};
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    bool success = true;
    for (uint32_t index = 0; index < options.frames; ++index) {
        const auto pixels = BuildPattern(options.width, options.height, index);
        context->UpdateSubresource(
            source.Get(), 0, nullptr, pixels.data(), options.width * 4u, 0);
        const int64_t qpc = start.QuadPart
            + static_cast<int64_t>(index)
                * frequency.QuadPart / static_cast<int64_t>(options.fps);
        if (!encoder.PrepareGpuFrame(source.Get(), 0)
            || !encoder.EncodeFrame(qpc)) {
            std::cerr << "AMD_ISOLATION_FAILURE stage=B frame=" << index
                      << " reason=" << encoder.GetLastError() << std::endl;
            success = false;
            break;
        }
    }
    const auto active = encoder.GetActiveEncoderInfo();
    encoder.Shutdown();
    const bool stopped = writer.Stop();
    if (!success || !stopped || writer.HasFailed() || writer_push_failed
        || packet_count < options.frames || first_pts < 0
        || last_pts < static_cast<int64_t>(options.frames - 1)) {
        if (!stopped && error.empty()) error = writer.LastError();
        if (error.empty() && writer_push_failed) error = "writer rejected an AMF packet";
        if (error.empty() && packet_count < options.frames) {
            error = "production AMF packet coverage below submitted frame count";
        }
        if (error.empty() && (first_pts < 0
                   || last_pts < static_cast<int64_t>(options.frames - 1))) {
            error = "production AMF PTS coverage did not reach submitted frame range";
        }
        std::cerr << "AMD_ISOLATION_FAILURE stage=B reason="
                  << (error.empty() ? writer.LastError() : error) << std::endl;
        return kExitHarnessFailure;
    }
    std::wcout << L"AMD_ISOLATION stage=B status=pass vendor=AMD backend=ffmpeg-amf"
               << L" codec=h264_amf fps=" << options.fps
               << L" mode=production_transform adapter=\""
               << adapter.description.Description << L"\" adapter_luid_high="
               << adapter.description.AdapterLuid.HighPart
                 << L" adapter_luid_low=" << adapter.description.AdapterLuid.LowPart
                 << L" output=\"" << options.output.wstring() << L"\" width="
               << options.output_width << L" height=" << options.output_height
               << L" source_width=" << options.width << L" source_height="
               << options.height << L" frames=" << options.frames
               << L" packets=" << packet_count << L" first_pts=" << first_pts
               << L" last_pts=" << last_pts << L" active_name=\""
               << std::wstring(active.name.begin(), active.name.end())
               << L"\"" << std::endl;
    return 0;
}

bool ResolveMonitorAmdAdapter(
    const Options& options,
    const std::vector<AmdAdapter>& adapters,
    size_t& adapter_index,
    fthr::monitor::AdapterLuid& monitor_luid,
    uint32_t& source_width,
    uint32_t& source_height,
    std::string& error) {
    fthr::monitor::WindowsMonitorTopologySource source;
    const auto topology = source.QueryActiveTopology();
    if (!topology.ok()) {
        error = "monitor_topology_query_failed:" + topology.diagnostic;
        return false;
    }
    fthr::monitor::MonitorResolver resolver(source);
    const auto resolved = resolver.Resolve(options.monitor_path);
    if (!resolved.ok()) {
        error = "monitor_resolve_failed:" + resolved.diagnostic;
        return false;
    }
    if (!resolved.monitor.adapter_luid_available) {
        error = "monitor_adapter_luid_unavailable";
        return false;
    }
    monitor_luid = resolved.monitor.adapter_luid;
    source_width = resolved.monitor.width;
    source_height = resolved.monitor.height;
    for (size_t index = 0; index < adapters.size(); ++index) {
        const auto& luid = adapters[index].description.AdapterLuid;
        if (static_cast<uint32_t>(luid.LowPart) == monitor_luid.low_part
            && static_cast<int32_t>(luid.HighPart) == monitor_luid.high_part) {
            adapter_index = index;
            return true;
        }
    }
    error = "monitor_adapter_is_not_enumerated_amd";
    return false;
}

int RunCaptureC(
    const Options& options,
    const std::vector<AmdAdapter>& adapters) {
    size_t monitor_adapter_index = 0;
    fthr::monitor::AdapterLuid monitor_luid{};
    uint32_t source_width = 0;
    uint32_t source_height = 0;
    std::string monitor_error;
    if (!ResolveMonitorAmdAdapter(
            options, adapters, monitor_adapter_index, monitor_luid,
            source_width, source_height, monitor_error)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=" << monitor_error
                  << std::endl;
        return kExitHarnessFailure;
    }
    if (monitor_adapter_index != static_cast<size_t>(options.adapter_index)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=monitor_adapter_index_mismatch"
                  << " requested=" << options.adapter_index
                  << " resolved=" << monitor_adapter_index << std::endl;
        return kExitHarnessFailure;
    }
    fthr::CaptureConfig config;
    config.framerate = options.fps;
    config.buffer_seconds = std::max<uint32_t>(
        options.duration_seconds + 3, 6);
    config.target_width = options.output_width;
    config.target_height = options.output_height;
    config.video_codec = fthr::VideoCodec::H264;
    config.encoder_preference = fthr::EncoderPreference::Amd;
    config.capture_backend_preference =
        fthr::WindowsCaptureBackendPreference::Wgc;
    config.monitor_device_path = options.monitor_path;
    config.audio_enabled = false;
    config.separate_audio_enabled = false;
    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason="
                  << engine.GetLastStartupError() << std::endl;
        return kExitHarnessFailure;
    }
    const auto capability = engine.GetActiveReplayCapability();
    const bool exact_backend = engine.GetStartupCaptureBackend() == "WGC_MONITOR";
    const bool exact_encoder = capability.active_backend
        == fthr::ReplayEncoderBackend::FfmpegAmf
        && capability.encoder_vendor == fthr::EncoderVendor::Amd
        && capability.same_adapter;
    std::cout << "AMD_ISOLATION stage=C status=initialized vendor=AMD"
              << " backend=ffmpeg-amf codec=h264 fps=" << options.fps
              << " mode=real_wgc_saveclip capture_backend="
              << engine.GetStartupCaptureBackend()
              << " target_width=" << options.output_width
              << " target_height=" << options.output_height
              << " source_width=" << source_width
              << " source_height=" << source_height
              << " monitor_adapter_index=" << monitor_adapter_index
              << " monitor_adapter_luid_high=" << monitor_luid.high_part
              << " monitor_adapter_luid_low=" << monitor_luid.low_part
              << " monitor_path=" << Utf8(options.monitor_path)
              << " controlled_content=false" << std::endl;
    if (!exact_backend || !exact_encoder) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=backend_identity_not_exact"
                  << std::endl;
        engine.Shutdown();
        return kExitHarnessFailure;
    }
    std::this_thread::sleep_for(std::chrono::seconds(options.warmup_seconds));
    std::cout << "AMD_ISOLATION SAVE_ATTEMPT stage=C attempt=1 denominator=1"
              << std::endl;
    if (!engine.SaveClip(options.output.c_str(), options.duration_seconds, nullptr)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=SaveClip_failed"
                  << std::endl;
        engine.Shutdown();
        return kExitHarnessFailure;
    }
    const auto save_deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(std::min<uint32_t>(
            options.duration_seconds + 30, kMaxWaitSeconds));
    bool save_completed = false;
    while (std::chrono::steady_clock::now() < save_deadline) {
        std::error_code file_error;
        if (std::filesystem::is_regular_file(options.output, file_error)
            && std::filesystem::file_size(options.output, file_error) > 0) {
            save_completed = true;
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    if (!save_completed) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=SaveClip_completion_timeout"
                  << std::endl;
        engine.Shutdown();
        return kExitHarnessFailure;
    }
    engine.Shutdown();
    if (!std::filesystem::is_regular_file(options.output)) {
        std::cerr << "AMD_ISOLATION_FAILURE stage=C reason=clip_missing_after_shutdown"
                  << std::endl;
        return kExitHarnessFailure;
    }
    std::cout << "AMD_ISOLATION stage=C status=transport_decode_pass_content_pending vendor=AMD"
              << " backend=ffmpeg-amf codec=h264 fps=" << options.fps
              << " mode=real_wgc_saveclip capture_backend=WGC_MONITOR"
              << " target_width=" << options.output_width
              << " target_height=" << options.output_height
              << " source_width=" << source_width
              << " source_height=" << source_height
              << " monitor_adapter_index=" << monitor_adapter_index
              << " monitor_adapter_luid_high=" << monitor_luid.high_part
              << " monitor_adapter_luid_low=" << monitor_luid.low_part
              << " controlled_content=false output="
              << Utf8(options.output.wstring()) << std::endl;
    return 0;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    Options options;
    if (!ParseOptions(argc, argv, options)) {
        std::cerr << "usage: --mode probe|a|b|c --output PATH [--monitor-path PATH]"
                  << " [--adapter-index N] [--width N --height N --output-width N"
                  << " --output-height N --fps N --frames N]" << std::endl;
        return kExitHarnessFailure;
    }
    if (options.mode == L"c") {
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
    }
    if (options.mode != L"probe") {
        std::error_code output_error;
        if (std::filesystem::exists(options.output, output_error)) {
            std::cerr << "AMD_ISOLATION_FAILURE reason=output_already_exists"
                      << std::endl;
            return kExitHarnessFailure;
        }
        if (output_error) {
            std::cerr << "AMD_ISOLATION_FAILURE reason=output_path_check_failed"
                      << std::endl;
            return kExitHarnessFailure;
        }
    }
    const auto adapters = EnumerateAmdAdapters();
    PrintAdapters(adapters);
    if (adapters.empty()) {
        std::cerr << "AMD_HARDWARE_REQUIRED reason=no_enumerated_amd_adapter"
                  << std::endl;
        return kExitAmdRequired;
    }
    if (options.mode == L"probe") {
        std::cout << "AMD_ISOLATION stage=probe status=pass vendor=AMD"
                  << " backend=ffmpeg-amf adapter_count=" << adapters.size()
                  << std::endl;
        return 0;
    }
    if (options.adapter_index < 0
        || static_cast<size_t>(options.adapter_index) >= adapters.size()) {
        std::cerr << "AMD_HARDWARE_REQUIRED reason=adapter_index_not_found"
                  << std::endl;
        return kExitAmdRequired;
    }
    const auto& adapter = adapters[static_cast<size_t>(options.adapter_index)];
    if (options.mode == L"a") return RunDirectA(options, adapter);
    if (options.mode == L"b") return RunProductionB(options, adapter);
    if (options.mode == L"c") return RunCaptureC(options, adapters);
    std::cerr << "AMD_ISOLATION_FAILURE reason=unknown_mode" << std::endl;
    return kExitHarnessFailure;
}
