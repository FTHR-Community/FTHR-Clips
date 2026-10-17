// audio_format_converter.h -- bounded WASAPI PCM conversion for native audio.

#pragma once
#ifndef FTHR_AUDIO_FORMAT_CONVERTER_H
#define FTHR_AUDIO_FORMAT_CONVERTER_H

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <mmreg.h>

#include "audio_signal_metrics.h"

namespace fthr {

// Converts one generation's native WASAPI format to the single format shared
// by the replay rings and AAC encoders: interleaved float32, 48 kHz, stereo.
// The converter owns one persistent SwrContext. Reusing it is important for
// resampler delay and prevents each WASAPI packet from becoming a new clock.
class AudioFormatConverter {
public:
    AudioFormatConverter();
    ~AudioFormatConverter();

    AudioFormatConverter(const AudioFormatConverter&) = delete;
    AudioFormatConverter& operator=(const AudioFormatConverter&) = delete;

    // Accepts PCM/IEEE_FLOAT WAVEFORMATEX, including WAVEFORMATEXTENSIBLE.
    // Supported input samples are float32, signed 16-bit, packed signed
    // 24-bit and signed 32-bit, with 1..8 channels and rates up to 192 kHz.
    bool Initialize(const WAVEFORMATEX* format);

    // Converts input_frames of interleaved native PCM. output is appended to;
    // it is safe to reuse the same vector for successive packets.
    bool Convert(const uint8_t* input, uint32_t input_frames,
                 std::vector<float>* output);

    // Emits samples retained by the persistent resampler delay line.
    bool Flush(std::vector<float>* output);

    bool ApplyDriftCorrection(int32_t sample_delta,
                              uint32_t compensation_distance_samples);

    void Reset();

    // Bounded lifetime signal health for the decoded native stage and the
    // canonical post-resampler stage.  These snapshots never retain PCM.
    AudioSignalMetricsSnapshot GetInputSignalMetrics() const;
    AudioSignalMetricsSnapshot GetOutputSignalMetrics() const;

    uint32_t input_sample_rate() const { return input_sample_rate_; }
    uint32_t input_channels() const { return input_channels_; }
    uint32_t sample_rate() const { return kOutputSampleRate; }
    uint32_t channels() const { return kOutputChannels; }
    uint32_t input_bytes_per_frame() const { return input_bytes_per_frame_; }
    uint64_t input_channel_mask() const { return input_channel_mask_; }
    const std::string& input_sample_format() const { return input_sample_format_; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    uint32_t input_sample_rate_ = 0;
    uint32_t input_channels_ = 0;
    uint32_t input_bytes_per_frame_ = 0;
    uint16_t input_bits_per_sample_ = 0;
    uint16_t input_valid_bits_per_sample_ = 0;
    uint64_t input_channel_mask_ = 0;
    bool input_float_ = false;
    std::string input_sample_format_ = "unavailable:not_initialized";

    static constexpr uint32_t kOutputSampleRate = 48000;
    static constexpr uint32_t kOutputChannels = 2;
};

}  // namespace fthr

#endif  // FTHR_AUDIO_FORMAT_CONVERTER_H
