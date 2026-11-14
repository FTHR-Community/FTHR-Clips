#include "native_encoder_probe.h"
#include "software_replay_encoder.h"
#include "hdr_frame_converter.h"
#include "encoded_video_config_ffmpeg.h"
#include "nvenc_codec_config.h"
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <iostream>

namespace {
int checks = 0;
void Check(bool value, const char* message) {
    ++checks;
    if (!value) { std::cerr << "native encoder probe: " << message << '\n'; std::abort(); }
}
void CheckSdrToneMapping() {
    using Microsoft::WRL::ComPtr;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    Check(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        0, nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context)),
        "WARP creates a shader-test device");

    constexpr uint32_t width = 4, height = 2;
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    // FP16 values 0, 0.25, 1.0 and 12.5 in scRGB (0, 20, 80, 1000 nits).
    const uint16_t levels[] = {0, 0x3400, 0x3c00, 0x4a40};
    uint16_t pixels[width * height * 4]{};
    for (uint32_t y = 0; y < height; ++y) {
        for (uint32_t x = 0; x < width; ++x) {
            for (uint32_t c = 0; c < 3; ++c)
                pixels[(y * width + x) * 4 + c] = levels[x];
            pixels[(y * width + x) * 4 + 3] = 0x3c00;
        }
    }
    D3D11_SUBRESOURCE_DATA source_data{pixels, width * 4 * sizeof(uint16_t), 0};
    ComPtr<ID3D11Texture2D> source;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, &source_data, &source)),
        "creates linear scRGB fixture");
    fthr::HdrFrameConverter converter;
    Check(SUCCEEDED(converter.Initialize(device.Get(), width, height,
        fthr::HdrFrameConverter::Output::Sdr)), "HDR-to-SDR shader initializes");
    constexpr fthr::CaptureScaleRect full{0, 0, width, height};
    Check(SUCCEEDED(converter.Convert(context.Get(), source.Get(), full, full, 1.0f)),
        "HDR-to-SDR shader converts linear colors");
    converter.Texture()->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &staging)),
        "creates BGRA readback fixture");
    context->CopyResource(staging.Get(), converter.Texture());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    Check(SUCCEEDED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped)),
        "maps tone-mapped colors");
    const auto* bgra = static_cast<const uint8_t*>(mapped.pData);
    const int expected[] = {0, 125, 239, 254};
    for (uint32_t x = 0; x < width; ++x) {
        Check(std::abs(bgra[x * 4] - expected[x]) <= 2
            && bgra[x * 4] == bgra[x * 4 + 1] && bgra[x * 4] == bgra[x * 4 + 2],
            "SDR OETF preserves dark tones and compresses HDR highlights");
    }
    context->Unmap(staging.Get(), 0);
}

void CheckHdrMetadata() {
    using fthr::ReplayEncoderBackend;
    using fthr::SelectHdrReplayCodec;
    using fthr::VideoCodec;
    Check(SelectHdrReplayCodec(ReplayEncoderBackend::NativeNvenc, VideoCodec::H264, true)
            == VideoCodec::HEVC
        && SelectHdrReplayCodec(ReplayEncoderBackend::FfmpegQsv, VideoCodec::H264, true)
            == VideoCodec::HEVC,
        "automatic codec selects HEVC on supported HDR backends");
    Check(!SelectHdrReplayCodec(ReplayEncoderBackend::NativeNvenc, VideoCodec::H264, false)
        && !SelectHdrReplayCodec(ReplayEncoderBackend::Software, VideoCodec::H264, true)
        && !SelectHdrReplayCodec(ReplayEncoderBackend::FfmpegAmf, VideoCodec::H264, true),
        "explicit H.264 and SDR-only backends retain SDR");

    NV_ENC_CONFIG nvenc{};
    Check(fthr::ConfigureNvencCodec(fthr::VideoCodec::HEVC, 60, nvenc, true),
        "HEVC HDR codec config");
    Check(std::memcmp(&nvenc.profileGUID, &NV_ENC_HEVC_PROFILE_MAIN10_GUID,
                sizeof(GUID)) == 0
        && nvenc.encodeCodecConfig.hevcConfig.inputBitDepth == NV_ENC_BIT_DEPTH_10
        && nvenc.encodeCodecConfig.hevcConfig.outputBitDepth == NV_ENC_BIT_DEPTH_10,
        "HEVC Main10 preserves 10-bit input");
    Check(nvenc.encodeCodecConfig.hevcConfig.hevcVUIParameters.colourPrimaries
            == NV_ENC_VUI_COLOR_PRIMARIES_BT2020
        && nvenc.encodeCodecConfig.hevcConfig.hevcVUIParameters.transferCharacteristics
            == NV_ENC_VUI_TRANSFER_CHARACTERISTIC_SMPTE2084
        && nvenc.encodeCodecConfig.hevcConfig.hevcVUIParameters.colourMatrix
            == NV_ENC_VUI_MATRIX_COEFFS_BT2020_NCL,
        "HEVC writes PQ and BT.2020 bitstream metadata");
    Check(!fthr::ConfigureNvencCodec(fthr::VideoCodec::H264, 60, nvenc, true),
        "H.264 does not accept the 10-bit recording contract");
    auto config = fthr::BuildNvencVideoConfig(fthr::VideoCodec::HEVC, 320, 180,
                                            60, 4000, {}, true);
    auto sdr = config;
    sdr.hdr10 = false;
    Check(config.hdr10 && !(config == sdr), "ring config tracks dynamic range");
    AVCodecParameters parameters{};
    fthr::ApplyVideoColorMetadata(&parameters, config.hdr10);
    Check(parameters.color_range == AVCOL_RANGE_MPEG
        && parameters.color_primaries == AVCOL_PRI_BT2020
        && parameters.color_trc == AVCOL_TRC_SMPTE2084
        && parameters.color_space == AVCOL_SPC_BT2020_NCL,
        "MP4 HDR color metadata");
    fthr::ApplyVideoColorMetadata(&parameters, sdr.hdr10);
    Check(parameters.color_range == AVCOL_RANGE_MPEG
        && parameters.color_primaries == AVCOL_PRI_BT709
        && parameters.color_trc == AVCOL_TRC_BT709
        && parameters.color_space == AVCOL_SPC_BT709,
        "MP4 SDR color metadata");
}

