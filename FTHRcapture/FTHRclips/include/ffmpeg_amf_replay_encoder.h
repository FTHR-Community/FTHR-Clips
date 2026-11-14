// FFmpeg AMF replay encoder for a same-adapter AMD D3D11 capture path.

#pragma once
#ifndef FTHR_FFMPEG_AMF_REPLAY_ENCODER_H
#define FTHR_FFMPEG_AMF_REPLAY_ENCODER_H

#include "replay_encoder.h"
#include <d3d11.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <vector>

struct AVHWFramesContext;
struct AVBufferRef;
struct AVFrame;

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

// Internal, directly testable caller-pool contract. No AMF encoder is needed
// to exercise these real D3D11/FFmpeg resource lifetimes. Accepts production
// BGRA and the explicit even-sized NV12 diagnostic candidate, each bounded to
// eight separate textures with slice zero.
struct AmfInputPoolDiagnostics {
    std::atomic<uint32_t> live_textures{0};
    std::atomic<bool> capacity_exhausted{false};
    std::atomic<bool> hresult_available{false};
    std::atomic<int32_t> hresult{0};
    std::atomic<bool> state_destroyed{false};
};
using AmfCreatePoolTexture = HRESULT (*)(ID3D11Device*,
    const D3D11_TEXTURE2D_DESC*, ID3D11Texture2D**);
int InstallAmfSingleTexturePool(AVHWFramesContext* frames, ID3D11Device* device,
    std::shared_ptr<AmfInputPoolDiagnostics> diagnostics = {},
    AmfCreatePoolTexture create_texture = nullptr) noexcept;
// Unrefs the destination before acquiring; failure leaves no pending frame.
int AcquireAmfInputFrame(AVBufferRef* frames, AVFrame* frame) noexcept;
const AmfInputPoolDiagnostics* GetAmfInputPoolDiagnostics(
    const AVHWFramesContext* frames) noexcept;

// Internal diagnostics only; does not change the exported replay/shared ABI.
struct AmfTextureEvidence {
    bool available = false;
    uint32_t width = 0, height = 0, format = 0, array_size = 0, mip_levels = 0;
    uint32_t bind_flags = 0, misc_flags = 0, usage = 0, cpu_access_flags = 0;
    uint32_t sample_count = 0, sample_quality = 0, subresource = 0;
};

struct AmfFailureEvidence {
    std::string operation, codec;
    bool ffmpeg_error_available = false;
    int ffmpeg_error = 0;
    bool hresult_available = false;
    int32_t hresult = 0;
    bool device_removed_available = false;
    int32_t device_removed_reason = 0;
    uint64_t generation = 0, successful_submits = 0, packets = 0;
    int64_t pts = -1, last_submit_pts = -1, last_output_pts = -1;
    bool source_device_matches = false, input_device_matches = false;
    bool hw_frames_ctx_matches = false;
    bool source_identity_available = false, input_identity_available = false;
    bool hw_frames_identity_available = false;
    AmfTextureEvidence source, input;
};

std::string BuildAmfFailureEvidence(const AmfFailureEvidence& evidence);

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
    virtual bool UsesDirectBgraCopy() const noexcept { return false; }
    virtual AmfSubmitStatus SubmitFrame(
        int64_t pts, bool force_keyframe, std::string& error) = 0;
    virtual AmfReceiveStatus ReceivePacket(
        AmfPacketView& packet, std::string& error) = 0;
    virtual AmfSubmitStatus SendEndOfStream(std::string& error) = 0;
    virtual void Reset() noexcept = 0;
};

std::unique_ptr<IAmfCodecSession> CreateProductionAmfCodecSession();
// Explicit diagnostic candidate only. Production continues to select BGRA.
std::unique_ptr<IAmfCodecSession> CreateNv12AmfComparisonSession();

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
    bool UsesDirectBgraCopy() const noexcept;
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
