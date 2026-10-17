#include "audio_format_converter.h"
#include "audio_encoder.h"
#include "audio_signal_metrics.h"
#include "audio_timeline.h"

#include <windows.h>
#include <ks.h>
#include <ksmedia.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/frame.h>
#include <libavutil/mem.h>
#include <libavutil/samplefmt.h>
}

namespace {

int converter_checks = 0;

void CheckConverter(bool condition, const char* message) {
    ++converter_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

WAVEFORMATEXTENSIBLE Extensible(WORD tag, WORD bits, WORD block_align,
                                WORD channels, DWORD sample_rate,
                                DWORD channel_mask, WORD valid_bits = 0) {
    WAVEFORMATEXTENSIBLE format{};
    format.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
    format.Format.nChannels = channels;
    format.Format.nSamplesPerSec = sample_rate;
    format.Format.wBitsPerSample = bits;
    format.Format.nBlockAlign = block_align;
    format.Format.nAvgBytesPerSec = sample_rate * block_align;
    format.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    format.Samples.wValidBitsPerSample = valid_bits == 0 ? bits : valid_bits;
    format.dwChannelMask = channel_mask;
    format.SubFormat = tag == WAVE_FORMAT_IEEE_FLOAT
        ? KSDATAFORMAT_SUBTYPE_IEEE_FLOAT : KSDATAFORMAT_SUBTYPE_PCM;
    return format;
}

void ConvertsIntegerAndFloatFormatsToCanonicalStereo() {
    const auto float_format = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 8, 2,
        48000, SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&float_format.Format),
        "float32 stereo format initializes");
    const float float_input[] = {0.25f, -0.5f, 0.5f, -1.0f};
    std::vector<float> output;
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(float_input),
        2, &output), "float32 stereo converts");
    CheckConverter(converter.sample_rate() == 48000 && converter.channels() == 2,
        "canonical output format is 48 kHz stereo");
    CheckConverter(output.size() == 4 && std::fabs(output[0] - 0.25f) < 0.001f
        && std::fabs(output[3] + 1.0f) < 0.001f,
        "float32 samples preserve channel values");
    const auto decoded_metrics = converter.GetInputSignalMetrics();
    const auto canonical_metrics = converter.GetOutputSignalMetrics();
    CheckConverter(decoded_metrics.frames == 2 && decoded_metrics.samples == 4
        && decoded_metrics.peak >= 1.0,
        "converter exposes decoded native signal metrics");
    CheckConverter(canonical_metrics.frames == 2 && canonical_metrics.channels == 2
        && canonical_metrics.channel_rms[0] > 0.0
        && canonical_metrics.channel_rms[1] > 0.0,
        "converter exposes canonical output signal metrics");

    const auto s16_format = Extensible(WAVE_FORMAT_PCM, 16, 2, 1, 48000,
        SPEAKER_FRONT_CENTER);
    CheckConverter(converter.Initialize(&s16_format.Format),
        "PCM16 mono format initializes");
    const int16_t s16_input[] = {16384, -16384};
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(s16_input),
        2, &output), "PCM16 mono converts");
    CheckConverter(output.size() == 4 && std::fabs(output[0] - 0.5f) < 0.01f
        && std::fabs(output[1] - output[0]) < 0.001f,
        "mono source is duplicated transparently to both channels");

    const auto stereo_format = Extensible(WAVE_FORMAT_PCM, 16, 4, 2, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    CheckConverter(converter.Initialize(&stereo_format.Format),
        "PCM16 stereo format initializes");
    const int16_t stereo_input[] = {16384, -8192};
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(stereo_input),
        1, &output), "PCM16 stereo converts");
    CheckConverter(output.size() == 2 && std::fabs(output[0] - 0.5f) < 0.01f
        && std::fabs(output[1] + 0.25f) < 0.01f,
        "stereo channels remain transparent at canonical format");
}

void ConvertsPacked24AndEightChannelMasks() {
    WAVEFORMATEX packed24{};
    packed24.wFormatTag = WAVE_FORMAT_PCM;
    packed24.nChannels = 2;
    packed24.nSamplesPerSec = 48000;
    packed24.wBitsPerSample = 24;
    packed24.nBlockAlign = 6;
    packed24.nAvgBytesPerSec = 48000 * 6;
    std::vector<uint8_t> packed = {0x00, 0x00, 0x40, 0x00, 0x00, 0xC0};
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&packed24),
        "packed PCM24 stereo format initializes");
    std::vector<float> output;
    CheckConverter(converter.Convert(packed.data(), 1, &output),
        "packed PCM24 converts");
    CheckConverter(output.size() == 2 && output[0] > 0.49f
        && output[1] < -0.49f, "packed PCM24 sign and scale are preserved");

    const auto valid20 = Extensible(WAVE_FORMAT_PCM, 24, 6, 2, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT, 20);
    CheckConverter(converter.Initialize(&valid20.Format),
        "20 valid bits in a packed 24-bit container initializes");
    output.clear();
    CheckConverter(converter.Convert(packed.data(), 1, &output),
        "valid-20-in-24 PCM converts");
    CheckConverter(output.size() == 2 && output[0] > 0.49f
        && output[1] < -0.49f,
        "valid-20-in-24 samples preserve left-aligned sign and scale");

    const auto valid24 = Extensible(WAVE_FORMAT_PCM, 32, 8, 2, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT, 24);
    CheckConverter(converter.Initialize(&valid24.Format),
        "24 valid bits in a 32-bit extensible container initializes");
    const int32_t valid24_input[] = {0x40000000, static_cast<int32_t>(0xC0000000)};
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(valid24_input),
        1, &output), "valid-24-in-32 PCM converts");
    CheckConverter(output.size() == 2 && output[0] > 0.49f
        && output[1] < -0.49f,
        "valid-24-in-32 samples preserve left-aligned sign and scale");

    const auto s32_format = Extensible(WAVE_FORMAT_PCM, 32, 8, 2, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    CheckConverter(converter.Initialize(&s32_format.Format),
        "PCM32 stereo format initializes");
    const int32_t s32_input[] = {0x40000000, static_cast<int32_t>(0xC0000000)};
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(s32_input),
        1, &output), "PCM32 stereo converts");
    CheckConverter(output.size() == 2 && output[0] > 0.49f
        && output[1] < -0.49f, "PCM32 sign and scale are preserved");

    const auto eight = Extensible(WAVE_FORMAT_PCM, 16, 16, 8, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT | SPEAKER_FRONT_CENTER
        | SPEAKER_LOW_FREQUENCY | SPEAKER_BACK_LEFT | SPEAKER_BACK_RIGHT
        | SPEAKER_SIDE_LEFT | SPEAKER_SIDE_RIGHT);
    CheckConverter(converter.Initialize(&eight.Format),
        "8-channel PCM16 format initializes");
    std::vector<int16_t> input(8, 32767);
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input.data()),
        1, &output), "8-channel PCM16 converts");
    CheckConverter(output.size() == 2 && output[0] <= 1.0f && output[1] <= 1.0f,
        "mask-aware eight-channel downmix has bounded headroom");

    const auto five_one = Extensible(WAVE_FORMAT_PCM, 16, 12, 6, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT | SPEAKER_FRONT_CENTER
        | SPEAKER_LOW_FREQUENCY | SPEAKER_BACK_LEFT | SPEAKER_BACK_RIGHT);
    CheckConverter(converter.Initialize(&five_one.Format),
        "5.1 PCM16 format initializes");
    input.assign(6, 32767);
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input.data()),
        1, &output), "5.1 PCM16 converts");
    CheckConverter(output.size() == 2 && std::fabs(output[0]) <= 0.951f
        && std::fabs(output[1]) <= 0.951f,
        "5.1 downmix stays below the documented headroom ceiling");

    const auto seven_one = Extensible(WAVE_FORMAT_PCM, 16, 16, 8, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT | SPEAKER_FRONT_CENTER
        | SPEAKER_LOW_FREQUENCY | SPEAKER_BACK_LEFT | SPEAKER_BACK_RIGHT
        | SPEAKER_SIDE_LEFT | SPEAKER_SIDE_RIGHT);
    CheckConverter(converter.Initialize(&seven_one.Format),
        "7.1 PCM16 format initializes");
    input.assign(8, 32767);
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input.data()),
        1, &output), "7.1 PCM16 converts");
    CheckConverter(output.size() == 2 && std::fabs(output[0]) <= 0.951f
        && std::fabs(output[1]) <= 0.951f,
        "7.1 downmix stays below the documented headroom ceiling");

    const auto sonar_like = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 32, 8, 96000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT | SPEAKER_FRONT_CENTER
        | SPEAKER_LOW_FREQUENCY | SPEAKER_BACK_LEFT | SPEAKER_BACK_RIGHT
        | SPEAKER_SIDE_LEFT | SPEAKER_SIDE_RIGHT);
    CheckConverter(converter.Initialize(&sonar_like.Format),
        "96 kHz eight-channel float format initializes");
    std::vector<float> sonar_input(960 * 8, 0.1f);
    output.clear();
    CheckConverter(converter.Convert(
        reinterpret_cast<const uint8_t*>(sonar_input.data()), 960, &output),
        "96 kHz eight-channel float input converts");
    CheckConverter(!output.empty() && output.size() <= 480 * 2,
        "96 kHz eight-channel conversion is downmixed and resampled once");

    WAVEFORMATEX malformed = Extensible(WAVE_FORMAT_PCM, 16, 4, 2, 48000,
        SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT).Format;
    malformed.nBlockAlign = 8;
    malformed.nAvgBytesPerSec = 48000 * malformed.nBlockAlign;
    CheckConverter(!converter.Initialize(&malformed),
        "malformed padded PCM block alignment is rejected");
}

void ResamplingIsPersistentAndFlushesDelay() {
    WAVEFORMATEX input_format{};
    input_format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    input_format.nChannels = 2;
    input_format.nSamplesPerSec = 44100;
    input_format.wBitsPerSample = 32;
    input_format.nBlockAlign = 8;
    input_format.nAvgBytesPerSec = 44100 * 8;
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&input_format),
        "44.1 kHz format initializes");
    std::vector<float> input(44100 * 2, 0.1f);
    std::vector<float> output;
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input.data()),
        44100, &output), "44.1 kHz input converts");
    const size_t before_flush = output.size();
    CheckConverter(converter.Flush(&output), "resampler flush succeeds");
    CheckConverter(output.size() >= before_flush
        && output.size() >= 48000 * 2 - 128,
        "persistent resampler emits approximately one canonical second");
    const auto resampled_metrics = converter.GetOutputSignalMetrics();
    CheckConverter(resampled_metrics.rms > 0.05 && resampled_metrics.rms < 0.15
        && resampled_metrics.nan_samples == 0
        && resampled_metrics.inf_samples == 0,
        "44.1 kHz resampling preserves constant amplitude without invalid samples");
}

