// Isolated, bounded qualification of the selected hardware replay input path.
#include "native_encoder_probe.h"
#include "replay_encoder.h"
#include "windows_monitor_resolver.h"
#include "bgra_direct_copy.h"
#include "capture_scale_geometry.h"
#include "hdr_frame_converter.h"
#include <dxgi1_6.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi1_2.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstring>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/pixdesc.h>
}

namespace fthr {
namespace {
using Microsoft::WRL::ComPtr;
constexpr uint32_t kFrames = 48;
constexpr size_t kPacketBudget = 64 * 1024 * 1024;

bool Number(const char* text, uint32_t& value, uint32_t maximum) {
    if (!text || !*text) return false;
    const char* end = text + std::strlen(text);
    const auto parsed = std::from_chars(text, end, value);
    return parsed.ec == std::errc{} && parsed.ptr == end && value <= maximum;
}
std::wstring Wide(const char* text) {
    const int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, nullptr, 0);
    if (size <= 1) return {};
    std::wstring value(static_cast<size_t>(size), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, value.data(), size)) return {};
    value.pop_back();
    return value;
}
std::string JsonText(const char* text) {
    std::string escaped;
    for (const unsigned char value : std::string(text)) {
        if (value == '\\' || value == '"') { escaped += '\\'; escaped += static_cast<char>(value); }
        else if (value >= 32) escaped += static_cast<char>(value);
    }
    return escaped;
}

// Check every presentation timestamp and the changing gray input. Packet
// presence alone would also accept black/frozen frames or missing clip tails.
bool Decode(const std::vector<detail::EncoderProbePacket>& packets, const EncodedVideoConfig& config,
            uint32_t& decoded) {
    const AVCodecID id = config.codec == VideoCodec::H264 ? AV_CODEC_ID_H264
        : config.codec == VideoCodec::HEVC ? AV_CODEC_ID_HEVC : AV_CODEC_ID_AV1;
    // Named software decoders avoid accidentally validating a second GPU path.
    const AVCodec* codec = avcodec_find_decoder_by_name(
        id == AV_CODEC_ID_H264 ? "h264" : id == AV_CODEC_ID_HEVC ? "hevc" : "libdav1d");
    if (!codec) return false;
    AVCodecContext* context = avcodec_alloc_context3(codec);
    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    bool ok = context && frame && packet;
    if (ok) {
        context->thread_count = 1;
        context->width = static_cast<int>(config.width);
        context->height = static_cast<int>(config.height);
        context->extradata_size = static_cast<int>(config.codec_extradata.size());
        context->extradata = static_cast<uint8_t*>(av_mallocz(
            config.codec_extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
        ok = context->extradata != nullptr;
        if (ok) std::memcpy(context->extradata, config.codec_extradata.data(), config.codec_extradata.size());
        if (ok) ok = avcodec_open2(context, codec, nullptr) >= 0;
    }
    std::array<bool, kFrames> seen{};
    auto receive = [&]() {
        while (true) {
            const int result = avcodec_receive_frame(context, frame);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
            if (result < 0) return false;
            const int64_t pts = frame->best_effort_timestamp;
            const auto* format = av_pix_fmt_desc_get(static_cast<AVPixelFormat>(frame->format));
            const int sample_bytes = config.hdr10 ? 2 : 1;
            if (pts < 0 || pts >= kFrames || seen[static_cast<size_t>(pts)]
                || frame->width != static_cast<int>(config.width)
                || frame->height != static_cast<int>(config.height)
                || frame->decode_error_flags || !format
                || format->comp[0].depth != (config.hdr10 ? 10 : 8)
                || format->comp[0].plane != 0 || format->comp[0].step != sample_bytes
                || !frame->data[0] || frame->linesize[0] < frame->width * sample_bytes
                || (config.hdr10 && (frame->color_range != AVCOL_RANGE_MPEG
                    || frame->color_primaries != AVCOL_PRI_BT2020
                    || frame->color_trc != AVCOL_TRC_SMPTE2084
                    || frame->colorspace != AVCOL_SPC_BT2020_NCL))) {
                std::cerr << "FTHR_ENCODER_PROBE_FAILURE reason=decoded_frame_contract pts="
                    << pts << " decoded=" << decoded << std::endl;
                return false;
            }
            const auto* pixel = frame->data[0] + static_cast<size_t>(frame->height / 2)
                * frame->linesize[0] + (frame->width / 2) * sample_bytes;
            const int center = config.hdr10 ? pixel[0] + 256 * pixel[1] : pixel[0];
            const bool bright = (pts / 6) % 2 != 0;
            // Limited-range PQ codes for 1000 and 20 nits respectively.
            const int expected = config.hdr10 ? (bright ? 723 : 377) : (bright ? 200 : 40);
            // Both full and studio luma ranges are accepted; a frozen/black
            // input cannot match the alternating levels for all 48 timestamps.
            if (std::abs(center - expected) > (config.hdr10 ? 12 : 28)) {
                std::cerr << "FTHR_ENCODER_PROBE_FAILURE reason=decoded_luma pts=" << pts
                    << " actual=" << center << " expected=" << expected << std::endl;
                return false;
            }
            seen[static_cast<size_t>(pts)] = true;
            ++decoded;
            av_frame_unref(frame);
        }
    };
    for (const auto& input : packets) {
        if (!ok) break;
        ok = av_new_packet(packet, static_cast<int>(input.bytes.size())) >= 0;
        if (ok) {
            std::memcpy(packet->data, input.bytes.data(), input.bytes.size());
            packet->pts = packet->dts = input.pts;
            ok = avcodec_send_packet(context, packet) >= 0 && receive();
        }
        av_packet_unref(packet);
    }
    if (ok) ok = avcodec_send_packet(context, nullptr) >= 0 && receive();
    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&context);
    return ok && decoded == kFrames;
}
} // namespace

