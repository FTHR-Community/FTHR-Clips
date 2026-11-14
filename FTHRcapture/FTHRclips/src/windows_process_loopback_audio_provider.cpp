// Windows 11 process-loopback application-stem implementation for AUDIT-050.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <ksmedia.h>
#include <mmdeviceapi.h>
#include <propvarutil.h>
#include <wrl/client.h>

#include "windows_process_loopback_audio_provider.h"

#include "audio_encoder.h"
#include "clip_audio_manifest.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <map>
#include <mutex>
#include <limits>
#include <thread>
#include <utility>

extern "C" {
#include <libavutil/channel_layout.h>
#include <libavutil/mathematics.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
}

#pragma comment(lib, "Ole32.lib")

using Microsoft::WRL::ComPtr;

namespace fthr {

namespace {

constexpr uint32_t kCanonicalSampleRate = kCanonicalAudioSampleRate;
constexpr uint32_t kCanonicalChannels = kCanonicalAudioChannels;
constexpr DWORD kActivationTimeoutMs = 5000;
constexpr DWORD kCaptureWaitMs = 250;
constexpr uint64_t kCaptureWait100ns = kCaptureWaitMs * 10'000ULL;
constexpr uint32_t kMaxRecoveryAttempts = 3;

int64_t CurrentQpc100ns() {
    // WASAPI packet QPC values and all native audio timelines use the same
    // 100-ns domain. Reuse the shared conversion, which divides before the
    // remainder multiplication and therefore cannot overflow on a normal
    // long-running QPC counter.
    return static_cast<int64_t>(CurrentAudioTimeline100ns());
}

bool WaitForStop(HANDLE stop_event, DWORD timeout_ms) {
    return WaitForSingleObject(stop_event, timeout_ms) == WAIT_OBJECT_0;
}

bool IsRecoverableAudioFailure(HRESULT hr) {
    return hr == AUDCLNT_E_DEVICE_INVALIDATED
        || hr == AUDCLNT_E_SERVICE_NOT_RUNNING
        || hr == AUDCLNT_E_RESOURCES_INVALIDATED;
}

std::string HrText(HRESULT hr) {
    char text[32] = {};
    std::snprintf(text, sizeof(text), "0x%08lX", static_cast<unsigned long>(hr));
    return text;
}

std::optional<AVSampleFormat> WaveSampleFormat(const WAVEFORMATEX* format) {
    if (!format || format->nChannels == 0 || format->nSamplesPerSec == 0) return std::nullopt;
    WORD tag = format->wFormatTag;
    GUID sub_format{};
    if (tag == WAVE_FORMAT_EXTENSIBLE) {
        if (format->cbSize < sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX))
            return std::nullopt;
        const auto* extensible = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(format);
        sub_format = extensible->SubFormat;
        if (sub_format == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT) tag = WAVE_FORMAT_IEEE_FLOAT;
        else if (sub_format == KSDATAFORMAT_SUBTYPE_PCM) tag = WAVE_FORMAT_PCM;
        else return std::nullopt;
    }
    if (tag == WAVE_FORMAT_IEEE_FLOAT && format->wBitsPerSample == 32)
        return AV_SAMPLE_FMT_FLT;
    if (tag == WAVE_FORMAT_PCM && format->wBitsPerSample == 16)
        return AV_SAMPLE_FMT_S16;
    if (tag == WAVE_FORMAT_PCM && format->wBitsPerSample == 32)
        return AV_SAMPLE_FMT_S32;
    // Packed 24-bit PCM needs a dedicated unpacking path.  Fail the source
    // explicitly instead of reinterpreting bytes as another sample type.
    return std::nullopt;
}

float CalculateRms(const float* samples, uint32_t frames) {
    if (!samples || frames == 0) return 0.0f;
    const size_t count = static_cast<size_t>(frames) * kCanonicalChannels;
    double sum = 0.0;
    for (size_t index = 0; index < count; ++index) {
        const double sample = samples[index];
        sum += sample * sample;
    }
    return static_cast<float>(std::sqrt(sum / static_cast<double>(count)));
}

class ActivationHandler final : public IActivateAudioInterfaceCompletionHandler,
                                public IAgileObject {
public:
    ActivationHandler() : event_(CreateEventW(nullptr, TRUE, FALSE, nullptr)) {}
    ~ActivationHandler() {
        if (event_) CloseHandle(event_);
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == IID_IUnknown || iid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *object = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == __uuidof(IAgileObject)) {
            *object = static_cast<IAgileObject*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++references_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG remaining = --references_;
        if (remaining == 0) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE ActivateCompleted(
        IActivateAudioInterfaceAsyncOperation* operation) override {
        if (operation) operation->GetActivateResult(&result_, &activated_);
        else result_ = E_POINTER;
        if (event_) SetEvent(event_);
        return S_OK;
    }

    HANDLE event() const { return event_; }
    HRESULT result() const { return result_; }
    ComPtr<IUnknown> activated() const { return activated_; }

private:
    std::atomic<ULONG> references_{1};
    HANDLE event_ = nullptr;
    HRESULT result_ = E_PENDING;
    ComPtr<IUnknown> activated_;
};

struct ProcessLoopbackSession {
    ComPtr<IAudioClient> audio_client;
    ComPtr<IAudioCaptureClient> capture_client;
    WAVEFORMATEX* format = nullptr;
    HANDLE sample_event = nullptr;
    SwrContext* resampler = nullptr;
    AVSampleFormat input_format = AV_SAMPLE_FMT_NONE;
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
        swr_free(&resampler);
        converted.clear();
        silent_input.clear();
    }
    ~ProcessLoopbackSession() { Close(); }
};

}  // namespace

struct WindowsProcessLoopbackAudioProvider::Impl {
    explicit Impl(WindowsProcessLoopbackProviderConfig initial,
                  ActivityCallback activity, EndedCallback ended, FailureCallback failure)
        : config(std::move(initial)), on_activity(std::move(activity)),
          on_ended(std::move(ended)), on_failure(std::move(failure)) {}

    WindowsProcessLoopbackProviderConfig config;
    ActivityCallback on_activity;
    EndedCallback on_ended;
    FailureCallback on_failure;
    std::thread thread;
    HANDLE stop_event = nullptr;
    std::atomic<bool> running{false};
    std::atomic<bool> failed{false};
    std::atomic<uint64_t> timeline_origin_100ns{0};
    std::atomic<uint64_t> replay_origin_100ns{0};
    mutable std::mutex state_mutex;
    std::string error;
    std::unique_ptr<AudioEncoder> encoder;
    std::unique_ptr<EncodedAudioPacketRing> ring;
    std::atomic<bool> admitted{false};
    std::atomic<uint64_t> input_frames{0};
    std::atomic<uint64_t> packet_count{0};
    std::atomic<uint64_t> converted_frames{0};
    std::atomic<uint64_t> submitted_frames{0};
    std::atomic<uint64_t> encoded_packets{0};
    std::atomic<uint64_t> encode_failures{0};
    std::atomic<uint64_t> finalize_failures{0};
    std::atomic<uint64_t> dropped_blocks{0};
    std::atomic<uint64_t> no_packet_intervals{0};
    std::atomic<uint64_t> largest_no_packet_gap_100ns{0};
    std::atomic<uint64_t> last_packet_qpc_100ns{0};
    std::atomic<uint64_t> last_packet_end_qpc_100ns{0};
    std::atomic<uint64_t> discontinuity_count{0};
    std::atomic<uint64_t> rejected_packets{0};
    std::atomic<uint64_t> restart_count{0};
    std::atomic<int64_t> first_input_qpc_100ns{0};
    std::atomic<int64_t> last_input_qpc_100ns{0};
    std::atomic<int64_t> last_submission_qpc_100ns{0};
    std::atomic<int64_t> last_encoded_qpc_100ns{0};
    std::atomic<int64_t> largest_gap_100ns{0};
    std::atomic<bool> ended_notified{false};
    std::atomic<bool> failure_notified{false};
    uint64_t previous_input_qpc_100ns = 0;
    uint64_t next_timeline_100ns = 0;
    std::array<float, 4800 * kCanonicalChannels> silence{};