void SilentNativePacketsKeepTheirCanonicalDuration() {
    WAVEFORMATEX input_format{};
    input_format.wFormatTag = WAVE_FORMAT_PCM;
    input_format.nChannels = 2;
    input_format.nSamplesPerSec = 96000;
    input_format.wBitsPerSample = 16;
    input_format.nBlockAlign = 4;
    input_format.nAvgBytesPerSec = 96000 * 4;
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&input_format),
        "96 kHz format initializes");
    std::vector<int16_t> input(96000 * 2, 0);
    std::vector<float> output;
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input.data()),
        96000, &output), "96 kHz silent input converts");
    CheckConverter(converter.Flush(&output), "96 kHz silent resampler flushes");
    CheckConverter(output.size() >= (48000u * 2 - 256)
        && output.size() <= (48000u * 2 + 256),
        "96 kHz silent packet retains one canonical second of duration");
}

void EncoderReportsSubmissionAndFlushResults() {
    fthr::AudioEncoder encoder;
    uint32_t packets = 0;
    CheckConverter(encoder.Initialize(48000, 2, 96,
        [&packets](const uint8_t*, uint32_t size, int64_t) {
            if (size > 0) ++packets;
        }), "AAC encoder initializes for observability test");
    std::vector<float> input(1024 * 2, 0.0f);
    CheckConverter(!encoder.EncodeSamples(input.data(), 3),
        "AAC encoder rejects incomplete interleaved samples");
    CheckConverter(encoder.EncodeSamples(input.data(),
        static_cast<uint32_t>(input.size())), "AAC frame submission succeeds");
    CheckConverter(encoder.stats().frames_submitted == 1,
        "AAC encoder counts submitted frames");
    CheckConverter(encoder.Finalize(), "AAC encoder flush succeeds");
    CheckConverter(encoder.stats().packets_emitted == packets,
        "AAC encoder packet count is observable");
}

void SignalMetricsRemainBoundedAndExposeInvalidSamples() {
    fthr::AudioSignalMetrics metrics;
    const float samples[] = {
        0.0f, 0.0f,
        0.25f, -0.25f,
        1.0f, -1.0f,
        0.00001f, -0.00001f,
        std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
    };
    metrics.Observe(samples, 5, 2);
    const auto snapshot = metrics.Snapshot();
    CheckConverter(snapshot.chunks == 1 && snapshot.frames == 5
        && snapshot.samples == 10, "signal metrics count one stereo chunk");
    CheckConverter(snapshot.nan_samples == 1 && snapshot.inf_samples == 1,
        "signal metrics preserve NaN and infinity counts");
    CheckConverter(snapshot.clipping_samples == 3,
        "signal metrics expose full-scale and infinity clipping");
    CheckConverter(snapshot.near_silence_samples >= 4
        && snapshot.near_silence_ratio > 0.0,
        "signal metrics expose near-silence samples and ratio");
    CheckConverter(snapshot.peak >= 1.0 && snapshot.rms > 0.0
        && snapshot.channel_rms[0] > 0.0 && snapshot.channel_rms[1] > 0.0,
        "signal metrics expose finite peak and per-channel RMS");

    fthr::AudioSignalMetrics silence;
    const float zeros[2 * 480] = {};
    silence.Observe(zeros, 480, 2);
    const auto silence_snapshot = silence.Snapshot();
    CheckConverter(silence_snapshot.near_silence_samples == 960
        && silence_snapshot.near_silence_ratio == 1.0,
        "synthetic zero silence is fully observable");
}

