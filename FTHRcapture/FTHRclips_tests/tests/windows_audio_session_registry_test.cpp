#include "windows_audio_session_registry.h"
#include "capture_engine.h"

#include <windows.h>

#include <cstdlib>
#include <iostream>

namespace {

int windows_audio_checks = 0;

void CheckWindowsAudio(bool condition, const char* message) {
    ++windows_audio_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

void BuildCapabilityIsNotMarketingName() {
    CheckWindowsAudio(!fthr::IsWindowsProcessLoopbackBuildSupported(19045),
        "Windows 10 19045 does not claim official process-loopback support");
    CheckWindowsAudio(fthr::IsWindowsProcessLoopbackBuildSupported(20348),
        "minimum documented process-loopback build is admitted by capability");
    CheckWindowsAudio(fthr::IsWindowsProcessLoopbackBuildSupported(22631),
        "supported Windows 11 build is admitted by capability");
}

void NamesAndRuntimeGroupingAreLocalAndDeterministic() {
    CheckWindowsAudio(
        fthr::NormalizeWindowsExecutableIdentity(L"Discord.EXE") == "discord",
        "executable identity is normalized without a path");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(L"VALORANT-Win64-Shipping.exe") == "VALORANT",
        "known local game executable gets a readable label");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(L"firefox.exe") == "Firefox",
        "known local browser executable gets a readable label");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(L"chrome.exe") == "Google Chrome",
        "Chromium browser executable uses the product name users recognize");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(L"chrome.exe", L"Helium") == "Helium",
        "branded Chromium browser uses its signed product description");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(
            L"FPSAimTrainer-Win64-Shipping.exe") == "KovaaK's",
        "KovaaK executable gets the game name users recognize");
    CheckWindowsAudio(
        fthr::ResolveWindowsAudioSourceName(
            L"unknown-player.exe", L"Example Audio Player") == "Example Audio Player",
        "unknown executables use their local version-resource description");
    CheckWindowsAudio(
        fthr::BuildWindowsRuntimeGroupKey(4321, L"Discord.exe") == "discord#4321",
        "grouping is process-tree-root scoped rather than name-only");
    CheckWindowsAudio(
        fthr::BuildWindowsRuntimeGroupKey(0, L"Discord.exe").empty(),
        "zero process root cannot become an application group");
}

void SteamGameAndHelperStayDisjoint() {
    std::map<std::string, fthr::WindowsAudioSessionDescriptor> groups;
    const auto add = [&](const wchar_t* executable, uint32_t pid, bool active) {
        fthr::WindowsAudioSessionDescriptor session;
        session.process_id = pid;
        session.process_group_root_id = pid;
        session.runtime_group_key = fthr::BuildWindowsRuntimeGroupKey(pid, executable);
        session.identity.persistent_identity = fthr::NormalizeWindowsExecutableIdentity(executable);
        session.identity.display_name = fthr::ResolveWindowsAudioSourceName(executable);
        session.currently_active = active;
        groups.emplace(session.runtime_group_key, session);
    };
    add(L"steam.exe", 10, false);
    add(L"steamwebhelper.exe", 20, true);
    add(L"chrome.exe", 50, false);
    add(L"browserhelper.exe", 60, true);
    const std::map<uint32_t, uint32_t> parents{{20, 10}, {40, 30}, {30, 10}, {60, 50}};
    const auto parent = [&](uint32_t pid) {
        const auto found = parents.find(pid);
        return found == parents.end() ? 0u : found->second;
    };
    auto no_game = groups;
    fthr::GroupWindowsAudioSessionTrees(no_game, parent);
    CheckWindowsAudio(no_game.count("steam#10") && !no_game.count("steamwebhelper#20")
            && no_game.at("steam#10").currently_active,
        "Steam's helper can share the main Steam capture tree without a game");

    add(L"Overwatch.exe", 40, true);
    fthr::GroupWindowsAudioSessionTrees(groups, parent);
    CheckWindowsAudio(!groups.count("steam#10") && groups.count("steamwebhelper#20"),
        "an independent Steam-launched game excludes the overlapping launcher root");
    CheckWindowsAudio(groups.count("overwatch#40") && groups.at("overwatch#40").currently_active
            && groups.at("overwatch#40").identity.display_name == "Overwatch",
        "Overwatch remains active under its own game name and capture root");
    CheckWindowsAudio(!groups.count("browserhelper#60") && groups.at("chrome#50").currently_active,
        "browser helper activity still belongs to its browser capture tree");
}