    struct PendingBlock {
        std::vector<float> samples;
        uint32_t frames = 0;
        uint64_t qpc_100ns = 0;
    };
    std::deque<PendingBlock> pending;

    void SetError(std::string value) {
        std::lock_guard<std::mutex> lock(state_mutex);
        error = std::move(value);
    }

    void ReportFailure(std::string value) {
        SetError(std::move(value));
        failed.store(true, std::memory_order_release);
        if (!failure_notified.exchange(true, std::memory_order_acq_rel)
                && on_failure) {
            on_failure(config.source.identity.id);
        }
    }

    bool OpenSession(ProcessLoopbackSession* session) {
        if (!session || config.target_process_id == 0) {
            SetError("process-loopback source has no target process");
            return false;
        }
        AUDIOCLIENT_ACTIVATION_PARAMS activation{};
        activation.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
        activation.ProcessLoopbackParams.TargetProcessId = config.target_process_id;
        activation.ProcessLoopbackParams.ProcessLoopbackMode = config.include_process_tree
            ? PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            : PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;

        PROPVARIANT params;
        PropVariantInit(&params);
        params.vt = VT_BLOB;
        params.blob.cbSize = sizeof(activation);
        params.blob.pBlobData = static_cast<BYTE*>(CoTaskMemAlloc(sizeof(activation)));
        if (!params.blob.pBlobData) {
            SetError("process-loopback activation parameter allocation failed");
            return false;
        }
        std::memcpy(params.blob.pBlobData, &activation, sizeof(activation));

        auto* handler = new ActivationHandler();
        if (!handler || !handler->event()) {
            if (handler) handler->Release();
            PropVariantClear(&params);
            SetError("process-loopback activation event creation failed");
            return false;
        }
        ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
        HRESULT hr = ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient),
            &params, handler, &operation);
        PropVariantClear(&params);
        if (FAILED(hr)) {
            handler->Release();
            SetError("ActivateAudioInterfaceAsync failed " + HrText(hr));
            return false;
        }
        HANDLE wait_handles[] = {stop_event, handler->event()};
        const DWORD wait = WaitForMultipleObjects(2, wait_handles, FALSE, kActivationTimeoutMs);
        if (wait != WAIT_OBJECT_0 + 1) {
            handler->Release();
            SetError(wait == WAIT_TIMEOUT ? "process-loopback activation timed out"
                                           : "process-loopback activation stopped");
            return false;
        }
        hr = handler->result();
        ComPtr<IUnknown> activated = handler->activated();
        handler->Release();
        if (FAILED(hr) || !activated || FAILED(activated.As(&session->audio_client))) {
            SetError("process-loopback activation did not return IAudioClient "
                + HrText(FAILED(hr) ? hr : E_NOINTERFACE));
            return false;
        }
        // The process-loopback virtual device is endpoint-independent and can
        // return E_NOTIMPL from GetMixFormat on current Windows 11 builds. Ask
        // the shared audio engine for our canonical format explicitly and let
        // AUTOCONVERTPCM handle whichever physical endpoints the app uses.
        session->format = static_cast<WAVEFORMATEX*>(
            CoTaskMemAlloc(sizeof(WAVEFORMATEX)));
        if (!session->format) {
            SetError("process-loopback format allocation failed");
            return false;
        }
        *session->format = {};
        session->format->wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
        session->format->nChannels = static_cast<WORD>(kCanonicalChannels);
        session->format->nSamplesPerSec = kCanonicalSampleRate;
        session->format->wBitsPerSample = 32;
        session->format->nBlockAlign = static_cast<WORD>(
            session->format->nChannels * session->format->wBitsPerSample / 8);
        session->format->nAvgBytesPerSec =
            session->format->nSamplesPerSec * session->format->nBlockAlign;
        const auto input_format = WaveSampleFormat(session->format);
        if (!input_format) {
            SetError("process-loopback source format is unsupported; source is not captured");
            return false;
        }
        session->input_format = *input_format;

        AVChannelLayout input_layout{};
        AVChannelLayout output_layout{};
        av_channel_layout_default(&input_layout, session->format->nChannels);
        av_channel_layout_default(&output_layout, kCanonicalChannels);
        const int swr_result = swr_alloc_set_opts2(&session->resampler,
            &output_layout, AV_SAMPLE_FMT_FLT, kCanonicalSampleRate,
            &input_layout, session->input_format,
            static_cast<int>(session->format->nSamplesPerSec), 0, nullptr);
        av_channel_layout_uninit(&input_layout);
        av_channel_layout_uninit(&output_layout);
        if (swr_result < 0 || !session->resampler || swr_init(session->resampler) < 0) {
            SetError("process-loopback canonical resampler initialization failed");
            return false;
        }

        session->sample_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!session->sample_event) {
            SetError("process-loopback sample event creation failed");
            return false;
        }
        constexpr DWORD flags = AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
        hr = session->audio_client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 0, 0,
            session->format, nullptr);
        if (SUCCEEDED(hr)) hr = session->audio_client->SetEventHandle(session->sample_event);
        if (SUCCEEDED(hr)) hr = session->audio_client->GetService(IID_PPV_ARGS(&session->capture_client));
        if (FAILED(hr)) {
            SetError("process-loopback WASAPI initialization failed " + HrText(hr));
            return false;
        }
        hr = session->audio_client->Start();
        if (FAILED(hr)) {
            SetError("process-loopback IAudioClient::Start failed " + HrText(hr));
            return false;
        }
        return true;
    }

    bool EnsureEncoder(uint64_t first_qpc_100ns) {
        if (admitted.load(std::memory_order_acquire) || pending.empty()) return true;
        // Pending activity-gate pre-roll belongs to the source too. Starting
        // at the admission packet discarded the beginning of the first sound.
        const uint64_t origin = pending.front().qpc_100ns > 0
            ? pending.front().qpc_100ns : first_qpc_100ns;
        if (origin == 0) {
            ReportFailure("process-loopback source received no usable capture timestamp");
            return false;
        }
        auto new_ring = std::make_unique<EncodedAudioPacketRing>(
            config.source.identity.id, config.generation,
            AudioSourceFormat{kCanonicalSampleRate, kCanonicalChannels, "flt", "pcm_f32le"},
            config.retention_seconds, config.replay_storage);
        auto new_encoder = std::make_unique<AudioEncoder>();
        const auto replay_origin = config.replay_pause_clock
            ? config.replay_pause_clock->Map(origin) : origin;
        if (!new_encoder->Initialize(kCanonicalSampleRate, kCanonicalChannels,
                config.bitrate_kbps,
                [this, ring = new_ring.get(), clock = config.replay_pause_clock,
                 origin, replay_origin](const uint8_t* data, uint32_t size, int64_t pts) {
                    const int64_t duration = size / (kCanonicalChannels * sizeof(float));
                    if (clock) {
                        const auto mapped = clock->MapAudio(
                            origin, replay_origin, pts, duration, kCanonicalSampleRate);
                        if (!mapped) return;
                        pts = *mapped;
                    }
                    // Continuous recordings own their disk queue and must not
                    // lose samples when the replay spool exhausts its budget.
                    if (config.packet_sink)
                        config.packet_sink(config.source, data, size,
                            AudioSamplePositionToTimeline100ns(replay_origin, pts, kCanonicalSampleRate), duration);
                    if (!ring->Push({std::vector<uint8_t>(data, data + size), pts, duration})) {
                        rejected_packets.fetch_add(1, std::memory_order_relaxed);
                        ReportFailure("Lossless audio spool failed or exceeded its memory/disk budget");
                        return;
                    }
                    encoded_packets.fetch_add(1, std::memory_order_relaxed);
                    last_encoded_qpc_100ns.store(
                        last_submission_qpc_100ns.load(std::memory_order_relaxed),
                        std::memory_order_relaxed);
                }, 0, true)) {
            ReportFailure("process-loopback AAC encoder initialization failed");
            pending.clear();
            return false;
        }
        new_ring->SetCodecExtradata(new_encoder->GetExtradata());
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            ring = std::move(new_ring);
            encoder = std::move(new_encoder);
        }
        // Publish readiness only after both owned objects are installed. Save
        // and diagnostic readers acquire the origin before touching either
        // pointer, so they cannot observe a half-initialized provider.
        timeline_origin_100ns.store(origin, std::memory_order_release);
        replay_origin_100ns.store(replay_origin, std::memory_order_release);
        admitted.store(true, std::memory_order_release);
        next_timeline_100ns = origin;
        for (const auto& block : pending) {
            if (!EncodeAligned(block.samples.data(), block.frames, block.qpc_100ns)) {
                pending.clear();
                return false;
            }
        }
        pending.clear();
        return true;
    }

    uint64_t AdvanceTimeline(uint64_t timestamp_100ns, uint64_t frames) const {
        return timestamp_100ns + (frames * 10'000'000ULL) / kCanonicalSampleRate;
    }

    bool EncodeSilence(uint64_t frames) {
        if (!encoder || frames == 0) return true;
        constexpr uint32_t chunk_frames = 4800;
        while (frames > 0) {
            const uint32_t chunk = static_cast<uint32_t>(
                std::min<uint64_t>(frames, chunk_frames));
            if (!encoder->EncodeSamples(silence.data(), chunk * kCanonicalChannels)) {
                encode_failures.fetch_add(1, std::memory_order_relaxed);
                ReportFailure("process-loopback AAC silence encoding failed");
                return false;
            }
            next_timeline_100ns = AdvanceTimeline(next_timeline_100ns, chunk);
            frames -= chunk;
        }
        return true;
    }

    bool FillSilenceUntil(uint64_t timestamp_100ns) {
        if (!admitted.load(std::memory_order_acquire) || !encoder || next_timeline_100ns == 0
                || timestamp_100ns <= next_timeline_100ns) {
            return true;
        }
        const auto timeline = ReconcileAudioTimelinePacket(
            next_timeline_100ns, timestamp_100ns, 0,
            kCanonicalSampleRate, kCanonicalSampleRate, 5);
        if (timeline.gap_100ns > 0) {
            uint64_t largest = largest_no_packet_gap_100ns.load(
                std::memory_order_relaxed);
            while (timeline.gap_100ns > largest
                    && !largest_no_packet_gap_100ns.compare_exchange_weak(
                        largest, timeline.gap_100ns, std::memory_order_release,
                        std::memory_order_relaxed)) {}
        }
        if (timeline.large_gap) {
            discontinuity_count.fetch_add(1, std::memory_order_relaxed);
            ReportFailure("process-loopback audio timeline discontinuity exceeds 5 seconds");
            return false;
        }
        if (!EncodeSilence(timeline.silence_frames)) return false;
        next_timeline_100ns = timeline.next_timeline_100ns;
        return true;
    }

    bool EncodeAligned(const float* samples, uint32_t frames, uint64_t qpc_100ns) {
        if (!encoder || !samples || frames == 0) return true;
        if (qpc_100ns == 0) qpc_100ns = static_cast<uint64_t>(CurrentQpc100ns());
        const auto timeline = ReconcileAudioTimelinePacket(
            next_timeline_100ns, qpc_100ns, frames,
            kCanonicalSampleRate, kCanonicalSampleRate, 5);
        if (timeline.gap_100ns > 0) {
            uint64_t largest = largest_no_packet_gap_100ns.load(
                std::memory_order_relaxed);
            while (timeline.gap_100ns > largest
                    && !largest_no_packet_gap_100ns.compare_exchange_weak(
                        largest, timeline.gap_100ns, std::memory_order_release,
                        std::memory_order_relaxed)) {}
        }
        if (timeline.large_gap) {
            discontinuity_count.fetch_add(1, std::memory_order_relaxed);
            ReportFailure("process-loopback audio timeline discontinuity exceeds 5 seconds");
            return false;
        }
        if (!EncodeSilence(timeline.silence_frames)) return false;
        const uint32_t skip_frames = timeline.skip_input_frames;
        const uint32_t submit_frames = frames - skip_frames;
        if (submit_frames == 0) {
            dropped_blocks.fetch_add(1, std::memory_order_relaxed);
            return true;
        }
        const uint64_t submit_qpc = AdvanceTimeline(qpc_100ns, skip_frames);
        submitted_frames.fetch_add(submit_frames, std::memory_order_relaxed);
        last_submission_qpc_100ns.store(static_cast<int64_t>(submit_qpc),
            std::memory_order_relaxed);
        if (!encoder->EncodeSamples(
            samples + static_cast<size_t>(skip_frames) * kCanonicalChannels,
            submit_frames * kCanonicalChannels)) {
            encode_failures.fetch_add(1, std::memory_order_relaxed);
            ReportFailure("process-loopback AAC encoding failed");
            return false;
        }
        next_timeline_100ns = std::max(
            timeline.next_timeline_100ns, AdvanceTimeline(submit_qpc, submit_frames));
        return true;
    }

    bool SubmitNormalized(const float* samples, uint32_t frames, uint64_t qpc_100ns) {
        if (!samples || frames == 0) return true;
        converted_frames.fetch_add(frames, std::memory_order_relaxed);
        const uint64_t previous = previous_input_qpc_100ns;
        if (previous > 0 && qpc_100ns > previous) {
            const int64_t gap = static_cast<int64_t>(qpc_100ns - previous);
            int64_t largest = largest_gap_100ns.load(std::memory_order_relaxed);
            while (gap > largest && !largest_gap_100ns.compare_exchange_weak(
                       largest, gap, std::memory_order_relaxed)) {}
        } else if (previous > 0 && qpc_100ns < previous) {
            discontinuity_count.fetch_add(1, std::memory_order_relaxed);
        }
        previous_input_qpc_100ns = qpc_100ns;
        if (first_input_qpc_100ns.load(std::memory_order_relaxed) == 0)
            first_input_qpc_100ns.store(static_cast<int64_t>(qpc_100ns),
                std::memory_order_relaxed);
        last_input_qpc_100ns.store(static_cast<int64_t>(qpc_100ns),
            std::memory_order_relaxed);
        const float rms = CalculateRms(samples, frames);
        const auto admission = on_activity
            ? on_activity(config.source.identity.id, rms, static_cast<int64_t>(qpc_100ns))
            : AudioSourceAdmission::RejectedInvalidIdentity;
        if (!admitted.load(std::memory_order_acquire)) {
            PendingBlock pending_block;
            pending_block.samples.assign(samples,
                samples + static_cast<size_t>(frames) * kCanonicalChannels);
            pending_block.frames = frames;
            pending_block.qpc_100ns = qpc_100ns;
            pending.push_back(std::move(pending_block));
            while (pending.size() > 2) {
                pending.pop_front();
                dropped_blocks.fetch_add(1, std::memory_order_relaxed);
            }
            if (admission == AudioSourceAdmission::Accepted)
                return EnsureEncoder(qpc_100ns);
            return true;
        }
        return EncodeAligned(samples, frames, qpc_100ns);
    }

    bool ConvertAndSubmit(ProcessLoopbackSession* session, const BYTE* data,
                          uint32_t source_frames, uint64_t qpc_100ns, bool silent) {
        if (!session || source_frames == 0) return true;
        const int64_t capacity_64 = av_rescale_rnd(
            swr_get_delay(session->resampler, session->format->nSamplesPerSec) + source_frames,
            kCanonicalSampleRate, session->format->nSamplesPerSec, AV_ROUND_UP);
        if (capacity_64 <= 0 || capacity_64 > static_cast<int64_t>(std::numeric_limits<int>::max())) {
            SetError("process-loopback resampler returned an invalid output capacity");
            return false;
        }
        const int capacity = static_cast<int>(capacity_64);
        session->converted.resize(static_cast<size_t>(capacity) * kCanonicalChannels);
        const uint8_t* input = data;
        if (silent || !input) {
            const size_t input_bytes = static_cast<size_t>(source_frames) * session->format->nBlockAlign;
            session->silent_input.assign(input_bytes, 0);
            input = session->silent_input.data();
        }
        const uint8_t* input_planes[] = {input};
        uint8_t* output_planes[] = {reinterpret_cast<uint8_t*>(session->converted.data())};
        const int converted = swr_convert(session->resampler, output_planes, capacity,
            input_planes, static_cast<int>(source_frames));
        if (converted < 0) {
            dropped_blocks.fetch_add(1, std::memory_order_relaxed);
            SetError("process-loopback resampling failed");
            return false;
        }
        return SubmitNormalized(session->converted.data(), static_cast<uint32_t>(converted), qpc_100ns);
    }

    HRESULT CaptureSession(ProcessLoopbackSession* session) {
        bool no_packet_interval = false;
        while (!WaitForStop(stop_event, 0)) {
            HANDLE wait_handles[] = {stop_event, session->sample_event};
            const DWORD wait = WaitForMultipleObjects(2, wait_handles, FALSE, kCaptureWaitMs);
            if (wait == WAIT_OBJECT_0) return S_FALSE;
            if (wait == WAIT_TIMEOUT) {
                // Count a contiguous no-packet interval once. The duration is
                // measured from the previous packet end when the next packet
                // arrives, rather than treating every event wake as a gap.
                if (!no_packet_interval) {
                    no_packet_intervals.fetch_add(1, std::memory_order_relaxed);
                    no_packet_interval = true;
                }
                // Packet timestamps describe when audio was captured, before
                // WASAPI publishes it. Keep one wait interval available for
                // delayed packets when extending a silent source.
                const uint64_t now = static_cast<uint64_t>(CurrentQpc100ns());
                if (now > kCaptureWait100ns
                        && !FillSilenceUntil(now - kCaptureWait100ns))
                    return E_FAIL;
                continue;
            }
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
                if (frames > 0) {
                    const uint64_t packet_qpc = qpc_100ns != 0
                        ? qpc_100ns : static_cast<uint64_t>(CurrentQpc100ns());
                    packet_count.fetch_add(1, std::memory_order_relaxed);
                    input_frames.fetch_add(frames, std::memory_order_relaxed);
                    if ((flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) != 0)
                        discontinuity_count.fetch_add(1, std::memory_order_relaxed);
                    const uint64_t previous_packet_end =
                        last_packet_end_qpc_100ns.load(std::memory_order_relaxed);
                    if (previous_packet_end > 0 && packet_qpc > previous_packet_end) {
                        const uint64_t gap = packet_qpc - previous_packet_end;
                        if (!no_packet_interval)
                            no_packet_intervals.fetch_add(1, std::memory_order_relaxed);
                        no_packet_interval = false;
                        uint64_t largest = largest_no_packet_gap_100ns.load(
                            std::memory_order_relaxed);
                        while (gap > largest && !largest_no_packet_gap_100ns.compare_exchange_weak(
                            largest, gap, std::memory_order_release,
                            std::memory_order_relaxed)) {}
                    } else {
                        no_packet_interval = false;
                    }
                    int64_t first = 0;
                    first_input_qpc_100ns.compare_exchange_strong(
                        first,
                        static_cast<int64_t>(packet_qpc),
                        std::memory_order_release,
                        std::memory_order_relaxed);
                    last_packet_qpc_100ns.store(packet_qpc, std::memory_order_release);
                    const uint64_t packet_duration = session->format->nSamplesPerSec > 0
                        ? (static_cast<uint64_t>(frames) * 10'000'000ULL)
                            / session->format->nSamplesPerSec : 0;
                    last_packet_end_qpc_100ns.store(
                        packet_qpc + packet_duration, std::memory_order_release);
                    qpc_100ns = packet_qpc;
                }
                const bool converted = ConvertAndSubmit(session, data, frames, qpc_100ns,
                    (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0);
                const HRESULT release_hr = session->capture_client->ReleaseBuffer(frames);
                if (!converted) return E_FAIL;
                if (FAILED(release_hr)) return release_hr;
                hr = session->capture_client->GetNextPacketSize(&packet_frames);
                if (FAILED(hr)) return hr;
            }
            // Draining the queue is normal between events. The next packet's
            // timestamp determines whether there is a gap to fill.
        }
        return S_FALSE;
    }

    void Run() {
        const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(apartment) && apartment != RPC_E_CHANGED_MODE) {
            SetError("process-loopback COM initialization failed " + HrText(apartment));
            failed.store(true, std::memory_order_release);
            if (on_failure) on_failure(config.source.identity.id);
            running.store(false, std::memory_order_release);
            return;
        }

        uint32_t recovery_attempts = 0;
        bool opened_once = false;
        while (!WaitForStop(stop_event, 0)) {
            ProcessLoopbackSession session;
            if (!OpenSession(&session)) {
                if (WaitForStop(stop_event, 0)) break;
                if (++recovery_attempts <= kMaxRecoveryAttempts) {
                    const DWORD delay = 200u * recovery_attempts;
                    if (!WaitForStop(stop_event, delay)) continue;
                    break;
                }
                failed.store(true, std::memory_order_release);
                if (on_failure) on_failure(config.source.identity.id);
                break;
            }
            if (opened_once) restart_count.fetch_add(1, std::memory_order_relaxed);
            opened_once = true;
            recovery_attempts = 0;
            const HRESULT capture_result = CaptureSession(&session);
            if (capture_result == S_FALSE || WaitForStop(stop_event, 0)) break;
            // Encoding/timeline failures already set the terminal state and
            // reported their precise cause. Do not overwrite that evidence
            // with a generic E_FAIL or retry an unsafe timeline.
            if (failed.load(std::memory_order_acquire)) break;
            SetError("process-loopback capture failed " + HrText(capture_result));
            if (!IsRecoverableAudioFailure(capture_result)
                    || ++recovery_attempts > kMaxRecoveryAttempts) {
                failed.store(true, std::memory_order_release);
                if (on_failure) on_failure(config.source.identity.id);
                break;
            }
            if (WaitForStop(stop_event, 200u * recovery_attempts)) break;
        }
        if (encoder && !encoder->Finalize()) {
            finalize_failures.fetch_add(1, std::memory_order_relaxed);
            ReportFailure("process-loopback AAC finalization failed");
        }
        running.store(false, std::memory_order_release);
        if (!WaitForStop(stop_event, 0) && on_ended
                && !ended_notified.exchange(true, std::memory_order_acq_rel)) {
            on_ended(config.source.identity.id, CurrentQpc100ns());
        }
        if (SUCCEEDED(apartment)) CoUninitialize();
    }
};

