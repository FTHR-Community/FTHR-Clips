#include "windows_monitor_resolver.h"
#include "windows_native_error.h"
#include "windows_dxgi_recovery.h"
#include "encoded_ring_buffer.h"
#include "encoded_video_config.h"
#include "encoded_video_config_ffmpeg.h"
#include "nvenc_codec_config.h"
#include "replay_encoder.h"
#include "wgc_frame_lease.h"
#include "capture_focus_policy.h"
#include "replay_pause_clock.h"
#include <thread>
#include <chrono>

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <iterator>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

int RunAmfReplayEncoderTests();
int RunSoftwareReplayEncoderTests();
int RunQsvReplayEncoderTests();
int RunWindowsReplayPolicyTests();
int RunAudioSourceModelTests();
int RunWindowsAudioSessionRegistryTests();
int RunWindowsProcessLoopbackAudioProviderTests();
int RunWindowsMicrophoneAudioProviderTests();
int RunAudioFormatConverterTests();
int RunWindowsCaptureBorderPolicyTests();
int RunWindowsCaptureBackendPolicyTests();
int RunContinuousRecordingWriterTests();
int RunNvencInputLifecycleTests();
int RunCaptureScaleGeometryTests();

void WgcFrameLeaseReturnsEveryPoolSlot() {
    struct Frame {
        int& closes;
        void Close() { ++closes; }
    };
    int closes = 0;
    // Exercise normal processing, rate/focus skips, exceptions and shutdown.
    for (int index = 0; index < 100; ++index) {
        try {
            Frame frame{closes};
            fthr::wgc::FrameLease lease(frame);
            if (index % 3 == 0) continue;
            if (index % 3 == 1) throw 1;
        } catch (int) {}
    }
    if (closes != 100) std::abort();
    {
        Frame frame{closes};
        fthr::wgc::FrameLease lease(frame);
    }
    if (closes != 101) std::abort();
}

namespace {

using fthr::monitor::AdapterLuid;
using fthr::monitor::DxgiOutputIdentity;
using fthr::monitor::MonitorResolveError;
using fthr::monitor::MonitorResolver;
using fthr::monitor::MonitorTopologyEntry;
using fthr::EncodedPacketFormat;
using fthr::EncodedRingBuffer;
using fthr::EncodedVideoConfig;
using fthr::VideoCodec;

bool GuidEquals(const GUID& left, const GUID& right) {
    return std::memcmp(&left, &right, sizeof(GUID)) == 0;
}

int checks = 0;

void Check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

MonitorTopologyEntry Monitor(
    const wchar_t* path,
    AdapterLuid luid,
    uint32_t source_id,
    uint32_t target_id,
    uintptr_t hmonitor,
    const wchar_t* gdi_name,
    bool primary = false) {
    MonitorTopologyEntry entry;
    entry.monitor_device_path = path;
    entry.adapter_luid = luid;
    entry.source_id = source_id;
    entry.target_id = target_id;
    entry.hmonitor = hmonitor;
    entry.source_gdi_name = gdi_name;
    entry.primary = primary;
    entry.active = true;
    return entry;
}

class FakeTopologySource final : public fthr::monitor::IMonitorTopologySource {
public:
    explicit FakeTopologySource(std::vector<MonitorTopologyEntry> entries)
        : entries_(std::move(entries)) {}

    fthr::monitor::TopologyQueryResult QueryActiveTopology() override {
        return {MonitorResolveError::None, entries_, {}};
    }

    void Set(std::vector<MonitorTopologyEntry> entries) {
        entries_ = std::move(entries);
    }

private:
    std::vector<MonitorTopologyEntry> entries_;
};

void LiveWindowsDesktopAdapterSmoke() {
    fthr::monitor::WindowsMonitorTopologySource source;
    const auto topology = source.QueryActiveTopology();
    Check(topology.ok(), "real Windows desktop topology query");
    IDXGIFactory1* factory = nullptr;
    Check(SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory))), "real DXGI factory");
    for (const auto& entry : topology.monitors) {
        Check(entry.active && entry.hmonitor && entry.width && entry.height, "real live desktop bounds");
        MonitorResolver resolver(source);
        Check(resolver.Resolve(entry.monitor_device_path).ok(), "real persistent monitor resolution");
        Check(resolver.Resolve(fthr::monitor::DesktopMonitorIdentity(entry)).ok(), "real fallback alias resolution");
        IDXGIAdapter1* adapter = nullptr; std::string detail;
        Check(fthr::monitor::OpenSelectedDxgiAdapter(factory, entry, &adapter, detail), "real adapter lookup without opening an output");
        if (adapter) adapter->Release();
    }
    factory->Release();
    std::cout << "Live Windows desktop adapter smoke: " << topology.monitors.size() << " monitor(s)" << std::endl;
}

void LiveDesktopSurvivesMissingPnpMetadata() {
    auto live = Monitor(L"", {}, 0, 0, 0x123, L"\\\\.\\DISPLAY2", true);
    live.width = 1280; live.height = 1024; live.adapter_luid_available = false;
    auto source_path = Monitor(L"", {7, 0}, 2, 3, 0, live.source_gdi_name.c_str());
    source_path.active = false; // targetAvailable/PnP state is not live-desktop availability.
    auto result = fthr::monitor::BuildLiveMonitorTopology({live}, {source_path}, {});
    Check(result.ok() && result.monitors.size() == 1, "live desktop survives disabled PnP");
    Check(result.monitors[0].adapter_luid == AdapterLuid{7, 0}, "CCD source supplies adapter without DXGI output");
    Check(result.monitors[0].monitor_device_path == L"fthr-monitor:123:display2", "explicit handle-qualified fallback identity");
    FakeTopologySource topology(result.monitors);
    MonitorResolver resolver(topology);
    Check(resolver.Resolve(L"fthr-monitor:123:display2").ok(), "fallback selection resolves");
    auto enriched = result.monitors[0]; enriched.monitor_device_path = L"PATH-NEW";
    topology.Set({enriched});
    Check(resolver.Resolve(L"fthr-monitor:123:display2").ok(), "fallback survives restored PnP metadata");
    enriched.hmonitor = 0x124; topology.Set({enriched});
    Check(!resolver.Resolve(L"fthr-monitor:123:display2").ok(), "stale handle requires reselection");
    const DxgiOutputIdentity output{{8, 0}, live.hmonitor, live.source_gdi_name, 1};
    result = fthr::monitor::BuildLiveMonitorTopology({live}, {}, {output});
    Check(result.ok() && result.monitors[0].adapter_luid == AdapterLuid{8, 0}, "DXGI adapter metadata fallback");
    Check(!fthr::monitor::BuildLiveMonitorTopology({live}, {source_path}, {output}).ok(), "conflicting adapters rejected");
    auto other = source_path; other.source_id++;
    Check(!fthr::monitor::BuildLiveMonitorTopology({live}, {source_path, other}, {}).ok(), "ambiguous source rejected");
    Check(fthr::monitor::BuildLiveMonitorTopology({}, {source_path}, {output}).monitors.empty(), "disconnected targets do not create desktop choices");
    auto selected = result.monitors[0];
    auto stale = output; stale.hmonitor++;
    Check(!fthr::monitor::ResolveDxgiOutput(selected, {stale}).ok(), "DXGI requires matching handle and GDI name");
}

