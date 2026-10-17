#ifndef NOMINMAX
#define NOMINMAX
#endif

#include "windows_monitor_resolver.h"
#include "windows_native_error.h"

#include <windows.h>
#include <dxgi1_2.h>

#include <algorithm>
#include <cwctype>
#include <sstream>
#include <iostream>
#include <utility>

namespace fthr::monitor {
namespace {

struct MonitorHandleEntry {
    std::wstring gdi_name;
    uintptr_t handle = 0;
    bool primary = false;
    RECT bounds{};
};

struct MonitorHandleCollection {
    std::vector<MonitorHandleEntry> entries;
    DWORD first_error = ERROR_SUCCESS;
    bool failed = false;
};

BOOL CALLBACK CollectMonitorHandle(
    HMONITOR monitor, HDC, LPRECT, LPARAM context_value) {
    auto* collection = reinterpret_cast<MonitorHandleCollection*>(context_value);
    MONITORINFOEXW info{};
    info.cbSize = sizeof(info);
    if (GetMonitorInfoW(monitor, &info)) {
        collection->entries.push_back({
            info.szDevice,
            reinterpret_cast<uintptr_t>(monitor),
            (info.dwFlags & MONITORINFOF_PRIMARY) != 0, info.rcMonitor});
    } else if (!collection->failed) {
        collection->first_error = GetLastError();
        collection->failed = true;
    }
    return TRUE;
}

bool EqualInsensitive(const std::wstring& left, const std::wstring& right) {
    if (left.size() != right.size()) return false;
    return std::equal(left.begin(), left.end(), right.begin(),
        [](wchar_t a, wchar_t b) { return towlower(a) == towlower(b); });
}

AdapterLuid ToAdapterLuid(const LUID& luid) {
    return {luid.LowPart, luid.HighPart};
}

uint64_t HashBytes(uint64_t hash, const void* data, size_t size) {
    constexpr uint64_t kPrime = 1099511628211ULL;
    const auto* bytes = static_cast<const uint8_t*>(data);
    for (size_t index = 0; index < size; ++index) {
        hash ^= bytes[index];
        hash *= kPrime;
    }
    return hash;
}

uint64_t TopologyGeneration(const std::vector<MonitorTopologyEntry>& entries) {
    std::vector<std::wstring> identities;
    identities.reserve(entries.size());
    for (const auto& entry : entries) {
        std::wostringstream value;
        value << NormalizeMonitorDevicePath(entry.monitor_device_path) << L'|'
              << entry.adapter_luid.high_part << L':' << entry.adapter_luid.low_part << L'|'
              << entry.source_id << L':' << entry.target_id << L'|'
              << entry.source_gdi_name << L'|' << entry.hmonitor;
        identities.push_back(value.str());
    }
    std::sort(identities.begin(), identities.end());

    uint64_t hash = 1469598103934665603ULL;
    for (const auto& identity : identities) {
        hash = HashBytes(hash, identity.data(), identity.size() * sizeof(wchar_t));
    }
    return hash;
}

std::string Win32Diagnostic(const char* operation, LONG error) {
    return diagnostics::FormatWin32Failure(
        operation, static_cast<DWORD>(error));
}

} // namespace

const char* ToString(MonitorResolveError error) noexcept {
    switch (error) {
    case MonitorResolveError::None: return "NONE";
    case MonitorResolveError::MonitorNotFound: return "MONITOR_NOT_FOUND";
    case MonitorResolveError::MonitorDisconnected: return "MONITOR_DISCONNECTED";
    case MonitorResolveError::MonitorTopologyChanged: return "MONITOR_TOPOLOGY_CHANGED";
    case MonitorResolveError::OutputResolutionFailed: return "OUTPUT_RESOLUTION_FAILED";
    case MonitorResolveError::CaptureItemCreationFailed: return "CAPTURE_ITEM_CREATION_FAILED";
    }
    return "MONITOR_UNKNOWN_ERROR";
}

std::wstring NormalizeMonitorDevicePath(const std::wstring& device_path) {
    auto first = std::find_if_not(device_path.begin(), device_path.end(), iswspace);
    auto last = std::find_if_not(device_path.rbegin(), device_path.rend(), iswspace).base();
    if (first >= last) return {};

    std::wstring normalized(first, last);
    std::transform(normalized.begin(), normalized.end(), normalized.begin(),
        [](wchar_t value) {
            if (value == L'/') return L'\\';
            return static_cast<wchar_t>(towlower(value));
        });
    return normalized;
}

std::wstring DesktopMonitorIdentity(const MonitorTopologyEntry& monitor) {
    auto name = NormalizeMonitorDevicePath(monitor.source_gdi_name);
    if (name.rfind(L"\\\\.\\", 0) == 0) name.erase(0, 4);
    if (!monitor.hmonitor || name.rfind(L"display", 0) != 0
        || name.size() <= 7
        || name.find_first_not_of(L"0123456789", 7) != std::wstring::npos) return {};
    std::wostringstream identity;
    identity << L"fthr-monitor:" << std::hex << monitor.hmonitor << L':' << name;
    return identity.str();
}

bool MatchesMonitorSelection(const MonitorTopologyEntry& monitor, const std::wstring& selection) {
    const auto normalized = NormalizeMonitorDevicePath(selection);
    return !normalized.empty()
        && (NormalizeMonitorDevicePath(monitor.monitor_device_path) == normalized
            || DesktopMonitorIdentity(monitor) == normalized);
}

TopologyQueryResult BuildLiveMonitorTopology(
    const std::vector<MonitorTopologyEntry>& desktop_monitors,
    const std::vector<MonitorTopologyEntry>& display_paths,
    const std::vector<DxgiOutputIdentity>& outputs) {
    TopologyQueryResult result;
    for (auto entry : desktop_monitors) {
        if (!entry.active || !entry.hmonitor || !entry.width || !entry.height) continue;
        std::vector<const MonitorTopologyEntry*> paths;
        for (const auto& path : display_paths) {
            if (EqualInsensitive(path.source_gdi_name, entry.source_gdi_name)) paths.push_back(&path);
        }
        if (!paths.empty()) {
            const auto& first = *paths.front();
            const bool one_source = std::all_of(paths.begin(), paths.end(), [&](const auto* path) {
                return path->adapter_luid == first.adapter_luid && path->source_id == first.source_id;
            });
            if (!one_source) {
                return {MonitorResolveError::MonitorTopologyChanged, {},
                    "live desktop monitor maps to multiple display sources"};
            }
            entry.adapter_luid = first.adapter_luid;
            entry.adapter_luid_available = true;
            entry.source_id = first.source_id;
            entry.target_id = first.target_id;
            if (paths.size() == 1 && entry.monitor_device_path.empty()) {
                entry.monitor_device_path = first.monitor_device_path;
                entry.friendly_name = first.friendly_name;
            }
        }
        // CCD can disappear while a disabled PnP monitor is still being driven.
        // DXGI is an optional source of adapter identity, not a WGC prerequisite.
        const DxgiOutputIdentity* matched_output = nullptr;
        for (const auto& output : outputs) {
            if (output.hmonitor != entry.hmonitor
                || !EqualInsensitive(output.source_gdi_name, entry.source_gdi_name)) continue;
            if (matched_output && matched_output->adapter_luid != output.adapter_luid) {
                return {MonitorResolveError::MonitorTopologyChanged, {},
                    "live desktop monitor maps to multiple DXGI adapters"};
            }
            matched_output = &output;
        }
        if (matched_output) {
            if (entry.adapter_luid_available && entry.adapter_luid != matched_output->adapter_luid) {
                return {MonitorResolveError::MonitorTopologyChanged, {},
                    "CCD and DXGI disagree about the live monitor adapter"};
            }
            entry.adapter_luid = matched_output->adapter_luid;
            entry.adapter_luid_available = true;
        }
        // Monitor availability is established by the live HMONITOR and bounds,
        // not by PnP ACTIVE/targetAvailable, which can be false in stretched modes.
        if (entry.monitor_device_path.empty()) entry.monitor_device_path = DesktopMonitorIdentity(entry);
        if (!entry.monitor_device_path.empty()) result.monitors.push_back(std::move(entry));
    }
    result.topology_generation = TopologyGeneration(result.monitors);
    for (auto& entry : result.monitors) entry.topology_generation = result.topology_generation;
    return result;
}

TopologyQueryResult WindowsMonitorTopologySource::QueryActiveTopology() {
    MonitorHandleCollection handles;
    if (!EnumDisplayMonitors(nullptr, nullptr, CollectMonitorHandle,
            reinterpret_cast<LPARAM>(&handles)) || handles.failed) {
        return {MonitorResolveError::MonitorTopologyChanged, {},
            Win32Diagnostic("EnumDisplayMonitors/GetMonitorInfoW",
                handles.failed ? handles.first_error : GetLastError())};
    }
    std::vector<MonitorTopologyEntry> desktops;
    for (const auto& handle : handles.entries) {
        MonitorTopologyEntry entry;
        entry.source_gdi_name = handle.gdi_name;
        entry.hmonitor = handle.handle;
        entry.primary = handle.primary;
        entry.active = true;
        entry.adapter_luid_available = false;
        entry.left = handle.bounds.left; entry.top = handle.bounds.top;
        entry.width = handle.bounds.right - handle.bounds.left;
        entry.height = handle.bounds.bottom - handle.bounds.top;
        std::vector<DISPLAY_DEVICEW> children, active_children;
        for (DWORD index = 0;; ++index) {
            DISPLAY_DEVICEW child{}; child.cb = sizeof(child);
            if (!EnumDisplayDevicesW(handle.gdi_name.c_str(), index, &child,
                    EDD_GET_DEVICE_INTERFACE_NAME)) break;
            children.push_back(child);
            if (child.StateFlags & DISPLAY_DEVICE_ACTIVE) active_children.push_back(child);
        }
        const auto& candidates = active_children.empty() ? children : active_children;
        if (candidates.size() == 1) {
            const auto path = NormalizeMonitorDevicePath(candidates[0].DeviceID);
            if (path.rfind(L"\\\\?\\display#", 0) == 0) entry.monitor_device_path = path;
            entry.friendly_name = candidates[0].DeviceString;
        }
        desktops.push_back(std::move(entry));
    }
    UINT32 path_count = 0, mode_count = 0;
    LONG status = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &path_count, &mode_count);
    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    for (int attempt = 0; status == ERROR_SUCCESS && attempt < 4; ++attempt) {
        paths.resize(path_count); modes.resize(mode_count);
        status = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &path_count, paths.data(),
            &mode_count, modes.data(), nullptr);
        if (status != ERROR_INSUFFICIENT_BUFFER) break;
        status = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &path_count, &mode_count);
        if (attempt == 3) status = ERROR_INSUFFICIENT_BUFFER;
    }
    std::vector<MonitorTopologyEntry> display_paths;
    if (status == ERROR_SUCCESS) {
        paths.resize(path_count);
        for (const auto& path : paths) {
            if (!(path.flags & DISPLAYCONFIG_PATH_ACTIVE)) continue;
            DISPLAYCONFIG_SOURCE_DEVICE_NAME source{};
            source.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, sizeof(source),
                path.sourceInfo.adapterId, path.sourceInfo.id};
            if (DisplayConfigGetDeviceInfo(&source.header) != ERROR_SUCCESS) continue;
            MonitorTopologyEntry entry;
            entry.source_gdi_name = source.viewGdiDeviceName;
            entry.adapter_luid = ToAdapterLuid(path.sourceInfo.adapterId);
            entry.source_id = path.sourceInfo.id; entry.target_id = path.targetInfo.id;
            DISPLAYCONFIG_TARGET_DEVICE_NAME target{};
            target.header = {DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME, sizeof(target),
                path.targetInfo.adapterId, path.targetInfo.id};
            if (DisplayConfigGetDeviceInfo(&target.header) == ERROR_SUCCESS) {
                entry.monitor_device_path = NormalizeMonitorDevicePath(target.monitorDevicePath);
                entry.friendly_name = target.monitorFriendlyDeviceName;
            }
            display_paths.push_back(std::move(entry));
        }
    } else {
        std::cerr << "[Monitor] Optional CCD metadata unavailable: "
                  << Win32Diagnostic("QueryDisplayConfig", status) << std::endl;
    }
    std::vector<DxgiOutputIdentity> outputs;
    IDXGIFactory1* factory = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)))) {
        for (UINT index = 0;; ++index) {
            IDXGIAdapter1* adapter = nullptr;
            if (FAILED(factory->EnumAdapters1(index, &adapter))) break;
            DXGI_ADAPTER_DESC1 desc{};
            if (SUCCEEDED(adapter->GetDesc1(&desc))) {
                for (UINT output_index = 0;; ++output_index) {
                    IDXGIOutput* output = nullptr;
                    if (FAILED(adapter->EnumOutputs(output_index, &output))) break;
                    DXGI_OUTPUT_DESC info{};
                    if (SUCCEEDED(output->GetDesc(&info)) && info.AttachedToDesktop) {
                        outputs.push_back({ToAdapterLuid(desc.AdapterLuid),
                            reinterpret_cast<uintptr_t>(info.Monitor), info.DeviceName, output_index});
                    }
                    output->Release();
                }
            }
            adapter->Release();
        }
        factory->Release();
    }
    return BuildLiveMonitorTopology(desktops, display_paths, outputs);
}

