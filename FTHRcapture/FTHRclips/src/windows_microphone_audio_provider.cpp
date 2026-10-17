// Native Windows microphone capture scoped to one capture generation.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <audioclient.h>
#include <propkeydef.h>
#include <functiondiscoverykeys_devpkey.h>
#include <mmdeviceapi.h>
#include <propvarutil.h>
#include <wrl/client.h>

#include "windows_microphone_audio_provider.h"

#include "audio_encoder.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <limits>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

#pragma comment(lib, "Ole32.lib")
#pragma comment(lib, "Propsys.lib")

using Microsoft::WRL::ComPtr;

namespace fthr {
namespace {

constexpr DWORD kCaptureWaitMs = 250;

std::string Utf8FromWide(const wchar_t* value) {
    if (!value || !*value) return {};
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1,
        nullptr, 0, nullptr, nullptr);
    if (size <= 1) return {};
    std::string result(static_cast<size_t>(size), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1,
            result.data(), size, nullptr, nullptr) != size) {
        return {};
    }
    result.pop_back();
    return result;
}

std::string HrText(HRESULT hr) {
    char text[32] = {};
    std::snprintf(text, sizeof(text), "0x%08lX", static_cast<unsigned long>(hr));
    return text;
}

std::string FriendlyName(IMMDevice* device) {
    if (!device) return {};
    ComPtr<IPropertyStore> properties;
    PROPVARIANT name;
    PropVariantInit(&name);
    const HRESULT hr = device->OpenPropertyStore(STGM_READ, &properties);
    const HRESULT get = SUCCEEDED(hr)
        ? properties->GetValue(PKEY_Device_FriendlyName, &name) : hr;
    const std::string result = SUCCEEDED(get) && name.vt == VT_LPWSTR
        ? Utf8FromWide(name.pwszVal) : std::string{};
    PropVariantClear(&name);
    return result;
}

struct MicrophoneSession {
    ComPtr<IAudioClient> audio_client;
    ComPtr<IAudioCaptureClient> capture_client;
    WAVEFORMATEX* format = nullptr;
    HANDLE sample_event = nullptr;
    AudioFormatConverter converter;
    std::vector<float> converted;
    std::vector<uint8_t> silent_input;

    void Close() {
        if (audio_client) audio_client->Stop();
        capture_client.Reset();
        audio_client.Reset();
        if (sample_event) CloseHandle(sample_event);
        sample_event = nullptr;
        if (format) CoTaskMemFree(format);
        format = nullptr;
        converted.clear();
        silent_input.clear();
    }
    ~MicrophoneSession() { Close(); }
};

}  // namespace

void DownmixWindowsMicrophoneToMono(float* samples, size_t sample_count) {
    if (!samples) return;
    for (size_t index = 0; index + 1 < sample_count; index += 2) {
        const float mono = (samples[index] + samples[index + 1]) * 0.5f;
        samples[index] = mono;
        samples[index + 1] = mono;
    }
}

void ApplyWindowsMicrophoneInputGain(float* samples, size_t sample_count,
                                     float configured_gain) {
    if (!samples || sample_count == 0) return;
    const float gain = std::clamp(configured_gain, 0.0f, 2.0f);
    if (gain == 1.0f) return;
    for (size_t index = 0; index < sample_count; ++index) {
        samples[index] *= gain;
    }
}

uint64_t WindowsMicrophoneProcessingDiagnosticsAccumulator::CaptureBufferDuration100ns(
    uint32_t buffer_frames, uint32_t sample_rate) {
    if (buffer_frames == 0 || sample_rate == 0) return 0;
    return (static_cast<uint64_t>(buffer_frames) * 10'000'000ULL)
        / static_cast<uint64_t>(sample_rate);
}

bool WindowsMicrophoneProcessingDiagnosticsAccumulator::ProcessingDurationMeetsBuffer(
    uint64_t processing_duration_100ns, uint64_t buffer_duration_100ns) {
    return buffer_duration_100ns != 0
        && processing_duration_100ns >= buffer_duration_100ns;
}

void WindowsMicrophoneProcessingDiagnosticsAccumulator::ConfigureCaptureBuffer(
    uint32_t buffer_frames, uint32_t sample_rate) {
    capture_buffer_frames_.store(buffer_frames, std::memory_order_relaxed);
    capture_buffer_duration_100ns_.store(
        CaptureBufferDuration100ns(buffer_frames, sample_rate),
        std::memory_order_relaxed);
}

