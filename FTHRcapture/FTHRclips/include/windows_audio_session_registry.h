// Runtime-only WASAPI session discovery for AUDIT-050 Windows audio providers.

#pragma once
#ifndef FTHR_WINDOWS_AUDIO_SESSION_REGISTRY_H
#define FTHR_WINDOWS_AUDIO_SESSION_REGISTRY_H

#include "audio_source_model.h"

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <map>
#include <string>
#include <vector>

namespace fthr {

inline constexpr uint32_t kProcessLoopbackMinimumBuild = 20348;

struct WindowsProcessLoopbackCapability {
    uint32_t os_build = 0;
    bool api_build_supported = false;
    // Runtime activation remains a separate check; a build number alone must
    // never be reported as an active per-application capture source.
    bool activation_verified = false;
};

WindowsProcessLoopbackCapability DetectWindowsProcessLoopbackCapability();
bool IsWindowsProcessLoopbackBuildSupported(uint32_t os_build);

struct WindowsAudioSessionDescriptor {
    uint32_t process_id = 0;             // generation-local; never persisted
    uint32_t process_group_root_id = 0;  // generation-local; never persisted
    std::string runtime_group_key;       // generation-local; never persisted
    AudioSourceIdentity identity;
    bool currently_active = false;
};

struct WindowsAudioSessionUpdate {
    std::vector<WindowsAudioSessionDescriptor> current;
    std::vector<std::string> appeared_runtime_groups;
    std::vector<std::string> disappeared_runtime_groups;
};

// Local policy helpers. These require no network/service lookup and provide a
// deterministic presentation/group identity once the provider has inspected a
// process.  They intentionally accept only executable basenames.
std::string NormalizeWindowsExecutableIdentity(const std::wstring& executable_basename);
std::string ResolveWindowsAudioSourceName(const std::wstring& executable_basename);
std::string ResolveWindowsAudioSourceName(const std::wstring& executable_basename,
                                          const std::wstring& executable_description);
std::string BuildWindowsRuntimeGroupKey(uint32_t root_process_id,
                                        const std::wstring& executable_basename);
bool IsVerifiedWindowsAudioParent(uint64_t child_created, uint64_t parent_created,
                                  uint64_t snapshot_started) noexcept;
bool IsFthrAudioUiParent(uint32_t verified_parent, uint32_t announced_ui,
                         const std::wstring& parent_image,
                         const std::wstring& engine_image);
void GroupWindowsAudioSessionTrees(
    std::map<std::string, WindowsAudioSessionDescriptor>& groups,
    const std::function<uint32_t(uint32_t)>& parent_process);
// Drop excluded executables and any broad INCLUDE provider whose subtree
// contains one. Other descendants remain independent candidates.
void ExcludeWindowsAudioSessionTrees(
    std::map<std::string, WindowsAudioSessionDescriptor>& groups,
    const std::vector<std::string>& excluded_identities,
    const std::function<uint32_t(uint32_t)>& parent_process,
    const std::vector<uint32_t>& excluded_processes = {});

class WindowsAudioSessionRegistry {
public:
    explicit WindowsAudioSessionRegistry(std::vector<std::string> excluded_identities = {});
    ~WindowsAudioSessionRegistry();

    WindowsAudioSessionRegistry(const WindowsAudioSessionRegistry&) = delete;
    WindowsAudioSessionRegistry& operator=(const WindowsAudioSessionRegistry&) = delete;

    // Start registers IAudioSessionNotification and captures an initial
    // snapshot. Callers own COM apartment setup and must call Stop on that
    // same apartment thread.
    bool Start();
    void Stop();

    bool needs_refresh() const { return dirty_.load(std::memory_order_acquire); }
    // Ask the next RefreshIfNeeded call to rescan endpoint topology as well as
    // live sessions. The production owner uses a slow safety poll in addition
    // to event notifications so a newly attached/rerouted output device cannot
    // make an application disappear from multitrack capture.
    void RequestRefresh() { MarkDirty(); }
    bool RefreshIfNeeded(WindowsAudioSessionUpdate* update);
    const std::string& last_error() const { return last_error_; }

private:
    class SessionNotification;
    class SessionEventNotification;
    struct EndpointRegistration;
    struct SessionEventRegistration;
    bool RefreshEndpoints();
    bool Refresh(WindowsAudioSessionUpdate* update);
    void MarkDirty() { dirty_.store(true, std::memory_order_release); }

    SessionNotification* notification_ = nullptr;
    // One IAudioSessionManager2 registration per active render endpoint. An
    // app routed away from the console default must still receive its own row.
    std::vector<std::unique_ptr<EndpointRegistration>> endpoint_registrations_;
    // One registration per live WASAPI session instance. Session-created alone
    // is insufficient for replay retention: disconnect/state notifications make
    // an exited source retire while its already-encoded history remains owned by
    // the AUDIT-050 source registry.
    std::map<std::wstring, std::unique_ptr<SessionEventRegistration>> session_events_;
    bool started_ = false;
    std::atomic<bool> dirty_{false};
    std::vector<WindowsAudioSessionDescriptor> current_;
    std::string last_error_;
    std::vector<std::string> excluded_identities_;
};

}  // namespace fthr

#endif  // FTHR_WINDOWS_AUDIO_SESSION_REGISTRY_H
