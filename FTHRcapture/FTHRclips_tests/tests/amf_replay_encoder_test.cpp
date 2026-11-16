#include "encoded_ring_buffer.h"
#include "encoded_video_config_ffmpeg.h"
#include "ffmpeg_amf_replay_encoder.h"
#include "replay_encoder.h"
#include "transactional_save.h"

#include <windows.h>
#include <d3d11.h>

extern "C" {
#include <libavutil/frame.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_d3d11va.h>
}

#include <cstdlib>
#include <deque>
#include <iostream>
#include <memory>
#include <set>
#include <sstream>
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
    bool always_need_drain = false;
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
        if (need_drain_once || always_need_drain) {
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
    CheckAmf(fake->submitted_pts_history[2] == fake->submitted_pts_history[3]
        && fake->submitted_force_keyframe_history[2] == fake->submitted_force_keyframe_history[3],
        "successful AMF EAGAIN retry retains the pending PTS and keyframe intent");
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

void FailureEvidenceKeepsErrorDomainsSeparateAndBounded() {
    fthr::detail::AmfFailureEvidence evidence;
    evidence.operation = "avcodec_send_frame";
    evidence.codec = "h264_amf";
    evidence.ffmpeg_error_available = true;
    evidence.ffmpeg_error = AVERROR_UNKNOWN;
    evidence.device_removed_available = true;
    evidence.device_removed_reason = 0;
    const auto json = fthr::detail::BuildAmfFailureEvidence(evidence);
    CheckAmf(json.find("\"native_amf_result\":\"unavailable:not_exposed_by_ffmpeg_api\"") != std::string::npos
        && json.find("\"hresult\":\"unavailable:not_observed\"") != std::string::npos
        && json.find("\"ffmpeg_error\":" + std::to_string(AVERROR_UNKNOWN)) != std::string::npos
        && json.find("\"device_removed_reason\":0") != std::string::npos
        && json.find("texture_operation_success") == std::string::npos,
        "AMF failure event does not invent a native result or HRESULT from FFmpeg UNKNOWN or S_OK");
    evidence.operation = std::string(100000, 'x');
    evidence.codec = std::string(100000, 'y');
    const auto bounded = fthr::detail::BuildAmfFailureEvidence(evidence);
    CheckAmf(bounded.size() < 8192 && bounded.back() == '}',
        "AMF failure evidence remains bounded and complete for oversized labels");
    evidence.operation = "quoted\"operation\n";
    evidence.hresult_available = true;
    evidence.hresult = static_cast<int32_t>(0x887A0005u);
    evidence.input.available = true;
    evidence.input.width = 1920;
    evidence.input.array_size = 8;
    evidence.input.subresource = 3;
    const auto native = fthr::detail::BuildAmfFailureEvidence(evidence);
    CheckAmf(native.find("\"hresult\":-2005270523") != std::string::npos
        && native.find("quoted\\\"operation\\n") != std::string::npos
        && native.find("\"subresource\":3") != std::string::npos
        && native.find("\"bind_flags\":0") != std::string::npos
        && native.find("\"source_device_matches\":\"unavailable:not_observed\"") != std::string::npos,
        "AMF evidence preserves real HRESULT, texture slice/flags, escaped labels and unavailable identities independently");
}

void ProductionFailureReporterRunsWithoutHardwareAndIsBoundedOnce() {
    auto session = fthr::detail::CreateProductionAmfCodecSession();
    std::ostringstream captured;
    auto* previous = std::cout.rdbuf(captured.rdbuf());
    std::string error;
    const bool first = session->PrepareGpuFrame(nullptr, 0, error);
    const bool second = session->PrepareGpuFrame(nullptr, 0, error);
    std::cout.rdbuf(previous);
    const auto output = captured.str();
    const std::string marker = "FTHR_DIAGNOSTIC_EVENT ";
    const auto position = output.find(marker);
    CheckAmf(!first && !second && position != std::string::npos
        && output.find(marker, position + marker.size()) == std::string::npos
        && output.find("PrepareGpuFrame:resources") != std::string::npos
        && output.find("unavailable:not_exposed_by_ffmpeg_api") != std::string::npos,
        "actual production AMF failure reporter executes and emits only one bounded event before reset");
}

void SubmitFailuresDoNotFabricatePacketsOrRetryFatalErrors() {
    for (const bool fatal : {false, true}) {
        auto session = std::make_unique<FakeAmfSession>();
        auto* fake = session.get();
        fake->submit_error = fatal;
        fake->always_need_drain = !fatal;
        uint32_t packets = 0;
        FfmpegAmfReplayEncoder encoder(VideoCodec::H264, std::move(session));
        const bool initialized = encoder.Initialize(TestEncoderConfig(),
            reinterpret_cast<ID3D11Device*>(1), reinterpret_cast<ID3D11DeviceContext*>(1),
            [&](const uint8_t*, uint32_t, int64_t, bool, int64_t) { ++packets; }, false);
        CheckAmf(initialized && encoder.PrepareGpuFrame(nullptr, 0) && !encoder.EncodeFrame(1)
            && fake->submit_calls == (fatal ? 1u : 2u) && packets == 0,
            "AMF fatal sends run once; persistent EAGAIN is bounded to two attempts without fabricated packets");
        CheckAmf(fake->submitted_pts_history.front() == fake->submitted_pts_history.back()
            && fake->submitted_force_keyframe_history.front() == fake->submitted_force_keyframe_history.back(),
            "AMF backpressure retries retain pending PTS and keyframe intent");
    }
}

void SuccessfulAmfUsesCompressedReplay() {
    CheckAmf(!fthr::ShouldAllocateRawReplayPool(
                  ReplayEncoderBackend::FfmpegAmf, true),
        "successful AMF selection skips the raw replay pool");
}

bool fail_next_pool_texture = false;
HRESULT FailFirstPoolTexture(ID3D11Device* device,
    const D3D11_TEXTURE2D_DESC* description, ID3D11Texture2D** texture) {
    if (fail_next_pool_texture) { fail_next_pool_texture = false; return E_OUTOFMEMORY; }
    return device->CreateTexture2D(description, nullptr, texture);
}

void LogPoolTestAdapter(ID3D11Device* device, const char* mode) {
    std::cout << "AMF input-pool contract: D3D11 mode=" << mode
              << ", no AMF encoding";
    IDXGIDevice* dxgi_device = nullptr;
    IDXGIAdapter* adapter = nullptr;
    DXGI_ADAPTER_DESC description{};
    if (SUCCEEDED(device->QueryInterface(__uuidof(IDXGIDevice),
            reinterpret_cast<void**>(&dxgi_device)))
        && SUCCEEDED(dxgi_device->GetAdapter(&adapter))
        && SUCCEEDED(adapter->GetDesc(&description))) {
        char name[512]{};
        WideCharToMultiByte(CP_UTF8, 0, description.Description, -1,
            name, sizeof(name), nullptr, nullptr);
        std::cout << ", vendor_id=" << description.VendorId << ", adapter=" << name;
    } else std::cout << ", adapter=unavailable";
    if (adapter) adapter->Release();
    if (dxgi_device) dxgi_device->Release();
    std::cout << std::endl;
}

void BoundedSingleTexturePoolUsesActualFfmpegFrameOwnership() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* immediate = nullptr;
    D3D_FEATURE_LEVEL level{};
    const HRESULT hardware_result = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE,
        nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        nullptr, 0, D3D11_SDK_VERSION, &device, &level, &immediate);
    if (FAILED(hardware_result)) {
        if (immediate) immediate->Release();
        if (device) device->Release();
        std::cout << "AMF FFmpeg hardware-frame integration: UNAVAILABLE, D3D11CreateDevice HRESULT="
                  << hardware_result << "; mandatory WARP factory contracts still run" << std::endl;
        return;
    }
    LogPoolTestAdapter(device, "HARDWARE, actual FFmpeg hwframe integration");
    AVBufferRef* device_ref = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
    CheckAmf(device_ref != nullptr, "pool test allocates actual FFmpeg device context");
    auto* device_ctx = reinterpret_cast<AVHWDeviceContext*>(device_ref->data);
    device->AddRef();
    reinterpret_cast<AVD3D11VADeviceContext*>(device_ctx->hwctx)->device = device;
    const int device_init = av_hwdevice_ctx_init(device_ref);
    if (device_init < 0) {
        av_buffer_unref(&device_ref);
        immediate->Release();
        device->Release();
        std::cout << "AMF FFmpeg hardware-frame integration: UNAVAILABLE, pinned av_hwdevice_ctx_init="
                  << device_init << "; mandatory WARP factory contracts still run" << std::endl;
        return;
    }
    auto create_frames = [&]() {
        auto* reference = av_hwframe_ctx_alloc(device_ref);
        CheckAmf(reference != nullptr, "pool test allocates frames context");
        auto* context = reinterpret_cast<AVHWFramesContext*>(reference->data);
        context->format = AV_PIX_FMT_D3D11;
        context->sw_format = AV_PIX_FMT_BGRA;
        context->width = 640;
        context->height = 360;
        return reference;
    };
    auto stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    AVBufferRef* frames_ref = create_frames();
    auto* frames = reinterpret_cast<AVHWFramesContext*>(frames_ref->data);
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(frames, device, stats) == 0
        && frames->initial_pool_size == 0 && frames->pool && frames->free,
        "production bounded caller pool is owned by frames-context final-free hook");
    CheckAmf(av_hwframe_ctx_init(frames_ref) == 0, "actual FFmpeg accepts bounded descriptor pool");
    std::vector<AVFrame*> held;
    std::set<ID3D11Texture2D*> textures;
    for (int index = 0; index < 8; ++index) {
        auto* frame = av_frame_alloc();
        CheckAmf(frame && fthr::detail::AcquireAmfInputFrame(frames_ref, frame) == 0,
            "each of eight actual FFmpeg frames acquires a bounded texture");
        auto* texture = reinterpret_cast<ID3D11Texture2D*>(frame->data[0]);
        D3D11_TEXTURE2D_DESC description{};
        texture->GetDesc(&description);
        auto* descriptor = reinterpret_cast<AVD3D11FrameDescriptor*>(frame->buf[0]->data);
        CheckAmf(description.ArraySize == 1 && description.MipLevels == 1
            && description.Format == DXGI_FORMAT_B8G8R8A8_UNORM
            && description.Usage == D3D11_USAGE_DEFAULT && description.CPUAccessFlags == 0
            && frame->data[1] == nullptr && descriptor->texture == texture && descriptor->index == 0,
            "production pool supplies FFmpeg descriptor ABI with GPU-only single texture and slice zero");
        textures.insert(texture);
        held.push_back(frame);
    }
    CheckAmf(textures.size() == 8 && stats->live_textures == 8,
        "eight held frames have eight distinct live single textures");
    auto* ninth = av_frame_alloc();
    CheckAmf(fthr::detail::AcquireAmfInputFrame(frames_ref, ninth) < 0
        && stats->capacity_exhausted && stats->live_textures == 8,
        "ninth concurrent acquisition fails immediately without allocating beyond eight");
    auto* clone = av_frame_clone(held.front());
    CheckAmf(clone != nullptr, "actual AVFrame clone retains texture lease");
    auto* first_texture = reinterpret_cast<ID3D11Texture2D*>(clone->data[0]);
    av_frame_free(&held.front());
    CheckAmf(fthr::detail::AcquireAmfInputFrame(frames_ref, ninth) < 0,
        "original frame release cannot reuse texture still held by clone");
    av_frame_free(&clone);
    CheckAmf(fthr::detail::AcquireAmfInputFrame(frames_ref, ninth) == 0
        && reinterpret_cast<ID3D11Texture2D*>(ninth->data[0]) == first_texture
        && !stats->capacity_exhausted && stats->live_textures == 8,
        "last clone release permits exact texture reuse without new allocation");
    av_buffer_unref(&frames_ref);
    av_buffer_unref(&device_ref);
    CheckAmf(!stats->state_destroyed, "owner reset cannot destroy context/pool while actual frames retain it");
    D3D11_TEXTURE2D_DESC still_valid{};
    first_texture->GetDesc(&still_valid);
    CheckAmf(still_valid.Width == 640, "outstanding frame texture remains valid after owner reset");
    auto* buffer_only_lease = av_buffer_ref(ninth->buf[0]);
    CheckAmf(buffer_only_lease != nullptr, "separate buffer lease does not require frames-context reference");
    for (auto*& frame : held) av_frame_free(&frame);
    av_frame_free(&ninth);
    CheckAmf(!stats->state_destroyed && stats->live_textures == 1,
        "pool state survives final context-free hook while separate buffer lease remains");
    first_texture->GetDesc(&still_valid);
    CheckAmf(still_valid.Width == 640, "separate buffer lease texture survives closed pool");
    av_buffer_unref(&buffer_only_lease);
    CheckAmf(stats->state_destroyed && stats->live_textures == 0,
        "last frame triggers context hook and frees all pool textures safely");

    // An initialization/allocation failure must not consume capacity or leave a dangling hook.
    device_ref = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
    device_ctx = reinterpret_cast<AVHWDeviceContext*>(device_ref->data);
    device->AddRef();
    reinterpret_cast<AVD3D11VADeviceContext*>(device_ctx->hwctx)->device = device;
    CheckAmf(av_hwdevice_ctx_init(device_ref) == 0, "allocation-failure test initializes device");
    frames_ref = create_frames();
    frames = reinterpret_cast<AVHWFramesContext*>(frames_ref->data);
    auto failure_stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    fail_next_pool_texture = true;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(frames, device, failure_stats,
        FailFirstPoolTexture) == 0 && av_hwframe_ctx_init(frames_ref) == 0,
        "fault seam uses same production pool with actual FFmpeg context");
    auto* retry = av_frame_alloc();
    CheckAmf(fthr::detail::AcquireAmfInputFrame(frames_ref, retry) < 0
        && failure_stats->live_textures == 0 && !failure_stats->capacity_exhausted
        && failure_stats->hresult_available && failure_stats->hresult == E_OUTOFMEMORY,
        "real texture allocation HRESULT is distinct from exhaustion and rolls back capacity");
    CheckAmf(fthr::detail::AcquireAmfInputFrame(frames_ref, retry) == 0
        && failure_stats->live_textures == 1 && !failure_stats->hresult_available,
        "allocation failure is recoverable without permanently losing a pool slot");
    av_frame_free(&retry);
    av_buffer_unref(&frames_ref);
    CheckAmf(failure_stats->state_destroyed && failure_stats->live_textures == 0,
        "failure/retry context teardown releases state and texture");
    frames_ref = create_frames();
    frames = reinterpret_cast<AVHWFramesContext*>(frames_ref->data);
    auto partial_stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(frames, device, partial_stats) == 0,
        "partial initialization installs owned pool");
    frames->width = 0;
    CheckAmf(av_hwframe_ctx_init(frames_ref) < 0, "invalid initialization actually fails in FFmpeg");
    av_buffer_unref(&frames_ref);
    CheckAmf(partial_stats->state_destroyed && partial_stats->live_textures == 0,
        "failed frames initialization still runs context pool cleanup");
    av_buffer_unref(&device_ref);
    immediate->Release();
    device->Release();
}

