#pragma once
#include "replay_encoder.h"
#include "capture_scale_geometry.h"
#include <deque>

struct AVCodecContext;
struct AVFrame;
struct AVPacket;
struct SwsContext;

namespace fthr {
// CPU encoding into the existing compressed replay ring, never a raw-frame ring.
class SoftwareReplayEncoder final : public IReplayEncoder {
public:
    ~SoftwareReplayEncoder() override { Shutdown(); }
    bool Initialize(const EncoderConfig&, ID3D11Device*, ID3D11DeviceContext*,
                    PacketCallback, bool) override;
    bool EncodeFrame(int64_t = 0) override { return false; }
    bool EncodeFrameCPU(const uint8_t*, uint32_t, int64_t = 0) override;
    ID3D11Texture2D* GetCurrentInputTexture() const noexcept override { return nullptr; }
    void Shutdown() override;
    EncodedVideoConfig GetVideoConfig() const override { return video_; }
    bool IsVideoConfigReady() const override { return !video_.codec_extradata.empty(); }
    ActiveEncoderInfo GetActiveEncoderInfo() const override {
        return {EncoderVendor::Software, ReplayEncoderBackend::Software,
                VideoCodec::H264, false, "libopenh264"};
    }
    std::string GetLastError() const override { return error_; }
    bool GetEncodeEpoch(int64_t& start, int64_t& frequency) const override;
    bool IsInitialized() const override { return initialized_; }
private:
    bool Drain();
    bool Fail(const char*, int);
    AVCodecContext* codec_ = nullptr;
    AVFrame* frame_ = nullptr;
    AVPacket* packet_ = nullptr;
    SwsContext* scaler_ = nullptr;
    CaptureScaleGeometry geometry_;
    EncodedVideoConfig video_;
    PacketCallback callback_;
    std::deque<std::pair<int64_t, int64_t>> timing_;
    int64_t next_pts_ = 0;
    std::atomic<int64_t> epoch_{0}, frequency_{0};
    bool initialized_ = false;
    std::string error_;
};
}