void OptionalLiveSessionDiagnostics() {
    wchar_t enabled[2] = {};
    if (GetEnvironmentVariableW(
            L"FTHR_TEST_LIVE_AUDIO_SESSIONS", enabled, 2) == 0) return;
    const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    fthr::WindowsAudioSessionRegistry registry;
    if (!registry.Start()) {
        std::cout << "LIVE AUDIO SESSIONS: unavailable: " << registry.last_error()
                  << std::endl;
    } else {
        fthr::WindowsAudioSessionUpdate update;
        registry.RequestRefresh();
        registry.RefreshIfNeeded(&update);
        std::cout << "LIVE AUDIO SESSIONS: " << update.current.size()
                  << " application group(s)" << std::endl;
        for (const auto& source : update.current) {
            std::cout << "  " << (source.currently_active ? "ACTIVE " : "idle   ")
                      << source.identity.display_name << " ["
                      << source.identity.persistent_identity << "] pid="
                      << source.process_id << std::endl;
        }
        registry.Stop();
    }
    if (SUCCEEDED(apartment)) CoUninitialize();
}

void OwnAudioTreesRequireVerifiedUiAncestry() {
    using fthr::IsFthrAudioUiParent;
    using fthr::IsVerifiedWindowsAudioParent;
    CheckWindowsAudio(IsFthrAudioUiParent(10, 0,
        LR"(C:\Program Files\FTHR Clips\FTHRClips.exe)",
        LR"(c:\program files\fthr clips\_internal\engine\FTHRclips.exe)"),
        "packaged engine accepts the UI at its own bundle root");
    CheckWindowsAudio(IsFthrAudioUiParent(10, 10, L"python.exe", L"FTHRclips.exe"),
        "source-mode UI can identify itself as the verified parent");
    CheckWindowsAudio(!IsFthrAudioUiParent(10, 11, L"python.exe", L"FTHRclips.exe")
        && !IsFthrAudioUiParent(0, 10, L"python.exe", L"FTHRclips.exe"),
        "an unrelated or unverified Python PID cannot become the UI root");
    CheckWindowsAudio(!IsFthrAudioUiParent(10, 0, LR"(C:\Other\FTHRClips.exe)",
        LR"(C:\App\_internal\engine\FTHRclips.exe)"),
        "an identically named process outside the bundle cannot be its UI");
    CheckWindowsAudio(!IsVerifiedWindowsAudioParent(20, 25, 30)
        && !IsVerifiedWindowsAudioParent(31, 10, 30)
        && !IsVerifiedWindowsAudioParent(20, 0, 30),
        "reused or unobservable process identities cannot establish ancestry");

    const std::map<uint32_t, uint32_t> parents{
        {10, 1}, {11, 10}, {12, 10}, {13, 12}, {30, 1}, {40, 10}, {50, 12}};
    const std::map<uint32_t, uint64_t> created{
        {1, 5}, {10, 15}, {11, 20}, {12, 25}, {13, 30}, {30, 35}, {40, 10}, {50, 70}};
    const auto parent = [&](uint32_t pid) {
        const auto entry = parents.find(pid);
        if (entry == parents.end()) return 0u;
        return IsVerifiedWindowsAudioParent(created.at(pid), created.at(entry->second), 60)
            ? entry->second : 0u;
    };
    std::map<std::string, fthr::WindowsAudioSessionDescriptor> groups;
    for (const auto& [key, pid] : std::map<std::string, uint32_t>{
        {"launcher", 1}, {"preview", 10}, {"engine", 11}, {"card", 12},
        {"card-child", 13}, {"game", 30}, {"music", 40}, {"reused-child", 50}}) {
        groups[key].process_id = pid;
    }
    fthr::ExcludeWindowsAudioSessionTrees(groups, {}, parent, {10});
    CheckWindowsAudio(groups.size() == 3 && groups.count("game")
        && groups.count("music") && groups.count("reused-child"),
        "UI preview, helpers and their broad launcher mix are excluded while independent apps remain");
}