WindowsProcessLoopbackAudioProvider::WindowsProcessLoopbackAudioProvider(
    WindowsProcessLoopbackProviderConfig config, ActivityCallback activity_callback,
    EndedCallback ended_callback, FailureCallback failure_callback)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(activity_callback),
          std::move(ended_callback), std::move(failure_callback))) {}

WindowsProcessLoopbackAudioProvider::~WindowsProcessLoopbackAudioProvider() {
    Stop();
}

bool WindowsProcessLoopbackAudioProvider::Start() {
    if (!impl_ || impl_->running.exchange(true, std::memory_order_acq_rel)) return false;
    impl_->stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!impl_->stop_event) {
        impl_->SetError("process-loopback stop event creation failed");
        impl_->failed.store(true, std::memory_order_release);
        impl_->running.store(false, std::memory_order_release);
        return false;
    }
    impl_->failed.store(false, std::memory_order_release);
    impl_->ended_notified.store(false, std::memory_order_release);
    impl_->failure_notified.store(false, std::memory_order_release);
    impl_->thread = std::thread([this] { impl_->Run(); });
    return true;
}

void WindowsProcessLoopbackAudioProvider::Stop() {
    if (!impl_) return;
    if (impl_->stop_event) SetEvent(impl_->stop_event);
    if (impl_->thread.joinable()) impl_->thread.join();
    if (impl_->stop_event) CloseHandle(impl_->stop_event);
    impl_->stop_event = nullptr;
    impl_->running.store(false, std::memory_order_release);
}

