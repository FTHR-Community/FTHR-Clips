#include "continuous_recording_writer.h"
#include "encoded_video_config_ffmpeg.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
}

#include <cstring>
#include <iostream>
#include <limits>
#include <utility>

namespace fthr {
namespace {

bool CopyExtradata(AVCodecParameters* parameters, const std::vector<uint8_t>& data) {
    if (!parameters || data.empty()
        || data.size() > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return false;
    }
    parameters->extradata = static_cast<uint8_t*>(
        av_malloc(data.size() + AV_INPUT_BUFFER_PADDING_SIZE));
    if (!parameters->extradata) return false;
    std::memcpy(parameters->extradata, data.data(), data.size());
    std::memset(
        parameters->extradata + data.size(), 0, AV_INPUT_BUFFER_PADDING_SIZE);
    parameters->extradata_size = static_cast<int>(data.size());
    return true;
}

} // namespace

ContinuousRecordingWriter::~ContinuousRecordingWriter() {
    Stop();
}

bool ContinuousRecordingWriter::Start(
    const std::filesystem::path& output_path,
    const EncodedVideoConfig& video_config,
    const ContinuousRecordingAudioConfig& audio_config) {
    if (running_.load(std::memory_order_acquire) || thread_.joinable()) {
        Fail("A recording writer is already active.");
        return false;
    }
    if (output_path.empty() || !IsValidEncodedVideoConfig(video_config)
        || !IsMp4PacketFormatCompatible(video_config)
        || video_config.codec_extradata.empty()) {
        Fail("The active replay stream is not ready for recording.");
        return false;
    }

    video_config_ = video_config;
    audio_config_ = audio_config.valid()
        ? audio_config : ContinuousRecordingAudioConfig{};
    audio_enabled_.store(audio_config_.valid(), std::memory_order_release);
    failed_.store(false, std::memory_order_release);
    last_error_.clear();
    video_started_ = false;
    audio_started_ = false;
    video_packets_written_ = 0;
    audio_packets_written_ = 0;

    const std::string output_utf8 = output_path.u8string();
    if (avformat_alloc_output_context2(
            &format_context_, nullptr, "mp4", output_utf8.c_str()) < 0
        || !format_context_) {
        Fail("Could not create the fragmented MP4 container.");
        CloseContainer(false);
        return false;
    }

    format_context_->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;
    // Keep muxer interleave buffering bounded so completed fragments reach the
    // filesystem promptly even if one stream is temporarily quiet.
    format_context_->max_interleave_delta = 1'000'000;
    format_context_->flags |= AVFMT_FLAG_FLUSH_PACKETS;

    video_stream_ = avformat_new_stream(format_context_, nullptr);
    if (!video_stream_) {
        Fail("Could not create the recording video stream.");
        CloseContainer(false);
        return false;
    }
    video_stream_->codecpar->codec_type = AVMEDIA_TYPE_VIDEO;
    video_stream_->codecpar->codec_id = ToAvCodecId(video_config_.codec);
    video_stream_->codecpar->width = static_cast<int>(video_config_.width);
    video_stream_->codecpar->height = static_cast<int>(video_config_.height);
    video_stream_->codecpar->format = AV_PIX_FMT_YUV420P;
    ApplySdrBt709ColorMetadata(video_stream_->codecpar);
    video_stream_->time_base = AVRational{1, 90000};
    ApplyConfiguredVideoMetadata(
        format_context_, video_stream_, video_config_);
    if (!CopyExtradata(
            video_stream_->codecpar, video_config_.codec_extradata)) {
        Fail("Could not copy the recording video decoder configuration.");
        CloseContainer(false);
        return false;
    }

    if (audio_config_.valid()) {
        audio_stream_ = avformat_new_stream(format_context_, nullptr);
        if (!audio_stream_) {
            Fail("Could not create the recording audio stream.");
            CloseContainer(false);
            return false;
        }
        audio_stream_->codecpar->codec_type = AVMEDIA_TYPE_AUDIO;
        audio_stream_->codecpar->codec_id = AV_CODEC_ID_AAC;
        audio_stream_->codecpar->sample_rate =
            static_cast<int>(audio_config_.sample_rate);
        audio_stream_->codecpar->ch_layout.nb_channels =
            static_cast<int>(audio_config_.channels);
        audio_stream_->codecpar->ch_layout.order = AV_CHANNEL_ORDER_UNSPEC;
        audio_stream_->codecpar->frame_size = 1024;
        audio_stream_->codecpar->format = AV_SAMPLE_FMT_FLTP;
        audio_stream_->time_base =
            AVRational{1, static_cast<int>(audio_config_.sample_rate)};
        audio_stream_->disposition |= AV_DISPOSITION_DEFAULT;
        av_dict_set(
            &audio_stream_->metadata, "handler_name", "Default Mix", 0);
        av_dict_set(&audio_stream_->metadata, "title", "Default Mix", 0);
        if (!CopyExtradata(
                audio_stream_->codecpar, audio_config_.codec_extradata)) {
            Fail("Could not copy the recording audio decoder configuration.");
            CloseContainer(false);
            return false;
        }
    }

    if (avio_open(
            &format_context_->pb, output_utf8.c_str(), AVIO_FLAG_WRITE) < 0) {
        Fail("Could not open the recording file. Check the folder and free space.");
        CloseContainer(false);
        return false;
    }

    AVDictionary* options = nullptr;
    // frag_custom lets this writer close the current fragment explicitly at
    // every keyframe. empty_moov writes the recovery metadata at Start rather
    // than keeping the only copy in memory until Stop.
    av_dict_set(
        &options,
        "movflags",
        "empty_moov+frag_custom+default_base_moof+omit_tfhd_offset+use_metadata_tags",
        0);
    av_dict_set(&options, "flush_packets", "1", 0);
    const int header_error = avformat_write_header(format_context_, &options);
    av_dict_free(&options);
    if (header_error < 0) {
        Fail("Could not write the recoverable recording header.");
        CloseContainer(false);
        return false;
    }
    avio_flush(format_context_->pb);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.clear();
        queued_bytes_ = 0;
        accepting_ = true;
    }
    running_.store(true, std::memory_order_release);
    try {
        thread_ = std::thread(&ContinuousRecordingWriter::WriterThread, this);
    } catch (const std::exception& error) {
        running_.store(false, std::memory_order_release);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            accepting_ = false;
        }
        Fail(std::string("Could not start the recording writer: ") + error.what());
        CloseContainer(true);
        return false;
    }

    std::cout << "[ContinuousRecording] Recoverable fragmented MP4 started: "
              << output_utf8 << (audio_stream_ ? " (video + AAC)" : " (video only)")
              << std::endl;
    return true;
}

