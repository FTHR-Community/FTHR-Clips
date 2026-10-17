// FFmpeg AMF replay encoder for a same-adapter AMD D3D11 capture path.

#pragma once
#ifndef FTHR_FFMPEG_AMF_REPLAY_ENCODER_H
#define FTHR_FFMPEG_AMF_REPLAY_ENCODER_H

#include "replay_encoder.h"

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <vector>

namespace fthr {

struct AmfCodecSelection {
    VideoCodec codec;
    const char* encoder_name;
    EncodedPacketFormat packet_format;
    int32_t profile;
};

const AmfCodecSelection* GetAmfCodecSelection(VideoCodec codec) noexcept;

EncodedVideoConfig BuildAmfVideoConfig(
    VideoCodec codec,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    uint32_t bitrate_kbps,
    const std::vector<uint8_t>& codec_extradata);

namespace detail {

enum class AmfSubmitStatus : uint32_t {
    Accepted,
    NeedDrain,
    Error,
};

enum class AmfReceiveStatus : uint32_t {
    Packet,
    NeedMoreInput,
    EndOfStream,
    Error,
};

struct AmfPacketView {
    const uint8_t* data = nullptr;
    uint32_t size = 0;
    int64_t pts = 0;
    int64_t dts = 0;
    bool keyframe = false;
};

// Narrow FFmpeg seam: the production implementation owns all AV*/D3D11
// hardware-context objects, while deterministic tests can exercise wrapper
// lifetime, failure, timestamp and packet behavior without claiming hardware.
class IAmfCodecSession {
public:
    virtual ~IAmfCodecSession() = default;

    virtual bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        const AmfCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) = 0;
    virtual bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource,
        std::string& error) = 0;
    virtual ID3D11Texture2D* GetCurrentInputTexture() const noexcept = 0;
    virtual uint32_t GetCurrentInputSubresource() const noexcept = 0;
    virtual AmfSubmitStatus SubmitFrame(
        int64_t pts, bool force_keyframe, std::string& error) = 0;
    virtual AmfReceiveStatus ReceivePacket(
        AmfPacketView& packet, std::string& error) = 0;
    virtual AmfSubmitStatus SendEndOfStream(std::string& error) = 0;
    virtual void Reset() noexcept = 0;
};

std::unique_ptr<IAmfCodecSession> CreateProductionAmfCodecSession();

} // namespace detail

class FfmpegAmfReplayEncoder final : public IReplayEncoder {
public:
    explicit FfmpegAmfReplayEncoder(VideoCodec codec);
    FfmpegAmfReplayEncoder(
        VideoCodec codec,
        std::unique_ptr<detail::IAmfCodecSession> session);
    ~FfmpegAmfReplayEncoder() override;

    bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        PacketCallback callback,
        bool cpu_input_mode) override;
    bool RequiresBackendGpuPreparation() const noexcept override;
    bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource = 0) override;
    bool EncodeFrame(int64_t present_qpc = 0) override;
    bool EncodeFrameCPU(
        const uint8_t* bgra_data,
        uint32_t source_stride,
        int64_t present_qpc = 0) override;
    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override;
    uint32_t GetCurrentInputSubresource() const noexcept override;
    void Shutdown() override;
    EncodedVideoConfig GetVideoConfig() const override;
    ActiveEncoderInfo GetActiveEncoderInfo() const override;
    std::string GetLastError() const override;
    ReplayEncoderDiagnostics GetDiagnostics() const noexcept override;
    bool GetEncodeEpoch(
        int64_t& start_qpc, int64_t& qpc_frequency) const override;
    bool IsInitialized() const override;

private:
    struct SubmittedTiming {
        int64_t pts;
        int64_t wall_qpc;
    };

    int64_t ComputePts(int64_t present_qpc);
    int64_t WallQpcForPts(int64_t pts);
    bool DrainAvailable(bool flushing);
    bool SubmitWithBackpressure(int64_t pts, bool force_keyframe);
    bool Flush();
    void SetError(std::string error);
    void ResetDiagnostics() noexcept;
    void ClearDiagnosticActivity() noexcept;

    // AMF has no NVENC-style registered resources or completion events. These
    // counters are backend-neutral progress evidence and are exported through
    // the existing ReplayEncoderDiagnostics fields without pretending that
    // AMF has NVENC slots. GetLastError() remains the authoritative bounded
    // detail for the last submit/receive/device-removal failure.
    std::atomic<uint64_t> diagnostic_prepare_attempts_{0};
    std::atomic<uint64_t> diagnostic_prepare_successes_{0};
    std::atomic<uint64_t> diagnostic_submit_attempts_{0};
    std::atomic<uint64_t> diagnostic_submit_returns_{0};
    std::atomic<uint64_t> diagnostic_submit_successes_{0};
    std::atomic<uint64_t> diagnostic_backpressure_{0};
    std::atomic<uint64_t> diagnostic_receive_attempts_{0};
    std::atomic<uint64_t> diagnostic_packets_{0};
    std::atomic<uint64_t> diagnostic_errors_{0};
    std::atomic<uint64_t> diagnostic_device_removed_{0};
    std::atomic<uint32_t> diagnostic_submit_stage_{0};
    std::atomic<uint32_t> diagnostic_drain_stage_{0};
    std::atomic<uint32_t> diagnostic_pending_submissions_{0};

    VideoCodec codec_;
    std::unique_ptr<detail::IAmfCodecSession> session_;
    PacketCallback packet_callback_;
    EncodedVideoConfig video_config_;
    std::deque<SubmittedTiming> submitted_timing_;
    std::string last_error_;

    uint32_t fps_ = 0;
    bool initialized_ = false;
    bool gpu_frame_prepared_ = false;
    bool first_frame_ = true;
    bool flushing_ = false;
    int64_t encode_start_qpc_ = 0;
    int64_t qpc_frequency_ = 0;
    int64_t last_pts_ = -1;
    int64_t last_forced_keyframe_pts_ = -1;
};

} // namespace fthr

#endif // FTHR_FFMPEG_AMF_REPLAY_ENCODER_H
