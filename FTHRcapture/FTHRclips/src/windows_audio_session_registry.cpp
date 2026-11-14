#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <audiopolicy.h>
#include <mmdeviceapi.h>
#include <tlhelp32.h>
#include <wrl/client.h>

#include "windows_audio_session_registry.h"
#include "audio_process_tree.h"

#include <algorithm>
#include <cctype>
#include <cwctype>
#include <filesystem>
#include <map>
#include <set>
#include <sstream>
#include <vector>

#pragma comment(lib, "Mmdevapi.lib")
#pragma comment(lib, "Ole32.lib")
#pragma comment(lib, "Version.lib")

using Microsoft::WRL::ComPtr;

namespace fthr {

namespace {

struct RtlOsVersionInfo {
    ULONG dwOSVersionInfoSize;
    ULONG dwMajorVersion;
    ULONG dwMinorVersion;
    ULONG dwBuildNumber;
    ULONG dwPlatformId;
    WCHAR szCSDVersion[128];
};
using RtlGetVersionFn = LONG(WINAPI*)(RtlOsVersionInfo*);

std::wstring ToLower(std::wstring text) {
    std::transform(text.begin(), text.end(), text.begin(), [](wchar_t character) {
        return static_cast<wchar_t>(towlower(character));
    });
    return text;
}

std::string ToUtf8(const std::wstring& text) {
    if (text.empty()) return {};
    const int required = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
        text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    if (required <= 0) return {};
    std::string result(static_cast<size_t>(required), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            text.data(), static_cast<int>(text.size()), result.data(), required,
            nullptr, nullptr) <= 0) {
        return {};
    }
    return result;
}

std::string AsciiLower(std::string text) {
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    return text;
}

struct ExecutableMetadata {
    std::wstring basename;
    std::wstring description;
};

std::wstring ExecutableDescription(const std::wstring& path) {
    if (path.empty()) return {};
    DWORD ignored = 0;
    const DWORD size = GetFileVersionInfoSizeW(path.c_str(), &ignored);
    if (size == 0) return {};
    std::vector<BYTE> version(size);
    if (!GetFileVersionInfoW(path.c_str(), 0, size, version.data())) return {};

    struct Translation { WORD language; WORD code_page; };
    Translation* translations = nullptr;
    UINT translation_bytes = 0;
    std::vector<Translation> fallbacks;
    if (!VerQueryValueW(version.data(), L"\\VarFileInfo\\Translation",
            reinterpret_cast<void**>(&translations), &translation_bytes)
            || !translations || translation_bytes < sizeof(Translation)) {
        // The common en-US Unicode resource remains a useful fallback for
        // executables that omit the translation table.
        fallbacks.push_back({0x0409, 0x04b0});
        translations = fallbacks.data();
        translation_bytes = static_cast<UINT>(fallbacks.size() * sizeof(Translation));
    }

    const size_t translation_count = translation_bytes / sizeof(Translation);
    for (const wchar_t* field : {L"FileDescription", L"ProductName"}) {
        for (size_t index = 0; index < translation_count; ++index) {
            wchar_t query[96] = {};
            _snwprintf_s(query, _TRUNCATE,
                L"\\StringFileInfo\\%04x%04x\\%ls",
                translations[index].language, translations[index].code_page, field);
            wchar_t* value = nullptr;
            UINT characters = 0;
            if (!VerQueryValueW(version.data(), query,
                    reinterpret_cast<void**>(&value), &characters)
                    || !value || characters <= 1) {
                continue;
            }
            std::wstring result(value, characters - 1);
            if (!SanitizeAudioSourceText(ToUtf8(result), 80).empty()) return result;
        }
    }
    return {};
}

std::wstring ProcessImagePath(HANDLE process) {
    if (!process) return {};
    std::wstring path(32768, L'\0');
    DWORD length = static_cast<DWORD>(path.size());
    const bool ok = QueryFullProcessImageNameW(process, 0, path.data(), &length) != FALSE;
    if (!ok || length == 0) return {};
    path.resize(length);
    return path;
}

ExecutableMetadata ExecutableMetadataForProcess(HANDLE process) {
    const auto path = ProcessImagePath(process);
    return {std::filesystem::path(path).filename().wstring(),
            ExecutableDescription(path)};
}

std::wstring SessionInstanceKey(IAudioSessionControl2* control) {
    if (!control) return {};
    LPWSTR instance_id = nullptr;
    if (FAILED(control->GetSessionInstanceIdentifier(&instance_id)) || !instance_id) return {};
    std::wstring result(instance_id);
    CoTaskMemFree(instance_id);
    return result;
}

uint64_t FileTimeValue(FILETIME time) noexcept {
    return (static_cast<uint64_t>(time.dwHighDateTime) << 32) | time.dwLowDateTime;
}

struct ProcessTreeSnapshot {
    struct ProcessStamp {
        HANDLE handle = nullptr;
        uint64_t created = 0;

