// Windows 11 process-loopback capture for application audio stems.
// This adapter owns COM activation and feeds PCM to persistent AAC replay rings.

#pragma once
#ifndef FTHR_WINDOWS_PROCESS_LOOPBACK_AUDIO_PROVIDER_H
#define FTHR_WINDOWS_PROCESS_LOOPBACK_AUDIO_PROVIDER_H

#include "audio_packet_ring.h"
#include "audio_timeline.h"
#include "audio_source_model.h"
#include "save_clip_task.h"
#include "windows_audio_session_registry.h"
#include "replay_pause_clock.h"

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace fthr {

struct WindowsProcessLoopbackProviderConfig {
    uint64_t generation = 0;
    uint32_t target_process_id = 0;
    // The documented INCLUDE mode captures the target and its child process
    // tree.  The provider never uses the exclude mode because a FTHR stem is
    // defined by one deterministic session group.
    bool include_process_tree = true;
    uint32_t retention_seconds = 30;
    std::shared_ptr<ReplayStorage> replay_storage;
    uint32_t bitrate_kbps = 320;
    AudioSourceMetadata source;
    std::shared_ptr<ReplayPauseClock> replay_pause_clock;
    AudioPacketSink packet_sink;
};

struct WindowsProcessLoopbackRuntimeInfo {
    std::string state = "STOPPED";
    bool admitted = false;
    uint64_t input_frames = 0;
    uint64_t packet_count = 0;
    uint64_t converted_frames = 0;
    uint64_t submitted_frames = 0;
    uint64_t encoded_packets = 0;
    uint64_t encode_failures = 0;
    uint64_t finalize_failures = 0;
    uint64_t dropped_blocks = 0;
    uint64_t no_packet_intervals = 0;
    uint64_t largest_no_packet_gap_100ns = 0;
    uint64_t discontinuity_count = 0;
    uint64_t rejected_packets = 0;
    uint64_t restart_count = 0;
    int64_t first_input_qpc_100ns = 0;
    int64_t last_input_qpc_100ns = 0;
    int64_t last_submission_qpc_100ns = 0;
    int64_t last_encoded_qpc_100ns = 0;
    int64_t largest_gap_100ns = 0;
};

// Owns one real Windows process-loopback IAudioClient.  It does not create an
// AAC encoder or replay ring until the common activity gate admits the source,
// so a merely discovered/silent session cannot become a stored clip stem.
class WindowsProcessLoopbackAudioProvider {
public:
    using ActivityCallback = std::function<AudioSourceAdmission(
        const AudioSourceId&, float rms, int64_t timestamp_100ns)>;
    using EndedCallback = std::function<void(const AudioSourceId&, int64_t timestamp_100ns)>;
    using FailureCallback = std::function<void(const AudioSourceId&)>;

    WindowsProcessLoopbackAudioProvider(WindowsProcessLoopbackProviderConfig config,
                                        ActivityCallback activity_callback,
                                        EndedCallback ended_callback,
                                        FailureCallback failure_callback);
    ~WindowsProcessLoopbackAudioProvider();

    WindowsProcessLoopbackAudioProvider(const WindowsProcessLoopbackAudioProvider&) = delete;
    WindowsProcessLoopbackAudioProvider& operator=(const WindowsProcessLoopbackAudioProvider&) = delete;

    // Starts a dedicated bounded event-driven capture thread.  A successful
    // return means the thread was created; runtime activation remains reported
    // through the source failure callback rather than being fabricated as audio.
    bool Start();
    void Stop();
    bool IsRunning() const;
    bool HasCapturedAudio() const;
    std::string last_error() const;
    WindowsProcessLoopbackRuntimeInfo runtime_info() const;

    // Returns a contract track only when this provider actually encoded packets
    // overlapping the requested video presentation interval.
    std::optional<EncodedAudioTrack> TakeTrackForInterval(
        double presentation_start_qpc_s, double presentation_end_qpc_s,
        const AudioSourceMetadata& source) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

struct WindowsApplicationSourceBinding {
    std::string runtime_group_key;
    uint32_t process_id = 0;
    AudioSourceMetadata source;
};

// Pure source-lifecycle coordinator.  It is intentionally testable without a
// COM apartment, audio endpoint, or Windows 11 host.  The production manager
// below supplies the real provider, while tests can drive discovery/activity/
// exit/restart through this same lifecycle boundary.
class WindowsApplicationSourceCoordinator {
public:
    using SourceIdGenerator = std::function<std::optional<AudioSourceId>()>;

    WindowsApplicationSourceCoordinator(uint64_t generation,
                                        WindowsProcessLoopbackCapability capability,
                                        SourceIdGenerator source_id_generator,
                                        uint32_t source_limit = kMaxRetainedAudioSources,
                                        uint32_t retention_seconds = 30);

    bool available() const { return capability_.api_build_supported; }
    const WindowsProcessLoopbackCapability& capability() const { return capability_; }

    // A runtime group maps to exactly one provider/source while it is alive.
    // A disappeared group is retired rather than merged on a later restart: the
    // current grouping policy is root-PID scoped, so this avoids PID-reuse or
    // name-only timeline corruption.
    std::optional<WindowsApplicationSourceBinding> Discover(
        const WindowsAudioSessionDescriptor& descriptor);
    std::optional<AudioSourceId> RetireRuntimeGroup(
        const std::string& runtime_group_key, int64_t timestamp_100ns);
    bool HasRuntimeGroup(const std::string& runtime_group_key) const {
        return runtime_groups_.find(runtime_group_key) != runtime_groups_.end();
    }
    // Provider callbacks carry the opaque source UUID, while the registry is
    // keyed by runtime group. Keep that translation in one tested boundary.
    std::optional<std::string> RetireSource(
        const AudioSourceId& id, int64_t timestamp_100ns);
    bool ShouldRetireRuntimeGroup(const std::string& runtime_group_key,
                                  int64_t now_100ns) const;
    void PruneExpired(int64_t now_100ns);
    AudioSourceAdmission ObserveActivity(const AudioSourceId& id, float rms,
                                         int64_t timestamp_100ns);
    void MarkFailed(const AudioSourceId& id);

    std::vector<AudioSourceMetadata> SourcesForInterval(
        int64_t start_100ns, int64_t end_100ns) const;
    const AudioSourceMetadata* Find(const AudioSourceId& id) const;
    size_t provider_candidate_count() const { return runtime_groups_.size(); }
    size_t source_count() const { return registry_.source_count(); }
    uint32_t admitted_count() const {
        return static_cast<uint32_t>(registry_.admitted_count());
    }

private:
    uint64_t generation_;
    WindowsProcessLoopbackCapability capability_;
    SourceIdGenerator source_id_generator_;
    AudioSourceRegistry registry_;
    int64_t retention_100ns_;
    std::map<std::string, AudioSourceId> runtime_groups_;
};

// Windows-only owner that keeps one session registry and one provider lifecycle
// coherent for a capture generation.  It is deliberately absent on Windows 10:
// no activation attempt, synthetic source, or per-app metadata is produced.
class WindowsApplicationAudioSourceManager {
public:
    WindowsApplicationAudioSourceManager(uint64_t generation, uint32_t retention_seconds,
        std::shared_ptr<ReplayPauseClock> replay_pause_clock = {}, AudioPacketSink packet_sink = {},
        std::shared_ptr<ReplayStorage> replay_storage = {},
        std::vector<std::string> excluded_identities = {});
    ~WindowsApplicationAudioSourceManager();

    WindowsApplicationAudioSourceManager(const WindowsApplicationAudioSourceManager&) = delete;
    WindowsApplicationAudioSourceManager& operator=(const WindowsApplicationAudioSourceManager&) = delete;

    bool Start();
    void Stop();
    bool available() const;
    WindowsProcessLoopbackCapability capability() const;
    std::string last_error() const;

    struct RuntimeInfo {
        bool available = false;
        bool running = false;
        uint32_t candidate_count = 0;
        uint32_t retained_provider_count = 0;
        uint32_t admitted_source_count = 0;
        uint32_t source_limit = kMaxRetainedAudioSources;
        uint64_t input_frames = 0;
        uint64_t packet_count = 0;
        uint64_t converted_frames = 0;
        uint64_t submitted_frames = 0;
        uint64_t encoded_packets = 0;
        uint64_t encode_failures = 0;
        uint64_t finalize_failures = 0;
        uint64_t dropped_blocks = 0;
        uint64_t no_packet_intervals = 0;
        uint64_t largest_no_packet_gap_100ns = 0;
        uint64_t discontinuity_count = 0;
        uint64_t rejected_packets = 0;
        uint64_t restart_count = 0;
        int64_t largest_gap_100ns = 0;
        uint64_t source_limit_rejections = 0;
        uint32_t failed_sources = 0;
    };
    RuntimeInfo runtime_info() const;

    std::vector<EncodedAudioTrack> TakeTracksForInterval(
        double presentation_start_qpc_s, double presentation_end_qpc_s) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace fthr

#endif  // FTHR_WINDOWS_PROCESS_LOOPBACK_AUDIO_PROVIDER_H
