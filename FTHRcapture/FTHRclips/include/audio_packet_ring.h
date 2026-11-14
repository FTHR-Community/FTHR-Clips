// Bounded timestamped audio replay: disk-backed float PCM or legacy AAC.

#pragma once
#ifndef FTHR_AUDIO_PACKET_RING_H
#define FTHR_AUDIO_PACKET_RING_H

#include "audio_source_model.h"
#include "audio_spool.h"
#include "replay_storage.h"

#include <cstdint>
#include <deque>
#include <mutex>
#include <vector>

namespace fthr {

struct EncodedAudioPacket {
    std::vector<uint8_t> data;
    int64_t pts_samples = 0;
    int64_t duration_samples = 1024;
    std::shared_ptr<AudioSpoolPayload> storage;
    std::shared_ptr<ReplayStoragePayload> replay_storage;
    size_t byte_size() const {
        if (replay_storage) return replay_storage->size();
        return storage ? storage->size() : data.size();
    }
    std::shared_ptr<const std::vector<uint8_t>> ReadData() const {
        if (replay_storage) return replay_storage->Read();
        return storage ? storage->Read() : std::make_shared<const std::vector<uint8_t>>(data);
    }
};

struct EncodedAudioSnapshot {
    // Several selection layers briefly copy a snapshot's packet descriptors.
    std::shared_ptr<ReplayMemoryReservation> memory;
    AudioSourceId source_id;
    uint64_t generation = 0;
    AudioSourceFormat format;
    std::vector<uint8_t> codec_extradata;
    std::vector<EncodedAudioPacket> packets;
    int64_t first_pts_samples = 0;
    int64_t last_pts_samples = 0;

    bool valid() const {
        return !packets.empty() && format.sample_rate > 0 && format.channels > 0;
    }
};

struct EncodedAudioRingStats {
    uint64_t accepted_packets = 0;
    uint64_t rejected_empty_packets = 0;
    uint64_t rejected_regressing_packets = 0;
    uint64_t trimmed_packets = 0;
};

// The ring's retention bound is in timeline samples, not packet count. This
// makes 30/60/300 second memory predictable even when encoders vary packet size.
class EncodedAudioPacketRing {
public:
    EncodedAudioPacketRing(AudioSourceId source_id, uint64_t generation,
                           AudioSourceFormat format, uint32_t retention_seconds,
                           std::shared_ptr<ReplayStorage> storage = nullptr);

    void SetCodecExtradata(std::vector<uint8_t> codec_extradata);
    bool Push(EncodedAudioPacket packet);
    EncodedAudioSnapshot TakeSnapshot(int64_t start_pts_samples,
                                      int64_t end_pts_samples) const;
    size_t packet_count() const;
    size_t byte_count() const;
    EncodedAudioRingStats stats() const;
    uint32_t retention_seconds() const { return retention_seconds_; }

private:
    void TrimLocked(int64_t newest);

    AudioSourceId source_id_;
    uint64_t generation_;
    AudioSourceFormat format_;
    uint32_t retention_seconds_;
    int64_t retention_samples_;
    std::vector<uint8_t> codec_extradata_;
    std::deque<EncodedAudioPacket> packets_;
    size_t byte_count_ = 0;
    EncodedAudioRingStats stats_;
    mutable std::mutex mutex_;
    std::unique_ptr<AudioSpool> spool_;
    std::shared_ptr<ReplayStorage> replay_storage_;
};

}  // namespace fthr

#endif  // FTHR_AUDIO_PACKET_RING_H
