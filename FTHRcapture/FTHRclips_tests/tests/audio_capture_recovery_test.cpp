#include "audio_capture.h"
#include "audio_timeline.h"
#include "audio_ring_buffer.h"
#include <windows.h>
#include <audioclient.h>
#include <iostream>
#include <vector>
#include <sstream>
#include <limits>

namespace fthr {
struct AudioCaptureRecoveryTestAccess {
    using Ops = AudioCapture::CaptureLoopOps;
    using Result = AudioCapture::SessionResult;
    static void Prepare(AudioCapture& capture) {
        capture.running_.store(true);
        capture.sample_rate_ = 48000;
        capture.channels_ = 2;
        capture.timeline_origin_qpc_100ns_.store(123456);
    }
    static void Run(AudioCapture& capture, const Ops& ops) { capture.RunCaptureLoop(ops); }
    static void Stop(AudioCapture& capture) { capture.running_.store(false); }
    static uint64_t Timestamp(AudioCapture& capture, uint64_t qpc, uint32_t flags) {
        const uint64_t fallback = qpc == 0 || (flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) != 0
            ? CurrentAudioTimeline100ns() : 0;
        return capture.SelectPacketTimestamp(qpc, flags, fallback);
    }
    static uint64_t UnavailableTimestamp(AudioCapture& capture) {
        return capture.SelectPacketTimestamp(7, AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, 0);
    }
    static bool ConfigurePcm(AudioCapture& capture, AudioRingBuffer& ring,
                             uint32_t rate = 48000, uint32_t channels = 2) {
        WAVEFORMATEX format{};
        format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
        format.nSamplesPerSec = rate;
        format.nChannels = static_cast<WORD>(channels);
        format.wBitsPerSample = 32;
        format.nBlockAlign = static_cast<WORD>(channels * sizeof(float));
        format.nAvgBytesPerSec = rate * format.nBlockAlign;
        if (!capture.format_converter_.Initialize(&format)) return false;
        capture.input_sample_rate_ = rate;
        capture.input_channels_ = channels;
        capture.input_bytes_per_frame_ = format.nBlockAlign;
        capture.sample_rate_ = capture.format_converter_.sample_rate();
        capture.channels_ = capture.format_converter_.channels();
        capture.ring_ = &ring;
        capture.silence_buf_.assign(9600 * 2, 0.0f);
        return true;
    }
    static bool Packet(AudioCapture& capture, uint32_t frames, uint64_t qpc) {
        std::vector<float> samples(static_cast<size_t>(frames) * capture.input_channels_, 0.125f);
        return capture.SubmitCapturedPacket(reinterpret_cast<const uint8_t*>(samples.data()), frames, qpc);
    }
    static bool Idle(AudioCapture& capture, uint32_t frames, uint64_t qpc) {
        return capture.InjectSilence(frames, qpc);
    }
    static void Reset(AudioCapture& capture) { capture.ResetGenerationRecoveryState(); }
    static void Terminal(AudioCapture& capture) { capture.MarkTerminalFailure(AudioCapture::FailureReason::TimelineGap); }
};
}

