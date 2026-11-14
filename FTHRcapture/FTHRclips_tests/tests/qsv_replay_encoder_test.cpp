#include "encoded_ring_buffer.h"
#include "encoded_video_config_ffmpeg.h"
#include "ffmpeg_qsv_replay_encoder.h"
#include "replay_encoder.h"
#include "transactional_save.h"

#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>

#include <cstdlib>
#include <deque>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

using fthr::EncodedPacketFormat;
using fthr::EncodedRingBuffer;
using fthr::EncodedVideoConfig;
using fthr::EncoderConfig;
using fthr::EncoderVendor;
using fthr::FfmpegQsvReplayEncoder;
using fthr::QsvCodecSelection;
using fthr::ReplayEncoderBackend;
using fthr::VideoCodec;
using fthr::detail::IQsvCodecSession;
using fthr::detail::QsvPacketView;
using fthr::detail::QsvReceiveStatus;
using fthr::detail::QsvSubmitStatus;

int qsv_checks = 0;

void CheckQsv(bool condition, const char* message) {
    ++qsv_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

EncodedVideoConfig ConfigFor(
    VideoCodec codec,
    std::vector<uint8_t> extradata = {1, 2, 3, 4}) {
    return fthr::BuildQsvVideoConfig(
        codec, 1920, 1080, 60, 16000, extradata);
}

class FakeQsvSession final : public IQsvCodecSession {
public:
    bool initialize_result = true;
    std::string initialize_error;
    bool return_wrong_codec = false;
    bool defer_extradata = false;
    bool prepare_result = true;
    bool submit_error = false;
    bool flush_error = false;
    bool emit_packet = false;
    bool emit_keyframe = false;
    bool flush_called = false;
    bool prepare_called = false;
    uint32_t prepared_subresource = 0;
    uint32_t input_subresource = 2;
    int64_t submitted_pts = -1;
    bool submitted_force_keyframe = false;

    bool Initialize(
        const EncoderConfig& config,
        ID3D11Device*,
        ID3D11DeviceContext*,
        const QsvCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) override {
        if (!initialize_result) {
            error = initialize_error;
            return false;
        }
        video_config = fthr::BuildQsvVideoConfig(
            return_wrong_codec ? VideoCodec::H264 : selection.codec,
            config.enc_width,
            config.enc_height,
            config.fps,
            config.bitrate_kbps,
            defer_extradata ? std::vector<uint8_t>{}
                            : std::vector<uint8_t>{1, 2, 3, 4});
        return true;
    }

    bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource,
        std::string& error) override {
        prepare_called = true;
        prepared_subresource = source_subresource;
        if (!prepare_result || !source) {
            error = "simulated QSV conversion failure";
            return false;
        }
        return true;
    }

    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override {
        return reinterpret_cast<ID3D11Texture2D*>(2);
    }

    uint32_t GetCurrentInputSubresource() const noexcept override {
        return input_subresource;
    }

    QsvSubmitStatus SubmitFrame(
        int64_t pts, bool force_keyframe, std::string& error) override {
        submitted_pts = pts;
        submitted_force_keyframe = force_keyframe;
        if (submit_error) {
            error = "simulated QSV submit failure";
            return QsvSubmitStatus::Error;
        }
        if (emit_packet) packets_.push_back({pts, emit_keyframe});
        return QsvSubmitStatus::Accepted;
    }

    QsvReceiveStatus ReceivePacket(
        QsvPacketView& packet, std::string&) override {
        if (packets_.empty()) {
            return flush_called
                ? QsvReceiveStatus::EndOfStream
                : QsvReceiveStatus::NeedMoreInput;
        }
        current_ = packets_.front();
        packets_.pop_front();
        packet.data = packet_bytes_;
        packet.size = sizeof(packet_bytes_);
        packet.pts = current_.first;
        packet.dts = current_.first;
        packet.keyframe = current_.second;
        if (defer_extradata) {
            packet.codec_extradata = extradata_;
            packet.codec_extradata_size = sizeof(extradata_);
        }
        return QsvReceiveStatus::Packet;
    }

    QsvSubmitStatus SendEndOfStream(std::string& error) override {
        flush_called = true;
        if (flush_error) {
            error = "simulated QSV flush failure";
            return QsvSubmitStatus::Error;
        }
        return QsvSubmitStatus::Accepted;
    }

    void Reset() noexcept override {
        packets_.clear();
    }

private:
    uint8_t packet_bytes_[3]{4, 5, 6};
    uint8_t extradata_[4]{1, 2, 3, 4};
    std::deque<std::pair<int64_t, bool>> packets_;
    std::pair<int64_t, bool> current_{};
};

