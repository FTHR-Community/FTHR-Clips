#pragma once
#include <string>
#include <vector>
#include <deque>
#include <mutex>
#include <map>
#include <atomic>
#include <cstdint>
#include <memory>
#include <pulse/pulseaudio.h>

namespace fthr {

struct AudioCategoryConfig {
    std::string              name;       // display name, e.g. "Discord"
    std::string              sink_name;  // PA name, e.g. "fthr_discord"
    std::vector<std::string> patterns;   // process binary substrings for heuristic matching
};

class AudioMultiCapture {
public:
    static constexpr int kSampleRate = 48000;
    static constexpr int kChannels   = 2;
    static constexpr int kMaxSeconds = 120;

    AudioMultiCapture() = default;
    ~AudioMultiCapture() { Stop(); }

    bool Start(const std::vector<AudioCategoryConfig>& categories);
    void Stop();
    bool IsRunning() const { return running_.load(); }

    // Returns float32 stereo PCM for the given category over the last duration_ms
    // ending at end_time_ns (CLOCK_MONOTONIC). 0 = most-recent.
    std::vector<float> ExtractSegment(const std::string& category_name,
                                      int64_t end_time_ns,
                                      uint32_t duration_ms) const;

    // Current app→category mappings (for display in UI), e.g. {"firefox":"Browser"}
    std::map<std::string, std::string> GetCurrentMappings() const;

private:
    struct TimedChunk {
        std::vector<float> samples;
        int64_t            start_ns;
    };

    struct CategoryState {
        AudioCategoryConfig     config;
        uint32_t                module_idx = UINT32_MAX;
        uint32_t                sink_idx   = UINT32_MAX;
        pa_stream*              stream     = nullptr;
        mutable std::mutex      mutex;
        std::deque<TimedChunk>  chunks;
    };

    static void context_state_cb(pa_context*, void* userdata);
    static void subscribe_cb(pa_context*, pa_subscription_event_type_t,
                              uint32_t idx, void* userdata);
    static void sink_input_info_cb(pa_context*, const pa_sink_input_info*,
                                   int eol, void* userdata);
    static void stream_read_cb(pa_stream*, size_t, void* userdata);

    pa_threaded_mainloop*                         mainloop_  = nullptr;
    pa_context*                                   context_   = nullptr;
    std::vector<std::unique_ptr<CategoryState>>   categories_;

    mutable std::mutex                 mappings_mutex_;
    std::map<std::string, std::string> current_mappings_;

    std::atomic<bool> running_{false};
};

} // namespace fthr