void OneMonitorOneAdapter() {
    FakeTopologySource source({Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true)});
    auto result = MonitorResolver(source).Resolve(L"path-a");
    Check(result.ok(), "one monitor resolves");
    Check(result.monitor.adapter_luid == AdapterLuid{1, 0}, "one adapter identity preserved");
}

void TwoMonitorsSameAdapter() {
    FakeTopologySource source({
        Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true),
        Monitor(L"PATH-B", {1, 0}, 1, 11, 200, L"DISPLAY2")});
    auto result = MonitorResolver(source).Resolve(L"path-b");
    Check(result.ok() && result.monitor.target_id == 11, "same-adapter target identity selected");
}

void SecondaryMonitorSelected() {
    FakeTopologySource source({
        Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true),
        Monitor(L"PATH-B", {1, 0}, 1, 11, 200, L"DISPLAY2")});
    auto result = MonitorResolver(source).Resolve(L"PATH-B");
    Check(result.ok() && result.monitor.hmonitor == 200, "secondary HMONITOR selected");
}

void TwoAdaptersOneMonitorEach() {
    FakeTopologySource source({
        Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true),
        Monitor(L"PATH-B", {2, -1}, 0, 20, 200, L"DISPLAY2")});
    auto result = MonitorResolver(source).Resolve(L"PATH-B");
    Check(result.ok() && result.monitor.adapter_luid == AdapterLuid{2, -1},
          "selected monitor owns second adapter");
}

void EnumerationOrderChanges() {
    const auto a = Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true);
    const auto b = Monitor(L"PATH-B", {2, 0}, 0, 20, 200, L"DISPLAY2");
    FakeTopologySource source({a, b});
    const auto first = MonitorResolver(source).Resolve(L"PATH-B");
    source.Set({b, a});
    const auto second = MonitorResolver(source).Resolve(L"PATH-B");
    Check(first.ok() && second.ok() && first.monitor == second.monitor,
          "enumeration order does not change identity");
}

void PrimaryMonitorChanges() {
    auto a = Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true);
    auto b = Monitor(L"PATH-B", {1, 0}, 1, 11, 200, L"DISPLAY2", false);
    FakeTopologySource source({a, b});
    const auto first = MonitorResolver(source).Resolve(L"PATH-B");
    a.primary = false;
    b.primary = true;
    source.Set({a, b});
    const auto second = MonitorResolver(source).Resolve(L"PATH-B");
    Check(first.ok() && second.ok() && second.monitor.hmonitor == 200,
          "primary change does not redirect selection");
}

void SelectedMonitorMissing() {
    FakeTopologySource source({Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true)});
    const auto result = MonitorResolver(source).Resolve(L"PATH-B");
    Check(result.error == MonitorResolveError::MonitorNotFound,
          "missing monitor returns typed error");
}

void DevicePathNormalization() {
    Check(fthr::monitor::NormalizeMonitorDevicePath(L"  \\\\?\\DISPLAY/ABC  ") ==
              L"\\\\?\\display\\abc",
          "device path normalized case/slashes/outer whitespace");
}

void MatchingLuidMultipleOutputs() {
    const auto selected = Monitor(L"PATH-B", {7, 0}, 1, 11, 200, L"DISPLAY2");
    const std::vector<DxgiOutputIdentity> outputs{
        {{7, 0}, 100, L"DISPLAY1", 0},
        {{7, 0}, 200, L"DISPLAY2", 1}};
    const auto result = fthr::monitor::ResolveDxgiOutput(selected, outputs);
    Check(result.ok() && result.output_index == 1, "matching LUID still resolves exact output");
}

void OutputRemovedAfterResolution() {
    const auto selected = Monitor(L"PATH-B", {7, 0}, 1, 11, 200, L"DISPLAY2");
    const std::vector<DxgiOutputIdentity> outputs{{{7, 0}, 100, L"DISPLAY1", 0}};
    const auto result = fthr::monitor::ResolveDxgiOutput(selected, outputs);
    Check(result.error == MonitorResolveError::OutputResolutionFailed,
          "removed output fails instead of redirecting");
}

void StaleTransientMappingRejected() {
    const auto old_entry = Monitor(L"PATH-B", {7, 0}, 1, 11, 200, L"DISPLAY2");
    const auto changed_entry = Monitor(L"PATH-B", {8, 0}, 0, 21, 300, L"DISPLAY3");
    FakeTopologySource source({changed_entry});
    Check(!MonitorResolver(source).IsCurrent(old_entry), "stale transient mapping rejected");
}

void NoFallbackToPrimary() {
    FakeTopologySource source({Monitor(L"PATH-A", {1, 0}, 0, 10, 100, L"DISPLAY1", true)});
    const auto result = MonitorResolver(source).Resolve(L"MISSING");
    Check(!result.ok() && result.monitor.hmonitor == 0,
          "resolver never returns primary for missing selection");
}

void NoFallbackToOutputZero() {
    const auto selected = Monitor(L"PATH-B", {7, 0}, 1, 11, 200, L"DISPLAY2");
    const std::vector<DxgiOutputIdentity> outputs{{{7, 0}, 100, L"DISPLAY1", 0}};
    const auto result = fthr::monitor::ResolveDxgiOutput(selected, outputs);
    Check(!result.ok() && result.output_index == UINT32_MAX,
          "resolver never returns output zero for missing selected output");
}