void TimelineMicroGapAndOverlapAreAbsorbed() {
    constexpr uint32_t kRate = 48000;
    constexpr uint32_t kFrames = 480;
    const uint64_t packet_duration = 100000000ULL / 1000ULL;
    const uint64_t next = 10'000'000ULL;

    // The physical BODYCAM runs produced 1-7 frame edits (approximately
    // 0.02-0.146 ms).  These are normal endpoint jitter and must not become
    // literal samples in the production waveform.
    const auto micro_gap = fthr::ReconcileAudioTimelinePacket(
        next, next + 2'500ULL, kFrames, kRate);
    CheckConverter(micro_gap.gap_100ns == 2'500ULL
        && micro_gap.silence_frames == 0
        && micro_gap.normal_jitter_absorbed
        && micro_gap.classification
            == fthr::AudioTimelineAdjustmentClass::NormalJitterAbsorbed,
        "sub-millisecond QPC gap is absorbed as normal jitter");
    fthr::AudioSignalMetrics encoder_input;
    std::vector<float> silence(static_cast<size_t>(micro_gap.silence_frames) * 2, 0.0f);
    encoder_input.Observe(silence.data(), micro_gap.silence_frames, 2);
    const auto silence_metrics = encoder_input.Snapshot();
    CheckConverter(silence_metrics.frames == 0,
        "absorbed jitter does not add synthetic encoder-input frames");

    const auto micro_overlap = fthr::ReconcileAudioTimelinePacket(
        next + packet_duration, next + packet_duration - 2'500ULL,
        kFrames, kRate);
    CheckConverter(micro_overlap.skip_input_frames == 0
        && micro_overlap.normal_jitter_absorbed
        && micro_overlap.timing_error_100ns == -2'500,
        "sub-millisecond QPC overlap is absorbed without dropping input");
}

void TimelinePolicyAbsorbsJitterAndBoundsRecovery() {
    constexpr uint64_t kStart = 50'000'000ULL;
    constexpr uint32_t kFrames = 480;
    constexpr uint32_t kRate = 48000;
    const uint64_t packet_duration = 100'000ULL;

    const auto exact = fthr::ReconcileAudioTimelinePacket(
        kStart, kStart, kFrames, kRate);
    CheckConverter(exact.classification == fthr::AudioTimelineAdjustmentClass::Aligned
        && exact.silence_frames == 0 && exact.skip_input_frames == 0,
        "exact packet cadence remains aligned");

    uint64_t cursor = kStart;
    const int64_t jitter[] = {2'500, -2'500, 7'500, -7'500, 1'000, -1'000};
    bool all_absorbed = true;
    for (const int64_t error : jitter) {
        const uint64_t packet = error >= 0
            ? cursor + packet_duration + static_cast<uint64_t>(error)
            : cursor + packet_duration - static_cast<uint64_t>(-error);
        const auto adjustment = fthr::ReconcileAudioTimelinePacket(
            cursor + packet_duration, packet, kFrames, kRate);
        all_absorbed = all_absorbed && adjustment.normal_jitter_absorbed
            && adjustment.silence_frames == 0
            && adjustment.skip_input_frames == 0;
        cursor = adjustment.next_timeline_100ns;
    }
    CheckConverter(all_absorbed,
        "alternating positive and negative endpoint jitter has no hard edits");

    const auto boundary_gap = fthr::ReconcileAudioTimelinePacket(
        kStart, kStart + fthr::kAudioTimelineNormalJitterTolerance100ns,
        kFrames, kRate);
    const auto hard_gap = fthr::ReconcileAudioTimelinePacket(
        kStart, kStart + fthr::kAudioTimelineNormalJitterTolerance100ns + 1,
        kFrames, kRate);
    CheckConverter(boundary_gap.normal_jitter_absorbed
        && boundary_gap.silence_frames == 0
        && hard_gap.hard_discontinuity
        && hard_gap.classification == fthr::AudioTimelineAdjustmentClass::HardGap
        && hard_gap.silence_frames == 48,
        "one millisecond is the exclusive normal-jitter boundary");

    const auto hard_overlap = fthr::ReconcileAudioTimelinePacket(
        kStart + packet_duration, kStart + packet_duration - 25'000,
        kFrames, kRate);
    CheckConverter(hard_overlap.hard_discontinuity
        && hard_overlap.classification
            == fthr::AudioTimelineAdjustmentClass::HardOverlap
        && hard_overlap.skip_input_frames == 120,
        "packet-sized overlap uses bounded hard recovery");

    const auto large_gap = fthr::ReconcileAudioTimelinePacket(
        kStart, kStart + 5ULL * 10'000'000ULL + 1,
        kFrames, kRate);
    CheckConverter(large_gap.large_gap
        && large_gap.classification == fthr::AudioTimelineAdjustmentClass::LargeGap
        && large_gap.silence_frames == 0,
        "suspend-sized gap remains an unrecoverable discontinuity");

    const auto full_overlap = fthr::ReconcileAudioTimelinePacket(
        kStart + packet_duration, kStart, kFrames, kRate);
    CheckConverter(full_overlap.skip_input_frames == kFrames,
        "full duplicate packet is bounded by the input frame count");

    for (const uint32_t rate : {44100U, 48000U, 96000U}) {
        const uint32_t frames = rate / 100;
        const auto resampled = fthr::ReconcileAudioTimelinePacket(
            kStart, kStart + 7'500, frames, rate, kRate);
        CheckConverter(resampled.normal_jitter_absorbed
            && resampled.silence_frames == 0
            && resampled.skip_input_frames == 0,
            "normal jitter policy is stable across 44.1/48/96 kHz");
    }

    fthr::AudioDriftController drift(kRate, 100);
    drift.Observe(kStart, 0);
    const auto bounded = drift.Observe(kStart + 10'000'000ULL, 47'995);
    CheckConverter(bounded.sample_delta != 0
        && std::abs(bounded.sample_delta) <= 5
        && bounded.compensation_distance_samples >= 24'000,
        "sustained clock drift uses bounded SWR compensation");
}

struct TimelineSimulationResult {
    const char* source = "unknown";
    uint32_t duration_seconds = 0;
    uint64_t hard_insert_count = 0;
    uint64_t hard_skip_count = 0;
    uint64_t hard_insert_frames = 0;
    uint64_t hard_skip_frames = 0;
    uint64_t swr_operations = 0;
    int64_t final_timeline_error_frames = 0;
    int64_t maximum_timeline_error_frames = 0;
    double output_duration_error_ms = 0.0;
};

TimelineSimulationResult SimulateTimeline(const char* source,
                                           uint32_t duration_seconds) {
    constexpr uint32_t kRate = 48000;
    constexpr uint32_t kPacketFrames = 480;
    constexpr uint64_t kPacketDuration100ns = 100'000ULL;
    constexpr uint64_t kStartQpc100ns = 100'000'000ULL;
    // 8 x 100 ns per 10 ms packet is an 80 ppm source-clock deviation. It is
    // intentionally small enough to remain in the normal jitter zone while
    // still exercising the real drift controller over the long runs.
    constexpr int64_t kClockDrift100nsPerPacket = 8;
    const uint64_t packet_count = static_cast<uint64_t>(duration_seconds) * 100;

    TimelineSimulationResult result{source, duration_seconds};
    fthr::AudioDriftController drift(kRate, 100);
    uint64_t timeline_cursor = kStartQpc100ns;
    int64_t submitted_frames = 0;
    int64_t maximum_error = 0;
    bool stayed_bounded = true;
    uint64_t last_packet_end_qpc = kStartQpc100ns;

    for (uint64_t packet_index = 0; packet_index < packet_count; ++packet_index) {
        const int64_t jitter_frames = static_cast<int64_t>(packet_index % 7) + 1;
        const int64_t signed_jitter_frames = (packet_index % 2) == 0
            ? jitter_frames : -jitter_frames;
        const int64_t nominal_packet_qpc = static_cast<int64_t>(kStartQpc100ns)
            + static_cast<int64_t>(packet_index)
                * static_cast<int64_t>(kPacketDuration100ns
                    + kClockDrift100nsPerPacket);
        const int64_t packet_qpc_signed = nominal_packet_qpc
            + signed_jitter_frames * 208;
        const uint64_t packet_qpc = static_cast<uint64_t>(packet_qpc_signed);
        const auto timeline = fthr::ReconcileAudioTimelinePacket(
            timeline_cursor, packet_qpc, kPacketFrames, kRate);
        if (timeline.silence_frames > 0) {
            ++result.hard_insert_count;
            result.hard_insert_frames += timeline.silence_frames;
        }
        if (timeline.skip_input_frames > 0) {
            ++result.hard_skip_count;
            result.hard_skip_frames += timeline.skip_input_frames;
        }
        stayed_bounded = stayed_bounded && !timeline.large_gap;
        timeline_cursor = timeline.next_timeline_100ns;

        const auto correction = drift.Observe(packet_qpc, submitted_frames);
        if (correction.sample_delta != 0
                && correction.compensation_distance_samples > 0) {
            ++result.swr_operations;
            // This is a compact model of the real persistent SWR operation:
            // the returned bounded sample delta is applied over its reported
            // compensation distance without retaining any audio samples.
            submitted_frames += correction.sample_delta;
        }
        submitted_frames += static_cast<int64_t>(kPacketFrames
            - timeline.skip_input_frames);

        const uint64_t packet_end = packet_qpc + kPacketDuration100ns;
        last_packet_end_qpc = packet_end;
        const int64_t expected_frames = static_cast<int64_t>(std::llround(
            static_cast<double>(packet_end - kStartQpc100ns)
                * static_cast<double>(kRate) / 10'000'000.0));
        const int64_t error = submitted_frames - expected_frames;
        maximum_error = std::max(maximum_error, std::llabs(error));
    }

    const int64_t expected_final_frames = static_cast<int64_t>(std::llround(
        static_cast<double>(last_packet_end_qpc - kStartQpc100ns)
            * static_cast<double>(kRate) / 10'000'000.0));
    result.final_timeline_error_frames = submitted_frames - expected_final_frames;
    result.maximum_timeline_error_frames = maximum_error;
    result.output_duration_error_ms = static_cast<double>(
        result.final_timeline_error_frames) * 1000.0 / kRate;
    CheckConverter(stayed_bounded,
        "timeline simulation stays within bounded recovery range");

    std::cout << std::fixed << std::setprecision(6)
              << "AUDIO_TIMELINE_SIM source=" << result.source
              << " duration_s=" << result.duration_seconds
              << " hard_insert_count=" << result.hard_insert_count
              << " hard_skip_count=" << result.hard_skip_count
              << " hard_insert_frames=" << result.hard_insert_frames
              << " hard_skip_frames=" << result.hard_skip_frames
              << " hard_frames=" << (result.hard_insert_frames
                    + result.hard_skip_frames)
              << " swr_operations=" << result.swr_operations
              << " final_timeline_error_ms="
              << static_cast<double>(result.final_timeline_error_frames)
                    * 1000.0 / kRate
              << " maximum_timeline_error_ms="
              << static_cast<double>(result.maximum_timeline_error_frames)
                    * 1000.0 / kRate
              << " output_duration_error_ms="
              << result.output_duration_error_ms << std::endl;
    return result;
}

void LongRunTimelinePolicyRemainsBounded() {
    for (const char* source : {"system", "microphone"}) {
        for (const uint32_t duration : {30U, 60U, 300U}) {
            const auto result = SimulateTimeline(source, duration);
            CheckConverter(result.hard_insert_count == 0
                && result.hard_skip_count == 0
                && result.hard_insert_frames == 0
                && result.hard_skip_frames == 0,
                "physical 1-7 frame jitter produces no hard timeline edits");
            CheckConverter(result.swr_operations > 0
                && result.maximum_timeline_error_frames <= 48,
                "long-run source drift remains bounded by the SWR controller");
            CheckConverter(std::fabs(result.output_duration_error_ms) <= 1.0,
                "long-run output duration error remains below one millisecond");
        }
    }
}

void TimelineSourcesRestartIndependentlyAndRejectRegression() {
    const auto system_before_restart = SimulateTimeline("system_restart_before", 30);
    const auto microphone_after_restart = SimulateTimeline(
        "microphone_restart_after", 30);
    CheckConverter(system_before_restart.hard_insert_count == 0
        && microphone_after_restart.hard_skip_count == 0,
        "system and microphone restart generations keep independent timelines");

    constexpr uint64_t kExpected = 80'000'000ULL;
    const auto regression = fthr::ReconcileAudioTimelinePacket(
        kExpected, kExpected - 100'000ULL, 480, 48000);
    CheckConverter(regression.classification
            == fthr::AudioTimelineAdjustmentClass::HardOverlap
        && regression.skip_input_frames == 480,
        "timestamp regression outside jitter tolerance uses bounded hard overlap");
    const auto restarted = fthr::ReconcileAudioTimelinePacket(
        0, kExpected + 100'000ULL, 480, 48000);
    CheckConverter(restarted.classification == fthr::AudioTimelineAdjustmentClass::Aligned
        && restarted.next_timeline_100ns > 0,
        "new source generation establishes a fresh timeline origin");
    std::cout << "AUDIO_TIMELINE_RESTART regression_class="
              << fthr::AudioTimelineAdjustmentClassName(regression.classification)
              << " regression_skip_frames=" << regression.skip_input_frames
              << " new_generation_class="
              << fthr::AudioTimelineAdjustmentClassName(restarted.classification)
              << std::endl;
}

void ConverterPreservesQuietSineAndStereoMultitone() {
    const auto format = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 8, 2,
        48000, SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&format.Format),
        "48 kHz float stereo format initializes for signal fidelity");

    constexpr uint32_t frames = 4096;
    constexpr double kPi = 3.14159265358979323846;
    std::vector<float> input(static_cast<size_t>(frames) * 2);
    for (uint32_t frame = 0; frame < frames; ++frame) {
        const double time = static_cast<double>(frame) / 48000.0;
        input[static_cast<size_t>(frame) * 2] = static_cast<float>(
            0.18 * std::sin(2.0 * kPi * 440.0 * time)
            + 0.04 * std::sin(2.0 * kPi * 1320.0 * time));
        input[static_cast<size_t>(frame) * 2 + 1] = static_cast<float>(
            0.11 * std::sin(2.0 * kPi * 220.0 * time)
            + 0.03 * std::sin(2.0 * kPi * 880.0 * time));
    }
    std::vector<float> output;
    // Deliberately awkward boundaries model variable WASAPI packet sizes.
    const uint32_t chunks[] = {257, 1023, 17, 777, 1, 511, 1510};
    uint32_t offset = 0;
    for (const uint32_t chunk : chunks) {
        CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(
            input.data() + static_cast<size_t>(offset) * 2), chunk, &output),
            "awkward 48 kHz chunk converts");
        offset += chunk;
    }
    CheckConverter(offset == frames && output.size() == input.size(),
        "48 kHz passthrough preserves duration across awkward chunks");
    double left_error = 0.0;
    double right_error = 0.0;
    for (size_t sample = 0; sample < input.size(); sample += 2) {
        left_error += std::fabs(static_cast<double>(output[sample] - input[sample]));
        right_error += std::fabs(static_cast<double>(
            output[sample + 1] - input[sample + 1]));
    }
    CheckConverter(left_error / frames < 0.00001 && right_error / frames < 0.00001,
        "48 kHz stereo channels preserve low-level sine/multitone shape");
    const auto metrics = converter.GetOutputSignalMetrics();
    CheckConverter(metrics.frames == frames && metrics.rms > 0.04
        && metrics.channel_rms[0] > metrics.channel_rms[1]
        && metrics.channel_rms[1] > 0.04,
        "stereo signal metrics preserve channel energy ordering");

    const float quiet_input[] = {
        0.00001f, -0.00002f, 0.00002f, -0.00001f,
        -0.00001f, 0.00002f, -0.00002f, 0.00001f,
    };
    CheckConverter(converter.Initialize(&format.Format),
        "48 kHz format reinitializes for quiet signal");
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(quiet_input),
        4, &output), "very quiet stereo input converts");
    const auto quiet_metrics = converter.GetOutputSignalMetrics();
    CheckConverter(quiet_metrics.peak < 0.0001
        && quiet_metrics.near_silence_ratio == 1.0,
        "very quiet signal remains quiet and is not amplified");

    const auto mono_format = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 4, 1,
        48000, SPEAKER_FRONT_CENTER);
    CheckConverter(converter.Initialize(&mono_format.Format),
        "48 kHz mono format initializes for duplication check");
    std::vector<float> mono_input(1024);
    for (uint32_t frame = 0; frame < mono_input.size(); ++frame) {
        mono_input[frame] = static_cast<float>(0.2 * std::sin(
            2.0 * kPi * 330.0 * static_cast<double>(frame) / 48000.0));
    }
    output.clear();
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(
        mono_input.data()), static_cast<uint32_t>(mono_input.size()), &output),
        "mono float input converts to canonical stereo");
    CheckConverter(output.size() == mono_input.size() * 2,
        "mono conversion preserves frame count while expanding channels");
    double mono_error = 0.0;
    for (size_t frame = 0; frame < mono_input.size(); ++frame) {
        mono_error += std::fabs(static_cast<double>(output[frame * 2]
            - output[frame * 2 + 1]));
    }
    CheckConverter(mono_error / mono_input.size() < 0.00001,
        "mono conversion duplicates the same signal to both channels");
}

