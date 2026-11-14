#include "windows_process_loopback_audio_provider.h"

#include <windows.h>
#include <mmsystem.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

#pragma comment(lib, "winmm.lib")

namespace {

int process_loopback_checks = 0;

void CheckProcessLoopback(bool condition, const char* message) {
    ++process_loopback_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

fthr::WindowsAudioSessionDescriptor Session(
    const char* group_key, uint32_t pid, const char* identity, const char* display) {
    fthr::WindowsAudioSessionDescriptor descriptor;
    descriptor.process_id = pid;
    descriptor.process_group_root_id = pid;
    descriptor.runtime_group_key = group_key;
    descriptor.identity.type = fthr::AudioSourceType::Application;
    descriptor.identity.persistent_identity = identity;
    descriptor.identity.display_name = display;
    descriptor.identity.icon_reference = "windows-app-icon";
    return descriptor;
}

fthr::WindowsApplicationSourceCoordinator::SourceIdGenerator Generator(
    std::vector<std::string> ids) {
    return [ids = std::move(ids), next = size_t{0}]() mutable
        -> std::optional<fthr::AudioSourceId> {
        if (next >= ids.size()) return std::nullopt;
        return fthr::AudioSourceId{ids[next++]};
    };
}

void CapabilityGateDoesNotAttemptWindows10Sources() {
    fthr::WindowsProcessLoopbackCapability unsupported;
    unsupported.os_build = 19045;
    unsupported.api_build_supported = false;
    fthr::WindowsApplicationSourceCoordinator coordinator(
        1, unsupported, Generator({"10000000-0000-4000-8000-000000000001"}));
    CheckProcessLoopback(!coordinator.available(),
        "Windows 10 capability leaves the per-app source coordinator unavailable");
    CheckProcessLoopback(!coordinator.Discover(Session("discord#77", 77, "discord", "Discord")),
        "unsupported capability creates no provider candidate or fabricated source");
}

void DiscoveryActivityAndSilentFilteringUseTheCommonModel() {
    fthr::WindowsProcessLoopbackCapability supported;
    supported.os_build = 22631;
    supported.api_build_supported = true;
    fthr::WindowsApplicationSourceCoordinator coordinator(
        2, supported, Generator({
            "20000000-0000-4000-8000-000000000001",
            "20000000-0000-4000-8000-000000000002",
            "20000000-0000-4000-8000-000000000003"}));
    const auto discord = coordinator.Discover(Session("discord#77", 77, "discord", "Discord"));
    const auto firefox = coordinator.Discover(Session("firefox#88", 88, "firefox", "Firefox"));
    CheckProcessLoopback(discord && firefox && coordinator.provider_candidate_count() == 2,
        "eligible discovered sessions create one deterministic provider candidate each");
    CheckProcessLoopback(discord->source.format.sample_rate == 48000
            && discord->source.format.channels == 2
            && discord->source.identity.type == fthr::AudioSourceType::Application,
        "provider sources declare the canonical processing format and application type");
    CheckProcessLoopback(coordinator.ObserveActivity(firefox->source.identity.id, 0.0f, 100)
            == fthr::AudioSourceAdmission::RejectedAtCapacity,
        "a digital-silence block does not admit a discovered source");
    CheckProcessLoopback(coordinator.ObserveActivity(discord->source.identity.id, 0.0006f, 200)
            == fthr::AudioSourceAdmission::Accepted,
        "the first nonzero block admits a quiet application source");
    const auto audible = coordinator.SourcesForInterval(0, 300);
    CheckProcessLoopback(audible.size() == 1
            && audible.front().identity.display_name == "Discord",
        "silent discovered Firefox does not publish a final stem");
    CheckProcessLoopback(audible.front().identity.id == discord->source.identity.id
            && audible.front().identity.persistent_identity == "discord",
        "manifest-facing source identity is real, stable for the runtime group, and not fabricated");
    auto child = Session("discord#7", 17, "discord", "Discord");
    child.process_group_root_id = 7;
    const auto grouped = coordinator.Discover(child);
    CheckProcessLoopback(grouped && grouped->process_id == 7,
        "process-loopback targets the deterministic group root so sibling helpers are not missed");
    const auto late = fthr::MapAudioSourcePresentationRange(0.0, 30.0,
        15ULL * 10'000'000ULL, 48000);
    CheckProcessLoopback(late.start_pts_samples == -720000
            && late.end_pts_samples == 720000,
        "a source beginning at 15 seconds retains its leading timeline gap instead of moving to t=0");
}

void ExitRestartFailureAndGenerationHistoryStayIsolated() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    fthr::WindowsApplicationSourceCoordinator coordinator(
        3, supported, Generator({
            "30000000-0000-4000-8000-000000000001",
            "30000000-0000-4000-8000-000000000002"}));
    const auto first = coordinator.Discover(Session("spotify#99", 99, "spotify", "Spotify"));
    CheckProcessLoopback(first.has_value(), "Spotify source is discoverable on supported Windows");
    CheckProcessLoopback(coordinator.HasRuntimeGroup("spotify#99"),
        "a discovered runtime group remains addressable until it is retired");
    coordinator.ObserveActivity(first->source.identity.id, 0.020f, 1000);
    coordinator.ObserveActivity(first->source.identity.id, 0.020f, 1100);
    const auto retired = coordinator.RetireRuntimeGroup("spotify#99", 1200);
    CheckProcessLoopback(retired && *retired == first->source.identity.id
            && coordinator.SourcesForInterval(900, 1250).size() == 1,
        "source exit retires only its runtime group while retaining replay history");
    CheckProcessLoopback(!coordinator.HasRuntimeGroup("spotify#99"),
        "retired runtime groups cannot keep an inactive provider candidate alive");
    const auto restart = coordinator.Discover(Session("spotify#99", 199, "spotify", "Spotify"));
    CheckProcessLoopback(restart && !(restart->source.identity.id == first->source.identity.id),
        "a restarted root-PID group receives a fresh source instead of unsafe name-only merging");
    coordinator.MarkFailed(restart->source.identity.id);
    CheckProcessLoopback(coordinator.SourcesForInterval(900, 1250).size() == 1,
        "one failed future source does not erase another source's retained history");

    fthr::WindowsApplicationSourceCoordinator next_generation(
        4, supported, Generator({"40000000-0000-4000-8000-000000000001"}));
    CheckProcessLoopback(next_generation.SourcesForInterval(900, 1250).empty(),
        "capture generations never inherit prior application identities or history");
}

void SourceRetirementByOpaqueIdUsesTheRuntimeGroupKey() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    fthr::WindowsApplicationSourceCoordinator coordinator(
        31, supported, Generator({
            "31000000-0000-4000-8000-000000000001"}));
    const auto source = coordinator.Discover(
        Session("game#31", 31, "game", "Game"));
    CheckProcessLoopback(source.has_value(), "source is discoverable for id retirement");
    coordinator.ObserveActivity(source->source.identity.id, 0.02f, 100);
    coordinator.ObserveActivity(source->source.identity.id, 0.02f, 200);