void WindowsMicrophoneProcessingDiagnosticsAccumulator::ObservePacket(
    bool timestamp_error, uint64_t processing_duration_100ns) {
    if (timestamp_error) {
        timestamp_error_count_.fetch_add(1, std::memory_order_relaxed);
    }
    uint64_t largest = max_packet_processing_duration_100ns_.load(
        std::memory_order_relaxed);
    while (processing_duration_100ns > largest
            && !max_packet_processing_duration_100ns_.compare_exchange_weak(
                largest, processing_duration_100ns, std::memory_order_release,
                std::memory_order_relaxed)) {}
    if (ProcessingDurationMeetsBuffer(
            processing_duration_100ns,
            capture_buffer_duration_100ns_.load(std::memory_order_relaxed))) {
        packet_processing_at_or_above_buffer_duration_count_.fetch_add(
            1, std::memory_order_relaxed);
    }
}

WindowsMicrophoneProcessingDiagnosticsSnapshot
WindowsMicrophoneProcessingDiagnosticsAccumulator::Snapshot() const {
    WindowsMicrophoneProcessingDiagnosticsSnapshot snapshot;
    snapshot.capture_buffer_frames = capture_buffer_frames_.load(
        std::memory_order_relaxed);
    snapshot.capture_buffer_duration_100ns = capture_buffer_duration_100ns_.load(
        std::memory_order_relaxed);
    snapshot.timestamp_error_count = timestamp_error_count_.load(
        std::memory_order_relaxed);
    snapshot.max_packet_processing_duration_100ns =
        max_packet_processing_duration_100ns_.load(std::memory_order_acquire);
    snapshot.packet_processing_at_or_above_buffer_duration_count =
        packet_processing_at_or_above_buffer_duration_count_.load(
            std::memory_order_relaxed);
    return snapshot;
}

void WindowsMicrophoneProcessingDiagnosticsAccumulator::Reset() {
    capture_buffer_frames_.store(0, std::memory_order_relaxed);
    capture_buffer_duration_100ns_.store(0, std::memory_order_relaxed);
    timestamp_error_count_.store(0, std::memory_order_relaxed);
    max_packet_processing_duration_100ns_.store(0, std::memory_order_relaxed);
    packet_processing_at_or_above_buffer_duration_count_.store(
        0, std::memory_order_relaxed);
}

bool EnumerateWindowsMicrophoneEndpoints(
    std::vector<WindowsMicrophoneEndpoint>* endpoints, std::string* error, bool render) {
    if (!endpoints) return false;
    endpoints->clear();
    const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(apartment) && apartment != RPC_E_CHANGED_MODE) {
        if (error) *error = "microphone enumeration could not initialize COM " + HrText(apartment);
        return false;
    }
    ComPtr<IMMDeviceEnumerator> enumerator;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        IID_PPV_ARGS(&enumerator));
    if (FAILED(hr)) {
        if (error) *error = "microphone enumeration could not create device enumerator " + HrText(hr);
        if (SUCCEEDED(apartment)) CoUninitialize();
        return false;
    }
    ComPtr<IMMDevice> default_device;
    std::wstring default_id;
    if (SUCCEEDED(enumerator->GetDefaultAudioEndpoint(render ? eRender : eCapture, eConsole, &default_device))) {
        LPWSTR value = nullptr;
        if (SUCCEEDED(default_device->GetId(&value)) && value) {
            default_id = value;
            CoTaskMemFree(value);
        }
    }
    ComPtr<IMMDeviceCollection> collection;
    hr = enumerator->EnumAudioEndpoints(render ? eRender : eCapture, DEVICE_STATEMASK_ALL, &collection);
    if (FAILED(hr)) {
        if (error) *error = "microphone enumeration could not list endpoints " + HrText(hr);
        if (SUCCEEDED(apartment)) CoUninitialize();
        return false;
    }
    UINT count = 0;
    collection->GetCount(&count);
    endpoints->reserve(count);
    for (UINT index = 0; index < count; ++index) {
        ComPtr<IMMDevice> device;
        if (FAILED(collection->Item(index, &device))) continue;
        LPWSTR endpoint_id = nullptr;
        if (FAILED(device->GetId(&endpoint_id)) || !endpoint_id) continue;
        DWORD state = 0;
        device->GetState(&state);
        WindowsMicrophoneEndpoint endpoint;
        endpoint.endpoint_id = endpoint_id;
        endpoint.display_name = FriendlyName(device.Get());
        endpoint.device_state = state;
        endpoint.is_default = endpoint.endpoint_id == default_id;
        CoTaskMemFree(endpoint_id);
        if (!endpoint.endpoint_id.empty() && !endpoint.display_name.empty())
            endpoints->push_back(std::move(endpoint));
    }
    std::sort(endpoints->begin(), endpoints->end(), [](const auto& left, const auto& right) {
        if (left.is_default != right.is_default) return left.is_default;
        return left.display_name < right.display_name;
    });
    if (SUCCEEDED(apartment)) CoUninitialize();
    return true;
}

