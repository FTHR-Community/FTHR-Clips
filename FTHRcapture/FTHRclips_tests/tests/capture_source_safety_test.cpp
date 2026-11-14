#include "async_content_sampler.h"
#include "bgra_direct_copy.h"
#include "window_capture_identity.h"
#include "capture_focus_policy.h"
#include <d3d11_4.h>
#include <wrl/client.h>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <vector>

namespace {
using Microsoft::WRL::ComPtr;
using Clock = std::chrono::steady_clock;
int checks = 0;
void Check(bool value, const char* message) {
    ++checks;
    if (!value) { std::cerr << "capture source safety: " << message << '\n'; std::abort(); }
}
double Milliseconds(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}
double Percentile(std::vector<double> values, double fraction) {
    std::sort(values.begin(), values.end());
    return values[static_cast<size_t>((values.size() - 1) * fraction)];
}
void WindowsKeepTheirIdentity() {
    const auto create = []() {
        return CreateWindowExW(WS_EX_NOACTIVATE, L"STATIC", L"FTHR source lifecycle test",
            WS_OVERLAPPEDWINDOW, -24000, -24000, 320, 180, nullptr, nullptr,
            GetModuleHandleW(nullptr), nullptr);
    };
    HWND window = create(), other = create();
    Check(window && other, "test windows created offscreen");
    fthr::WindowCaptureIdentity identity;
    Check(!identity.Bind(nullptr), "null source rejected");
    Check(identity.Bind(window) && identity.Matches(window), "bind exact window identity");
    Check(identity.HasProcessWatch(), "current process lifetime retained");
    Check(!identity.Matches(other), "different HWND cannot become selected source");
    Check(!identity.IsRenderable(), "hidden window pauses source");
    ShowWindow(window, SW_SHOWNOACTIVATE);
    Check(identity.IsRenderable(), "shown source can render");
    ShowWindow(window, SW_SHOWMINNOACTIVE);
    Check(identity.Matches(window) && !identity.IsRenderable(), "minimize pauses without losing identity");
    ShowWindow(window, SW_SHOWNOACTIVATE);
    Check(identity.IsRenderable(), "restore retains selected source");
    SetWindowPos(window, nullptr, 0, 0, 640, 360, SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOZORDER);
    Check(identity.Matches(window) && identity.IsRenderable(), "resize preserves identity");
    DestroyWindow(window);
    Check(!identity.Matches(window) && !identity.IsRenderable(), "game end invalidates source");
    identity.Reset();
    Check(!identity.Matches(other) && !identity.HasProcessWatch(), "reset releases identity and process");
    DWORD before = 0, after = 0;
    GetProcessHandleCount(GetCurrentProcess(), &before);
    for (int index = 0; index < 20; ++index) {
        Check(identity.Bind(other), "rebind for repeated source transitions");
        identity.Reset();
    }
    GetProcessHandleCount(GetCurrentProcess(), &after);
    Check(after <= before + 1, "repeated bind/reset does not accumulate process handles");
    DestroyWindow(other);
    fthr::CaptureFocusPolicy policy;
    Check(!policy.Observe(false).paused, "focused source starts active");
    // Simulate focus loss after the early poll while frames are drained.
    Check(policy.Observe(true).discard_queued_frames, "late focus loss discards pool images");
    const auto returned = policy.Observe(false);
    Check(returned.resumed && returned.discard_queued_frames,
        "rapid Alt-Tab return still requires resume warmup");
}

D3D11_TEXTURE2D_DESC Description(UINT width = 320, UINT height = 180) {
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width; desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    return desc;
}
void DirectCopyContracts() {
    auto source = Description(), target = source;
    using fthr::detail::CanDirectCopyBgra;
    Check(CanDirectCopyBgra(source, 0, target, 0), "matching BGRA can copy directly");
    target.Width += 2;
    Check(!CanDirectCopyBgra(source, 0, target, 0), "resize requires processor");
    target = source; target.Format = DXGI_FORMAT_NV12;
    Check(!CanDirectCopyBgra(source, 0, target, 0), "NV12 requires conversion");
    target = source; target.Format = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
    Check(!CanDirectCopyBgra(source, 0, target, 0), "SRGB is not the production BGRA contract");
    target = source; target.SampleDesc.Count = 2;
    Check(!CanDirectCopyBgra(source, 0, target, 0), "multisample rejects direct path");
    target = source;
    Check(!CanDirectCopyBgra(source, 1, target, 0), "invalid source slice rejected");
    source.ArraySize = 3;
    Check(CanDirectCopyBgra(source, 2, target, 0), "valid source slice accepted");
    Check(!CanDirectCopyBgra(source, 2, target, 1), "invalid target slice rejected");
    source = Description(640, 360); source.MipLevels = 2;
    Check(CanDirectCopyBgra(source, 1, target, 0), "selected mip geometry compared");
    source.Width = 0;
    Check(!CanDirectCopyBgra(source, 1, target, 0), "zero geometry rejected");
}
struct Samples {
    uint32_t callbacks = 0;
    fthr::ContentSampleMetrics metrics;
    static void Publish(void* owner, const fthr::ContentSampleMetrics& metrics) {
        auto& self = *static_cast<Samples*>(owner);
        ++self.callbacks; self.metrics = metrics;
    }
};
void Drain(fthr::AsyncContentSampler& sampler, ID3D11Device* device,
           ID3D11DeviceContext* context, ID3D11Texture2D* source, Samples& samples,
           uint32_t count) {
    // Only the test submits and waits. Production Tick never flushes/waits.
    context->Flush();
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    while (samples.callbacks < count && Clock::now() < deadline) {
        sampler.Tick(device, context, source, false, &samples, Samples::Publish);
        Sleep(1);
    }
    Check(samples.callbacks == count, "asynchronous sample completes within test deadline");
}
void GpuContracts(D3D_DRIVER_TYPE type) {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    const HRESULT created = D3D11CreateDevice(nullptr, type, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
        &device, nullptr, &context);
    if (FAILED(created) && type == D3D_DRIVER_TYPE_HARDWARE) {
        std::cout << "capture source safety: hardware D3D unavailable; WARP still required\n";
        return;
    }
    Check(SUCCEEDED(created), "D3D test device created");
    auto desc = Description();
    desc.ArraySize = 2;
    std::vector<uint32_t> dark(desc.Width * desc.Height, 0xff202020);
    std::vector<uint32_t> gray(desc.Width * desc.Height, 0xff606060);
    const D3D11_SUBRESOURCE_DATA initial[] = {
        {dark.data(), desc.Width * 4, 0}, {gray.data(), desc.Width * 4, 0}};
    ComPtr<ID3D11Texture2D> array, target, staging;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, initial, &array)), "source texture array created");
    desc.ArraySize = 1;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &target)), "separate BGRA target created");
    context->CopySubresourceRegion(target.Get(), 0, 0, 0, 0, array.Get(), 1, nullptr);
    auto readback = desc; readback.BindFlags = 0;
    readback.Usage = D3D11_USAGE_STAGING; readback.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    Check(SUCCEEDED(device->CreateTexture2D(&readback, nullptr, &staging)), "readback created");
    context->CopyResource(staging.Get(), target.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    Check(SUCCEEDED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped)), "copy readable");
    bool exact = true;
    for (UINT row = 0; row < desc.Height; ++row) {
        exact &= std::memcmp(static_cast<uint8_t*>(mapped.pData) + row * mapped.RowPitch,
            gray.data() + row * desc.Width, desc.Width * 4) == 0;
    }
    context->Unmap(staging.Get(), 0);
    Check(exact, "BGRA copy of selected array slice is byte exact");
    fthr::AsyncContentSampler sampler;
    Samples samples;
    sampler.Tick(device.Get(), context.Get(), target.Get(), true, &samples, Samples::Publish);
    Check(!samples.callbacks && sampler.Stats().queued == 1,
        "new GPU copy never mapped in its enqueue tick");
    Drain(sampler, device.Get(), context.Get(), target.Get(), samples, 1);
    Check(samples.metrics.count == 144 && samples.metrics.sum == 144 * 96
        && samples.metrics.sum_sq == 144 * 96 * 96, "async sample preserves exact luma metrics");
    const auto completed = samples.callbacks;
    sampler.Tick(device.Get(), context.Get(), target.Get(), true, &samples, Samples::Publish);
    sampler.Reset();
    context->Flush();
    sampler.Tick(device.Get(), context.Get(), target.Get(), false, &samples, Samples::Publish);
    Check(samples.callbacks == completed, "reset discards evidence from previous source epoch");
    desc.Width = 97; desc.Height = 53;
    gray.assign(desc.Width * desc.Height, 0xffc8c8c8);
    D3D11_SUBRESOURCE_DATA resized{gray.data(), desc.Width * 4, 0};
    ComPtr<ID3D11Texture2D> resized_source;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, &resized, &resized_source)), "resized source created");
    sampler.Tick(device.Get(), context.Get(), resized_source.Get(), true, &samples, Samples::Publish);
    Drain(sampler, device.Get(), context.Get(), resized_source.Get(), samples, completed + 1);
    Check(samples.metrics.sum == 144 * 200 && samples.metrics.sum_sq == 144 * 200 * 200,
        "resized source sampled without stale geometry or pixels");
    sampler.Reset();
    fthr::AsyncContentSampler bounded;
    Samples burst;
    for (int index = 0; index < 100; ++index) {
        bounded.Tick(device.Get(), context.Get(), target.Get(), true, &burst, Samples::Publish);
        const auto stats = bounded.Stats();
        Check(stats.queued >= stats.completed && stats.queued - stats.completed <= 2,
            "burst cannot create more than two pending readbacks");
    }
    Check(!bounded.Stats().errors && SUCCEEDED(device->GetDeviceRemovedReason()),
        "burst and resize keep D3D device healthy");
}