namespace {
int checks = 0, failures = 0;
void Check(bool condition, const char* message) {
    ++checks;
    if (!condition) { ++failures; std::cerr << "FAILED: " << message << '\n'; }
}
using Access = fthr::AudioCaptureRecoveryTestAccess;

void SetupFailuresDoNotFallThroughNullSession() {
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    bool ready = true;
    int setups = 0, starts = 0, sessions = 0;
    std::vector<int> delays;
    const Access::Ops ops{
        [&] { return ready; },
        [&](uint64_t&) { return ++sessions == 1 ? Access::Result::EndpointFailure : Access::Result::Stopped; },
        [&] { ready = false; },
        [&] { ++setups; ready = setups >= 3; return ready; },
        [&] { ++starts; return true; },
        [&](int delay) { delays.push_back(delay); },
    };
    Access::Run(capture, ops);
    Check(setups == 3 && starts == 1 && sessions == 2, "failed setup retries through fail/fail/pass");
    Check(delays == std::vector<int>({1000, 2000, 3000}), "setup failures consume increasing delay budget");
}

void LargeGapDoesNotReopenOrResetOrigin() {
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    int sessions = 0, setups = 0;
    const Access::Ops ops{
        [] { return true; },
        [&](uint64_t& cursor) {
            if (++sessions > 8) { Access::Stop(capture); return Access::Result::Stopped; }
            cursor = 10'000'000;
            const auto adjustment = fthr::ReconcileAudioTimelinePacket(
                cursor, cursor + 60'000'000, 480, 48000);
            Check(adjustment.large_gap, "production timeline helper identifies oversized gap");
            return adjustment.large_gap ? Access::Result::TimelineGap : Access::Result::EndpointFailure;
        },
        [] {}, [&] { ++setups; return true; }, [] { return true; }, [](int) {},
    };
    Access::Run(capture, ops);
    Check(setups == 0 && sessions == 1, "large gap ends current generation without reopen");
    Check(capture.IsDeviceLost() && !capture.IsRunning(), "large gap publishes terminal lost/stopped state");
    Check(capture.GetTimelineOriginQpc100ns() == 123456, "terminal gap preserves AAC timeline origin");
    Check(capture.GetFailureReason() == fthr::AudioCapture::FailureReason::TimelineGap,
        "gap failure exposes an atomic terminal reason");
    Check(!capture.Start(), "Start refuses a terminal generation before touching endpoint objects");
    capture.Shutdown();
    Check(!capture.IsDeviceLost() && capture.GetTimelineOriginQpc100ns() == 0,
        "normal Shutdown resets loss and timeline generation origin");
}

void BriefSuccessfulStartsCannotResetRecoveryBudget() {
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    int sessions = 0, setups = 0;
    std::vector<int> delays;
    const Access::Ops ops{
        [] { return true; },
        [&](uint64_t&) {
            if (++sessions > 12) { Access::Stop(capture); return Access::Result::Stopped; }
            return Access::Result::EndpointFailure;
        },
        [] {}, [&] { ++setups; return true; }, [] { return true; },
        [&](int delay) { delays.push_back(delay); },
    };
    Access::Run(capture, ops);
    Check(setups == 5 && sessions == 6, "short open/error cycles terminate after five recovery attempts");
    Check(delays == std::vector<int>({1000, 2000, 3000, 4000, 5000}), "successful Start alone cannot reset retry delays");
    Check(capture.IsDeviceLost() && !capture.IsRunning(), "retry exhaustion is terminal and stops capture");
}

void ErrorFlagOverridesNonzeroPacketTimestamp() {
    fthr::AudioCapture capture;
    const uint64_t before = fthr::CurrentAudioTimeline100ns();
    const auto timestamp = Access::Timestamp(capture, 7, AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR);
    const uint64_t after = fthr::CurrentAudioTimeline100ns();
    Check(timestamp >= before && timestamp <= after, "timestamp-error packet uses processing-time fallback even when nonzero");
    Check(Access::Timestamp(capture, 1234567, AUDCLNT_BUFFERFLAGS_SILENT
        | AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) == 1234567,
        "valid nonzero 100ns timestamp is unchanged by other flags");
    Access::Timestamp(capture, 0, 0);
    Access::Timestamp(capture, 0, AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR);
    Check(capture.GetTimestampErrorPacketCount() == 2, "flagged packets are counted once");
    Check(capture.GetProcessingTimeFallbackCount() == 3, "zero or flagged fallback provenance counted once per packet");
    Access::Reset(capture);
    Check(capture.GetTimestampErrorPacketCount() == 0 && capture.GetProcessingTimeFallbackCount() == 0,
        "Initialize recovery-state reset clears timestamp counters");
}

void StartFailuresAndExhaustionCleanPartialSessions() {
    for (const bool all_fail : {false, true}) {
        fthr::AudioCapture capture;
        Access::Prepare(capture);
        bool ready = true;
        int setups = 0, starts = 0, teardown = 0, sessions = 0;
        std::vector<int> delays;
        const Access::Ops ops{
            [&] { return ready; },
            [&](uint64_t&) { return ++sessions == 1 ? Access::Result::EndpointFailure : Access::Result::Stopped; },
            [&] { ++teardown; ready = false; },
            [&] { ++setups; ready = true; return true; },
            [&] { ++starts; return !all_fail && starts >= 3; },
            [&](int delay) { delays.push_back(delay); },
        };
        Access::Run(capture, ops);
        Check(setups == (all_fail ? 5 : 3) && starts == setups, "Start fail/fail/pass or five-failure attempt count");
        Check(teardown == (all_fail ? 10 : 5), "every failed Start cleans partial session before next attempt");
        Check(capture.IsDeviceLost() == all_fail, "only exhausted Start retries publish loss");
        if (all_fail) Check(delays == std::vector<int>({1000, 2000, 3000, 4000, 5000}),
            "all five failed starts consume distinct increasing delays");
    }
}

void FiveFailedSetupsCleanPartialSessions() {
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    bool ready = true;
    int setups = 0, starts = 0, teardown = 0, sessions = 0;
    std::vector<int> delays;
    const Access::Ops ops{
        [&] { return ready; },
        [&](uint64_t&) { ++sessions; return Access::Result::EndpointFailure; },
        [&] { ++teardown; ready = false; },
        [&] { ++setups; return false; },
        [&] { ++starts; return true; },
        [&](int delay) { delays.push_back(delay); },
    };
    Access::Run(capture, ops);
    Check(setups == 5 && starts == 0 && sessions == 1, "five failed setups never enter packet loop with null state");
    Check(teardown == 10, "each failed setup tears down owned partial session before retry");
    Check(delays == std::vector<int>({1000, 2000, 3000, 4000, 5000}), "five setup failures have exactly five delayed attempts");
    Check(capture.IsDeviceLost() && !capture.IsRunning(), "five setup failures terminally stop only audio generation");
}

void RealSubmittedProgressResetsBudgetAndPublishesRecoveryImmediately() {
    fthr::AudioRingBuffer ring(100000);
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    Check(Access::ConfigurePcm(capture, ring), "real canonical converter/ring configured without endpoint");
    int sessions = 0;
    std::vector<int> delays;
    const Access::Ops ops{
        [] { return true; },
        [&](uint64_t& cursor) {
            ++sessions;
            if (sessions == 2) {
                Check(capture.IsRecoveryPending(), "Start success remains RECOVERING before packet progress");
                Check(Access::Packet(capture, 47999, cursor), "actual native converter/ring submits short first packet");
                Check(capture.IsRecoveryPending(), "less than one-second real sample progress cannot recover");
                cursor += (47999ULL * 10'000'000ULL) / 48000;
                Check(Access::Packet(capture, 1, cursor), "actual final frame submits successfully");
                Check(!capture.IsRecoveryPending(), "one-second real sample duration recovers before RunSession returns");
            }
            return sessions >= 3 ? Access::Result::Stopped : Access::Result::EndpointFailure;
        }, [] {}, [] { return true; }, [] { return true; },
        [&](int delay) { delays.push_back(delay); },
    };
    Access::Run(capture, ops);
    Check(delays == std::vector<int>({1000, 1000}), "real one-second packet submission resets attempt budget");
    Check(!capture.IsDeviceLost() && !capture.IsRunning(), "normal stop after recovered session is not device loss");
    Check(capture.GetSubmittedFrameCount() == 48000, "recovery proof counts actual canonical frames only");
}

void ShortRealProgressAndIdleSilenceRemainBounded() {
    for (const bool idle : {false, true}) {
        fthr::AudioRingBuffer ring(60000);
        fthr::AudioCapture capture;
        Access::Prepare(capture);
        Check(Access::ConfigurePcm(capture, ring), "short/idle fixture uses actual conversion and submission");
        int sessions = 0, setups = 0;
        const Access::Ops ops{
            [] { return true; },
            [&](uint64_t& cursor) {
                if (++sessions > 10) { Access::Stop(capture); return Access::Result::Stopped; }
                if (idle) Check(Access::Idle(capture, 48000, cursor), "idle silence is actually submitted but cannot prove recovery");
                else Check(Access::Packet(capture, 24000, cursor), "half-second actual packet is submitted");
                cursor += idle ? 10'000'000 : 5'000'000;
                Check(!Access::Packet(capture, 0, cursor), "zero-frame real packet does not create recovery proof");
                return Access::Result::EndpointFailure;
            }, [] {}, [&] { ++setups; return true; }, [] { return true; }, [](int) {},
        };
        Access::Run(capture, ops);
        Check(setups == 5 && sessions == 6, "short or idle sample progress cannot extend retry budget");
        Check(capture.IsDeviceLost() && !capture.IsRecoveryPending(), "bounded short/idle recovery ends terminally");
    }
}

void StopDuringBackoffIsNotLoss() {
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    int setups = 0, teardown = 0;
    const Access::Ops ops{
        [] { return true; }, [](uint64_t&) { return Access::Result::EndpointFailure; },
        [&] { ++teardown; }, [&] { ++setups; return true; }, [] { return true; },
        [&](int delay) { Check(delay == 1000, "first backoff delay is bounded"); capture.Stop(); },
    };
    Access::Run(capture, ops);
    Check(setups == 0 && teardown == 0, "Stop during backoff does not reopen any endpoint");
    Check(!capture.IsDeviceLost() && !capture.IsRunning(), "interruptible Stop is normal, not terminal loss");
}

void NativeFormatChangesKeepCanonicalFormatAndOrigin() {
    fthr::AudioRingBuffer ring(100000);
    fthr::AudioCapture capture;
    Access::Prepare(capture);
    Check(Access::ConfigurePcm(capture, ring), "initial float stereo endpoint conversion configured");
    Check(Access::ConfigurePcm(capture, ring, 96000, 1), "reopened mono 96k endpoint conversion configured");
    Check(capture.GetSampleRate() == 48000 && capture.GetChannels() == 2,
        "native format change never changes canonical AAC 48k stereo contract");
    Check(Access::Packet(capture, 96000, fthr::CurrentAudioTimeline100ns()), "new native format actually converts and submits");
    Check(capture.GetConvertedFrameCount() > 47000 && capture.GetTimelineOriginQpc100ns() == 123456,
        "native format conversion keeps existing generation timeline origin");
}

void TerminalEventIsOncePerGeneration() {
    fthr::AudioCapture capture;
    std::ostringstream events;
    auto* previous = std::cout.rdbuf(events.rdbuf());
    Access::Terminal(capture);
    Access::Terminal(capture);
    std::cout.rdbuf(previous);
    const auto first = events.str().find("system_audio_terminal_failure");
    Check(first != std::string::npos && events.str().find("system_audio_terminal_failure", first + 1) == std::string::npos,
        "terminal structured event emitted only once for a generation");
    capture.Shutdown();
    Check(capture.GetFailureReason() == fthr::AudioCapture::FailureReason::None,
        "Shutdown clears terminal reason for next generation lifecycle");
}

void UnavailableResolvedTimestampCannotSubmitOrInventOrigin() {
    fthr::AudioRingBuffer ring(100000);
    fthr::AudioCapture capture;
    Check(Access::ConfigurePcm(capture, ring), "unavailable-clock fixture configures converter without endpoint");
    Check(Access::UnavailableTimestamp(capture) == 0 && capture.GetUnavailableTimestampCount() == 1,
        "unavailable processing clock remains explicitly unavailable, not a fabricated packet time");
    Check(!Access::Packet(capture, 480, 0), "unavailable resolved timestamp is refused before a second clock fallback");
    Check(ring.GetFramesPushed() == 0 && capture.GetSubmittedFrameCount() == 0,
        "unavailable timestamp cannot publish any canonical audio samples");
    Check(capture.GetTimelineOriginQpc100ns() == 0, "unavailable timestamp cannot invent a valid AAC origin");
    Access::Prepare(capture);
    int reopen = 0;
    const Access::Ops ops{
        [] { return true; }, [](uint64_t&) { return Access::Result::TimestampUnavailable; },
        [] {}, [&] { ++reopen; return true; }, [] { return true; }, [](int) {},
    };
    Access::Run(capture, ops);
    Check(reopen == 0 && capture.IsDeviceLost()
        && capture.GetFailureReason() == fthr::AudioCapture::FailureReason::TimestampUnavailable,
        "unavailable clock terminates generation without reopening or claiming an audio timestamp");
    Access::Reset(capture);
    Check(capture.GetUnavailableTimestampCount() == 0 && !capture.IsDeviceLost()
        && capture.GetTimelineOriginQpc100ns() == 0,
        "new Initialize generation reset clears unavailable-clock history and loss");
}

void ActualAttemptLogsFollowCompletedBackoffOnly() {
    for (const bool stop_in_backoff : {false, true}) {
        fthr::AudioCapture capture;
        Access::Prepare(capture);
        int sessions = 0;
        std::ostringstream events;
        auto* previous = std::cout.rdbuf(events.rdbuf());
        const Access::Ops ops{
            [] { return true; },
            [&](uint64_t&) { return ++sessions == 1 ? Access::Result::EndpointFailure : Access::Result::Stopped; },
            [&] { std::cout << "TEST_TEARDOWN\n"; },
            [] { return true; }, [] { return true; },
            [&](int) { std::cout << "TEST_BACKOFF_DONE\n"; if (stop_in_backoff) capture.Stop(); },
        };
        Access::Run(capture, ops);
        std::cout.rdbuf(previous);
        const auto text = events.str();
        const auto attempt = text.find("\"event\":\"system_audio_recovery_attempt\"");
        Check((attempt != std::string::npos) == !stop_in_backoff,
            "only an actually executed recovery attempt is logged, not cancelled scheduled wait");
        if (!stop_in_backoff) {
            Check(text.find("TEST_BACKOFF_DONE") < attempt && attempt < text.find("TEST_TEARDOWN"),
                "attempt event occurs after backoff and before session teardown/setup");
            Check(text.find("\"attempt\":1") != std::string::npos
                && text.find("\"backoff_ms\":1000") != std::string::npos,
                "attempt event exposes actual bounded attempt and elapsed backoff contract");
        }
    }
}

void StopAtRecoveryCallbackBoundariesPreventsSuccessorWork() {
    for (const int stop_at : {0, 1, 2}) {
        fthr::AudioCapture capture;
        Access::Prepare(capture);
        int sessions = 0, teardown = 0, setups = 0, starts = 0;
        const Access::Ops ops{
            [] { return true; },
            [&](uint64_t&) { ++sessions; return Access::Result::EndpointFailure; },
            [&] { ++teardown; if (stop_at == 0) capture.Stop(); },
            [&] { ++setups; if (stop_at == 1) capture.Stop(); return true; },
            [&] { ++starts; if (stop_at == 2) capture.Stop(); return true; },
            [](int) {},
        };
        Access::Run(capture, ops);
        Check(setups == (stop_at == 0 ? 0 : 1), "Stop during teardown prevents later Setup");
        Check(starts == (stop_at == 2 ? 1 : 0), "Stop during successful Setup prevents later Start");
        Check(teardown == (stop_at == 0 ? 1 : 2), "Stop after Setup/Start cleans the owned session");
        Check(sessions == 1 && !capture.IsDeviceLost() && !capture.IsRunning(),
            "Stop at callback boundary never enters another packet session or claims device loss");
    }
}
}