void ExclusionsSurviveRestartsAndDoNotHideInsideParentMixes() {
    const auto make_group = [](const char* identity, uint32_t pid) {
        fthr::WindowsAudioSessionDescriptor descriptor;
        descriptor.process_id = descriptor.process_group_root_id = pid;
        descriptor.runtime_group_key = std::string(identity) + "#" + std::to_string(pid);
        descriptor.identity.persistent_identity = identity;
        descriptor.currently_active = true;
        return descriptor;
    };
    const auto add = [&](auto& groups, const char* identity, uint32_t pid) {
        auto descriptor = make_group(identity, pid);
        groups.emplace(descriptor.runtime_group_key, descriptor);
    };
    for (uint32_t excluded_pid : {20u, 120u}) {
        std::map<std::string, fthr::WindowsAudioSessionDescriptor> groups;
        add(groups, "launcher", 10);
        add(groups, "discord", excluded_pid);
        add(groups, "renderer", excluded_pid + 1);
        add(groups, "game", 30);
        add(groups, "spotify", 40);
        const auto parent = [&](uint32_t pid) {
            if (pid == excluded_pid || pid == 30) return 10u;
            if (pid == excluded_pid + 1) return excluded_pid;
            return 0u;
        };
        fthr::ExcludeWindowsAudioSessionTrees(groups, {"discord"}, parent);
        fthr::GroupWindowsAudioSessionTrees(groups, parent);
        CheckWindowsAudio(groups.size() == 2 && groups.count("game#30") && groups.count("spotify#40"),
            "excluded exe, its renderer, and ancestor mix are removed; game sibling remains after restart");
    }
    std::map<std::string, fthr::WindowsAudioSessionDescriptor> groups;
    add(groups, "renderer", 3);
    add(groups, "game", 9);
    fthr::ExcludeWindowsAudioSessionTrees(groups, {"discord"},
        [](uint32_t pid) { return pid == 3 ? 2u : 0u; }, {2});
    CheckWindowsAudio(groups.size() == 1 && groups.count("game#9"),
        "excluded running parent need not have its own audio session");

    fthr::CaptureConfig config;
    config.application_audio_tracks_enabled = true;
    CheckWindowsAudio(config.keep_system_audio_backup, "system backup defaults on for compatibility");
    CheckWindowsAudio(config.RetainDefaultMixAudio(), "unfiltered programs retain compatibility backup");
    config.keep_system_audio_backup = false;
    CheckWindowsAudio(!config.RetainDefaultMixAudio(),
        "Programs backup off omits the device loopback even without exclusions");
    config.application_audio_tracks_enabled = false;
    CheckWindowsAudio(config.RetainDefaultMixAudio(),
        "Device mode retains its system audio when Programs backup is off");
    config.application_audio_tracks_enabled = true;
    config.keep_system_audio_backup = true;
    config.application_audio_exclusions = {"discord"};
    CheckWindowsAudio(config.HasApplicationAudioExclusions() && !config.RetainDefaultMixAudio(),
        "program blacklist disables entire system mix, independent of process-loopback availability");
    config.keep_system_audio_backup = false;
    CheckWindowsAudio(!config.RetainDefaultMixAudio(), "exclusions also omit a disabled backup");
    config.keep_system_audio_backup = true;
    CheckWindowsAudio(!config.RetainDefaultMixAudio(), "re-enabling the preference cannot bypass exclusions");
    config.application_audio_tracks_enabled = false;
    CheckWindowsAudio(!config.HasApplicationAudioExclusions() && config.RetainDefaultMixAudio(),
        "saved program exclusions do not alter device capture mode");
}

}  // namespace

int RunWindowsAudioSessionRegistryTests() {
    BuildCapabilityIsNotMarketingName();
    NamesAndRuntimeGroupingAreLocalAndDeterministic();
    SteamGameAndHelperStayDisjoint();
    OwnAudioTreesRequireVerifiedUiAncestry();
    ExclusionsSurviveRestartsAndDoNotHideInsideParentMixes();
    OptionalLiveSessionDiagnostics();
    std::cout << "AUDIT-050 Windows session registry tests: " << windows_audio_checks
              << " checks passed" << std::endl;
    return windows_audio_checks;
}