struct WindowsMicrophoneAudioProvider::Impl {
    explicit Impl(WindowsMicrophoneAudioProviderConfig initial) : config(std::move(initial)) {}

    WindowsMicrophoneAudioProviderConfig config;
    std::thread thread;
    HANDLE stop_event = nullptr;
    std::atomic<bool> running{false};
    std::atomic<bool> failed{false};
    std::atomic<uint64_t> timeline_origin_100ns{0};
    std::atomic<uint64_t> replay_origin_100ns{0};
    std::atomic<uint64_t> last_packet_qpc_100ns{0};
    std::atomic<uint64_t> packet_count{0};
    std::atomic<uint64_t> discontinuity_count{0};
    std::atomic<uint64_t> first_packet_qpc_100ns{0};
    std::atomic<uint64_t> last_packet_end_qpc_100ns{0};
    std::atomic<uint64_t> converted_frame_count{0};
    std::atomic<uint64_t> largest_no_packet_gap_100ns{0};
    std::atomic<uint64_t> synthetic_silence_frame_count{0};
    std::atomic<uint64_t> skipped_input_frame_count{0};
    std::atomic<uint64_t> resampler_correction_count{0};
    std::atomic<uint64_t> normal_jitter_absorbed_count{0};
    std::atomic<uint64_t> hard_discontinuity_recovery_count{0};
    std::atomic<uint64_t> last_swr_compensation_qpc_100ns{0};
    AudioCorrectionDistribution silence_corrections;
    AudioCorrectionDistribution skip_corrections;
    AudioCorrectionDistribution resampler_corrections;
    AudioCorrectionEventRing correction_events;
    WindowsMicrophoneProcessingDiagnosticsAccumulator processing_diagnostics;
    mutable std::mutex state_mutex;
    std::string error;
    WindowsMicrophoneRuntimeInfo info;
    std::unique_ptr<AudioEncoder> encoder;
    std::unique_ptr<EncodedAudioPacketRing> ring;
    AudioDriftController drift{kCanonicalAudioSampleRate};
    int64_t submitted_frames = 0;
    uint64_t next_timeline_100ns = 0;
    std::vector<float> silence;
    AudioSignalMetrics encoder_input_metrics;

    void SetError(std::string value) {
        std::lock_guard<std::mutex> lock(state_mutex);
        error = std::move(value);
    }

    void SetState(WindowsMicrophoneRuntimeInfo::State state) {
        std::lock_guard<std::mutex> lock(state_mutex);
        info.state = state;
    }

    bool StopRequested() const {
        return stop_event && WaitForSingleObject(stop_event, 0) == WAIT_OBJECT_0;
    }