void NativeError4551PreservesWin32AndHresultIdentity() {
    const std::string win32 = fthr::diagnostics::FormatWin32Failure(
        "LoadLibraryA(nvEncodeAPI64.dll)", 4551);
    Check(win32.find("LoadLibraryA(nvEncodeAPI64.dll)") != std::string::npos,
          "Win32 diagnostic preserves the exact API call");
    Check(win32.find("\"win32_error_decimal\":4551") != std::string::npos,
          "Win32 diagnostic preserves decimal error 4551");
    Check(win32.find("\"native_error_hex\":\"0x000011C7\"")
              != std::string::npos,
          "Win32 diagnostic preserves hexadecimal error 4551");
    Check(win32.find("ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION")
              != std::string::npos,
          "Windows SDK symbolic name for 4551 is preserved");

    const HRESULT result = HRESULT_FROM_WIN32(4551);
    const std::string hresult = fthr::diagnostics::FormatHResultFailure(
        "IGraphicsCaptureItemInterop::CreateForMonitor", result);
    Check(hresult.find("IGraphicsCaptureItemInterop::CreateForMonitor")
              != std::string::npos,
          "HRESULT diagnostic preserves the exact API call");
    Check(hresult.find("\"win32_error_decimal\":4551") != std::string::npos,
          "HRESULT_FROM_WIN32 diagnostic preserves embedded Win32 4551");
    Check(hresult.find("\"native_error_hex\":\"0x800711C7\"")
              != std::string::npos,
          "HRESULT diagnostic preserves the full native HRESULT");

    const std::string runtime_json = fthr::diagnostics::HResultFailureJson(
        "IDXGIOutputDuplication::AcquireNextFrame", DXGI_ERROR_ACCESS_LOST);
    Check(!runtime_json.empty() && runtime_json.front() == '{'
              && runtime_json.back() == '}',
          "runtime HRESULT diagnostic is an embeddable JSON object");
    Check(runtime_json.find("IDXGIOutputDuplication::AcquireNextFrame")
              != std::string::npos,
          "DXGI runtime diagnostic preserves the exact failing API call");
    Check(runtime_json.find("\"native_error_hex\":\"0x887A0026\"")
              != std::string::npos,
          "DXGI runtime diagnostic preserves DXGI_ERROR_ACCESS_LOST");
    const std::string release_json = fthr::diagnostics::HResultFailureJson(
        "IDXGIOutputDuplication::ReleaseFrame", DXGI_ERROR_ACCESS_LOST);
    Check(release_json.find("IDXGIOutputDuplication::ReleaseFrame")
              != std::string::npos,
          "ReleaseFrame access loss preserves its exact failing API call");
}

struct FakeDxgiResource {
    int* releases = nullptr;

    unsigned long Release() {
        ++*releases;
        return 0;
    }
};

struct FakeDxgiDuplication {
    int* releases = nullptr;
    HRESULT result = S_OK;

    HRESULT ReleaseFrame() {
        ++*releases;
        return result;
    }
};

void DxgiFrameLeaseReleasesExactlyOnce() {
    int resource_releases = 0;
    int frame_releases = 0;
    FakeDxgiResource resource{&resource_releases};
    FakeDxgiDuplication duplication{&frame_releases};

    {
        fthr::dxgi::FrameLease<FakeDxgiResource, FakeDxgiDuplication> lease(
            &resource, &duplication);
        Check(lease.owns_frame(), "successful acquire owns one DXGI frame");
        lease.ReleaseResource();
        lease.ReleaseResource();
        Check(resource_releases == 1,
              "DXGI frame resource is released exactly once");
        Check(lease.ReleaseFrame() == S_OK,
              "owned DXGI frame can be released explicitly");
        lease.ReleaseFrame();
    }

    Check(resource_releases == 1,
          "frame lease destructor does not double release resource");
    Check(frame_releases == 1,
          "frame lease destructor does not double ReleaseFrame");

    int lost_releases = 0;
    FakeDxgiDuplication lost{&lost_releases, DXGI_ERROR_ACCESS_LOST};
    {
        fthr::dxgi::FrameLease<FakeDxgiResource, FakeDxgiDuplication> lease(
            nullptr, &lost);
        Check(lease.ReleaseFrame() == DXGI_ERROR_ACCESS_LOST,
              "ReleaseFrame access loss is returned to the recovery owner");
    }
    Check(lost_releases == 1,
          "failed ReleaseFrame remains an exact-once ownership transition");
}

void RecentDxgiEvidencePublishesCompleteBoundedEvents() {
    fthr::dxgi::RecentDxgiEvidence<> evidence;
    fthr::dxgi::RecentDxgiEvent event;
    event.acquire_start_qpc = 100;
    event.acquire_end_qpc = 125;
    event.acquire_hresult = S_OK;
    event.acquired = true;
    event.pacing_accepted = true;
    event.release_attempted = true;
    event.release_hresult = S_OK;
    event.resource_token = 7;
    event.generation = 3;
    evidence.Record(event);

    const auto snapshot = evidence.Snapshot();
    Check(snapshot.size() == 1
              && snapshot.front().acquired
              && snapshot.front().release_attempted
              && snapshot.front().pacing_accepted
              && !snapshot.front().pacing_rejected
              && snapshot.front().release_hresult == S_OK
              && snapshot.front().resource_token == 7,
          "one successful DXGI event retains acquire and release evidence");

    for (uint32_t index = 0; index < 40; ++index) {
        event.generation = index;
        evidence.Record(event);
    }
    const auto bounded = evidence.Snapshot();
    Check(bounded.size() == 32 && bounded.front().generation == 8
              && bounded.back().generation == 39,
          "DXGI evidence history remains bounded and chronologically ordered");
}