    const auto retired = coordinator.RetireSource(source->source.identity.id, 300);
    CheckProcessLoopback(retired && *retired == "game#31",
        "retirement resolves the opaque source id back to its runtime group");
    CheckProcessLoopback(coordinator.provider_candidate_count() == 0
            && coordinator.SourcesForInterval(0, 400).size() == 1,
        "retirement removes only the active group while preserving replay history");
}

void InactiveSourcePruningIsBoundedToTheReplayWindow() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    fthr::WindowsApplicationSourceCoordinator coordinator(
        32, supported, Generator({
            "32000000-0000-4000-8000-000000000001",
            "32000000-0000-4000-8000-000000000002"}), 2, 1);
    const auto stale = coordinator.Discover(
        Session("stale#1", 1, "stale", "Stale"));
    const auto current = coordinator.Discover(
        Session("current#2", 2, "current", "Current"));
    CheckProcessLoopback(stale && current, "sources are discoverable before pruning");
    coordinator.ObserveActivity(stale->source.identity.id, 0.02f, 100);
    coordinator.ObserveActivity(stale->source.identity.id, 0.02f, 200);
    coordinator.RetireRuntimeGroup("stale#1", 300);

    coordinator.PruneExpired(20'000'000);
    CheckProcessLoopback(coordinator.Find(stale->source.identity.id) == nullptr,
        "ended source metadata is removed after the replay retention window");
    CheckProcessLoopback(coordinator.Find(current->source.identity.id) != nullptr,
        "current source metadata remains available while its generation is active");
}