    bool OpenSession(MicrophoneSession* session) {
        if (!session) return false;
        ComPtr<IMMDeviceEnumerator> enumerator;
        HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
            IID_PPV_ARGS(&enumerator));
        if (FAILED(hr)) {
            SetError("microphone could not create device enumerator " + HrText(hr));
            return false;
        }
        ComPtr<IMMDevice> device;
        if (config.use_default_endpoint) {
            hr = enumerator->GetDefaultAudioEndpoint(eCapture, eConsole, &device);
        } else if (!config.endpoint_id.empty()) {
            hr = enumerator->GetDevice(config.endpoint_id.c_str(), &device);
        } else {
            hr = E_INVALIDARG;
        }
        if (FAILED(hr) || !device) {
            SetError(config.use_default_endpoint
                ? "default microphone is unavailable " + HrText(hr)
                : "selected microphone is unavailable; it was not replaced " + HrText(hr));
            return false;
        }
        LPWSTR active_id = nullptr;
        if (SUCCEEDED(device->GetId(&active_id)) && active_id) {
            std::lock_guard<std::mutex> lock(state_mutex);
            info.active_endpoint_id = active_id;
            info.active_display_name = FriendlyName(device.Get());
            CoTaskMemFree(active_id);
        }
        hr = device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
            reinterpret_cast<void**>(session->audio_client.GetAddressOf()));
        if (FAILED(hr)) {
            SetError("microphone IAudioClient activation failed " + HrText(hr));
            return false;
        }
        hr = session->audio_client->GetMixFormat(&session->format);
        if (FAILED(hr) || !session->format) {
            SetError("microphone mix-format query failed " + HrText(hr));
            return false;
        }
        if (!session->converter.Initialize(session->format)) {
            SetError("microphone source format is unsupported; source is not captured");
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            info.input_format = {session->format->nSamplesPerSec, session->format->nChannels,
                                 session->converter.input_sample_format()};
        }
        session->sample_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!session->sample_event) {
            SetError("microphone sample event creation failed");
            return false;
        }
        hr = session->audio_client->Initialize(AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_EVENTCALLBACK, 0, 0, session->format, nullptr);
        if (SUCCEEDED(hr)) hr = session->audio_client->SetEventHandle(session->sample_event);
        if (SUCCEEDED(hr)) hr = session->audio_client->GetService(IID_PPV_ARGS(&session->capture_client));
        if (FAILED(hr)) {
            SetError("microphone WASAPI initialization failed " + HrText(hr));
            return false;
        }
        UINT32 capture_buffer_frames = 0;
        if (SUCCEEDED(session->audio_client->GetBufferSize(&capture_buffer_frames))) {
            processing_diagnostics.ConfigureCaptureBuffer(
                capture_buffer_frames, session->format->nSamplesPerSec);
        }
        hr = session->audio_client->Start();
        if (FAILED(hr)) {
            SetError("microphone IAudioClient::Start failed " + HrText(hr));
            return false;
        }
        return true;
    }

    bool EnsureEncoder(uint64_t qpc_100ns) {
        if (encoder && ring) return true;
        if (qpc_100ns == 0 || !config.source.identity.id.IsValid()) {
            SetError("microphone did not receive a usable capture timestamp");
            return false;
        }
        auto new_ring = std::make_unique<EncodedAudioPacketRing>(
            config.source.identity.id, config.generation,
            AudioSourceFormat{kCanonicalAudioSampleRate, kCanonicalAudioChannels, "fltp"},
            config.retention_seconds);
        auto new_encoder = std::make_unique<AudioEncoder>();
        const auto replay_origin = config.replay_pause_clock
            ? config.replay_pause_clock->Map(qpc_100ns) : qpc_100ns;
        if (!new_encoder->Initialize(kCanonicalAudioSampleRate, kCanonicalAudioChannels,
                config.bitrate_kbps,
                [packet_ring = new_ring.get(), clock = config.replay_pause_clock,
                 qpc_100ns, replay_origin](const uint8_t* data, uint32_t size, int64_t pts) {
                    if (clock) {
                        const auto mapped = clock->MapAudio(
                            qpc_100ns, replay_origin, pts, 1024, kCanonicalAudioSampleRate);
                        if (!mapped) return;
                        pts = *mapped;
                    }
                    packet_ring->Push({std::vector<uint8_t>(data, data + size), pts, 1024});
                })) {
            SetError("microphone AAC encoder initialization failed");
            return false;
        }
        new_ring->SetCodecExtradata(new_encoder->GetExtradata());
        drift.Reset();
        last_swr_compensation_qpc_100ns.store(0, std::memory_order_relaxed);
        drift.Observe(qpc_100ns, 0);
        submitted_frames = 0;
        next_timeline_100ns = qpc_100ns;
        silence.assign(4800 * kCanonicalAudioChannels, 0.0f);
        ring = std::move(new_ring);
        encoder = std::move(new_encoder);
        // Publish ownership only after both objects are fully initialized.
        // Readers acquire the origin before dereferencing either pointer.
        timeline_origin_100ns.store(qpc_100ns, std::memory_order_release);
        replay_origin_100ns.store(replay_origin, std::memory_order_release);
        return true;
    }

    bool SubmitCanonicalSilence(uint32_t frames) {
        if (!encoder || frames == 0) return true;
        const uint32_t block = static_cast<uint32_t>(silence.size())
            / kCanonicalAudioChannels;
        if (block == 0) return false;
        uint32_t remaining = frames;
        while (remaining > 0) {
            const uint32_t count = std::min(remaining, block);
            encoder_input_metrics.Observe(silence.data(), count,
                kCanonicalAudioChannels);
            if (!encoder->EncodeSamples(silence.data(), count * kCanonicalAudioChannels)) {
                SetError("microphone AAC encoding of timeline silence failed");
                return false;
            }
            submitted_frames += count;
            synthetic_silence_frame_count.fetch_add(count, std::memory_order_relaxed);
            remaining -= count;
        }
        silence_corrections.Observe(frames);
        return true;
    }

    bool ConvertAndSubmit(MicrophoneSession* session, const BYTE* data,
                          uint32_t input_frames, uint64_t qpc_100ns, bool silent) {
        if (!session || input_frames == 0) return true;
        if (qpc_100ns == 0) qpc_100ns = CurrentAudioTimeline100ns();
        const uint32_t input_rate = session->converter.input_sample_rate();
        if (input_rate == 0) {
            SetError("microphone converter has no input sample rate");
            return false;
        }
        // Record endpoint packet timing independently of converter output. A
        // persistent resampler may legally emit no canonical samples for its
        // first packet, but diagnostics still need the packet start/end and
        // the next packet's no-data gap.
        const uint64_t packet_duration = (static_cast<uint64_t>(input_frames)
            * 10'000'000ULL) / input_rate;
        const uint64_t packet_end = qpc_100ns + packet_duration;
        last_packet_qpc_100ns.store(qpc_100ns, std::memory_order_release);
        const uint64_t previous_packet_end = last_packet_end_qpc_100ns.load(
            std::memory_order_relaxed);
        if (previous_packet_end > 0 && qpc_100ns > previous_packet_end) {
            const uint64_t gap = qpc_100ns - previous_packet_end;
            uint64_t largest = largest_no_packet_gap_100ns.load(
                std::memory_order_relaxed);
            while (gap > largest && !largest_no_packet_gap_100ns.compare_exchange_weak(
                largest, gap, std::memory_order_release,
                std::memory_order_relaxed)) {}
        }
        last_packet_end_qpc_100ns.store(packet_end, std::memory_order_release);
        uint64_t no_packet = 0;
        first_packet_qpc_100ns.compare_exchange_strong(
            no_packet, qpc_100ns, std::memory_order_release,
            std::memory_order_relaxed);
        if (!EnsureEncoder(qpc_100ns)) return false;
        const auto timeline = ReconcileAudioTimelinePacket(
            next_timeline_100ns, qpc_100ns, input_frames, input_rate);
        if (timeline.normal_jitter_absorbed) {
            normal_jitter_absorbed_count.fetch_add(1, std::memory_order_relaxed);
        }
        if (timeline.classification == AudioTimelineAdjustmentClass::HardGap
                || timeline.classification
                    == AudioTimelineAdjustmentClass::HardOverlap) {
            hard_discontinuity_recovery_count.fetch_add(1, std::memory_order_relaxed);
        }
        if (timeline.large_gap) {
            // There is no representable discontinuity marker in the current
            // AAC ring. Stop this generation rather than advancing the cursor
            // and encoding resumed media at a collapsed PTS.
            discontinuity_count.fetch_add(1, std::memory_order_relaxed);
            SetError("microphone packet gap exceeded bounded continuity window; "
                "capture stopped to preserve the audio timeline");
            return false;
        }
        if (timeline.silence_frames > 0
                    && !SubmitCanonicalSilence(timeline.silence_frames)) {
            return false;
        }
        const uint32_t skip_frames = timeline.skip_input_frames;
        // Apply any hard overlap before observing drift.  The drift controller
        // must see the post-repair packet position; otherwise one timestamp
        // error is corrected once by prefix dropping and again by SWR.
        const uint64_t submit_qpc_100ns = qpc_100ns
            + (static_cast<uint64_t>(skip_frames) * 10'000'000ULL)
                / static_cast<uint64_t>(input_rate);
        const auto adjustment = drift.Observe(submit_qpc_100ns, submitted_frames);
        if (adjustment.sample_delta != 0 && adjustment.compensation_distance_samples > 0
                && !session->converter.ApplyDriftCorrection(
                    adjustment.sample_delta, adjustment.compensation_distance_samples)) {
            SetError("microphone drift compensation setup failed");
            return false;
        }
        if (adjustment.sample_delta != 0
                && adjustment.compensation_distance_samples > 0) {
            resampler_correction_count.fetch_add(1, std::memory_order_relaxed);
            resampler_corrections.Observe(adjustment.sample_delta);
            last_swr_compensation_qpc_100ns.store(submit_qpc_100ns,
                std::memory_order_release);
        }
        const uint64_t expected_qpc_100ns = next_timeline_100ns;
        const uint64_t last_swr = last_swr_compensation_qpc_100ns.load(
            std::memory_order_acquire);
        const uint64_t swr_delta = expected_qpc_100ns >= last_swr
            ? expected_qpc_100ns - last_swr : last_swr - expected_qpc_100ns;
        if (timeline.silence_frames > 0) {
            correction_events.Record(AudioCorrectionEvent{
                AudioCorrectionSource::Microphone,
                AudioCorrectionDirection::Insert,
                expected_qpc_100ns,
                qpc_100ns,
                expected_qpc_100ns,
                timeline.silence_frames,
                kCanonicalAudioSampleRate,
                0.0,
                0,
                0.0,
                last_swr > 0
                    && swr_delta <= kAudioCorrectionRecentWindow100ns});
        }
        if (skip_frames > 0) {
            skipped_input_frame_count.fetch_add(skip_frames, std::memory_order_relaxed);
            skip_corrections.Observe(skip_frames);
            correction_events.Record(AudioCorrectionEvent{
                AudioCorrectionSource::Microphone,
                AudioCorrectionDirection::Skip,
                expected_qpc_100ns,
                qpc_100ns,
                expected_qpc_100ns,
                skip_frames,
                input_rate,
                0.0,
                0,
                0.0,
                last_swr > 0
                    && swr_delta <= kAudioCorrectionRecentWindow100ns});
        }
        const uint32_t submit_frames = input_frames - skip_frames;
        if (submit_frames == 0) {
            next_timeline_100ns = timeline.next_timeline_100ns;
            return true;
        }
        const uint8_t* input = data;
        if (silent || !input) {
            const size_t bytes = static_cast<size_t>(input_frames)
                * session->format->nBlockAlign;
            session->silent_input.assign(bytes, 0);
            input = session->silent_input.data();
        }
        input += static_cast<size_t>(skip_frames)
            * session->converter.input_bytes_per_frame();
        session->converted.clear();
        if (!session->converter.Convert(input, submit_frames, &session->converted)) {
            SetError("microphone resampling failed");
            return false;
        }
        if (!session->converted.empty()) {
            // Preserve the canonical stereo ABI: mono is duplicated to both
            // channels so voice is centered in combined and separate tracks.
            if (config.downmix_mono) {
                DownmixWindowsMicrophoneToMono(session->converted.data(),
                    session->converted.size());
            }
            ApplyWindowsMicrophoneInputGain(session->converted.data(),
                session->converted.size(), config.input_gain);
            const uint32_t converted = static_cast<uint32_t>(
                session->converted.size() / kCanonicalAudioChannels);
            encoder_input_metrics.Observe(session->converted.data(), converted,
                kCanonicalAudioChannels);
            if (!encoder->EncodeSamples(session->converted.data(),
                    static_cast<uint32_t>(converted) * kCanonicalAudioChannels)) {
                SetError("microphone AAC encoding failed");
                return false;
            }
            submitted_frames += converted;
            converted_frame_count.fetch_add(converted, std::memory_order_relaxed);
        }
        // Keep the cursor at the previous committed packet boundary until
        // conversion and encoding both succeed. A failed packet must not make
        // a future packet appear to have already been submitted.
        next_timeline_100ns = timeline.next_timeline_100ns;
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            info.native_input_signal = session->converter.GetInputSignalMetrics();
            info.converter_output_signal = session->converter.GetOutputSignalMetrics();
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            info.max_observed_drift_samples = drift.max_observed_drift_samples();
        }
        return true;
    }

    HRESULT CaptureSession(MicrophoneSession* session) {
        while (!StopRequested()) {
            HANDLE waits[] = {stop_event, session->sample_event};
            const DWORD wait = WaitForMultipleObjects(2, waits, FALSE, kCaptureWaitMs);
            if (wait == WAIT_OBJECT_0) return S_FALSE;
            if (wait == WAIT_TIMEOUT) continue;
            if (wait != WAIT_OBJECT_0 + 1) return E_FAIL;
            UINT32 packet_frames = 0;
            HRESULT hr = session->capture_client->GetNextPacketSize(&packet_frames);
            if (FAILED(hr)) return hr;
            while (packet_frames > 0) {
                BYTE* data = nullptr;
                UINT32 frames = 0;
                DWORD flags = 0;
                UINT64 device_position = 0;
                UINT64 qpc_100ns = 0;
                hr = session->capture_client->GetBuffer(&data, &frames, &flags,
                    &device_position, &qpc_100ns);
                if (FAILED(hr)) return hr;
                packet_count.fetch_add(1, std::memory_order_relaxed);
                if ((flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) != 0) {
                    discontinuity_count.fetch_add(1, std::memory_order_relaxed);
                }
                const auto processing_started = std::chrono::steady_clock::now();
                const bool converted = ConvertAndSubmit(session, data, frames, qpc_100ns,
                    (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0);
                const auto processing_duration = std::chrono::duration_cast<
                    std::chrono::nanoseconds>(
                        std::chrono::steady_clock::now() - processing_started);
                processing_diagnostics.ObservePacket(
                    (flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) != 0,
                    static_cast<uint64_t>(processing_duration.count() / 100));
                const HRESULT release = session->capture_client->ReleaseBuffer(frames);
                if (!converted) return E_FAIL;
                if (FAILED(release)) return release;
                hr = session->capture_client->GetNextPacketSize(&packet_frames);
                if (FAILED(hr)) return hr;
            }
        }
        return S_FALSE;
    }

    void Run() {
        SetState(WindowsMicrophoneRuntimeInfo::State::Starting);
        const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(apartment) && apartment != RPC_E_CHANGED_MODE) {
            SetError("microphone capture could not initialize COM " + HrText(apartment));
            failed.store(true, std::memory_order_release);
            SetState(WindowsMicrophoneRuntimeInfo::State::Failed);
            running.store(false, std::memory_order_release);
            return;
        }
        MicrophoneSession session;
        if (!OpenSession(&session)) {
            failed.store(true, std::memory_order_release);
            SetState(WindowsMicrophoneRuntimeInfo::State::Failed);
        } else {
            SetState(WindowsMicrophoneRuntimeInfo::State::Active);
            const HRESULT capture = CaptureSession(&session);
            if (FAILED(capture) && !StopRequested()) {
                SetError("microphone capture stopped " + HrText(capture)
                    + "; restart capture to use this source again");
                failed.store(true, std::memory_order_release);
                SetState(WindowsMicrophoneRuntimeInfo::State::Failed);
            }
        }
        if (encoder && !encoder->Finalize()) {
            SetError("microphone AAC finalization failed");
            failed.store(true, std::memory_order_release);
            SetState(WindowsMicrophoneRuntimeInfo::State::Failed);
        }
        if (!failed.load(std::memory_order_acquire))
            SetState(WindowsMicrophoneRuntimeInfo::State::Stopped);
        running.store(false, std::memory_order_release);
        if (SUCCEEDED(apartment)) CoUninitialize();
    }
};