void DxgiFrameMetadataAndTextureContractsRejectFalseFrames() {
    Check(!fthr::dxgi::HasNewDesktopImage(0),
          "pointer-only update is not a newly presented desktop image");
    Check(fthr::dxgi::HasNewDesktopImage(42),
          "positive presentation QPC identifies a new desktop image");

    const fthr::dxgi::TextureContract expected{1920, 1080, 87, 1};
    Check(fthr::dxgi::IsCompatibleTexture(
              expected, {1920, 1080, 87, 1}),
          "matching dimensions, format and sample count are accepted");
    Check(!fthr::dxgi::IsCompatibleTexture(
              expected, {2560, 1440, 87, 1}),
          "unexpected capture dimensions are rejected");
    Check(!fthr::dxgi::IsCompatibleTexture(
              expected, {1920, 1080, 28, 1}),
          "unexpected capture pixel format is rejected");
    Check(!fthr::dxgi::IsCompatibleTexture(
              expected, {1920, 1080, 87, 4}),
          "multisampled capture input is rejected");
    Check(fthr::dxgi::IsValidBgraRowPitch(1920, 8192),
          "padded BGRA row pitch is accepted");
    Check(!fthr::dxgi::IsValidBgraRowPitch(1920, 4096),
          "undersized BGRA row pitch is rejected");

    using Boundary = fthr::dxgi::PipelineStallBoundary;
    Check(fthr::dxgi::ClassifyPipelineStall(true, false, false)
              == Boundary::CaptureNoFrames,
          "capture with no source frames is classified at acquisition");
    Check(fthr::dxgi::ClassifyPipelineStall(true, true, false)
              == Boundary::CaptureFrameStalled,
          "capture progress that later stops is a capture-frame stall");
    Check(fthr::dxgi::ClassifyPipelineStall(false, true, false)
              == Boundary::EncoderSubmitFailed,
          "no recent encoder submission is an encoder-submit failure");
    Check(fthr::dxgi::ClassifyPipelineStall(false, true, true)
              == Boundary::EncoderOutputStalled,
          "recent submissions without packets are encoder-output stalls");
    Check(std::string(fthr::dxgi::PipelineStallCode(
              Boundary::EncoderOutputStalled)) == "ENCODER_OUTPUT_STALLED",
          "encoder-output stall keeps its stable diagnostic taxonomy");
    Check(fthr::dxgi::IsDxgiAliveButDesktopStatic(
              true, true, true, false, true),
          "recent S_OK pointer-only activity keeps a static DXGI desktop alive");
    Check(fthr::dxgi::IsDxgiAliveButDesktopStatic(
              true, true, true, true, false),
          "recent DXGI WAIT_TIMEOUT keeps a static desktop alive");
    Check(!fthr::dxgi::IsDxgiAliveButDesktopStatic(
              true, true, true, false, false),
          "S_OK without recent pointer-only activity is not exempted");
    Check(!fthr::dxgi::IsDxgiAliveButDesktopStatic(
              true, true, false, true, true),
          "pending encoder work prevents the static DXGI exemption");
    Check(!fthr::dxgi::IsFatalDuplicationRecreateFailure(E_ACCESSDENIED),
          "secure-desktop access denial remains bounded-retryable");
    Check(fthr::dxgi::IsFatalDuplicationRecreateFailure(
              DXGI_ERROR_DEVICE_REMOVED),
          "removed D3D11 device cannot reuse the live encoder generation");
}

fthr::dxgi::RecoveryIdentity RecoveryIdentity(
    const wchar_t* path, AdapterLuid luid,
    uint32_t width = 1920, uint32_t height = 1080) {
    return {path, luid, width, height};
}

void DxgiRecoveryEligibilityRequiresSameCaptureContract() {
    const auto active = RecoveryIdentity(L"PATH-A", {7, 0});
    fthr::dxgi::RecoveryObservation observation;
    observation.running = true;
    observation.monitor_resolved = true;
    observation.current = RecoveryIdentity(L"path-a", {7, 0});
    observation.device_removed_reason = S_OK;

    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::RecreateDuplication,
          "same monitor, adapter, dimensions and device permit local recovery");

    observation.running = false;
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::StopRequested,
          "shutdown cancels DXGI recovery");
    observation.running = true;

    observation.monitor_resolved = false;
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::MonitorUnavailable,
          "missing selected monitor fails recovery without redirecting output");
    observation.monitor_resolved = true;

    observation.current = RecoveryIdentity(L"PATH-B", {7, 0});
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::MonitorIdentityChanged,
          "recovery never migrates to a different persistent monitor");

    observation.current = RecoveryIdentity(L"PATH-A", {8, 0});
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::AdapterChanged,
          "recovery rejects a selected monitor that moved to another adapter");

    observation.current = RecoveryIdentity(L"PATH-A", {7, 0}, 2560, 1440);
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::DimensionsChanged,
          "recovery rejects dimensions incompatible with the live encoder");
    Check(fthr::dxgi::RecoveryAttemptForDecision(
              fthr::dxgi::RecoveryDecision::DimensionsChanged)
              == fthr::dxgi::RecoveryAttemptResult::RetryableFailure,
          "a transient post-ACCESS_LOST resize remains retryable");

    observation.current = active;
    observation.device_removed_reason = DXGI_ERROR_DEVICE_REMOVED;
    Check(fthr::dxgi::EvaluateRecovery(active, observation)
              == fthr::dxgi::RecoveryDecision::DeviceRemoved,
          "device removal cannot reuse the live encoder device");
    Check(fthr::dxgi::RecoveryAttemptForDecision(
              fthr::dxgi::RecoveryDecision::DeviceRemoved)
              == fthr::dxgi::RecoveryAttemptResult::FatalFailure,
          "device removal remains a fatal recovery boundary");
}

void DxgiRecoveryRunnerIsBoundedAndInterruptible() {
    std::vector<uint32_t> delays;
    int attempts = 0;
    const auto recovered = fthr::dxgi::RunBoundedRecovery(
        [] { return true; },
        [&delays](uint32_t delay_ms) {
            delays.push_back(delay_ms);
            return true;
        },
        [&attempts](uint32_t) {
            ++attempts;
            return attempts == 3
                ? fthr::dxgi::RecoveryAttemptResult::Recovered
                : fthr::dxgi::RecoveryAttemptResult::RetryableFailure;
        });
    Check(recovered.outcome == fthr::dxgi::RecoveryOutcome::Recovered
              && recovered.attempts == 3,
          "controlled ACCESS_LOST injection recovers on the third attempt");
    Check(delays == std::vector<uint32_t>({0, 100, 250}),
          "DXGI recovery uses the reviewed bounded backoff sequence");

    const auto exhausted = fthr::dxgi::RunBoundedRecovery(
        [] { return true; },
        [](uint32_t) { return true; },
        [](uint32_t) {
            return fthr::dxgi::RecoveryAttemptResult::RetryableFailure;
        });
    Check(exhausted.outcome == fthr::dxgi::RecoveryOutcome::AttemptsExhausted
              && exhausted.attempts == fthr::dxgi::kMaxRecoveryAttempts,
          "persistent duplication failure exhausts a finite retry budget");

    const auto persistent_dimensions = fthr::dxgi::RunBoundedRecovery(
        [] { return true; },
        [](uint32_t) { return true; },
        [](uint32_t) {
            return fthr::dxgi::RecoveryAttemptForDecision(
                fthr::dxgi::RecoveryDecision::DimensionsChanged);
        });
    Check(persistent_dimensions.outcome
              == fthr::dxgi::RecoveryOutcome::AttemptsExhausted
              && persistent_dimensions.attempts
                  == fthr::dxgi::kMaxRecoveryAttempts,
          "persistent dimension mismatch exhausts the bounded retry budget");

    int stopped_attempts = 0;
    const auto stopped = fthr::dxgi::RunBoundedRecovery(
        [&stopped_attempts] { return stopped_attempts == 0; },
        [](uint32_t) { return true; },
        [&stopped_attempts](uint32_t) {
            ++stopped_attempts;
            return fthr::dxgi::RecoveryAttemptResult::RetryableFailure;
        });
    Check(stopped.outcome == fthr::dxgi::RecoveryOutcome::StopRequested
              && stopped.attempts == 1,
          "shutdown interrupts recovery before another duplication attempt");
}