bool detail::ValidateEncoderProbePackets(const std::vector<EncoderProbePacket>& packets,
    const EncodedVideoConfig& config, uint32_t& decoded) {
    decoded = 0;
    return IsValidEncodedVideoConfig(config) && packets.size() <= kFrames * 2
        && Decode(packets, config, decoded);
}

int RunNativeEncoderProbe(int argc, char* argv[]) {
    // This safe nested probe also lets Python qualify exclusion enforcement
    // without passing an unknown top-level command to older capture engines.
    if (argc == 3 && std::strcmp(argv[2], "--audio-exclusions-capability") == 0) {
        std::cout << "FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1" << std::endl;
        return 0;
    }
    if (argc == 3 && std::strcmp(argv[2], "--audio-backup-capability") == 0) {
        std::cout << "FTHR_AUDIO_BACKUP_CAPABILITY_V1" << std::endl;
        return 0;
    }
    // argv: --probe-encoder backend codec monitor output-w output-h fps kbps preset fit [hdr10]
    const bool hdr10 = argc == 12 && std::strcmp(argv[11], "hdr10") == 0;
    if (argc != 11 && !hdr10) return 2;
    EncoderPreference preference;
    const std::string backend = argv[2], codec_name = argv[3];
    if (backend == "nvenc") preference = EncoderPreference::Nvidia;
    else if (backend == "amf") preference = EncoderPreference::Amd;
    else if (backend == "qsv") preference = EncoderPreference::Intel;
    else return 2;
    VideoCodec codec;
    if (codec_name == "h264") codec = VideoCodec::H264;
    else if (codec_name == "hevc") codec = VideoCodec::HEVC;
    else if (codec_name == "av1") codec = VideoCodec::AV1;
    else return 2;
    if (hdr10 && !((preference == EncoderPreference::Nvidia && codec != VideoCodec::H264)
        || (preference == EncoderPreference::Intel && codec == VideoCodec::HEVC))) return 2;
    EncoderConfig config;
    config.hdr10 = hdr10;
    if (!Number(argv[5], config.enc_width, 8192) || !Number(argv[6], config.enc_height, 8192)
        || !Number(argv[7], config.fps, 360) || !config.fps
        || !Number(argv[8], config.bitrate_kbps, 200000) || config.bitrate_kbps < 500
        || !Number(argv[9], config.hardware_preset, 7) || !config.hardware_preset
        || !Number(argv[10], config.scaling_mode, 1)
        || ((config.enc_width == 0) != (config.enc_height == 0))) return 2;
    const auto requested_monitor = Wide(argv[4]);
    if (*argv[4] && requested_monitor.empty()) return 2;
    monitor::WindowsMonitorTopologySource source;
    monitor::MonitorResolver resolver(source);
    monitor::MonitorResolveResult selected;
    if (requested_monitor.empty()) {
        const auto topology = source.QueryActiveTopology();
        if (!topology.ok()) return 3;
        const auto primary = std::find_if(topology.monitors.begin(), topology.monitors.end(),
            [](const auto& item) { return item.active && item.primary; });
        if (primary == topology.monitors.end()) return 3;
        selected = resolver.Resolve(monitor::DesktopMonitorIdentity(*primary));
    } else selected = resolver.Resolve(requested_monitor);
    if (!selected.ok()) return 3;
    ComPtr<IDXGIFactory1> factory;
    ComPtr<IDXGIAdapter1> adapter;
    std::string error;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))
        || !monitor::OpenSelectedDxgiAdapter(factory.Get(), selected.monitor, &adapter, error)) return 3;
    DXGI_ADAPTER_DESC1 capture_desc{};
    if (FAILED(adapter->GetDesc1(&capture_desc))) return 3;
    const auto selection = SelectWindowsReplayPolicy(
        EncoderVendorFromPciVendorId(capture_desc.VendorId), preference, codec);
    if (!selection.allowed) return 4;
    const bool cpu_input = !selection.same_adapter;
    const auto vendor = selection.encoder_vendor;
    config.src_width = selected.monitor.width;
    config.src_height = selected.monitor.height;
    CaptureScaleGeometry geometry;
    if (config.src_width > 8192 || config.src_height > 8192
        || !BuildCaptureScaleGeometry(config.src_width, config.src_height,
            config.enc_width, config.enc_height, config.scaling_mode == 1, geometry)) return 5;
    config.enc_width = geometry.target_width;
    config.enc_height = geometry.target_height;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    if (FAILED(D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
        hdr10 ? 0 : D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context))) return 5;
    // QSV must establish its own runtime requirements, just as it does for
    // the initially unprotected production capture context. Pre-enabling
    // the lock here used to qualify QSV even when real capture could not start.
    if (preference != EncoderPreference::Intel) {
        ComPtr<ID3D11Multithread> multithread;
        if (FAILED(context.As(&multithread))) return 5;
        multithread->SetMultithreadProtected(TRUE);
    }
    ComPtr<ID3D11Device> encoder_device = device;
    ComPtr<ID3D11DeviceContext> encoder_context = context;
    ComPtr<IDXGIAdapter> encoder_adapter = adapter;
    if (cpu_input) {
        if (!CreateD3D11DeviceForVendor(vendor,
                encoder_device.ReleaseAndGetAddressOf(),
                encoder_context.ReleaseAndGetAddressOf(), error)) {
            std::cerr << "FTHR_ENCODER_PROBE_FAILURE " << error << std::endl;
            return 5;
        }
        ComPtr<IDXGIDevice> dxgi_device;
        if (FAILED(encoder_device.As(&dxgi_device))
            || FAILED(dxgi_device->GetAdapter(encoder_adapter.ReleaseAndGetAddressOf()))) return 5;
    }
    DXGI_ADAPTER_DESC encoder_desc{};
    if (FAILED(encoder_adapter->GetDesc(&encoder_desc))
        || EncoderVendorFromPciVendorId(encoder_desc.VendorId) != vendor) return 5;
    D3D11_TEXTURE2D_DESC texture_desc{};
    texture_desc.Width = config.src_width; texture_desc.Height = config.src_height;
    texture_desc.MipLevels = texture_desc.ArraySize = texture_desc.SampleDesc.Count = 1;
    texture_desc.Format = hdr10 ? DXGI_FORMAT_R16G16B16A16_FLOAT
                                : DXGI_FORMAT_B8G8R8A8_UNORM;
    texture_desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11RenderTargetView> view;
    if (FAILED(device->CreateTexture2D(&texture_desc, nullptr, &texture))
        || FAILED(device->CreateRenderTargetView(texture.Get(), nullptr, &view))) return 5;
    HdrFrameConverter converter;
    if (hdr10) {
        const HRESULT hr = converter.Initialize(device.Get(), config.enc_width,
            config.enc_height, HdrFrameConverter::Output::Pq);
        if (FAILED(hr)) {
            std::cerr << "FTHR_ENCODER_PROBE_FAILURE P010 setup HRESULT=0x"
                      << std::hex << hr << std::endl;
            return 5;
        }
        config.src_width = config.enc_width;
        config.src_height = config.enc_height;
    }
    ComPtr<ID3D11Texture2D> staging;
    if (cpu_input) {
        auto staging_desc = texture_desc;
        if (hdr10) converter.Texture()->GetDesc(&staging_desc);
        staging_desc.BindFlags = 0;
        staging_desc.Usage = D3D11_USAGE_STAGING;
        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(device->CreateTexture2D(&staging_desc, nullptr, &staging))) return 5;
    }
    std::mutex packet_mutex;
    std::vector<detail::EncoderProbePacket> packets;
    size_t bytes = 0;
    bool overflow = false;
    auto callback = [&](const uint8_t* data, uint32_t size, int64_t pts, bool, int64_t) {
        std::lock_guard<std::mutex> lock(packet_mutex);
        if (!data || !size || packets.size() >= kFrames * 2 || size > kPacketBudget - bytes) {
            overflow = true; return;
        }
        bytes += size;
        packets.push_back({{data, data + size}, pts});
    };
    auto encoder = CreateProductionReplayEncoder(vendor, codec);
    if (!encoder) return 6;
    if (!encoder->Initialize(config, encoder_device.Get(), encoder_context.Get(), callback, cpu_input)) {
        std::cerr << "FTHR_ENCODER_PROBE_FAILURE " << encoder->GetLastError() << std::endl;
        return 6;
    }
    bool ok = true;
    LARGE_INTEGER input_epoch{}, frequency{};
    QueryPerformanceCounter(&input_epoch);
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) return 6;
    std::array<int64_t, kFrames> submit_us{};
    const auto start = std::chrono::steady_clock::now();
    for (uint32_t index = 0; index < kFrames && ok; ++index) {
        // Qualify input at the configured rate, not an unbounded submission
        // burst. At very low configured rates keep the diagnostic under 2s.
        std::this_thread::sleep_until(start + std::chrono::microseconds(
            static_cast<int64_t>(index) * 1000000 / std::max(30u, config.fps)));
        const bool bright = (index / 6) % 2 != 0;
        const float level = hdr10 ? (bright ? 12.5f : 0.25f)
                                  : (bright ? 200.f / 255.f : 40.f / 255.f);
        const float color[] = {level, level, level, 1.f};
        context->ClearRenderTargetView(view.Get(), color);
        auto* encode_texture = texture.Get();
        if (hdr10) {
            ok = SUCCEEDED(converter.Convert(context.Get(), texture.Get(),
                {0, 0, texture_desc.Width, texture_desc.Height}, geometry.destination, 1.0f));
            if (!ok) break;
            encode_texture = converter.Texture();
        }
        // Controlled presentation clock: scheduler/driver latency in this
        // diagnostic must not fabricate missing PTS in an otherwise good encode.
        const int64_t qpc = input_epoch.QuadPart
            + static_cast<int64_t>(index) * frequency.QuadPart / config.fps;
        const auto submit_start = std::chrono::steady_clock::now();
        if (cpu_input) {
            // Match the recorder's cross-adapter path: copy on the capture
            // adapter, block for readback, and submit to NVENC with its row pitch.
            context->CopyResource(staging.Get(), encode_texture);
            D3D11_MAPPED_SUBRESOURCE mapped{};
            ok = SUCCEEDED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped));
            if (ok) {
                ok = mapped.pData && mapped.RowPitch >= config.src_width * (hdr10 ? 2 : 4)
                    && encoder->EncodeFrameCPU(static_cast<const uint8_t*>(mapped.pData),
                        mapped.RowPitch, qpc);
                context->Unmap(staging.Get(), 0);
            }
        } else {
            if (hdr10 && encoder->RequiresBackendGpuPreparation()) {
                ok = encoder->PrepareGpuFrame(encode_texture, 0);
            } else if (hdr10) {
                auto* input = encoder->GetCurrentInputTexture();
                ok = input != nullptr;
                if (ok) context->CopyResource(input, encode_texture);
            } else if (encoder->RequiresBackendGpuPreparation()) ok = encoder->PrepareGpuFrame(texture.Get(), 0);
            else {
                auto* input = encoder->GetCurrentInputTexture();
                D3D11_TEXTURE2D_DESC input_desc{};
                if (input) input->GetDesc(&input_desc);
                ok = input && detail::CanDirectCopyBgra(texture_desc, 0, input_desc,
                    encoder->GetCurrentInputSubresource());
                if (ok) context->CopySubresourceRegion(input, encoder->GetCurrentInputSubresource(),
                    0, 0, 0, texture.Get(), 0, nullptr);
            }
            if (ok) ok = encoder->EncodeFrame(qpc);
        }
        submit_us[index] = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - submit_start).count();
    }
    const auto active = encoder->GetActiveEncoderInfo();
    const auto video = encoder->GetVideoConfig();
    encoder->Shutdown(); // Joins/drains the packet callback before inspecting its output.
    uint32_t decoded = 0;
    if (!ok || overflow || !active.hardware || active.vendor != vendor || active.codec != codec
        || active.backend != selection.backend
        || !IsValidEncodedVideoConfig(video) || video.codec != codec
        || !detail::ValidateEncoderProbePackets(packets, video, decoded) || !resolver.IsCurrent(selected.monitor)
        || FAILED(device->GetDeviceRemovedReason())
        || FAILED(encoder_device->GetDeviceRemovedReason())) {
        std::cerr << "FTHR_ENCODER_PROBE_FAILURE submitted_ok=" << ok
            << " overflow=" << overflow << " packets=" << packets.size()
            << " decoded=" << decoded << " config_valid=" << IsValidEncodedVideoConfig(video)
            << " hardware=" << active.hardware << std::endl;
        return 7;
    }
    bool display_hdr = false;
    for (UINT i = 0; ; ++i) {
        ComPtr<IDXGIOutput> output;
        if (FAILED(adapter->EnumOutputs(i, &output))) break;
        ComPtr<IDXGIOutput6> output6;
        DXGI_OUTPUT_DESC1 desc{};
        if (SUCCEEDED(output.As(&output6)) && SUCCEEDED(output6->GetDesc1(&desc))
            && reinterpret_cast<uintptr_t>(desc.Monitor) == selected.monitor.hmonitor) {
            display_hdr = desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
            break;
        }
    }
    LARGE_INTEGER driver{};
    if (FAILED(encoder_adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &driver))) return 7;
    // Nearest-rank percentiles of host-side input transfer and encoder submission.
    std::sort(submit_us.begin(), submit_us.end());
    std::cout << kEncoderProbeMarker << " {\"schema_version\":1,\"backend\":\"" << backend
        << "\",\"codec\":\"" << codec_name << "\",\"hardware\":true,\"gpu_input\":"
        << (cpu_input ? "false" : "true") << ",\"input_path\":\""
        << (cpu_input ? "cross-adapter-cpu" : "same-adapter-gpu") << "\","
        << "\"hdr10\":" << (hdr10 ? "true" : "false")
        << ",\"display_hdr\":" << (display_hdr ? "true" : "false")
        << ",\"frames_submitted\":" << kFrames << ",\"frames_decoded\":" << decoded
        << ",\"submit_p50_us\":" << submit_us[(kFrames + 1) / 2 - 1]
        << ",\"submit_p95_us\":" << submit_us[(95 * kFrames + 99) / 100 - 1]
        << ",\"source_width\":" << texture_desc.Width << ",\"source_height\":" << texture_desc.Height
        << ",\"width\":" << video.width << ",\"height\":" << video.height
        << ",\"fps\":" << config.fps << ",\"bitrate_kbps\":" << config.bitrate_kbps
        << ",\"preset\":" << config.hardware_preset << ",\"fit\":" << config.scaling_mode
        << ",\"monitor\":\"" << JsonText(argv[4]) << '"'
        << ",\"capture_adapter_luid_high\":" << capture_desc.AdapterLuid.HighPart
        << ",\"capture_adapter_luid_low\":" << capture_desc.AdapterLuid.LowPart
        << ",\"adapter_vendor_id\":" << encoder_desc.VendorId
        << ",\"adapter_luid_high\":" << encoder_desc.AdapterLuid.HighPart
        << ",\"adapter_luid_low\":" << encoder_desc.AdapterLuid.LowPart
        << ",\"driver_version\":\"" << driver.QuadPart << "\"}" << std::endl;
    return 0;
}
} // namespace fthr

#ifdef FTHR_ENCODER_PROBE_STANDALONE
int main(int argc, char* argv[]) { return fthr::RunNativeEncoderProbe(argc, argv); }
#endif
