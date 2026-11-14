// Synchronous FFmpeg decoder/mixer called only by the Python decoder worker.
// One demuxer feeds selected streams into bounded segment queues and a single
// 48 kHz float32 stereo mix. Never call from Qt UI or audio pull callbacks.

#include "ffmpeg_playback_bridge.h"
#include "audio_gain_ramp.h"
#include "audio_stream_timing.h"
#include "ffmpeg_audio_filter.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/error.h>
#include <libavutil/opt.h>
#include <libavutil/samplefmt.h>
#include <libavutil/mathematics.h>
#include <libswresample/swresample.h>
}

#include <algorithm>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <iterator>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

constexpr int kSampleRate = 48'000;
constexpr int kChannels = 2;
constexpr float kLimiterCeiling = 1.0f;
// A seek cuts the old endpoint at an arbitrary waveform value.  Ramp only the
// first 2.7 ms of replacement PCM so the discontinuity cannot become a click;
// this is far below an AAC frame and does not create perceptible seek lag.
constexpr int kSeekFadeFrames = 128;
// Keep every demux pass bounded.  This permits normal interleaved streams to
// fill a mixer block while avoiding a large decoded-audio runway when a
// selected stream is missing or malformed.
constexpr int kReadPacketBudget = 64;

std::string ErrorText(const int result) {
    char buffer[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(result, buffer, sizeof(buffer));
    return buffer;
}

void WriteError(char* destination, const size_t capacity,
                const std::string& message) {
    if (!destination || capacity == 0) return;
    const size_t copied = std::min(capacity - 1, message.size());
    std::memcpy(destination, message.data(), copied);
    destination[copied] = '\0';
}

int ResampleCapacity(const int64_t input_samples, const int source_rate) {
    const int64_t converted = av_rescale_rnd(
        input_samples, kSampleRate, source_rate, AV_ROUND_UP) + 32;
    return static_cast<int>(std::clamp<int64_t>(converted, 1, INT_MAX));
}

struct Segment {
    int64_t first_sample = 0;
    std::vector<float> samples;

    [[nodiscard]] int64_t FrameCount() const {
        return static_cast<int64_t>(samples.size() / kChannels);
    }

    [[nodiscard]] int64_t LastSampleExclusive() const {
        return first_sample + FrameCount();
    }
};

struct DecoderState {
    int stream_index = -1;
    AVRational time_base{};
    AVCodecContext* codec = nullptr;
    SwrContext* resampler = nullptr;
    std::deque<Segment> segments;
    int64_t decoded_until = std::numeric_limits<int64_t>::min();
    int64_t fallback_next_sample = 0;
    bool input_eof = false;
    bool flushed = false;
    bool failed = false;
    bool unavailable = false;
    fthr::AudioGainRamp gain;
    std::string processing = "anull";
    std::unique_ptr<FthrAudioFilter> filter;
    int64_t first_sample_hint = std::numeric_limits<int64_t>::min();
    int64_t last_sample_hint = -1;

    ~DecoderState() {
        swr_free(&resampler);
        avcodec_free_context(&codec);
    }

    DecoderState() = default;
    DecoderState(const DecoderState&) = delete;
    DecoderState& operator=(const DecoderState&) = delete;
};

class Mixer {
public:
    bool unlimited_output = false;
    std::function<int(float*, int, const float*, int, float)> external_pull;
    std::string output_filter;
    float output_peak = 0.0f;

    bool SetProcessing(int index, const char* text, std::string& error) {
        if (index < 0 || index >= static_cast<int>(decoders_.size()) || !text) return false;
        auto& decoder = *decoders_[index];
        decoder.processing = text;
        if (decoder.unavailable || decoder.processing == "anull") return true;
        decoder.filter = std::make_unique<FthrAudioFilter>();
        if (!decoder.filter->Open(decoder.processing)) {
            error = "could not configure source processing";
            return false;
        }
        return true;
    }

    bool ResetExternalClock(std::string& error) {
        if (tempo_active_) return ConfigureTempo(tempo_rate_, error);
        return true;
    }
    ~Mixer() {
        ResetTempo();
        av_frame_free(&frame_);
        av_packet_free(&packet_);
        avformat_close_input(&format_);
    }

    bool Open(const char* path, const int* stream_indexes, const int stream_count,
              std::string& error) {
        if (!path || !*path || !stream_indexes || stream_count < 1 || stream_count > 64) {
            error = "invalid playback stream selection";
            return false;
        }
        int result = avformat_open_input(&format_, path, nullptr, nullptr);
        if (result < 0) {
            error = "could not open clip: " + ErrorText(result);
            return false;
        }
        result = avformat_find_stream_info(format_, nullptr);
        if (result < 0) {
            error = "could not inspect clip streams: " + ErrorText(result);
            return false;
        }
        // FFmpeg timestamps are expressed on the container's absolute
        // timeline, while the editor and the mixer cursor use a relative
        // zero-origin timeline.  Keep this shared origin exactly once.  It
        // must come from the format, never from the earliest selected audio
        // stream: a later audio stem is intentionally late on the same
        // timeline and must remain so.
        container_start_time_us_ = format_->start_time == AV_NOPTS_VALUE
            ? 0
            : format_->start_time;
        container_origin_sample_ = av_rescale_q(
            container_start_time_us_, AVRational{1, AV_TIME_BASE},
            AVRational{1, kSampleRate});
        for (int input_index = 0; input_index < stream_count; ++input_index) {
            const int stream_index = stream_indexes[input_index];
            auto decoder = std::make_unique<DecoderState>();
            if (stream_index < 0 || stream_index >= static_cast<int>(format_->nb_streams)) {
                decoder->failed = decoder->unavailable = true;
                decoders_.push_back(std::move(decoder));
                continue;
            }
            AVStream* stream = format_->streams[stream_index];
            if (stream->codecpar->codec_type != AVMEDIA_TYPE_AUDIO
                    || !ConfigureDecoder(*decoder, stream_index, *stream, error)) {
                decoder->failed = decoder->unavailable = true;
                decoders_.push_back(std::move(decoder));
                continue;
            }
            if (seek_stream_index_ < 0
                    || (stream->start_time != AV_NOPTS_VALUE
                        && (seek_start_time_ == AV_NOPTS_VALUE
                            || av_compare_ts(stream->start_time, stream->time_base,
                                             seek_start_time_, seek_time_base_) < 0))) {
                seek_stream_index_ = stream_index;
                seek_time_base_ = stream->time_base;
                seek_start_time_ = stream->start_time;
            }
            decoders_.push_back(std::move(decoder));
        }
        if (seek_stream_index_ < 0) {
            if (error.empty()) error = "no selected audio stream could be decoded";
            return false;
        }
        packet_ = av_packet_alloc();
        frame_ = av_frame_alloc();
        if (!packet_ || !frame_) {
            error = "could not allocate FFmpeg playback packet/frame";
            return false;
        }
        return true;
    }

    bool SetPlaybackRate(const float requested_rate, const bool preserve_pitch,
                         std::string& error) {
        const float rate = std::clamp(requested_rate, 0.25f, 2.0f);
        const bool enabled = preserve_pitch && std::abs(rate - 1.0f) > 1e-6f;
        if (!output_filter.empty()) {
            if (tempo_active_ && tempo_rate_ == rate && tempo_preserve_pitch_ == preserve_pitch)
                return true;
            tempo_preserve_pitch_ = preserve_pitch;
            return ConfigureTempo(rate, error);
        }
        if (!enabled) {
            ResetTempo();
            tempo_rate_ = rate;
            tempo_preserve_pitch_ = preserve_pitch;
            return true;
        }
        if (tempo_active_ && tempo_preserve_pitch_
                && std::abs(tempo_rate_ - rate) <= 1e-6f) {
            return true;
        }
        ResetTempo();
        tempo_rate_ = rate;
        tempo_preserve_pitch_ = true;
        return ConfigureTempo(rate, error);
    }

    int Pull(float* output, const int requested_frames,
             const float* gains, const int gain_count, const float master_gain) {
        output_peak = 0.0f;
        if (!tempo_active_) {
            return PullSource(output, requested_frames, gains, gain_count, master_gain);
        }
        return PullPitchPreserved(output, requested_frames, gains, gain_count, master_gain);
    }

    int PullSource(float* output, const int requested_frames,
                   const float* gains, const int gain_count, const float master_gain) {
        if (external_pull) return external_pull(output, requested_frames, gains, gain_count, master_gain);
        if (!output || requested_frames <= 0 || gain_count != static_cast<int>(decoders_.size())) {
            return -1;
        }
        const int64_t first = cursor_sample_;
        int64_t end = first + requested_frames;
        for (auto& decoder : decoders_)
            if (decoder->last_sample_hint >= 0 && first >= decoder->last_sample_hint)
                FlushDecoder(*decoder);
        if (std::all_of(decoders_.begin(), decoders_.end(), [](const auto& decoder) {
                return decoder->input_eof || decoder->failed;
            })) input_eof_ = true;
        if (!DecodeUntil(end)) return -1;
        if (std::all_of(decoders_.begin(), decoders_.end(), [](const auto& decoder) {
                return decoder->input_eof || decoder->failed;
            })) input_eof_ = true;

        int produced_frames = requested_frames;
        if (input_eof_) {
            int64_t last_sample = first;
            for (const auto& decoder : decoders_)
                for (const Segment& segment : decoder->segments)
                    last_sample = std::max(last_sample, segment.LastSampleExclusive());
            end = std::min(end, last_sample);
            produced_frames = static_cast<int>(end - first);
            if (produced_frames <= 0) return 0;
        }

        std::fill(output, output + static_cast<size_t>(produced_frames) * kChannels, 0.0f);
        for (int index = 0; index < gain_count; ++index) {
            // Application stems use a reversible delta against the hidden
            // desktop base, so their gain legitimately ranges from -1 to 0.
            const float gain = std::clamp(gains[index], -2.0f, 2.0f);
            auto& decoder = *decoders_[index];
            decoder.gain.SetTarget(gain);
            std::vector<float> ramp(produced_frames);
            for (auto& value : ramp) value = decoder.gain.Next();
            MixDecoder(decoder, first, end, ramp.data(), output);
        }
        const float master = std::clamp(master_gain, 0.0f, 1.0f);
        master_ramp_.SetTarget(master);
        for (int frame = 0; frame < produced_frames; ++frame) {
            const float master_value = master_ramp_.Next();
            float seek_gain = 1.0f;
            if (seek_fade_remaining_ > 0) {
                seek_gain = static_cast<float>(
                    kSeekFadeFrames - seek_fade_remaining_) / kSeekFadeFrames;
                --seek_fade_remaining_;
            }
            for (int channel = 0; channel < kChannels; ++channel) {
                const size_t sample = static_cast<size_t>(frame) * kChannels + channel;
                const float value = output[sample] * master_value;
                output_peak = std::max(output_peak, std::abs(value));
                output[sample] = (unlimited_output || !output_filter.empty() ? value :
                    std::clamp(value, -kLimiterCeiling, kLimiterCeiling)) * seek_gain;
            }
        }
        cursor_sample_ = end;
        return produced_frames;
    }

    bool Seek(const int64_t position_ms, std::string& error) {
        const int64_t clamped_ms = std::max<int64_t>(0, position_ms);
        const int64_t relative_target = av_rescale_q(
            clamped_ms, AVRational{1, 1000}, seek_time_base_);
        const int64_t container_origin = av_rescale_q(
            container_start_time_us_, AVRational{1, AV_TIME_BASE}, seek_time_base_);
        const int64_t target = container_origin + relative_target;
        // Seek on an audio stream, not the global/video timeline. Global seeks
        // snap to a potentially distant video keyframe; Pull() then emitted
        // silence while every AAC decoder caught up. AAC packets are valid
        // seek points, and AVSEEK_FLAG_ANY keeps the first post-seek block near
        // the requested editor position.
        const int result = av_seek_frame(
            format_, seek_stream_index_, target,
            AVSEEK_FLAG_BACKWARD | AVSEEK_FLAG_ANY);
        if (result < 0) {
            error = "could not seek clip audio: " + ErrorText(result);
            return false;
        }
        // av_seek_frame already flushes the demuxer and restores timestamp
        // state at the seek position. A second avformat_flush resets WAV's
        // sample timestamps to zero, making every post-seek packet look old.
        cursor_sample_ = av_rescale_q(clamped_ms, AVRational{1, 1000},
                                      AVRational{1, kSampleRate});
        seek_fade_remaining_ = kSeekFadeFrames;
        master_ramp_.Reset();
        input_eof_ = false;
        for (const auto& decoder : decoders_) {
            if (decoder->unavailable) continue;
            decoder->gain.Reset();
            avcodec_flush_buffers(decoder->codec);
            swr_close(decoder->resampler);
            if (swr_init(decoder->resampler) < 0) {
                error = "could not reset clip audio resampler";
                return false;
            }
            decoder->segments.clear();
            decoder->decoded_until = std::numeric_limits<int64_t>::min();
            decoder->fallback_next_sample = cursor_sample_;
            decoder->input_eof = false;
            decoder->flushed = false;
            decoder->failed = false;
            if (decoder->filter && !decoder->filter->Open(decoder->processing)) {
                error = "could not reset source processing after seek";
                return false;
            }
        }
        if (tempo_active_ && !ConfigureTempo(tempo_rate_, error)) return false;
        return true;
    }

    [[nodiscard]] bool IsEof() const { return input_eof_; }
    [[nodiscard]] int64_t BufferedFrames() const {
        int64_t count = 0;
        for (const auto& decoder : decoders_)
            for (const auto& segment : decoder->segments)
                count += static_cast<int64_t>(segment.samples.size() / kChannels);
        return count;
    }
    [[nodiscard]] bool SourceFailed(const int source_ordinal) const {
        return source_ordinal < 0 || source_ordinal >= static_cast<int>(decoders_.size())
            || decoders_[source_ordinal]->failed;
    }

private:
    void ResetTempo() {
        avfilter_graph_free(&tempo_graph_);
        tempo_source_ = nullptr;
        tempo_sink_ = nullptr;
        tempo_pending_.clear();
        tempo_pending_offset_frames_ = 0;
        tempo_input_pts_ = 0;
        tempo_input_eof_ = false;
        tempo_eof_sent_ = false;
        tempo_filter_eof_ = false;
        tempo_active_ = false;
    }

    bool ConfigureTempo(const float rate, std::string& error) {
        const bool preserve = tempo_preserve_pitch_;
        ResetTempo();
        std::string chain = output_filter.empty() ? "anull" : output_filter;
        if (std::abs(rate - 1.0f) > 1e-6f) {
            if (preserve) {
                float remaining = rate;
                while (remaining < 0.5f) { chain += ",atempo=0.5"; remaining /= 0.5f; }
                chain += ",atempo=" + std::to_string(remaining);
            } else {
                chain += ",asetrate=" + std::to_string(static_cast<int>(kSampleRate * rate))
                    + ",aresample=48000";
            }
        }
        FthrAudioFilter filter;
        if (!filter.Open(chain)) {
            error = "could not configure shared audio output processing";
            return false;
        }
        tempo_graph_ = filter.graph; filter.graph = nullptr;
        tempo_source_ = filter.source; tempo_sink_ = filter.sink;
        tempo_active_ = true;
        tempo_rate_ = rate;
        tempo_preserve_pitch_ = preserve;
        return true;
    }

    int CopyTempoPending(float* output, const int output_offset,
                         const int requested_frames) {
        const int available = static_cast<int>(tempo_pending_.size() / kChannels)
            - tempo_pending_offset_frames_;
        const int copied = std::max(0, std::min(requested_frames, available));
        if (copied > 0) {
            const size_t source = static_cast<size_t>(tempo_pending_offset_frames_) * kChannels;
            std::memcpy(output + static_cast<size_t>(output_offset) * kChannels,
                        tempo_pending_.data() + source,
                        static_cast<size_t>(copied) * kChannels * sizeof(float));
            tempo_pending_offset_frames_ += copied;
        }
        if (tempo_pending_offset_frames_ >= static_cast<int>(tempo_pending_.size() / kChannels)) {
            tempo_pending_.clear();
            tempo_pending_offset_frames_ = 0;
        }
        return copied;
    }

    bool DrainTempo(std::string& error) {
        while (true) {
            AVFrame* frame = av_frame_alloc();
            if (!frame) {
                error = "could not allocate pitch-preserving output frame";
                return false;
            }
            const int result = av_buffersink_get_frame(tempo_sink_, frame);
            if (result == AVERROR(EAGAIN)) {
                av_frame_free(&frame);
                return true;
            }
            if (result == AVERROR_EOF) {
                tempo_filter_eof_ = true;
                av_frame_free(&frame);
                return true;
            }
            if (result < 0) {
                error = "could not read pitch-preserving audio: " + ErrorText(result);
                av_frame_free(&frame);
                return false;
            }
            if (frame->nb_samples > 0 && frame->data[0]) {
                const size_t samples = static_cast<size_t>(frame->nb_samples) * kChannels;
                const size_t old_size = tempo_pending_.size();
                tempo_pending_.resize(old_size + samples);
                std::memcpy(tempo_pending_.data() + old_size, frame->data[0],
                            samples * sizeof(float));
            }
            av_frame_free(&frame);
        }
    }

    bool FeedTempo(const float* samples, const int frames, std::string& error) {
        AVFrame* frame = av_frame_alloc();
        if (!frame) {
            error = "could not allocate pitch-preserving input frame";
            return false;
        }
        frame->format = AV_SAMPLE_FMT_FLT;
        frame->sample_rate = kSampleRate;
        frame->nb_samples = frames;
        frame->pts = tempo_input_pts_;
        av_channel_layout_default(&frame->ch_layout, kChannels);
        int result = av_frame_get_buffer(frame, 0);
        if (result >= 0) {
            std::memcpy(frame->data[0], samples,
                        static_cast<size_t>(frames) * kChannels * sizeof(float));
            result = av_buffersrc_add_frame_flags(
                tempo_source_, frame, AV_BUFFERSRC_FLAG_KEEP_REF);
        }
        av_frame_free(&frame);
        if (result < 0) {
            error = "could not feed pitch-preserving audio: " + ErrorText(result);
            return false;
        }
        tempo_input_pts_ += frames;
        return true;
    }

    int PullPitchPreserved(float* output, const int requested_frames,
                           const float* gains, const int gain_count,
                           const float master_gain) {
        int produced = CopyTempoPending(output, 0, requested_frames);
        while (produced < requested_frames) {
            std::string error;
            if (!DrainTempo(error)) return -1;
            produced += CopyTempoPending(output, produced, requested_frames - produced);
            if (produced >= requested_frames) break;
            if (tempo_filter_eof_) return produced;

            if (tempo_input_eof_) {
                if (tempo_eof_sent_) return produced;
                const int result = av_buffersrc_add_frame_flags(tempo_source_, nullptr, 0);
                if (result < 0 && result != AVERROR_EOF) return -1;
                tempo_eof_sent_ = true;
                continue;
            }

            // Time stretching needs its overlap window. At normal speed keep
            // DSP frames short so a gain edit reaches the limiter promptly.
            const int feed_frames = std::abs(tempo_rate_ - 1.0f) < 1e-6f ? 512 : 2048;
            std::vector<float> source(static_cast<size_t>(feed_frames) * kChannels);
            const int source_frames = PullSource(
                source.data(), feed_frames, gains, gain_count, master_gain);
            if (source_frames < 0) return -1;
            if (source_frames == 0) {
                tempo_input_eof_ = true;
                continue;
            }
            if (!FeedTempo(source.data(), source_frames, error)) return -1;
        }
        return produced;
    }

    bool ConfigureDecoder(DecoderState& decoder, const int stream_index,
                          AVStream& stream, std::string& error) {
        const AVCodec* codec = avcodec_find_decoder(stream.codecpar->codec_id);
        if (!codec) {
            error = "no FFmpeg decoder for selected audio stream";
            return false;
        }
        decoder.codec = avcodec_alloc_context3(codec);
        if (!decoder.codec) {
            error = "could not allocate selected audio decoder";
            return false;
        }
        int result = avcodec_parameters_to_context(decoder.codec, stream.codecpar);
        // The controller owns exactly one decoder worker. Do not let each
        // selected AAC stem create an unbounded FFmpeg worker pool.
        decoder.codec->thread_count = 1;
        if (result < 0 || (result = avcodec_open2(decoder.codec, codec, nullptr)) < 0) {
            error = "could not open selected audio decoder: " + ErrorText(result);
            return false;
        }
        AVChannelLayout output_layout{};
        av_channel_layout_default(&output_layout, kChannels);
        result = swr_alloc_set_opts2(
            &decoder.resampler, &output_layout, AV_SAMPLE_FMT_FLT, kSampleRate,
            &decoder.codec->ch_layout, decoder.codec->sample_fmt,
            decoder.codec->sample_rate, 0, nullptr);
        av_channel_layout_uninit(&output_layout);
        if (result < 0 || !decoder.resampler || swr_init(decoder.resampler) < 0) {
            error = "could not create selected audio resampler";
            return false;
        }
        decoder.stream_index = stream_index;
        decoder.time_base = stream.time_base;
        if (stream.start_time != AV_NOPTS_VALUE)
            decoder.first_sample_hint = av_rescale_q(stream.start_time, stream.time_base,
                AVRational{1, kSampleRate}) - container_origin_sample_;
        if (const auto* tag = av_dict_get(stream.metadata, "handler_name", nullptr, 0)) {
            const auto timing = fthr::AudioSourceTiming(tag->value);
            if (timing.first >= 0) decoder.first_sample_hint = timing.first;
            decoder.last_sample_hint = timing.last;
        }
        return true;
    }

    bool DecodeUntil(const int64_t target_sample) {
        if (ReadyFor(target_sample)) return true;
        for (int count = 0; count < kReadPacketBudget; ++count) {
            const int result = av_read_frame(format_, packet_);
            if (result == AVERROR_EOF) {
                input_eof_ = true;
                for (const auto& decoder : decoders_) FlushDecoder(*decoder);
                return true;
            }
            if (result < 0) return false;
            for (const auto& decoder : decoders_) {
                if (!decoder->failed && !decoder->input_eof
                        && decoder->stream_index == packet_->stream_index) {
                    if (!SendPacket(*decoder, packet_)) decoder->failed = true;
                    if (decoder->last_sample_hint >= 0 && decoder->decoded_until >= decoder->last_sample_hint)
                        FlushDecoder(*decoder);
                }
            }
            av_packet_unref(packet_);
            if (ReadyFor(target_sample)) return true;
        }
        // The queue remains bounded even for a malformed stream. A later pull
        // continues decoding; meanwhile QAudioSink receives an intentional
        // short silence instead of blocking an audio callback indefinitely.
        return true;
    }

    [[nodiscard]] bool ReadyFor(const int64_t target_sample) const {
        return std::all_of(decoders_.begin(), decoders_.end(),
                           [target_sample](const auto& decoder) {
                               return decoder->input_eof || decoder->failed
                                   || decoder->first_sample_hint >= target_sample
                                   || decoder->decoded_until >= target_sample;
                           });
    }

    bool SendPacket(DecoderState& decoder, const AVPacket* packet) {
        int result = avcodec_send_packet(decoder.codec, packet);
        if (result == AVERROR(EAGAIN)) {
            if (!ReceiveFrames(decoder)) return false;
            result = avcodec_send_packet(decoder.codec, packet);
        }
        if (result < 0 && result != AVERROR_EOF) return false;
        return ReceiveFrames(decoder);
    }

    void FlushDecoder(DecoderState& decoder) {
        if (decoder.flushed) return;
        decoder.flushed = true;
        decoder.input_eof = true;
        if (!decoder.failed) {
            if (!SendPacket(decoder, nullptr)) decoder.failed = true;
            const int delayed = static_cast<int>(swr_get_delay(
                decoder.resampler, decoder.codec->sample_rate));
            if (delayed > 0 && !decoder.failed) {
                const int capacity = ResampleCapacity(delayed, decoder.codec->sample_rate);
                std::vector<float> tail(static_cast<size_t>(capacity) * kChannels);
                uint8_t* planes[] = {reinterpret_cast<uint8_t*>(tail.data()), nullptr};
                const int converted = swr_convert(decoder.resampler, planes, capacity, nullptr, 0);
                if (converted > 0) {
                    tail.resize(static_cast<size_t>(converted) * kChannels);
                    AppendSegment(decoder, decoder.fallback_next_sample, std::move(tail));
                }
            }
            if (decoder.filter && !decoder.failed) {
                if (decoder.filter->Send(nullptr, 0, 0) < 0) decoder.failed = true;
                else DrainSourceFilter(decoder);
            }
        }
    }

    bool ReceiveFrames(DecoderState& decoder) {
        while (true) {
            const int result = avcodec_receive_frame(decoder.codec, frame_);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
            if (result < 0) return false;
            const int64_t resample_delay = swr_get_delay(decoder.resampler, kSampleRate);
            const int capacity = ResampleCapacity(
                swr_get_delay(decoder.resampler, decoder.codec->sample_rate) + frame_->nb_samples,
                decoder.codec->sample_rate);
            std::vector<float> converted(static_cast<size_t>(capacity) * kChannels);
            uint8_t* planes[] = {reinterpret_cast<uint8_t*>(converted.data()), nullptr};
            const int samples = swr_convert(
                decoder.resampler, planes, capacity,
                const_cast<const uint8_t**>(frame_->extended_data), frame_->nb_samples);
            if (samples < 0) {
                av_frame_unref(frame_);
                return false;
            }
            converted.resize(static_cast<size_t>(samples) * kChannels);
            int64_t source_timestamp = frame_->best_effort_timestamp;
            if (source_timestamp == AV_NOPTS_VALUE) source_timestamp = frame_->pts;
            int64_t first = source_timestamp == AV_NOPTS_VALUE
                ? decoder.fallback_next_sample
                : av_rescale_q(source_timestamp, decoder.time_base,
                               AVRational{1, kSampleRate}) - container_origin_sample_
                    - resample_delay;
            // SWR retains fractional-rate filter history. Its next output
            // starts before this input frame's PTS; ignoring that delay puts
            // a gap into every 44.1 kHz frame. Absorb a one-sample rounding
            // difference while preserving actual timestamp discontinuities.
            if (decoder.decoded_until != std::numeric_limits<int64_t>::min()
                    && std::abs(first - decoder.fallback_next_sample) <= 1) {
                first = decoder.fallback_next_sample;
            }
            av_frame_unref(frame_);
            AppendSegment(decoder, first, std::move(converted));
        }
    }

    void AppendSegment(DecoderState& decoder, const int64_t first,
                       std::vector<float>&& samples) {
        const int64_t frames = static_cast<int64_t>(samples.size() / kChannels);
        if (frames == 0) return;
        decoder.fallback_next_sample = first + frames;
        if (decoder.filter) {
            if (decoder.filter->Send(samples.data(), static_cast<int>(frames), first) < 0) {
                decoder.failed = true;
                return;
            }
            DrainSourceFilter(decoder);
            return;
        }
        decoder.decoded_until = std::max(decoder.decoded_until, first + frames);
        decoder.segments.push_back(Segment{first, std::move(samples)});
    }

    void DrainSourceFilter(DecoderState& decoder) {
        while (true) {
            std::vector<float> samples;
            int64_t pts = 0;
            const int result = decoder.filter->Read(samples, pts);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return;
            if (result < 0) { decoder.failed = true; return; }
            const int64_t frames = static_cast<int64_t>(samples.size() / kChannels);
            decoder.decoded_until = std::max(decoder.decoded_until, pts + frames);
            decoder.segments.push_back(Segment{pts, std::move(samples)});
        }
    }

    void MixDecoder(DecoderState& decoder, const int64_t first,
                    const int64_t end, const float* gains, float* output) {
        while (!decoder.segments.empty()
               && decoder.segments.front().LastSampleExclusive() <= first) {
            decoder.segments.pop_front();
        }
        for (const Segment& segment : decoder.segments) {
            const int64_t overlap_first = std::max(first, segment.first_sample);
            const int64_t overlap_end = std::min(end, segment.LastSampleExclusive());
            if (overlap_first >= overlap_end) continue;
            const size_t source_offset = static_cast<size_t>(overlap_first - segment.first_sample) * kChannels;
            const size_t output_offset = static_cast<size_t>(overlap_first - first) * kChannels;
            const size_t sample_count = static_cast<size_t>(overlap_end - overlap_first) * kChannels;
            for (size_t sample = 0; sample < sample_count; ++sample) {
                output[output_offset + sample] += segment.samples[source_offset + sample]
                    * gains[(output_offset + sample) / kChannels];
            }
        }
    }

    AVFormatContext* format_ = nullptr;
    AVPacket* packet_ = nullptr;
    AVFrame* frame_ = nullptr;
    std::vector<std::unique_ptr<DecoderState>> decoders_;
    int seek_stream_index_ = -1;
    AVRational seek_time_base_{1, kSampleRate};
    int64_t seek_start_time_ = AV_NOPTS_VALUE;
    int64_t container_start_time_us_ = 0;
    int64_t container_origin_sample_ = 0;
    int64_t cursor_sample_ = 0;
    int seek_fade_remaining_ = 0;
    bool input_eof_ = false;
    fthr::AudioGainRamp master_ramp_;
    AVFilterGraph* tempo_graph_ = nullptr;
    AVFilterContext* tempo_source_ = nullptr;
    AVFilterContext* tempo_sink_ = nullptr;
    std::vector<float> tempo_pending_;
    int tempo_pending_offset_frames_ = 0;
    int64_t tempo_input_pts_ = 0;
    float tempo_rate_ = 1.0f;
    bool tempo_preserve_pitch_ = false;
    bool tempo_active_ = false;
    bool tempo_input_eof_ = false;
    bool tempo_eof_sent_ = false;
    bool tempo_filter_eof_ = false;
};

} // namespace

