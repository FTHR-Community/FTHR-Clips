#include "continuous_recording_writer.h"
#include "encoded_video_config_ffmpeg.h"
#include "mp4_audio_track_headers.h"
#include "audio_source_handler.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
}

#include <cstring>
#include <algorithm>
#include <iostream>
#include <limits>
#include <new>
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
    const ContinuousRecordingAudioConfig& audio_config,
    uint64_t generation, bool independent_sources) {
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
    independent_sources_ = independent_sources;
    headers_finalized_ = false;
    output_path_ = output_path;
    first_video_timeline_100ns_ = 0;
    audio_streams_.clear(); source_infos_.clear(); source_slots_.clear();
    audio_config_ = audio_config.valid()
        ? audio_config : ContinuousRecordingAudioConfig{};
    if (independent_sources_)
        audio_config_ = ContinuousRecordingAudioConfig{48000, 2, {}, "pcm_f32le"};
    audio_enabled_.store(audio_config_.valid(), std::memory_order_release);
    failed_.store(false, std::memory_order_release);
    last_error_.clear();
    video_started_ = false;
    audio_started_ = false;
    first_video_pts_ = 0;
    first_audio_pts_ = 0;
    video_packets_written_ = 0;
    audio_packets_written_ = 0;
    last_video_pts_ = 0;
    generation_ = generation;
    pending_markers_.clear();
    admitted_marker_ids_.clear();
    queued_marker_count_ = 0;

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
    if (independent_sources_) video_stream_->id = 1;
    video_stream_->codecpar->codec_id = ToAvCodecId(video_config_.codec);
    video_stream_->codecpar->width = static_cast<int>(video_config_.width);
    video_stream_->codecpar->height = static_cast<int>(video_config_.height);
    video_stream_->codecpar->format = AV_PIX_FMT_YUV420P;
    video_stream_->time_base = AVRational{1, 90000};
    ApplyConfiguredVideoMetadata(
        format_context_, video_stream_, video_config_);
    if (!CopyExtradata(
            video_stream_->codecpar, video_config_.codec_extradata)) {
        Fail("Could not copy the recording video decoder configuration.");
        CloseContainer(false);
        return false;
    }

    const size_t audio_slots = independent_sources_ ? kMaxClipAudioTracks : (audio_config_.valid() ? 1 : 0);
    source_infos_.resize(audio_slots);
    source_written_.assign(audio_slots, false);
    source_committed_.assign(audio_slots, false);
    source_first_sample_.assign(audio_slots, -1);
    source_last_sample_.assign(audio_slots, -1);
    for (size_t slot = 0; slot < audio_slots; ++slot) {
        audio_stream_ = avformat_new_stream(format_context_, nullptr);
        if (!audio_stream_) {
            Fail("Could not create the recording audio stream.");
            CloseContainer(false);
            return false;
        }
        audio_stream_->codecpar->codec_type = AVMEDIA_TYPE_AUDIO;
        audio_streams_.push_back(audio_stream_);
        if (independent_sources_) audio_stream_->id = static_cast<int>(slot + 2);
        const bool lossless = audio_config_.codec == "pcm_f32le";
        audio_stream_->codecpar->codec_id = lossless ? AV_CODEC_ID_PCM_F32LE : AV_CODEC_ID_AAC;
        audio_stream_->codecpar->sample_rate =
            static_cast<int>(audio_config_.sample_rate);
        av_channel_layout_default(&audio_stream_->codecpar->ch_layout,
                                   static_cast<int>(audio_config_.channels));
        audio_stream_->codecpar->frame_size = lossless ? 0 : 1024;
        audio_stream_->codecpar->format = lossless ? AV_SAMPLE_FMT_FLT : AV_SAMPLE_FMT_FLTP;
        if (lossless) {
            audio_stream_->codecpar->bits_per_coded_sample = 32;
            audio_stream_->codecpar->block_align = audio_config_.channels * sizeof(float);
        }
        audio_stream_->time_base =
            AVRational{1, static_cast<int>(audio_config_.sample_rate)};
        audio_stream_->disposition |= AV_DISPOSITION_DEFAULT;
        av_dict_set(
            &audio_stream_->metadata, "handler_name", "Default Mix", 0);
        av_dict_set(&audio_stream_->metadata, "title", "Default Mix", 0);
        if (independent_sources_) {
            const std::string reserved = "FTHR reserved audio " + std::string(320, ' ');
            av_dict_set(&audio_stream_->metadata, "handler_name", reserved.c_str(), 0);
            av_dict_set(&audio_stream_->metadata, "title", nullptr, 0);
            if (slot != 0) audio_stream_->disposition &= ~AV_DISPOSITION_DEFAULT;
        }
        if (!lossless && !CopyExtradata(
                audio_stream_->codecpar, audio_config_.codec_extradata)) {
            Fail("Could not copy the recording audio decoder configuration.");
            CloseContainer(false);
            return false;
        }
    }
    audio_stream_ = audio_streams_.empty() ? nullptr : audio_streams_.front();

    if (avio_open(
            &format_context_->pb, output_utf8.c_str(), AVIO_FLAG_WRITE) < 0) {
        Fail("Could not open the recording file. Check the folder and free space.");
        CloseContainer(false);
        return false;
    }

    AVDictionary* options = nullptr;
    if (independent_sources_) {
        av_dict_set(&format_context_->metadata, "comment", "fthr-audio-mode=separated", 0);
        av_dict_set(&format_context_->metadata, "fthr_capture_audio", "independent-float-v1", 0);
    }
    // frag_custom lets this writer close the current fragment explicitly at
    // every keyframe. empty_moov writes the recovery metadata at Start rather
    // than keeping the only copy in memory until Stop.
    av_dict_set(
        &options,
        "movflags",
        "empty_moov+frag_custom+default_base_moof+omit_tfhd_offset+use_metadata_tags",
        0);
    av_dict_set(&options, "flush_packets", "1", 0);
    if (independent_sources_) {
        av_dict_set(&options, "use_stream_ids_as_track_ids", "1", 0);
        // Unstarted reserved tracks must retain DTS zero as their origin.
        // Otherwise movenc rebases a later program's first packet to zero.
        av_dict_set(&options, "movflags",
            "empty_moov+frag_custom+default_base_moof+omit_tfhd_offset+use_metadata_tags+frag_discont", 0);
        av_dict_set(&options, "use_editlist", "0", 0);
    }
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
        queued_marker_count_ = 0;
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
              << output_utf8 << (independent_sources_ ? " (video + independent float audio)" :
                                audio_stream_ ? " (video + AAC)" : " (video only)")
              << std::endl;
    return true;
}

