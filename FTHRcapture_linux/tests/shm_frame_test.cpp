#include "shm_frame.h"

#include <array>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <vector>

#include <wayland-client-protocol.h>

extern "C" {
#include <libavutil/pixfmt.h>
}

namespace {

constexpr uint32_t kWidth = 3;
constexpr uint32_t kHeight = 2;

// Distinct red, green and blue per pixel so a channel swap or a row flip is
// visible in the output.
struct Rgb {
    uint8_t r, g, b;
};
constexpr std::array<Rgb, kWidth * kHeight> kPixels{{
    {200, 10, 20}, {30, 150, 40}, {50, 60, 250},
    {90, 80, 70},  {5, 6, 7},     {255, 128, 0},
}};

// Builds a buffer with the given bytes-per-pixel writer and padded rows.
template <typename Write>
std::vector<uint8_t> MakeBuffer(uint32_t stride, bool y_invert, Write write) {
    std::vector<uint8_t> buffer(static_cast<size_t>(stride) * kHeight, 0xEE);
    for (uint32_t y = 0; y < kHeight; ++y) {
        const uint32_t stored_row = y_invert ? kHeight - 1 - y : y;
        for (uint32_t x = 0; x < kWidth; ++x)
            write(&buffer[stored_row * stride + x * 4], kPixels[y * kWidth + x]);
    }
    return buffer;
}

void ExpectBgr(const fthr::RawFrame& frame) {
    assert(frame.width == kWidth && frame.height == kHeight);
    assert(frame.av_pix_fmt == AV_PIX_FMT_BGR0 || frame.av_pix_fmt == AV_PIX_FMT_BGRA);
    for (uint32_t y = 0; y < kHeight; ++y) {
        for (uint32_t x = 0; x < kWidth; ++x) {
            const uint8_t* p = frame.data + y * frame.stride + x * 4;
            const Rgb& want = kPixels[y * kWidth + x];
            assert(p[0] == want.b);
            assert(p[1] == want.g);
            assert(p[2] == want.r);
        }
    }
}

void TestFormatMapping() {
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_XRGB8888) == AV_PIX_FMT_BGR0);
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_ARGB8888) == AV_PIX_FMT_BGRA);
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_XBGR8888) == AV_PIX_FMT_RGB0);
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_XRGB2101010) == AV_PIX_FMT_X2RGB10LE);
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_XBGR2101010) == AV_PIX_FMT_X2BGR10LE);
    assert(fthr::WlShmFormatToAvPixFmt(WL_SHM_FORMAT_RGB565) == -1);
}

void TestFormatPreference() {
    uint32_t chosen = 0;
    // The old ext backend took the first offer; an RGB-order or 10-bit
    // format listed first must not win over plain XRGB8888.
    assert(fthr::PickWlShmFormat(
        {WL_SHM_FORMAT_XBGR2101010, WL_SHM_FORMAT_ABGR8888, WL_SHM_FORMAT_XRGB8888},
        chosen));
    assert(chosen == WL_SHM_FORMAT_XRGB8888);
    assert(fthr::PickWlShmFormat({WL_SHM_FORMAT_RGB565, WL_SHM_FORMAT_XBGR8888}, chosen));
    assert(chosen == WL_SHM_FORMAT_XBGR8888);
    assert(!fthr::PickWlShmFormat({WL_SHM_FORMAT_RGB565}, chosen));
    assert(!fthr::PickWlShmFormat({}, chosen));
}

void TestPassThrough() {
    constexpr uint32_t stride = kWidth * 4 + 4;
    auto buffer = MakeBuffer(stride, false, [](uint8_t* p, const Rgb& c) {
        p[0] = c.b; p[1] = c.g; p[2] = c.r; p[3] = 0;
    });
    fthr::ShmFrameNormalizer normalizer;
    fthr::RawFrame frame;
    assert(normalizer.Normalize(buffer.data(), stride, kWidth, kHeight,
                                WL_SHM_FORMAT_XRGB8888, false, 42, frame));
    assert(frame.data == buffer.data());   // no copy for the native layout
    assert(frame.stride == stride);
    assert(frame.timestamp_ns == 42);
    ExpectBgr(frame);
}