EncoderConfig TestEncoderConfig() {
    EncoderConfig config;
    config.src_width = 1920;
    config.src_height = 1080;
    config.enc_width = 1920;
    config.enc_height = 1080;
    config.fps = 60;
    config.bitrate_kbps = 16000;
    return config;
}

bool InitializeWithFailure(VideoCodec codec, const char* error_fragment) {
    auto session = std::make_unique<FakeQsvSession>();
    session->initialize_result = false;
    session->initialize_error = error_fragment;
    FfmpegQsvReplayEncoder encoder(codec, std::move(session));
    const bool initialized = encoder.Initialize(
        TestEncoderConfig(),
        reinterpret_cast<ID3D11Device*>(1),
        reinterpret_cast<ID3D11DeviceContext*>(1),
        [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
        false);
    return !initialized
        && encoder.GetLastError().find(error_fragment) != std::string::npos;
}

void VendorPolicySelectsOnlyTheExpectedBackend() {
    CheckQsv(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Intel, VideoCodec::H264)
              == ReplayEncoderBackend::FfmpegQsv,
        "Intel adapter selects FFmpeg QSV");
    CheckQsv(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Nvidia, VideoCodec::AV1)
              == ReplayEncoderBackend::NativeNvenc,
        "NVIDIA adapter remains on native NVENC");
    CheckQsv(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Amd, VideoCodec::HEVC)
              == ReplayEncoderBackend::FfmpegAmf,
        "AMD adapter remains on FFmpeg AMF");
}

void RequestedCodecsMapToExactQsvNames() {
    CheckQsv(std::string(fthr::GetQsvCodecSelection(VideoCodec::H264)->encoder_name)
                  == "h264_qsv",
        "H.264 maps to h264_qsv");
    CheckQsv(std::string(fthr::GetQsvCodecSelection(VideoCodec::HEVC)->encoder_name)
                  == "hevc_qsv",
        "HEVC maps to hevc_qsv");
    CheckQsv(std::string(fthr::GetQsvCodecSelection(VideoCodec::AV1)->encoder_name)
                  == "av1_qsv",
        "AV1 maps to av1_qsv");
}

void RuntimeAndCodecFailuresAreHonest() {
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "oneVPL runtime unavailable"),
        "missing QSV runtime fails honestly");
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "h264_qsv unsupported"),
        "unsupported H.264 fails honestly");
    CheckQsv(InitializeWithFailure(VideoCodec::HEVC, "hevc_qsv unsupported"),
        "unsupported HEVC fails honestly");
    CheckQsv(InitializeWithFailure(VideoCodec::AV1, "av1_qsv unsupported"),
        "unsupported AV1 fails honestly");
}

void DeviceAndConversionFailuresPropagate() {
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "wrong Intel adapter"),
        "wrong adapter/device is rejected");
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "D3D11 hw device failure"),
        "D3D11 hardware-device failure propagates");
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "QSV derivation failure"),
        "QSV device derivation failure propagates");
    CheckQsv(InitializeWithFailure(VideoCodec::H264, "conversion setup failure"),
        "video-processor setup failure propagates");

    ID3D11Device* warp_device = nullptr;
    ID3D11DeviceContext* warp_context = nullptr;
    D3D_FEATURE_LEVEL feature_level{};
    const HRESULT device_result = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_WARP,
        nullptr,
        0,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        &warp_device,
        &feature_level,
        &warp_context);
    FfmpegQsvReplayEncoder real_encoder(VideoCodec::H264);
    const bool wrong_device_rejected = SUCCEEDED(device_result)
        && !real_encoder.Initialize(
            TestEncoderConfig(),
            warp_device,
            warp_context,
            [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
            false)
        && real_encoder.GetLastError().find("not Intel") != std::string::npos;
    if (warp_context) warp_context->Release();
    if (warp_device) warp_device->Release();
    CheckQsv(wrong_device_rejected,
        "real QSV session rejects a non-Intel D3D11 device");

    auto session = std::make_unique<FakeQsvSession>();
    auto* fake = session.get();
    fake->prepare_result = false;
    FfmpegQsvReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckQsv(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && encoder.RequiresBackendGpuPreparation()
            && !encoder.PrepareGpuFrame(
                reinterpret_cast<ID3D11Texture2D*>(1), 3)
            && fake->prepare_called
            && fake->prepared_subresource == 3,
        "per-frame GPU conversion errors propagate");
}

