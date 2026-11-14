#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <algorithm>

namespace fthr::detail {

inline bool CanDirectCopyBgra(const D3D11_TEXTURE2D_DESC& source, UINT source_subresource,
                              const D3D11_TEXTURE2D_DESC& target, UINT target_subresource) noexcept {
    if (source.Format != DXGI_FORMAT_B8G8R8A8_UNORM || target.Format != source.Format
        || source.SampleDesc.Count != 1 || target.SampleDesc.Count != 1
        || source.SampleDesc.Quality != 0 || target.SampleDesc.Quality != 0
        || !source.Width || !source.Height || !target.Width || !target.Height
        || !source.MipLevels || !target.MipLevels || !source.ArraySize || !target.ArraySize
        || source_subresource / source.MipLevels >= source.ArraySize
        || target_subresource / target.MipLevels >= target.ArraySize) return false;
    const UINT source_mip = source_subresource % source.MipLevels;
    const UINT target_mip = target_subresource % target.MipLevels;
    if (source_mip >= 32 || target_mip >= 32) return false;
    return std::max(1u, source.Width >> source_mip) == std::max(1u, target.Width >> target_mip)
        && std::max(1u, source.Height >> source_mip) == std::max(1u, target.Height >> target_mip);
}

} // namespace fthr::detail