void BoundedSingleTexturePoolWarpFactoryContractAlwaysRuns() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* immediate = nullptr;
    D3D_FEATURE_LEVEL level{};
    CheckAmf(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
        &device, &level, &immediate)), "mandatory WARP pool test creates BGRA device without VIDEO_SUPPORT");
    LogPoolTestAdapter(device, "WARP, direct factory/Stock AVBufferPool contract only");
    // WARP need not expose video interfaces required by pinned hwdevice_init.
    // Exercise the same production caller-pool factory before backend init,
    // using an isolated stack context and its actual registered free hook.
    AVHWFramesContext frames{};
    frames.format = AV_PIX_FMT_D3D11;
    frames.sw_format = AV_PIX_FMT_BGRA;
    frames.width = 640;
    frames.height = 360;
    auto stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device, stats) == 0
        && frames.pool && frames.free && frames.initial_pool_size == 0,
        "WARP installs the identical production caller-pool factory without fake FFmpeg device init");
    auto acquire = [&]() {
        auto* frame = av_frame_alloc();
        CheckAmf(frame != nullptr, "WARP allocates actual AVFrame");
        frame->buf[0] = av_buffer_pool_get(frames.pool);
        if (!frame->buf[0]) { av_frame_free(&frame); return frame; }
        const auto* descriptor = reinterpret_cast<AVD3D11FrameDescriptor*>(frame->buf[0]->data);
        frame->data[0] = reinterpret_cast<uint8_t*>(descriptor->texture);
        frame->data[1] = reinterpret_cast<uint8_t*>(descriptor->index);
        frame->format = AV_PIX_FMT_D3D11;
        frame->width = frames.width;
        frame->height = frames.height;
        return frame;
    };
    std::vector<AVFrame*> held;
    std::set<ID3D11Texture2D*> textures;
    for (int index = 0; index < 8; ++index) {
        auto* frame = acquire();
        CheckAmf(frame != nullptr, "WARP acquires eight simultaneous production-pool textures");
        auto* texture = reinterpret_cast<ID3D11Texture2D*>(frame->data[0]);
        D3D11_TEXTURE2D_DESC description{};
        texture->GetDesc(&description);
        CheckAmf(description.ArraySize == 1 && description.MipLevels == 1
            && description.Format == DXGI_FORMAT_B8G8R8A8_UNORM
            && description.Usage == D3D11_USAGE_DEFAULT && description.CPUAccessFlags == 0
            && frame->data[1] == nullptr, "WARP validates actual single-texture descriptor and slice zero");
        textures.insert(texture);
        held.push_back(frame);
    }
    CheckAmf(textures.size() == 8 && stats->live_textures == 8,
        "WARP holds eight distinct GPU-resource textures, no ninth allocation");
    CheckAmf(acquire() == nullptr && stats->capacity_exhausted && stats->live_textures == 8,
        "WARP ninth lease fails boundedly at capacity eight");
    auto* clone = av_frame_clone(held.front());
    CheckAmf(clone != nullptr, "WARP real AVFrame clone retains production-pool lease");
    auto* first_texture = reinterpret_cast<ID3D11Texture2D*>(clone->data[0]);
    av_frame_free(&held.front());
    CheckAmf(acquire() == nullptr, "WARP frame clone blocks reuse after original release");
    av_frame_free(&clone);
    held.front() = acquire();
    CheckAmf(held.front() && held.front()->data[0] == reinterpret_cast<uint8_t*>(first_texture)
        && stats->live_textures == 8, "WARP final clone release permits exact stock-pool reuse");
    auto* buffer_lease = av_buffer_ref(held.front()->buf[0]);
    CheckAmf(buffer_lease != nullptr, "WARP retains separate actual AVBufferRef");
    for (auto*& frame : held) av_frame_free(&frame);
    frames.free(&frames);
    CheckAmf(!frames.pool && !frames.user_opaque && !stats->state_destroyed
        && stats->live_textures == 1, "actual context-free hook closes WARP pool without invalidating buffer lease");
    D3D11_TEXTURE2D_DESC still_valid{};
    first_texture->GetDesc(&still_valid);
    CheckAmf(still_valid.Width == 640, "WARP descriptor/texture remains valid after context-free hook");
    av_buffer_unref(&buffer_lease);
    CheckAmf(stats->state_destroyed && stats->live_textures == 0,
        "WARP final buffer ref frees texture and pool state");

    AVHWFramesContext partial{};
    partial.format = AV_PIX_FMT_D3D11;
    partial.sw_format = AV_PIX_FMT_BGRA;
    partial.width = 640;
    partial.height = 360;
    auto failure_stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    fail_next_pool_texture = true;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&partial, device, failure_stats,
        FailFirstPoolTexture) == 0, "WARP installs same production factory with allocation fault seam");
    CheckAmf(!av_buffer_pool_get(partial.pool) && failure_stats->live_textures == 0
        && failure_stats->hresult_available && failure_stats->hresult == E_OUTOFMEMORY
        && !failure_stats->capacity_exhausted, "WARP allocation failure rolls capacity back with distinct actual HRESULT");
    auto* retry = av_buffer_pool_get(partial.pool);
    CheckAmf(retry && failure_stats->live_textures == 1, "WARP allocation retry succeeds without capacity leak");
    av_buffer_unref(&retry);
    partial.free(&partial);
    CheckAmf(failure_stats->state_destroyed && failure_stats->live_textures == 0,
        "WARP partially initialized context cleanup frees actual factory resources");
    immediate->Release();
    device->Release();
}