void FirstEqualRateCompensationKeepsContinuousSignalBounded() {
    const auto format = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 8, 2,
        48000, SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&format.Format),
        "48 kHz float stereo format initializes for equal-rate compensation");

    constexpr uint32_t chunk_frames = 480;
    constexpr uint32_t warmup_chunks = 8;
    constexpr uint32_t post_compensation_chunks = 16;
    constexpr double kPi = 3.14159265358979323846;
    constexpr double kFrequency = 220.0;
    constexpr float kAmplitude = 0.05f;
    std::vector<float> output;
    uint32_t input_frames = 0;
    auto convert_chunk = [&](uint32_t first_frame) {
        std::vector<float> input(static_cast<size_t>(chunk_frames) * 2);
        for (uint32_t frame = 0; frame < chunk_frames; ++frame) {
            const double time = static_cast<double>(first_frame + frame) / 48000.0;
            const float value = static_cast<float>(kAmplitude * std::sin(
                2.0 * kPi * kFrequency * time));
            input[static_cast<size_t>(frame) * 2] = value;
            input[static_cast<size_t>(frame) * 2 + 1] = value;
        }
        CheckConverter(converter.Convert(
            reinterpret_cast<const uint8_t*>(input.data()), chunk_frames, &output),
            "equal-rate sine chunk converts before and after compensation");
        input_frames += chunk_frames;
    };

    for (uint32_t chunk = 0; chunk < warmup_chunks; ++chunk) {
        convert_chunk(chunk * chunk_frames);
    }
    const size_t output_before_compensation = output.size();
    CheckConverter(converter.ApplyDriftCorrection(1, 24'000),
        "the production-bounded first equal-rate compensation is accepted");
    for (uint32_t chunk = 0; chunk < post_compensation_chunks; ++chunk) {
        convert_chunk((warmup_chunks + chunk) * chunk_frames);
    }

    const size_t output_frames = output.size() / 2;
    CheckConverter(output.size() % 2 == 0 && output_frames <= input_frames + 64
            && output_frames + 64 >= input_frames,
        "first equal-rate compensation keeps frame accounting within bounded delay");
    double largest_step = 0.0;
    double largest_stereo_difference = 0.0;
    for (size_t frame = 0; frame < output_frames; ++frame) {
        const float left = output[frame * 2];
        const float right = output[frame * 2 + 1];
        CheckConverter(std::isfinite(left) && std::isfinite(right),
            "equal-rate compensation output remains finite");
        largest_stereo_difference = std::max(largest_stereo_difference,
            std::fabs(static_cast<double>(left) - static_cast<double>(right)));
        if (frame > 0) {
            largest_step = std::max(largest_step,
                std::fabs(static_cast<double>(left) -
                    static_cast<double>(output[(frame - 1) * 2])));
        }
    }
    CheckConverter(largest_stereo_difference < 0.00001,
        "equal-rate compensation preserves a matching stereo source");
    CheckConverter(largest_step < 0.01,
        "first equal-rate compensation has no gross sample discontinuity");
    CheckConverter(output.size() > output_before_compensation,
        "equal-rate compensation produces post-compensation output");
}

void HighLevelInputIsClampedWithoutNumericalWrap() {
    const auto format = Extensible(WAVE_FORMAT_IEEE_FLOAT, 32, 8, 2,
        48000, SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT);
    fthr::AudioFormatConverter converter;
    CheckConverter(converter.Initialize(&format.Format),
        "float format initializes for high-level input");
    const float input[] = {2.0f, -2.0f, 1.5f, -1.5f};
    std::vector<float> output;
    CheckConverter(converter.Convert(reinterpret_cast<const uint8_t*>(input),
        2, &output), "high-level input converts");
    CheckConverter(output.size() == 4 && std::isfinite(output[0])
        && std::isfinite(output[1]) && std::isfinite(output[2])
        && std::isfinite(output[3]) && output[0] <= 1.0f
        && output[1] >= -1.0f && output[2] <= 1.0f && output[3] >= -1.0f,
        "high-level signal is bounded without integer-style wraparound");
    CheckConverter(converter.GetInputSignalMetrics().clipping_samples == 4,
        "high-level input clipping remains observable at the decoded stage");
}

void ObserveDecodedFrame(const AVFrame* frame, fthr::AudioSignalMetrics* metrics) {
    CheckConverter(frame && metrics && frame->ch_layout.nb_channels == 2,
        "AAC decoder returns stereo frames");
    const auto format = static_cast<AVSampleFormat>(frame->format);
    const int bytes = av_get_bytes_per_sample(format);
    CheckConverter(bytes == static_cast<int>(sizeof(float))
        && (format == AV_SAMPLE_FMT_FLTP || format == AV_SAMPLE_FMT_FLT),
        "AAC decoder returns float samples for round-trip metric");
    std::vector<float> interleaved(static_cast<size_t>(frame->nb_samples) * 2);
    for (int sample = 0; sample < frame->nb_samples; ++sample) {
        for (int channel = 0; channel < 2; ++channel) {
            const float* source = format == AV_SAMPLE_FMT_FLTP
                ? reinterpret_cast<const float*>(frame->extended_data[channel])
                : reinterpret_cast<const float*>(frame->extended_data[0]);
            interleaved[static_cast<size_t>(sample) * 2 + channel] =
                format == AV_SAMPLE_FMT_FLTP
                    ? source[sample]
                    : source[static_cast<size_t>(sample) * 2 + channel];
        }
    }
    metrics->Observe(interleaved.data(), static_cast<uint32_t>(frame->nb_samples), 2);
}

void AacRoundTripPreservesFiniteSignalMetrics() {
    struct PacketCopy {
        std::vector<uint8_t> data;
        int64_t pts = 0;
    };
    std::vector<PacketCopy> packets;
    fthr::AudioEncoder encoder;
    CheckConverter(encoder.Initialize(48000, 2, 96,
        [&packets](const uint8_t* data, uint32_t size, int64_t pts) {
            packets.push_back({std::vector<uint8_t>(data, data + size), pts});
        }), "AAC encoder initializes for round-trip metrics");

    constexpr uint32_t frames = 4096;
    constexpr double kPi = 3.14159265358979323846;
    std::vector<float> input(static_cast<size_t>(frames) * 2);
    for (uint32_t frame = 0; frame < frames; ++frame) {
        const double time = static_cast<double>(frame) / 48000.0;
        input[static_cast<size_t>(frame) * 2] = static_cast<float>(
            0.20 * std::sin(2.0 * kPi * 440.0 * time)
            + 0.08 * std::sin(2.0 * kPi * 1760.0 * time));
        input[static_cast<size_t>(frame) * 2 + 1] = static_cast<float>(
            0.17 * std::sin(2.0 * kPi * 330.0 * time)
            + 0.05 * std::sin(2.0 * kPi * 990.0 * time));
    }
    fthr::AudioSignalMetrics source_metrics;
    source_metrics.Observe(input.data(), frames, 2);
    const auto source_snapshot = source_metrics.Snapshot();
    const uint32_t chunks[] = {257, 1023, 777, 1, 2038};
    uint32_t offset = 0;
    for (const uint32_t chunk : chunks) {
        CheckConverter(encoder.EncodeSamples(input.data() +
            static_cast<size_t>(offset) * 2, chunk * 2),
            "AAC encoder accepts awkward interleaved chunks");
        offset += chunk;
    }
    CheckConverter(offset == frames && encoder.Finalize() && !packets.empty(),
        "AAC encoder finalizes and emits packets");
    const auto extradata = encoder.GetExtradata();
    CheckConverter(!extradata.empty(), "AAC round-trip has decoder configuration");

    const AVCodec* decoder = avcodec_find_decoder(AV_CODEC_ID_AAC);
    CheckConverter(decoder != nullptr, "AAC decoder is available");
    AVCodecContext* context = avcodec_alloc_context3(decoder);
    CheckConverter(context != nullptr, "AAC decoder context allocates");
    context->sample_rate = 48000;
    av_channel_layout_default(&context->ch_layout, 2);
    context->extradata = static_cast<uint8_t*>(av_mallocz(
        extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
    CheckConverter(context->extradata != nullptr, "AAC decoder extradata allocates");
    std::memcpy(context->extradata, extradata.data(), extradata.size());
    context->extradata_size = static_cast<int>(extradata.size());
    CheckConverter(avcodec_open2(context, decoder, nullptr) >= 0,
        "AAC decoder opens with encoder configuration");
    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    CheckConverter(frame != nullptr && packet != nullptr,
        "AAC decoder frame and packet allocate");
    fthr::AudioSignalMetrics decoded_metrics;
    uint64_t decoded_frames = 0;
    auto drain = [&]() {
        for (;;) {
            const int result = avcodec_receive_frame(context, frame);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) break;
            CheckConverter(result >= 0, "AAC decoder receives a valid frame");
            ObserveDecodedFrame(frame, &decoded_metrics);
            decoded_frames += static_cast<uint64_t>(frame->nb_samples);
        }
    };
    for (const PacketCopy& encoded : packets) {
        CheckConverter(av_new_packet(packet,
            static_cast<int>(encoded.data.size())) >= 0,
            "AAC decoder packet allocates");
        std::memcpy(packet->data, encoded.data.data(), encoded.data.size());
        packet->pts = encoded.pts;
        CheckConverter(avcodec_send_packet(context, packet) >= 0,
            "AAC decoder accepts encoded packet");
        drain();
        av_packet_unref(packet);
    }
    CheckConverter(avcodec_send_packet(context, nullptr) >= 0,
        "AAC decoder flush starts");
    drain();
    const auto decoded_snapshot = decoded_metrics.Snapshot();
    CheckConverter(decoded_frames >= frames
        && decoded_snapshot.rms >= source_snapshot.rms * 0.4
        && decoded_snapshot.rms <= source_snapshot.rms * 1.6
        && decoded_snapshot.peak <= 1.0 + 0.01
        && decoded_snapshot.nan_samples == 0 && decoded_snapshot.inf_samples == 0,
        "AAC round-trip preserves finite non-silent signal metrics");
    CheckConverter(decoded_snapshot.channel_rms[0] > 0.01
        && decoded_snapshot.channel_rms[1] > 0.01,
        "AAC round-trip preserves both channel energies");
    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&context);
}