        ~ProcessStamp() { if (handle) CloseHandle(handle); }
        ProcessStamp() = default;
        ProcessStamp(const ProcessStamp&) = delete;
        ProcessStamp& operator=(const ProcessStamp&) = delete;
    };

    std::map<uint32_t, PROCESSENTRY32W> entries;
    // Keep handles to validated processes for the lifetime of this refresh.
    std::map<uint32_t, ProcessStamp> processes;
    uint64_t started_at = 0;

    ProcessTreeSnapshot() {
        FILETIME time{};
        GetSystemTimePreciseAsFileTime(&time);
        started_at = FileTimeValue(time);
        const HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == INVALID_HANDLE_VALUE) return;
        PROCESSENTRY32W entry{};
        entry.dwSize = sizeof(entry);
        if (Process32FirstW(snapshot, &entry)) {
            do {
                entries.emplace(entry.th32ProcessID, entry);
            } while (Process32NextW(snapshot, &entry));
        }
        CloseHandle(snapshot);
    }

    const ProcessStamp& Inspect(uint32_t pid) {
        auto [entry, inserted] = processes.try_emplace(pid);
        auto& stamp = entry->second;
        if (!inserted) return stamp;
        stamp.handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (!stamp.handle) return stamp;
        FILETIME created{}, exited{}, kernel{}, user{};
        if (GetProcessTimes(stamp.handle, &created, &exited, &kernel, &user))
            stamp.created = FileTimeValue(created);
        return stamp;
    }

    uint32_t Parent(uint32_t child) {
        const auto entry = entries.find(child);
        if (entry == entries.end()) return 0;
        const uint32_t parent = entry->second.th32ParentProcessID;
        if (!parent || parent == child || !entries.count(parent)) return 0;
        return IsVerifiedWindowsAudioParent(
            Inspect(child).created, Inspect(parent).created, started_at) ? parent : 0;
    }
};

uint32_t AnnouncedAudioUiPid() {
    wchar_t value[16]{};
    const DWORD length = GetEnvironmentVariableW(
        L"FTHR_AUDIO_UI_PID", value, ARRAYSIZE(value));
    if (!length || length >= ARRAYSIZE(value)) return 0;
    uint64_t pid = 0;
    for (DWORD i = 0; i < length; ++i) {
        if (value[i] < L'0' || value[i] > L'9') return 0;
        pid = pid * 10 + static_cast<unsigned>(value[i] - L'0');
        if (pid > UINT32_MAX) return 0;
    }
    return static_cast<uint32_t>(pid);
}

bool IsInFthrAudioTree(uint32_t pid, uint32_t root, ProcessTreeSnapshot& processes) {
    if (!root) return false;
    for (uint32_t depth = 0; pid && depth < 64; ++depth) {
        if (pid == root) return true;
        pid = processes.Parent(pid);
    }
    return false;
}

uint32_t ProcessGroupRoot(uint32_t process_id, const std::wstring& executable_basename,
                          uint32_t ui_pid, ProcessTreeSnapshot& processes) {
    const std::wstring wanted = ToLower(executable_basename);
    uint32_t root = process_id;
    uint32_t cursor = process_id;
    // Only merge a child into an ancestor when the executable identity agrees.
    // That makes launcher/game boundaries conservative rather than guessing.
    for (uint32_t depth = 0; depth < 32; ++depth) {
        const uint32_t parent = processes.Parent(cursor);
        if (parent == 0 || ToLower(processes.entries.at(parent).szExeFile) != wanted) break;
        // A same-name launcher can own both the UI tree and an independent
        // renderer. Keep that renderer disjoint from the broad excluded mix.
        if (IsInFthrAudioTree(ui_pid, parent, processes)) break;
        root = parent;
        cursor = parent;
    }
    return root;
}