void SilentCodecFallbackIsRejected() {
    auto session = std::make_unique<FakeQsvSession>();
    session->return_wrong_codec = true;
    FfmpegQsvReplayEncoder encoder(VideoCodec::AV1, std::move(session));
    CheckQsv(!encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "requested AV1 cannot silently become H.264");
}

void VideoConfigsAreCodecCorrect() {
    const auto h264 = ConfigFor(VideoCodec::H264);
    const auto hevc = ConfigFor(VideoCodec::HEVC);
    const auto av1 = ConfigFor(VideoCodec::AV1);
    CheckQsv(h264.codec == VideoCodec::H264
            && h264.packet_format == EncodedPacketFormat::LengthPrefixedNalUnits,
        "QSV H.264 config is AVCC-compatible");
    CheckQsv(hevc.codec == VideoCodec::HEVC
            && hevc.packet_format == EncodedPacketFormat::AnnexBNalUnits,
        "QSV HEVC config preserves Annex B");
    CheckQsv(av1.codec == VideoCodec::AV1
            && av1.packet_format == EncodedPacketFormat::LowOverheadObu,
        "QSV AV1 config preserves low-overhead OBUs");
}

void RingMuxAndTransactionRemainCodecNeutral() {
    EncodedRingBuffer ring(8, 60, 1000);
    const auto config = ConfigFor(VideoCodec::HEVC);
    CheckQsv(ring.SetVideoConfig(config)
            && ring.TakeSnapshotByTime(1, 1000).video_config == config,
        "ring preserves QSV video config");
    CheckQsv(fthr::ToAvCodecId(VideoCodec::AV1) == AV_CODEC_ID_AV1,
        "mux mapping preserves requested QSV codec");

    bool cleanup_called = false;
    bool rename_called = false;
    bool writer_saw_partial = false;
    fthr::transactional_save::FileOps operations;
    operations.exists = [](const std::filesystem::path&, std::error_code&) {
        return false;
    };
    operations.remove = [&](const std::filesystem::path&, std::error_code&) {
        cleanup_called = true;
        return true;
    };
    operations.rename_no_replace = [&](
        const std::filesystem::path&,
        const std::filesystem::path&,
        std::error_code&) {
        rename_called = true;
        return true;
    };
    const auto failed_mux = fthr::transactional_save::Run(
        L"qsv-fault.mp4",
        [&](const std::filesystem::path& temporary_path, std::string& error) {
            writer_saw_partial = temporary_path.extension() == L".partial";
            error = "simulated QSV mux failure";
            return false;
        },
        operations);
    CheckQsv(!failed_mux.success
            && writer_saw_partial
            && cleanup_called
            && !rename_called,
        "QSV mux failure cleans partial output and never publishes success");
}

void PacketMetadataAndDeferredAv1ConfigPropagate() {
    auto session = std::make_unique<FakeQsvSession>();
    auto* fake = session.get();
    fake->emit_packet = true;
    fake->emit_keyframe = true;
    fake->defer_extradata = true;
    int64_t callback_pts = -1;
    bool callback_keyframe = false;
    FfmpegQsvReplayEncoder encoder(VideoCodec::AV1, std::move(session));
    CheckQsv(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [&](const uint8_t*, uint32_t, int64_t pts,
                      bool keyframe, int64_t) {
                      callback_pts = pts;
                      callback_keyframe = keyframe;
                  },
                  false)
            && !encoder.IsVideoConfigReady()
            && encoder.PrepareGpuFrame(
                reinterpret_cast<ID3D11Texture2D*>(1), 0)
            && encoder.EncodeFrame(1)
            && encoder.IsVideoConfigReady()
            && !encoder.GetVideoConfig().codec_extradata.empty()
            && callback_pts == fake->submitted_pts,
        "QSV PTS and first-packet AV1 configuration propagate");
    CheckQsv(callback_keyframe,
        "QSV keyframe metadata propagates");
    CheckQsv(encoder.PrepareGpuFrame(reinterpret_cast<ID3D11Texture2D*>(1), 0) && encoder.EncodeFrame(2)
            && !fake->submitted_force_keyframe,
        "normal following frame does not force an IDR");
    encoder.RequestKeyframe();
    CheckQsv(encoder.PrepareGpuFrame(reinterpret_cast<ID3D11Texture2D*>(1), 0) && encoder.EncodeFrame(3)
            && fake->submitted_force_keyframe,
        "recording start requests an immediate random-access frame");
    CheckQsv(encoder.PrepareGpuFrame(reinterpret_cast<ID3D11Texture2D*>(1), 0) && encoder.EncodeFrame(4)
            && !fake->submitted_force_keyframe,
        "keyframe request is consumed exactly once");

}

