// hardware_encoder.h
// FTHR Capture Engine - NVENC Hardware Encoder
//
// HardwareEncoder is a pure encode-only component.
// Encoded NAL units are delivered via PacketCallback to CaptureEngine,
// which pushes them into EncodedRingBuffer.
//
// Two input paths:
//   GPU zero-copy (default): CaptureEngine CopyResource's into GetCurrentInputTexture()
//     then calls EncodeFrame(). No CPU involvement, no staging texture.
//   CPU-input / Optimus: CaptureEngine maps a staging texture and calls EncodeFrameCPU()
//     with the CPU pointer. NVENC still encodes in hardware; only the copy is on the CPU.
//
// Lifecycle:
//   Initialize()      - start of engine lifetime
//   EncodeFrame() or EncodeFrameCPU() - called every frame from CaptureThread
//   Finalize()        - called at engine shutdown

#pragma once
#ifndef FTHR_HARDWARE_ENCODER_H
#define FTHR_HARDWARE_ENCODER_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <cstdint>
#include <string>
#include <vector>
#include <functional>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <deque>

#include "video_encoder.h"

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;


namespace fthr {


    // ---------------------------------------------------------------------------
    // NVENC Detection Result
    // ---------------------------------------------------------------------------
    struct NVENCDetectionResult {
        bool        available;
        bool        h264_supported;
        bool        hevc_supported;
        uint32_t    max_encode_width;
        uint32_t    max_encode_height;
        uint32_t    max_encode_sessions;
        uint32_t    driver_version;
        std::string gpu_name;
        std::string error_message;
    };

    NVENCDetectionResult DetectNVENC();


    // ---------------------------------------------------------------------------
    // HardwareEncoder - NVENC H.264 Encoder (encode-only)
    // ---------------------------------------------------------------------------
    class HardwareEncoder {
    public:
        // Callback: fired once per encoded frame from within EncodeFrame / EncodeFrameCPU.
        // Runs on CaptureThread. Must be fast — no blocking, no allocation.
        using PacketCallback = std::function<void(const uint8_t* avcc_data,
            uint32_t       size,
            int64_t        pts,
            bool           is_keyframe,
            int64_t        wall_qpc)>;

        HardwareEncoder();
        ~HardwareEncoder();

        // Initialize NVENC session and allocate input buffer pool.
        // shared_device:   D3D11 device for NVENC session. NON-OWNING — caller keeps alive
        //                  until Finalize() returns.
        // shared_context:  Immediate context for the shared device.
        // callback:        receives every encoded packet.
        // cpu_input_mode:  false (default) = GPU zero-copy path (NVIDIA adapter drives display).
        //                  true            = Optimus path (Intel drives display; NVIDIA device
        //                                    passed in for NVENC only; frames arrive via CPU memcpy).
        bool Initialize(const EncoderConfig&  config,
                        ID3D11Device*         shared_device,
                        ID3D11DeviceContext*  shared_context,
                        PacketCallback        callback,
                        bool                  cpu_input_mode = false);

        // GPU zero-copy path: encode the frame already CopyResource'd into GetCurrentInputTexture().
        // dxgi_present_qpc: LastPresentTime from DXGI_OUTDUPL_FRAME_INFO (raw QPC counts).
        //   Pass 0 to fall back to internal QPC reading.
        bool EncodeFrame(int64_t dxgi_present_qpc = 0);

        // Optimus / CPU-input path: encode from CPU memory mapped off a staging texture.
        // bgra_data:        pointer to pixel data from D3D11 Map().
        // src_stride:       RowPitch from D3D11_MAPPED_SUBRESOURCE (may exceed width*4).
        // dxgi_present_qpc: same semantics as EncodeFrame().
        bool EncodeFrameCPU(const uint8_t* bgra_data, uint32_t src_stride,
                            int64_t dxgi_present_qpc = 0);

        // Returns the D3D11 texture for the current encode slot (GPU path only).
        // CaptureEngine calls CopyResource(GetCurrentInputTexture(), dxgi_frame)
        // before calling EncodeFrame().
        ID3D11Texture2D* GetCurrentInputTexture() const noexcept;

        // Flush encoder (EOS), drain remaining output, free all NVENC resources.
        void Finalize();

        // Return the AVCC decoder configuration record (SPS + PPS) extracted during
        // Initialize(). Call this after Initialize() succeeds to populate
        // EncodedRingBuffer::SetExtradata().
        std::vector<uint8_t> GetExtradata() const;

