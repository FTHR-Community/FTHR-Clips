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
#include "audio_encoder.h"
#include "audio_ring_buffer.h"
#include "audio_timeline.h"

#include <iostream>
#include <vector>
#include <cstring>
#include <cassert>
#include <algorithm>
#include <limits>


// Helper macro - log HRESULT failures without throwing
#define FTHR_CHECK_HR(hr, msg)                                          \
    if (FAILED(hr)) {                                                   \
        std::cerr << "[AudioCapture] " << (msg)                         \
                  << " HRESULT=0x" << std::hex << (hr) << std::dec     \
                  << std::endl;                                         \
        return false;                                                   \
    }


namespace fthr {


    // Constructor / Destructor

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



    // Release partially initialized WASAPI session objects. Preserve the ring,
    // silence buffer, and COM apartment for reconnection.

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


    // Open or recover the stored WASAPI device and resize the silence buffer
    // for its format. COM must be initialized on the caller thread. On failure,
    // call TeardownWASAPISession to release partial state.

    bool AudioCapture::SetupWASAPISession() {
        HRESULT hr;

        IMMDeviceEnumerator* enumerator = nullptr;
        hr = CoCreateInstance(
            __uuidof(MMDeviceEnumerator), nullptr,
            CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
            reinterpret_cast<void**>(&enumerator));
        FTHR_CHECK_HR(hr, "CoCreateInstance(MMDeviceEnumerator) failed");
        enumerator_ = static_cast<void*>(enumerator);

        IMMDevice* device = nullptr;
        if (!device_id_.empty()) {
            hr = enumerator->GetDevice(device_id_.c_str(), &device);
            DWORD state = 0;
            if (FAILED(hr) || !device || FAILED(device->GetState(&state))
                    || !(state & DEVICE_STATE_ACTIVE)) {
                if (device) { device->Release(); device = nullptr; }
                std::cerr << "[AudioCapture] Render endpoint unavailable; using Windows default" << std::endl;
                device_id_.clear();
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
                friendly_name_ = buf;
                std::cout << "[AudioCapture] Capture device: " << buf << std::endl;
            }
            PropVariantClear(&name);
            props->Release();
        }

        IAudioClient* audio_client = nullptr;
        hr = device->Activate(__uuidof(IAudioClient), CLSCTX_ALL,
            nullptr, reinterpret_cast<void**>(&audio_client));
        FTHR_CHECK_HR(hr, "IMMDevice::Activate(IAudioClient) failed");
        audio_client_ = static_cast<void*>(audio_client);

        WAVEFORMATEX* mix_fmt = nullptr;
        hr = audio_client->GetMixFormat(&mix_fmt);
        FTHR_CHECK_HR(hr, "IAudioClient::GetMixFormat failed");
        mix_format_ = static_cast<void*>(mix_fmt);

        std::cout << "[AudioCapture] Device mix format: "
            << mix_fmt->nSamplesPerSec << "Hz / " << mix_fmt->nChannels << "ch / "
            << mix_fmt->wBitsPerSample << "-bit" << std::endl;

        if (!format_converter_.Initialize(mix_fmt)) {
            std::cerr << "[AudioCapture] Unsupported WASAPI endpoint format; "
                << "audio capture is disabled rather than distorted." << std::endl;
            return false;
        }
        input_sample_rate_ = format_converter_.input_sample_rate();
        input_channels_ = format_converter_.input_channels();
        input_bytes_per_frame_ = format_converter_.input_bytes_per_frame();
        input_sample_format_ = format_converter_.input_sample_format();
        sample_rate_ = format_converter_.sample_rate();
        channels_ = format_converter_.channels();

        hr = audio_client->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            0, 0, mix_fmt, nullptr);
        FTHR_CHECK_HR(hr, "IAudioClient::Initialize failed");

        IAudioCaptureClient* capture_client = nullptr;
        hr = audio_client->GetService(__uuidof(IAudioCaptureClient),
            reinterpret_cast<void**>(&capture_client));
        FTHR_CHECK_HR(hr, "IAudioClient::GetService(IAudioCaptureClient) failed");
        capture_client_ = static_cast<void*>(capture_client);

