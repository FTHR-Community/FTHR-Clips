// NVENC encoder delivering packets through PacketCallback to the replay ring.
// Normally accepts a same-adapter GPU texture via EncodeFrame; legacy CPU
// input uses EncodeFrameCPU after readback. Initialize once per engine
// generation, submit from CaptureThread, and Finalize at shutdown.

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
#include <memory>

#include "replay_encoder.h"
#include "nvenc_input_lifecycle.h"
#include "capture_scale_geometry.h"

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;
struct ID3D11VideoDevice;
struct ID3D11VideoContext;
struct ID3D11VideoProcessorEnumerator;
struct ID3D11VideoProcessor;
struct ID3D11VideoProcessorInputView;
struct ID3D11VideoProcessorOutputView;
struct SwsContext;


namespace fthr {


    // NVENC Detection Result
    struct NVENCDetectionResult {
        bool        available;
        bool        h264_supported;
        bool        hevc_supported;
        bool        av1_supported;
        uint32_t    max_encode_width;
        uint32_t    max_encode_height;
        uint32_t    max_encode_sessions;
        uint32_t    driver_version;
        std::string gpu_name;
        std::string error_message;
    };

    NVENCDetectionResult DetectNVENC();


    // HardwareEncoder - native NVENC H.264/HEVC/AV1 encoder (encode-only)
    class HardwareEncoder final : public IReplayEncoder {
    public:
        // Called by DrainThread for each encoded packet. Consume or copy the
        // bytes before returning; the encoder reuses its output storage.
        using PacketCallback = IReplayEncoder::PacketCallback;

        explicit HardwareEncoder(VideoCodec codec);
        ~HardwareEncoder();

        // Open NVENC and allocate input buffers. The caller owns shared_device and
        // shared_context and must keep them alive through Finalize(). cpu_input_mode
        // selects system-memory input; otherwise frames use registered GPU textures.
        bool Initialize(const EncoderConfig&  config,
                        ID3D11Device*         shared_device,
                        ID3D11DeviceContext*  shared_context,
                        PacketCallback        callback,
                        bool                  cpu_input_mode = false) override;

        // GPU zero-copy path: encode the frame already CopyResource'd into GetCurrentInputTexture().
        // dxgi_present_qpc: LastPresentTime from DXGI_OUTDUPL_FRAME_INFO (raw QPC counts).
        //   Pass 0 to fall back to internal QPC reading.
        bool EncodeFrame(int64_t dxgi_present_qpc = 0) override;

        // Optimus / CPU-input path: encode from CPU memory mapped off a staging texture.
        // bgra_data:        pointer to pixel data from D3D11 Map().
        // src_stride:       RowPitch from D3D11_MAPPED_SUBRESOURCE (may exceed width*4).
        // dxgi_present_qpc: same semantics as EncodeFrame().
        bool EncodeFrameCPU(const uint8_t* bgra_data, uint32_t src_stride,
                            int64_t dxgi_present_qpc = 0) override;

        // Returns the D3D11 texture for the current encode slot (GPU path only).
        // CaptureEngine calls CopyResource(GetCurrentInputTexture(), dxgi_frame)
        // before calling EncodeFrame().
        ID3D11Texture2D* GetCurrentInputTexture() const noexcept override;
        bool RequiresBackendGpuPreparation() const noexcept override;
        bool PrepareGpuFrame(
            ID3D11Texture2D* source,
            uint32_t source_subresource = 0) override;

        // Flush encoder (EOS), drain remaining output, free all NVENC resources.
        void Finalize();
        void Shutdown() override { Finalize(); }

        EncodedVideoConfig GetVideoConfig() const override;
        ActiveEncoderInfo GetActiveEncoderInfo() const override;
        std::string GetLastError() const override { return last_error_; }
        ReplayEncoderDiagnostics GetDiagnostics() const noexcept override;
        CaptureStagePercentiles TakeDrainTimings() noexcept override {
            return drain_timings_.Take();
        }
        void SetCaptureGeneration(uint64_t generation) noexcept override {
            diagnostic_generation_.store(generation, std::memory_order_release);
        }