// Measurement is opt-in and never asserts a speedup. Each method queues the
// same 144 sparse copies. The async drain time is reported separately.
void MeasureSampler() {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
        &device, nullptr, &context))) return;
    auto desc = Description(1920, 1080);
    ComPtr<ID3D11Texture2D> source, staging;
    std::vector<uint32_t> pixels(desc.Width * desc.Height);
    for (size_t pixel = 0; pixel < pixels.size(); ++pixel)
        pixels[pixel] = 0xff000000u | static_cast<uint32_t>((pixel * 2654435761u) & 0x00ffffffu);
    D3D11_SUBRESOURCE_DATA data{pixels.data(), desc.Width * 4, 0};
    Check(SUCCEEDED(device->CreateTexture2D(&desc, &data, &source)), "benchmark source");
    desc = Description(16, 9); desc.BindFlags = 0;
    desc.Usage = D3D11_USAGE_STAGING; desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &staging)), "benchmark staging");
    fthr::AsyncContentSampler sampler;
    Samples samples;
    std::vector<double> blocking, enqueue, drain;
    for (int index = 0; index < 72; ++index) {
        auto start = Clock::now();
        for (UINT row = 0; row < 9; ++row) for (UINT column = 0; column < 16; ++column) {
            const UINT x = (2 * column + 1) * 1920 / 32;
            const UINT y = (2 * row + 1) * 1080 / 18;
            D3D11_BOX pixel{x, y, 0, x + 1, y + 1, 1};
            context->CopySubresourceRegion(staging.Get(), 0, column, row, 0, source.Get(), 0, &pixel);
        }
        D3D11_MAPPED_SUBRESOURCE mapped{};
        Check(SUCCEEDED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped)), "blocking reference maps");
        context->Unmap(staging.Get(), 0);
        const double reference_ms = Milliseconds(start);
        start = Clock::now();
        sampler.Tick(device.Get(), context.Get(), source.Get(), true, &samples, Samples::Publish);
        const double enqueue_ms = Milliseconds(start);
        start = Clock::now();
        Drain(sampler, device.Get(), context.Get(), source.Get(), samples, index + 1);
        const double drain_ms = Milliseconds(start);
        if (index >= 12) {
            blocking.push_back(reference_ms); enqueue.push_back(enqueue_ms); drain.push_back(drain_ms);
        }
    }
    std::cout << "FTHR_SAMPLER_MEASUREMENT {\"samples\":60,\"width\":1920,\"height\":1080,"
        << "\"blocking_cpu_p50_ms\":" << Percentile(blocking, .5)
        << ",\"blocking_cpu_p95_ms\":" << Percentile(blocking, .95)
        << ",\"async_enqueue_cpu_p50_ms\":" << Percentile(enqueue, .5)
        << ",\"async_enqueue_cpu_p95_ms\":" << Percentile(enqueue, .95)
        << ",\"test_drain_wall_p50_ms\":" << Percentile(drain, .5) << "}\n";
}