void TimelineDiscontinuityDoesNotCreateUnboundedSilenceWork() {
    const auto first = fthr::ReconcileAudioTimelinePacket(
        0, 10'000'000, 480, 48'000, 48'000, 5);
    CheckProcessLoopback(first.next_timeline_100ns == 10'100'000,
        "the first process-loopback packet establishes a bounded timeline cursor");
    const auto bounded = fthr::ReconcileAudioTimelinePacket(
        first.next_timeline_100ns, 11'100'000, 480, 48'000, 48'000, 5);
    CheckProcessLoopback(!bounded.large_gap && bounded.silence_frames == 4800,
        "a short process-loopback gap produces bounded synthetic silence");
    const auto suspended = fthr::ReconcileAudioTimelinePacket(
        bounded.next_timeline_100ns, 70'000'000, 480, 48'000, 48'000, 5);
    CheckProcessLoopback(suspended.large_gap && suspended.silence_frames == 0,
        "a suspend-sized process-loopback gap is classified instead of encoded wholesale");
}

void SourceLimitAndTrackContractRemainBounded() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    fthr::WindowsApplicationSourceCoordinator coordinator(
        5, supported, Generator({
            "50000000-0000-4000-8000-000000000001",
            "50000000-0000-4000-8000-000000000002",
            "50000000-0000-4000-8000-000000000003"}), 2);
    const auto one = coordinator.Discover(Session("a#1", 1, "a", "A"));
    const auto two = coordinator.Discover(Session("b#2", 2, "b", "B"));
    const auto three = coordinator.Discover(Session("c#3", 3, "c", "C"));
    CheckProcessLoopback(one && two && three, "silent candidates do not consume the active-stem limit");
    coordinator.ObserveActivity(one->source.identity.id, 0.02f, 100);
    coordinator.ObserveActivity(one->source.identity.id, 0.02f, 200);
    coordinator.ObserveActivity(two->source.identity.id, 0.02f, 300);
    coordinator.ObserveActivity(two->source.identity.id, 0.02f, 400);
    CheckProcessLoopback(coordinator.ObserveActivity(three->source.identity.id, 0.02f, 500)
            == fthr::AudioSourceAdmission::RejectedAtCapacity,
        "the configured source admission limit rejects a third stem deterministically");
    const auto sources = coordinator.SourcesForInterval(0, 700);
    CheckProcessLoopback(sources.size() == 2
            && sources[0].identity.id.IsValid() && sources[1].identity.id.IsValid(),
        "only admitted sources can become valid multi-stream manifest contracts");
}

void ExpiredReplayHistoryReleasesAnApplicationSlot() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    fthr::WindowsApplicationSourceCoordinator coordinator(
        51, supported, Generator({
            "51000000-0000-4000-8000-000000000001",
            "51000000-0000-4000-8000-000000000002",
            "51000000-0000-4000-8000-000000000003"}), 2, 1);
    const auto one = coordinator.Discover(Session("one#1", 1, "one", "One"));
    const auto two = coordinator.Discover(Session("two#2", 2, "two", "Two"));
    const auto three = coordinator.Discover(Session("three#3", 3, "three", "Three"));
    coordinator.ObserveActivity(one->source.identity.id, 0.02f, 100);
    coordinator.ObserveActivity(one->source.identity.id, 0.02f, 200);
    coordinator.RetireRuntimeGroup("one#1", 300);
    coordinator.ObserveActivity(two->source.identity.id, 0.02f, 400);
    coordinator.ObserveActivity(two->source.identity.id, 0.02f, 500);

    CheckProcessLoopback(
        coordinator.ObserveActivity(
            three->source.identity.id, 0.02f, 20'000'000)
            == fthr::AudioSourceAdmission::Accepted,
        "an inactive source older than the replay window releases its stem slot");
    const auto current = coordinator.SourcesForInterval(0, 21'000'000);
    CheckProcessLoopback(current.size() == 2
            && current[0].identity.display_name == "Two"
            && current[1].identity.display_name == "Three",
        "slot recycling retains current sources without reviving expired history");
}