bool ContinuousRecordingWriter::PushVideo(
    const uint8_t* data,
    uint32_t size,
    int64_t pts,
    bool is_keyframe,
    uint64_t generation, int64_t timeline_100ns) {
    if (!data || size == 0) return true;
    if (!GenerationMatches(generation)) return false;
    QueuedPacket packet;
    packet.kind = PacketKind::Video;
    packet.data.assign(data, data + size);
    packet.pts = pts;
    packet.duration = 1;
    packet.is_keyframe = is_keyframe;
    packet.timeline_100ns = timeline_100ns;
    packet.generation = generation;
    return Enqueue(std::move(packet));
}

bool ContinuousRecordingWriter::PushHighlight(
    const std::string& marker_id, int64_t pts, uint64_t generation) {
    if (!IsCanonicalHighlightUuid(marker_id) || pts < 0
        || !GenerationMatches(generation)) return false;
    try {
        QueuedPacket packet;
        packet.kind = PacketKind::Highlight;
        packet.pts = pts;
        packet.generation = generation;
        // The marker copy happens before Enqueue owns the packet. Treat an
        // allocation refusal as an optional annotation drop; media remains
        // accepting and healthy.
        packet.marker_id = marker_id;
        return Enqueue(std::move(packet));
    } catch (const std::bad_alloc&) {
        return false;
    }
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

bool ContinuousRecordingWriter::PushSourceAudio(const AudioSourceMetadata& source,
    const uint8_t* data, uint32_t size, int64_t timeline_100ns, int64_t duration_samples) {
    if (!independent_sources_ || !source.identity.id.IsValid() || !data || size == 0
            || timeline_100ns <= 0 || duration_samples <= 0
            || static_cast<uint64_t>(duration_samples) * 2 * sizeof(float) != size) return false;
    QueuedPacket packet;
    packet.kind = PacketKind::Audio; packet.data.assign(data, data + size);
    packet.duration = duration_samples; packet.source = source;
    packet.timeline_100ns = timeline_100ns;
    return Enqueue(std::move(packet));
}

bool ContinuousRecordingWriter::Enqueue(QueuedPacket packet) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!accepting_ || failed_.load(std::memory_order_relaxed)) return false;
    if (packet.kind == PacketKind::Highlight) {
        if (!GenerationMatches(packet.generation)) return false;
        if (std::find(admitted_marker_ids_.begin(), admitted_marker_ids_.end(),
                packet.marker_id) != admitted_marker_ids_.end()) {
            // Duplicate requests are idempotent optional annotations.
            return true;
        }
        if (admitted_marker_ids_.size() >= kMaxHighlightMarkers) {
            std::cerr << "[ContinuousRecording] Highlight marker limit reached; "
                         "recording continues." << std::endl;
            return false;
        }
    }
    const size_t accounting_bytes = packet.kind == PacketKind::Highlight
        ? packet.marker_id.size() + sizeof(packet.pts) + sizeof(packet.generation)
        : packet.data.size();
    if (accounting_bytes > kMaxQueuedBytes
        || queued_bytes_ > kMaxQueuedBytes - accounting_bytes) {
        if (packet.kind == PacketKind::Highlight) {
            std::cerr << "[ContinuousRecording] Highlight marker dropped because "
                         "the bounded queue is full." << std::endl;
            return false;
        }
        FailLocked(
            "The recording disk could not keep up with capture. The completed "
            "fragments already on disk remain playable.");
        accepting_ = false;
        condition_.notify_all();
        return false;
    }
    if (packet.kind == PacketKind::Highlight) {
        try {
            admitted_marker_ids_.push_back(packet.marker_id);
        } catch (const std::bad_alloc&) {
            std::cerr << "[ContinuousRecording] Highlight marker dropped because "
                         "the bounded admission set is full." << std::endl;
            return false;
        }
    }
    queued_bytes_ += accounting_bytes;
    if (packet.kind == PacketKind::Highlight) ++queued_marker_count_;
    try {
        queue_.push_back(std::move(packet));
    } catch (const std::bad_alloc&) {
        queued_bytes_ -= accounting_bytes;
        if (packet.kind == PacketKind::Highlight) {
            if (queued_marker_count_ > 0) --queued_marker_count_;
            if (!admitted_marker_ids_.empty()) admitted_marker_ids_.pop_back();
            std::cerr << "[ContinuousRecording] Highlight marker dropped because "
                         "the recording queue could not allocate memory."
                      << std::endl;
            return false;
        }
        FailLocked("Out of memory while queueing the recording.");
        accepting_ = false;
        condition_.notify_all();
        return false;
    }
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
    if (independent_sources_ && !headers_finalized_ && video_packets_written_ > 0 && !failed_) {
        if (!PublishAudioIdentities(true))
            Fail("Could not finalize independent audio identities; recording fragments remain on disk.");
        headers_finalized_ = true;
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
            const size_t accounting_bytes = packet.kind == PacketKind::Highlight
                ? packet.marker_id.size() + sizeof(packet.pts) + sizeof(packet.generation)
                : packet.data.size();
            queued_bytes_ -= accounting_bytes;
            if (packet.kind == PacketKind::Highlight && queued_marker_count_ > 0)
                --queued_marker_count_;
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
    } else if (video_packets_written_ > 0 && !failed_.load(std::memory_order_acquire)
        && !AppendEligibleMarkers(last_video_pts_)) {
        Fail("Could not append recording highlight metadata.");
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
    if (!format_context_) return false;
    if (packet.kind != PacketKind::Highlight && packet.data.empty()) return false;

    if (packet.kind == PacketKind::Highlight) {
        if (!GenerationMatches(packet.generation)) return true;
        try {
            pending_markers_.push_back(packet);
        } catch (const std::bad_alloc&) {
            // Marker admission is optional. Do not turn a bounded annotation
            // allocation failure into a media recording failure.
            std::cerr << "[ContinuousRecording] Highlight marker dropped because "
                         "the pending marker list could not allocate memory."
                      << std::endl;
        }
        return true;
    }
    if (packet.kind == PacketKind::Video
        && !GenerationMatches(packet.generation)) {
        std::cerr << "[ContinuousRecording] Dropped stale video packet." << std::endl;
        return true;
    }

    AVStream* selected_audio = audio_stream_;
    size_t selected_slot = 0;
    if (packet.kind == PacketKind::Video) {
        if (!video_started_) {
            // A fragmented recording must begin on a random-access point. The
            // replay encoder emits frequent GOPs, so at most one GOP is skipped.
            if (!packet.is_keyframe) return true;
            first_video_pts_ = packet.pts;
            first_video_timeline_100ns_ = packet.timeline_100ns;
            video_started_ = true;
        } else if (packet.is_keyframe && video_packets_written_ > 0) {
            // Close and flush the previous GOP before beginning the next one.
            // A crash after this boundary cannot invalidate older fragments.
            if (!FlushFragment()) {
                Fail("Could not commit a completed recording fragment.");
                return false;
            }
            if (!AppendEligibleMarkers(last_video_pts_)) {
                Fail("Could not append recording highlight metadata.");
                return false;
            }
        }
    } else {
        if (!video_started_ || !audio_stream_) return true;
        if (independent_sources_) {
            if (first_video_timeline_100ns_ <= 0 || packet.timeline_100ns <= 0) {
                Fail("Independent recording audio requires a common video/audio clock.");
                return false;
            }
            auto found = source_slots_.find(packet.source.identity.id.value);
            if (found == source_slots_.end()) {
                const auto type = packet.source.identity.type;
                size_t slot = type == AudioSourceType::Microphone ? 1
                    : type == AudioSourceType::System && packet.source.identity.persistent_identity == "default-mix"
                        ? 0 : type == AudioSourceType::System ? 2 : 6;
                const size_t end = slot < 2 ? slot + 1 : slot < 6 ? 6 : source_infos_.size();
                while (slot < end && source_infos_[slot]) ++slot;
                if (slot == end) {
                    Fail("The recording source budget is full. Completed fragments remain recoverable.");
                    return false;
                }
                source_infos_[slot] = packet.source;
                found = source_slots_.emplace(packet.source.identity.id.value, slot).first;
            }
            selected_audio = audio_streams_[found->second];
            selected_slot = found->second;
        }
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
    int64_t audio_end_sample = -1;

    if (packet.kind == PacketKind::Video) {
        // Preserve the capture clock. Delayed or missing capture frames must
        // create a longer interval, not make the surviving frames play faster.
        int64_t rebased_pts = 0;
        if (!CheckedSignedSubtract(packet.pts, first_video_pts_, rebased_pts)) {
            av_packet_free(&output);
            Fail("A video packet timestamp overflowed the recording range.");
            return false;
        }
        output->pts = rebased_pts;
        output->dts = rebased_pts;
        output->duration = packet.duration;
        output->stream_index = video_stream_->index;
        output->flags = packet.is_keyframe ? AV_PKT_FLAG_KEY : 0;
        const AVRational input_time_base = {
            video_config_.time_base.numerator,
            video_config_.time_base.denominator};
        av_packet_rescale_ts(output, input_time_base, video_stream_->time_base);
    } else {
        int64_t rebased_pts = 0;
        if (independent_sources_) {
            int64_t delta = 0;
            if (!CheckedSignedSubtract(packet.timeline_100ns, first_video_timeline_100ns_, delta)) {
                av_packet_free(&output); Fail("Recording audio clock overflow."); return false;
            }
            rebased_pts = av_rescale_q(delta, AVRational{1, 10000000}, AVRational{1, 48000});
            if (rebased_pts < 0) {
                const int64_t skip = std::min(-rebased_pts, packet.duration);
                const size_t bytes = static_cast<size_t>(skip) * 2 * sizeof(float);
                if (bytes >= packet.data.size()) { av_packet_free(&output); return true; }
                std::memmove(output->data, output->data + bytes, packet.data.size() - bytes);
                av_shrink_packet(output, static_cast<int>(packet.data.size() - bytes));
                output->duration = packet.duration - skip;
                rebased_pts = 0;
            } else output->duration = packet.duration;
        } else if (!CheckedSignedSubtract(packet.pts, first_audio_pts_, rebased_pts)) {
            av_packet_free(&output);
            Fail("An audio packet timestamp overflowed the recording range.");
            return false;
        }
        if (independent_sources_ && !source_written_[selected_slot])
            source_first_sample_[selected_slot] = rebased_pts;
        audio_end_sample = rebased_pts + output->duration;
        output->pts = rebased_pts;
        output->dts = rebased_pts;
        if (!independent_sources_) output->duration = packet.duration;
        output->stream_index = selected_audio->index;
        // The AAC encoder emits sync flags; the callback transport carries
        // only data and timing, so restore the MP4 packet flag here.
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
    if (packet.kind == PacketKind::Audio && independent_sources_) {
        source_written_[selected_slot] = true;
        source_last_sample_[selected_slot] = audio_end_sample;
    }
    if (packet.kind == PacketKind::Video) last_video_pts_ = packet.pts;
    return true;
}

bool ContinuousRecordingWriter::PublishAudioIdentities(bool remove_unused) {
    std::map<uint32_t, std::string> handlers;
    const auto& emitted = remove_unused ? source_written_ : source_committed_;
    bool applications = false;
    for (size_t slot = 0; slot < source_infos_.size(); ++slot)
        applications |= emitted[slot] && source_infos_[slot]
            && source_infos_[slot]->identity.type == AudioSourceType::Application;
    for (size_t slot = 0; slot < source_infos_.size(); ++slot) {
        if (!emitted[slot] || !source_infos_[slot]) continue;
        auto source = *source_infos_[slot];
        if (source.identity.persistent_identity == "default-mix" && applications)
            source.identity.display_name = "System Audio (backup)";
        handlers[static_cast<uint32_t>(slot + 2)] = AudioSourceHandler(
            source, source_first_sample_[slot], source_last_sample_[slot]);
    }
    return FinalizeMp4AudioHeaders(output_path_, handlers, remove_unused);
}

bool ContinuousRecordingWriter::FlushFragment() {
    if (!format_context_ || !format_context_->pb) return false;
    // First drain libavformat's interleave queue, then frag_custom turns the
    // explicit null write into an MP4 fragment boundary.
    if (av_interleaved_write_frame(format_context_, nullptr) < 0) return false;
    if (av_write_frame(format_context_, nullptr) < 0) return false;
    avio_flush(format_context_->pb);
    if (format_context_->pb->error >= 0 && independent_sources_) {
        source_committed_ = source_written_;
        if (!PublishAudioIdentities(false)) return false;
    }
    return format_context_->pb->error >= 0;
}

bool ContinuousRecordingWriter::AppendEligibleMarkers(int64_t last_video_pts) {
    if (!format_context_ || !format_context_->pb || !video_started_
        || last_video_pts < first_video_pts_) return true;
    try {
        std::vector<HighlightMarker> eligible;
        std::deque<QueuedPacket> remaining;
        for (const auto& pending : pending_markers_) {
            if (pending.pts < first_video_pts_ || pending.pts > last_video_pts) {
                if (pending.pts > last_video_pts) remaining.push_back(pending);
                continue;
            }
            if (!IsCanonicalHighlightUuid(pending.marker_id)) continue;
            int64_t position_100ns = 0;
            int64_t pts_delta = 0;
            if (!CheckedNonNegativeDelta(pending.pts, first_video_pts_, pts_delta))
                continue;
            if (!CheckedPtsTo100ns(
                    pts_delta,
                    video_config_.time_base.numerator,
                    video_config_.time_base.denominator,
                    position_100ns)) continue;
            eligible.push_back({pending.marker_id, position_100ns, {}});
        }
        pending_markers_.swap(remaining);
        if (eligible.empty()) return true;

        // Prepare the complete bounded box before touching AVIO. A rejected
        // optional batch is dropped without changing media health; only the
        // write/flush result can fail the recording.
        std::vector<uint8_t> box;
        if (!BuildHighlightUuidBox(eligible, box)
            || box.size() > static_cast<size_t>(std::numeric_limits<int>::max()))
            return true;
        avio_write(format_context_->pb, box.data(), static_cast<int>(box.size()));
        avio_flush(format_context_->pb);
        return format_context_->pb->error >= 0;
    } catch (const std::bad_alloc&) {
        // No allocation or stream logging is safe on this path. The pending
        // batch is optional, so discard it and keep ordinary media alive.
        pending_markers_.clear();
        return true;
    }
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