void MeasureBgraPreparation() {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context))) return;
    ComPtr<ID3D11Multithread> multithread;
    if (SUCCEEDED(context.As(&multithread))) multithread->SetMultithreadProtected(TRUE);
    ComPtr<ID3D11VideoDevice> video_device;
    ComPtr<ID3D11VideoContext> video_context;
    if (FAILED(device.As(&video_device)) || FAILED(context.As(&video_context))) return;
    D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
    content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    content.InputFrameRate = content.OutputFrameRate = {60, 1};
    content.InputWidth = content.OutputWidth = 1920;
    content.InputHeight = content.OutputHeight = 1080;
    content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
    ComPtr<ID3D11VideoProcessor> processor;
    if (FAILED(video_device->CreateVideoProcessorEnumerator(&content, &enumerator))
        || FAILED(video_device->CreateVideoProcessor(enumerator.Get(), 0, &processor))) return;
    auto desc = Description(1920, 1080);
    ComPtr<ID3D11Texture2D> source, converter;
    std::array<ComPtr<ID3D11Texture2D>, 8> leases;
    std::vector<uint32_t> pixels(desc.Width * desc.Height);
    for (size_t pixel = 0; pixel < pixels.size(); ++pixel)
        pixels[pixel] = 0xff000000u | static_cast<uint32_t>((pixel * 2654435761u) & 0x00ffffffu);
    D3D11_SUBRESOURCE_DATA data{pixels.data(), desc.Width * 4, 0};
    Check(SUCCEEDED(device->CreateTexture2D(&desc, &data, &source)), "BGRA benchmark source");
    Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &converter)), "BGRA benchmark converter");
    for (auto& lease : leases)
        Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &lease)), "BGRA benchmark independent lease");
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_desc{};
    output_desc.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    ComPtr<ID3D11VideoProcessorOutputView> output;
    Check(SUCCEEDED(video_device->CreateVideoProcessorOutputView(converter.Get(), enumerator.Get(),
        &output_desc, &output)), "BGRA benchmark processor output");
    RECT rectangle{0, 0, 1920, 1080};
    video_context->VideoProcessorSetStreamFrameFormat(processor.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    video_context->VideoProcessorSetStreamSourceRect(processor.Get(), 0, TRUE, &rectangle);
    video_context->VideoProcessorSetStreamDestRect(processor.Get(), 0, TRUE, &rectangle);
    video_context->VideoProcessorSetOutputTargetRect(processor.Get(), TRUE, &rectangle);
    video_context->VideoProcessorSetStreamAutoProcessingMode(processor.Get(), 0, FALSE);
    D3D11_VIDEO_COLOR background{}; background.RGBA = {0.f, 0.f, 0.f, 1.f};
    video_context->VideoProcessorSetOutputBackgroundColor(processor.Get(), FALSE, &background);
    D3D11_QUERY_DESC query_desc{D3D11_QUERY_EVENT, 0};
    ComPtr<ID3D11Query> completion;
    Check(SUCCEEDED(device->CreateQuery(&query_desc, &completion)), "BGRA benchmark completion event");
    std::vector<double> direct, processor_copy;
    double total_direct = 0, total_processor = 0;
    for (bool use_processor : {false, true, true, false}) { // ABBA limits order bias.
        const auto started = Clock::now();
        for (int index = 0; index < 72; ++index) {
            const auto prepared = Clock::now();
            if (use_processor) {
                D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_desc{};
                input_desc.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
                ComPtr<ID3D11VideoProcessorInputView> input;
                Check(SUCCEEDED(video_device->CreateVideoProcessorInputView(source.Get(), enumerator.Get(),
                    &input_desc, &input)), "BGRA benchmark processor input");
                D3D11_VIDEO_PROCESSOR_STREAM stream{};
                stream.Enable = TRUE; stream.pInputSurface = input.Get();
                Check(SUCCEEDED(video_context->VideoProcessorBlt(processor.Get(), output.Get(), 0, 1, &stream)),
                    "BGRA benchmark processor blit");
                context->CopySubresourceRegion(leases[index % 8].Get(), 0, 0, 0, 0, converter.Get(), 0, nullptr);
            } else context->CopySubresourceRegion(leases[index % 8].Get(), 0, 0, 0, 0, source.Get(), 0, nullptr);
            const auto elapsed = Milliseconds(prepared);
            if (index >= 12) (use_processor ? processor_copy : direct).push_back(elapsed);
        }
        context->End(completion.Get());
        context->Flush(); // Test timing only, never used by the capture sampler.
        HRESULT result = S_FALSE;
        const auto deadline = Clock::now() + std::chrono::seconds(5);
        while (result == S_FALSE && Clock::now() < deadline) {
            result = context->GetData(completion.Get(), nullptr, 0, D3D11_ASYNC_GETDATA_DONOTFLUSH);
            if (result == S_FALSE) SwitchToThread();
        }
        Check(result == S_OK, "BGRA benchmark batch completed");
        (use_processor ? total_processor : total_direct) += Milliseconds(started);
    }
    std::cout << "FTHR_BGRA_MEASUREMENT {\"width\":1920,\"height\":1080,\"cpu_samples_each\":120,"
        << "\"pool_capacity\":8,\"order\":\"ABBA\",\"scope\":\"D3D11 preparation only, no AMF encoding\","
        << "\"direct_cpu_p50_ms\":" << Percentile(direct, .5)
        << ",\"direct_cpu_p95_ms\":" << Percentile(direct, .95)
        << ",\"processor_copy_cpu_p50_ms\":" << Percentile(processor_copy, .5)
        << ",\"processor_copy_cpu_p95_ms\":" << Percentile(processor_copy, .95)
        << ",\"direct_batch_wall_ms_per_frame\":" << total_direct / 144
        << ",\"processor_copy_batch_wall_ms_per_frame\":" << total_processor / 144 << "}\n";
}
} // namespace

int RunCaptureSourceSafetyTests() {
    WindowsKeepTheirIdentity();
    DirectCopyContracts();
    GpuContracts(D3D_DRIVER_TYPE_WARP);
    GpuContracts(D3D_DRIVER_TYPE_HARDWARE);
    std::cout << "capture source safety tests: " << checks << " checks\n";
    return checks;
}
#ifdef FTHR_CAPTURE_SAFETY_STANDALONE
int main(int argc, char*[]) {
    RunCaptureSourceSafetyTests();
    if (argc > 1) { MeasureSampler(); MeasureBgraPreparation(); }
}
#endif