void TestRgbOrderIsSwapped() {
    constexpr uint32_t stride = kWidth * 4;
    auto buffer = MakeBuffer(stride, false, [](uint8_t* p, const Rgb& c) {
        p[0] = c.r; p[1] = c.g; p[2] = c.b; p[3] = 0;
    });
    fthr::ShmFrameNormalizer normalizer;
    fthr::RawFrame frame;
    assert(normalizer.Normalize(buffer.data(), stride, kWidth, kHeight,
                                WL_SHM_FORMAT_XBGR8888, false, 0, frame));
    assert(frame.av_pix_fmt == AV_PIX_FMT_BGR0);
    ExpectBgr(frame);
}

void TestYInvertIsFlipped() {
    constexpr uint32_t stride = kWidth * 4 + 8;
    auto buffer = MakeBuffer(stride, true, [](uint8_t* p, const Rgb& c) {
        p[0] = c.b; p[1] = c.g; p[2] = c.r; p[3] = 0;
    });
    fthr::ShmFrameNormalizer normalizer;
    fthr::RawFrame frame;
    assert(normalizer.Normalize(buffer.data(), stride, kWidth, kHeight,
                                WL_SHM_FORMAT_XRGB8888, true, 0, frame));
    ExpectBgr(frame);
}

void TestTenBitIsConverted() {
    constexpr uint32_t stride = kWidth * 4;
    // XRGB2101010: x:2 r:10 g:10 b:10, little-endian. Expand 8-bit values to
    // 10 bits by shifting; converting back must land within 1 of the input.
    auto buffer = MakeBuffer(stride, false, [](uint8_t* p, const Rgb& c) {
        const uint32_t word = (uint32_t{c.r} << 22) | (uint32_t{c.g} << 12) |
                              (uint32_t{c.b} << 2);
        p[0] = word & 0xFF; p[1] = (word >> 8) & 0xFF;
        p[2] = (word >> 16) & 0xFF; p[3] = (word >> 24) & 0xFF;
    });
    fthr::ShmFrameNormalizer normalizer;
    fthr::RawFrame frame;
    assert(normalizer.Normalize(buffer.data(), stride, kWidth, kHeight,
                                WL_SHM_FORMAT_XRGB2101010, false, 0, frame));
    for (uint32_t i = 0; i < kWidth * kHeight; ++i) {
        const uint8_t* p = frame.data + (i / kWidth) * frame.stride + (i % kWidth) * 4;
        const Rgb& want = kPixels[i];
        assert(p[0] + 1 >= want.b && p[0] <= want.b + 1);
        assert(p[1] + 1 >= want.g && p[1] <= want.g + 1);
        assert(p[2] + 1 >= want.r && p[2] <= want.r + 1);
    }
}

void TestRejectsBadInput() {
    std::vector<uint8_t> buffer(kWidth * kHeight * 4, 0);
    fthr::ShmFrameNormalizer normalizer;
    fthr::RawFrame frame;
    assert(!normalizer.Normalize(buffer.data(), kWidth * 4, kWidth, kHeight,
                                 WL_SHM_FORMAT_RGB565, false, 0, frame));
    assert(!normalizer.Normalize(buffer.data(), kWidth * 4 - 1, kWidth, kHeight,
                                 WL_SHM_FORMAT_XRGB8888, false, 0, frame));
    assert(!normalizer.Normalize(nullptr, kWidth * 4, kWidth, kHeight,
                                 WL_SHM_FORMAT_XRGB8888, false, 0, frame));
}

} // namespace

int main() {
    TestFormatMapping();
    TestFormatPreference();
    TestPassThrough();
    TestRgbOrderIsSwapped();
    TestYInvertIsFlipped();
    TestTenBitIsConverted();
    TestRejectsBadInput();
    std::cout << "shm frame normalisation tests passed" << std::endl;
    return 0;
}
