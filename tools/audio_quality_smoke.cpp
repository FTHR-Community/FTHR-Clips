#include "audio_encoder.h"
#include "audio_packet_ring.h"
#include "audio_gain_ramp.h"
#include "audio_process_tree.h"
#include <cstdlib>
#include <chrono>
#include <cstring>
#include <iostream>
#include <thread>

#define CHECK(expression) do { if (!(expression)) { \
    std::cerr << "FAILED line " << __LINE__ << ": " << #expression << '\n'; \
    std::exit(1); } } while (false)

int main() {
    using namespace fthr;
    std::vector<float> input(2050 * 2);
    for (size_t index = 0; index < input.size(); ++index)
        input[index] = (static_cast<int>(index % 101) - 50) / 13.0f;
    std::vector<uint8_t> bytes;
    AudioEncoder encoder;
    CHECK(encoder.Initialize(48000, 2, 0,
        [&](const uint8_t* data, uint32_t size, int64_t) {
            bytes.insert(bytes.end(), data, data + size);
        }, 0, true));
    CHECK(encoder.EncodeSamples(input.data(), static_cast<uint32_t>(input.size())));
    CHECK(bytes.size() == input.size() * sizeof(float)); // No hidden live-recording tail.
    CHECK(encoder.Finalize());
    CHECK(bytes.size() == input.size() * sizeof(float));
    CHECK(std::memcmp(bytes.data(), input.data(), bytes.size()) == 0);

    EncodedAudioSnapshot snapshot;
    {
        EncodedAudioPacketRing ring(AudioSourceId{"11111111-1111-4111-8111-111111111111"},
            1, AudioSourceFormat{48000, 2, "flt", "pcm_f32le"}, 1);
        for (int packet = 0; packet < 100; ++packet)
            CHECK(ring.Push({bytes, packet * 2050, 2050}));
        snapshot = ring.TakeSnapshot(52 * 2050, 100 * 2050);
        CHECK(snapshot.valid());
        CHECK(snapshot.packets.size() <= 25);
        for (const auto& packet : snapshot.packets) {
            auto data = packet.ReadData();
            CHECK(data && *data == bytes);
        }
    }
    // Saved snapshots keep chunks alive after capture is destroyed.
    for (const auto& packet : snapshot.packets) CHECK(*packet.ReadData() == bytes);
    snapshot = {};
    CHECK(audio_spool_bytes.load() == 0);

    {
        EncodedAudioPacketRing silent(AudioSourceId{"11111111-1111-4111-8111-111111111111"},
            1, AudioSourceFormat{48000, 2, "flt", "pcm_f32le"}, 4);
        for (int index = 0; index < 100; ++index)
            CHECK(silent.Push({std::vector<uint8_t>(1024 * 8, 0), index * 1024, 1024}));
        const auto zeros = silent.TakeSnapshot(0, 102400);
        CHECK(zeros.valid() && zeros.packets.size() <= 3);
        for (const auto& packet : zeros.packets) {
            const auto data = packet.ReadData();
            CHECK(data && std::all_of(data->begin(), data->end(), [](uint8_t value) { return value == 0; }));
        }
        CHECK(audio_spool_bytes.load() == 0);
    }
    {
        AudioSpool limited;
        CHECK(!limited.Push(std::vector<uint8_t>(5 * 1024 * 1024, 1)));
        CHECK(!limited.healthy()); // Resource exhaustion is explicit, never silent truncation.
        CHECK(!limited.Push(std::vector<uint8_t>(1024, 1)));
    }
    {
        AudioSpoolChunk chunk;
        audio_spool_bytes.store(kAudioSpoolBudget);
        uint64_t offset = 0;
        CHECK(!chunk.Append(bytes, offset));
        CHECK(chunk.size() == 0);
        audio_spool_bytes.store(0);
        CHECK(chunk.Append(bytes, offset));
        CHECK(chunk.size() == bytes.size());
    }
    const std::map<uint32_t, uint32_t> parents{{40, 30}, {30, 20}, {20, 10}, {10, 0}, {50, 0}};
    const auto owners = AudioProcessTreeOwners({10, 20, 40, 50}, [&](uint32_t pid) {
        const auto found = parents.find(pid); return found == parents.end() ? 0u : found->second;
    });
    CHECK(owners.at(40) == 10 && owners.at(20) == 10 && owners.at(50) == 50);
    const auto cycle = AudioProcessTreeOwners({1, 2}, [](uint32_t pid) { return pid == 1 ? 2u : 1u; });
    CHECK(cycle.at(1) == 1 && cycle.at(2) == 1);

    AudioGainRamp ramp;
    ramp.SetTarget(1);
    CHECK(ramp.Next() == 1);
    ramp.SetTarget(0, 240);
    float previous = 1;
    for (int index = 0; index < 240; ++index) {
        const float value = ramp.Next();
        CHECK(value <= previous && previous - value < .005f);
        previous = value;
    }
    CHECK(ramp.Next() == 0);
    std::cout << "Lossless packet roundtrip, partial tail, disk spill, retention, "
                 "snapshot ownership, sparse silence, resource failure, non-overlapping "
                 "process groups, cleanup and gain ramp passed.\n";
}
