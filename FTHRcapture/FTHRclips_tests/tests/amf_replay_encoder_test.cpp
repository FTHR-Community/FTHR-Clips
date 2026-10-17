#include "encoded_ring_buffer.h"
#include "encoded_video_config_ffmpeg.h"
#include "ffmpeg_amf_replay_encoder.h"
#include "replay_encoder.h"
#include "transactional_save.h"

#include <windows.h>
#include <d3d11.h>

#include <cstdlib>
#include <deque>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

using fthr::AmfCodecSelection;
using fthr::EncodedPacketFormat;
using fthr::EncodedRingBuffer;
using fthr::EncodedVideoConfig;
using fthr::EncoderConfig;
using fthr::EncoderVendor;
using fthr::FfmpegAmfReplayEncoder;
using fthr::ReplayEncoderBackend;
using fthr::VideoCodec;
using fthr::detail::AmfPacketView;
using fthr::detail::AmfReceiveStatus;
using fthr::detail::AmfSubmitStatus;
using fthr::detail::IAmfCodecSession;

int amf_checks = 0;

void CheckAmf(bool condition, const char* message) {
    ++amf_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

EncodedVideoConfig ConfigFor(VideoCodec codec) {
    return fthr::BuildAmfVideoConfig(
        codec, 1920, 1080, 60, 16000, {1, 2, 3, 4});
}

class FakeAmfSession final : public IAmfCodecSession {
public:
    bool initialize_result = true;
    std::string initialize_error;
    bool return_wrong_codec = false;
    bool submit_error = false;
    bool need_drain_once = false;
    bool flush_error = false;
    bool receive_error = false;
    bool emit_packet = false;
    bool emit_keyframe = false;
    bool prepare_gpu_result = true;
    std::string prepare_gpu_error;
    bool flush_called = false;
    uint32_t input_subresource = 0;
    int64_t submitted_pts = -1;
    bool submitted_force_keyframe = false;
    uint32_t submit_calls = 0;

    void QueuePacket(
        int64_t pts, bool keyframe, std::vector<uint8_t> bytes = {4, 5, 6}) {
        packets_.push_back({pts, pts, keyframe, std::move(bytes)});
    }

    void QueuePacketAtEndOfStream(
        int64_t pts, bool keyframe, std::vector<uint8_t> bytes = {4, 5, 6}) {
        eos_packets_.push_back({pts, pts, keyframe, std::move(bytes)});
    }

    bool Initialize(
        const EncoderConfig& config,
        ID3D11Device*,
        ID3D11DeviceContext*,
        const AmfCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) override {
        flush_called = false;
        if (!initialize_result) {
            error = initialize_error;
            return false;
        }
        const uint32_t width = config.enc_width > 0
            ? config.enc_width : config.src_width;
        const uint32_t height = config.enc_height > 0
            ? config.enc_height : config.src_height;
        video_config = fthr::BuildAmfVideoConfig(
            return_wrong_codec ? VideoCodec::H264 : selection.codec,
            width,
            height,
            config.fps,
            config.bitrate_kbps,
            {1, 2, 3, 4});
        return true;
    }

    bool PrepareGpuFrame(
        ID3D11Texture2D*,
        uint32_t,
        std::string& error) override {
        if (!prepare_gpu_result) {
            error = prepare_gpu_error;
            return false;
        }
        return true;
    }

    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override {
        return reinterpret_cast<ID3D11Texture2D*>(1);
    }

    uint32_t GetCurrentInputSubresource() const noexcept override {
        return input_subresource;
    }

    AmfSubmitStatus SubmitFrame(
        int64_t pts, bool force_keyframe, std::string& error) override {
        ++submit_calls;
        submitted_pts = pts;
        submitted_force_keyframe = force_keyframe;
        submitted_pts_history.push_back(pts);
        submitted_force_keyframe_history.push_back(force_keyframe);
        if (submit_error) {
            error = "simulated AMF submit failure";
            return AmfSubmitStatus::Error;
        }
        if (need_drain_once) {
            need_drain_once = false;
            return AmfSubmitStatus::NeedDrain;
        }
        if (emit_packet) {
            QueuePacket(pts, emit_keyframe);
        }
        return AmfSubmitStatus::Accepted;
    }

    AmfReceiveStatus ReceivePacket(
        AmfPacketView& packet, std::string& error) override {
        if (packets_.empty()) {
            if (receive_error) {
                error = "simulated AMF receive failure";
                return AmfReceiveStatus::Error;
            }
            return flush_called
                ? AmfReceiveStatus::EndOfStream
                : AmfReceiveStatus::NeedMoreInput;
        }
        current_ = std::move(packets_.front());
        packets_.pop_front();
        packet.data = current_.bytes.data();
        packet.size = static_cast<uint32_t>(current_.bytes.size());
        packet.pts = current_.pts;
        packet.dts = current_.dts;
        packet.keyframe = current_.keyframe;
        return AmfReceiveStatus::Packet;
    }

    AmfSubmitStatus SendEndOfStream(std::string& error) override {
        flush_called = true;
        if (flush_error) {
            error = "simulated AMF flush failure";
            return AmfSubmitStatus::Error;
        }
        while (!eos_packets_.empty()) {
            packets_.push_back(std::move(eos_packets_.front()));
            eos_packets_.pop_front();
        }
        return AmfSubmitStatus::Accepted;
    }

    void Reset() noexcept override {
        packets_.clear();
        eos_packets_.clear();
        current_ = {};
        submit_calls = 0;
        submitted_pts_history.clear();
        submitted_force_keyframe_history.clear();
    }

private:
    struct FakePacket {
        int64_t pts = 0;
        int64_t dts = 0;
        bool keyframe = false;
        std::vector<uint8_t> bytes;
    };
    std::deque<FakePacket> packets_;
    std::deque<FakePacket> eos_packets_;
    FakePacket current_{};

public:
    std::vector<int64_t> submitted_pts_history;
    std::vector<bool> submitted_force_keyframe_history;
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
    auto session = std::make_unique<FakeAmfSession>();
    session->initialize_result = false;
    session->initialize_error = error_fragment;
    FfmpegAmfReplayEncoder encoder(codec, std::move(session));
    const bool initialized = encoder.Initialize(
        TestEncoderConfig(),
        reinterpret_cast<ID3D11Device*>(1),
        reinterpret_cast<ID3D11DeviceContext*>(1),
        [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
        false);
    return !initialized
        && encoder.GetLastError().find(error_fragment) != std::string::npos;
}

bool InitializeWithFps(uint32_t fps, const char* error_fragment) {
    auto session = std::make_unique<FakeAmfSession>();
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    auto config = TestEncoderConfig();
    config.fps = fps;
    const bool initialized = encoder.Initialize(
        config,
        reinterpret_cast<ID3D11Device*>(1),
        reinterpret_cast<ID3D11DeviceContext*>(1),
        [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
        false);
    return !initialized
        && encoder.GetLastError().find(error_fragment) != std::string::npos;
}

void AmdAdapterSelectsAmf() {
    CheckAmf(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Amd, VideoCodec::H264)
              == ReplayEncoderBackend::FfmpegAmf,
        "AMD adapter selects FFmpeg AMF");
}

void NvidiaStillSelectsNativeNvenc() {
    CheckAmf(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Nvidia, VideoCodec::AV1)
              == ReplayEncoderBackend::NativeNvenc,
        "NVIDIA adapter remains on native NVENC");
}

void IntelStageDoesNotAffectAmdPolicy() {
    CheckAmf(fthr::SelectProductionReplayBackend(
                  EncoderVendor::Intel, VideoCodec::H264)
              == ReplayEncoderBackend::FfmpegQsv,
        "Intel now selects QSV without changing AMD AMF policy");
}

void RequestedCodecsMapToExactAmfNames() {
    CheckAmf(std::string(fthr::GetAmfCodecSelection(VideoCodec::H264)->encoder_name)
                  == "h264_amf",
        "H.264 maps to h264_amf");
    CheckAmf(std::string(fthr::GetAmfCodecSelection(VideoCodec::HEVC)->encoder_name)
                  == "hevc_amf",
        "HEVC maps to hevc_amf");
    CheckAmf(std::string(fthr::GetAmfCodecSelection(VideoCodec::AV1)->encoder_name)
                  == "av1_amf",
        "AV1 maps to av1_amf");
}

void MissingEncodersFailHonestly() {
    CheckAmf(InitializeWithFailure(VideoCodec::H264, "h264_amf not found"),
        "missing h264_amf fails honestly");
    CheckAmf(InitializeWithFailure(VideoCodec::HEVC, "hevc_amf not found"),
        "missing hevc_amf fails honestly");
    CheckAmf(InitializeWithFailure(VideoCodec::AV1, "av1_amf not found"),
        "missing av1_amf fails honestly");
}

void InitializationFailuresPropagate() {
    CheckAmf(InitializeWithFailure(VideoCodec::H264, "encoder open failed"),
        "encoder open failure propagates");
    CheckAmf(InitializeWithFailure(VideoCodec::HEVC, "hevc_amf open failed"),
        "HEVC encoder initialization failure propagates");
    CheckAmf(InitializeWithFailure(VideoCodec::AV1, "AV1 unavailable on adapter"),
        "AV1 hardware capability failure propagates");
    CheckAmf(InitializeWithFailure(VideoCodec::H264, "D3D11 hw context failed"),
        "D3D11 hw context failure propagates");
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
    FfmpegAmfReplayEncoder real_encoder(VideoCodec::H264);
    const bool wrong_device_rejected = SUCCEEDED(device_result)
        && !real_encoder.Initialize(
            TestEncoderConfig(),
            warp_device,
            warp_context,
            [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
            false)
        && real_encoder.GetLastError().find("not AMD") != std::string::npos;
    if (warp_context) warp_context->Release();
    if (warp_device) warp_device->Release();
    CheckAmf(wrong_device_rejected,
        "real AMF session rejects a non-AMD D3D11 device");
}

void SilentCodecFallbackIsRejected() {
    auto session = std::make_unique<FakeAmfSession>();
    session->return_wrong_codec = true;
    FfmpegAmfReplayEncoder encoder(VideoCodec::AV1, std::move(session));
    CheckAmf(!encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "requested AV1 cannot silently become H.264");
}

void AmfVideoConfigsAreCodecCorrect() {
    const auto h264 = ConfigFor(VideoCodec::H264);
    const auto hevc = ConfigFor(VideoCodec::HEVC);
    const auto av1 = ConfigFor(VideoCodec::AV1);
    CheckAmf(h264.codec == VideoCodec::H264
            && h264.packet_format == EncodedPacketFormat::LengthPrefixedNalUnits,
        "AMF H.264 config is AVCC-compatible");
    CheckAmf(hevc.codec == VideoCodec::HEVC
            && hevc.packet_format == EncodedPacketFormat::AnnexBNalUnits,
        "AMF HEVC config preserves Annex B");
    CheckAmf(av1.codec == VideoCodec::AV1
            && av1.packet_format == EncodedPacketFormat::LowOverheadObu,
        "AMF AV1 config preserves low-overhead OBUs");
}

void AmfFrameRateMatrixAndBounds() {
    for (const uint32_t fps : {30u, 60u, 120u, 144u}) {
        for (const auto codec : {VideoCodec::H264, VideoCodec::HEVC,
                                 VideoCodec::AV1}) {
            const auto config = fthr::BuildAmfVideoConfig(
                codec, 1920, 1080, fps, 16000, {1, 2, 3, 4});
            CheckAmf(config.frame_rate.numerator == static_cast<int32_t>(fps)
                    && config.frame_rate.denominator == 1
                    && config.time_base.numerator == 1
                    && config.time_base.denominator == static_cast<int32_t>(fps)
                    && config.max_keyframe_interval_frames == fps
                    && config.max_b_frames == 0,
                "AMF publishes the tested 30/60/120/144 FPS codec timing");
        }
    }
    CheckAmf(InitializeWithFps(0, "between 1 and 360"),
        "AMF rejects zero FPS before session initialization");
    CheckAmf(InitializeWithFps(361, "between 1 and 360"),
        "AMF rejects FPS above the shared 360 FPS limit");
}

void RingAndMuxRemainCodecNeutral() {
    EncodedRingBuffer ring(8, 60, 1000);
    const auto config = ConfigFor(VideoCodec::HEVC);
    CheckAmf(ring.SetVideoConfig(config)
            && ring.TakeSnapshotByTime(1, 1000).video_config == config,
        "ring preserves AMF video config");
    CheckAmf(fthr::ToAvCodecId(VideoCodec::AV1) == AV_CODEC_ID_AV1,
        "mux mapping preserves requested AMF codec");

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
        L"amf-fault.mp4",
        [&](const std::filesystem::path& temporary_path, std::string& error) {
            writer_saw_partial = temporary_path.extension() == L".partial";
            error = "simulated AMF mux failure";
            return false;
        },
        operations);
    CheckAmf(!failed_mux.success
            && failed_mux.failure == fthr::transactional_save::Failure::Writer
            && writer_saw_partial
            && cleanup_called
            && !rename_called,
        "AMF mux failure cleans the partial file and never publishes success");
}

void PacketMetadataPropagates() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    fake->emit_packet = true;
    fake->emit_keyframe = true;
    fake->input_subresource = 3;
    int64_t callback_pts = -1;
    bool callback_keyframe = false;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [&](const uint8_t*, uint32_t, int64_t pts, bool keyframe, int64_t) {
                      callback_pts = pts;
                      callback_keyframe = keyframe;
                  },
                   false)
            && encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(1)
            && callback_pts == fake->submitted_pts
            && encoder.GetCurrentInputSubresource() == 3,
        "AMF packet timestamp propagates through wrapper");
    CheckAmf(callback_keyframe,
        "AMF keyframe flag propagates through wrapper");
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0) && encoder.EncodeFrame(2)
            && !fake->submitted_force_keyframe,
        "normal following frame does not force an IDR");
    encoder.RequestKeyframe();
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0) && encoder.EncodeFrame(3)
            && fake->submitted_force_keyframe,
        "recording start requests an immediate random-access frame");
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0) && encoder.EncodeFrame(4)
            && !fake->submitted_force_keyframe,
        "keyframe request is consumed exactly once");

}

