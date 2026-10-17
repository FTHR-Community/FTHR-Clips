// audio_format_converter.cpp -- bounded WASAPI PCM conversion.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <mmreg.h>
#include <ks.h>
#include <ksmedia.h>

#include "audio_format_converter.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

extern "C" {
#include <libavutil/channel_layout.h>
#include <libavutil/mathematics.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
}

namespace fthr {
namespace {

constexpr uint32_t kMaxSupportedChannels = 8;
constexpr uint32_t kMaxSupportedSampleRate = 192000;

struct ParsedWaveFormat {
    uint32_t sample_rate = 0;
    uint32_t channels = 0;
    uint32_t bytes_per_frame = 0;
    uint16_t bits_per_sample = 0;
    uint16_t valid_bits_per_sample = 0;
    uint64_t channel_mask = 0;
    bool is_float = false;
};

bool ParseWaveFormat(const WAVEFORMATEX* format, ParsedWaveFormat* result) {
    if (!format || !result || format->nChannels == 0
        || format->nChannels > kMaxSupportedChannels
        || format->nSamplesPerSec == 0
        || format->nSamplesPerSec > kMaxSupportedSampleRate
        || format->nBlockAlign == 0) {
        return false;
    }

    ParsedWaveFormat parsed;
    parsed.sample_rate = format->nSamplesPerSec;
    parsed.channels = format->nChannels;
    parsed.bytes_per_frame = format->nBlockAlign;
    parsed.bits_per_sample = format->wBitsPerSample;
    parsed.valid_bits_per_sample = format->wBitsPerSample;

    WORD tag = format->wFormatTag;
    if (tag == WAVE_FORMAT_EXTENSIBLE) {
        if (format->cbSize < sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX))
            return false;
        const auto* extensible = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(format);
        parsed.channel_mask = extensible->dwChannelMask;
        parsed.valid_bits_per_sample = extensible->Samples.wValidBitsPerSample;
        if (extensible->SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
            tag = WAVE_FORMAT_IEEE_FLOAT;
        else if (extensible->SubFormat == KSDATAFORMAT_SUBTYPE_PCM)
            tag = WAVE_FORMAT_PCM;
        else
            return false;
    }

    if (tag == WAVE_FORMAT_IEEE_FLOAT) {
        if (parsed.bits_per_sample != 32 || parsed.valid_bits_per_sample != 32
            || parsed.bytes_per_frame != parsed.channels * sizeof(float)) {
            return false;
        }
        parsed.is_float = true;
    } else if (tag == WAVE_FORMAT_PCM) {
        if (parsed.bits_per_sample != 16 && parsed.bits_per_sample != 24
            && parsed.bits_per_sample != 32) {
            return false;
        }
        if (parsed.valid_bits_per_sample == 0
            || parsed.valid_bits_per_sample > parsed.bits_per_sample
            || parsed.bytes_per_frame != parsed.channels
                * ((parsed.bits_per_sample + 7) / 8)) {
            return false;
        }
    } else {
        return false;
    }

    const uint64_t expected_bytes_per_second =
        static_cast<uint64_t>(parsed.sample_rate) * parsed.bytes_per_frame;
    if (format->nAvgBytesPerSec != 0
            && format->nAvgBytesPerSec != expected_bytes_per_second) return false;

    uint32_t mask_channels = 0;
    for (uint64_t mask = parsed.channel_mask; mask != 0; mask >>= 1)
        mask_channels += static_cast<uint32_t>(mask & 1);
    if (parsed.channel_mask != 0 && mask_channels != parsed.channels) {
        // A malformed extensible mask cannot safely describe channel order.
        return false;
    }
    *result = parsed;
    return true;
}

AVChannelLayout InputLayout(const ParsedWaveFormat& format) {
    AVChannelLayout layout{};
    if (format.channel_mask != 0
        && av_channel_layout_from_mask(&layout, format.channel_mask) == 0
        && layout.nb_channels == static_cast<int>(format.channels)) {
        return layout;
    }
    av_channel_layout_default(&layout, static_cast<int>(format.channels));
    return layout;
}

AVChannel ChannelAt(const AVChannelLayout& layout, uint32_t index) {
    return av_channel_layout_channel_from_index(&layout, index);
}

void AddMatrixContribution(AVChannel channel, double* left, double* right) {
    constexpr double kFront = 0.50;
    constexpr double kCenter = 0.353553390593;
    constexpr double kSurround = 0.353553390593;
    constexpr double kLfe = 0.25;
    switch (channel) {
    case AV_CHAN_FRONT_LEFT:
    case AV_CHAN_FRONT_LEFT_OF_CENTER:
        *left = kFront;
        break;
    case AV_CHAN_FRONT_RIGHT:
    case AV_CHAN_FRONT_RIGHT_OF_CENTER:
        *right = kFront;
        break;
    case AV_CHAN_FRONT_CENTER:
        *left = kCenter;
        *right = kCenter;
        break;
    case AV_CHAN_LOW_FREQUENCY:
    case AV_CHAN_LOW_FREQUENCY_2:
        *left = kLfe;
        *right = kLfe;
        break;
    case AV_CHAN_BACK_LEFT:
    case AV_CHAN_SIDE_LEFT:
        *left = kSurround;
        break;
    case AV_CHAN_BACK_RIGHT:
    case AV_CHAN_SIDE_RIGHT:
        *right = kSurround;
        break;
    case AV_CHAN_BACK_CENTER:
        *left = kSurround;
        *right = kSurround;
        break;
    default:
        // Unknown channels are retained at a conservative level in both
        // outputs rather than silently dropping a source.
        *left = 0.25;
        *right = 0.25;
        break;
    }
}

std::vector<double> DownmixMatrix(const AVChannelLayout& input) {
    std::vector<double> matrix(static_cast<size_t>(input.nb_channels) * 2, 0.0);
    if (input.nb_channels == 1) {
        // A mono microphone or endpoint is duplicated without attenuation.
        matrix[0] = 1.0;
        matrix[1] = 1.0;
        return matrix;
    }
    if (input.nb_channels == 2
            && ChannelAt(input, 0) == AV_CHAN_FRONT_LEFT
            && ChannelAt(input, 1) == AV_CHAN_FRONT_RIGHT) {
        // The common stereo case is a transparent channel-preserving path.
        matrix[0] = 1.0;
        matrix[3] = 1.0;
        return matrix;
    }
    double left_sum = 0.0;
    double right_sum = 0.0;
    for (int index = 0; index < input.nb_channels; ++index) {
        double left = 0.0;
        double right = 0.0;
        AddMatrixContribution(ChannelAt(input, static_cast<uint32_t>(index)),
            &left, &right);
        matrix[static_cast<size_t>(index)] = left;
        matrix[static_cast<size_t>(input.nb_channels) + index] = right;
        left_sum += left;
        right_sum += right;
    }
    // Keep every output below full scale for the worst-case correlated input.
    const double scale = std::min(1.0,
        0.95 / std::max(left_sum, right_sum));
    for (double& coefficient : matrix) coefficient *= scale;
    return matrix;
}

float DecodeSample(const uint8_t* bytes, bool is_float, uint16_t bits,
                   uint16_t valid_bits) {
    if (is_float) {
        float value = 0.0f;
        std::memcpy(&value, bytes, sizeof(value));
        return std::isfinite(value) ? std::clamp(value, -1.0f, 1.0f) : 0.0f;
    }
    if (bits == 16) {
        int16_t value = 0;
        std::memcpy(&value, bytes, sizeof(value));
        return static_cast<float>(value) / 32768.0f;
    }
    if (bits == 24) {
        int32_t value = static_cast<int32_t>(bytes[0])
            | (static_cast<int32_t>(bytes[1]) << 8)
            | (static_cast<int32_t>(bytes[2]) << 16);
        if ((value & 0x00800000) != 0) value |= ~0x00FFFFFF;
        const int shift = std::max(0, 24 - static_cast<int>(valid_bits));
        if (shift > 0) value >>= shift;
        const float divisor = static_cast<float>(uint64_t{1} << (valid_bits - 1));
        return std::clamp(static_cast<float>(value) / divisor, -1.0f, 1.0f);
    }
    int32_t value = 0;
    std::memcpy(&value, bytes, sizeof(value));
    const int shift = std::max(0, 32 - static_cast<int>(valid_bits));
    if (shift > 0) value >>= shift;
    const float divisor = static_cast<float>(uint64_t{1} << (valid_bits - 1));
    return std::clamp(static_cast<float>(value) / divisor, -1.0f, 1.0f);
}

}  // namespace

struct AudioFormatConverter::Impl {
    SwrContext* resampler = nullptr;
    std::vector<float> native_float;
    std::vector<float> converted;
    AudioSignalMetrics input_metrics;
    AudioSignalMetrics output_metrics;