bool OpenSelectedDxgiAdapter(IDXGIFactory1* factory, const MonitorTopologyEntry& monitor,
    IDXGIAdapter1** adapter, std::string& diagnostic) {
    if (!factory || !adapter || !monitor.adapter_luid_available) {
        diagnostic = "selected live monitor has no unambiguous adapter LUID";
        return false;
    }
    *adapter = nullptr;
    for (UINT index = 0;; ++index) {
        IDXGIAdapter1* candidate = nullptr;
        if (FAILED(factory->EnumAdapters1(index, &candidate))) break;
        DXGI_ADAPTER_DESC1 desc{};
        if (SUCCEEDED(candidate->GetDesc1(&desc))
            && ToAdapterLuid(desc.AdapterLuid) == monitor.adapter_luid) {
            *adapter = candidate;
            return true;
        }
        candidate->Release();
    }
    diagnostic = "selected live monitor adapter LUID is not present in DXGI";
    return false;
}

MonitorResolveResult MonitorResolver::Resolve(
    const std::wstring& monitor_device_path) {
    const std::wstring selected = NormalizeMonitorDevicePath(monitor_device_path);
    if (selected.empty()) {
        return {MonitorResolveError::MonitorNotFound, {},
                "persistent monitor device path is empty"};
    }

    auto topology = source_.QueryActiveTopology();
    if (!topology.ok()) {
        return {topology.error, {}, topology.diagnostic};
    }

    const MonitorTopologyEntry* match = nullptr;
    for (const auto& entry : topology.monitors) {
        if (!MatchesMonitorSelection(entry, selected)) continue;
        if (match) {
            return {MonitorResolveError::MonitorTopologyChanged, {},
                    "monitor device path appears more than once in active topology"};
        }
        match = &entry;
    }
    if (!match) {
        return {MonitorResolveError::MonitorNotFound, {},
                "selected monitor device path is not in the active topology"};
    }
    if (!match->active || match->hmonitor == 0) {
        return {MonitorResolveError::MonitorDisconnected, {},
                "selected monitor has no active HMONITOR"};
    }
    auto resolved = *match;
    // Retain the selection key across metadata becoming available/unavailable.
    resolved.monitor_device_path = selected;
    return {MonitorResolveError::None, resolved, {}};
}