bool nv12_descriptor_observed = false;
HRESULT ObserveNv12DescriptorFailure(ID3D11Device*,
    const D3D11_TEXTURE2D_DESC* description, ID3D11Texture2D**) {
    nv12_descriptor_observed = description->Format == DXGI_FORMAT_NV12
        && description->Width == 640 && description->Height == 360
        && description->ArraySize == 1 && description->MipLevels == 1
        && description->SampleDesc.Count == 1 && description->CPUAccessFlags == 0
        && description->Usage == D3D11_USAGE_DEFAULT;
    return E_OUTOFMEMORY;
}

void Nv12PoolValidatesFormatGeometryAndFailureLifetime() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    CheckAmf(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
        &device, nullptr, &context)), "NV12 descriptor contract creates WARP device");
    AVHWFramesContext frames{};
    frames.format = AV_PIX_FMT_D3D11;
    frames.sw_format = AV_PIX_FMT_NV12;
    frames.width = 641;
    frames.height = 360;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device) == AVERROR(EINVAL)
        && !frames.pool, "odd NV12 width is rejected before pool/resource creation");
    frames.width = 640;
    frames.height = 361;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device) == AVERROR(EINVAL),
        "odd NV12 height is rejected");
    frames.height = 360;
    frames.sw_format = AV_PIX_FMT_P010;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device) == AVERROR(EINVAL),
        "unqualified P010 cannot be substituted for NV12");
    frames.sw_format = AV_PIX_FMT_NV12;
    auto stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    nv12_descriptor_observed = false;
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device, stats,
        ObserveNv12DescriptorFailure) == 0, "NV12 installs the bounded descriptor factory");
    auto* failed = av_buffer_pool_get(frames.pool);
    CheckAmf(!failed && nv12_descriptor_observed && stats->live_textures == 0
        && stats->hresult_available && stats->hresult == static_cast<int32_t>(E_OUTOFMEMORY),
        "NV12 descriptor, allocation rollback and original HRESULT are preserved");
    frames.free(&frames);
    CheckAmf(stats->state_destroyed && !frames.pool && !frames.user_opaque,
        "NV12 failed allocation cleans up the real FFmpeg pool");
    context->Release();
    device->Release();
}