bool WindowsProcessLoopbackAudioProvider::IsRunning() const {
    return impl_ && impl_->running.load(std::memory_order_acquire);
}

bool WindowsProcessLoopbackAudioProvider::HasCapturedAudio() const {
    return impl_ && impl_->admitted.load(std::memory_order_acquire);
}

std::string WindowsProcessLoopbackAudioProvider::last_error() const {
    if (!impl_) return {};
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    return impl_->error;
}

WindowsProcessLoopbackRuntimeInfo
WindowsProcessLoopbackAudioProvider::runtime_info() const {
    WindowsProcessLoopbackRuntimeInfo result;
    if (!impl_) return result;
    result.admitted = impl_->admitted.load(std::memory_order_acquire);
    if (impl_->running.load(std::memory_order_acquire))
        result.state = result.admitted ? "ACTIVE" : "STARTING";
    else if (impl_->failed.load(std::memory_order_acquire))
        result.state = "FAILED";
    else
        result.state = "STOPPED";
    result.input_frames = impl_->input_frames.load(std::memory_order_relaxed);
    result.packet_count = impl_->packet_count.load(std::memory_order_relaxed);
    result.converted_frames = impl_->converted_frames.load(std::memory_order_relaxed);
    result.submitted_frames = impl_->submitted_frames.load(std::memory_order_relaxed);
    result.encoded_packets = impl_->encoded_packets.load(std::memory_order_relaxed);
    result.encode_failures = impl_->encode_failures.load(std::memory_order_relaxed);
    result.finalize_failures = impl_->finalize_failures.load(std::memory_order_relaxed);
    result.dropped_blocks = impl_->dropped_blocks.load(std::memory_order_relaxed);
    result.no_packet_intervals = impl_->no_packet_intervals.load(std::memory_order_relaxed);
    result.largest_no_packet_gap_100ns = impl_->largest_no_packet_gap_100ns.load(
        std::memory_order_relaxed);
    result.discontinuity_count = impl_->discontinuity_count.load(
        std::memory_order_relaxed);
    result.rejected_packets = impl_->rejected_packets.load(std::memory_order_relaxed);
    result.restart_count = impl_->restart_count.load(std::memory_order_relaxed);
    result.first_input_qpc_100ns = impl_->first_input_qpc_100ns.load(std::memory_order_relaxed);
    result.last_input_qpc_100ns = impl_->last_input_qpc_100ns.load(std::memory_order_relaxed);
    result.last_submission_qpc_100ns = impl_->last_submission_qpc_100ns.load(std::memory_order_relaxed);
    result.last_encoded_qpc_100ns = impl_->last_encoded_qpc_100ns.load(std::memory_order_relaxed);
    result.largest_gap_100ns = impl_->largest_gap_100ns.load(std::memory_order_relaxed);
    return result;
}