        // Return the QPC epoch used for PTS computation.
        // Returns false if the first frame has not been encoded yet.
        bool GetEncodeEpoch(int64_t& out_start_qpc, int64_t& out_qpc_freq) const {
            if (first_frame_) { out_start_qpc = 0; out_qpc_freq = 0; return false; }
            out_start_qpc = encode_start_qpc_;
            out_qpc_freq = qpc_freq_;
            return true;
        }

        bool IsInitialized() const { return initialized_; }


    private:
        // Lock output bitstream buffer, convert Annex B -> AVCC, fire callback, unlock.
        // Runs on DrainThread — never called from CaptureThread in the async path.
        bool RetrieveOutput(uint32_t buf_idx);

        // Compute wall-clock PTS from a DXGI present QPC (or current QPC if 0).
        int64_t ComputePts(int64_t dxgi_present_qpc);

        // Drain thread: pops submitted slot indices in FIFO order, blocks in
        // nvEncLockBitstream until each output is ready, fires packet_callback_.
        // Keeps nvEncLockBitstream off the CaptureThread so video capture never
        // waits on GPU encode completion.
        void DrainThread();

        // Block until the submit ring has a free slot (pending_count_ < buffer_count_).
        void WaitForFreeSlot();

        // -----------------------------------------------------------------------
        // Async drain state
        // -----------------------------------------------------------------------
        std::thread*            drain_thread_;
        std::atomic<bool>       drain_stop_;
        std::mutex              drain_mutex_;
        std::condition_variable drain_cv_;     // signalled when an idx is enqueued
        std::condition_variable slot_cv_;      // signalled when drain frees a slot
        std::deque<uint32_t>    drain_queue_;

        // -----------------------------------------------------------------------
        // NVENC state
        // -----------------------------------------------------------------------
        void* nvenc_encoder_;  // NV_ENCODE_API_FUNCTION_LIST*
        void* nvenc_session_;  // NVENC encoder session handle
        void* nvenc_dll_;      // HMODULE

        // -----------------------------------------------------------------------
        // D3D11 state (NON-OWNING - shared from CaptureEngine, do NOT Release)
        // -----------------------------------------------------------------------
        ID3D11Device*        d3d11_device_;
        ID3D11DeviceContext* d3d11_context_;

        // -----------------------------------------------------------------------
        // Input mode
        // -----------------------------------------------------------------------
        bool cpu_input_mode_;  // true = Optimus path (CPU-side NVENC input buffers)

        // -----------------------------------------------------------------------
        // GPU zero-copy path: D3D11 texture pool registered with NVENC
        // -----------------------------------------------------------------------
        ID3D11Texture2D** input_textures_;        // GPU-only D3D11_USAGE_DEFAULT
        void**            registered_resources_;  // NV_ENC_REGISTERED_PTR handles

        // -----------------------------------------------------------------------
        // CPU-input path: NVENC system-memory input buffers
        // -----------------------------------------------------------------------
        void** cpu_input_buffers_;  // NV_ENC_INPUT_PTR handles

        // -----------------------------------------------------------------------
        // Shared I/O pool state
        // -----------------------------------------------------------------------
        void**   output_buffers_;    // NV_ENC bitstream output buffer handles
        int64_t* slot_qpc_;         // Per-slot raw QPC ticks, indexed same as output_buffers_
        uint32_t buffer_count_;
        uint32_t current_buf_idx_;
        uint32_t pending_count_;

        // -----------------------------------------------------------------------
        // Encoder configuration
        // -----------------------------------------------------------------------
        uint32_t src_width_;
        uint32_t src_height_;
        uint32_t enc_width_;
        uint32_t enc_height_;
        uint32_t fps_;
        uint32_t bitrate_kbps_;
        bool     initialized_;
        int64_t  pts_;

        // -----------------------------------------------------------------------
        // Wall-clock PTS (QPC-based)
        // -----------------------------------------------------------------------
        bool    first_frame_;
        int64_t encode_start_qpc_;
        int64_t qpc_freq_;
        int64_t last_frame_qpc_;  // Raw QPC ticks of the most recently computed PTS frame

        // -----------------------------------------------------------------------
        // Packet callback + conversion scratch buffers
        //
        // Both buffers are reused across encoded packets to avoid heap allocation
        // on the drain thread (~60 calls/sec). Capacity stabilises after the
        // first few keyframes; the resize/clear operations after that are O(1).
        // -----------------------------------------------------------------------
        PacketCallback       packet_callback_;
        std::vector<uint8_t> avcc_buf_;
        std::vector<std::pair<const uint8_t*, int>> nals_scratch_;

        // -----------------------------------------------------------------------
        // SPS/PPS extradata (AVCC decoder config record)
        // -----------------------------------------------------------------------
        std::vector<uint8_t> extradata_;
    };


} // namespace fthr


#endif // FTHR_HARDWARE_ENCODER_H
