#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <DirectXPackedVector.h>
#include <cstring>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>

namespace fthr {

struct ContentSampleMetrics { uint64_t sum = 0, sum_sq = 0; uint32_t count = 0; };
struct ContentSamplerStats {
    uint64_t calls = 0, queued = 0, completed = 0, busy = 0, skipped = 0, errors = 0;
    uint64_t max_call_ns = 0;
};

// Diagnostic sampling only: two tiny readbacks, queried without flushing and
// mapped without waiting. A busy GPU can delay/skip evidence, never the encoder.
class AsyncContentSampler {
public:
    using Publish = void (*)(void*, const ContentSampleMetrics&);
    ~AsyncContentSampler() { Reset(); }
    AsyncContentSampler() = default;
    AsyncContentSampler(const AsyncContentSampler&) = delete;
    AsyncContentSampler& operator=(const AsyncContentSampler&) = delete;

    void Tick(ID3D11Device* device, ID3D11DeviceContext* context,
              ID3D11Texture2D* source, bool enqueue, void* owner, Publish publish) {
        const auto started = std::chrono::steady_clock::now();
        std::lock_guard<std::mutex> lock(mutex_);
        struct Duration {
            ContentSamplerStats& stats;
            std::chrono::steady_clock::time_point started;
            ~Duration() {
                ++stats.calls;
                const auto ns = static_cast<uint64_t>(std::chrono::duration_cast<
                    std::chrono::nanoseconds>(std::chrono::steady_clock::now() - started).count());
                stats.max_call_ns = std::max(stats.max_call_ns, ns);
            }
        } duration{stats_, started};
        if (!device || !context || !source) return;
        if (device_ != device) { ReleaseLocked(); device_ = device; }
        // Reset() shares this lock with publication, so recovery cannot clear
        // its streak and then receive a result from an older source epoch.
        for (auto& slot : slots_) {
            if (!slot.pending) continue;
            const HRESULT ready = context->GetData(slot.completion, nullptr, 0,
                                                   D3D11_ASYNC_GETDATA_DONOTFLUSH);
            if (ready == S_FALSE) { ++stats_.busy; continue; }
            if (FAILED(ready)) { slot.pending = false; ++stats_.errors; continue; }
            D3D11_MAPPED_SUBRESOURCE mapped{};
            const HRESULT result = context->Map(slot.staging, 0, D3D11_MAP_READ,
                                                D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped);
            if (result == DXGI_ERROR_WAS_STILL_DRAWING) { ++stats_.busy; continue; }
            slot.pending = false;
            if (FAILED(result)) { ++stats_.errors; continue; }
            ContentSampleMetrics metrics;
            const bool scrgb = slot.format == DXGI_FORMAT_R16G16B16A16_FLOAT;
            const UINT pixel_bytes = scrgb ? 8 : 4;
            if (mapped.pData && mapped.RowPitch >= kColumns * pixel_bytes) {
                for (UINT row = 0; row < kRows; ++row) {
                    const auto* bytes = static_cast<const uint8_t*>(mapped.pData)
                        + static_cast<size_t>(row) * mapped.RowPitch;
                    for (UINT column = 0; column < kColumns; ++column) {
                        const auto* pixel = bytes + column * pixel_bytes;
                        uint32_t luma;
                        if (scrgb) {
                            uint16_t rgb[3];
                            std::memcpy(rgb, pixel, sizeof(rgb));
                            using DirectX::PackedVector::XMConvertHalfToFloat;
                            const float linear = 0.2126f * XMConvertHalfToFloat(rgb[0])
                                + 0.7152f * XMConvertHalfToFloat(rgb[1])
                                + 0.0722f * XMConvertHalfToFloat(rgb[2]);
                            luma = static_cast<uint32_t>(
                                std::min(1.f, std::max(0.f, linear)) * 255.f);
                        } else {
                            luma = (19u * pixel[0] + 183u * pixel[1]
                                    + 54u * pixel[2]) >> 8;
                        }
                        metrics.sum += luma;
                        metrics.sum_sq += luma * luma;
                        ++metrics.count;
                    }
                }
            }
            context->Unmap(slot.staging, 0);
            if (metrics.count == kColumns * kRows) {
                ++stats_.completed;
                if (publish) publish(owner, metrics);
            } else ++stats_.errors;
        }
        if (!enqueue) return;
        D3D11_TEXTURE2D_DESC desc{};
        source->GetDesc(&desc);
        if (!desc.Width || !desc.Height
            || (desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM
                && desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT)
            || desc.SampleDesc.Count != 1) { ++stats_.skipped; return; }
        for (auto& slot : slots_) {
            if (slot.pending) continue;
            if (slot.staging && slot.format != desc.Format) {
                slot.staging->Release();
                slot.staging = nullptr;
            }
            if (!slot.staging) {
                D3D11_TEXTURE2D_DESC staging{};
                staging.Width = kColumns; staging.Height = kRows;
                staging.MipLevels = staging.ArraySize = staging.SampleDesc.Count = 1;
                staging.Format = desc.Format;
                staging.Usage = D3D11_USAGE_STAGING;
                staging.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                if (FAILED(device->CreateTexture2D(&staging, nullptr, &slot.staging))) {
                    ++stats_.errors; return;
                }
                slot.format = staging.Format;
            }
            if (!slot.completion) {
                D3D11_QUERY_DESC query{D3D11_QUERY_EVENT, 0};
                if (FAILED(device->CreateQuery(&query, &slot.completion))) {
                    ++stats_.errors; return;
                }
            }
            for (UINT row = 0; row < kRows; ++row) {
                const UINT y = std::min(desc.Height - 1, (2 * row + 1) * desc.Height / (2 * kRows));
                for (UINT column = 0; column < kColumns; ++column) {
                    const UINT x = std::min(desc.Width - 1,
                        (2 * column + 1) * desc.Width / (2 * kColumns));
                    D3D11_BOX pixel{x, y, 0, x + 1, y + 1, 1};
                    context->CopySubresourceRegion(slot.staging, 0, column, row, 0, source, 0, &pixel);
                }
            }
            context->End(slot.completion);
            slot.pending = true;
            ++stats_.queued;
            return; // Never map the copy just enqueued on this tick.
        }
        ++stats_.skipped;
    }

    void Reset() noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        ReleaseLocked();
    }
    ContentSamplerStats Stats() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return stats_;
    }

private:
    static constexpr UINT kColumns = 16, kRows = 9;
    struct Slot {
        ID3D11Texture2D* staging = nullptr;
        ID3D11Query* completion = nullptr;
        DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
        bool pending = false;
    };
    void ReleaseLocked() noexcept {
        for (auto& slot : slots_) {
            if (slot.completion) slot.completion->Release();
            if (slot.staging) slot.staging->Release();
            slot = {};
        }
        device_ = nullptr;
    }
    std::array<Slot, 2> slots_{};
    ID3D11Device* device_ = nullptr; // Child resources retain the device.
    mutable std::mutex mutex_;
    ContentSamplerStats stats_{};
};

} // namespace fthr