void AvTimelineEvidencePreservesRelativeTiming() {
    using fthr::MediaTimelineRange;
    const auto compare = [](MediaTimelineRange video, MediaTimelineRange audio,
                            bool continuous = true) {
        return fthr::EvaluateAvSyncTimeline(
            100.0, 130.0, video, audio, continuous);
    };

    const auto aligned = compare({100.0, 130.0, true}, {100.0, 130.0, true});
    CheckConverter(aligned.valid && std::fabs(aligned.av_start_delta_ms) < 0.001
        && std::fabs(aligned.av_end_delta_ms) < 0.001,
        "aligned A/V timeline reports zero edge deltas");

    const auto starts_late = compare(
        {100.0, 130.0, true}, {100.5, 130.0, true});
    CheckConverter(starts_late.valid
        && std::fabs(starts_late.av_start_delta_ms - 500.0) < 0.001,
        "audio starting 500 ms late remains visible");
    const auto starts_early = compare(
        {100.0, 130.0, true}, {99.5, 130.0, true});
    CheckConverter(starts_early.valid
        && std::fabs(starts_early.av_start_delta_ms + 500.0) < 0.001,
        "audio starting 500 ms early remains visible");

    const auto ends_late = compare(
        {100.0, 130.0, true}, {100.0, 130.5, true});
    CheckConverter(ends_late.valid
        && std::fabs(ends_late.av_end_delta_ms - 500.0) < 0.001,
        "audio ending 500 ms late remains visible");
    const auto ends_early = compare(
        {100.0, 130.0, true}, {100.0, 129.5, true});
    CheckConverter(ends_early.valid
        && std::fabs(ends_early.av_end_delta_ms + 500.0) < 0.001,
        "audio ending 500 ms early remains visible");

    const auto audio_29 = compare(
        {100.0, 130.0, true}, {100.0, 129.0, true});
    CheckConverter(audio_29.valid
        && std::fabs(audio_29.video_duration_s - 30.0) < 0.001
        && std::fabs(audio_29.audio_duration_s - 29.0) < 0.001,
        "30-second video and 29-second audio stay distinct");
    const auto video_29 = compare(
        {100.0, 129.0, true}, {100.0, 130.0, true});
    CheckConverter(video_29.valid
        && std::fabs(video_29.video_duration_s - 29.0) < 0.001
        && std::fabs(video_29.audio_duration_s - 30.0) < 0.001,
        "29-second video and 30-second audio stay distinct");

    const auto reset = compare(
        {100.0, 130.0, true}, {100.0, 130.0, true}, false);
    CheckConverter(!reset.valid && !reset.generation_continuous,
        "capture-generation reset invalidates sync evidence");

    const auto system = compare(
        {100.0, 130.0, true}, {100.0, 130.0, true});
    const auto microphone = compare(
        {100.0, 130.0, true}, {100.25, 129.75, true});
    CheckConverter(system.valid && microphone.valid
        && std::fabs(microphone.av_start_delta_ms - 250.0) < 0.001
        && std::fabs(microphone.av_end_delta_ms + 250.0) < 0.001,
        "system and microphone source offsets remain independently observable");

    // The authoritative comparison is performed before mux rebasing. Rebasing
    // each stream to zero would erase this intentional half-second offset.
    CheckConverter(std::fabs(starts_late.av_start_delta_ms) > 499.0,
        "independent zero rebasing cannot replace pre-mux relative evidence");
}

void CorrectionDistributionsRemainBoundedAndSigned() {
    fthr::AudioCorrectionDistribution corrections;
    for (const int64_t value : {1, 3, 12, 48, 200, 600, -4}) {
        corrections.Observe(value);
    }
    const auto snapshot = corrections.Snapshot();
    CheckConverter(snapshot.event_count == 7
        && snapshot.total_magnitude_frames == 868
        && snapshot.signed_total_frames == 860
        && snapshot.max_single_frames == 600,
        "correction distribution retains totals, sign and maximum");
    CheckConverter(snapshot.buckets[0] == 1 && snapshot.buckets[1] == 2
        && snapshot.buckets[2] == 1 && snapshot.buckets[3] == 1
        && snapshot.buckets[4] == 1 && snapshot.buckets[5] == 1,
        "correction distribution uses stable bounded size buckets");
    corrections.Reset();
    CheckConverter(corrections.Snapshot().event_count == 0,
        "correction distribution reset clears a new capture generation");
}

void ContinuityCorrectionsMapToClipAndRemainBounded() {
    fthr::AudioCorrectionEventRing system;
    fthr::AudioCorrectionEventRing microphone;
    const auto record = [](fthr::AudioCorrectionEventRing& ring,
                           fthr::AudioCorrectionSource source,
                           fthr::AudioCorrectionDirection direction,
                           uint64_t qpc, uint32_t frames) {
        ring.Record({source, direction, qpc, qpc + 100, qpc - 100, frames,
            48000, 0.0, 0, 0.0, false});
    };

    // The selected replay window is [1.0 s, 2.0 s). The event exactly at
    // the end is intentionally excluded by the half-open contract.
    record(system, fthr::AudioCorrectionSource::System,
        fthr::AudioCorrectionDirection::Insert, 9'900'000, 3);
    record(system, fthr::AudioCorrectionSource::System,
        fthr::AudioCorrectionDirection::Insert, 10'100'000, 12);
    record(system, fthr::AudioCorrectionSource::System,
        fthr::AudioCorrectionDirection::Skip, 10'900'000, 48);
    record(system, fthr::AudioCorrectionSource::System,
        fthr::AudioCorrectionDirection::Insert, 15'000'000, 5);
    record(microphone, fthr::AudioCorrectionSource::Microphone,
        fthr::AudioCorrectionDirection::Skip, 10'080'000, 20);
    record(microphone, fthr::AudioCorrectionSource::Microphone,
        fthr::AudioCorrectionDirection::Skip, 20'000'000, 99);
    record(microphone, fthr::AudioCorrectionSource::Microphone,
        fthr::AudioCorrectionDirection::Insert, 20'100'000, 101);

    const auto system_snapshot = system.Snapshot();
    const auto microphone_snapshot = microphone.Snapshot();
    CheckConverter(system_snapshot.events[0].cumulative_signed_correction_frames == 3
        && system_snapshot.events[1].cumulative_signed_correction_frames == 15
        && system_snapshot.events[2].cumulative_signed_correction_frames == -33
        && system_snapshot.events[3].cumulative_signed_correction_frames == -28,
        "system events retain cumulative signed correction frames");
    CheckConverter(std::fabs(system_snapshot.events[1]
                .time_since_previous_correction_ms - 20.0) < 0.0001
        && std::fabs(system_snapshot.events[2]
                .time_since_previous_correction_ms - 80.0) < 0.0001
        && std::fabs(system_snapshot.events[3]
                .time_since_previous_correction_ms - 410.0) < 0.0001
        && std::fabs(microphone_snapshot.events[1]
                .time_since_previous_correction_ms - 992.0) < 0.0001,
        "correction events retain exact inter-event milliseconds");
    const auto summary = fthr::SummarizeAudioCorrections(
        system_snapshot, microphone_snapshot, 10'000'000, 20'000'000);
    CheckConverter(summary.events.size() == 4,
        "clip summary excludes corrections before, at, and after replay end");
    CheckConverter(summary.system_corrections_inside_clip == 3
        && summary.mic_corrections_inside_clip == 1,
        "clip summary keeps system and microphone corrections separate");
    CheckConverter(summary.system_inserted_frames_inside_clip == 17
        && summary.system_skipped_frames_inside_clip == 48
        && summary.mic_inserted_frames_inside_clip == 0
        && summary.mic_skipped_frames_inside_clip == 20,
        "clip summary distinguishes insert and skip frame totals");
    CheckConverter(std::fabs(summary.events.front().clip_relative_time_ms - 8.0)
            < 0.001,
        "QPC correction maps to clip-relative milliseconds");
    CheckConverter(summary.largest_system_correction_inside_clip.present
        && summary.largest_system_correction_inside_clip.correction_frames == 48
        && summary.largest_system_correction_inside_clip.correction_qpc_100ns
            == 10'900'000
        && std::fabs(summary.largest_system_correction_inside_clip
                .clip_relative_time_ms - 90.0) < 0.001
        && summary.largest_mic_correction_inside_clip.correction_frames == 20,
        "largest correction retains source, QPC, and clip timestamp");
    CheckConverter(summary.maximum_density_250ms.event_count == 3
        && summary.maximum_density_250ms.corrected_frames == 80
        && summary.maximum_density_1s.event_count == 4
        && summary.maximum_density_1s.corrected_frames == 85,
        "clip summary computes bounded combined correction density windows");

    fthr::AudioCorrectionEventRing bounded;
    for (size_t index = 0; index < fthr::kAudioCorrectionEventRingCapacity + 7;
            ++index) {
        record(bounded, fthr::AudioCorrectionSource::System,
            fthr::AudioCorrectionDirection::Insert,
            1'000'000 + static_cast<uint64_t>(index), 1);
    }
    const auto bounded_snapshot = bounded.Snapshot();
    CheckConverter(bounded_snapshot.events.size()
            == fthr::kAudioCorrectionEventRingCapacity
        && bounded_snapshot.overwritten_count == 7
        && bounded_snapshot.events.front().correction_qpc_100ns == 1'000'007
        && bounded_snapshot.events.back().correction_qpc_100ns == 1'001'030,
        "correction event retention is circular, chronological, and bounded");
    CheckConverter(bounded_snapshot.events.front()
            .time_since_previous_correction_ms >= 0.0,
        "bounded correction events retain non-negative inter-event timing");
}