void DelayedPacketsKeepTheirSourceTimingAndBorrowedBytes() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    std::vector<int64_t> callback_pts;
    std::vector<int64_t> callback_wall_qpc;
    std::vector<std::vector<uint8_t>> callback_bytes;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [&](const uint8_t* data, uint32_t size, int64_t pts,
                      bool, int64_t wall_qpc) {
                      callback_pts.push_back(pts);
                      callback_wall_qpc.push_back(wall_qpc);
                      callback_bytes.emplace_back(data, data + size);
                  },
                  false),
        "AMF delayed-output fake initializes");
    LARGE_INTEGER frequency{};
    CheckAmf(QueryPerformanceFrequency(&frequency) && frequency.QuadPart > 0,
        "QPC frequency is available for delayed AMF output test");
    const int64_t start = 1'000'000;
    const int64_t period = frequency.QuadPart / 60;
    for (int64_t frame = 0; frame < 3; ++frame) {
        CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
                && encoder.EncodeFrame(start + period * frame),
            "AMF accepts an input before its delayed output is available");
    }
    CheckAmf(callback_pts.empty(),
        "AMF produces no callback while the fake encoder withholds output");
    fake->QueuePacket(fake->submitted_pts_history[0], true, {9, 8, 7, 6});
    fake->QueuePacket(fake->submitted_pts_history[1], false, {1, 3, 5});
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(start + period * 3),
        "AMF drains an older output after later inputs have been accepted");
    CheckAmf(callback_pts == std::vector<int64_t>{0, 1}
            && callback_wall_qpc == std::vector<int64_t>{start, start + period}
            && callback_bytes == std::vector<std::vector<uint8_t>>{
                {9, 8, 7, 6}, {1, 3, 5}},
        "delayed AMF packets keep source timing and distinct callback bytes");
}