bool MonitorResolver::IsCurrent(
    const MonitorTopologyEntry& previously_resolved) {
    const auto current = Resolve(previously_resolved.monitor_device_path);
    return current.ok() && current.monitor == previously_resolved;
}

DxgiOutputResolveResult ResolveDxgiOutput(
    const MonitorTopologyEntry& selected_monitor,
    const std::vector<DxgiOutputIdentity>& outputs) {
    const DxgiOutputIdentity* match = nullptr;
    for (const auto& output : outputs) {
        if (output.adapter_luid != selected_monitor.adapter_luid) continue;
        const bool exact_handle = selected_monitor.hmonitor != 0
            && output.hmonitor == selected_monitor.hmonitor;
        const bool exact_name = !selected_monitor.source_gdi_name.empty()
            && EqualInsensitive(output.source_gdi_name,
                                selected_monitor.source_gdi_name);
        if (!exact_handle || !exact_name) continue;
        if (match && match->output_index != output.output_index) {
            return {MonitorResolveError::OutputResolutionFailed, UINT32_MAX,
                    "selected monitor matched multiple DXGI outputs"};
        }
        match = &output;
    }
    if (!match) {
        return {MonitorResolveError::OutputResolutionFailed, UINT32_MAX,
                "selected monitor has no matching output on its adapter"};
    }
    return {MonitorResolveError::None, match->output_index, {}};
}

