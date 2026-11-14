// FFmpeg QSV replay encoder for a same-adapter Intel D3D11 capture path.

#pragma once
#ifndef FTHR_FFMPEG_QSV_REPLAY_ENCODER_H
#define FTHR_FFMPEG_QSV_REPLAY_ENCODER_H

#include "replay_encoder.h"

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <vector>

namespace fthr {

struct QsvCodecSelection {
    VideoCodec codec;
    const char* encoder_name;
    EncodedPacketFormat packet_format;
    int32_t profile;
};

const QsvCodecSelection* GetQsvCodecSelection(VideoCodec codec) noexcept;

EncodedVideoConfig BuildQsvVideoConfig(
    VideoCodec codec,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    uint32_t bitrate_kbps,
    const std::vector<uint8_t>& codec_extradata);

namespace detail {

// oneVPL shares the immediate context with capture and requires D3D11's
// multithread lock before the device handle is supplied to the runtime.
bool EnableQsvMultithreadProtection(
    ID3D11DeviceContext* context, std::string& error);

enum class QsvSubmitStatus : uint32_t {
    Accepted,
    NeedDrain,
    Error,
};

enum class QsvReceiveStatus : uint32_t {
    Packet,
    NeedMoreInput,
    EndOfStream,
    Error,
};

struct QsvPacketView {
    const uint8_t* data = nullptr;
    uint32_t size = 0;
    int64_t pts = 0;
    int64_t dts = 0;
    bool keyframe = false;
    const uint8_t* codec_extradata = nullptr;
    uint32_t codec_extradata_size = 0;
};

// Narrow FFmpeg/QSV seam. Production owns all FFmpeg, oneVPL and D3D11
// resources; deterministic tests exercise policy, lifetime, conversion,
// packet/config and failure behavior without pretending to run Intel hardware.
class IQsvCodecSession {
public:
    virtual ~IQsvCodecSession() = default;

    virtual bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        const QsvCodecSelection& selection,
        EncodedVideoConfig& video_config,
        std::string& error) = 0;
    virtual bool PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource,
        std::string& error) = 0;
    virtual ID3D11Texture2D* GetCurrentInputTexture() const noexcept = 0;
    virtual uint32_t GetCurrentInputSubresource() const noexcept = 0;
    virtual QsvSubmitStatus SubmitFrame(
        int64_t pts, bool force_keyframe, std::string& error) = 0;
    virtual QsvReceiveStatus ReceivePacket(
        QsvPacketView& packet, std::string& error) = 0;
    virtual QsvSubmitStatus SendEndOfStream(std::string& error) = 0;
    virtual void Reset() noexcept = 0;
};

std::unique_ptr<IQsvCodecSession> CreateProductionQsvCodecSession();

} // namespace detail

class FfmpegQsvReplayEncoder final : public IReplayEncoder {
public:
    explicit FfmpegQsvReplayEncoder(VideoCodec codec);
    FfmpegQsvReplayEncoder(
        VideoCodec codec,
        std::unique_ptr<detail::IQsvCodecSession> session);
    ~FfmpegQsvReplayEncoder() override;

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
    bool IsVideoConfigReady() const override;
    ActiveEncoderInfo GetActiveEncoderInfo() const override;
    std::string GetLastError() const override;
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
    bool UpdateDeferredVideoConfig(const detail::QsvPacketView& packet);
    void SetError(std::string error);

    VideoCodec codec_;
    std::unique_ptr<detail::IQsvCodecSession> session_;
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

#endif // FTHR_FFMPEG_QSV_REPLAY_ENCODER_H
