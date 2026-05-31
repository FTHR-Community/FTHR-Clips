// audio_capture.cpp
// FTHR Capture Engine - WASAPI Loopback Audio Capture implementation

#ifdef _MSC_VER
#if __has_include("pch.h")
#include "pch.h"
#elif __has_include("stdafx.h")
#include "stdafx.h"
#endif
#endif

#ifndef NOMINMAX
#define NOMINMAX
#endif

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <functiondiscoverykeys_devpkey.h>

// Auto-link required Windows libraries.
// CoInitializeEx, CoCreateInstance, CoTaskMemFree -> ole32.lib
// PropVariantClear, PKEY_Device_FriendlyName     -> propsys.lib
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "propsys.lib")

#include "audio_capture.h"
#include "audio_ring_buffer.h"

#include <iostream>
#include <vector>
#include <cstring>
#include <cassert>


// ---------------------------------------------------------------------------
// Helper macro - log HRESULT failures without throwing
// ---------------------------------------------------------------------------
#define FTHR_CHECK_HR(hr, msg)                                          \
    if (FAILED(hr)) {                                                   \
        std::cerr << "[AudioCapture] " << (msg)                         \
                  << " HRESULT=0x" << std::hex << (hr) << std::dec     \
                  << std::endl;                                         \
        return false;                                                   \
    }


namespace fthr {


    // ===========================================================================
    // Constructor / Destructor
    // ===========================================================================

    AudioCapture::AudioCapture()
        : enumerator_(nullptr)
        , device_(nullptr)
        , audio_client_(nullptr)
        , capture_client_(nullptr)
        , mix_format_(nullptr)
        , buffer_event_(nullptr)
        , ring_(nullptr)
        , sample_rate_(0)
        , channels_(0)
    {
    }

    AudioCapture::~AudioCapture() {
        Stop();
        Shutdown();
    }


    // ===========================================================================
    // Initialize
    //
    // Steps:
    //   1.  CoInitializeEx (apartment for this thread)
    //   2.  Create IMMDeviceEnumerator
    //   3.  Get default render device (or device_id if specified)
    //   4.  Activate IAudioClient
    //   5.  Query mix format (WAVEFORMATEX)
    //   6.  Validate / log format details
    //   7.  Initialize IAudioClient in loopback event-driven mode
    //   8.  Get IAudioCaptureClient
    //   9.  Create buffer-ready event + associate with audio client
    //   10. Initialize AudioEncoder with discovered format
    //   11. Pre-allocate silence buffer
    // ===========================================================================

    // ===========================================================================
    // TeardownWASAPISession
    //
    // Releases the WASAPI COM objects. Safe with partial state (each pointer is
    // null-checked before Release). Does NOT touch ring_, silence_buf_, or the
    // thread's COM apartment — only the session-level objects that can be
    // recreated on reconnect.
    // ===========================================================================

    void AudioCapture::TeardownWASAPISession() {
        if (audio_client_) {
            static_cast<IAudioClient*>(audio_client_)->Stop();
        }
        if (capture_client_) {
            static_cast<IAudioCaptureClient*>(capture_client_)->Release();
            capture_client_ = nullptr;
        }
        if (audio_client_) {
            static_cast<IAudioClient*>(audio_client_)->Release();
            audio_client_ = nullptr;
        }
        if (device_) {
            static_cast<IMMDevice*>(device_)->Release();
            device_ = nullptr;
        }
        if (enumerator_) {
            static_cast<IMMDeviceEnumerator*>(enumerator_)->Release();
            enumerator_ = nullptr;
        }
        if (mix_format_) {
            CoTaskMemFree(mix_format_);
            mix_format_ = nullptr;
        }
        if (buffer_event_) {
            CloseHandle(static_cast<HANDLE>(buffer_event_));
            buffer_event_ = nullptr;
        }
    }


    // ===========================================================================
    // SetupWASAPISession
    //
    // Creates the WASAPI COM objects needed to capture loopback audio. Extracted
    // from Initialize() so it can also be called from CaptureThread during
    // recovery (e.g. after BT headphones reconnect or device invalidation).
    //
    // COM must already be initialized on the calling thread.
    // Uses device_id_ (stored in Initialize) to re-open the same device.
    // On failure, COM objects may be partially created — caller must call
    // TeardownWASAPISession() to clean up.
    //
    // Also resizes silence_buf_ to match the new device format, so recovery
    // after a format change (e.g. 44100 -> 48000 headset) works correctly.
    // ===========================================================================

