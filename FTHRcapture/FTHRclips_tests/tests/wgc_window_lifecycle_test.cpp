#include "wgc_window_lifecycle.h"

#include <cstdlib>
#include <iostream>
#include <memory>
#include <objbase.h>
#include <thread>

#include <d3d11.h>

#pragma comment(lib, "d3d11.lib")

namespace {

int checks = 0;

void Check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << '\n';
        std::exit(1);
    }
}

void WindowStateClassification() {
    using fthr::wgc::WindowState;
    Check(fthr::wgc::ClassifyWindowState(true, false, true)
            == WindowState::Active,
        "valid visible window with client area is active");
    Check(fthr::wgc::ClassifyWindowState(true, true, true)
            == WindowState::Paused,
        "iconic window pauses without being treated as closed");
    Check(fthr::wgc::ClassifyWindowState(true, false, false)
            == WindowState::Paused,
        "zero-area or hidden window pauses while occlusion is not inferred");
    Check(fthr::wgc::ClassifyWindowState(false, false, false)
            == WindowState::Closed,
        "invalid HWND is a terminal closed-source observation");
}

void FrameBoundsClassification() {
    using fthr::wgc::FrameAction;
    Check(fthr::wgc::ClassifyFrameAction(1920, 1080, 1920, 1080,
                                         1920, 1080, 1920, 1080, false)
            == FrameAction::Copy,
        "same-size frame keeps the fast copy path");
    Check(fthr::wgc::ClassifyFrameAction(1280, 720, 1920, 1080,
                                         1920, 1080, 1920, 1080, false)
            == FrameAction::Normalize,
        "smaller content is normalized using only its valid rectangle");
    Check(fthr::wgc::ClassifyFrameAction(2, 2, 4, 2,
                                         4, 2, 2, 2, false)
            == FrameAction::CopyValidRect,
        "canvas-sized content in a padded texture uses a bounded rectangle copy");
    Check(fthr::wgc::ClassifyFrameAction(1921, 1080, 1920, 1080,
                                         1920, 1080, 1920, 1080, false)
            == FrameAction::RecreatePool,
        "content larger than the current pool requires a safe pool recreate");
    Check(fthr::wgc::ClassifyFrameAction(1, 1080, 1920, 1080,
                                         1920, 1080, 1920, 1080, false)
            == FrameAction::Normalize,
        "one-pixel content is not rejected by a minimum-size blacklist");
    Check(fthr::wgc::ClassifyFrameAction(0, 1080, 1920, 1080,
                                         1920, 1080, 1920, 1080, false)
            == FrameAction::Discard,
        "empty content is never copied or used as a normalization source");
    Check(fthr::wgc::ClassifyFrameAction(1280, 720, 1920, 1080,
                                         1920, 1080, 1920, 1080, true)
            == FrameAction::Normalize,
        "same-aspect FIT resize can normalize without stacked letterboxing");
    Check(fthr::wgc::ClassifyFrameAction(1920, 1080, 1920, 1080,
                                         1920, 1080, 1440, 1080, true)
            == FrameAction::RequiresReconfigure,
        "FIT aspect change requires fresh encoder geometry, not distortion/bars");
}

void RecreateAttemptsResetOnlyAfterStableProgress() {
    fthr::wgc::RecreateAttemptBudget budget(2);
    Check(budget.CanAttempt(), "recreate budget initially permits attempts");
    budget.RecordFailure();
    budget.RecordFailure();
    Check(!budget.CanAttempt(), "resize transitions share a bounded attempt budget");
    budget.ObserveStableFrameProgress();
    Check(!budget.CanAttempt(), "one frame does not reset a failed recreate budget");
    for (int i = 0; i < 28; ++i) budget.ObserveStableFrameProgress();
    Check(!budget.CanAttempt(), "budget remains exhausted before stable interval");
    budget.ObserveStableFrameProgress();
    Check(budget.CanAttempt() && budget.Attempts() == 0,
        "only proven stable frame progress resets recreate attempts");
}

void RecreateFailureClassification() {
    using fthr::wgc::RecreateFailureAction;
    Check(fthr::wgc::ClassifyRecreateFailure(RPC_E_WRONG_THREAD)
            == RecreateFailureAction::ReconfigureRequired,
        "RPC_E_WRONG_THREAD requests one fresh capture generation");
    Check(fthr::wgc::ClassifyRecreateFailure(E_FAIL)
            == RecreateFailureAction::RetryWithinBudget,
        "ordinary recreate failures retain the bounded in-place retry policy");
    Check(fthr::wgc::ClassifyRecreateFailure(
              HRESULT_FROM_WIN32(ERROR_TIMEOUT))
            == RecreateFailureAction::RetryWithinBudget,
        "non-apartment HRESULTs do not trigger the generation fallback");
}