std::optional<EncodedAudioTrack> WindowsProcessLoopbackAudioProvider::TakeTrackForInterval(
    double presentation_start_qpc_s, double presentation_end_qpc_s,
    const AudioSourceMetadata& source) const {
    if (!impl_ || presentation_end_qpc_s <= presentation_start_qpc_s) return std::nullopt;
    const uint64_t origin = impl_->replay_origin_100ns.load(std::memory_order_acquire);
    if (origin == 0) return std::nullopt;
    const auto range = MapAudioSourcePresentationRange(
        presentation_start_qpc_s, presentation_end_qpc_s, origin, kCanonicalSampleRate);
    EncodedAudioSnapshot snapshot;
    {
        std::lock_guard<std::mutex> lock(impl_->state_mutex);
        if (!impl_->ring) return std::nullopt;
        snapshot = impl_->ring->TakeSnapshot(
            range.start_pts_samples, range.end_pts_samples);
    }
    if (!snapshot.valid()) return std::nullopt;
    EncodedAudioTrack track;
    track.source = source;
    track.source.format = snapshot.format;
    track.source.state.first_active_100ns = AudioSamplePositionToTimeline100ns(
        origin, snapshot.first_pts_samples, kCanonicalSampleRate);
    track.source.state.last_active_100ns = AudioSamplePositionToTimeline100ns(
        origin, snapshot.last_pts_samples, kCanonicalSampleRate);
    track.snapshot = std::move(snapshot);
    track.presentation_start_pts_samples = range.start_pts_samples;
    return track;
}