WindowsMicrophoneAudioProvider::WindowsMicrophoneAudioProvider(
    WindowsMicrophoneAudioProviderConfig config)
    : impl_(std::make_unique<Impl>(std::move(config))) {}

WindowsMicrophoneAudioProvider::~WindowsMicrophoneAudioProvider() {
    Stop();
}

bool WindowsMicrophoneAudioProvider::Start() {
    if (!impl_ || impl_->running.exchange(true, std::memory_order_acq_rel)) return false;
    if (impl_->thread.joinable()) impl_->thread.join();
    if (impl_->stop_event) {
        CloseHandle(impl_->stop_event);
        impl_->stop_event = nullptr;
    }
    impl_->stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!impl_->stop_event) {
        impl_->SetError("microphone stop event creation failed");
        impl_->running.store(false, std::memory_order_release);
        return false;
    }
    impl_->failed.store(false, std::memory_order_release);
    impl_->synthetic_silence_frame_count.store(0, std::memory_order_relaxed);
    impl_->skipped_input_frame_count.store(0, std::memory_order_relaxed);
    impl_->resampler_correction_count.store(0, std::memory_order_relaxed);
    impl_->normal_jitter_absorbed_count.store(0, std::memory_order_relaxed);
    impl_->hard_discontinuity_recovery_count.store(0, std::memory_order_relaxed);
    impl_->last_swr_compensation_qpc_100ns.store(0, std::memory_order_relaxed);
    impl_->processing_diagnostics.Reset();
    impl_->silence_corrections.Reset();
    impl_->skip_corrections.Reset();
    impl_->resampler_corrections.Reset();
    impl_->correction_events.Reset();
    impl_->encoder_input_metrics.Reset();
    {
        std::lock_guard<std::mutex> lock(impl_->state_mutex);
        impl_->info.native_input_signal = {};
        impl_->info.converter_output_signal = {};
        impl_->info.encoder_input_signal = {};
    }
    impl_->SetState(WindowsMicrophoneRuntimeInfo::State::Starting);
    impl_->thread = std::thread([this] { impl_->Run(); });
    return true;
}