void BackpressureDrainDeliversOlderPacketBeforeRetry() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    std::vector<std::pair<int64_t, int64_t>> callbacks;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [&](const uint8_t*, uint32_t, int64_t pts, bool,
                      int64_t wall_qpc) {
                      callbacks.push_back({pts, wall_qpc});
                  },
                  false),
        "AMF backpressure timing fake initializes");
    LARGE_INTEGER frequency{};
    CheckAmf(QueryPerformanceFrequency(&frequency) && frequency.QuadPart > 0,
        "QPC frequency is available for AMF backpressure output test");
    const int64_t start = 2'000'000;
    const int64_t period = frequency.QuadPart / 60;
    for (int64_t frame = 0; frame < 2; ++frame) {
        CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
                && encoder.EncodeFrame(start + period * frame),
            "AMF accepts an input before the backpressure retry");
    }
    fake->QueuePacket(fake->submitted_pts_history[0], false);
    fake->need_drain_once = true;
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(start + period * 2),
        "AMF retries a submission after draining output on backpressure");
    CheckAmf(fake->submit_calls == 4
            && callbacks == std::vector<std::pair<int64_t, int64_t>>{{0, start}},
        "backpressure drain delivers the older packet before successful retry");
}

void EndOfStreamEmitsFinalDelayedPacket() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    std::vector<std::pair<int64_t, int64_t>> callbacks;
    FfmpegAmfReplayEncoder encoder(VideoCodec::HEVC, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [&](const uint8_t*, uint32_t, int64_t pts, bool,
                      int64_t wall_qpc) {
                      callbacks.push_back({pts, wall_qpc});
                  },
                  false),
        "AMF EOS timing fake initializes");
    const int64_t present_qpc = 3'000'000;
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(present_qpc),
        "AMF accepts an input whose output is delayed until EOS");
    fake->QueuePacketAtEndOfStream(fake->submitted_pts_history[0], true);
    encoder.Shutdown();
    CheckAmf(fake->flush_called
            && callbacks == std::vector<std::pair<int64_t, int64_t>>{{0, present_qpc}},
        "AMF flush emits the final delayed packet with its input wall time");
}