void RepeatedHardContinuityEditsExposeBoundaryEnergy() {
    constexpr uint32_t kRate = 48000;
    constexpr uint32_t kChunk = 480;
    constexpr uint32_t kEdit = 12;
    constexpr uint32_t kChunks = 40;
    constexpr double kPi = 3.14159265358979323846;
    std::vector<float> clean(kChunk * kChunks);
    for (size_t frame = 0; frame < clean.size(); ++frame) {
        clean[frame] = static_cast<float>(0.4 * std::sin(
            2.0 * kPi * 997.0 * static_cast<double>(frame) / kRate));
    }
    std::vector<float> edited;
    edited.reserve(clean.size());
    for (uint32_t chunk = 0; chunk < kChunks; ++chunk) {
        const size_t base = static_cast<size_t>(chunk) * kChunk;
        edited.insert(edited.end(), kEdit, 0.0f);
        edited.insert(edited.end(), clean.begin() + base + kEdit,
            clean.begin() + base + kChunk);
    }
    auto boundary_energy = [](const std::vector<float>& samples) {
        double energy = 0.0;
        for (size_t boundary = kChunk; boundary < samples.size();
                boundary += kChunk) {
            const double jump = static_cast<double>(samples[boundary])
                - static_cast<double>(samples[boundary - 1]);
            energy += jump * jump;
        }
        return energy;
    };
    CheckConverter(edited.size() == clean.size()
        && boundary_energy(edited) > boundary_energy(clean) * 20.0,
        "repeated hard insert/skip edits create measurable boundary energy");
}

constexpr uint32_t kBodycamRate = 48000;
constexpr uint32_t kBodycamChannels = 2;
constexpr uint32_t kBodycamChunkFrames = 480;
constexpr uint32_t kBodycamEditFrames = 12;
constexpr uint32_t kBodycamChunks = 40;
constexpr size_t kBodycamDenseFrames = static_cast<size_t>(kBodycamChunkFrames)
    * kBodycamChunks;
// Derived field-rate scenario: 112 total events over 20 seconds, alternating
// 56 inserts and 56 skips. This is 5.6 events/s and 67.2 corrected frames/s,
// close to the observed aggregate rate but not a reconstruction of events.
constexpr uint32_t kBodycamSparseDurationSeconds = 20;
constexpr size_t kBodycamSparseFrames = static_cast<size_t>(kBodycamRate)
    * kBodycamSparseDurationSeconds;
constexpr uint32_t kBodycamSparseEvents = 112;
constexpr uint32_t kBodycamSparseCadenceFrames = 8571;
constexpr double kBodycamPi = 3.14159265358979323846;

struct BodycamEditMetrics {
    double source_rms = 0.0;
    double edited_rms = 0.0;
    double source_peak = 0.0;
    double edited_peak = 0.0;
    double source_boundary_energy = 0.0;
    double edited_boundary_energy = 0.0;
    double source_difference_energy = 0.0;
    double edited_difference_energy = 0.0;
    double error_energy = 0.0;
    double max_boundary_delta = 0.0;
    bool stereo_integrity = false;
};

double BodycamRms(const std::vector<float>& samples) {
    if (samples.empty()) return 0.0;
    double energy = 0.0;
    for (const float sample : samples) {
        const double value = static_cast<double>(sample);
        energy += value * value;
    }
    return std::sqrt(energy / static_cast<double>(samples.size()));
}

double BodycamPeak(const std::vector<float>& samples) {
    double peak = 0.0;
    for (const float sample : samples) {
        peak = std::max(peak, std::fabs(static_cast<double>(sample)));
    }
    return peak;
}

// This is a deterministic high-frequency/difference metric. It is the mean
// squared one-frame difference over both channels, so hard sample edges are
// represented without depending on an FFT implementation or platform math.
double BodycamDifferenceEnergy(const std::vector<float>& samples) {
    constexpr size_t kStride = kBodycamChannels;
    const size_t frames = samples.size() / kStride;
    if (frames < 2) return 0.0;
    double energy = 0.0;
    for (size_t frame = 1; frame < frames; ++frame) {
        for (size_t channel = 0; channel < kStride; ++channel) {
            const double delta = static_cast<double>(
                samples[frame * kStride + channel]
                - samples[(frame - 1) * kStride + channel]);
            energy += delta * delta;
        }
    }
    return energy / static_cast<double>((frames - 1) * kStride);
}

double BodycamBoundaryEnergy(const std::vector<float>& samples,
                             const std::vector<size_t>& boundaries,
                             double* max_delta) {
    constexpr size_t kStride = kBodycamChannels;
    double energy = 0.0;
    double maximum = 0.0;
    for (const size_t boundary : boundaries) {
        if (boundary < kStride || boundary + kStride > samples.size()) {
            continue;
        }
        for (size_t channel = 0; channel < kStride; ++channel) {
            const double delta = static_cast<double>(
                samples[boundary + channel]
                - samples[boundary - kStride + channel]);
            energy += delta * delta;
            maximum = std::max(maximum, std::fabs(delta));
        }
    }
    if (max_delta) *max_delta = maximum;
    return energy;
}

std::vector<float> BodycamApplyHardInsertSkip(
        const std::vector<float>& source,
        std::vector<size_t>* boundaries = nullptr) {
    constexpr size_t kStride = kBodycamChannels;
    std::vector<float> edited;
    edited.reserve(source.size());
    if (boundaries) boundaries->clear();
    for (uint32_t chunk = 0; chunk < kBodycamChunks; ++chunk) {
        const size_t base = static_cast<size_t>(chunk) * kBodycamChunkFrames
            * kStride;
        if (boundaries && !edited.empty()) boundaries->push_back(edited.size());
        edited.insert(edited.end(), static_cast<size_t>(kBodycamEditFrames)
            * kStride, 0.0f);
        if (boundaries) boundaries->push_back(edited.size());
        edited.insert(edited.end(), source.begin() + base
            + static_cast<size_t>(kBodycamEditFrames) * kStride,
            source.begin() + base + static_cast<size_t>(kBodycamChunkFrames)
                * kStride);
    }
    return edited;
}

bool BodycamEditPreservesStereoPattern(const std::vector<float>& source,
                                       const std::vector<float>& edited) {
    constexpr size_t kStride = kBodycamChannels;
    if (source.size() != edited.size()) return false;
    for (uint32_t chunk = 0; chunk < kBodycamChunks; ++chunk) {
        const size_t base = static_cast<size_t>(chunk) * kBodycamChunkFrames
            * kStride;
        for (uint32_t frame = 0; frame < kBodycamChunkFrames; ++frame) {
            const size_t output = base + static_cast<size_t>(frame) * kStride;
            if (frame < kBodycamEditFrames) {
                for (size_t channel = 0; channel < kStride; ++channel) {
                    if (edited[output + channel] != 0.0f) return false;
                }
            } else {
                const size_t input = output;
                for (size_t channel = 0; channel < kStride; ++channel) {
                    if (edited[output + channel] != source[input + channel]) {
                        return false;
                    }
                }
            }
        }
    }
    return true;
}

std::vector<float> BodycamApplySparseFieldRateCorrections(
        const std::vector<float>& source,
        std::vector<size_t>* source_boundaries,
        std::vector<size_t>* edited_boundaries,
        uint32_t* insert_events,
        uint32_t* skip_events) {
    constexpr size_t kStride = kBodycamChannels;
    const size_t source_frames = source.size() / kStride;
    std::vector<float> edited;
    edited.reserve(source.size());
    if (source_boundaries) source_boundaries->clear();
    if (edited_boundaries) edited_boundaries->clear();
    uint32_t inserts = 0;
    uint32_t skips = 0;
    size_t source_frame = 0;
    for (uint32_t event = 0; event < kBodycamSparseEvents; ++event) {
        const size_t target_frame = static_cast<size_t>(event + 1)
            * kBodycamSparseCadenceFrames;
        while (source_frame < target_frame && source_frame < source_frames) {
            const size_t source_sample = source_frame * kStride;
            edited.insert(edited.end(), source.begin() + source_sample,
                source.begin() + source_sample + kStride);
            ++source_frame;
        }
        if ((event % 2) == 0) {
            if (source_boundaries) {
                source_boundaries->push_back(target_frame * kStride);
                source_boundaries->push_back((target_frame
                    + kBodycamEditFrames) * kStride);
            }
            if (edited_boundaries && !edited.empty()) {
                edited_boundaries->push_back(edited.size());
            }
            edited.insert(edited.end(), static_cast<size_t>(kBodycamEditFrames)
                * kStride, 0.0f);
            if (edited_boundaries) edited_boundaries->push_back(edited.size());
            ++inserts;
        } else {
            if (source_boundaries) {
                source_boundaries->push_back(target_frame * kStride);
            }
            if (edited_boundaries && !edited.empty()) {
                edited_boundaries->push_back(edited.size());
            }
            source_frame = std::min(source_frame
                + static_cast<size_t>(kBodycamEditFrames), source_frames);
            ++skips;
        }
    }
    while (source_frame < source_frames) {
        const size_t source_sample = source_frame * kStride;
        edited.insert(edited.end(), source.begin() + source_sample,
            source.begin() + source_sample + kStride);
        ++source_frame;
    }
    if (insert_events) *insert_events = inserts;
    if (skip_events) *skip_events = skips;
    return edited;
}

bool BodycamSparseEditPreservesStereoPattern(
        const std::vector<float>& source,
        const std::vector<float>& edited) {
    constexpr size_t kStride = kBodycamChannels;
    if (source.size() != edited.size()) return false;
    const size_t source_frames = source.size() / kStride;
    size_t source_frame = 0;
    size_t output_frame = 0;
    const auto same_frame = [&](size_t output, size_t input) {
        for (size_t channel = 0; channel < kStride; ++channel) {
            if (edited[output * kStride + channel]
                    != source[input * kStride + channel]) {
                return false;
            }
        }
        return true;
    };
    for (uint32_t event = 0; event < kBodycamSparseEvents; ++event) {
        const size_t target_frame = static_cast<size_t>(event + 1)
            * kBodycamSparseCadenceFrames;
        while (source_frame < target_frame && source_frame < source_frames) {
            if (!same_frame(output_frame, source_frame)) return false;
            ++source_frame;
            ++output_frame;
        }
        if ((event % 2) == 0) {
            for (uint32_t frame = 0; frame < kBodycamEditFrames; ++frame) {
                for (size_t channel = 0; channel < kStride; ++channel) {
                    if (edited[output_frame * kStride + channel] != 0.0f) {
                        return false;
                    }
                }
                ++output_frame;
            }
        } else {
            source_frame = std::min(source_frame
                + static_cast<size_t>(kBodycamEditFrames), source_frames);
        }
    }
    while (source_frame < source_frames) {
        if (!same_frame(output_frame, source_frame)) return false;
        ++source_frame;
        ++output_frame;
    }
    return output_frame == source_frames;
}