WindowsApplicationSourceCoordinator::WindowsApplicationSourceCoordinator(
    uint64_t generation, WindowsProcessLoopbackCapability capability,
    SourceIdGenerator source_id_generator, uint32_t source_limit,
    uint32_t retention_seconds)
    : generation_(generation), capability_(capability),
      source_id_generator_(std::move(source_id_generator)),
      registry_(generation, source_limit),
      retention_100ns_(static_cast<int64_t>(std::max<uint32_t>(1, retention_seconds))
          * 10'000'000LL) {}

std::optional<WindowsApplicationSourceBinding> WindowsApplicationSourceCoordinator::Discover(
    const WindowsAudioSessionDescriptor& descriptor) {
    if (!available() || descriptor.runtime_group_key.empty() || descriptor.process_id == 0)
        return std::nullopt;
    const auto existing = runtime_groups_.find(descriptor.runtime_group_key);
    if (existing != runtime_groups_.end()) {
        const auto* source = registry_.Find(existing->second);
        if (!source) return std::nullopt;
        return WindowsApplicationSourceBinding{descriptor.runtime_group_key,
                                                descriptor.process_id, *source};
    }
    if (!source_id_generator_) return std::nullopt;
    const auto id = source_id_generator_();
    if (!id || !id->IsValid()) return std::nullopt;
    AudioSourceMetadata source;
    source.identity = descriptor.identity;
    source.identity.id = *id;
    source.identity.type = AudioSourceType::Application;
    source.format = {kCanonicalSampleRate, kCanonicalChannels, "fltp"};
    if (!registry_.Discover(source)) return std::nullopt;
    runtime_groups_.emplace(descriptor.runtime_group_key, *id);
    // Process loopback INCLUDE operates on a target and its descendants.  The
    // registry's deterministic same-executable root is therefore the target;
    // capturing an arbitrary child would miss its sibling render/helper tree.
    const uint32_t capture_process_id = descriptor.process_group_root_id != 0
        ? descriptor.process_group_root_id : descriptor.process_id;
    return WindowsApplicationSourceBinding{descriptor.runtime_group_key,
                                            capture_process_id, source};
}

std::optional<AudioSourceId> WindowsApplicationSourceCoordinator::RetireRuntimeGroup(
    const std::string& runtime_group_key, int64_t timestamp_100ns) {
    const auto it = runtime_groups_.find(runtime_group_key);
    if (it == runtime_groups_.end()) return std::nullopt;
    registry_.MarkEnded(it->second, timestamp_100ns);
    const AudioSourceId id = it->second;
    runtime_groups_.erase(it);
    return id;
}

std::optional<std::string> WindowsApplicationSourceCoordinator::RetireSource(
    const AudioSourceId& id, int64_t timestamp_100ns) {
    for (auto it = runtime_groups_.begin(); it != runtime_groups_.end(); ++it) {
        if (!(it->second == id)) continue;
        registry_.MarkEnded(id, timestamp_100ns);
        const std::string runtime_group_key = it->first;
        runtime_groups_.erase(it);
        return runtime_group_key;
    }
    return std::nullopt;
}

bool WindowsApplicationSourceCoordinator::ShouldRetireRuntimeGroup(
    const std::string& runtime_group_key, int64_t now_100ns) const {
    const auto group = runtime_groups_.find(runtime_group_key);
    if (group == runtime_groups_.end()) return false;
    const auto* source = registry_.Find(group->second);
    if (!source || source->state.active_in_generation || source->state.admitted
            || source->state.last_active_100ns < 0) {
        return false;
    }
    return now_100ns >= source->state.last_active_100ns
        && now_100ns - source->state.last_active_100ns >= retention_100ns_;
}

void WindowsApplicationSourceCoordinator::PruneExpired(int64_t now_100ns) {
    registry_.ReleaseAdmissionsOlderThan(now_100ns - retention_100ns_);
    registry_.PruneEndedOlderThan(now_100ns - retention_100ns_);
}

AudioSourceAdmission WindowsApplicationSourceCoordinator::ObserveActivity(
    const AudioSourceId& id, float rms, int64_t timestamp_100ns) {
    registry_.ReleaseAdmissionsOlderThan(timestamp_100ns - retention_100ns_);
    return registry_.ObserveActivity(id, rms, timestamp_100ns);
}

void WindowsApplicationSourceCoordinator::MarkFailed(const AudioSourceId& id) {
    registry_.MarkFailed(id);
}

std::vector<AudioSourceMetadata> WindowsApplicationSourceCoordinator::SourcesForInterval(
    int64_t start_100ns, int64_t end_100ns) const {
    return registry_.SourcesForInterval(start_100ns, end_100ns);
}

const AudioSourceMetadata* WindowsApplicationSourceCoordinator::Find(const AudioSourceId& id) const {
    return registry_.Find(id);
}

struct WindowsApplicationAudioSourceManager::Impl {
    explicit Impl(uint64_t initial_generation, uint32_t initial_retention_seconds,
                  std::shared_ptr<ReplayPauseClock> clock, AudioPacketSink sink,
                  std::shared_ptr<ReplayStorage> storage, std::vector<std::string> exclusions)
        : generation(initial_generation), retention_seconds(initial_retention_seconds),
          replay_pause_clock(std::move(clock)),
          packet_sink(std::move(sink)),
          replay_storage(std::move(storage)),
          excluded_identities(std::move(exclusions)),
          capability(DetectWindowsProcessLoopbackCapability()) {}

    struct ProviderEntry {
        std::string runtime_group_key;
        std::shared_ptr<WindowsProcessLoopbackAudioProvider> provider;
        int64_t retired_at_100ns = 0;
    };

    uint64_t generation;
    uint32_t retention_seconds;
    std::shared_ptr<ReplayStorage> replay_storage;
    std::shared_ptr<ReplayPauseClock> replay_pause_clock;
    AudioPacketSink packet_sink;
    WindowsProcessLoopbackCapability capability;
    std::vector<std::string> excluded_identities;
    mutable std::mutex mutex;
    std::unique_ptr<WindowsApplicationSourceCoordinator> coordinator;
    std::map<std::string, ProviderEntry> providers_by_source_id;
    std::map<std::string, AudioSourceId> active_runtime_groups;
    std::thread monitor_thread;
    HANDLE stop_event = nullptr;
    std::atomic<bool> running{false};
    std::atomic<uint64_t> source_limit_rejections{0};
    std::string error;

    void OnProviderEnded(const AudioSourceId& id, int64_t timestamp_100ns) {
        std::lock_guard<std::mutex> lock(mutex);
        const auto entry = providers_by_source_id.find(id.value);
        if (entry == providers_by_source_id.end()) return;
        const auto active = active_runtime_groups.find(entry->second.runtime_group_key);
        if (active != active_runtime_groups.end() && active->second == id)
            active_runtime_groups.erase(active);
        if (coordinator) coordinator->RetireSource(id, timestamp_100ns);
        entry->second.retired_at_100ns = timestamp_100ns;
    }