        // Return the QPC epoch used for PTS computation.
        // Returns false if the first frame has not been encoded yet.
        bool GetEncodeEpoch(
            int64_t& out_start_qpc, int64_t& out_qpc_freq) const override {
            if (first_frame_) { out_start_qpc = 0; out_qpc_freq = 0; return false; }
            out_start_qpc = encode_start_qpc_;
            out_qpc_freq = qpc_freq_;
            return true;
        }

        bool IsInitialized() const override { return initialized_; }


    private:
        // Lock output, normalize H.264 to AVCC, fire the codec packet, then unlock.
        // Runs on DrainThread — never called from CaptureThread in the async path.
        bool RetrieveOutput(uint32_t buf_idx);

        // Compute wall-clock PTS from a DXGI present QPC (or current QPC if 0).
        int64_t ComputePts(int64_t dxgi_present_qpc);

        // Drain thread: pops submitted slot indices in FIFO order, waits for the
        // completion event, then polls nvEncLockBitstream with a bounded timeout.
        // Keeps GPU encode completion work off the CaptureThread.
        void DrainThread();

        // Block until the submit ring has a free slot (pending_count_ < buffer_count_).
        bool WaitForFreeSlot();

        // Prepare the next GPU slot on the submit thread. This owns the matching
        // unmap operation so map/unmap calls cannot race across threads.
        bool PrepareCurrentGpuInputSlot();

        bool InitializeGpuScalePipeline(std::string& error);
        void ReleaseGpuScalePipeline() noexcept;
        bool ScaleIntoCurrentGpuInput(ID3D11VideoProcessorInputView* source_view);

        // Async drain state
        std::thread*            drain_thread_;
        std::atomic<bool>       drain_stop_;
        std::atomic<bool>       drain_failed_;
        std::mutex              drain_mutex_;
        std::condition_variable drain_cv_;     // signalled when an idx is enqueued
        std::condition_variable slot_cv_;      // signalled when drain frees a slot
        std::deque<uint32_t>    drain_queue_;

        // NVENC state
        void* nvenc_encoder_;  // NV_ENCODE_API_FUNCTION_LIST*
        void* nvenc_session_;  // NVENC encoder session handle
        void* nvenc_dll_;      // HMODULE

        // D3D11 state (NON-OWNING - shared from CaptureEngine, do NOT Release)
        ID3D11Device*        d3d11_device_;
        ID3D11DeviceContext* d3d11_context_;

        // Scale into a target-sized NVENC slot on the encoder's GPU. Hybrid
        // capture uploads its source-sized BGRA frame before using this pipeline.
        ID3D11VideoDevice*              video_device_;
        ID3D11VideoContext*             video_context_;
        ID3D11VideoProcessorEnumerator* video_processor_enumerator_;
        ID3D11VideoProcessor*           video_processor_;
        ID3D11Texture2D*                gpu_scale_texture_;
        ID3D11VideoProcessorOutputView* gpu_scale_output_view_;
        ID3D11Texture2D*                cpu_upload_texture_;
        ID3D11VideoProcessorInputView*  cpu_upload_view_;
        CaptureScaleGeometry            gpu_scale_geometry_;
        uint32_t                        input_width_;
        uint32_t                        input_height_;
        bool                            gpu_scale_required_;
        bool                            gpu_frame_prepared_;

        // Input mode
        bool hdr10_ = false;
        bool cpu_input_mode_;  // true when using CPU-side NVENC input buffers

        // D3D11 texture pool registered with NVENC
        ID3D11Texture2D** input_textures_;        // GPU-only D3D11_USAGE_DEFAULT
        void**            registered_resources_;  // NV_ENC_REGISTERED_PTR handles
        void**            mapped_input_resources_; // Kept mapped until output lock completes
        NvencInputSlotLifecycle* input_slot_lifecycle_;

        // CPU-input path: NVENC system-memory input buffers
        void** cpu_input_buffers_;  // NV_ENC_INPUT_PTR handles
        SwsContext* cpu_scale_context_ = nullptr;