int RunAudioCaptureRecoveryTests() {
    SetupFailuresDoNotFallThroughNullSession();
    LargeGapDoesNotReopenOrResetOrigin();
    BriefSuccessfulStartsCannotResetRecoveryBudget();
    ErrorFlagOverridesNonzeroPacketTimestamp();
    StartFailuresAndExhaustionCleanPartialSessions();
    FiveFailedSetupsCleanPartialSessions();
    RealSubmittedProgressResetsBudgetAndPublishesRecoveryImmediately();
    ShortRealProgressAndIdleSilenceRemainBounded();
    StopDuringBackoffIsNotLoss();
    NativeFormatChangesKeepCanonicalFormatAndOrigin();
    TerminalEventIsOncePerGeneration();
    UnavailableResolvedTimestampCannotSubmitOrInventOrigin();
    ActualAttemptLogsFollowCompletedBackoffOnly();
    StopAtRecoveryCallbackBoundariesPreventsSuccessorWork();
    std::cout << "AudioCapture recovery: " << checks << " checks, " << failures << " failures\n";
    return failures ? -1 : checks;
}

#ifdef FTHR_AUDIO_CAPTURE_RECOVERY_STANDALONE
#ifdef FTHR_AUDIO_CAPTURE_RECOVERY_WITH_CONVERTER
int RunAudioFormatConverterTests();
#endif
int main() {
    if (RunAudioCaptureRecoveryTests() < 0) return 1;
#ifdef FTHR_AUDIO_CAPTURE_RECOVERY_WITH_CONVERTER
    if (RunAudioFormatConverterTests() < 0) return 1;
#endif
    return 0;
}
#endif