void ReceiveAndFlushErrorsPropagate() {
    auto receive_session = std::make_unique<FakeAmfSession>();
    receive_session->receive_error = true;
    FfmpegAmfReplayEncoder receive_failure(
        VideoCodec::H264, std::move(receive_session));
    CheckAmf(receive_failure.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && receive_failure.PrepareGpuFrame(nullptr, 0)
            && !receive_failure.EncodeFrame(1)
            && receive_failure.GetLastError().find("receive failure")
                != std::string::npos,
        "AMF receive errors fail EncodeFrame and preserve backend detail");

    auto flush_session = std::make_unique<FakeAmfSession>();
    flush_session->flush_error = true;
    FfmpegAmfReplayEncoder flush_failure(
        VideoCodec::AV1, std::move(flush_session));
    CheckAmf(flush_failure.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF flush-error fake initializes");
    flush_failure.Shutdown();
    CheckAmf(flush_failure.GetLastError().find("flush failure")
                != std::string::npos,
        "AMF shutdown preserves a codec flush failure");
}

void ReinitializeStartsFreshTimingKeyframeAndConfig() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    std::vector<int64_t> first_generation_callbacks;
    std::vector<std::pair<int64_t, int64_t>> second_generation_callbacks;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    const auto initialize = [&](const EncoderConfig& config,
                                FfmpegAmfReplayEncoder::PacketCallback callback) {
        return encoder.Initialize(
            config,
            reinterpret_cast<ID3D11Device*>(1),
            reinterpret_cast<ID3D11DeviceContext*>(1),
            std::move(callback),
            false);
    };
    CheckAmf(initialize(TestEncoderConfig(),
                  [&](const uint8_t*, uint32_t, int64_t pts, bool, int64_t) {
                      first_generation_callbacks.push_back(pts);
                  }),
        "AMF first generation initializes");
    const int64_t first_start = 4'000'000;
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(first_start),
        "AMF first generation accepts an input with no output");
    encoder.Shutdown();

    EncoderConfig second_config = TestEncoderConfig();
    second_config.fps = 30;
    second_config.enc_width = 1280;
    second_config.enc_height = 720;
    const int64_t second_start = 8'000'000;
    CheckAmf(initialize(second_config,
                  [&](const uint8_t*, uint32_t, int64_t pts, bool, int64_t wall_qpc) {
                      second_generation_callbacks.push_back({pts, wall_qpc});
                  })
            && encoder.GetVideoConfig().width == 1280
            && encoder.GetVideoConfig().height == 720
            && encoder.GetVideoConfig().frame_rate.numerator == 30,
        "AMF reinitialization publishes only the new generation config");
    fake->QueuePacket(0, true);
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(second_start)
            && fake->submitted_pts_history == std::vector<int64_t>{0}
            && fake->submitted_force_keyframe_history
                == std::vector<bool>{true}
            && second_generation_callbacks
                == std::vector<std::pair<int64_t, int64_t>>{{0, second_start}}
            && first_generation_callbacks.empty(),
        "AMF reinitialization resets PTS, keyframe, and timing ownership");
}