    bool AudioCapture::SetupWASAPISession() {
        HRESULT hr;

        // Step 2: Create device enumerator
        IMMDeviceEnumerator* enumerator = nullptr;
        hr = CoCreateInstance(
            __uuidof(MMDeviceEnumerator), nullptr,
            CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
            reinterpret_cast<void**>(&enumerator));
        FTHR_CHECK_HR(hr, "CoCreateInstance(MMDeviceEnumerator) failed");
        enumerator_ = static_cast<void*>(enumerator);

        // Step 3: Get the target render device (or default)
        IMMDevice* device = nullptr;
        if (!device_id_.empty()) {
            hr = enumerator->GetDevice(device_id_.c_str(), &device);
            if (FAILED(hr)) {
                std::cerr << "[AudioCapture] Stored device not found, "
                    << "falling back to default" << std::endl;
                hr = enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device);
            }
        }
        else {
            hr = enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device);
        }
        FTHR_CHECK_HR(hr, "GetDefaultAudioEndpoint failed");
        device_ = static_cast<void*>(device);

        IPropertyStore* props = nullptr;
        if (SUCCEEDED(device->OpenPropertyStore(STGM_READ, &props))) {
            PROPVARIANT name;
            PropVariantInit(&name);
            if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &name))
                && name.vt == VT_LPWSTR) {
                char buf[256] = {};
                WideCharToMultiByte(CP_UTF8, 0, name.pwszVal, -1,
                    buf, sizeof(buf) - 1, nullptr, nullptr);
                std::cout << "[AudioCapture] Capture device: " << buf << std::endl;
            }
            PropVariantClear(&name);
            props->Release();
        }

        // Step 4: Activate IAudioClient
        IAudioClient* audio_client = nullptr;
        hr = device->Activate(__uuidof(IAudioClient), CLSCTX_ALL,
            nullptr, reinterpret_cast<void**>(&audio_client));
        FTHR_CHECK_HR(hr, "IMMDevice::Activate(IAudioClient) failed");
        audio_client_ = static_cast<void*>(audio_client);

        // Step 5: Query mix format
        WAVEFORMATEX* mix_fmt = nullptr;
        hr = audio_client->GetMixFormat(&mix_fmt);
        FTHR_CHECK_HR(hr, "IAudioClient::GetMixFormat failed");
        mix_format_ = static_cast<void*>(mix_fmt);

        // Step 6: Validate and log format
        sample_rate_ = mix_fmt->nSamplesPerSec;
        channels_    = mix_fmt->nChannels;

        const bool is_float = (mix_fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) ||
            (mix_fmt->wFormatTag == WAVE_FORMAT_EXTENSIBLE &&
                reinterpret_cast<WAVEFORMATEXTENSIBLE*>(mix_fmt)->SubFormat ==
                KSDATAFORMAT_SUBTYPE_IEEE_FLOAT);

        std::cout << "[AudioCapture] Device mix format: "
            << sample_rate_ << "Hz / " << channels_ << "ch / "
            << mix_fmt->wBitsPerSample << "-bit "
            << (is_float ? "float" : "integer") << std::endl;

        if (!is_float) {
            std::cerr << "[AudioCapture] WARNING: device is not float32. "
                << "Audio may be distorted." << std::endl;
        }

        // Step 7: Initialize IAudioClient in loopback event-driven mode
        hr = audio_client->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            0, 0, mix_fmt, nullptr);
        FTHR_CHECK_HR(hr, "IAudioClient::Initialize failed");

        // Step 8: Get IAudioCaptureClient
        IAudioCaptureClient* capture_client = nullptr;
        hr = audio_client->GetService(__uuidof(IAudioCaptureClient),
            reinterpret_cast<void**>(&capture_client));
        FTHR_CHECK_HR(hr, "IAudioClient::GetService(IAudioCaptureClient) failed");
        capture_client_ = static_cast<void*>(capture_client);

        // Step 9: Create buffer-ready event
        HANDLE evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!evt) {
            std::cerr << "[AudioCapture] CreateEvent failed" << std::endl;
            return false;
        }
        buffer_event_ = static_cast<void*>(evt);

        hr = audio_client->SetEventHandle(evt);
        FTHR_CHECK_HR(hr, "IAudioClient::SetEventHandle failed");

        // Resize silence scratch buffer to match (possibly new) device format.
        const uint32_t max_silence_frames = (sample_rate_ * 200) / 1000;
        silence_buf_.assign(static_cast<size_t>(max_silence_frames) * channels_, 0.0f);

        return true;
    }


    // ===========================================================================
    // Initialize
    // ===========================================================================

    bool AudioCapture::Initialize(AudioRingBuffer* ring,
        const AudioCaptureConfig& config) {
        if (!ring) {
            std::cerr << "[AudioCapture] ring must not be null" << std::endl;
            return false;
        }
        ring_      = ring;
        device_id_ = config.device_id;

        std::cout << "[AudioCapture] Initializing WASAPI loopback..." << std::endl;

        // COM apartment for this thread (the calling/main thread).
        // RPC_E_CHANGED_MODE = already initialized differently, that's fine.
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
            FTHR_CHECK_HR(hr, "CoInitializeEx failed");
        }

        if (!SetupWASAPISession()) {
            TeardownWASAPISession();
            return false;
        }

        std::cout << "[AudioCapture] Ready ("
            << sample_rate_ << "Hz, " << channels_ << "ch)" << std::endl;
        return true;
    }


    // ===========================================================================
    // Start
    // ===========================================================================

    bool AudioCapture::Start() {
        if (!audio_client_ || !capture_client_) {
            std::cerr << "[AudioCapture] Start() called before Initialize()"
                << std::endl;
            return false;
        }

        if (running_.load()) {
            std::cerr << "[AudioCapture] Already running" << std::endl;
            return false;
        }

        IAudioClient* client = static_cast<IAudioClient*>(audio_client_);
        HRESULT hr = client->Start();
        if (FAILED(hr)) {
            std::cerr << "[AudioCapture] IAudioClient::Start failed: 0x"
                << std::hex << hr << std::dec << std::endl;
            return false;
        }

        running_.store(true, std::memory_order_release);
        capture_thread_ = std::thread(&AudioCapture::CaptureThread, this);

        std::cout << "[AudioCapture] Capture started." << std::endl;
        return true;
    }


    // ===========================================================================
    // Stop
    // ===========================================================================

    void AudioCapture::Stop() {
        if (!running_.load()) return;

        running_.store(false, std::memory_order_release);

        // Wake the capture thread if it's blocked on WaitForSingleObject
        if (buffer_event_) {
            SetEvent(static_cast<HANDLE>(buffer_event_));
        }

        if (capture_thread_.joinable()) {
            capture_thread_.join();
        }

        if (audio_client_) {
            static_cast<IAudioClient*>(audio_client_)->Stop();
        }

        std::cout << "[AudioCapture] Capture stopped." << std::endl;
    }


    // ===========================================================================
    // Shutdown
    // ===========================================================================

    void AudioCapture::Shutdown() {
        TeardownWASAPISession();
        silence_buf_.clear();
        sample_rate_ = 0;
        channels_    = 0;
        ring_        = nullptr;
        device_lost_.store(false, std::memory_order_relaxed);
    }


    // ===========================================================================
    // CaptureThread
    //
    // Hot loop. Waits for WASAPI to signal buffer_event_ (~every 10ms),
    // then drains all available packets from IAudioCaptureClient.
    //
    // For each packet:
    //   - If AUDCLNT_BUFFERFLAGS_SILENT or data is null: inject silence
    //   - Otherwise: pass float32 PCM directly to encoder_->EncodeSamples()
    //
    // The encoder accumulates samples into 1024-sample AAC frames, encodes,
    // and fires its callback (AudioRingBuffer::Push) autonomously.
    // ===========================================================================

    void AudioCapture::CaptureThread() {
        std::cout << "[AudioCapture] Thread started." << std::endl;

        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);

        // COM must be initialized on this thread so SetupWASAPISession() can call
        // CoCreateInstance during recovery. RPC_E_CHANGED_MODE = already done, fine.
        HRESULT hr_co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        const bool co_owned = SUCCEEDED(hr_co);

        // Recovery loop — runs the inner capture loop, and on device loss tears down
        // and rebuilds the WASAPI session (handles BT reconnect, driver resets, etc.).
        // Retries with increasing back-off. Gives up after kMaxRetries failures.
        static const int kMaxRetries = 5;
        int  retry_count = 0;
        bool gave_up     = false;

        while (running_.load(std::memory_order_relaxed)) {

            IAudioCaptureClient* client =
                static_cast<IAudioCaptureClient*>(capture_client_);
            HANDLE evt = static_cast<HANDLE>(buffer_event_);

            if (!client || !evt) {
                std::cerr << "[AudioCapture] Null WASAPI state, cannot capture." << std::endl;
                gave_up = true;
                break;
            }

            // ------------------------------------------------------------------
            // Inner capture loop — runs until the device is lost or Stop() is called
            // ------------------------------------------------------------------
            bool device_error = false;

            while (running_.load(std::memory_order_relaxed) && !device_error) {

                const DWORD wait_result = WaitForSingleObject(evt, 50);
                if (!running_.load(std::memory_order_relaxed)) break;

                if (wait_result == WAIT_FAILED) {
                    std::cerr << "[AudioCapture] WaitForSingleObject failed: "
                        << GetLastError() << std::endl;
                    device_error = true;
                    break;
                }

                UINT32  next_packet_size = 0;
                HRESULT hr = client->GetNextPacketSize(&next_packet_size);
                if (FAILED(hr)) {
                    std::cerr << "[AudioCapture] GetNextPacketSize failed: 0x"
                        << std::hex << hr << std::dec << std::endl;
                    device_error = true;
                    break;
                }

                while (next_packet_size > 0) {

                    BYTE*  data  = nullptr;
                    UINT32 frames = 0;
                    DWORD  flags  = 0;
                    UINT64 qpc_position = 0;

                    hr = client->GetBuffer(&data, &frames, &flags,
                        nullptr, &qpc_position);
                    if (FAILED(hr)) {
                        std::cerr << "[AudioCapture] GetBuffer failed: 0x"
                            << std::hex << hr << std::dec << std::endl;
                        device_error = true;
                        break;
                    }

                    if (frames > 0) {
                        const bool is_silent =
                            (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0 || !data;
                        if (is_silent) {
                            InjectSilence(frames, qpc_position);
                        }
                        else {
                            ring_->Push(
                                reinterpret_cast<const float*>(data),
                                frames,
                                qpc_position);
                        }
                    }

                    client->ReleaseBuffer(frames);

                    if (FAILED(client->GetNextPacketSize(&next_packet_size))) {
                        device_error = true;
                        break;
                    }
                }
            }

            if (!running_.load(std::memory_order_relaxed)) break;

            // ------------------------------------------------------------------
            // Device error recovery
            //
            // Typical cause: BT headphones disconnected/reconnected, Windows audio
            // engine restarted, exclusive-mode app grabbed the device.
            // Strategy: wait for Windows to settle the new default device, then
            // tear down the old WASAPI session and build a fresh one.
            // Back-off: 1s, 2s, 3s, 4s, 5s — then give up.
            // ------------------------------------------------------------------
            if (device_error) {
                if (retry_count >= kMaxRetries) {
                    std::cerr << "[AudioCapture] WASAPI recovery failed after "
                        << kMaxRetries << " attempts — disabling audio." << std::endl;
                    gave_up = true;
                    break;
                }

                const int delay_ms = 1000 * (retry_count + 1);
                std::cout << "[AudioCapture] Audio device lost. Recovery attempt "
                    << (retry_count + 1) << "/" << kMaxRetries
                    << " in " << delay_ms << "ms..." << std::endl;

                // Interruptible sleep: wake immediately if Stop() is called.
                for (int elapsed = 0; elapsed < delay_ms; elapsed += 100) {
                    if (!running_.load(std::memory_order_relaxed)) break;
                    Sleep(100);
                }
                if (!running_.load(std::memory_order_relaxed)) break;

                TeardownWASAPISession();

                if (SetupWASAPISession()) {
                    IAudioClient* ac = static_cast<IAudioClient*>(audio_client_);
                    if (SUCCEEDED(ac->Start())) {
                        std::cout << "[AudioCapture] WASAPI session restored ("
                            << sample_rate_ << "Hz, " << channels_ << "ch)." << std::endl;
                        retry_count = 0;  // successful recovery resets the counter
                        continue;        // re-enter inner loop with fresh client/evt
                    }
                    std::cerr << "[AudioCapture] IAudioClient::Start failed after recovery." << std::endl;
                }
                else {
                    TeardownWASAPISession();  // clean up partial state from failed setup
                    std::cerr << "[AudioCapture] SetupWASAPISession failed on recovery attempt "
                        << (retry_count + 1) << std::endl;
                }

                retry_count++;
                // Outer loop will retry (with a longer delay next time)
            }
        }

        if (gave_up) {
            device_lost_.store(true, std::memory_order_release);
        }

        if (co_owned) CoUninitialize();
        std::cout << "[AudioCapture] Thread stopped." << std::endl;
    }


    // ===========================================================================
    // InjectSilence
    //
    // Write 'num_frames' zero-filled frames to the encoder so its PTS
    // counter advances even when no audio is playing.
    //
    // Uses the pre-allocated silence_buf_ - zero cost, no allocation.
    // If num_frames exceeds the pre-allocated buffer (pathological case),
    // we process it in chunks rather than silently truncating.
    // ===========================================================================

    void AudioCapture::InjectSilence(uint32_t num_frames, uint64_t qpc_100ns) {
        if (!ring_ || num_frames == 0) return;

        const uint64_t ticks_per_frame = (sample_rate_ > 0)
            ? (10000000ULL / static_cast<uint64_t>(sample_rate_)) : 208ULL;

        const uint32_t buf_frames =
            static_cast<uint32_t>(silence_buf_.size()) / channels_;
        uint32_t remaining = num_frames;
        uint64_t current_qpc = qpc_100ns;

        while (remaining > 0) {
            const uint32_t chunk = std::min(remaining, buf_frames);
            ring_->Push(silence_buf_.data(), chunk, current_qpc);
            current_qpc += static_cast<uint64_t>(chunk) * ticks_per_frame;
            remaining -= chunk;
        }
    }


} // namespace fthr