AudioSourceIdentity IdentityForProcess(uint32_t process_id,
                                       const std::wstring& executable_basename,
                                       const std::wstring& executable_description) {
    AudioSourceIdentity identity;
    // Runtime creation gets the final opaque UUID from the source provider.
    // The session registry never derives an ID from PID, because PIDs are
    // recyclable and must not be a persistent source identity.
    identity.type = AudioSourceType::Application;
    identity.persistent_identity = NormalizeWindowsExecutableIdentity(executable_basename);
    identity.display_name = ResolveWindowsAudioSourceName(
        executable_basename, executable_description);
    identity.icon_reference = "windows-app-icon";
    (void)process_id;
    return identity;
}

}  // namespace

WindowsProcessLoopbackCapability DetectWindowsProcessLoopbackCapability() {
    WindowsProcessLoopbackCapability result;
    const HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    const auto get_version = ntdll ? reinterpret_cast<RtlGetVersionFn>(
        GetProcAddress(ntdll, "RtlGetVersion")) : nullptr;
    RtlOsVersionInfo version{};
    version.dwOSVersionInfoSize = sizeof(version);
    if (get_version && get_version(&version) == 0)
        result.os_build = version.dwBuildNumber;
    result.api_build_supported = IsWindowsProcessLoopbackBuildSupported(result.os_build);
    return result;
}

bool IsWindowsProcessLoopbackBuildSupported(uint32_t os_build) {
    return os_build >= kProcessLoopbackMinimumBuild;
}

bool IsVerifiedWindowsAudioParent(uint64_t child_created, uint64_t parent_created,
                                  uint64_t snapshot_started) noexcept {
    // A replacement PID born after its child cannot be the original parent.
    // Reject processes created during or after enumeration as well so a
    // recycled child cannot inherit an older snapshot entry.
    return parent_created > 0 && parent_created < child_created
        && child_created < snapshot_started;
}

bool IsFthrAudioUiParent(uint32_t verified_parent, uint32_t announced_ui,
                         const std::wstring& parent_image,
                         const std::wstring& engine_image) {
    if (!verified_parent) return false;
    // Source-mode Python has no unique image name. The UI identifies itself
    // in the launch environment, and only its verified direct child trusts it.
    if (verified_parent == announced_ui) return true;
    const std::filesystem::path ui_path(parent_image);
    if (ToLower(ui_path.filename().wstring()) != L"fthrclips.exe") return false;
    const auto packaged_engine = ui_path.parent_path()
        / L"_internal" / L"engine" / ui_path.filename();
    return ToLower(packaged_engine.wstring()) == ToLower(engine_image);
}

std::string NormalizeWindowsExecutableIdentity(const std::wstring& executable_basename) {
    std::wstring identity = ToLower(executable_basename);
    const std::wstring suffix = L".exe";
    if (identity.size() > suffix.size()
            && identity.compare(identity.size() - suffix.size(), suffix.size(), suffix) == 0) {
        identity.resize(identity.size() - suffix.size());
    }
    return SanitizeAudioSourceText(ToUtf8(identity), 128);
}

std::string ResolveWindowsAudioSourceName(const std::wstring& executable_basename) {
    return ResolveWindowsAudioSourceName(executable_basename, {});
}

std::string ResolveWindowsAudioSourceName(
    const std::wstring& executable_basename,
    const std::wstring& executable_description) {
    const std::string identity = NormalizeWindowsExecutableIdentity(executable_basename);
    if (identity == "valorant-win64-shipping") return "VALORANT";
    if (identity == "overwatch") return "Overwatch";
    if (identity == "fpsaimtrainer" || identity == "fpsaimtrainer-win64-shipping")
        return "KovaaK's";
    if (identity == "discord") return "Discord";
    if (identity == "discordcanary") return "Discord Canary";
    if (identity == "discordptb") return "Discord PTB";
    if (identity == "firefox") return "Firefox";
    const std::string described = SanitizeAudioSourceText(
        ToUtf8(executable_description), 80);
    if (identity == "chrome") {
        // Chromium products often retain chrome.exe as OriginalFilename. The
        // signed version resource is a better user-facing identity for Helium
        // and other branded Chromium browsers.
        const std::string normalized_description = AsciiLower(described);
        if (!described.empty() && normalized_description != "google chrome"
                && normalized_description != "chromium") {
            return described;
        }
        return "Google Chrome";
    }
    if (identity == "msedge") return "Microsoft Edge";
    if (identity == "brave") return "Brave";
    if (identity == "vivaldi") return "Vivaldi";
    if (identity == "opera" || identity == "opera_gx") return "Opera";
    if (identity == "spotify") return "Spotify";
    if (identity == "vlc") return "VLC";
    if (identity == "steam" || identity == "steamwebhelper") return "Steam";
    if (identity == "teams" || identity == "ms-teams") return "Microsoft Teams";
    if (identity == "obs64" || identity == "obs32") return "OBS Studio";
    if (!described.empty()) return described;
    if (identity.empty()) return "Application";
    std::string result = identity;
    result[0] = static_cast<char>(toupper(static_cast<unsigned char>(result[0])));
    return result;
}

