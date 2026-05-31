// audio_capture.h
// FTHR Capture Engine - WASAPI Loopback Audio Capture
//
// Captures all desktop audio output (everything the user hears) using
// Windows Audio Session API (WASAPI) in loopback mode. No driver install
// required - loopback is a built-in Windows feature since Vista.
//
// How loopback capture works:
//   WASAPI lets you open a render device (speakers/headphones) in loopback
//   mode. Instead of recording a microphone, you record what the OS mixer
//   is sending to the output device - i.e. all application audio mixed.
//   This captures game audio, Discord, music, system sounds, everything.
//
// Format:
//   The device mix format is queried at Initialize() time. Modern Windows
//   systems virtually always use float32, 48kHz, stereo. If a different
//   format is detected the engine logs a warning but proceeds - AudioEncoder
//   handles the sample rate and channel count dynamically.
//
// Silence injection:
//   WASAPI loopback only delivers packets when audio is actively playing.
//   When nothing is playing (or the buffer comes back with SILENT flag),
//   AudioCapture synthesizes zero-filled PCM frames for the elapsed period.
//   This keeps the encoder PTS advancing continuously and prevents gaps in
//   the audio ring buffer, which would cause audio/video desync in saved clips.
//
// Threading model:
//   CaptureThread() runs on its own dedicated thread started by Start().
//   It uses event-driven mode: the OS fires buffer_event_ every device
//   period (~10ms), waking the thread to drain available packets.
//   EncodeSamples() is called from this thread only.
//
// What does NOT live here:
//   - AAC encoding       -> audio_encoder.h
//   - Packet ring buffer -> audio_ring_buffer.h
//   - Per-app mute/volume control -> AudioSessionManager (Phase 2)

#pragma once
#ifndef FTHR_AUDIO_CAPTURE_H
#define FTHR_AUDIO_CAPTURE_H

#include <cstdint>
#include <thread>
#include <atomic>
#include <string>
#include <vector>


namespace fthr {

    // Forward declaration
    class AudioRingBuffer;


    // ---------------------------------------------------------------------------
    // AudioCaptureConfig
    // ---------------------------------------------------------------------------
    struct AudioCaptureConfig {
        // Target sample rate and channel count.
        // If the device mix format differs, AudioCapture logs a warning.
        // AudioEncoder is initialized with whatever the device actually provides.
        uint32_t preferred_sample_rate = 48000;
        uint32_t preferred_channels = 2;

        // AAC encode bitrate passed through to AudioEncoder
        uint32_t bitrate_kbps = 128;

        // Device ID string for future per-device selection (Phase 2).
        // Leave empty to use the system default render device.
        std::wstring device_id;
    };


    // ---------------------------------------------------------------------------
    // AudioCapture
    // ---------------------------------------------------------------------------
    class AudioCapture {
    public:
        AudioCapture();
        ~AudioCapture();

        // Initialize WASAPI loopback capture.
        // ring:   AudioRingBuffer to push raw PCM into. Must remain valid until Stop().
        //         AudioCapture writes float32 PCM directly - no encoding on this thread.
        // config: device selection and format preferences.
        bool Initialize(AudioRingBuffer* ring, const AudioCaptureConfig& config);

        // Start the capture thread. Call after Initialize() succeeds.
        // Returns true if the thread started successfully.
        bool Start();

        // Signal the capture thread to stop and block until it exits.
        // Safe to call even if Start() was never called.
        void Stop();

        // Release all WASAPI and COM resources.
        // Must be called after Stop().
        void Shutdown();

        // Actual device format discovered at Initialize() time.
        uint32_t GetSampleRate() const { return sample_rate_; }
        uint32_t GetChannels()   const { return channels_; }
        bool     IsRunning()     const { return running_.load(std::memory_order_relaxed); }

        // True if the WASAPI device was lost at runtime (device change, driver reset,
        // exclusive-mode app, etc.). Audio ring is stale once this is set.
        // Cleared by Shutdown() so a fresh Initialize()+Start() starts clean.
        bool     IsDeviceLost()  const { return device_lost_.load(std::memory_order_relaxed); }


    private:
        // -----------------------------------------------------------------------
        // Thread entry point
        // -----------------------------------------------------------------------
        void CaptureThread();

        // Write 'num_frames' frames of silence to the encoder.
        // Used when WASAPI returns a silent buffer or no data is available.
        void InjectSilence(uint32_t num_frames, uint64_t qpc_100ns = 0);

        // Create WASAPI COM objects (enumerator, device, audio_client, capture_client,
        // buffer_event). Sets sample_rate_, channels_, silence_buf_.
        // Assumes COM is initialized on the calling thread.
        // On failure, partial state is left — call TeardownWASAPISession() to clean up.
        bool SetupWASAPISession();

        // Release WASAPI COM objects. Safe to call with partial state (checks each
        // pointer). Does NOT touch ring_, silence_buf_, or the COM apartment.
        void TeardownWASAPISession();

        // -----------------------------------------------------------------------
        // WASAPI COM interfaces stored as void* to keep windows.h / mmdeviceapi.h
        // out of this header. Cast back to their real types inside the .cpp.
        //
        //   enumerator_    IMMDeviceEnumerator*
        //   device_        IMMDevice*
        //   audio_client_  IAudioClient*
        //   capture_client_ IAudioCaptureClient*
        //   mix_format_    WAVEFORMATEX*       (heap-allocated by WASAPI, freed with CoTaskMemFree)
        //   buffer_event_  HANDLE              (auto-reset event, signals when buffer is ready)
        // -----------------------------------------------------------------------
        void* enumerator_;
        void* device_;
        void* audio_client_;
        void* capture_client_;
        void* mix_format_;
        void* buffer_event_;

        // -----------------------------------------------------------------------
        // Ring buffer reference (not owned) - receives raw PCM float32
        // -----------------------------------------------------------------------
        AudioRingBuffer* ring_;

        // -----------------------------------------------------------------------
        // Actual device format (read from mix_format_ during Initialize)
        // -----------------------------------------------------------------------
        uint32_t sample_rate_;
        uint32_t channels_;

        // -----------------------------------------------------------------------
        // Thread state
        // -----------------------------------------------------------------------
        std::thread       capture_thread_;
        std::atomic<bool> running_{ false };
        std::atomic<bool> device_lost_{ false };

        // Stored from the last Initialize() call so recovery can re-open the same
        // device (or fall back to default if empty).
        std::wstring device_id_;

        // -----------------------------------------------------------------------
        // Silence injection scratch buffer
        // Pre-allocated in Initialize() to avoid heap alloc in the hot path.
        // Size: preferred_channels * max_expected_frames_per_callback floats.
        // -----------------------------------------------------------------------
        std::vector<float> silence_buf_;
    };


} // namespace fthr

#endif // FTHR_AUDIO_CAPTURE_H