void ScaledAmfConfigUsesEncoderDimensions() {
    auto session = std::make_unique<FakeAmfSession>();
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    EncoderConfig config = TestEncoderConfig();
    config.src_width = 2560;
    config.src_height = 1440;
    config.enc_width = 1920;
    config.enc_height = 1080;
    config.scaling_mode = 1;
    CheckAmf(encoder.Initialize(
                  config,
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF fake accepts a scaled encoder configuration");
    const auto active = encoder.GetVideoConfig();
    CheckAmf(active.width == 1920 && active.height == 1080,
        "AMF active config reports encoder dimensions");
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0),
        "AMF scaled frame preparation reaches the backend seam");
}

void AmfGpuPreparationFailurePropagates() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    fake->prepare_gpu_result = false;
    fake->prepare_gpu_error = "simulated AMF PrepareGpuFrame failure";
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && !encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.GetLastError().find("PrepareGpuFrame failure")
                != std::string::npos,
        "AMF GPU preparation failure propagates without fallback");
}

void AmfNoPacketOutputIsObservableAtTheSeam() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    fake->emit_packet = false;
    FfmpegAmfReplayEncoder encoder(VideoCodec::AV1, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(1)
            && fake->submitted_pts >= 0,
        "AMF output-without-packet remains observable to the stall watchdog seam");
}