void Nv12HardwarePoolRemainsBoundedWithoutClaimingAmfEncoding() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    const HRESULT created = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
        &device, nullptr, &context);
    if (FAILED(created)) {
        std::cout << "NV12 hardware pool: unavailable; no AMF encoding claimed\n";
        return;
    }
    LogPoolTestAdapter(device, "HARDWARE, NV12 caller-pool resources only; no AMF encoding");
    AVHWFramesContext frames{};
    frames.format = AV_PIX_FMT_D3D11;
    frames.sw_format = AV_PIX_FMT_NV12;
    frames.width = 640;
    frames.height = 360;
    auto stats = std::make_shared<fthr::detail::AmfInputPoolDiagnostics>();
    CheckAmf(fthr::detail::InstallAmfSingleTexturePool(&frames, device, stats) == 0,
        "hardware installs identical bounded NV12 pool");
    std::vector<AVBufferRef*> held;
    std::set<ID3D11Texture2D*> textures;
    for (int index = 0; index < 8; ++index) {
        auto* lease = av_buffer_pool_get(frames.pool);
        if (!lease && index == 0) {
            CheckAmf(stats->live_textures == 0, "unsupported NV12 allocation rolls back capacity");
            std::cout << "NV12 texture allocation unavailable on this hardware; no AMF encoding claimed\n";
            frames.free(&frames);
            context->Release();
            device->Release();
            return;
        }
        CheckAmf(lease != nullptr, "eight simultaneous hardware NV12 leases are available");
        auto* descriptor = reinterpret_cast<AVD3D11FrameDescriptor*>(lease->data);
        D3D11_TEXTURE2D_DESC actual{};
        descriptor->texture->GetDesc(&actual);
        CheckAmf(actual.Format == DXGI_FORMAT_NV12 && actual.ArraySize == 1
            && actual.MipLevels == 1 && descriptor->index == 0,
            "hardware NV12 frame has actual single-texture layout and slice zero");
        textures.insert(descriptor->texture);
        held.push_back(lease);
    }
    auto* ninth = av_buffer_pool_get(frames.pool);
    CheckAmf(!ninth && textures.size() == 8 && stats->capacity_exhausted,
        "NV12 cannot allocate a ninth texture");
    frames.free(&frames);
    CheckAmf(stats->live_textures == 8 && !stats->state_destroyed,
        "hardware NV12 leases survive context destruction");
    for (auto*& lease : held) av_buffer_unref(&lease);
    CheckAmf(stats->live_textures == 0 && stats->state_destroyed,
        "last NV12 lease releases all textures and pool state");
    context->Release();
    device->Release();
}

