#pragma once

#include <dxgi1_2.h>
#include <d3d11.h>

#include <cstdint>
#include <string>
#include <vector>

namespace fthr::dxgi_cursor {

// Both clean replay images and cursor-composed images feed the video processor.
inline constexpr UINT kReplayTextureBindFlags =
    D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;


// Desktop Duplication reports the cursor position in output-local top-left
// coordinates.  The cursor compositor deliberately keeps that contract; the
// hotspot is already accounted for by DXGI and must not be subtracted again.
class Composer final {
public:
    static constexpr uint32_t kMaxShapeWidth = 2048;
    static constexpr uint32_t kMaxShapeHeight = 2048;
    static constexpr uint64_t kMaxShapeBytes = 16ull * 1024ull * 1024ull;

    Composer() = default;
    ~Composer();

    Composer(const Composer&) = delete;
    Composer& operator=(const Composer&) = delete;

    bool Initialize(ID3D11Device* device, ID3D11DeviceContext* context,
        uint32_t output_width, uint32_t output_height, std::string& detail);
    void Reset() noexcept;
    void InvalidateGeneration() noexcept;
    void InvalidateBackground() noexcept;

    // Must be called for every successful AcquireNextFrame before ReleaseFrame.
    // Shape and position state is retained only for the current duplication
    // generation.  A cursor API failure is fatal to the current DXGI capture;
    // the caller must release the frame and enter its normal recovery path.
    bool ObserveFrame(IDXGIOutputDuplication* duplication,
        const DXGI_OUTDUPL_FRAME_INFO& frame_info, std::string& detail);

    // Composes the currently visible cursor over a clean owned background.
    // When no cursor needs drawing, composed_texture receives background itself.
    // The returned pointer is borrowed and remains valid until the next reset.
    bool Compose(ID3D11Texture2D* background,
        ID3D11Texture2D** composed_texture, std::string& detail);

    bool HasShape() const noexcept { return shape_ready_; }
    bool Visible() const noexcept { return visible_; }
    int64_t LastMouseUpdateTime() const noexcept { return last_update_time_; }

private:
    enum class ShapeMode : uint32_t {
        Color = 0,
        MaskedColor = 1,
        Monochrome = 2,
    };

    bool FetchShape(IDXGIOutputDuplication* duplication,
        uint32_t reported_size, std::string& detail);
    bool ValidateShape(const DXGI_OUTDUPL_POINTER_SHAPE_INFO& info,
        uint32_t buffer_size, std::string& detail) const;
    bool BuildNormalizedShape(const DXGI_OUTDUPL_POINTER_SHAPE_INFO& info,
        const std::vector<uint8_t>& source, std::string& detail);
    bool EnsureOutputResources(uint32_t width, uint32_t height,
        DXGI_FORMAT format, std::string& detail);
    bool EnsureBackgroundView(ID3D11Texture2D* background,
        std::string& detail);
    bool EnsureCursorTexture(std::string& detail);
    bool EnsurePipeline(std::string& detail);
    void ReleaseComResources() noexcept;

    ID3D11Device* device_ = nullptr;
    ID3D11DeviceContext* context_ = nullptr;
    ID3D11Texture2D* output_texture_ = nullptr;
    ID3D11RenderTargetView* output_rtv_ = nullptr;
    ID3D11ShaderResourceView* background_srv_ = nullptr;
    ID3D11Texture2D* background_source_ = nullptr;
    ID3D11Texture2D* cursor_texture_ = nullptr;
    ID3D11ShaderResourceView* cursor_srv_ = nullptr;
    ID3D11VertexShader* vertex_shader_ = nullptr;
    ID3D11PixelShader* pixel_shader_ = nullptr;
    ID3D11SamplerState* sampler_ = nullptr;
    ID3D11Buffer* constants_ = nullptr;

    uint32_t output_width_ = 0;
    uint32_t output_height_ = 0;
    DXGI_FORMAT output_format_ = DXGI_FORMAT_UNKNOWN;

    std::vector<uint8_t> normalized_shape_;
    uint32_t shape_width_ = 0;
    uint32_t shape_height_ = 0;
    ShapeMode shape_mode_ = ShapeMode::Color;
    bool shape_ready_ = false;
    bool visible_ = false;
    POINT position_{};
    int64_t last_update_time_ = 0;
};

}  // namespace fthr::dxgi_cursor