void WinRtApartmentGuardBalancesNestedMtaInitialization() {
    struct ApartmentState {
        HRESULT result = E_UNEXPECTED;
        APTTYPE type = APTTYPE_CURRENT;
        APTTYPEQUALIFIER qualifier = APTTYPEQUALIFIER_NONE;
    };
    struct ApartmentResults {
        ApartmentState before;
        HRESULT outer_init = E_UNEXPECTED;
        HRESULT nested_init = E_UNEXPECTED;
        ApartmentState while_nested;
        ApartmentState after_nested;
        ApartmentState after_outer;
    } results;

    std::thread isolated_thread([&results] {
        const auto query_state = [] {
            ApartmentState state;
            state.result = CoGetApartmentType(&state.type, &state.qualifier);
            return state;
        };
        results.before = query_state();
        {
            fthr::wgc::WinRtApartmentGuard outer;
            results.outer_init = outer.Result();
            {
                fthr::wgc::WinRtApartmentGuard nested;
                results.nested_init = nested.Result();
                results.while_nested = query_state();
            }
            results.after_nested = query_state();
        }
        results.after_outer = query_state();
    });
    isolated_thread.join();

    const bool before_uninitialized = results.before.result == CO_E_NOTINITIALIZED;
    const bool before_implicit_mta = results.before.result == S_OK
        && results.before.type == APTTYPE_MTA
        && results.before.qualifier == APTTYPEQUALIFIER_IMPLICIT_MTA;
    Check(before_uninitialized || before_implicit_mta,
        "isolated test thread begins uninitialized or in the process implicit MTA");
    Check(SUCCEEDED(results.outer_init),
        "outer WinRT apartment guard initializes or joins the MTA");
    Check(results.nested_init == S_FALSE,
        "nested WinRT apartment guard records balanced nested initialization");
    Check(results.while_nested.result == S_OK
            && results.while_nested.type == APTTYPE_MTA
            && results.while_nested.qualifier == APTTYPEQUALIFIER_NONE,
        "guard establishes an explicit MTA while nested");
    Check(results.after_nested.result == S_OK
            && results.after_nested.type == APTTYPE_MTA
            && results.after_nested.qualifier == APTTYPEQUALIFIER_NONE,
        "destroying nested guard does not uninitialize the outer apartment");
    const bool after_uninitialized =
        results.after_outer.result == CO_E_NOTINITIALIZED;
    const bool after_implicit_mta = results.after_outer.result == S_OK
        && results.after_outer.type == APTTYPE_MTA
        && results.after_outer.qualifier == APTTYPEQUALIFIER_IMPLICIT_MTA;
    Check(after_uninitialized || after_implicit_mta,
        "outer guard teardown leaves no explicit MTA initialization behind");
}

void WinRtApartmentGuardPreservesConflictingSta() {
    struct ApartmentResults {
        HRESULT sta_init = E_UNEXPECTED;
        HRESULT guard_init = E_UNEXPECTED;
        HRESULT during_guard = E_UNEXPECTED;
        APTTYPE during_type = APTTYPE_CURRENT;
        APTTYPEQUALIFIER during_qualifier = APTTYPEQUALIFIER_NONE;
        HRESULT after_guard = E_UNEXPECTED;
        APTTYPE after_type = APTTYPE_CURRENT;
        APTTYPEQUALIFIER after_qualifier = APTTYPEQUALIFIER_NONE;
    } results;

    std::thread isolated_sta_thread([&results] {
        results.sta_init = RoInitialize(RO_INIT_SINGLETHREADED);
        if (FAILED(results.sta_init)) return;
        {
            fthr::wgc::WinRtApartmentGuard guard;
            results.guard_init = guard.Result();
            results.during_guard = CoGetApartmentType(
                &results.during_type, &results.during_qualifier);
        }
        results.after_guard = CoGetApartmentType(
            &results.after_type, &results.after_qualifier);
        RoUninitialize();
    });
    isolated_sta_thread.join();

    Check(results.sta_init == S_OK,
        "isolated STA test thread owns its single-threaded apartment");
    Check(results.guard_init == RPC_E_CHANGED_MODE,
        "MTA guard reports a conflicting existing STA without claiming ownership");
    Check(results.during_guard == S_OK
            && (results.during_type == APTTYPE_STA
                || results.during_type == APTTYPE_MAINSTA),
        "failed MTA guard leaves the owned STA valid while in scope");
    Check(results.after_guard == S_OK
            && results.after_type == results.during_type
            && results.after_qualifier == results.during_qualifier,
        "failed MTA guard does not uninitialize the caller-owned STA");
}