void AmfDiagnosticsExposeBoundedProgress() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    fake->emit_packet = true;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF diagnostics fake initializes");
    CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
            && encoder.EncodeFrame(1),
        "AMF diagnostics observe a completed frame");
    const auto completed = encoder.GetDiagnostics();
    CheckAmf(completed.map_attempts == 1 && completed.maps_succeeded == 1
            && completed.encode_attempts == 1
            && completed.encode_returns == 1
            && completed.encode_successes == 1
            && completed.packets_produced == 1
            && completed.pending_resources == 0
            && completed.submit_stage == 0
            && completed.drain_stage == 0,
        "AMF diagnostics expose prepare/submit/packet progress without slots");

    auto backpressure_session = std::make_unique<FakeAmfSession>();
    auto* backpressure_fake = backpressure_session.get();
    backpressure_fake->need_drain_once = true;
    backpressure_fake->emit_packet = true;
    FfmpegAmfReplayEncoder backpressure(
        VideoCodec::H264, std::move(backpressure_session));
    CheckAmf(backpressure.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && backpressure.PrepareGpuFrame(nullptr, 0)
            && backpressure.EncodeFrame(1),
        "AMF backpressure test completes after one bounded drain");
    const auto drained = backpressure.GetDiagnostics();
    CheckAmf(backpressure_fake->submit_calls == 2
            && drained.encode_attempts == 2
            && drained.encode_returns == 2
            && drained.encode_successes == 1
            && drained.backpressure_events == 1,
        "AMF diagnostics expose bounded backpressure retries");

    auto failing_session = std::make_unique<FakeAmfSession>();
    failing_session->submit_error = true;
    FfmpegAmfReplayEncoder failing(VideoCodec::H264, std::move(failing_session));
    CheckAmf(failing.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && failing.PrepareGpuFrame(nullptr, 0)
            && !failing.EncodeFrame(1),
        "AMF diagnostics retain submit failure detail");
    const auto failed = failing.GetDiagnostics();
    CheckAmf(failed.encode_attempts == 1
            && failed.encode_returns == 1
            && failed.encode_successes == 0
            && failed.backend_errors == 1
            && failed.pending_resources == 0
            && failed.submit_stage == 0
            && failed.drain_stage == 0
            && failing.GetLastError().find("submit failure") != std::string::npos,
        "AMF diagnostics distinguish an error from an accepted submit");

    auto removed_session = std::make_unique<FakeAmfSession>();
    removed_session->prepare_gpu_result = false;
    removed_session->prepare_gpu_error =
        "ID3D11Device::GetDeviceRemovedReason(AMF): DXGI_ERROR_DEVICE_REMOVED";
    FfmpegAmfReplayEncoder removed(VideoCodec::H264, std::move(removed_session));
    CheckAmf(removed.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false)
            && !removed.PrepareGpuFrame(nullptr, 0)
            && removed.GetLastError().find(
                   "GetDeviceRemovedReason(AMF)") != std::string::npos,
        "AMF diagnostics preserve the device-removal API boundary");
    const auto removed_snapshot = removed.GetDiagnostics();
    CheckAmf(removed_snapshot.backend_errors == 1
            && removed_snapshot.device_removed_errors == 1
            && removed_snapshot.pending_resources == 0
            && removed_snapshot.submit_stage == 0
            && removed_snapshot.drain_stage == 0,
        "AMF diagnostics export device-removal and cleared activity state");

    auto bounded_session = std::make_unique<FakeAmfSession>();
    FfmpegAmfReplayEncoder bounded(VideoCodec::H264, std::move(bounded_session));
    CheckAmf(bounded.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF bounded diagnostics fake initializes");
    for (uint32_t frame = 0; frame < 20; ++frame) {
        CheckAmf(bounded.PrepareGpuFrame(nullptr, 0)
                && bounded.EncodeFrame(static_cast<int64_t>(frame + 1)),
            "AMF no-output frames remain submit-valid at the seam");
    }
    const auto bounded_snapshot = bounded.GetDiagnostics();
    CheckAmf(bounded_snapshot.pending_resources == 8
            && bounded_snapshot.queued_outputs == 8
            && bounded_snapshot.pool_capacity == 8,
        "AMF pending diagnostic state is bounded by the eight-frame pool");
    bounded.Shutdown();
    const auto shutdown_snapshot = bounded.GetDiagnostics();
    CheckAmf(shutdown_snapshot.pending_resources == 0
            && shutdown_snapshot.submit_stage == 0
            && shutdown_snapshot.drain_stage == 0,
        "AMF shutdown clears active diagnostic state");
}