bool ContinuousRecordingWriter::PushVideo(
    const uint8_t* data,
    uint32_t size,
    int64_t pts,
    bool is_keyframe) {
    if (!data || size == 0) return true;
    QueuedPacket packet;
    packet.kind = PacketKind::Video;
    packet.data.assign(data, data + size);
    packet.pts = pts;
    packet.duration = 1;
    packet.is_keyframe = is_keyframe;
    return Enqueue(std::move(packet));
}

bool ContinuousRecordingWriter::PushAudio(
    const uint8_t* data,
    uint32_t size,
    int64_t pts_samples,
    int64_t duration_samples) {
    if (!audio_enabled_.load(std::memory_order_acquire)
        || !data || size == 0 || duration_samples <= 0) return true;
    QueuedPacket packet;
    packet.kind = PacketKind::Audio;
    packet.data.assign(data, data + size);
    packet.pts = pts_samples;
    packet.duration = duration_samples;
    return Enqueue(std::move(packet));
}

bool ContinuousRecordingWriter::Enqueue(QueuedPacket packet) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!accepting_ || failed_.load(std::memory_order_relaxed)) return false;
    if (packet.data.size() > kMaxQueuedBytes
        || queued_bytes_ > kMaxQueuedBytes - packet.data.size()) {
        FailLocked(
            "The recording disk could not keep up with capture. The completed "
            "fragments already on disk remain playable.");
        accepting_ = false;
        condition_.notify_all();
        return false;
    }
    queued_bytes_ += packet.data.size();
    queue_.push_back(std::move(packet));
    condition_.notify_one();
    return true;
}

bool ContinuousRecordingWriter::Stop() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        accepting_ = false;
    }
    condition_.notify_all();
    if (thread_.joinable()) thread_.join();
    else if (format_context_) {
        CloseContainer(true);
        running_.store(false, std::memory_order_release);
    }
    return !failed_.load(std::memory_order_acquire)
        && video_packets_written_ > 0;
}

void ContinuousRecordingWriter::WriterThread() {
    while (true) {
        QueuedPacket packet;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            condition_.wait(lock, [this] {
                return !queue_.empty() || !accepting_;
            });
            if (queue_.empty()) {
                if (!accepting_) break;
                continue;
            }
            packet = std::move(queue_.front());
            queue_.pop_front();
            queued_bytes_ -= packet.data.size();
        }

        if (!WritePacket(packet)) {
            std::lock_guard<std::mutex> lock(mutex_);
            accepting_ = false;
            queue_.clear();
            queued_bytes_ = 0;
            break;
        }
    }

    if (video_packets_written_ > 0 && !FlushFragment()
        && !failed_.load(std::memory_order_acquire)) {
        Fail("Could not flush the final recording fragment.");
    }
    CloseContainer(true);
    running_.store(false, std::memory_order_release);
    std::cout << "[ContinuousRecording] Closed ("
              << video_packets_written_ << " video, "
              << audio_packets_written_ << " audio packets, "
              << (failed_.load() ? "recoverable partial" : "complete")
              << ")." << std::endl;
}