struct FTHRPlaybackMixer {
    Mixer mixer;
    fthr::AudioGainRamp master_ramp;
    struct Input {
        std::string path;
        std::unique_ptr<Mixer> decoder;
        std::vector<int> indexes;
        std::vector<int> ordinals;
    };
    std::vector<Input> inputs;
    int source_count = 0;
    bool eof = false;

    int Pull(float* output, int frames, const float* gains, int count, float master) {
        return mixer.Pull(output, frames, gains, count, master);
    }

    int PullInputs(float* output, int frames, const float* gains, int count, float master) {
        if (!output || !gains || frames < 1 || frames > kSampleRate || count != source_count)
            return -1;
        std::fill(output, output + static_cast<size_t>(frames) * kChannels, 0.0f);
        std::vector<float> block(static_cast<size_t>(frames) * kChannels);
        int produced = 0;
        for (auto& input : inputs) {
            std::vector<float> selected;
            for (int ordinal : input.ordinals) selected.push_back(gains[ordinal]);
            const int read = input.decoder->Pull(block.data(), frames,
                selected.data(), static_cast<int>(selected.size()), 1.0f);
            if (read < 0) return -1;
            produced = std::max(produced, read);
            for (int index = 0; index < read * kChannels; ++index) output[index] += block[index];
        }
        master_ramp.SetTarget(std::clamp(master, 0.0f, 1.0f));
        for (int frame = 0; frame < produced; ++frame) {
            const float gain = master_ramp.Next();
            for (int channel = 0; channel < kChannels; ++channel) {
                auto& value = output[frame * kChannels + channel];
                value *= gain;
                mixer.output_peak = std::max(mixer.output_peak, std::abs(value));
                if (mixer.output_filter.empty())
                    value = std::clamp(value, -kLimiterCeiling, kLimiterCeiling);
            }
        }
        eof = produced == 0;
        return produced;
    }