void PendingEncoderWorkWinsStallClassification() {
    Check(fthr::dxgi::ClassifyPipelineStall(true, true, false, true)
              == fthr::dxgi::PipelineStallBoundary::EncoderOutputStalled,
          "pending NVENC work is classified as encoder output stall");
    Check(std::string(fthr::dxgi::PipelineStallCode(
              fthr::dxgi::PipelineStallBoundary::EncoderOutputStalled))
              == "ENCODER_OUTPUT_STALLED",
          "pending encoder stall keeps its actionable diagnostic code");
}

void TenDxgiRecoveryCyclesDoNotAccumulateOwnedFrames() {
    int resource_releases = 0;
    int frame_releases = 0;
    int recoveries = 0;

    for (int cycle = 0; cycle < 10; ++cycle) {
        FakeDxgiResource resource{&resource_releases};
        FakeDxgiDuplication duplication{&frame_releases};
        {
            fthr::dxgi::FrameLease<FakeDxgiResource, FakeDxgiDuplication> lease(
                &resource, &duplication);
        }
        const auto result = fthr::dxgi::RunBoundedRecovery(
            [] { return true; },
            [](uint32_t) { return true; },
            [](uint32_t) {
                return fthr::dxgi::RecoveryAttemptResult::Recovered;
            });
        if (result.outcome == fthr::dxgi::RecoveryOutcome::Recovered) {
            ++recoveries;
        }
    }

    Check(recoveries == 10, "ten injected ACCESS_LOST incidents recover");
    Check(resource_releases == 10 && frame_releases == 10,
          "ten recovery cycles leave no accumulated frame ownership");
}

EncodedVideoConfig VideoConfig(VideoCodec codec) {
    EncodedVideoConfig config;
    config.codec = codec;
    config.width = 1920;
    config.height = 1080;
    config.frame_rate = {60, 1};
    config.time_base = {1, 60};
    config.bitrate_kbps = 16000;
    config.max_keyframe_interval_frames = 240;
    config.max_b_frames = 0;
    config.packet_format = codec == VideoCodec::H264
        ? EncodedPacketFormat::LengthPrefixedNalUnits
        : codec == VideoCodec::HEVC
            ? EncodedPacketFormat::AnnexBNalUnits
            : EncodedPacketFormat::LowOverheadObu;
    return config;
}

void NativeNvencCodecSelectionCoversNvidiaMatrix() {
    const auto* h264 = fthr::GetNvencCodecSelection(VideoCodec::H264);
    const auto* hevc = fthr::GetNvencCodecSelection(VideoCodec::HEVC);
    const auto* av1 = fthr::GetNvencCodecSelection(VideoCodec::AV1);

    Check(h264 && GuidEquals(h264->encode_guid, NV_ENC_CODEC_H264_GUID),
          "H.264 selects the native NVENC H.264 GUID");
    Check(h264 && GuidEquals(h264->profile_guid, NV_ENC_H264_PROFILE_MAIN_GUID),
          "H.264 selects the NVENC main profile");
    Check(hevc && GuidEquals(hevc->encode_guid, NV_ENC_CODEC_HEVC_GUID),
          "HEVC selects the native NVENC HEVC GUID");
    Check(hevc && GuidEquals(hevc->profile_guid, NV_ENC_HEVC_PROFILE_MAIN_GUID),
          "HEVC selects the NVENC main profile");
    Check(av1 && GuidEquals(av1->encode_guid, NV_ENC_CODEC_AV1_GUID),
          "AV1 selects the native NVENC AV1 GUID");
    Check(av1 && GuidEquals(av1->profile_guid, NV_ENC_AV1_PROFILE_MAIN_GUID),
          "AV1 selects the NVENC main profile");
    Check(h264 && std::string(h264->active_codec_name) == "h264_nvenc",
          "H.264 has a truthful active codec name");
    Check(hevc && std::string(hevc->active_codec_name) == "hevc_nvenc",
          "HEVC has a truthful active codec name");
    Check(av1 && std::string(av1->active_codec_name) == "av1_nvenc",
          "AV1 has a truthful active codec name");
}

void UnsupportedNvencCodecIsRejected() {
    const GUID h264_only[] = {NV_ENC_CODEC_H264_GUID};
    Check(fthr::IsNvencCodecSupported(
              VideoCodec::H264, h264_only, std::size(h264_only)),
          "advertised H.264 GUID is accepted");
    Check(!fthr::IsNvencCodecSupported(
              VideoCodec::HEVC, h264_only, std::size(h264_only)),
          "missing HEVC GUID is rejected");
    Check(!fthr::IsNvencCodecSupported(
              VideoCodec::AV1, h264_only, std::size(h264_only)),
          "missing AV1 GUID is rejected without becoming H.264");
    Check(fthr::GetNvencCodecSelection(static_cast<VideoCodec>(99)) == nullptr,
          "unknown codec has no native NVENC selection");

    const NV_ENC_BUFFER_FORMAT formats[] = {NV_ENC_BUFFER_FORMAT_NV12};
    Check(!fthr::IsNvencInputFormatSupported(
              NV_ENC_BUFFER_FORMAT_ARGB, formats, std::size(formats)),
          "missing ARGB support rejects the existing D3D11 frame path");
}