void AmfKeyframeCadenceMatchesPublishedBound() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    fake->emit_packet = true;
    FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF keyframe cadence fake initializes");
    LARGE_INTEGER frequency{};
    CheckAmf(QueryPerformanceFrequency(&frequency) && frequency.QuadPart > 0,
        "QPC frequency is available for AMF cadence test");
    const int64_t start = 1'000'000;
    const int64_t period = frequency.QuadPart / 60;
    bool first = false;
    bool at_one_second = false;
    bool before_one_second = false;
    for (uint32_t frame = 0; frame <= 60; ++frame) {
        CheckAmf(encoder.PrepareGpuFrame(nullptr, 0)
                && encoder.EncodeFrame(start + period * frame),
            "AMF cadence frame submits through the wrapper");
        if (frame == 0) first = fake->submitted_force_keyframe;
        if (frame == 59) before_one_second = fake->submitted_force_keyframe;
        if (frame == 60) at_one_second = fake->submitted_force_keyframe;
    }
    CheckAmf(first && !before_one_second && at_one_second,
        "AMF explicitly forces keyframes at the advertised one-second bound");
}

void GenerationConfigCannotChange() {
    EncodedRingBuffer ring(8, 60, 1000);
    CheckAmf(ring.SetVideoConfig(ConfigFor(VideoCodec::H264))
            && !ring.SetVideoConfig(ConfigFor(VideoCodec::AV1)),
        "AMF generation rejects a codec/config change");
}