std::string BuildWindowsRuntimeGroupKey(uint32_t root_process_id,
                                        const std::wstring& executable_basename) {
    const std::string identity = NormalizeWindowsExecutableIdentity(executable_basename);
    if (identity.empty() || root_process_id == 0) return {};
    return identity + "#" + std::to_string(root_process_id);
}

void GroupWindowsAudioSessionTrees(
    std::map<std::string, WindowsAudioSessionDescriptor>& groups,
    const std::function<uint32_t(uint32_t)>& parent_process) {
    std::vector<uint32_t> roots;
    std::map<uint32_t, std::string> key_by_root;
    for (const auto& [key, descriptor] : groups) {
        roots.push_back(descriptor.process_id);
        key_by_root[descriptor.process_id] = key;
    }
    auto owners = AudioProcessTreeOwners(roots, parent_process);
    std::set<uint32_t> conflicting_launchers;
    for (const auto& [_, descriptor] : groups) {
        const uint32_t owner = owners.at(descriptor.process_id);
        if (owner == descriptor.process_id) continue;
        const auto& ancestor = groups.at(key_by_root.at(owner));
        if (ancestor.identity.persistent_identity == "steam"
                && descriptor.identity.persistent_identity != "steam"
                && descriptor.identity.persistent_identity != "steamwebhelper") {
            conflicting_launchers.insert(owner);
        }
    }
    if (!conflicting_launchers.empty()) {
        // WASAPI INCLUDE also captures descendants. A Steam client root would
        // contain both its own audio and the separately identified game.
        // Drop that broad root so the game and disjoint Steam helpers can be
        // captured once each. The endpoint mix still contains client audio.
        for (const uint32_t launcher : conflicting_launchers)
            groups.erase(key_by_root.at(launcher));
        roots.erase(std::remove_if(roots.begin(), roots.end(), [&](uint32_t root) {
            return conflicting_launchers.count(root) != 0;
        }), roots.end());
        owners = AudioProcessTreeOwners(roots, parent_process);
    }
    std::vector<std::string> nested;
    for (const auto& [key, descriptor] : groups) {
        const auto owner = owners.at(descriptor.process_id);
        if (owner == descriptor.process_id) continue;
        auto& group = groups.at(key_by_root.at(owner));
        group.currently_active = group.currently_active || descriptor.currently_active;
        if (group.identity.display_name.find(" (+ child apps)") == std::string::npos)
            group.identity.display_name = SanitizeAudioSourceText(
                group.identity.display_name.substr(0, 64) + " (+ child apps)", 80);
        nested.push_back(key);
    }
    for (const auto& key : nested) groups.erase(key);
}