void GenerationFlushAndErrorsAreObservable() {
    EncodedRingBuffer ring(8, 60, 1000);
    CheckQsv(ring.SetVideoConfig(ConfigFor(VideoCodec::H264))
            && !ring.SetVideoConfig(ConfigFor(VideoCodec::AV1)),
        "QSV generation rejects a codec/config change");

    auto session = std::make_unique<FakeQsvSession>();
    auto* fake = session.get();
    FfmpegQsvReplayEncoder encoder(VideoCodec::HEVC, std::move(session));
    CheckQsv(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "QSV fake session initializes for flush test");
    encoder.Shutdown();
    CheckQsv(fake->flush_called,
        "QSV shutdown flushes the codec session");

    auto failing_session = std::make_unique<FakeQsvSession>();
    failing_session->submit_error = true;
    FfmpegQsvReplayEncoder failing(VideoCodec::H264, std::move(failing_session));
    CheckQsv(failing.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && failing.PrepareGpuFrame(
                reinterpret_cast<ID3D11Texture2D*>(1), 0)
            && !failing.EncodeFrame(1)
            && failing.GetLastError().find("submit failure") != std::string::npos,
        "QSV encode errors propagate without fallback");
}

void QsvProtectsAnInitiallyUnprotectedCaptureContext() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    CheckQsv(SUCCEEDED(D3D11CreateDevice(
        nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
        D3D11_SDK_VERSION, &device, nullptr, &context)),
        "WARP creates a real capture-style device without an Intel GPU");
    ID3D11Multithread* multithread = nullptr;
    CheckQsv(SUCCEEDED(context->QueryInterface(
        __uuidof(ID3D11Multithread), reinterpret_cast<void**>(&multithread))),
        "capture context exposes the D3D11 multithread interface");
    multithread->SetMultithreadProtected(FALSE);
    CheckQsv(multithread->GetMultithreadProtected() == FALSE,
        "regression starts with the production context's unprotected state");
    std::string error;
    CheckQsv(fthr::detail::EnableQsvMultithreadProtection(context, error)
            && multithread->GetMultithreadProtected() != FALSE,
        "QSV enables the context lock before sharing the device with oneVPL");
    CheckQsv(fthr::detail::EnableQsvMultithreadProtection(context, error)
            && multithread->GetMultithreadProtected() != FALSE,
        "QSV reinitialization preserves the shared context lock");
    CheckQsv(!fthr::detail::EnableQsvMultithreadProtection(nullptr, error)
            && error.find("immediate context") != std::string::npos,
        "missing QSV context fails with a specific diagnostic");
    ID3D11DeviceContext* deferred = nullptr;
    CheckQsv(SUCCEEDED(device->CreateDeferredContext(0, &deferred)),
        "deferred context is available for invalid-input regression");
    CheckQsv(!fthr::detail::EnableQsvMultithreadProtection(deferred, error),
        "QSV rejects a deferred context rather than reporting it protected");
    deferred->Release();
    multithread->Release();
    context->Release();
    device->Release();
}

void SuccessfulQsvUsesCompressedReplayAndPoliciesStayStable() {
    CheckQsv(!fthr::ShouldAllocateRawReplayPool(
                  ReplayEncoderBackend::FfmpegQsv, true),
        "successful QSV selection skips the raw replay pool");
    CheckQsv(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Nvidia, VideoCodec::H264)
              == ReplayEncoderBackend::NativeNvenc,
        "NVIDIA policy regression remains protected");
    CheckQsv(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Amd, VideoCodec::AV1)
              == ReplayEncoderBackend::FfmpegAmf,
        "AMD policy regression remains protected");
}

} // namespace

int RunQsvReplayEncoderTests() {
    VendorPolicySelectsOnlyTheExpectedBackend();
    RequestedCodecsMapToExactQsvNames();
    RuntimeAndCodecFailuresAreHonest();
    DeviceAndConversionFailuresPropagate();
    SilentCodecFallbackIsRejected();
    VideoConfigsAreCodecCorrect();
    RingMuxAndTransactionRemainCodecNeutral();
    PacketMetadataAndDeferredAv1ConfigPropagate();
    GenerationFlushAndErrorsAreObservable();
    QsvProtectsAnInitiallyUnprotectedCaptureContext();
    SuccessfulQsvUsesCompressedReplayAndPoliciesStayStable();
    std::cout << "FTHRclips QSV tests: 28 scenarios passed ("
              << qsv_checks << " checks)" << std::endl;
    return qsv_checks;
}