    void CleanupRetired(int64_t now_100ns) {
        std::lock_guard<std::mutex> lock(mutex);
        if (coordinator) coordinator->PruneExpired(now_100ns);
        const int64_t cutoff = now_100ns
            - static_cast<int64_t>(std::max<uint32_t>(1, retention_seconds))
                * 10'000'000LL;
        for (auto it = providers_by_source_id.begin();
             it != providers_by_source_id.end();) {
            const auto& entry = it->second;
            if (entry.retired_at_100ns <= 0 || entry.retired_at_100ns >= cutoff) {
                ++it;
                continue;
            }
            it = providers_by_source_id.erase(it);
        }
    }

    std::optional<AudioSourceId> GenerateSourceId() {
        std::string value;
        std::string ignored;
        if (!CreateAudioManifestTransactionId(&value, &ignored)) return std::nullopt;
        return AudioSourceId{value};
    }

    void SetError(std::string value) {
        std::lock_guard<std::mutex> lock(mutex);
        error = std::move(value);
    }

    AudioSourceAdmission OnActivity(const AudioSourceId& id, float rms, int64_t timestamp) {
        std::lock_guard<std::mutex> lock(mutex);
        return coordinator ? coordinator->ObserveActivity(id, rms, timestamp)
                           : AudioSourceAdmission::RejectedInvalidIdentity;
    }

    void OnFailure(const AudioSourceId& id) {
        std::lock_guard<std::mutex> lock(mutex);
        if (coordinator) coordinator->MarkFailed(id);
        const auto entry = providers_by_source_id.find(id.value);
        const auto* source = coordinator ? coordinator->Find(id) : nullptr;
        std::cerr << "[ApplicationAudio] Source '"
            << (source ? source->identity.display_name : "Application")
            << "' failed";
        if (entry != providers_by_source_id.end() && entry->second.provider) {
            const std::string detail = entry->second.provider->last_error();
            if (!detail.empty()) std::cerr << ": " << detail;
        }
        std::cerr << std::endl;
    }

    void EnsureProvider(const WindowsAudioSessionDescriptor& descriptor) {
        if (std::find(excluded_identities.begin(), excluded_identities.end(),
                      descriptor.identity.persistent_identity) != excluded_identities.end()) return;
        WindowsProcessLoopbackAudioProvider* to_start = nullptr;
        AudioSourceId start_id;
        std::string start_group;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!coordinator) return;
            const bool existing_group = coordinator->HasRuntimeGroup(
                descriptor.runtime_group_key);
            if (!existing_group
                    && providers_by_source_id.size() >= kMaxRetainedAudioSources) {
                source_limit_rejections.fetch_add(1, std::memory_order_relaxed);
                return;
            }
            const auto binding = coordinator->Discover(descriptor);
            if (!binding) return;
            active_runtime_groups[binding->runtime_group_key] = binding->source.identity.id;
            const auto source_key = binding->source.identity.id.value;
            if (providers_by_source_id.find(source_key) != providers_by_source_id.end()) return;
            WindowsProcessLoopbackProviderConfig config;
            config.generation = generation;
            config.target_process_id = binding->process_id;
            config.include_process_tree = true;
            config.retention_seconds = retention_seconds;
            config.replay_storage = replay_storage;
            config.source = binding->source;
            config.replay_pause_clock = replay_pause_clock;
            config.packet_sink = packet_sink;
            auto provider = std::make_shared<WindowsProcessLoopbackAudioProvider>(
                std::move(config),
                [this](const AudioSourceId& id, float rms, int64_t timestamp) {
                    return OnActivity(id, rms, timestamp);
                },
                [this](const AudioSourceId& id, int64_t timestamp) {
                    OnProviderEnded(id, timestamp);
                },
                [this](const AudioSourceId& id) { OnFailure(id); });
            to_start = provider.get();
            start_id = binding->source.identity.id;
            start_group = binding->runtime_group_key;
            providers_by_source_id.emplace(source_key,
                ProviderEntry{binding->runtime_group_key, std::move(provider)});
        }
        if (to_start) {
            std::cout << "[ApplicationAudio] Capturing active source '"
                << descriptor.identity.display_name << "' (PID "
                << descriptor.process_id << ")" << std::endl;
            if (!to_start->Start()) {
                OnFailure(start_id);
                RetireProvider(start_group);
            }
        }
    }

    void RetireProvider(const std::string& runtime_group_key) {
        std::shared_ptr<WindowsProcessLoopbackAudioProvider> provider;
        AudioSourceId id;
        {
            std::lock_guard<std::mutex> lock(mutex);
            const auto active = active_runtime_groups.find(runtime_group_key);
            if (active == active_runtime_groups.end()) return;
            id = active->second;
            active_runtime_groups.erase(active);
            const auto entry = providers_by_source_id.find(id.value);
            if (entry != providers_by_source_id.end()) provider = entry->second.provider;
        }
        if (provider) provider->Stop();
        // Stop first so a final capture callback cannot reactivate an already
        // retired group while the provider thread is winding down.
        const int64_t retired_at_100ns = CurrentQpc100ns();
        std::lock_guard<std::mutex> lock(mutex);
        if (coordinator) coordinator->RetireRuntimeGroup(runtime_group_key, retired_at_100ns);
        const auto entry = providers_by_source_id.find(id.value);
        if (entry != providers_by_source_id.end())
            entry->second.retired_at_100ns = retired_at_100ns;
    }

    bool ProviderHasCapturedAudio(const std::string& runtime_group_key) {
        std::lock_guard<std::mutex> lock(mutex);
        const auto active = active_runtime_groups.find(runtime_group_key);
        if (active == active_runtime_groups.end()) return false;
        const auto entry = providers_by_source_id.find(active->second.value);
        return entry != providers_by_source_id.end() && entry->second.provider
            && entry->second.provider->HasCapturedAudio();
    }

    bool ProviderShouldRetire(const std::string& runtime_group_key,
                              int64_t now_100ns) {
        std::lock_guard<std::mutex> lock(mutex);
        return coordinator && coordinator->ShouldRetireRuntimeGroup(
            runtime_group_key, now_100ns);
    }

    void HandleUpdate(const WindowsAudioSessionUpdate& update) {
        // Retire an old nested owner before activating its replacement. The
        // registry publishes only non-overlapping process trees.
        for (const auto& removed : update.disappeared_runtime_groups) RetireProvider(removed);
        // Inactive WASAPI sessions can linger for hours. Starting a process-
        // loopback client for all of them created an unbounded set of idle
        // threads and made genuinely audible sources unreliable. State-change
        // notifications (plus the safety poll below) start a provider only
        // when Windows reports that application as actively rendering.
        const int64_t now_100ns = static_cast<int64_t>(CurrentQpc100ns());
        for (const auto& descriptor : update.current) {
            const bool quiet_expired = ProviderShouldRetire(
                descriptor.runtime_group_key, now_100ns);
            if (descriptor.currently_active || (!quiet_expired
                    && ProviderHasCapturedAudio(descriptor.runtime_group_key))) {
                EnsureProvider(descriptor);
            } else {
                RetireProvider(descriptor.runtime_group_key);
            }
        }
    }

    void RunMonitor() {
        const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(apartment) && apartment != RPC_E_CHANGED_MODE) {
            SetError("Windows application-audio session monitor could not initialize COM");
            running.store(false, std::memory_order_release);
            return;
        }
        WindowsAudioSessionRegistry sessions(excluded_identities);
        if (!sessions.Start()) {
            SetError(sessions.last_error());
            if (SUCCEEDED(apartment)) CoUninitialize();
            running.store(false, std::memory_order_release);
            return;
        }
        WindowsAudioSessionUpdate initial;
        sessions.RefreshIfNeeded(&initial);
        // Only sessions currently reported active receive a provider. This
        // keeps silent/background session discovery from creating idle capture
        // threads; later state notifications start a provider on demand.
        HandleUpdate(initial);
        auto next_topology_refresh = std::chrono::steady_clock::now()
            + std::chrono::seconds(2);
        while (!WaitForStop(stop_event, 25)) {
            const auto now = std::chrono::steady_clock::now();
            CleanupRetired(static_cast<int64_t>(CurrentQpc100ns()));
            if (now >= next_topology_refresh) {
                sessions.RequestRefresh();
                next_topology_refresh = now + std::chrono::seconds(2);
            }
            if (!sessions.needs_refresh()) continue;
            WindowsAudioSessionUpdate update;
            if (sessions.RefreshIfNeeded(&update)) HandleUpdate(update);
            else SetError(sessions.last_error());
        }
        sessions.Stop();
        if (SUCCEEDED(apartment)) CoUninitialize();
        running.store(false, std::memory_order_release);
    }
};

