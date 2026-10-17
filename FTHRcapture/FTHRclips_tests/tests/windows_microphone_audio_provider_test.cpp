#include "audio_timeline.h"
#include "audio_signal_metrics.h"
#include "windows_microphone_audio_provider.h"

#include <cmath>
#include <cstdlib>
#include <iostream>

namespace {

int microphone_checks = 0;

void CheckMicrophone(bool condition, const char* message) {
    ++microphone_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

void QpcTimelineKeepsLateSourcesAtTheirRealOffset() {
    const auto range = fthr::MapAudioSourcePresentationRange(
        30.0, 60.0, 45ULL * 10'000'000ULL, fthr::kCanonicalAudioSampleRate);
    CheckMicrophone(range.start_pts_samples == -720000
            && range.end_pts_samples == 720000,
        "shared QPC mapping preserves a microphone that begins halfway through a clip");
}

void EncoderDelayStaysSignedInManifestTimeline() {
    constexpr uint64_t origin = 50ULL * 10'000'000ULL;
    CheckMicrophone(fthr::AudioSamplePositionToTimeline100ns(
            origin, -1024, fthr::kCanonicalAudioSampleRate) == 499'786'667,
        "negative AAC encoder delay stays before the source origin without unsigned wrap");
    CheckMicrophone(fthr::AudioSamplePositionToTimeline100ns(
            origin, 1024, fthr::kCanonicalAudioSampleRate) == 500'213'333,
        "positive AAC packet positions map after the source origin");
}

void DriftCorrectionIsSmallAndBounded() {
    fthr::AudioDriftController controller(48000, 100);
    const auto initial = controller.Observe(10'000'000, 0);
    CheckMicrophone(initial.sample_delta == 0,
        "the first microphone packet establishes the common QPC origin without a jump");

    // At 500 ms the source is 120 samples behind.  A 100 ppm policy may only
    // correct a couple of samples over this 500 ms window, never a pop-inducing
    // one-block jump.
    const auto bounded = controller.Observe(15'000'000, 23'880);
    CheckMicrophone(bounded.observed_drift_samples == 120
            && bounded.sample_delta > 0 && bounded.sample_delta <= 3
            && bounded.compensation_distance_samples >= 24000,
        "microphone drift correction is rate-limited against the common clock");

    const auto stable = controller.Observe(15'100'000, 24'485);
    CheckMicrophone(stable.sample_delta == 0,
        "correction is not repeatedly applied before the next bounded window");
    CheckMicrophone(controller.max_observed_drift_samples() >= 120,
        "drift telemetry retains the largest observed source-clock offset");
}

void ExplicitEndpointIdentityIsNotManifestIdentity() {
    fthr::WindowsMicrophoneAudioProviderConfig config;
    config.endpoint_id = L"{0.0.1.00000000}.{example-endpoint}";
    config.use_default_endpoint = false;
    config.source.identity.id.value = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    config.source.identity.type = fthr::AudioSourceType::Microphone;
    config.source.identity.persistent_identity = "microphone";
    config.source.identity.display_name = "Microphone";
    config.source.format = {48000, 2, "fltp"};
    CheckMicrophone(!config.use_default_endpoint && !config.endpoint_id.empty()
            && config.source.identity.persistent_identity == "microphone"
            && config.source.identity.display_name == "Microphone",
        "a selected endpoint is runtime-only while the portable source remains semantic");
}

void TimelineContinuityBoundsGapAndOverlap() {
    const auto first = fthr::ReconcileAudioTimelinePacket(
        0, 10'000'000, 480, 48000);
    CheckMicrophone(first.silence_frames == 0 && first.skip_input_frames == 0
        && first.next_timeline_100ns == 10'100'000,
        "first packet establishes its native-duration timeline end");
    const auto gap = fthr::ReconcileAudioTimelinePacket(
        first.next_timeline_100ns, 11'100'000, 480, 48000);
    CheckMicrophone(gap.silence_frames == 4800 && gap.gap_100ns == 1'000'000
        && gap.next_timeline_100ns == 11'200'000,
        "temporary packet gap becomes bounded canonical silence");
    const auto overlap = fthr::ReconcileAudioTimelinePacket(
        gap.next_timeline_100ns, 11'150'000, 480, 48000);
    CheckMicrophone(overlap.skip_input_frames == 240
        && overlap.next_timeline_100ns == 11'250'000,
        "overlapping packet prefix is dropped without rewinding the timeline");
    const auto bounded = fthr::ReconcileAudioTimelinePacket(
        gap.next_timeline_100ns, 61'200'000, 480, 48000);
    CheckMicrophone(!bounded.large_gap && bounded.silence_frames == 240'000
            && bounded.next_timeline_100ns == 61'300'000,
        "five-second continuity repair stays bounded at the configured limit");
    const auto large = fthr::ReconcileAudioTimelinePacket(
        gap.next_timeline_100ns, 70'000'000, 480, 48000);
    CheckMicrophone(large.large_gap && large.silence_frames == 0
            && large.next_timeline_100ns == gap.next_timeline_100ns,
        "large gaps preserve the cursor for explicit failure/restart instead "
            "of creating unbounded work or collapsing media time");
}

void ProcessingDiagnosticsUseExplicitTimestampAndBufferThresholds() {
    using fthr::WindowsMicrophoneProcessingDiagnosticsAccumulator;
    CheckMicrophone(
        WindowsMicrophoneProcessingDiagnosticsAccumulator::CaptureBufferDuration100ns(
            480, 48000) == 100'000,
        "480 frames at 48 kHz have a 10 ms capture-buffer duration");
    CheckMicrophone(
        WindowsMicrophoneProcessingDiagnosticsAccumulator::CaptureBufferDuration100ns(
            0, 48000) == 0,
        "an unavailable capture buffer has no derived duration");
    CheckMicrophone(
        WindowsMicrophoneProcessingDiagnosticsAccumulator::ProcessingDurationMeetsBuffer(
            100'000, 100'000),
        "processing exactly one buffer duration meets the diagnostic threshold");
    CheckMicrophone(
        !WindowsMicrophoneProcessingDiagnosticsAccumulator::ProcessingDurationMeetsBuffer(
            99'999, 100'000),
        "processing below one buffer duration does not meet the threshold");
    CheckMicrophone(
        !WindowsMicrophoneProcessingDiagnosticsAccumulator::ProcessingDurationMeetsBuffer(
            100'000, 0),
        "an unavailable buffer duration cannot imply a deadline observation");

    WindowsMicrophoneProcessingDiagnosticsAccumulator diagnostics;
    diagnostics.ConfigureCaptureBuffer(480, 48000);
    diagnostics.ObservePacket(false, 99'999);
    diagnostics.ObservePacket(true, 100'000);
    diagnostics.ObservePacket(true, 100'001);
    const auto snapshot = diagnostics.Snapshot();
    CheckMicrophone(snapshot.capture_buffer_frames == 480
            && snapshot.capture_buffer_duration_100ns == 100'000,
        "capture-buffer topology is retained in the bounded snapshot");
    CheckMicrophone(snapshot.timestamp_error_count == 2,
        "only packets carrying the timestamp-error flag are counted");
    CheckMicrophone(snapshot.max_packet_processing_duration_100ns == 100'001,
        "maximum packet processing duration is monotonic");
    CheckMicrophone(
        snapshot.packet_processing_at_or_above_buffer_duration_count == 2,
        "at-or-above-buffer processing threshold counts equality and overshoot");

    diagnostics.Reset();
    const auto reset = diagnostics.Snapshot();
    CheckMicrophone(reset.capture_buffer_frames == 0
            && reset.capture_buffer_duration_100ns == 0
            && reset.timestamp_error_count == 0
            && reset.max_packet_processing_duration_100ns == 0
            && reset.packet_processing_at_or_above_buffer_duration_count == 0,
        "processing diagnostics reset with a new capture generation");
}

void InputGainPreservesIntentionalOverRangeSemantics() {
    const float source[] = {0.75f, -0.75f};
    float unity[] = {source[0], source[1]};
    fthr::ApplyWindowsMicrophoneInputGain(unity, 2, 1.0f);
    CheckMicrophone(unity[0] == source[0] && unity[1] == source[1],
        "100 percent microphone gain leaves samples unchanged");

    float one_fifty[] = {source[0], source[1]};
    fthr::ApplyWindowsMicrophoneInputGain(one_fifty, 2, 1.5f);
    CheckMicrophone(std::fabs(one_fifty[0] - 1.125f) < 0.000001f
            && std::fabs(one_fifty[1] + 1.125f) < 0.000001f,
        "150 percent microphone gain multiplies samples without limiting");

    float two_hundred[] = {source[0], source[1]};
    fthr::ApplyWindowsMicrophoneInputGain(two_hundred, 2, 2.0f);
    CheckMicrophone(std::fabs(two_hundred[0] - 1.5f) < 0.000001f
            && std::fabs(two_hundred[1] + 1.5f) < 0.000001f,
        "200 percent microphone gain multiplies samples without limiting");

    fthr::AudioSignalMetrics metrics;
    metrics.Observe(two_hundred, 2, 1);
    const auto snapshot = metrics.Snapshot();
    CheckMicrophone(snapshot.peak == 1.5 && snapshot.clipping_samples == 2,
        "post-gain over-range samples remain visible to signal diagnostics");
}

void MonoDownmixCentersStereoWithoutClamping() {
    float samples[] = {1.0f, 0.0f, 0.25f, 0.75f, -0.5f, -1.0f, 2.0f, 2.0f};
    fthr::DownmixWindowsMicrophoneToMono(samples, 8);
    CheckMicrophone(samples[0] == 0.5f && samples[1] == 0.5f,
        "single-channel microphone is centered in both speakers");
    CheckMicrophone(samples[2] == 0.5f && samples[3] == 0.5f
            && samples[4] == -0.75f && samples[5] == -0.75f,
        "stereo microphone channels are averaged per frame");
    CheckMicrophone(samples[6] == 2.0f && samples[7] == 2.0f,
        "mono downmix preserves diagnostic over-range samples");
    fthr::ApplyWindowsMicrophoneInputGain(samples, 8, 1.5f);
    CheckMicrophone(samples[0] == 0.75f && samples[1] == 0.75f,
        "capture gain is applied once after mono downmix");
}

}  // namespace

int RunWindowsMicrophoneAudioProviderTests() {
    QpcTimelineKeepsLateSourcesAtTheirRealOffset();
    EncoderDelayStaysSignedInManifestTimeline();
    DriftCorrectionIsSmallAndBounded();
    ExplicitEndpointIdentityIsNotManifestIdentity();
    TimelineContinuityBoundsGapAndOverlap();
    ProcessingDiagnosticsUseExplicitTimestampAndBufferThresholds();
    InputGainPreservesIntentionalOverRangeSemantics();
    MonoDownmixCentersStereoWithoutClamping();
    std::cout << "AUDIT-050 Windows microphone provider tests: "
              << microphone_checks << " checks passed" << std::endl;
    return microphone_checks;
}