        HANDLE evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!evt) {
            std::cerr << "[AudioCapture] CreateEvent failed" << std::endl;
            return false;
        }
        buffer_event_ = static_cast<void*>(evt);

        hr = audio_client->SetEventHandle(evt);
        FTHR_CHECK_HR(hr, "IAudioClient::SetEventHandle failed");

        // Silence is always generated in the canonical replay format.
        const uint32_t max_silence_frames = (kCanonicalAudioSampleRate * 200) / 1000;
        silence_buf_.assign(static_cast<size_t>(max_silence_frames) * channels_, 0.0f);
        const uint32_t max_native_silence_frames = (input_sample_rate_ * 200) / 1000;
        native_silence_buf_.assign(static_cast<size_t>(max_native_silence_frames)
            * input_bytes_per_frame_, 0);

        return true;
    }



    bool AudioCapture::Initialize(AudioRingBuffer* ring,
        const AudioCaptureConfig& config) {
        ring_      = ring;
        device_id_ = config.device_id;
        requested_sample_rate_ = config.preferred_sample_rate;
        requested_channels_ = config.preferred_channels;
        friendly_name_ = device_id_.empty()
            ? "Default Render Endpoint" : "Explicit Render Endpoint";
        packet_count_.store(0, std::memory_order_relaxed);
        discontinuity_count_.store(0, std::memory_order_relaxed);
        first_packet_qpc_100ns_.store(0, std::memory_order_relaxed);
        last_packet_qpc_100ns_.store(0, std::memory_order_relaxed);
        last_packet_end_qpc_100ns_.store(0, std::memory_order_relaxed);
        converted_frame_count_.store(0, std::memory_order_relaxed);
        encoder_error_count_.store(0, std::memory_order_relaxed);
        largest_no_packet_gap_100ns_.store(0, std::memory_order_relaxed);
        silent_packet_count_.store(0, std::memory_order_relaxed);
        no_packet_interval_count_.store(0, std::memory_order_relaxed);
        restart_count_.store(0, std::memory_order_relaxed);
        submitted_frame_count_.store(0, std::memory_order_relaxed);
        synthetic_silence_frame_count_.store(0, std::memory_order_relaxed);
        skipped_input_frame_count_.store(0, std::memory_order_relaxed);
        resampler_correction_count_.store(0, std::memory_order_relaxed);
        normal_jitter_absorbed_count_.store(0, std::memory_order_relaxed);
        hard_discontinuity_recovery_count_.store(0, std::memory_order_relaxed);
        max_observed_drift_samples_.store(0, std::memory_order_relaxed);
        last_swr_compensation_qpc_100ns_.store(0, std::memory_order_relaxed);
        silence_correction_distribution_.Reset();
        skip_correction_distribution_.Reset();
        resampler_correction_distribution_.Reset();
        correction_events_.Reset();
        encoder_input_metrics_.Reset();
        submitted_frames_ = 0;
        drift_controller_.Reset();
        initialize_thread_id_ = std::this_thread::get_id();

        std::cout << "[AudioCapture] Initializing WASAPI loopback..." << std::endl;

        // COM apartment for this thread (the calling/main thread).
        // RPC_E_CHANGED_MODE = already initialized differently, that's fine.
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        initialize_thread_com_owned_ = SUCCEEDED(hr);
        if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
            FTHR_CHECK_HR(hr, "CoInitializeEx failed");
        }

        if (!SetupWASAPISession()) {
            TeardownWASAPISession();
            if (initialize_thread_com_owned_) {
                CoUninitialize();
                initialize_thread_com_owned_ = false;
            }
            return false;
        }

        std::cout << "[AudioCapture] Ready ("
            << sample_rate_ << "Hz, " << channels_ << "ch)" << std::endl;
        return true;
    }

    void AudioCapture::SetEncoder(AudioEncoder* encoder) {
        encoder_ = encoder;
    }


    bool AudioCapture::Start() {
        if (!audio_client_ || !capture_client_ || (!ring_ && !encoder_)) {
            std::cerr << "[AudioCapture] Start() called before Initialize()"
                << std::endl;
            return false;
        }

        if (running_.load()) {
            std::cerr << "[AudioCapture] Already running" << std::endl;
            return false;
        }

        // A capture thread can terminate itself after exhausting recovery
        // attempts. Reap that finished thread before creating a new one.
        if (capture_thread_.joinable()) capture_thread_.join();

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



    void AudioCapture::Stop() {
        if (running_.load(std::memory_order_acquire)) {
            running_.store(false, std::memory_order_release);

            // Wake the capture thread if it's blocked on WaitForSingleObject
            if (buffer_event_) {
                SetEvent(static_cast<HANDLE>(buffer_event_));
            }
        }

        // Always reap a thread which already marked itself stopped. This also
        // makes repeated Stop() calls safe after a failed recovery sequence.
        if (capture_thread_.joinable()) {
            capture_thread_.join();
        }

        if (audio_client_) {
            static_cast<IAudioClient*>(audio_client_)->Stop();
        }

        std::cout << "[AudioCapture] Capture stopped." << std::endl;
    }



    void AudioCapture::Shutdown() {
        TeardownWASAPISession();
        if (initialize_thread_com_owned_
                && std::this_thread::get_id() == initialize_thread_id_) {
            CoUninitialize();
            initialize_thread_com_owned_ = false;
        }
        silence_buf_.clear();
        native_silence_buf_.clear();
        converted_samples_.clear();
        format_converter_.Reset();
        input_sample_rate_ = 0;
        input_channels_ = 0;
        input_bytes_per_frame_ = 0;
        input_sample_format_ = "unavailable:not_resolved";
        sample_rate_ = 0;
        channels_    = 0;
        ring_        = nullptr;
        encoder_     = nullptr;
        timeline_origin_qpc_100ns_.store(0, std::memory_order_release);
        device_lost_.store(false, std::memory_order_relaxed);
    }


    // Drain packets when WASAPI signals buffer_event_. Convert active PCM to
    // the canonical format and insert silence for silent or missing data.

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

        uint64_t next_timeline_100ns = CurrentAudioTimeline100ns();
        while (running_.load(std::memory_order_relaxed)) {

            IAudioCaptureClient* client =
                static_cast<IAudioCaptureClient*>(capture_client_);
            HANDLE evt = static_cast<HANDLE>(buffer_event_);

            if (!client || !evt) {
                std::cerr << "[AudioCapture] Null WASAPI state, cannot capture." << std::endl;
                gave_up = true;
                break;
            }

            // Inner capture loop — runs until the device is lost or Stop() is called
            bool device_error = false;
            // A render endpoint can legitimately stop producing WASAPI
            // packets when every application is silent. Keep the Default Mix
            // source continuous in the same QPC domain so a microphone-only
            // clip still has its mandatory compatibility stream. This is not
            // a second clock: synthetic silence only fills measured gaps
            // between QPC-stamped endpoint packets.
            const auto advance_timeline = [](uint64_t timestamp, uint32_t frames,
                                             uint32_t rate) {
                if (rate == 0) return timestamp;
                return timestamp + (static_cast<uint64_t>(frames) * 10'000'000ULL)
                    / static_cast<uint64_t>(rate);
            };
            const auto fill_silence_until = [this, &next_timeline_100ns,
                                             &device_error](
                                                uint64_t end_100ns,
                                                uint64_t packet_qpc_100ns,
                                                uint32_t input_frames,
                                                AudioTimelinePacketAdjustment* result_out) -> bool {
                if (end_100ns == 0 || sample_rate_ == 0) return true;
                const uint64_t expected_qpc_100ns = next_timeline_100ns;
                // Reuse the same bounded continuity policy as microphone
                // capture. For a real packet, pass its frame count so the
                // shared helper owns both gap insertion and overlap skipping.
                const auto adjustment = ReconcileAudioTimelinePacket(
                    next_timeline_100ns, end_100ns, input_frames,
                    input_frames > 0 ? input_sample_rate_ : sample_rate_,
                    sample_rate_, kMaxSyntheticAudioGapSeconds);
                if (result_out) *result_out = adjustment;
                if (adjustment.normal_jitter_absorbed) {
                    normal_jitter_absorbed_count_.fetch_add(
                        1, std::memory_order_relaxed);
                }
                if (adjustment.classification == AudioTimelineAdjustmentClass::HardGap
                        || adjustment.classification
                            == AudioTimelineAdjustmentClass::HardOverlap) {
                    hard_discontinuity_recovery_count_.fetch_add(
                        1, std::memory_order_relaxed);
                }
                if (adjustment.large_gap) {
                    // The AAC ring has no discontinuity marker. Refuse a
                    // suspend-sized catch-up rather than encoding an
                    // unbounded silence burst or collapsing resumed PTS.
                    discontinuity_count_.fetch_add(1, std::memory_order_relaxed);
                    std::cerr << "[AudioCapture] Synthetic silence gap exceeds "
                        << kMaxSyntheticAudioGapSeconds
                        << "s; restarting audio session." << std::endl;
                    device_error = true;
                    return false;
                }
                if (adjustment.silence_frames > 0) {
                    if (!InjectSilence(adjustment.silence_frames,
                                       expected_qpc_100ns)) {
                        device_error = true;
                        return false;
                    }
                    const uint64_t last_swr =
                        last_swr_compensation_qpc_100ns_.load(
                            std::memory_order_acquire);
                    const uint64_t swr_delta = expected_qpc_100ns >= last_swr
                        ? expected_qpc_100ns - last_swr : last_swr - expected_qpc_100ns;
                    correction_events_.Record(AudioCorrectionEvent{
                        AudioCorrectionSource::System,
                        AudioCorrectionDirection::Insert,
                        expected_qpc_100ns,
                        packet_qpc_100ns,
                        expected_qpc_100ns,
                        adjustment.silence_frames,
                        sample_rate_,
                        0.0,
                        0,
                        0.0,
                        last_swr > 0
                            && swr_delta <= kAudioCorrectionRecentWindow100ns});
                }
                next_timeline_100ns = adjustment.next_timeline_100ns;
                return true;
            };

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

                // When the default render endpoint is totally idle it can
                // report no packet on both an event wake and a timeout. The
                // gap is real timeline silence, not a missing source.
                if (next_packet_size == 0) {
                    no_packet_interval_count_.fetch_add(1, std::memory_order_relaxed);
                    if (!fill_silence_until(CurrentAudioTimeline100ns(), 0, 0, nullptr)) break;
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
                            silent_packet_count_.fetch_add(1, std::memory_order_relaxed);
                        }
                        const uint64_t packet_qpc = qpc_position != 0
                            ? qpc_position : CurrentAudioTimeline100ns();
                        packet_count_.fetch_add(1, std::memory_order_relaxed);
                        if ((flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) != 0) {
                            discontinuity_count_.fetch_add(1, std::memory_order_relaxed);
                        }
                        uint64_t no_packet = 0;
                        first_packet_qpc_100ns_.compare_exchange_strong(
                            no_packet, packet_qpc, std::memory_order_release,
                            std::memory_order_relaxed);
                        const uint64_t previous_packet_end_qpc =
                            last_packet_end_qpc_100ns_.load(std::memory_order_relaxed);
                        if (previous_packet_end_qpc > 0 && packet_qpc > previous_packet_end_qpc) {
                            const uint64_t gap = packet_qpc - previous_packet_end_qpc;
                            uint64_t largest = largest_no_packet_gap_100ns_.load(
                                std::memory_order_relaxed);
                            while (gap > largest && !largest_no_packet_gap_100ns_.compare_exchange_weak(
                                largest, gap, std::memory_order_release,
                                std::memory_order_relaxed)) {}
                        }
                        last_packet_qpc_100ns_.store(
                            packet_qpc, std::memory_order_release);
                        const uint64_t packet_duration = input_sample_rate_ > 0
                            ? (static_cast<uint64_t>(frames) * 10'000'000ULL)
                                / input_sample_rate_ : 0;
                        last_packet_end_qpc_100ns_.store(
                            packet_qpc + packet_duration, std::memory_order_release);
                        const uint64_t expected_skip_qpc = next_timeline_100ns;
                        AudioTimelinePacketAdjustment adjustment;
                        if (!fill_silence_until(packet_qpc, packet_qpc, frames,
                                                &adjustment)) {
                            client->ReleaseBuffer(frames);
                            break;
                        }

                        // The shared policy has already classified a small
                        // overlap as absorbed jitter and only returns a
                        // prefix skip for a real hard overlap.
                        const uint32_t skip_frames = adjustment.skip_input_frames;
                        const uint32_t submit_frames = frames - skip_frames;
                        if (skip_frames > 0) {
                            skipped_input_frame_count_.fetch_add(
                                skip_frames, std::memory_order_relaxed);
                            skip_correction_distribution_.Observe(skip_frames);
                            const uint64_t last_swr =
                                last_swr_compensation_qpc_100ns_.load(
                                    std::memory_order_acquire);
                            const uint64_t swr_delta = expected_skip_qpc >= last_swr
                                ? expected_skip_qpc - last_swr : last_swr - expected_skip_qpc;
                            correction_events_.Record(AudioCorrectionEvent{
                                AudioCorrectionSource::System,
                                AudioCorrectionDirection::Skip,
                                expected_skip_qpc,
                                packet_qpc,
                                expected_skip_qpc,
                                skip_frames,
                                input_sample_rate_,
                                0.0,
                                0,
                                0.0,
                                last_swr > 0
                                    && swr_delta <= kAudioCorrectionRecentWindow100ns});
                        }
                        if (submit_frames > 0) {
                            const uint64_t submit_qpc = advance_timeline(packet_qpc, skip_frames,
                                input_sample_rate_);
                            if (is_silent) {
                                if (!SubmitNativeSamples(nullptr, submit_frames, submit_qpc)) {
                                    device_error = true;
                                    client->ReleaseBuffer(frames);
                                    break;
                                }
                            } else {
                                if (!SubmitNativeSamples(data + static_cast<size_t>(skip_frames)
                                        * input_bytes_per_frame_, submit_frames, submit_qpc)) {
                                    device_error = true;
                                    client->ReleaseBuffer(frames);
                                    break;
                                }
                            }
                        }
                        next_timeline_100ns = adjustment.next_timeline_100ns;
                    }

                    client->ReleaseBuffer(frames);

                    if (FAILED(client->GetNextPacketSize(&next_packet_size))) {
                        device_error = true;
                        break;
                    }
                }
            }

            if (!running_.load(std::memory_order_relaxed)) break;

            // Reopen the WASAPI session after device loss, allowing Windows to settle.
            // Retry with delays of 1 through 5 seconds, then stop recovery.
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

                // Connection loss switches this session to the Windows default.
                // Keep the user's preference on disk for the next app launch.
                device_id_.clear();
                if (SetupWASAPISession()) {
                    IAudioClient* ac = static_cast<IAudioClient*>(audio_client_);
                    if (SUCCEEDED(ac->Start())) {
                        std::cout << "[AudioCapture] WASAPI session restored ("
                            << sample_rate_ << "Hz, " << channels_ << "ch)." << std::endl;
                        retry_count = 0;  // successful recovery resets the counter
                        restart_count_.fetch_add(1, std::memory_order_relaxed);
                        continue;        // re-enter inner loop with fresh client/evt
                    }
                    std::cerr << "[AudioCapture] IAudioClient::Start failed after recovery." << std::endl;
                    // Session was set up but Start() failed — tear down to avoid leaking
                    // a partially-running session that would block the next SetupWASAPISession().
                    TeardownWASAPISession();
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


    // Advance the audio timeline with zero-filled frames from silence_buf_.
    // Process oversized requests in chunks to avoid allocating in the capture loop.

    bool AudioCapture::InjectSilence(uint32_t num_frames, uint64_t qpc_100ns) {
        if ((!ring_ && !encoder_) || num_frames == 0) return num_frames == 0;

        const uint64_t ticks_per_frame = (sample_rate_ > 0)
            ? (10000000ULL / static_cast<uint64_t>(sample_rate_)) : 208ULL;

        const uint32_t buf_frames =
            static_cast<uint32_t>(silence_buf_.size()) / channels_;
        if (buf_frames == 0) return false;
        uint32_t remaining = num_frames;
        uint64_t current_qpc = qpc_100ns;

        while (remaining > 0) {
            const uint32_t chunk = std::min(remaining, buf_frames);
            if (!SubmitSamples(silence_buf_.data(), chunk, current_qpc)) return false;
            synthetic_silence_frame_count_.fetch_add(chunk, std::memory_order_relaxed);
            current_qpc += static_cast<uint64_t>(chunk) * ticks_per_frame;
            remaining -= chunk;
        }
        silence_correction_distribution_.Observe(num_frames);
        return true;
    }

    bool AudioCapture::SubmitSamples(const float* interleaved_data,
        uint32_t frame_count, uint64_t qpc_100ns) {
        if (!interleaved_data || frame_count == 0) return false;
        // Some virtual/device drivers omit pu64QPCPosition. A zero timestamp
        // must not make the persistent AAC ring unsaveable: use the same QPC
        // clock at capture time instead of creating a separate wall clock.
        if (qpc_100ns == 0) qpc_100ns = CurrentAudioTimeline100ns();
        if (qpc_100ns > 0) {
            uint64_t expected = 0;
            timeline_origin_qpc_100ns_.compare_exchange_strong(
                expected, qpc_100ns, std::memory_order_release,
                std::memory_order_relaxed);
        }
        bool encoder_ok = true;
        encoder_input_metrics_.Observe(interleaved_data, frame_count, channels_);
        if (encoder_ && !encoder_->EncodeSamples(interleaved_data, frame_count * channels_)) {
            encoder_error_count_.fetch_add(1, std::memory_order_relaxed);
            encoder_ok = false;
        }
        if (ring_) {
            ring_->Push(interleaved_data, frame_count, qpc_100ns);
        }
        submitted_frames_ += frame_count;
        submitted_frame_count_.fetch_add(frame_count, std::memory_order_relaxed);
        return encoder_ok;
    }

    bool AudioCapture::SubmitNativeSamples(const uint8_t* interleaved_data,
        uint32_t frame_count, uint64_t qpc_100ns) {
        if (frame_count == 0 || !format_converter_.input_channels())
            return false;
        const auto correction = drift_controller_.Observe(qpc_100ns, submitted_frames_);
        max_observed_drift_samples_.store(
            drift_controller_.max_observed_drift_samples(), std::memory_order_release);
        if (correction.sample_delta != 0
                && correction.compensation_distance_samples > 0
                && !format_converter_.ApplyDriftCorrection(
                    correction.sample_delta, correction.compensation_distance_samples)) {
            std::cerr << "[AudioCapture] Audio drift compensation setup failed" << std::endl;
            return false;
        }
        if (correction.sample_delta != 0
                && correction.compensation_distance_samples > 0) {
            resampler_correction_count_.fetch_add(1, std::memory_order_relaxed);
            resampler_correction_distribution_.Observe(correction.sample_delta);
            last_swr_compensation_qpc_100ns_.store(qpc_100ns,
                std::memory_order_release);
        }
        const uint8_t* input = interleaved_data;
        if (!input) {
            const size_t required = static_cast<size_t>(frame_count)
                * input_bytes_per_frame_;
            if (native_silence_buf_.size() < required) native_silence_buf_.resize(required);
            std::fill(native_silence_buf_.begin(),
                native_silence_buf_.begin() + required, uint8_t{0});
            input = native_silence_buf_.data();
        }
        converted_samples_.clear();
        if (!format_converter_.Convert(input, frame_count, &converted_samples_)) {
            std::cerr << "[AudioCapture] WASAPI format conversion failed" << std::endl;
            return false;
        }
        if (!converted_samples_.empty()) {
            const uint32_t converted_frames = static_cast<uint32_t>(
                converted_samples_.size() / kCanonicalAudioChannels);
            const bool submitted = SubmitSamples(converted_samples_.data(),
                converted_frames, qpc_100ns);
            converted_frame_count_.fetch_add(converted_frames, std::memory_order_relaxed);
            if (!submitted) return false;
        }
        return true;
    }

    uint64_t AudioCapture::GetEncodedPacketCount() const {
        return encoder_ ? encoder_->stats().packets_emitted : 0;
    }

} // namespace fthr