bool ContinuousRecordingWriter::WritePacket(const QueuedPacket& packet) {
    if (!format_context_ || packet.data.empty()) return false;

    if (packet.kind == PacketKind::Video) {
        if (!video_started_) {
            // A fragmented recording must begin on a random-access point. The
            // replay encoder emits frequent GOPs, so at most one GOP is skipped.
            if (!packet.is_keyframe) return true;
            first_video_pts_ = packet.pts;
            video_started_ = true;
        } else if (packet.is_keyframe && video_packets_written_ > 0) {
            // Close and flush the previous GOP before beginning the next one.
            // A crash after this boundary cannot invalidate older fragments.
            if (!FlushFragment()) {
                Fail("Could not commit a completed recording fragment.");
                return false;
            }
        }
    } else {
        if (!video_started_ || !audio_stream_) return true;
        if (!audio_started_) {
            first_audio_pts_ = packet.pts;
            audio_started_ = true;
        }
    }

    AVPacket* output = av_packet_alloc();
    if (!output) {
        Fail("Out of memory while writing the recording.");
        return false;
    }
    const int allocation_error =
        av_new_packet(output, static_cast<int>(packet.data.size()));
    if (allocation_error < 0) {
        av_packet_free(&output);
        Fail("Out of memory while copying a recording packet.");
        return false;
    }
    std::memcpy(output->data, packet.data.data(), packet.data.size());

    if (packet.kind == PacketKind::Video) {
        // Preserve the capture clock. Delayed or missing capture frames must
        // create a longer interval, not make the surviving frames play faster.
        output->pts = packet.pts - first_video_pts_;
        output->dts = output->pts;
        output->duration = packet.duration;
        output->stream_index = video_stream_->index;
        output->flags = packet.is_keyframe ? AV_PKT_FLAG_KEY : 0;
        const AVRational input_time_base = {
            video_config_.time_base.numerator,
            video_config_.time_base.denominator};
        av_packet_rescale_ts(output, input_time_base, video_stream_->time_base);
    } else {
        output->pts = packet.pts - first_audio_pts_;
        output->dts = output->pts;
        output->duration = packet.duration;
        output->stream_index = audio_stream_->index;
        output->flags = AV_PKT_FLAG_KEY;
    }

    const int write_error = av_interleaved_write_frame(format_context_, output);
    av_packet_free(&output);
    if (write_error < 0) {
        Fail(
            "The recording file could not be written. The completed fragments "
            "already on disk remain playable.");
        return false;
    }
    if (packet.kind == PacketKind::Video) ++video_packets_written_;
    else ++audio_packets_written_;
    return true;
}

bool ContinuousRecordingWriter::FlushFragment() {
    if (!format_context_ || !format_context_->pb) return false;
    // First drain libavformat's interleave queue, then frag_custom turns the
    // explicit null write into an MP4 fragment boundary.
    if (av_interleaved_write_frame(format_context_, nullptr) < 0) return false;
    if (av_write_frame(format_context_, nullptr) < 0) return false;
    avio_flush(format_context_->pb);
    return format_context_->pb->error >= 0;
}

void ContinuousRecordingWriter::CloseContainer(bool write_trailer) {
    if (!format_context_) return;
    if (write_trailer && format_context_->pb) {
        // A trailer improves normal-close compatibility but is not the recovery
        // boundary. Every older fragment is already independently decodable.
        const int trailer_error = av_write_trailer(format_context_);
        if (trailer_error < 0) {
            std::cerr << "[ContinuousRecording] Trailer warning: "
                      << trailer_error << "; fragments remain recoverable."
                      << std::endl;
        }
    }
    if (format_context_->pb) {
        const int close_error = avio_closep(&format_context_->pb);
        if (close_error < 0) {
            std::cerr << "[ContinuousRecording] Close warning: "
                      << close_error << std::endl;
            Fail(
                "The recording file reported an error while closing. Completed "
                "fragments remain recoverable.");
        }
    }
    avformat_free_context(format_context_);
    format_context_ = nullptr;
    video_stream_ = nullptr;
    audio_stream_ = nullptr;
}

void ContinuousRecordingWriter::Fail(std::string message) {
    std::lock_guard<std::mutex> lock(mutex_);
    FailLocked(std::move(message));
}

void ContinuousRecordingWriter::FailLocked(std::string message) {
    if (last_error_.empty()) last_error_ = std::move(message);
    failed_.store(true, std::memory_order_release);
    std::cerr << "[ContinuousRecording] " << last_error_ << std::endl;
}

std::string ContinuousRecordingWriter::LastError() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

} // namespace fthr