bool OpenSelectedDxgiOutput(
    IDXGIFactory1* factory,
    const MonitorTopologyEntry& selected_monitor,
    IDXGIAdapter1** adapter,
    IDXGIOutput** output,
    DxgiOutputIdentity* output_identity,
    std::string& diagnostic) {
    if (!factory || !adapter || !output || !selected_monitor.adapter_luid_available) {
        diagnostic = "invalid DXGI output resolver arguments";
        return false;
    }
    *adapter = nullptr;
    *output = nullptr;
    if (output_identity) *output_identity = {};

    for (UINT adapter_index = 0;; ++adapter_index) {
        IDXGIAdapter1* candidate_adapter = nullptr;
        const HRESULT adapter_status = factory->EnumAdapters1(
            adapter_index, &candidate_adapter);
        if (adapter_status == DXGI_ERROR_NOT_FOUND) break;
        if (FAILED(adapter_status) || !candidate_adapter) {
            diagnostic = diagnostics::FormatHResultFailure(
                "IDXGIFactory1::EnumAdapters1", adapter_status);
            return false;
        }
        DXGI_ADAPTER_DESC1 adapter_desc{};
        const HRESULT adapter_desc_status = candidate_adapter->GetDesc1(
            &adapter_desc);
        if (FAILED(adapter_desc_status)) {
            diagnostic = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::GetDesc1", adapter_desc_status);
            candidate_adapter->Release();
            return false;
        }
        if (ToAdapterLuid(adapter_desc.AdapterLuid)
                != selected_monitor.adapter_luid) {
            candidate_adapter->Release();
            continue;
        }

        std::vector<DxgiOutputIdentity> identities;
        for (UINT output_index = 0;; ++output_index) {
            IDXGIOutput* candidate_output = nullptr;
            const HRESULT output_status = candidate_adapter->EnumOutputs(
                output_index, &candidate_output);
            if (output_status == DXGI_ERROR_NOT_FOUND) break;
            if (FAILED(output_status) || !candidate_output) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIAdapter1::EnumOutputs", output_status);
                candidate_adapter->Release();
                return false;
            }
            DXGI_OUTPUT_DESC output_desc{};
            const HRESULT output_desc_status = candidate_output->GetDesc(
                &output_desc);
            if (FAILED(output_desc_status)) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIOutput::GetDesc", output_desc_status);
                candidate_output->Release();
                candidate_adapter->Release();
                return false;
            }
            identities.push_back({
                selected_monitor.adapter_luid,
                reinterpret_cast<uintptr_t>(output_desc.Monitor),
                output_desc.DeviceName,
                output_index});
            candidate_output->Release();
        }

        const auto resolved = ResolveDxgiOutput(selected_monitor, identities);
        if (!resolved.ok()) {
            diagnostic = resolved.diagnostic;
            candidate_adapter->Release();
            return false;
        }
        const HRESULT reopen_status = candidate_adapter->EnumOutputs(
            resolved.output_index, output);
        if (FAILED(reopen_status) || !*output) {
            diagnostic = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::EnumOutputs(reopen selected output)",
                reopen_status);
            candidate_adapter->Release();
            return false;
        }
        if (output_identity) {
            const auto match = std::find_if(
                identities.begin(), identities.end(),
                [&resolved](const DxgiOutputIdentity& identity) {
                    return identity.output_index == resolved.output_index;
                });
            if (match != identities.end()) *output_identity = *match;
        }
        *adapter = candidate_adapter;
        return true;
    }

    diagnostic = "selected monitor adapter LUID is not present in DXGI";
    return false;
}

} // namespace fthr::monitor