void CheckDecodedEvidence(bool frozen) {
    fthr::SoftwareReplayEncoder encoder;
    fthr::EncoderConfig config;
    config.src_width = 160; config.src_height = 90;
    config.enc_width = 320; config.enc_height = 180;
    config.fps = 60; config.bitrate_kbps = 8000;
    std::vector<fthr::detail::EncoderProbePacket> packets;
    Check(encoder.Initialize(config, nullptr, nullptr,
        [&](const uint8_t* data, uint32_t size, int64_t pts, bool, int64_t) {
            packets.push_back({{data, data + size}, pts});
        }, true), "software fixture encoder initialized");
    std::vector<uint8_t> pixels(160 * 90 * 4);
    for (int index = 0; index < 48; ++index) {
        const uint8_t level = !frozen && (index / 6) % 2 ? 200 : 40;
        for (size_t pixel = 0; pixel < pixels.size(); pixel += 4) {
            pixels[pixel] = pixels[pixel + 1] = pixels[pixel + 2] = level;
            pixels[pixel + 3] = 255;
        }
        Check(encoder.EncodeFrameCPU(pixels.data(), 160 * 4, index + 1), "fixture frame encoded");
    }
    const auto video = encoder.GetVideoConfig();
    encoder.Shutdown();
    uint32_t decoded = 0;
    Check(fthr::detail::ValidateEncoderProbePackets(packets, video, decoded) == !frozen,
        "real decoded content accepts moving input and rejects frozen input");
    if (frozen) return;
    Check(decoded == 48, "all 48 fixture timestamps decoded");
    const auto last = packets.back();
    packets.pop_back();
    Check(!fthr::detail::ValidateEncoderProbePackets(packets, video, decoded), "missing tail rejected");
    packets.push_back(last);
    packets.back().pts = packets.front().pts;
    Check(!fthr::detail::ValidateEncoderProbePackets(packets, video, decoded), "duplicate timestamp rejected");
    packets.back() = last;
    packets.front().bytes.assign(packets.front().bytes.size(), 0);
    Check(!fthr::detail::ValidateEncoderProbePackets(packets, video, decoded), "corrupt bitstream rejected");
    packets.clear();
    Check(!fthr::detail::ValidateEncoderProbePackets(packets, video, decoded), "no packets rejected");
}
} // namespace

int RunNativeEncoderProbeTests() {
    CheckHdrMetadata();
    CheckSdrToneMapping();
    CheckDecodedEvidence(false);
    CheckDecodedEvidence(true);
    char name[] = "FTHRClips", flag[] = "--probe-encoder";
    char* argv[] = {name, flag};
    Check(fthr::RunNativeEncoderProbe(2, argv) == 2, "malformed CLI never starts capture");
    std::cout << "native encoder probe tests: " << checks << " checks (software decoder fixture; no GPU encoding claim)\n";
    return checks;
}
#ifdef FTHR_ENCODER_PROBE_TEST_STANDALONE
int main() { RunNativeEncoderProbeTests(); }
#endif