void NativeNvencLowLatencyConfigurationIsCodecSpecific() {
    NV_ENC_CONFIG h264 = {};
    NV_ENC_CONFIG hevc = {};
    NV_ENC_CONFIG av1 = {};

    Check(fthr::ConfigureNvencCodec(VideoCodec::H264, 60, h264),
          "H.264 NVENC config is produced");
    Check(fthr::ConfigureNvencCodec(VideoCodec::HEVC, 60, hevc),
          "HEVC NVENC config is produced");
    Check(fthr::ConfigureNvencCodec(VideoCodec::AV1, 60, av1),
          "AV1 NVENC config is produced");

    Check(h264.frameIntervalP == 1 && h264.gopLength == 60
              && h264.encodeCodecConfig.h264Config.idrPeriod == 60,
          "H.264 preserves no-B-frame four-second GOP behavior");
    Check(hevc.frameIntervalP == 1 && hevc.gopLength == 60
              && hevc.encodeCodecConfig.hevcConfig.idrPeriod == 60,
          "HEVC uses no B-frames and a four-second IDR bound");
    Check(av1.frameIntervalP == 1 && av1.gopLength == 60
              && av1.encodeCodecConfig.av1Config.idrPeriod == 60,
          "AV1 uses no B-frames and a one-second keyframe bound");
    Check(av1.encodeCodecConfig.av1Config.outputAnnexBFormat == 0,
          "AV1 emits MP4-compatible low-overhead OBUs");
    Check(av1.encodeCodecConfig.av1Config.disableSeqHdr == 0
              && av1.encodeCodecConfig.av1Config.repeatSeqHdr == 1,
          "AV1 repeats its sequence header on MP4 keyframes");
    Check(av1.encodeCodecConfig.av1Config.enableTimingInfo == 1,
          "AV1 publishes the configured frame-rate timing");
    const auto& h264_vui = h264.encodeCodecConfig.h264Config.h264VUIParameters;
    Check(h264_vui.timingInfoPresentFlag == 1
              && h264_vui.numUnitInTicks == 1
              && h264_vui.timeScale == 120,
          "H.264 publishes the configured fixed frame rate in VUI timing");
    Check(h264_vui.videoSignalTypePresentFlag == 1
              && h264_vui.videoFullRangeFlag == 0
              && h264_vui.colourDescriptionPresentFlag == 1
              && h264_vui.colourPrimaries == NV_ENC_VUI_COLOR_PRIMARIES_BT709
              && h264_vui.transferCharacteristics
                  == NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709
              && h264_vui.colourMatrix == NV_ENC_VUI_MATRIX_COEFFS_BT709,
          "H.264 explicitly signals studio-range BT.709 SDR");
    const auto& hevc_vui = hevc.encodeCodecConfig.hevcConfig.hevcVUIParameters;
    Check(hevc_vui.timingInfoPresentFlag == 1
              && hevc_vui.numUnitInTicks == 1
              && hevc_vui.timeScale == 120,
          "HEVC publishes the configured fixed frame rate in VUI timing");
    Check(hevc_vui.videoSignalTypePresentFlag == 1
              && hevc_vui.videoFullRangeFlag == 0
              && hevc_vui.colourDescriptionPresentFlag == 1
              && hevc_vui.colourPrimaries == NV_ENC_VUI_COLOR_PRIMARIES_BT709
              && hevc_vui.transferCharacteristics
                  == NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709
              && hevc_vui.colourMatrix == NV_ENC_VUI_MATRIX_COEFFS_BT709,
          "HEVC explicitly signals studio-range BT.709 SDR");
    Check(av1.encodeCodecConfig.av1Config.colorRange == 0
              && av1.encodeCodecConfig.av1Config.colorPrimaries
                  == NV_ENC_VUI_COLOR_PRIMARIES_BT709
              && av1.encodeCodecConfig.av1Config.transferCharacteristics
                  == NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709
              && av1.encodeCodecConfig.av1Config.matrixCoefficients
                  == NV_ENC_VUI_MATRIX_COEFFS_BT709,
          "AV1 explicitly signals studio-range BT.709 SDR");
}