void AmfVideoProcessorScalesShaderResourceOnlyCapture() {
    using Microsoft::WRL::ComPtr;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    const HRESULT created = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        nullptr, 0, D3D11_SDK_VERSION, device.GetAddressOf(), nullptr, context.GetAddressOf());
    if (FAILED(created)) {
        std::cout << "AMF resize test skipped: hardware D3D11 video device unavailable\n";
        return;
    }
    ComPtr<ID3D11VideoDevice> video_device;
    ComPtr<ID3D11VideoContext> video_context;
    CheckAmf(SUCCEEDED(device.As(&video_device)) && SUCCEEDED(context.As(&video_context)),
        "resize test obtains the hardware video device and context");
    LogPoolTestAdapter(device.Get(), "HARDWARE, BGRA video processor resize");

    D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
    content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    content.InputFrameRate = content.OutputFrameRate = {60, 1};
    content.InputWidth = 1600;
    content.InputHeight = 900;
    content.OutputWidth = 1280;
    content.OutputHeight = 720;
    content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
    UINT format_support = 0;
    if (FAILED(video_device->CreateVideoProcessorEnumerator(&content, enumerator.GetAddressOf()))
        || FAILED(enumerator->CheckVideoProcessorFormat(DXGI_FORMAT_B8G8R8A8_UNORM, &format_support))
        || !(format_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT)
        || !(format_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT)) {
        std::cout << "AMF resize test skipped: hardware BGRA video processing unavailable\n";
        return;
    }
    ComPtr<ID3D11VideoProcessor> processor;
    CheckAmf(SUCCEEDED(video_device->CreateVideoProcessor(enumerator.Get(), 0, processor.GetAddressOf())),
        "resize test creates real video processor");
    const RECT source_rect{0, 0, static_cast<LONG>(content.InputWidth),
        static_cast<LONG>(content.InputHeight)};
    const RECT target_rect{0, 0, static_cast<LONG>(content.OutputWidth),
        static_cast<LONG>(content.OutputHeight)};
    video_context->VideoProcessorSetStreamFrameFormat(processor.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    video_context->VideoProcessorSetStreamSourceRect(processor.Get(), 0, TRUE, &source_rect);
    video_context->VideoProcessorSetStreamDestRect(processor.Get(), 0, TRUE, &target_rect);
    video_context->VideoProcessorSetOutputTargetRect(processor.Get(), TRUE, &target_rect);
    video_context->VideoProcessorSetStreamAutoProcessingMode(processor.Get(), 0, FALSE);

    fthr::detail::AmfVideoProcessorInput input;
    CheckAmf(SUCCEEDED(input.Initialize(device.Get(), video_device.Get(), enumerator.Get(),
        content.InputWidth, content.InputHeight)), "real reusable BGRA input view initializes before frame preparation");

    D3D11_TEXTURE2D_DESC description{};
    description.Width = content.InputWidth;
    description.Height = content.InputHeight;
    description.MipLevels = 1;
    description.ArraySize = 2;
    description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    description.SampleDesc.Count = 1;
    description.Usage = D3D11_USAGE_DEFAULT;
    description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Texture2D> capture;
    CheckAmf(SUCCEEDED(device->CreateTexture2D(&description, nullptr, capture.GetAddressOf())),
        "resize test creates a shader-resource-only source");
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC direct_description{};
    direct_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    const HRESULT direct_result = video_device->CreateVideoProcessorInputView(
        capture.Get(), enumerator.Get(), &direct_description, nullptr);
    std::cout << "Shader-resource-only direct video input view check HRESULT=0x"
              << std::hex << static_cast<uint32_t>(direct_result) << std::dec << '\n';

    D3D11_TEXTURE2D_DESC output_description = description;
    output_description.Width = content.OutputWidth;
    output_description.Height = content.OutputHeight;
    output_description.ArraySize = 1;
    output_description.BindFlags = D3D11_BIND_RENDER_TARGET;
    ComPtr<ID3D11Texture2D> output;
    ComPtr<ID3D11VideoProcessorOutputView> output_view;
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC view_description{};
    view_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    CheckAmf(SUCCEEDED(device->CreateTexture2D(&output_description, nullptr, output.GetAddressOf()))
        && SUCCEEDED(video_device->CreateVideoProcessorOutputView(output.Get(), enumerator.Get(),
            &view_description, output_view.GetAddressOf())), "resize test creates a 1280x720 output view");

    output_description.BindFlags = 0;
    output_description.Usage = D3D11_USAGE_STAGING;
    output_description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> readback;
    CheckAmf(SUCCEEDED(device->CreateTexture2D(&output_description, nullptr, readback.GetAddressOf())),
        "resize test creates a readback texture for pixel verification");
    ComPtr<ID3D11VideoProcessorInputView> first_view;
    for (UINT slice = 0; slice < description.ArraySize; ++slice) {
        const uint8_t gray = slice == 0 ? 48 : 192;
        const uint32_t bgra = 0xff000000 | (gray * 0x00010101u);
        const std::vector<uint32_t> pixels(description.Width * description.Height, bgra);
        context->UpdateSubresource(capture.Get(), slice, nullptr, pixels.data(), description.Width * 4, 0);

        ComPtr<ID3D11VideoProcessorInputView> view;
        CheckAmf(SUCCEEDED(input.Prepare(context.Get(), video_device.Get(), enumerator.Get(), capture.Get(),
            description, slice, view.GetAddressOf())) && view, "shader-resource-only slice prepares a valid input view");
        if (!first_view) first_view = view;
        ComPtr<ID3D11Resource> view_resource;
        view->GetResource(view_resource.GetAddressOf());
        CheckAmf(view.Get() == first_view.Get() && view_resource.Get() != capture.Get(),
            "shader-resource-only frames reuse one intermediate view and texture");

        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.pInputSurface = view.Get();
        CheckAmf(SUCCEEDED(video_context->VideoProcessorBlt(processor.Get(), output_view.Get(), 0, 1, &stream)),
            "hardware video processor scales the copied BGRA frame to 720p");
        context->CopyResource(readback.Get(), output.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        CheckAmf(SUCCEEDED(context->Map(readback.Get(), 0, D3D11_MAP_READ, 0, &mapped)),
            "resized pixels can be read back");
        const auto* pixel = static_cast<const uint8_t*>(mapped.pData)
            + (output_description.Height / 2) * mapped.RowPitch + (output_description.Width / 2) * 4;
        CheckAmf(std::abs(int(pixel[0]) - gray) <= 16 && std::abs(int(pixel[1]) - gray) <= 16
            && std::abs(int(pixel[2]) - gray) <= 16, "scaled pixels match the current source array slice");
        context->Unmap(readback.Get(), 0);
    }

    description.ArraySize = 1;
    description.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Texture2D> compatible;
    ComPtr<ID3D11VideoProcessorInputView> direct_view;
    CheckAmf(SUCCEEDED(device->CreateTexture2D(&description, nullptr, compatible.GetAddressOf()))
        && SUCCEEDED(input.Prepare(context.Get(), video_device.Get(), enumerator.Get(), compatible.Get(),
            description, 0, direct_view.GetAddressOf())) && direct_view,
        "compatible BGRA source prepares for resizing");
    ComPtr<ID3D11Resource> direct_resource;
    direct_view->GetResource(direct_resource.GetAddressOf());
    CheckAmf(direct_resource.Get() == compatible.Get(), "compatible BGRA source needs no intermediate GPU copy");
    input.Reset();
    direct_view.Reset();
    CheckAmf(input.Prepare(context.Get(), video_device.Get(), enumerator.Get(), compatible.Get(),
        description, 0, direct_view.GetAddressOf()) == E_UNEXPECTED && !direct_view,
        "resize input cannot be reused after reset until reinitialized");
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
    FailureEvidenceKeepsErrorDomainsSeparateAndBounded();
    ProductionFailureReporterRunsWithoutHardwareAndIsBoundedOnce();
    SubmitFailuresDoNotFabricatePacketsOrRetryFatalErrors();
    SuccessfulAmfUsesCompressedReplay();
    BoundedSingleTexturePoolUsesActualFfmpegFrameOwnership();
    BoundedSingleTexturePoolWarpFactoryContractAlwaysRuns();
    Nv12PoolValidatesFormatGeometryAndFailureLifetime();
    Nv12HardwarePoolRemainsBoundedWithoutClaimingAmfEncoding();
    AmfVideoProcessorScalesShaderResourceOnlyCapture();
    std::cout << "FTHRclips AMF tests completed ("
              << amf_checks << " checks)" << std::endl;
    return amf_checks;
}
