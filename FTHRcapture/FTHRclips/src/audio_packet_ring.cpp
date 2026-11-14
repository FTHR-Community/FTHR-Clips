#include "audio_packet_ring.h"

#include <algorithm>
#include <limits>

namespace fthr {

EncodedAudioPacketRing::EncodedAudioPacketRing(
    AudioSourceId source_id, uint64_t generation, AudioSourceFormat format,
    uint32_t retention_seconds, std::shared_ptr<ReplayStorage> storage)
    : source_id_(std::move(source_id)), generation_(generation), format_(std::move(format)),
      retention_seconds_(std::max<uint32_t>(1, retention_seconds)),
      retention_samples_(static_cast<int64_t>(format_.sample_rate) * retention_seconds_),
      replay_storage_(std::move(storage)) {
    if (!replay_storage_ && format_.codec == "pcm_f32le") spool_ = std::make_unique<AudioSpool>();
}

void EncodedAudioPacketRing::SetCodecExtradata(std::vector<uint8_t> codec_extradata) {
    std::lock_guard<std::mutex> lock(mutex_);
    codec_extradata_ = std::move(codec_extradata);
}

bool EncodedAudioPacketRing::Push(EncodedAudioPacket packet) {
    if (packet.data.empty() || packet.duration_samples <= 0) {
        std::lock_guard<std::mutex> lock(mutex_);
        ++stats_.rejected_empty_packets;
        return false;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (!packets_.empty() && packet.pts_samples < packets_.back().pts_samples) {
        ++stats_.rejected_regressing_packets;
        return false;
    }
    // Retire expired payloads before admitting this packet, so an otherwise
    // full ring can reuse the released capacity immediately.
    TrimLocked(packet.pts_samples + packet.duration_samples);
    if (replay_storage_) {
        const bool silence = format_.codec == "pcm_f32le"
            && std::all_of(packet.data.begin(), packet.data.end(), [](uint8_t byte) { return byte == 0; });
        if (silence && !packets_.empty()) {
            auto& previous = packets_.back();
            if (previous.replay_storage && previous.replay_storage->is_silence()
                && previous.pts_samples + previous.duration_samples == packet.pts_samples
                && previous.duration_samples + packet.duration_samples <= format_.sample_rate) {
                auto merged = replay_storage_->Push(nullptr, previous.byte_size() + packet.data.size(), true);
                if (!merged) return false;
                byte_count_ += packet.data.size();
                previous.replay_storage = std::move(merged);
                previous.duration_samples += packet.duration_samples;
                ++stats_.accepted_packets;
                return true;
            }
        }
        packet.replay_storage = replay_storage_->Push(packet.data.data(), packet.data.size(), silence);
        if (!packet.replay_storage) return false;
        std::vector<uint8_t>().swap(packet.data);
    } else if (spool_) {
        packet.storage = spool_->Push(std::move(packet.data));
        if (!packet.storage) return false;
    }
    byte_count_ += packet.byte_size();
    if (packet.storage && packet.storage->is_silence() && !packets_.empty()) {
        auto& previous = packets_.back();
        if (previous.storage && previous.storage->is_silence()
                && previous.pts_samples + previous.duration_samples == packet.pts_samples
                && previous.duration_samples + packet.duration_samples <= format_.sample_rate) {
            previous.storage = std::make_shared<AudioSpoolPayload>(previous.byte_size() + packet.byte_size());
            previous.duration_samples += packet.duration_samples;
            ++stats_.accepted_packets;
            return true;
        }
    }
    packets_.push_back(std::move(packet));
    ++stats_.accepted_packets;
    return true;
}

EncodedAudioSnapshot EncodedAudioPacketRing::TakeSnapshot(
    int64_t start_pts_samples, int64_t end_pts_samples) const {
    EncodedAudioSnapshot result;
    result.source_id = source_id_;
    result.generation = generation_;
    result.format = format_;
    if (end_pts_samples <= start_pts_samples) return result;

    std::lock_guard<std::mutex> lock(mutex_);
    if (spool_ && !spool_->healthy()) return result;
    const auto first = std::find_if(packets_.begin(), packets_.end(), [start_pts_samples](const auto& packet) {
        return packet.pts_samples + packet.duration_samples > start_pts_samples;
    });
    const auto last = std::find_if(first, packets_.end(), [end_pts_samples](const auto& packet) {
        return packet.pts_samples >= end_pts_samples;
    });
    if (replay_storage_) {
        result.memory = std::make_shared<ReplayMemoryReservation>(
            replay_storage_->memory_budget(), ReplayMemoryUse::Save);
        constexpr size_t metadata_per_packet = 4 * sizeof(EncodedAudioPacket);
        if (!result.memory->Grow(static_cast<uint64_t>(last - first) * metadata_per_packet
                + codec_extradata_.size())) throw std::bad_alloc();
    }
    result.codec_extradata = codec_extradata_;
    result.packets.assign(first, last);
    if (!result.packets.empty()) {
        result.first_pts_samples = result.packets.front().pts_samples;
        const auto& last = result.packets.back();
        result.last_pts_samples = last.pts_samples + last.duration_samples;
    }
    return result;
}

size_t EncodedAudioPacketRing::packet_count() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return packets_.size();
}

size_t EncodedAudioPacketRing::byte_count() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return byte_count_;
}

EncodedAudioRingStats EncodedAudioPacketRing::stats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

void EncodedAudioPacketRing::TrimLocked(int64_t newest) {
    const int64_t oldest_allowed = newest - retention_samples_;
    while (!packets_.empty()) {
        const auto& packet = packets_.front();
        if (packet.pts_samples + packet.duration_samples > oldest_allowed) break;
        byte_count_ -= packet.byte_size();
        packets_.pop_front();
        ++stats_.trimmed_packets;
    }
}

}  // namespace fthr