WindowsApplicationAudioSourceManager::WindowsApplicationAudioSourceManager(
    uint64_t generation, uint32_t retention_seconds,
    std::shared_ptr<ReplayPauseClock> replay_pause_clock, AudioPacketSink packet_sink,
    std::shared_ptr<ReplayStorage> replay_storage, std::vector<std::string> excluded_identities)
    : impl_(std::make_unique<Impl>(generation, retention_seconds,
                                 std::move(replay_pause_clock), std::move(packet_sink),
                                 std::move(replay_storage), std::move(excluded_identities))) {}

WindowsApplicationAudioSourceManager::~WindowsApplicationAudioSourceManager() {
    Stop();
}

bool WindowsApplicationAudioSourceManager::Start() {
    if (!impl_ || !impl_->capability.api_build_supported) return false;
    if (impl_->running.exchange(true, std::memory_order_acq_rel)) return true;
    impl_->source_limit_rejections.store(0, std::memory_order_relaxed);
    impl_->stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!impl_->stop_event) {
        impl_->SetError("Windows application-audio stop event creation failed");
        impl_->running.store(false, std::memory_order_release);
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->coordinator = std::make_unique<WindowsApplicationSourceCoordinator>(
            impl_->generation, impl_->capability,
            [this] { return impl_->GenerateSourceId(); },
            // The provisional app-stem cap is eight. Default Mix is a separate
            // mandatory compatibility track and does not consume an app slot.
            kMaxRetainedAudioSources, impl_->retention_seconds);
    }
    impl_->monitor_thread = std::thread([this] { impl_->RunMonitor(); });
    return true;
}

void WindowsApplicationAudioSourceManager::Stop() {
    if (!impl_) return;
    if (impl_->stop_event) SetEvent(impl_->stop_event);
    if (impl_->monitor_thread.joinable()) impl_->monitor_thread.join();
    std::vector<std::shared_ptr<WindowsProcessLoopbackAudioProvider>> providers;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        for (auto& [_, entry] : impl_->providers_by_source_id) {
            if (entry.provider) providers.push_back(entry.provider);
        }
        impl_->active_runtime_groups.clear();
    }
    for (const auto& provider : providers) provider->Stop();
    if (impl_->stop_event) CloseHandle(impl_->stop_event);
    impl_->stop_event = nullptr;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->providers_by_source_id.clear();
        impl_->coordinator.reset();
    }
    impl_->running.store(false, std::memory_order_release);
}

bool WindowsApplicationAudioSourceManager::available() const {
    return impl_ && impl_->capability.api_build_supported;
}

WindowsProcessLoopbackCapability WindowsApplicationAudioSourceManager::capability() const {
    return impl_ ? impl_->capability : WindowsProcessLoopbackCapability{};
}

std::string WindowsApplicationAudioSourceManager::last_error() const {
    if (!impl_) return {};
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->error;
}

WindowsApplicationAudioSourceManager::RuntimeInfo
WindowsApplicationAudioSourceManager::runtime_info() const {
    RuntimeInfo result;
    if (!impl_) return result;
    result.available = impl_->capability.api_build_supported;
    result.running = impl_->running.load(std::memory_order_acquire);
    result.source_limit_rejections = impl_->source_limit_rejections.load(
        std::memory_order_relaxed);
    std::vector<std::shared_ptr<WindowsProcessLoopbackAudioProvider>> providers;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        result.candidate_count = static_cast<uint32_t>(
            impl_->active_runtime_groups.size());
        result.retained_provider_count = static_cast<uint32_t>(
            impl_->providers_by_source_id.size());
        result.admitted_source_count = impl_->coordinator
            ? impl_->coordinator->admitted_count() : 0;
        providers.reserve(impl_->providers_by_source_id.size());
        for (const auto& [_, entry] : impl_->providers_by_source_id)
            if (entry.provider) providers.push_back(entry.provider);
    }
    for (const auto& provider : providers) {
        const auto info = provider->runtime_info();
        result.input_frames += info.input_frames;
        if (info.state == "FAILED") ++result.failed_sources;
        result.packet_count += info.packet_count;
        result.converted_frames += info.converted_frames;
        result.submitted_frames += info.submitted_frames;
        result.encoded_packets += info.encoded_packets;
        result.encode_failures += info.encode_failures;
        result.finalize_failures += info.finalize_failures;
        result.dropped_blocks += info.dropped_blocks;
        result.no_packet_intervals += info.no_packet_intervals;
        result.largest_no_packet_gap_100ns = std::max(
            result.largest_no_packet_gap_100ns, info.largest_no_packet_gap_100ns);
        result.discontinuity_count += info.discontinuity_count;
        result.rejected_packets += info.rejected_packets;
        result.restart_count += info.restart_count;
        result.largest_gap_100ns = std::max(result.largest_gap_100ns,
                                            info.largest_gap_100ns);
    }
    return result;
}

std::vector<EncodedAudioTrack> WindowsApplicationAudioSourceManager::TakeTracksForInterval(
    double presentation_start_qpc_s, double presentation_end_qpc_s) const {
    std::vector<std::pair<std::shared_ptr<WindowsProcessLoopbackAudioProvider>, AudioSourceMetadata>> candidates;
    if (!impl_ || presentation_end_qpc_s <= presentation_start_qpc_s) return {};
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (!impl_->coordinator) return {};
        // Registry activity uses wall QPC; presentation windows use compact
        // replay time after focus pauses. Let the mapped packet rings perform
        // interval selection instead of comparing the two clock domains.
        for (const auto& source : impl_->coordinator->SourcesForInterval(
                0, CurrentQpc100ns())) {
            const auto entry = impl_->providers_by_source_id.find(source.identity.id.value);
            if (entry != impl_->providers_by_source_id.end() && entry->second.provider)
                candidates.emplace_back(entry->second.provider, source);
        }
    }
    std::vector<EncodedAudioTrack> tracks;
    tracks.reserve(candidates.size());
    for (const auto& [provider, source] : candidates) {
        if (const auto track = provider->TakeTrackForInterval(
                presentation_start_qpc_s, presentation_end_qpc_s, source)) {
            tracks.push_back(*track);
        }
    }
    return tracks;
}

}  // namespace fthr
