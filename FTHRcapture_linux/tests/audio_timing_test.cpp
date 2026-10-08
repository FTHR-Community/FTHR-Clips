#include "audio_timing.h"

#include <cassert>
#include <cstdint>
#include <vector>

int main() {
    constexpr int64_t second = 1'000'000'000LL;

    // A 10 ms block delivered at t=1.050 s from a stream reporting 40 ms
    // source-to-client latency belongs at t=1.000 s, not at read completion.
    const int64_t start = fthr::AudioChunkStartNs(
        1'050'000'000LL, 40'000, 480, 48'000);
    assert(start == second);

    // Negative/unknown latency must not move samples into the future.
    assert(fthr::AudioChunkStartNs(2'000'000'000LL, -1, 480, 48'000)
           == 1'990'000'000LL);

    // AssembleAudioWindow: mono, 1 kHz so one frame is 1 ms.
    constexpr uint32_t rate = 1000;
    auto block = [](std::vector<float>& storage, float value, std::size_t frames,
                    int64_t start_ns) {
        storage.assign(frames, value);
        return fthr::TimedAudioBlock{storage.data(), frames, start_ns};
    };
    const int64_t ms = 1'000'000LL;

    {
        // Contiguous audio with small jitter is joined without inserted silence.
        std::vector<float> a, b, c;
        std::vector<fthr::TimedAudioBlock> blocks = {
            block(a, 1.0f, 100, 0),
            block(b, 2.0f, 100, 103 * ms),     // 3 ms late: jitter
            block(c, 3.0f, 100, 198 * ms),     // 2 ms early: overlap
        };
        const auto pcm = fthr::AssembleAudioWindow(blocks, 1, rate, 300 * ms, 300);
        assert(pcm.size() == 300);
        assert(pcm[99] == 1.0f && pcm[100] == 2.0f && pcm[200] == 3.0f);
    }
    {
        // A reconnect gap keeps later audio at its capture time.
        std::vector<float> a, b;
        std::vector<fthr::TimedAudioBlock> blocks = {
            block(a, 1.0f, 100, 0),
            block(b, 2.0f, 100, 400 * ms),     // 300 ms outage
        };
        const auto pcm = fthr::AssembleAudioWindow(blocks, 1, rate, 500 * ms, 500);
        assert(pcm.size() == 500);
        assert(pcm[99] == 1.0f);
        assert(pcm[100] == 0.0f && pcm[399] == 0.0f);
        assert(pcm[400] == 2.0f && pcm[499] == 2.0f);
    }
    {
        // Audio that started after the window began is preceded by silence
        // rather than sliding to the start of the clip.
        std::vector<float> a;
        std::vector<fthr::TimedAudioBlock> blocks = {block(a, 1.0f, 200, 300 * ms)};
        const auto pcm = fthr::AssembleAudioWindow(blocks, 1, rate, 500 * ms, 500);
        assert(pcm.size() == 500);
        assert(pcm[0] == 0.0f && pcm[299] == 0.0f && pcm[300] == 1.0f);
    }
    {
        // Blocks straddling the window start are trimmed; stereo stays interleaved.
        std::vector<float> a(400);
        for (std::size_t i = 0; i < a.size(); ++i) a[i] = static_cast<float>(i);
        std::vector<fthr::TimedAudioBlock> blocks = {{a.data(), 200, 0}};
        const auto pcm = fthr::AssembleAudioWindow(blocks, 2, rate, 200 * ms, 150);
        assert(pcm.size() == 300);
        assert(pcm[0] == 100.0f && pcm[1] == 101.0f);   // frame 50, both channels
    }
    {
        // Nothing overlapping the window means no audio track at all.
        std::vector<float> a;
        std::vector<fthr::TimedAudioBlock> blocks = {block(a, 1.0f, 100, 0)};
        assert(fthr::AssembleAudioWindow(blocks, 1, rate, 900 * ms, 100).empty());
        assert(fthr::AssembleAudioWindow({}, 1, rate, 900 * ms, 100).empty());
    }

    return 0;
}