uint32_t NextBodycamNoise(uint32_t* state) {
    *state = *state * 1664525u + 1013904223u;
    return *state;
}

double BodycamNoise(uint32_t* state) {
    const uint32_t value = (NextBodycamNoise(state) >> 8) & 0xffffu;
    return static_cast<double>(value) / 32767.5 - 1.0;
}

std::vector<float> MakeBodycamSignal(const char* name, size_t frames) {
    std::vector<float> samples(frames * kBodycamChannels, 0.0f);
    uint32_t noise_state = 0x13579bdfu;
    for (size_t frame = 0; frame < frames; ++frame) {
        const double time = static_cast<double>(frame)
            / static_cast<double>(kBodycamRate);
        const double noise = BodycamNoise(&noise_state);
        const double noise_right = BodycamNoise(&noise_state);
        const double speech_envelope = 0.35
            + 0.65 * (0.5 + 0.5 * std::sin(2.0 * kBodycamPi * 2.7 * time));
        const double speech_left = speech_envelope * (
            0.13 * std::sin(2.0 * kBodycamPi * 137.0 * time)
            + 0.06 * std::sin(2.0 * kBodycamPi * 274.0 * time)
            + 0.035 * std::sin(2.0 * kBodycamPi * 411.0 * time)
            + 0.018 * std::sin(2.0 * kBodycamPi * 685.0 * time));
        const double speech_right = speech_envelope * (
            0.11 * std::sin(2.0 * kBodycamPi * 139.0 * time + 0.2)
            + 0.05 * std::sin(2.0 * kBodycamPi * 278.0 * time + 0.2)
            + 0.03 * std::sin(2.0 * kBodycamPi * 417.0 * time + 0.2));
        const double breath_envelope = 0.20
            + 0.80 * (0.5 + 0.5 * std::sin(2.0 * kBodycamPi * 1.9 * time
                - 0.4));
        const double breath_left = breath_envelope * (0.045 * noise
            + 0.012 * std::sin(2.0 * kBodycamPi * 41.0 * time));
        const double breath_right = breath_envelope * (0.040 * noise_right
            + 0.010 * std::sin(2.0 * kBodycamPi * 43.0 * time + 0.3));

        double left = 0.0;
        double right = 0.0;
        if (std::strcmp(name, "low-level ambience") == 0) {
            left = 0.010 * noise + 0.004 * std::sin(
                2.0 * kBodycamPi * 180.0 * time);
            right = 0.009 * noise_right + 0.003 * std::sin(
                2.0 * kBodycamPi * 220.0 * time + 0.1);
        } else if (std::strcmp(name, "voiced speech-like harmonic") == 0) {
            left = speech_left;
            right = speech_right;
        } else if (std::strcmp(name, "breath/noise-like") == 0) {
            left = breath_left;
            right = breath_right;
        } else if (std::strcmp(name, "ambience+speech") == 0) {
            left = 0.010 * noise + 0.004 * std::sin(
                2.0 * kBodycamPi * 180.0 * time) + speech_left;
            right = 0.009 * noise_right + 0.003 * std::sin(
                2.0 * kBodycamPi * 220.0 * time + 0.1) + speech_right;
        } else if (std::strcmp(name, "gunshot transient") == 0
                   || std::strcmp(name, "explosion transient") == 0) {
            const bool explosion = std::strcmp(name, "explosion transient") == 0;
            const size_t dense_centers[] = {1375, 6133, 10421, 15017};
            const int32_t sparse_jitter[] = {
                -3200, 1700, 5200, -4100, 2800, -900, 4600, -2500,
                1100, 3900, -4700, 2300, -1300, 5700, -3600, 800,
                3300, -5200, 1900, -700,
            };
            const size_t burst_count = frames == kBodycamDenseFrames ? 4 : 20;
            for (size_t burst_index = 0; burst_index < burst_count; ++burst_index) {
                const size_t center = frames == kBodycamDenseFrames
                    ? dense_centers[burst_index]
                    : static_cast<size_t>(24000 + burst_index * 48000
                        + sparse_jitter[burst_index]);
                if (frame < center) continue;
                const size_t age = frame - center;
                const size_t duration = explosion ? 4200 : 650;
                if (age >= duration) continue;
                const double decay = explosion
                    ? std::exp(-static_cast<double>(age) / 1200.0)
                    : std::exp(-static_cast<double>(age) / 130.0);
                const double thump = std::sin(2.0 * kBodycamPi
                    * (explosion ? 62.0 : 118.0) * time);
                const double burst_amplitude = (explosion ? 0.075 : 0.19) * decay;
                left += burst_amplitude * (0.72 * noise + 0.28 * thump);
                right += burst_amplitude * (0.68 * noise_right + 0.32 * thump);
            }
        }
        samples[frame * kBodycamChannels] = static_cast<float>(left);
        samples[frame * kBodycamChannels + 1] = static_cast<float>(right);
    }
    return samples;
}

BodycamEditMetrics MeasureBodycamEdit(const std::vector<float>& source,
                                      const std::vector<float>& edited,
                                      const std::vector<size_t>& source_boundaries,
                                      const std::vector<size_t>& edited_boundaries,
                                      bool stereo_integrity) {
    BodycamEditMetrics metrics;
    metrics.source_rms = BodycamRms(source);
    metrics.edited_rms = BodycamRms(edited);
    metrics.source_peak = BodycamPeak(source);
    metrics.edited_peak = BodycamPeak(edited);
    metrics.source_difference_energy = BodycamDifferenceEnergy(source);
    metrics.edited_difference_energy = BodycamDifferenceEnergy(edited);
    metrics.source_boundary_energy = BodycamBoundaryEnergy(source,
        source_boundaries, nullptr);
    metrics.edited_boundary_energy = BodycamBoundaryEnergy(edited,
        edited_boundaries, &metrics.max_boundary_delta);
    double error = 0.0;
    for (size_t sample = 0; sample < source.size(); ++sample) {
        const double delta = static_cast<double>(edited[sample]
            - source[sample]);
        error += delta * delta;
    }
    metrics.error_energy = source.empty() ? 0.0
        : error / static_cast<double>(source.size());
    metrics.stereo_integrity = stereo_integrity;
    return metrics;
}

void BodycamSignalClassMatrixReportsDeterministicDamage() {
    // Stress scenario only: one paired edit every 10 ms (100 paired edits/s),
    // intentionally denser than the field-rate-derived scenario below.
    const std::array<const char*, 7> names = {
        "silence", "low-level ambience", "voiced speech-like harmonic",
        "breath/noise-like", "gunshot transient", "explosion transient",
        "ambience+speech",
    };
    std::vector<BodycamEditMetrics> all_metrics;
    all_metrics.reserve(names.size());
    double voice_breath_boundary = 0.0;
    double transient_boundary = 0.0;
    double voice_breath_error = 0.0;
    double transient_error = 0.0;
    const auto gap = fthr::ReconcileAudioTimelinePacket(
        10'000'000ULL, 10'002'500ULL, kBodycamChunkFrames, kBodycamRate);
    const auto overlap = fthr::ReconcileAudioTimelinePacket(
        10'100'000ULL, 10'097'500ULL, kBodycamChunkFrames, kBodycamRate);
    CheckConverter(gap.normal_jitter_absorbed && gap.silence_frames == 0
        && overlap.normal_jitter_absorbed && overlap.skip_input_frames == 0,
        "BODYCAM matrix absorbs the old 0.25 ms production jitter");
    for (const char* name : names) {
        const auto source = std::strcmp(name, "silence") == 0
            ? std::vector<float>(kBodycamDenseFrames * kBodycamChannels, 0.0f)
            : MakeBodycamSignal(name, kBodycamDenseFrames);
        std::vector<size_t> boundaries;
        const auto edited = BodycamApplyHardInsertSkip(source, &boundaries);
        const auto metrics = MeasureBodycamEdit(source, edited, boundaries,
            boundaries,
            BodycamEditPreservesStereoPattern(source, edited));
        all_metrics.push_back(metrics);

        CheckConverter(source.size() == kBodycamDenseFrames * kBodycamChannels
            && edited.size() == source.size(),
            "BODYCAM edit preserves exact stereo frame count");
        CheckConverter(metrics.stereo_integrity,
            "BODYCAM edit preserves the same hard correction in both channels");
        CheckConverter(std::isfinite(metrics.edited_rms)
            && std::isfinite(metrics.edited_peak)
            && std::isfinite(metrics.edited_boundary_energy)
            && std::isfinite(metrics.edited_difference_energy)
            && std::isfinite(metrics.error_energy)
            && std::isfinite(metrics.max_boundary_delta),
            "BODYCAM edit metrics remain finite and deterministic");
        if (std::strcmp(name, "silence") == 0) {
            CheckConverter(metrics.edited_rms == 0.0
                && metrics.edited_peak == 0.0
                && metrics.edited_boundary_energy == 0.0
                && metrics.edited_difference_energy == 0.0
                && metrics.error_energy == 0.0
                && metrics.max_boundary_delta == 0.0,
                "BODYCAM hard correction leaves digital silence silent");
        } else {
            CheckConverter(metrics.source_peak > 0.0
                && metrics.edited_peak > 0.0
                && metrics.error_energy > 0.0,
                "BODYCAM non-silent class has measurable source and edit error");
        }

        if (std::strcmp(name, "voiced speech-like harmonic") == 0
                || std::strcmp(name, "breath/noise-like") == 0) {
            voice_breath_boundary += metrics.edited_boundary_energy;
            voice_breath_error += metrics.error_energy;
        } else if (std::strcmp(name, "gunshot transient") == 0
                   || std::strcmp(name, "explosion transient") == 0) {
            transient_boundary += metrics.edited_boundary_energy;
            transient_error += metrics.error_energy;
        }

        std::cout << std::fixed << std::setprecision(9)
                  << "BODYCAM_EDIT_DENSE_STRESS class=\"" << name << "\""
                  << " source_rms=" << metrics.source_rms
                  << " edited_rms=" << metrics.edited_rms
                  << " source_peak=" << metrics.source_peak
                  << " edited_peak=" << metrics.edited_peak
                  << " source_boundary_energy=" << metrics.source_boundary_energy
                  << " edited_boundary_energy=" << metrics.edited_boundary_energy
                  << " source_difference_energy="
                  << metrics.source_difference_energy
                  << " edited_difference_energy="
                  << metrics.edited_difference_energy
                  << " error_energy=" << metrics.error_energy
                  << " max_boundary_delta=" << metrics.max_boundary_delta
                  << std::endl;
    }
    const double boundary_ratio = transient_boundary > 0.0
        ? voice_breath_boundary / transient_boundary : 0.0;
    const double error_ratio = transient_error > 0.0
        ? voice_breath_error / transient_error : 0.0;
    std::cout << std::fixed << std::setprecision(9)
              << "BODYCAM_EDIT_DENSE_STRESS comparison=voice_breath_vs_transient"
              << " boundary_energy_ratio=" << boundary_ratio
              << " error_energy_ratio=" << error_ratio << std::endl;
    CheckConverter(all_metrics.size() == names.size(),
        "BODYCAM signal class matrix reports every requested class");
}