    bool Seek(int64_t position, std::string& error) {
        if (inputs.empty()) return mixer.Seek(position, error);
        eof = false;
        master_ramp.Reset();
        for (auto& input : inputs) {
            if (!input.decoder->Seek(position, error)) return false;
        }
        return mixer.ResetExternalClock(error);
    }

    bool SetPlaybackRate(float rate, bool preserve_pitch, std::string& error) {
        // Stretch the complete mix once, matching export. Stretching each
        // input independently chooses different WSOLA windows for each source.
        return mixer.SetPlaybackRate(rate, preserve_pitch, error);
    }

    bool SourceFailed(int ordinal) const {
        if (inputs.empty()) return mixer.SourceFailed(ordinal);
        for (const auto& input : inputs) {
            for (size_t index = 0; index < input.ordinals.size(); ++index) {
                if (input.ordinals[index] == ordinal)
                    return input.decoder->SourceFailed(static_cast<int>(index));
            }
        }
        return true;
    }
};

extern "C" int fthr_playback_configure_dsp(FTHRPlaybackMixer* handle,
    const char* const* source_filters, int count, const char* output_filter,
    char* error_text, size_t error_capacity) {
    if (!handle || !source_filters || !output_filter || std::strlen(output_filter) > 4096)
        return 0;
    std::string error;
    if (handle->inputs.empty()) {
        for (int index = 0; index < count; ++index) {
            if (!handle->mixer.SetProcessing(index, source_filters[index], error)) {
                WriteError(error_text, error_capacity, error); return 0;
            }
        }
    } else {
        if (count != handle->source_count) return 0;
        for (auto& input : handle->inputs) {
            for (size_t index = 0; index < input.ordinals.size(); ++index) {
                if (!input.decoder->SetProcessing(static_cast<int>(index),
                        source_filters[input.ordinals[index]], error)) {
                    WriteError(error_text, error_capacity, error); return 0;
                }
            }
        }
    }
    handle->mixer.output_filter = output_filter;
    if (!handle->mixer.SetPlaybackRate(1.0f, true, error)) {
        WriteError(error_text, error_capacity, error); return 0;
    }
    return 1;
}