void WindowsMicrophoneAudioProvider::Stop() {
    if (!impl_) return;
    if (impl_->stop_event) SetEvent(impl_->stop_event);
    if (impl_->thread.joinable()) impl_->thread.join();
    if (impl_->stop_event) CloseHandle(impl_->stop_event);
    impl_->stop_event = nullptr;
    if (!impl_->failed.load(std::memory_order_acquire))
        impl_->SetState(WindowsMicrophoneRuntimeInfo::State::Stopped);
    impl_->running.store(false, std::memory_order_release);
}

bool WindowsMicrophoneAudioProvider::IsRunning() const {
    return impl_ && impl_->running.load(std::memory_order_acquire);
}

std::string WindowsMicrophoneAudioProvider::last_error() const {
    if (!impl_) return {};
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    return impl_->error;
}

AudioCorrectionEventSnapshot WindowsMicrophoneAudioProvider::correction_events() const {
    if (!impl_) return {};
    return impl_->correction_events.Snapshot();
}

WindowsMicrophoneRuntimeInfo WindowsMicrophoneAudioProvider::runtime_info() const {
    if (!impl_) return {};
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    WindowsMicrophoneRuntimeInfo info = impl_->info;
    info.configured_input_gain = std::clamp(impl_->config.input_gain, 0.0f, 2.0f);
    info.encoder_input_signal = impl_->encoder_input_metrics.Snapshot();
    info.synthetic_silence_frame_count = impl_->synthetic_silence_frame_count.load(
        std::memory_order_relaxed);
    info.skipped_input_frame_count = impl_->skipped_input_frame_count.load(
        std::memory_order_relaxed);
    info.resampler_correction_count = impl_->resampler_correction_count.load(
        std::memory_order_relaxed);
    info.normal_jitter_absorbed_count = impl_->normal_jitter_absorbed_count.load(
        std::memory_order_relaxed);
    info.hard_discontinuity_recovery_count =
        impl_->hard_discontinuity_recovery_count.load(std::memory_order_relaxed);
    info.silence_corrections = impl_->silence_corrections.Snapshot();
    info.skip_corrections = impl_->skip_corrections.Snapshot();
    info.resampler_corrections = impl_->resampler_corrections.Snapshot();
    info.processing_diagnostics = impl_->processing_diagnostics.Snapshot();
    info.failed = impl_->failed.load(std::memory_order_acquire);
    info.state = info.failed ? WindowsMicrophoneRuntimeInfo::State::Failed : info.state;
    info.packet_count = impl_->packet_count.load(std::memory_order_relaxed);
    info.discontinuity_count = impl_->discontinuity_count.load(
        std::memory_order_relaxed);
    info.first_packet_qpc_100ns = impl_->first_packet_qpc_100ns.load(
        std::memory_order_acquire);
    info.last_packet_qpc_100ns = impl_->last_packet_qpc_100ns.load(
        std::memory_order_acquire);
    info.last_packet_end_qpc_100ns = impl_->last_packet_end_qpc_100ns.load(
        std::memory_order_acquire);
    info.converted_frame_count = impl_->converted_frame_count.load(
        std::memory_order_relaxed);
    info.largest_no_packet_gap_100ns = impl_->largest_no_packet_gap_100ns.load(
        std::memory_order_acquire);
    const bool encoder_ready =
        impl_->timeline_origin_100ns.load(std::memory_order_acquire) != 0;
    if (encoder_ready && impl_->encoder) {
        const auto stats = impl_->encoder->stats();
        info.encoded_packet_count = stats.packets_emitted;
        info.encoder_error_count = stats.encode_errors + stats.flush_errors;
    }
    if (encoder_ready && impl_->ring) {
        const auto stats = impl_->ring->stats();
        info.rejected_packet_count = stats.rejected_empty_packets
            + stats.rejected_regressing_packets;
    }
    return info;
}