void ResizeEncoderGeometryPolicy() {
    using fthr::wgc::RequiresEncoderReconfigureForResize;
    Check(RequiresEncoderReconfigureForResize(
            800, 450, 640, 360, 0, 0, false),
        "native/source-preset output reconfigures on any source-size change");
    Check(!RequiresEncoderReconfigureForResize(
            800, 450, 640, 360, 640, 360, false),
        "source growth beyond canvas is safe when output is the smaller bound");
    Check(RequiresEncoderReconfigureForResize(
            800, 450, 640, 360, 1920, 1080, false),
        "source growth beyond both canvas and output detail requires reconfigure");
    Check(!RequiresEncoderReconfigureForResize(
            320, 180, 640, 360, 1920, 1080, false),
        "source shrink can remain on the fixed encoder geometry");
    Check(RequiresEncoderReconfigureForResize(
            800, 450, 640, 360, 1920, 1080, true),
        "crop-enabled source growth re-resolves the fixed crop profile");
}

void CloseSignalIsGenerationLocal() {
    auto old_item = std::make_shared<fthr::wgc::ItemClosedSignal>();
    auto replacement_item = std::make_shared<fthr::wgc::ItemClosedSignal>();
    old_item->closed.store(true, std::memory_order_release);
    Check(old_item->closed.load(std::memory_order_acquire),
        "closed callback publishes through its owned signal");
    Check(!replacement_item->closed.load(std::memory_order_acquire),
        "old item close cannot invalidate the replacement WGC generation");
}

void WARPValidRectCopyExcludesTexturePadding() {
    winrt::com_ptr<ID3D11Device> device;
    winrt::com_ptr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL obtained{};
    const D3D_FEATURE_LEVEL requested[] = {D3D_FEATURE_LEVEL_11_0};
    HRESULT result = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        0, requested, 1, D3D11_SDK_VERSION, device.put(), &obtained,
        context.put());
    Check(SUCCEEDED(result) && device && context,
        "WARP D3D11 device is available for the valid-rectangle copy test");

    D3D11_TEXTURE2D_DESC source_description{};
    source_description.Width = 4;
    source_description.Height = 2;
    source_description.MipLevels = 1;
    source_description.ArraySize = 1;
    source_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    source_description.SampleDesc.Count = 1;
    source_description.Usage = D3D11_USAGE_DEFAULT;
    const uint32_t pixels[8] = {
        0xFFFF0000, 0xFFFF0000, 0xFF00FF00, 0xFF00FF00,
        0xFFFF0000, 0xFFFF0000, 0xFF00FF00, 0xFF00FF00,
    };
    D3D11_SUBRESOURCE_DATA initial{};
    initial.pSysMem = pixels;
    initial.SysMemPitch = 4 * sizeof(uint32_t);
    winrt::com_ptr<ID3D11Texture2D> source;
    result = device->CreateTexture2D(&source_description, &initial, source.put());
    Check(SUCCEEDED(result) && source,
        "padded WARP source texture was created");

    fthr::wgc::OwnedCanvasNormalizer normalizer;
    std::string error;
    Check(normalizer.Initialize(device.get(), 2, 2, 30, error),
        "canvas matching ContentSize was created");
    Check(normalizer.Copy(context.get(), source.get(), 2, 2, 4, 2,
                          false, error),
        "canvas-sized ContentSize uses CopySubresourceRegion without VP");

    D3D11_TEXTURE2D_DESC staging_description{};
    staging_description.Width = 2;
    staging_description.Height = 2;
    staging_description.MipLevels = 1;
    staging_description.ArraySize = 1;
    staging_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    staging_description.SampleDesc.Count = 1;
    staging_description.Usage = D3D11_USAGE_STAGING;
    staging_description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    winrt::com_ptr<ID3D11Texture2D> staging;
    result = device->CreateTexture2D(&staging_description, nullptr, staging.put());
    Check(SUCCEEDED(result) && staging,
        "valid-rectangle staging texture was created");
    context->CopyResource(staging.get(), normalizer.Texture());
    context->Flush();
    D3D11_MAPPED_SUBRESOURCE mapped{};
    result = context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped);
    Check(SUCCEEDED(result), "valid-rectangle canvas was mapped");
    for (uint32_t y = 0; y < 2; ++y) {
        const auto* row = static_cast<const uint8_t*>(mapped.pData)
            + y * mapped.RowPitch;
        for (uint32_t x = 0; x < 2; ++x) {
            const uint8_t* pixel = row + x * 4;
            Check(pixel[2] > 180 && pixel[1] < 80,
                "only red pixels inside ContentSize reach the owned canvas");
        }
    }
    context->Unmap(staging.get(), 0);
}