extern "C" float fthr_playback_output_peak(const FTHRPlaybackMixer* handle) {
    return handle ? handle->mixer.output_peak : 0.0f;
}

extern "C" int64_t fthr_playback_buffered_frames(const FTHRPlaybackMixer* handle) {
    if (!handle) return 0;
    int64_t total = handle->mixer.BufferedFrames();
    for (const auto& input : handle->inputs) total += input.decoder->BufferedFrames();
    return total;
}

extern "C" FTHRPlaybackMixer* fthr_playback_open_sources(
    const char* const* paths, const int* stream_indexes, const int stream_count,
    char* error_text, const size_t error_text_capacity) {
    if (!paths || !stream_indexes || stream_count < 1 || stream_count > 64) {
        WriteError(error_text, error_text_capacity, "invalid imported audio selection");
        return nullptr;
    }
    auto handle = std::make_unique<FTHRPlaybackMixer>();
    handle->source_count = stream_count;
    for (int index = 0; index < stream_count; ++index) {
        if (!paths[index] || !*paths[index]) {
            WriteError(error_text, error_text_capacity, "missing local audio path");
            return nullptr;
        }
        auto found = std::find_if(handle->inputs.begin(), handle->inputs.end(),
            [&](const auto& input) { return input.path == paths[index]; });
        if (found == handle->inputs.end()) {
            handle->inputs.push_back({paths[index], std::make_unique<Mixer>(), {}, {}});
            found = std::prev(handle->inputs.end());
        }
        found->indexes.push_back(stream_indexes[index]);
        found->ordinals.push_back(index);
    }
    for (auto& input : handle->inputs) {
        std::string error;
        input.decoder->unlimited_output = true;
        if (!input.decoder->Open(input.path.c_str(), input.indexes.data(),
                static_cast<int>(input.indexes.size()), error)) {
            WriteError(error_text, error_text_capacity, error);
            return nullptr;
        }
    }
    handle->mixer.external_pull = [owner = handle.get()](
            float* output, int frames, const float* gains, int count, float master) {
        return owner->PullInputs(output, frames, gains, count, master);
    };
    return handle.release();
}