void NativeNvencSupportsHighFrameRateMatrix() {
    for (const uint32_t fps : {30u, 60u, 120u, 144u}) {
        for (const auto codec : {
                 VideoCodec::H264, VideoCodec::HEVC, VideoCodec::AV1}) {
            NV_ENC_CONFIG config = {};
            Check(fthr::ConfigureNvencCodec(codec, fps, config),
                "NVENC codec policy accepts the tested frame-rate matrix");
            uint32_t time_scale = 0;
            bool timing_enabled = false;
            switch (codec) {
            case VideoCodec::H264:
                time_scale = config.encodeCodecConfig.h264Config
                    .h264VUIParameters.timeScale;
                timing_enabled = time_scale != 0;
                break;
            case VideoCodec::HEVC:
                time_scale = config.encodeCodecConfig.hevcConfig
                    .hevcVUIParameters.timeScale;
                timing_enabled = time_scale != 0;
                break;
            case VideoCodec::AV1:
                timing_enabled = config.encodeCodecConfig.av1Config
                    .enableTimingInfo != 0;
                break;
            }
            Check(config.gopLength == fps
                    && timing_enabled
                    && ((codec == VideoCodec::AV1)
                        || time_scale == fps * 2),
                "NVENC publishes the requested high frame rate timing");
        }
    }

    for (const auto resolution : {
             std::pair<uint32_t, uint32_t>{1920, 1080},
             std::pair<uint32_t, uint32_t>{2560, 1440},
             std::pair<uint32_t, uint32_t>{3840, 2160}}) {
        for (const uint32_t fps : {30u, 60u, 120u, 144u}) {
            const auto config = fthr::BuildNvencVideoConfig(
                VideoCodec::H264, resolution.first, resolution.second,
                fps, 200'000, {});
            Check(config.width == resolution.first
                    && config.height == resolution.second
                    && config.frame_rate.numerator
                        == static_cast<int32_t>(fps)
                    && config.frame_rate.denominator == 1
                    && config.time_base.numerator == 1
                    && config.time_base.denominator
                        == static_cast<int32_t>(fps)
                    && config.bitrate_kbps == 200'000
                    && fthr::IsValidEncodedVideoConfig(config),
                "NVENC video config preserves resolution/FPS requests including 4K120");
        }
    }
}

void NativeNvencVideoConfigsPropagateCodecData() {
    const std::vector<uint8_t> h264_extra{1, 100, 0, 31};
    const std::vector<uint8_t> hevc_extra{0, 0, 0, 1, 64};
    const std::vector<uint8_t> av1_extra{0x0A, 0x02, 0x18, 0x00};

    const auto h264 = fthr::BuildNvencVideoConfig(
        VideoCodec::H264, 1920, 1080, 60, 16000, h264_extra);
    const auto hevc = fthr::BuildNvencVideoConfig(
        VideoCodec::HEVC, 1920, 1080, 60, 16000, hevc_extra);
    const auto av1 = fthr::BuildNvencVideoConfig(
        VideoCodec::AV1, 1920, 1080, 60, 16000, av1_extra);

    Check(h264.codec == VideoCodec::H264
              && h264.packet_format == EncodedPacketFormat::LengthPrefixedNalUnits
              && h264.codec_extradata == h264_extra,
          "H.264 config propagates AVCC extradata");
    Check(hevc.codec == VideoCodec::HEVC
              && hevc.packet_format == EncodedPacketFormat::AnnexBNalUnits
              && hevc.codec_extradata == hevc_extra,
          "HEVC config propagates Annex B VPS/SPS/PPS");
    Check(av1.codec == VideoCodec::AV1
              && av1.packet_format == EncodedPacketFormat::LowOverheadObu
              && av1.codec_extradata == av1_extra,
          "AV1 config propagates its sequence header OBUs");

    const std::pair<uint32_t, uint32_t> resolutions[] = {
        {1920, 1080}, {2560, 1440}, {3840, 2160},
    };
    const uint32_t frame_rates[] = {30, 60, 120, 144};
    for (const auto codec_and_extra : {
             std::pair<VideoCodec, const std::vector<uint8_t>*>(
                 VideoCodec::H264, &h264_extra),
             std::pair<VideoCodec, const std::vector<uint8_t>*>(
                 VideoCodec::HEVC, &hevc_extra),
             std::pair<VideoCodec, const std::vector<uint8_t>*>(
                 VideoCodec::AV1, &av1_extra)}) {
        for (const auto resolution : resolutions) {
            for (const uint32_t fps : frame_rates) {
                const auto config = fthr::BuildNvencVideoConfig(
                    codec_and_extra.first,
                    resolution.first,
                    resolution.second,
                    fps,
                    200'000,
                    *codec_and_extra.second);
                Check(config.width == resolution.first
                        && config.height == resolution.second
                        && config.frame_rate.numerator
                            == static_cast<int32_t>(fps)
                        && config.frame_rate.denominator == 1
                        && config.time_base.numerator == 1
                        && config.time_base.denominator
                            == static_cast<int32_t>(fps)
                        && config.bitrate_kbps == 200'000
                        && config.codec_extradata == *codec_and_extra.second
                        && fthr::IsValidEncodedVideoConfig(config),
                    "NVENC config preserves the resolution/FPS matrix including 4K120");
            }
        }
    }
}

void H264CodecMapsCorrectly() {
    Check(fthr::ToAvCodecId(VideoCodec::H264) == AV_CODEC_ID_H264,
          "H.264 maps to FFmpeg H.264 codec id");
}

void HevcConfigIsProductionEnabled() {
    const auto config = VideoConfig(VideoCodec::HEVC);
    Check(config.codec == VideoCodec::HEVC
              && fthr::ToAvCodecId(config.codec) == AV_CODEC_ID_HEVC,
          "HEVC config and mux mapping exist");
    Check(fthr::IsProductionReplayCodecEnabled(config.codec),
          "HEVC is enabled for the native NVIDIA production backend");
}

void Av1ConfigIsProductionEnabled() {
    const auto config = VideoConfig(VideoCodec::AV1);
    Check(config.codec == VideoCodec::AV1
              && fthr::ToAvCodecId(config.codec) == AV_CODEC_ID_AV1,
          "AV1 config and mux mapping exist");
    Check(fthr::IsProductionReplayCodecEnabled(config.codec),
          "AV1 is enabled for the native NVIDIA production backend");
}

void RingStoresAndSnapshotsGenericConfig() {
    EncodedRingBuffer ring(8, 60, 1000);
    auto config = VideoConfig(VideoCodec::HEVC);
    config.codec_extradata = {1, 2, 3, 4};
    ring.SetVideoConfig(config);
    const uint8_t packet[] = {9, 8, 7};
    ring.Push(packet, sizeof(packet), 41, true, 1000);

    const auto snapshot = ring.TakeSnapshotByTime(1, 1000);
    Check(snapshot.video_config == config,
          "ring snapshot preserves codec-neutral video config");
    Check(snapshot.video_config.codec_extradata == config.codec_extradata,
          "codec extradata survives ring snapshot");
}

void PacketTimingAndKeyframeMetadataRemainUnchanged() {
    EncodedRingBuffer ring(8, 60, 1000);
    ring.SetVideoConfig(VideoConfig(VideoCodec::H264));
    const uint8_t packet[] = {4, 5, 6};
    ring.Push(packet, sizeof(packet), 123, true, 2000);

    const auto snapshot = ring.TakeSnapshotByTime(1, 2000);
    Check(snapshot.packets.size() == 1 && snapshot.packets[0].pts == 123,
          "packet PTS unchanged through codec-neutral ring");
    Check(snapshot.packets[0].is_keyframe,
          "keyframe metadata unchanged through codec-neutral ring");
}

void CodecNeutralRingKeepsTimestampIntervalSelection() {
    EncodedRingBuffer ring(16, 1, 1000);
    ring.SetVideoConfig(VideoConfig(VideoCodec::H264));
    const uint8_t packet[] = {1};
    for (int64_t second = 1; second <= 5; ++second) {
        ring.Push(packet, sizeof(packet), second - 1,
                  second == 1 || second == 4, second * 1000);
    }

    const auto snapshot = ring.TakeSnapshotByTime(2, 5000);
    Check(snapshot.full_history && snapshot.presentation_start_qpc_s == 3.0,
          "AUDIT-042 timestamp boundary remains two seconds before save");
    Check(!snapshot.packets.empty() && snapshot.packets.front().is_keyframe,
          "timestamp selection retains decoder keyframe pre-roll");
}

void RingKeepsOneImmutableConfigPerGeneration() {
    EncodedRingBuffer ring(8, 60, 1000);
    const auto h264 = VideoConfig(VideoCodec::H264);
    const auto hevc = VideoConfig(VideoCodec::HEVC);
    Check(ring.SetVideoConfig(h264), "first generation config is accepted");
    const uint8_t packet[] = {1, 2, 3};
    ring.Push(packet, sizeof(packet), 0, true, 1000);
    Check(!ring.SetVideoConfig(hevc),
          "published H.264 generation cannot be relabeled HEVC");
    Check(ring.TakeSnapshotByTime(1, 1000).video_config.codec == VideoCodec::H264,
          "snapshot retains the original generation codec");
}

void ReplayEncoderInterfaceIsPolymorphic() {
    Check(std::has_virtual_destructor_v<fthr::IReplayEncoder>,
          "replay encoder interface has a virtual destructor");
    Check(fthr::IsProductionReplayBackendEnabled(
              fthr::EncoderVendor::Nvidia, VideoCodec::H264)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Nvidia, VideoCodec::HEVC)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Nvidia, VideoCodec::AV1),
          "NVIDIA native production backend enables all three codecs");
    Check(fthr::IsProductionReplayBackendEnabled(
              fthr::EncoderVendor::Amd, VideoCodec::H264)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Amd, VideoCodec::HEVC)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Amd, VideoCodec::AV1),
          "AMD FFmpeg AMF production backend enables all three codecs");
    Check(fthr::IsProductionReplayBackendEnabled(
              fthr::EncoderVendor::Intel, VideoCodec::H264)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Intel, VideoCodec::HEVC)
              && fthr::IsProductionReplayBackendEnabled(
                  fthr::EncoderVendor::Intel, VideoCodec::AV1),
          "Intel FFmpeg QSV production backend enables all three codecs");
}

} // namespace