void WARPNormalizationUsesOnlyContentSize() {
    winrt::com_ptr<ID3D11Device> device;
    winrt::com_ptr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL obtained{};
    const D3D_FEATURE_LEVEL requested[] = {D3D_FEATURE_LEVEL_11_0};
    HRESULT result = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        0, requested, 1, D3D11_SDK_VERSION, device.put(), &obtained,
        context.put());
    Check(SUCCEEDED(result) && device && context,
        "WARP D3D11 device is available for the pixel-path test");

    D3D11_TEXTURE2D_DESC source_description{};
    source_description.Width = 4;
    source_description.Height = 2;
    source_description.MipLevels = 1;
    source_description.ArraySize = 1;
    source_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    source_description.SampleDesc.Count = 1;
    source_description.Usage = D3D11_USAGE_DEFAULT;
    const uint32_t pixels[8] = {
        0xFFFF0000, 0xFFFF0000, 0xFF00FF00, 0xFF00FF00,
        0xFFFF0000, 0xFFFF0000, 0xFF00FF00, 0xFF00FF00,
    };
    D3D11_SUBRESOURCE_DATA initial{};
    initial.pSysMem = pixels;
    initial.SysMemPitch = 4 * sizeof(uint32_t);
    winrt::com_ptr<ID3D11Texture2D> source;
    result = device->CreateTexture2D(&source_description, &initial, source.put());
    Check(SUCCEEDED(result) && source, "WARP source texture was created");

    fthr::wgc::OwnedCanvasNormalizer normalizer;
    std::string error;
    Check(normalizer.Initialize(device.get(), 4, 2, 30, error),
        "owned BGRA canvas was created");
    if (!normalizer.Copy(context.get(), source.get(), 2, 2, 4, 2,
                         false, error)) {
        Check(error.find("Video") != std::string::npos,
            "normalization failure is an explicit video-processor capability error");
        std::cout << "WARP pixel assertions skipped: " << error << '\n';
        return;
    }

    D3D11_TEXTURE2D_DESC staging_description{};
    staging_description.Width = 4;
    staging_description.Height = 2;
    staging_description.MipLevels = 1;
    staging_description.ArraySize = 1;
    staging_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    staging_description.SampleDesc.Count = 1;
    staging_description.Usage = D3D11_USAGE_STAGING;
    staging_description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    winrt::com_ptr<ID3D11Texture2D> staging;
    result = device->CreateTexture2D(&staging_description, nullptr, staging.put());
    Check(SUCCEEDED(result) && staging, "WARP staging texture was created");
    context->CopyResource(staging.get(), normalizer.Texture());
    context->Flush();
    D3D11_MAPPED_SUBRESOURCE mapped{};
    result = context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped);
    Check(SUCCEEDED(result), "normalized canvas readback was mapped");
    const auto* row = static_cast<const uint8_t*>(mapped.pData);
    for (uint32_t y = 0; y < 2; ++y) {
        row = static_cast<const uint8_t*>(mapped.pData) + y * mapped.RowPitch;
        for (uint32_t x = 0; x < 4; ++x) {
            const uint8_t* pixel = row + x * 4;
            Check(pixel[2] > 180 && pixel[1] < 80,
                "padding pixels outside ContentSize never enter the canvas");
        }
    }
    context->Unmap(staging.get(), 0);
}

}  // namespace

int RunWgcWindowLifecycleTests() {
    WindowStateClassification();
    FrameBoundsClassification();
    RecreateAttemptsResetOnlyAfterStableProgress();
    RecreateFailureClassification();
    WinRtApartmentGuardBalancesNestedMtaInitialization();
    WinRtApartmentGuardPreservesConflictingSta();
    ResizeEncoderGeometryPolicy();
    CloseSignalIsGenerationLocal();
    WARPValidRectCopyExcludesTexturePadding();
    WARPNormalizationUsesOnlyContentSize();
    std::cout << "WGC window lifecycle tests: " << checks
              << " checks passed\n";
    return checks;
}

#if defined(WGC_LIFECYCLE_STANDALONE)
int main() {
    (void)RunWgcWindowLifecycleTests();
    return 0;
}
#endif