    ~Impl() { swr_free(&resampler); }
};

AudioFormatConverter::AudioFormatConverter() : impl_(std::make_unique<Impl>()) {}

AudioFormatConverter::~AudioFormatConverter() = default;

bool AudioFormatConverter::Initialize(const WAVEFORMATEX* format) {
    ParsedWaveFormat parsed;
    if (!ParseWaveFormat(format, &parsed)) return false;

    Reset();
    AVChannelLayout input_layout = InputLayout(parsed);
    AVChannelLayout output_layout = AV_CHANNEL_LAYOUT_STEREO;
    SwrContext* resampler = nullptr;
    const int result = swr_alloc_set_opts2(&resampler, &output_layout,
        AV_SAMPLE_FMT_FLT, static_cast<int>(kOutputSampleRate), &input_layout,
        AV_SAMPLE_FMT_FLT, static_cast<int>(parsed.sample_rate), 0, nullptr);
    av_channel_layout_uninit(&input_layout);
    if (result < 0 || !resampler) {
        swr_free(&resampler);
        return false;
    }
    AVChannelLayout matrix_layout = InputLayout(parsed);
    const std::vector<double> matrix = DownmixMatrix(matrix_layout);
    av_channel_layout_uninit(&matrix_layout);
    if (swr_set_matrix(resampler, matrix.data(), parsed.channels) < 0
        || swr_init(resampler) < 0) {
        swr_free(&resampler);
        return false;
    }

    impl_->resampler = resampler;
    input_sample_rate_ = parsed.sample_rate;
    input_channels_ = parsed.channels;
    input_bytes_per_frame_ = parsed.bytes_per_frame;
    input_bits_per_sample_ = parsed.bits_per_sample;
    input_valid_bits_per_sample_ = parsed.valid_bits_per_sample;
    input_channel_mask_ = parsed.channel_mask;
    input_float_ = parsed.is_float;
    input_sample_format_ = parsed.is_float
        ? "float32"
        : (parsed.bits_per_sample == 16 ? "pcm_s16"
            : (parsed.bits_per_sample == 24 ? "pcm_s24" : "pcm_s32"));
    return true;
}

bool AudioFormatConverter::Convert(const uint8_t* input, uint32_t input_frames,
                                   std::vector<float>* output) {
    if (!impl_->resampler || !output || (input_frames > 0 && !input)
        || input_frames > static_cast<uint32_t>(std::numeric_limits<int>::max())) return false;
    if (input_frames == 0) return true;

    const size_t sample_count = static_cast<size_t>(input_frames) * input_channels_;
    impl_->native_float.resize(sample_count);
    const uint32_t bytes_per_sample = input_bytes_per_frame_ / input_channels_;
    for (uint32_t frame = 0; frame < input_frames; ++frame) {
        const uint8_t* source = input + static_cast<size_t>(frame)
            * input_bytes_per_frame_;
        for (uint32_t channel = 0; channel < input_channels_; ++channel) {
            impl_->native_float[static_cast<size_t>(frame) * input_channels_ + channel]
                = DecodeSample(source + static_cast<size_t>(channel) * bytes_per_sample,
                    input_float_, input_bits_per_sample_, input_valid_bits_per_sample_);
        }
    }
    impl_->input_metrics.Observe(impl_->native_float.data(), input_frames,
        input_channels_);

    const int64_t delay = swr_get_delay(impl_->resampler,
        static_cast<int64_t>(input_sample_rate_));
    const int64_t capacity64 = av_rescale_rnd(delay + input_frames,
        static_cast<int64_t>(kOutputSampleRate), input_sample_rate_, AV_ROUND_UP) + 64;
    if (capacity64 <= 0 || capacity64 > std::numeric_limits<int>::max()) return false;
    impl_->converted.resize(static_cast<size_t>(capacity64) * kOutputChannels);
    const uint8_t* input_plane = reinterpret_cast<const uint8_t*>(
        impl_->native_float.data());
    uint8_t* output_plane = reinterpret_cast<uint8_t*>(impl_->converted.data());
    const int produced = swr_convert(impl_->resampler, &output_plane,
        static_cast<int>(capacity64), &input_plane, static_cast<int>(input_frames));
    if (produced < 0) return false;
    impl_->output_metrics.Observe(impl_->converted.data(),
        static_cast<uint32_t>(produced), kOutputChannels);
    output->insert(output->end(), impl_->converted.begin(),
        impl_->converted.begin() + static_cast<size_t>(produced) * kOutputChannels);
    return true;
}

bool AudioFormatConverter::Flush(std::vector<float>* output) {
    if (!impl_->resampler || !output) return false;
    for (int attempt = 0; attempt < 8; ++attempt) {
        const int64_t delay = swr_get_delay(impl_->resampler,
            static_cast<int64_t>(input_sample_rate_));
        if (delay <= 0) return true;
        const int64_t capacity64 = av_rescale_rnd(delay, kOutputSampleRate,
            input_sample_rate_, AV_ROUND_UP) + 64;
        if (capacity64 <= 0 || capacity64 > std::numeric_limits<int>::max()) return false;
        impl_->converted.resize(static_cast<size_t>(capacity64) * kOutputChannels);
        uint8_t* output_plane = reinterpret_cast<uint8_t*>(impl_->converted.data());
        const int produced = swr_convert(impl_->resampler, &output_plane,
            static_cast<int>(capacity64), nullptr, 0);
        if (produced < 0) return false;
        impl_->output_metrics.Observe(impl_->converted.data(),
            static_cast<uint32_t>(produced), kOutputChannels);
        output->insert(output->end(), impl_->converted.begin(),
            impl_->converted.begin() + static_cast<size_t>(produced) * kOutputChannels);
        if (produced == 0) return true;
    }
    return swr_get_delay(impl_->resampler, input_sample_rate_) == 0;
}

bool AudioFormatConverter::ApplyDriftCorrection(
    int32_t sample_delta, uint32_t compensation_distance_samples) {
    if (!impl_->resampler || compensation_distance_samples == 0) return false;
    return swr_set_compensation(impl_->resampler, sample_delta,
        static_cast<int>(std::min<uint32_t>(compensation_distance_samples,
            static_cast<uint32_t>(std::numeric_limits<int>::max())))) >= 0;
}

AudioSignalMetricsSnapshot AudioFormatConverter::GetInputSignalMetrics() const {
    return impl_ ? impl_->input_metrics.Snapshot() : AudioSignalMetricsSnapshot{};
}

AudioSignalMetricsSnapshot AudioFormatConverter::GetOutputSignalMetrics() const {
    return impl_ ? impl_->output_metrics.Snapshot() : AudioSignalMetricsSnapshot{};
}

void AudioFormatConverter::Reset() {
    if (impl_) swr_free(&impl_->resampler);
    input_sample_rate_ = 0;
    input_channels_ = 0;
    input_bytes_per_frame_ = 0;
    input_bits_per_sample_ = 0;
    input_valid_bits_per_sample_ = 0;
    input_channel_mask_ = 0;
    input_float_ = false;
    input_sample_format_ = "unavailable:not_initialized";
    if (impl_) {
        impl_->native_float.clear();
        impl_->converted.clear();
        impl_->input_metrics.Reset();
        impl_->output_metrics.Reset();
    }
}

}  // namespace fthr