void DefaultApplicationStemBoundaryIsExplicit() {
    fthr::WindowsProcessLoopbackCapability supported{22631, true, false};
    constexpr uint32_t expected_limit = 32;
    std::vector<std::string> ids;
    for (uint32_t index = 0; index <= expected_limit; ++index) {
        ids.push_back("60000000-0000-4000-8000-0000000000" + std::to_string(10 + index));
    }
    fthr::WindowsApplicationSourceCoordinator coordinator(
        6, supported, Generator(std::move(ids)));
    std::vector<fthr::WindowsApplicationSourceBinding> bindings;
    for (uint32_t index = 0; index <= expected_limit; ++index) {
        const std::string number = std::to_string(index + 1);
        const auto binding = coordinator.Discover(Session(
            ("app#" + number).c_str(), index + 1,
            ("app" + number).c_str(), ("App " + number).c_str()));
        CheckProcessLoopback(binding.has_value(), "all candidate sessions are discoverable");
        bindings.push_back(*binding);
    }
    for (uint32_t index = 0; index < expected_limit; ++index) {
        CheckProcessLoopback(coordinator.ObserveActivity(bindings[index].source.identity.id,
                0.02f, static_cast<int64_t>(index + 1) * 1000)
                == fthr::AudioSourceAdmission::Accepted,
            "each of the first 32 real app stems is admitted");
    }
    const int64_t over_limit_timestamp = static_cast<int64_t>(expected_limit + 1) * 1000;
    CheckProcessLoopback(coordinator.ObserveActivity(bindings.back().source.identity.id,
            0.02f, over_limit_timestamp) == fthr::AudioSourceAdmission::RejectedAtCapacity,
        "the 33rd real app stem is rejected without displacing retained history");
    CheckProcessLoopback(
        coordinator.SourcesForInterval(0, over_limit_timestamp).size() == expected_limit,
        "the production limit retains exactly 32 application stems");
}

void OptionalLiveApplicationCaptureDiagnostics() {
    wchar_t enabled[2] = {};
    if (GetEnvironmentVariableW(
            L"FTHR_TEST_LIVE_AUDIO_CAPTURE", enabled, 2) == 0) return;
    fthr::WindowsApplicationAudioSourceManager manager(77, 10);
    if (!manager.Start()) {
        std::cout << "LIVE APPLICATION CAPTURE: unavailable: "
                  << manager.last_error() << std::endl;
        return;
    }
    Sleep(4000);
    LARGE_INTEGER counter{};
    LARGE_INTEGER frequency{};
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    const double end = static_cast<double>(counter.QuadPart)
        / static_cast<double>(frequency.QuadPart);
    const auto tracks = manager.TakeTracksForInterval(end - 3.5, end);
    std::cout << "LIVE APPLICATION CAPTURE: " << tracks.size()
              << " audible stem(s)" << std::endl;
    for (const auto& track : tracks) {
        std::cout << "  CAPTURED " << track.source.identity.display_name
                  << " (" << track.snapshot.packets.size() << " AAC packets)"
                  << std::endl;
    }
    manager.Stop();
}