extern "C" FTHRPlaybackMixer* fthr_playback_open(
    const char* media_path_utf8, const int* stream_indexes, const int stream_count,
    char* error_text, const size_t error_text_capacity) {
    auto handle = std::make_unique<FTHRPlaybackMixer>();
    std::string error;
    if (!handle->mixer.Open(media_path_utf8, stream_indexes, stream_count, error)) {
        WriteError(error_text, error_text_capacity, error);
        return nullptr;
    }
    return handle.release();
}

extern "C" int fthr_playback_pull(
    FTHRPlaybackMixer* mixer, float* interleaved_float_stereo, const int frame_count,
    const float* gains, const int gain_count, const float master_gain) {
    if (!mixer) return -1;
    return mixer->Pull(interleaved_float_stereo, frame_count, gains, gain_count, master_gain);
}

extern "C" int fthr_playback_set_playback_rate(
    FTHRPlaybackMixer* mixer, const float playback_rate, const int preserve_pitch,
    char* error_text, const size_t error_text_capacity) {
    if (!mixer) {
        WriteError(error_text, error_text_capacity, "playback mixer is not open");
        return 0;
    }
    std::string error;
    if (!mixer->SetPlaybackRate(playback_rate, preserve_pitch != 0, error)) {
        WriteError(error_text, error_text_capacity, error);
        return 0;
    }
    return 1;
}

extern "C" int fthr_playback_seek(
    FTHRPlaybackMixer* mixer, const int64_t position_milliseconds,
    char* error_text, const size_t error_text_capacity) {
    if (!mixer) {
        WriteError(error_text, error_text_capacity, "playback mixer is not open");
        return 0;
    }
    std::string error;
    if (!mixer->Seek(position_milliseconds, error)) {
        WriteError(error_text, error_text_capacity, error);
        return 0;
    }
    return 1;
}

extern "C" int fthr_playback_is_eof(const FTHRPlaybackMixer* mixer) {
    return mixer && (mixer->inputs.empty() ? mixer->mixer.IsEof() : mixer->eof) ? 1 : 0;
}

extern "C" int fthr_playback_source_failed(
    const FTHRPlaybackMixer* mixer, const int source_ordinal) {
    return mixer && mixer->SourceFailed(source_ordinal) ? 1 : 0;
}

extern "C" void fthr_playback_close(FTHRPlaybackMixer* mixer) {
    delete mixer;
}