std::optional<EncodedAudioTrack> WindowsMicrophoneAudioProvider::TakeTrackForInterval(
    double presentation_start_qpc_s, double presentation_end_qpc_s,
    AudioSourceMetadata source) const {
    if (!impl_ || presentation_end_qpc_s <= presentation_start_qpc_s)
        return std::nullopt;
    const uint64_t origin = impl_->replay_origin_100ns.load(std::memory_order_acquire);
    const uint64_t last = impl_->last_packet_qpc_100ns.load(std::memory_order_acquire);
    if (origin == 0 || !impl_->ring || last < origin) return std::nullopt;
    const auto range = MapAudioSourcePresentationRange(
        presentation_start_qpc_s, presentation_end_qpc_s, origin, kCanonicalAudioSampleRate);
    EncodedAudioSnapshot snapshot = impl_->ring->TakeSnapshot(
        range.start_pts_samples, range.end_pts_samples);
    if (!snapshot.valid()) return std::nullopt;
    source.format = snapshot.format;
    source.state.admitted = true;
    source.state.active_in_generation = !impl_->failed.load(std::memory_order_acquire);
    source.state.health = impl_->failed.load(std::memory_order_acquire)
        ? AudioSourceHealth::Failed : AudioSourceHealth::Active;
    // Manifest timing is clip-local packet history, not the lifetime of the
    // endpoint. In particular, a microphone active before the requested
    // replay interval must not be reported as if it began before this clip.
    source.state.first_active_100ns = AudioSamplePositionToTimeline100ns(
        origin, snapshot.first_pts_samples, kCanonicalAudioSampleRate);
    source.state.last_active_100ns = AudioSamplePositionToTimeline100ns(
        origin, snapshot.last_pts_samples, kCanonicalAudioSampleRate);
    EncodedAudioTrack track;
    track.source = std::move(source);
    track.snapshot = std::move(snapshot);
    track.presentation_start_pts_samples = range.start_pts_samples;
    return track;
}

}  // namespace fthr
