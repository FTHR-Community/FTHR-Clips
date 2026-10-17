// Stable Windows monitor identity and current-topology resolution (AUDIT-048).

#pragma once
#ifndef FTHR_WINDOWS_MONITOR_RESOLVER_H
#define FTHR_WINDOWS_MONITOR_RESOLVER_H

#include <cstdint>
#include <string>
#include <vector>

struct IDXGIFactory1;
struct IDXGIAdapter1;
struct IDXGIOutput;

namespace fthr::monitor {

struct AdapterLuid {
    uint32_t low_part = 0;
    int32_t high_part = 0;

    friend bool operator==(const AdapterLuid& left, const AdapterLuid& right) {
        return left.low_part == right.low_part && left.high_part == right.high_part;
    }
    friend bool operator!=(const AdapterLuid& left, const AdapterLuid& right) {
        return !(left == right);
    }
};

enum class MonitorResolveError {
    None,
    MonitorNotFound,
    MonitorDisconnected,
    MonitorTopologyChanged,
    OutputResolutionFailed,
    CaptureItemCreationFailed,
};

const char* ToString(MonitorResolveError error) noexcept;
std::wstring NormalizeMonitorDevicePath(const std::wstring& device_path);

// Persistent identity is monitor_device_path. Every other field is valid only
// for the topology snapshot that produced it and must be re-resolved after a
// backend/topology change.
struct MonitorTopologyEntry {
    std::wstring monitor_device_path;
    AdapterLuid adapter_luid;
    uint32_t source_id = 0;
    uint32_t target_id = 0;
    uintptr_t hmonitor = 0;
    std::wstring source_gdi_name;
    std::wstring friendly_name;
    bool primary = false;
    bool active = false;
    uint64_t topology_generation = 0;
    bool adapter_luid_available = true;
    int32_t left = 0, top = 0;
    uint32_t width = 0, height = 0;

    friend bool operator==(
        const MonitorTopologyEntry& left,
        const MonitorTopologyEntry& right) {
        return NormalizeMonitorDevicePath(left.monitor_device_path) ==
                   NormalizeMonitorDevicePath(right.monitor_device_path)
            && left.adapter_luid == right.adapter_luid
            && left.source_id == right.source_id
            && left.target_id == right.target_id
            && left.hmonitor == right.hmonitor
            && left.source_gdi_name == right.source_gdi_name
            && left.adapter_luid_available == right.adapter_luid_available;
    }
};

struct TopologyQueryResult {
    MonitorResolveError error = MonitorResolveError::None;
    std::vector<MonitorTopologyEntry> monitors;
    std::string diagnostic;
    uint64_t topology_generation = 0;

    bool ok() const noexcept { return error == MonitorResolveError::None; }
};

class IMonitorTopologySource {
public:
    virtual ~IMonitorTopologySource() = default;
    virtual TopologyQueryResult QueryActiveTopology() = 0;
};

class WindowsMonitorTopologySource final : public IMonitorTopologySource {
public:
    TopologyQueryResult QueryActiveTopology() override;
};

struct MonitorResolveResult {
    MonitorResolveError error = MonitorResolveError::None;
    MonitorTopologyEntry monitor;
    std::string diagnostic;

    bool ok() const noexcept { return error == MonitorResolveError::None; }
};

class MonitorResolver {
public:
    explicit MonitorResolver(IMonitorTopologySource& source) : source_(source) {}

    MonitorResolveResult Resolve(const std::wstring& monitor_device_path);
    bool IsCurrent(const MonitorTopologyEntry& previously_resolved);

private:
    IMonitorTopologySource& source_;
};

struct DxgiOutputIdentity {
    AdapterLuid adapter_luid;
    uintptr_t hmonitor = 0;
    std::wstring source_gdi_name;
    uint32_t output_index = UINT32_MAX;
};

// Explicit, transient selection for a live desktop output whose PnP identity
// is unavailable. Never use a bare display index/name as a native argument.
std::wstring DesktopMonitorIdentity(const MonitorTopologyEntry& monitor);
bool MatchesMonitorSelection(const MonitorTopologyEntry&, const std::wstring&);
TopologyQueryResult BuildLiveMonitorTopology(
    const std::vector<MonitorTopologyEntry>& desktop_monitors,
    const std::vector<MonitorTopologyEntry>& display_paths,
    const std::vector<DxgiOutputIdentity>& outputs);
bool OpenSelectedDxgiAdapter(
    IDXGIFactory1*, const MonitorTopologyEntry&, IDXGIAdapter1**, std::string&);

struct DxgiOutputResolveResult {
    MonitorResolveError error = MonitorResolveError::None;
    uint32_t output_index = UINT32_MAX;
    std::string diagnostic;

    bool ok() const noexcept { return error == MonitorResolveError::None; }
};

DxgiOutputResolveResult ResolveDxgiOutput(
    const MonitorTopologyEntry& selected_monitor,
    const std::vector<DxgiOutputIdentity>& outputs);

// Returns owning COM references in adapter/output on success. The caller must
// Release both. No adapter/output positional fallback is performed.
bool OpenSelectedDxgiOutput(
    IDXGIFactory1* factory,
    const MonitorTopologyEntry& selected_monitor,
    IDXGIAdapter1** adapter,
    IDXGIOutput** output,
    DxgiOutputIdentity* output_identity,
    std::string& diagnostic);

} // namespace fthr::monitor

#endif // FTHR_WINDOWS_MONITOR_RESOLVER_H