void BodycamFieldRateDerivedSparseMatrixReportsDeterministicDamage() {
    const std::array<const char*, 7> names = {
        "silence", "low-level ambience", "voiced speech-like harmonic",
        "breath/noise-like", "gunshot transient", "explosion transient",
        "ambience+speech",
    };
    const auto gap = fthr::ReconcileAudioTimelinePacket(
        10'000'000ULL, 10'002'500ULL, kBodycamChunkFrames, kBodycamRate);
    const auto overlap = fthr::ReconcileAudioTimelinePacket(
        10'100'000ULL, 10'097'500ULL, kBodycamChunkFrames, kBodycamRate);
    CheckConverter(gap.normal_jitter_absorbed && gap.silence_frames == 0
        && overlap.normal_jitter_absorbed && overlap.skip_input_frames == 0,
        "BODYCAM sparse matrix absorbs the old 0.25 ms production jitter");

    double voice_breath_boundary = 0.0;
    double transient_boundary = 0.0;
    double voice_breath_error = 0.0;
    double transient_error = 0.0;
    uint32_t expected_inserts = 0;
    uint32_t expected_skips = 0;
    for (const char* name : names) {
        const auto source = std::strcmp(name, "silence") == 0
            ? std::vector<float>(kBodycamSparseFrames * kBodycamChannels, 0.0f)
            : MakeBodycamSignal(name, kBodycamSparseFrames);
        std::vector<size_t> source_boundaries;
        std::vector<size_t> edited_boundaries;
        uint32_t insert_events = 0;
        uint32_t skip_events = 0;
        const auto edited = BodycamApplySparseFieldRateCorrections(source,
            &source_boundaries, &edited_boundaries,
            &insert_events, &skip_events);
        expected_inserts = insert_events;
        expected_skips = skip_events;
        const auto metrics = MeasureBodycamEdit(source, edited,
            source_boundaries, edited_boundaries,
            BodycamSparseEditPreservesStereoPattern(source, edited));

        CheckConverter(source.size() == kBodycamSparseFrames * kBodycamChannels
            && edited.size() == source.size(),
            "BODYCAM sparse corrections preserve final stereo duration");
        CheckConverter(metrics.stereo_integrity,
            "BODYCAM sparse corrections preserve both stereo channels");
        CheckConverter(std::isfinite(metrics.edited_rms)
            && std::isfinite(metrics.edited_peak)
            && std::isfinite(metrics.edited_boundary_energy)
            && std::isfinite(metrics.edited_difference_energy)
            && std::isfinite(metrics.error_energy)
            && std::isfinite(metrics.max_boundary_delta),
            "BODYCAM sparse metrics remain finite and deterministic");
        if (std::strcmp(name, "silence") == 0) {
            CheckConverter(metrics.edited_rms == 0.0
                && metrics.edited_peak == 0.0
                && metrics.edited_boundary_energy == 0.0
                && metrics.edited_difference_energy == 0.0
                && metrics.error_energy == 0.0
                && metrics.max_boundary_delta == 0.0,
                "BODYCAM sparse corrections leave digital silence silent");
        } else {
            CheckConverter(metrics.source_peak > 0.0
                && metrics.edited_peak > 0.0
                && metrics.error_energy > 0.0,
                "BODYCAM sparse class has measurable source and edit error");
        }

        if (std::strcmp(name, "voiced speech-like harmonic") == 0
                || std::strcmp(name, "breath/noise-like") == 0) {
            voice_breath_boundary += metrics.edited_boundary_energy;
            voice_breath_error += metrics.error_energy;
        } else if (std::strcmp(name, "gunshot transient") == 0
                   || std::strcmp(name, "explosion transient") == 0) {
            transient_boundary += metrics.edited_boundary_energy;
            transient_error += metrics.error_energy;
        }

        std::cout << std::fixed << std::setprecision(9)
                  << "BODYCAM_EDIT_FIELD_RATE_DERIVED class=\"" << name << "\""
                  << " source_rms=" << metrics.source_rms
                  << " edited_rms=" << metrics.edited_rms
                  << " source_peak=" << metrics.source_peak
                  << " edited_peak=" << metrics.edited_peak
                  << " source_boundary_energy=" << metrics.source_boundary_energy
                  << " edited_boundary_energy=" << metrics.edited_boundary_energy
                  << " source_difference_energy="
                  << metrics.source_difference_energy
                  << " edited_difference_energy="
                  << metrics.edited_difference_energy
                  << " error_energy=" << metrics.error_energy
                  << " max_boundary_delta=" << metrics.max_boundary_delta
                  << std::endl;
    }
    const double boundary_ratio = transient_boundary > 0.0
        ? voice_breath_boundary / transient_boundary : 0.0;
    const double error_ratio = transient_error > 0.0
        ? voice_breath_error / transient_error : 0.0;
    CheckConverter(expected_inserts == kBodycamSparseEvents / 2
        && expected_skips == kBodycamSparseEvents / 2,
        "BODYCAM sparse schedule alternates 56 inserts and 56 skips");
    std::cout << std::fixed << std::setprecision(9)
              << "BODYCAM_EDIT_FIELD_RATE_DERIVED scenario=representative_not_exact"
              << " duration_s=" << kBodycamSparseDurationSeconds
              << " event_rate_per_s="
              << static_cast<double>(kBodycamSparseEvents)
                    / kBodycamSparseDurationSeconds
              << " corrected_frames_per_s="
              << static_cast<double>(kBodycamSparseEvents * kBodycamEditFrames)
                    / kBodycamSparseDurationSeconds
              << " inserted_frames="
              << expected_inserts * kBodycamEditFrames
              << " skipped_frames=" << expected_skips * kBodycamEditFrames
              << " boundary_energy_ratio=" << boundary_ratio
              << " error_energy_ratio=" << error_ratio << std::endl;
}

void BodycamProductionPolicySignalQualityIsImproved() {
    const std::array<const char*, 4> names = {
        "voiced speech-like harmonic", "breath/noise-like",
        "gunshot transient", "explosion transient",
    };
    for (const char* name : names) {
        const auto source = MakeBodycamSignal(name, kBodycamDenseFrames);
        std::vector<size_t> old_boundaries;
        const auto old_edited = BodycamApplyHardInsertSkip(
            source, &old_boundaries);
        const auto old_metrics = MeasureBodycamEdit(source, old_edited,
            old_boundaries, old_boundaries,
            BodycamEditPreservesStereoPattern(source, old_edited));
        // The new production policy absorbs the physical 1-7-frame jitter;
        // with no hard edit, the published samples remain the source samples.
        const auto new_metrics = MeasureBodycamEdit(source, source, {}, {}, true);

        std::cout << std::fixed << std::setprecision(9)
                  << "BODYCAM_POLICY_SIGNAL class=\"" << name << "\""
                  << " old_boundary_energy=" << old_metrics.edited_boundary_energy
                  << " new_boundary_energy=" << new_metrics.edited_boundary_energy
                  << " old_error_energy=" << old_metrics.error_energy
                  << " new_error_energy=" << new_metrics.error_energy
                  << " old_peak=" << old_metrics.edited_peak
                  << " new_peak=" << new_metrics.edited_peak << std::endl;

        if (std::strcmp(name, "voiced speech-like harmonic") == 0
                || std::strcmp(name, "breath/noise-like") == 0) {
            CheckConverter(old_metrics.edited_boundary_energy > 0.0
                && old_metrics.error_energy > 0.0
                && new_metrics.edited_boundary_energy
                    < old_metrics.edited_boundary_energy * 0.1
                && new_metrics.error_energy < old_metrics.error_energy * 0.1,
                "speech/breath hard-edit boundary energy is materially reduced");
        } else {
            CheckConverter(new_metrics.edited_peak == old_metrics.source_peak
                && new_metrics.edited_rms == old_metrics.source_rms
                && new_metrics.error_energy <= old_metrics.error_energy,
                "transient signal quality is not degraded by the new policy");
        }
    }
}

}  // namespace

int RunAudioFormatConverterTests() {
    ConvertsIntegerAndFloatFormatsToCanonicalStereo();
    ConvertsPacked24AndEightChannelMasks();
    ResamplingIsPersistentAndFlushesDelay();
    SilentNativePacketsKeepTheirCanonicalDuration();
    EncoderReportsSubmissionAndFlushResults();
    SignalMetricsRemainBoundedAndExposeInvalidSamples();
    TimelineMicroGapAndOverlapAreAbsorbed();
    TimelinePolicyAbsorbsJitterAndBoundsRecovery();
    LongRunTimelinePolicyRemainsBounded();
    TimelineSourcesRestartIndependentlyAndRejectRegression();
    ConverterPreservesQuietSineAndStereoMultitone();
    FirstEqualRateCompensationKeepsContinuousSignalBounded();
    HighLevelInputIsClampedWithoutNumericalWrap();
    AacRoundTripPreservesFiniteSignalMetrics();
    AvTimelineEvidencePreservesRelativeTiming();
    CorrectionDistributionsRemainBoundedAndSigned();
    ContinuityCorrectionsMapToClipAndRemainBounded();
    RepeatedHardContinuityEditsExposeBoundaryEnergy();
    BodycamSignalClassMatrixReportsDeterministicDamage();
    BodycamFieldRateDerivedSparseMatrixReportsDeterministicDamage();
    BodycamProductionPolicySignalQualityIsImproved();
    std::cout << "AUDIT-050 audio format converter tests: "
              << converter_checks << " checks passed" << std::endl;
    return converter_checks;
}