void OptionalLiveProcessLoopbackPreservesContinuousAudio() {
    // A quiet, continuous one-second 1 kHz mono WAV. Loop it in this process
    // so the actual WASAPI capture thread sees consecutive render packets.
    constexpr uint32_t sample_rate = 48'000;
    struct ToneWave {
        char riff[4] = {'R', 'I', 'F', 'F'};
        uint32_t riff_size = 36 + sample_rate * sizeof(int16_t);
        char wave[4] = {'W', 'A', 'V', 'E'};
        char format[4] = {'f', 'm', 't', ' '};
        uint32_t format_size = 16;
        uint16_t pcm = WAVE_FORMAT_PCM;
        uint16_t channels = 1;
        uint32_t rate = sample_rate;
        uint32_t byte_rate = sample_rate * sizeof(int16_t);
        uint16_t block_align = sizeof(int16_t);
        uint16_t bits = 16;
        char data[4] = {'d', 'a', 't', 'a'};
        uint32_t data_size = sample_rate * sizeof(int16_t);
        int16_t samples[sample_rate];
    } tone;
    static_assert(offsetof(ToneWave, samples) == 44);
    for (uint32_t frame = 0; frame < sample_rate; ++frame) {
        tone.samples[frame] = static_cast<int16_t>(
            std::sin((frame % 48) * (6.283185307179586 / 48.0)) * 819.0);
    }
    CheckProcessLoopback(PlaySoundA(reinterpret_cast<LPCSTR>(&tone), nullptr,
            SND_MEMORY | SND_ASYNC | SND_LOOP | SND_NODEFAULT) != FALSE,
        "the live process-loopback regression has an active render stream");

    fthr::WindowsProcessLoopbackProviderConfig config;
    config.generation = 1;
    config.target_process_id = GetCurrentProcessId();
    config.source.identity.id = fthr::AudioSourceId{"52000000-0000-4000-8000-000000000001"};
    config.source.identity.type = fthr::AudioSourceType::Application;
    uint64_t observed_frames = 0;
    uint32_t observed_packets = 0;
    uint32_t consecutive_zero_frames = 0;
    uint32_t longest_zero_run = 0;
    config.packet_sink = [&](const fthr::AudioSourceMetadata&, const uint8_t* data,
                             uint32_t, int64_t, int64_t frames) {
        // Give the capture queue time to run out while the next 10 ms tone
        // packet is pending. Emulate a short processing delay that exceeds
        // the timeline's 1 ms tolerance without skipping a render period.
        if (++observed_packets % 10 == 0) {
            LARGE_INTEGER frequency{}, begin{}, now{};
            QueryPerformanceFrequency(&frequency);
            QueryPerformanceCounter(&begin);
            do {
                SwitchToThread();
                QueryPerformanceCounter(&now);
            } while (now.QuadPart - begin.QuadPart < frequency.QuadPart * 4 / 1000);
        }
        for (int64_t frame = 0; frame < frames; ++frame) {
            // Ignore the first 200 ms to allow the render stream to settle.
            if (++observed_frames <= sample_rate / 5) continue;
            float stereo[2];
            std::memcpy(stereo, data + frame * sizeof(stereo), sizeof(stereo));
            if (stereo[0] == 0.0f && stereo[1] == 0.0f) {
                ++consecutive_zero_frames;
                longest_zero_run = (std::max)(longest_zero_run, consecutive_zero_frames);
            } else {
                consecutive_zero_frames = 0;
            }
        }
    };
    fthr::WindowsProcessLoopbackAudioProvider provider(
        std::move(config), [](const fthr::AudioSourceId&, float rms, int64_t) {
            return rms > 0.001f ? fthr::AudioSourceAdmission::Accepted
                : fthr::AudioSourceAdmission::RejectedInvalidIdentity;
        }, {}, {});
    const bool started = provider.Start();
    if (started) Sleep(3000);
    provider.Stop();
    PlaySoundA(nullptr, nullptr, 0);
    std::cout << "LIVE PROCESS LOOPBACK: " << observed_frames << " frames, longest zero run "
              << longest_zero_run << " frames" << std::endl;
    CheckProcessLoopback(started && observed_frames >= sample_rate,
        "the live process-loopback provider captures the test tone");
    CheckProcessLoopback(longest_zero_run < 20,
        "draining the WASAPI queue does not insert silence before the next tone packet");
}

}  // namespace

bool RunLiveProcessLoopbackToneTestIfRequested() {
    if (GetEnvironmentVariableW(
            L"FTHR_TEST_LIVE_PROCESS_LOOPBACK_TONE", nullptr, 0) == 0) return false;
    OptionalLiveProcessLoopbackPreservesContinuousAudio();
    return true;
}

int RunWindowsProcessLoopbackAudioProviderTests() {
    CapabilityGateDoesNotAttemptWindows10Sources();
    DiscoveryActivityAndSilentFilteringUseTheCommonModel();
    ExitRestartFailureAndGenerationHistoryStayIsolated();
    SourceRetirementByOpaqueIdUsesTheRuntimeGroupKey();
    InactiveSourcePruningIsBoundedToTheReplayWindow();
    TimelineDiscontinuityDoesNotCreateUnboundedSilenceWork();
    SourceLimitAndTrackContractRemainBounded();
    ExpiredReplayHistoryReleasesAnApplicationSlot();
    DefaultApplicationStemBoundaryIsExplicit();
    OptionalLiveApplicationCaptureDiagnostics();
    std::cout << "AUDIT-050 Windows process-loopback provider tests: "
              << process_loopback_checks << " checks passed" << std::endl;
    return process_loopback_checks;
}