void FlushAndErrorsAreObservable() {
    auto session = std::make_unique<FakeAmfSession>();
    auto* fake = session.get();
    FfmpegAmfReplayEncoder encoder(VideoCodec::HEVC, std::move(session));
    CheckAmf(encoder.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                  false),
        "AMF fake session initializes for flush test");
    encoder.Shutdown();
    CheckAmf(fake->flush_called,
        "AMF shutdown flushes the codec session");

    auto failing_session = std::make_unique<FakeAmfSession>();
    failing_session->submit_error = true;
    FfmpegAmfReplayEncoder failing(VideoCodec::AV1, std::move(failing_session));
    CheckAmf(failing.Initialize(
                  TestEncoderConfig(),
                  reinterpret_cast<ID3D11Device*>(1),
                  reinterpret_cast<ID3D11DeviceContext*>(1),
                  [](const uint8_t*, uint32_t, int64_t, bool, int64_t) {},
                   false)
            && failing.PrepareGpuFrame(nullptr, 0)
            && !failing.EncodeFrame(1)
            && failing.GetLastError().find("submit failure") != std::string::npos,
        "AMF encode errors propagate without fallback");
}

void SuccessfulAmfUsesCompressedReplay() {
    CheckAmf(!fthr::ShouldAllocateRawReplayPool(
                  ReplayEncoderBackend::FfmpegAmf, true),
        "successful AMF selection skips the raw replay pool");
}

} // namespace

int RunAmfReplayEncoderTests() {
    AmdAdapterSelectsAmf();
    NvidiaStillSelectsNativeNvenc();
    IntelStageDoesNotAffectAmdPolicy();
    RequestedCodecsMapToExactAmfNames();
    MissingEncodersFailHonestly();
    InitializationFailuresPropagate();
    SilentCodecFallbackIsRejected();
    AmfVideoConfigsAreCodecCorrect();
    AmfFrameRateMatrixAndBounds();
    RingAndMuxRemainCodecNeutral();
    PacketMetadataPropagates();
    DelayedPacketsKeepTheirSourceTimingAndBorrowedBytes();
    BackpressureDrainDeliversOlderPacketBeforeRetry();
    EndOfStreamEmitsFinalDelayedPacket();
    ReceiveAndFlushErrorsPropagate();
    ReinitializeStartsFreshTimingKeyframeAndConfig();
    ScaledAmfConfigUsesEncoderDimensions();
    AmfGpuPreparationFailurePropagates();
    AmfNoPacketOutputIsObservableAtTheSeam();
    AmfDiagnosticsExposeBoundedProgress();
    AmfKeyframeCadenceMatchesPublishedBound();
    GenerationConfigCannotChange();
    FlushAndErrorsAreObservable();
    SuccessfulAmfUsesCompressedReplay();
    std::cout << "FTHRclips AMF tests completed ("
              << amf_checks << " checks)" << std::endl;
    return amf_checks;
}