        // Shared I/O pool state
        void**   output_buffers_;    // NV_ENC bitstream output buffer handles
        void**   completion_events_; // Registered Windows events, one per output
        int64_t* slot_qpc_;         // Per-slot raw QPC ticks, indexed same as output_buffers_
        std::unique_ptr<std::atomic<uint64_t>[]> diagnostic_slot_frame_;
        std::unique_ptr<std::atomic<int64_t>[]> diagnostic_slot_pts_;
        std::unique_ptr<std::atomic<int64_t>[]> diagnostic_slot_qpc_;
        std::unique_ptr<std::atomic<uint64_t>[]> diagnostic_slot_generation_;
        uint32_t buffer_count_;
        uint32_t current_buf_idx_;
        uint32_t pending_count_;

        // Encoder configuration
        uint32_t src_width_;
        uint32_t src_height_;
        uint32_t enc_width_;
        uint32_t enc_height_;
        uint32_t fps_;
        uint32_t bitrate_kbps_;
        VideoCodec codec_;
        bool     initialized_;
        std::string last_error_;
        int64_t  pts_;
        int64_t  last_forced_idr_pts_;

        // Wall-clock PTS (QPC-based)
        bool    first_frame_;
        int64_t encode_start_qpc_;
        int64_t qpc_freq_;
        int64_t last_frame_qpc_;  // Raw QPC ticks of the most recently computed PTS frame

        // Reuse packet conversion buffers on the drain thread to avoid allocations
        // once capacity has grown to fit the encoded packets.
        PacketCallback       packet_callback_;
        std::vector<uint8_t> avcc_buf_;
        std::vector<std::pair<const uint8_t*, int>> nals_scratch_;

        // H.264 avcC, HEVC Annex B VPS/SPS/PPS, or AV1 sequence header OBUs.
        std::vector<uint8_t> extradata_;

        // Per-instance packet log counter — logs first 3 encoded packets so the
        // diagnostic output is visible on every Initialize() instead of only once
        // per process lifetime (static locals never reset after Finalize).
        int callback_log_count_;

        // Forward-progress accounting. Stage values are intentionally numeric
        // and stable so a watchdog can report a compact snapshot without locks.
        // Submit: 0 idle, 1 waiting-slot, 2 mapping, 3 encode-picture, 4 queued.
        // Drain: 0 idle, 5 dequeue, 6 completion wait, 9 lock-bitstream,
        // 7 callback, 8 unlock/unmap.
        std::atomic<uint64_t> diag_input_slots_acquired_{0};
        std::atomic<uint64_t> diag_map_attempts_{0};
        std::atomic<uint64_t> diag_maps_succeeded_{0};
        std::atomic<uint64_t> diag_encode_attempts_{0};
        std::atomic<uint64_t> diag_encode_returns_{0};
        std::atomic<uint64_t> diag_encode_successes_{0};
        std::atomic<uint64_t> diag_drain_dequeues_{0};
        std::atomic<uint64_t> diag_completion_events_{0};
        std::atomic<uint64_t> diag_bitstream_lock_attempts_{0};
        std::atomic<uint64_t> diag_bitstream_locks_{0};
        std::atomic<uint64_t> diag_bitstream_unlocks_{0};
        std::atomic<uint64_t> diag_resources_unmapped_{0};
        std::atomic<uint64_t> diag_packets_produced_{0};
        std::atomic<uint64_t> diag_slots_recycled_{0};
        std::atomic<uint32_t> diag_mapped_resources_{0};
        std::atomic<uint32_t> diag_locked_bitstreams_{0};
        std::atomic<uint32_t> diag_pending_resources_{0};
        std::atomic<uint32_t> diag_queued_outputs_{0};
        std::atomic<uint32_t> diag_submit_stage_{0};
        std::atomic<uint32_t> diag_drain_stage_{0};
        CaptureStageTiming drain_timings_;
        std::atomic<uint32_t> diag_submit_slot_{0xffffffffu};
        std::atomic<uint32_t> diag_drain_slot_{0xffffffffu};
        std::atomic<int32_t> diag_last_nvenc_status_{0};
        std::atomic<uint64_t> diagnostic_generation_{0};
    };

} // namespace fthr


#endif // FTHR_HARDWARE_ENCODER_H