void FocusPauseAndDeferredRecordingConfig() {
    fthr::ReplayPauseClock replay_clock(30);
    constexpr int64_t second = 10'000'000;
    replay_clock.Observe(true, 10 * second);
    if (replay_clock.Map(100 * second) != 10 * second) std::abort();
    if (replay_clock.MapAudio(second, second, 48'000 * 20, 1024, 48'000)) std::abort();
    replay_clock.Observe(false, 110 * second);
    if (replay_clock.Map(115 * second) != 15 * second) std::abort();
    const auto audio_pts = replay_clock.MapAudio(
        second, second, 48'000 * 114, 1024, 48'000);
    if (!audio_pts || *audio_pts != 48'000 * 14) std::abort();
    // Delayed AAC output overlapping a completed pause must still be dropped.
    if (replay_clock.MapAudio(second, second, 48'000 * 50, 1024, 48'000)) std::abort();
    replay_clock.Observe(true, 120 * second);
    replay_clock.Observe(false, 125 * second);
    if (replay_clock.Map(126 * second) != 21 * second) std::abort();
    // Pruning old pause spans retains the cumulative offset for future media.
    replay_clock.Observe(false, 200 * second);
    if (replay_clock.Map(201 * second) != 96 * second) std::abort();
    fthr::CaptureFocusPolicy focus;
    auto transition = focus.Observe(false);
    if (transition.paused || transition.resumed || transition.discard_queued_frames)
        std::abort();
    for (int cycle = 0; cycle < 10; ++cycle) {
        for (int tick = 0; tick < 600; ++tick) {
            transition = focus.Observe(true);
            if (!transition.paused || transition.resumed || !transition.discard_queued_frames
                || !fthr::ReplayWatchdogSuspended(
                    fthr::CAPTURE_HEALTH_ACTIVE | fthr::CAPTURE_HEALTH_PAUSED))
                std::abort();
        }
        transition = focus.Observe(false);
        if (transition.paused || !transition.resumed || !transition.discard_queued_frames)
            std::abort();
        transition = focus.Observe(false);
        if (transition.paused || transition.resumed || transition.discard_queued_frames
            || fthr::ReplayWatchdogSuspended(fthr::CAPTURE_HEALTH_ACTIVE))
            std::abort();
    }
    fthr::EncodedRingBuffer ring(8, 60, 1000);
    fthr::EncodedVideoConfig output;
    if (ring.WaitForVideoConfig(output, std::chrono::milliseconds(1))) std::abort();
    fthr::EncodedVideoConfig config;
    config.width = 1920;
    config.height = 1080;
    config.codec_extradata = {1, 2, 3, 4};
    std::thread publisher([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        ring.SetVideoConfig(config);
    });
    const bool ready = ring.WaitForVideoConfig(output, std::chrono::seconds(1));
    publisher.join();
    if (!ready || !(output == config)) std::abort();
}

int main() {
    FocusPauseAndDeferredRecordingConfig();
    LiveWindowsDesktopAdapterSmoke();
    LiveDesktopSurvivesMissingPnpMetadata();
    OneMonitorOneAdapter();
    WgcFrameLeaseReturnsEveryPoolSlot();
    TwoMonitorsSameAdapter();
    SecondaryMonitorSelected();
    TwoAdaptersOneMonitorEach();
    EnumerationOrderChanges();
    PrimaryMonitorChanges();
    SelectedMonitorMissing();
    DevicePathNormalization();
    MatchingLuidMultipleOutputs();
    OutputRemovedAfterResolution();
    StaleTransientMappingRejected();
    NoFallbackToPrimary();
    NoFallbackToOutputZero();
    NativeError4551PreservesWin32AndHresultIdentity();
    DxgiFrameLeaseReleasesExactlyOnce();
    RecentDxgiEvidencePublishesCompleteBoundedEvents();
    DxgiFrameMetadataAndTextureContractsRejectFalseFrames();
    DxgiRecoveryEligibilityRequiresSameCaptureContract();
    DxgiRecoveryRunnerIsBoundedAndInterruptible();
    TenDxgiRecoveryCyclesDoNotAccumulateOwnedFrames();
    PendingEncoderWorkWinsStallClassification();
    NativeNvencCodecSelectionCoversNvidiaMatrix();
    UnsupportedNvencCodecIsRejected();
    NativeNvencLowLatencyConfigurationIsCodecSpecific();
    NativeNvencSupportsHighFrameRateMatrix();
    NativeNvencVideoConfigsPropagateCodecData();
    H264CodecMapsCorrectly();
    HevcConfigIsProductionEnabled();
    Av1ConfigIsProductionEnabled();
    RingStoresAndSnapshotsGenericConfig();
    PacketTimingAndKeyframeMetadataRemainUnchanged();
    CodecNeutralRingKeepsTimestampIntervalSelection();
    RingKeepsOneImmutableConfigPerGeneration();
    ReplayEncoderInterfaceIsPolymorphic();
    RunSoftwareReplayEncoderTests();
    const int amf_checks = RunAmfReplayEncoderTests();
    const int qsv_checks = RunQsvReplayEncoderTests();
    const int policy_checks = RunWindowsReplayPolicyTests();
    const int audio_source_checks = RunAudioSourceModelTests();
    const int windows_audio_checks = RunWindowsAudioSessionRegistryTests();
    const int windows_process_loopback_checks = RunWindowsProcessLoopbackAudioProviderTests();
    const int windows_microphone_checks = RunWindowsMicrophoneAudioProviderTests();
    const int audio_format_checks = RunAudioFormatConverterTests();
    const int capture_border_checks = RunWindowsCaptureBorderPolicyTests();
    const int capture_backend_checks = RunWindowsCaptureBackendPolicyTests();
    const int recording_checks = RunContinuousRecordingWriterTests();
    const int nvenc_lifecycle_checks = RunNvencInputLifecycleTests();
    const int capture_scale_checks = RunCaptureScaleGeometryTests();
    std::cout << "FTHRclips_tests: native suites completed ("
              << (checks + amf_checks + qsv_checks + policy_checks + audio_source_checks
                  + windows_audio_checks + windows_process_loopback_checks)
                  + windows_microphone_checks + audio_format_checks + capture_border_checks
                  + capture_backend_checks
                  + recording_checks
                  + nvenc_lifecycle_checks
                  + capture_scale_checks
              << " checks total)" << std::endl;
    return 0;
}