class WindowsAudioSessionRegistry::SessionNotification final
    : public IAudioSessionNotification {
public:
    explicit SessionNotification(WindowsAudioSessionRegistry* owner) : owner_(owner) {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == IID_IUnknown || iid == __uuidof(IAudioSessionNotification)) {
            *object = static_cast<IAudioSessionNotification*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (refs == 0) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE OnSessionCreated(IAudioSessionControl*) override {
        if (owner_) owner_->MarkDirty();
        return S_OK;
    }

private:
    std::atomic<ULONG> refs_{1};
    WindowsAudioSessionRegistry* owner_;
};

class WindowsAudioSessionRegistry::SessionEventNotification final
    : public IAudioSessionEvents {
public:
    explicit SessionEventNotification(WindowsAudioSessionRegistry* owner) : owner_(owner) {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == IID_IUnknown || iid == __uuidof(IAudioSessionEvents)) {
            *object = static_cast<IAudioSessionEvents*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (refs == 0) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE OnDisplayNameChanged(LPCWSTR, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnIconPathChanged(LPCWSTR, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSimpleVolumeChanged(float, BOOL, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnChannelVolumeChanged(DWORD, float[], DWORD, LPCGUID) override {
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnGroupingParamChanged(LPCGUID, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnStateChanged(AudioSessionState) override {
        if (owner_) owner_->MarkDirty();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnSessionDisconnected(AudioSessionDisconnectReason) override {
        if (owner_) owner_->MarkDirty();
        return S_OK;
    }

private:
    std::atomic<ULONG> refs_{1};
    WindowsAudioSessionRegistry* owner_;
};

struct WindowsAudioSessionRegistry::SessionEventRegistration {
    IAudioSessionControl* control = nullptr;
    SessionEventNotification* notification = nullptr;

    ~SessionEventRegistration() {
        if (control && notification) control->UnregisterAudioSessionNotification(notification);
        if (notification) notification->Release();
        if (control) control->Release();
    }
};

struct WindowsAudioSessionRegistry::EndpointRegistration {
    std::wstring device_id;
    IAudioSessionManager2* manager = nullptr;

    ~EndpointRegistration() {
        if (manager) manager->Release();
    }
};

void ExcludeWindowsAudioSessionTrees(
    std::map<std::string, WindowsAudioSessionDescriptor>& groups,
    const std::vector<std::string>& excluded_identities,
    const std::function<uint32_t(uint32_t)>& parent_process,
    const std::vector<uint32_t>& excluded_processes) {
    std::set<uint32_t> excluded_roots(excluded_processes.begin(), excluded_processes.end());
    std::set<uint32_t> forbidden_roots;
    for (const auto& [key, descriptor] : groups) {
        if (std::find(excluded_identities.begin(), excluded_identities.end(),
                      descriptor.identity.persistent_identity) == excluded_identities.end()) continue;
        excluded_roots.insert(descriptor.process_id);
    }
    for (uint32_t root : excluded_roots) {
        uint32_t cursor = root;
        for (uint32_t depth = 0; cursor && depth < 64; ++depth) {
            if (!forbidden_roots.insert(cursor).second) break;
            const uint32_t parent = parent_process(cursor);
            if (parent == cursor) break;
            cursor = parent;
        }
    }
    for (auto it = groups.begin(); it != groups.end();) {
        bool excluded = forbidden_roots.count(it->second.process_id) != 0;
        uint32_t cursor = it->second.process_id;
        for (uint32_t depth = 0; !excluded && cursor && depth < 64; ++depth) {
            excluded = excluded_roots.count(cursor) != 0;
            const uint32_t parent = parent_process(cursor);
            if (parent == cursor) break;
            cursor = parent;
        }
        if (excluded) it = groups.erase(it);
        else ++it;
    }
}

WindowsAudioSessionRegistry::WindowsAudioSessionRegistry(std::vector<std::string> excluded_identities)
    : excluded_identities_(std::move(excluded_identities)) {}

WindowsAudioSessionRegistry::~WindowsAudioSessionRegistry() {
    Stop();
}

bool WindowsAudioSessionRegistry::Start() {
    if (started_) return true;
    last_error_.clear();
    notification_ = new SessionNotification(this);
    if (!RefreshEndpoints()) {
        notification_->Release();
        notification_ = nullptr;
        return false;
    }
    started_ = true;
    dirty_.store(true, std::memory_order_release);
    WindowsAudioSessionUpdate ignored;
    if (RefreshIfNeeded(&ignored)) return true;
    Stop();
    return false;
}

void WindowsAudioSessionRegistry::Stop() {
    for (auto& endpoint : endpoint_registrations_) {
        if (endpoint && endpoint->manager && notification_)
            endpoint->manager->UnregisterSessionNotification(notification_);
    }
    endpoint_registrations_.clear();
    if (notification_) notification_->Release();
    notification_ = nullptr;
    session_events_.clear();
    current_.clear();
    started_ = false;
    dirty_.store(false, std::memory_order_release);
}

bool WindowsAudioSessionRegistry::RefreshIfNeeded(WindowsAudioSessionUpdate* update) {
    if (!update) return false;
    if (!started_) return false;
    if (!dirty_.exchange(false, std::memory_order_acq_rel)) {
        update->current = current_;
        return true;
    }
    if (!Refresh(update)) {
        dirty_.store(true, std::memory_order_release);
        return false;
    }
    return true;
}

bool WindowsAudioSessionRegistry::RefreshEndpoints() {
    if (!notification_) return false;
    ComPtr<IMMDeviceEnumerator> enumerator;
    ComPtr<IMMDeviceCollection> devices;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr,
        CLSCTX_ALL, IID_PPV_ARGS(&enumerator));
    if (SUCCEEDED(hr)) hr = enumerator->EnumAudioEndpoints(
        eRender, DEVICE_STATE_ACTIVE, &devices);
    if (FAILED(hr) || !devices) {
        last_error_ = "could not enumerate active Windows render endpoints";
        return !endpoint_registrations_.empty();
    }

    UINT count = 0;
    if (FAILED(devices->GetCount(&count))) {
        last_error_ = "could not count active Windows render endpoints";
        return !endpoint_registrations_.empty();
    }
    std::set<std::wstring> live_ids;
    for (UINT index = 0; index < count; ++index) {
        ComPtr<IMMDevice> device;
        LPWSTR raw_id = nullptr;
        if (FAILED(devices->Item(index, &device))
                || FAILED(device->GetId(&raw_id)) || !raw_id) {
            if (raw_id) CoTaskMemFree(raw_id);
            continue;
        }
        const std::wstring device_id(raw_id);
        CoTaskMemFree(raw_id);
        live_ids.insert(device_id);
        const auto existing = std::find_if(
            endpoint_registrations_.begin(), endpoint_registrations_.end(),
            [&device_id](const auto& registration) {
                return registration && registration->device_id == device_id;
            });
        if (existing != endpoint_registrations_.end()) continue;

        ComPtr<IAudioSessionManager2> manager;
        hr = device->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL,
            nullptr, reinterpret_cast<void**>(manager.GetAddressOf()));
        if (FAILED(hr) || !manager
                || FAILED(manager->RegisterSessionNotification(notification_))) {
            continue;
        }
        auto registration = std::make_unique<EndpointRegistration>();
        registration->device_id = device_id;
        registration->manager = manager.Detach();
        endpoint_registrations_.push_back(std::move(registration));
    }

    for (auto it = endpoint_registrations_.begin(); it != endpoint_registrations_.end();) {
        if (*it && live_ids.count((*it)->device_id)) {
            ++it;
            continue;
        }
        if (*it && (*it)->manager)
            (*it)->manager->UnregisterSessionNotification(notification_);
        it = endpoint_registrations_.erase(it);
    }
    if (endpoint_registrations_.empty()) {
        last_error_ = "could not open an active Windows render-session manager";
        return false;
    }
    return true;
}

bool WindowsAudioSessionRegistry::Refresh(WindowsAudioSessionUpdate* update) {
    if (!update || !RefreshEndpoints()) return false;

    std::map<uint32_t, bool> session_activity;
    std::map<std::wstring, bool> live_session_instances;
    uint32_t enumerated_endpoints = 0;
    for (const auto& endpoint : endpoint_registrations_) {
        if (!endpoint || !endpoint->manager) continue;
        ComPtr<IAudioSessionEnumerator> sessions;
        if (FAILED(endpoint->manager->GetSessionEnumerator(&sessions)) || !sessions)
            continue;
        int count = 0;
        if (FAILED(sessions->GetCount(&count))) continue;
        ++enumerated_endpoints;
        for (int index = 0; index < count; ++index) {
            ComPtr<IAudioSessionControl> control;
            ComPtr<IAudioSessionControl2> control2;
            if (FAILED(sessions->GetSession(index, &control))
                    || FAILED(control.As(&control2))) {
                continue;
            }
            const std::wstring session_instance = SessionInstanceKey(control2.Get());
            const std::wstring event_key = endpoint->device_id + L"|" + session_instance;
            if (!session_instance.empty()) {
                live_session_instances.emplace(event_key, true);
                if (!session_events_.count(event_key)) {
                    auto registration = std::make_unique<SessionEventRegistration>();
                    registration->control = control.Get();
                    registration->control->AddRef();
                    registration->notification = new SessionEventNotification(this);
                    const HRESULT notification_hr = registration->control
                        ->RegisterAudioSessionNotification(registration->notification);
                    if (FAILED(notification_hr)) {
                        registration.reset();
                    } else {
                        session_events_.emplace(event_key, std::move(registration));
                    }
                }
            }
            DWORD pid = 0;
            if (FAILED(control2->GetProcessId(&pid)) || pid == 0) continue;
            AudioSessionState state = AudioSessionStateInactive;
            auto& active = session_activity[pid];
            active = active || (SUCCEEDED(control->GetState(&state))
                && state == AudioSessionStateActive);
        }
    }
    if (enumerated_endpoints == 0) {
        last_error_ = "could not enumerate Windows audio sessions on any active endpoint";
        return false;
    }
    for (auto it = session_events_.begin(); it != session_events_.end();) {
        if (!live_session_instances.count(it->first)) it = session_events_.erase(it);
        else ++it;
    }

    // A newly created card can start a WASAPI session during enumeration.
    // Snapshot after discovery so its process ancestry is visible immediately.
    ProcessTreeSnapshot processes;
    const uint32_t engine_pid = GetCurrentProcessId();
    const uint32_t parent = processes.Parent(engine_pid);
    const uint32_t ui_pid = IsFthrAudioUiParent(parent, AnnouncedAudioUiPid(),
        parent ? ProcessImagePath(processes.Inspect(parent).handle) : std::wstring{},
        ProcessImagePath(GetCurrentProcess())) ? parent : 0;
    std::map<std::string, WindowsAudioSessionDescriptor> groups;
    for (const auto& [pid, active] : session_activity) {
        if (pid == engine_pid || IsInFthrAudioTree(pid, ui_pid, processes)) continue;
        const ExecutableMetadata executable =
            ExecutableMetadataForProcess(processes.Inspect(pid).handle);
        if (executable.basename.empty()) continue;
        const uint32_t root_pid = ProcessGroupRoot(pid, executable.basename, ui_pid, processes);
        const std::string group_key = BuildWindowsRuntimeGroupKey(root_pid, executable.basename);
        if (group_key.empty()) continue;

        auto [it, inserted] = groups.try_emplace(group_key);
        auto& descriptor = it->second;
        if (inserted) {
            // Target the entire grouped application so browsers and other
            // applications with sibling audio processes share one stem.
            descriptor.process_id = root_pid;
            descriptor.process_group_root_id = root_pid;
            descriptor.runtime_group_key = group_key;
            descriptor.identity = IdentityForProcess(
                pid, executable.basename, executable.description);
        }
        descriptor.currently_active = descriptor.currently_active || active;
    }

    const auto parent_process = [&](uint32_t pid) { return processes.Parent(pid); };
    if (ui_pid) ExcludeWindowsAudioSessionTrees(groups, {}, parent_process, {ui_pid});

    if (!excluded_identities_.empty()) {
        // Inspect executable names even when the excluded parent has no audio
        // session. A differently named renderer still belongs to its tree.
        if (processes.entries.empty()) {
            // No unfiltered fallback is safe when tree identity is unavailable.
            groups.clear();
        } else {
            std::vector<uint32_t> excluded_processes;
            for (const auto& [pid, entry] : processes.entries) {
                const auto identity = NormalizeWindowsExecutableIdentity(entry.szExeFile);
                if (std::find(excluded_identities_.begin(), excluded_identities_.end(),
                              identity) != excluded_identities_.end()) {
                    excluded_processes.push_back(pid);
                }
            }
            // The user blacklist retains its conservative raw-snapshot
            // policy even if an excluded process denies process-time access.
            ExcludeWindowsAudioSessionTrees(groups, excluded_identities_,
                [&](uint32_t pid) {
                    const auto entry = processes.entries.find(pid);
                    return entry == processes.entries.end()
                        ? 0u : entry->second.th32ParentProcessID;
                }, excluded_processes);
        }
    }
    GroupWindowsAudioSessionTrees(groups, parent_process);

    std::map<std::string, bool> previous;
    for (const auto& session : current_) previous.emplace(session.runtime_group_key, true);
    std::map<std::string, bool> next;
    for (const auto& [key, session] : groups) {
        next.emplace(key, true);
        if (!previous.count(key)) update->appeared_runtime_groups.push_back(key);
    }
    for (const auto& [key, _] : previous) {
        if (!next.count(key)) update->disappeared_runtime_groups.push_back(key);
    }
    update->current.clear();
    update->current.reserve(groups.size());
    for (auto& [_, session] : groups) update->current.push_back(std::move(session));
    current_ = update->current;
    return true;
}

}  // namespace fthr
