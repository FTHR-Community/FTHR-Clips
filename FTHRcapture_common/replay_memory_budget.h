// Byte admission for compressed video replay and every live save snapshot.
// Save admission cannot consume the capacity reserved for capture progress.
#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <memory>
#include <mutex>
#include <utility>

namespace fthr {

enum class ReplayMemoryUse { Ring, Save };

class ReplayMemoryBudget {
public:
    ReplayMemoryBudget(uint64_t total_bytes, uint64_t ring_bytes)
        : total_limit_(total_bytes), ring_limit_(std::min(total_bytes, ring_bytes)) {}

    bool TryAcquire(ReplayMemoryUse use, uint64_t bytes) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& used = use == ReplayMemoryUse::Ring ? ring_used_ : save_used_;
        const auto limit = use == ReplayMemoryUse::Ring
            ? ring_limit_ : total_limit_ - ring_limit_;
        if (bytes > limit - used) return false;
        used += bytes;
        return true;
    }
    void Release(ReplayMemoryUse use, uint64_t bytes) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& used = use == ReplayMemoryUse::Ring ? ring_used_ : save_used_;
        used -= bytes;
    }
    uint64_t Used(ReplayMemoryUse use) const {
        std::lock_guard<std::mutex> lock(mutex_);
        return use == ReplayMemoryUse::Ring ? ring_used_ : save_used_;
    }
    uint64_t total_limit() const { return total_limit_; }
    uint64_t ring_limit() const { return ring_limit_; }

private:
    const uint64_t total_limit_, ring_limit_;
    mutable std::mutex mutex_;
    uint64_t ring_used_ = 0, save_used_ = 0;
};

// Move-only: copying a lease must never authorize a second payload allocation.
class ReplayMemoryReservation {
public:
    ReplayMemoryReservation() = default;
    ReplayMemoryReservation(std::shared_ptr<ReplayMemoryBudget> budget, ReplayMemoryUse use)
        : budget_(std::move(budget)), use_(use) {}
    ~ReplayMemoryReservation() { Shrink(bytes_); }
    ReplayMemoryReservation(const ReplayMemoryReservation&) = delete;
    ReplayMemoryReservation& operator=(const ReplayMemoryReservation&) = delete;
    ReplayMemoryReservation(ReplayMemoryReservation&& other) noexcept
        : budget_(std::move(other.budget_)), use_(other.use_),
          bytes_(std::exchange(other.bytes_, 0)) {}
    ReplayMemoryReservation& operator=(ReplayMemoryReservation&& other) noexcept {
        if (this != &other) {
            Shrink(bytes_);
            budget_ = std::move(other.budget_);
            use_ = other.use_;
            bytes_ = std::exchange(other.bytes_, 0);
        }
        return *this;
    }
    bool Grow(uint64_t bytes) {
        if (bytes > std::numeric_limits<uint64_t>::max() - bytes_) return false;
        if (budget_ && !budget_->TryAcquire(use_, bytes)) return false;
        bytes_ += bytes;
        return true;
    }
    void Shrink(uint64_t bytes) {
        if (budget_ && bytes) budget_->Release(use_, bytes);
        bytes_ -= bytes;
    }
    // Used to merge already-admitted allocations within the same budget.
    void TransferFrom(ReplayMemoryReservation& other) {
        bytes_ += std::exchange(other.bytes_, 0);
    }
    uint64_t bytes() const { return bytes_; }

private:
    std::shared_ptr<ReplayMemoryBudget> budget_;
    ReplayMemoryUse use_ = ReplayMemoryUse::Save;
    uint64_t bytes_ = 0;
};

inline uint64_t ReplayAvailableMemoryBudget(uint64_t available_bytes) {
    constexpr uint64_t reserve = 256ULL * 1024 * 1024;
    return std::min(available_bytes - std::min(reserve, available_bytes / 2),
                    available_bytes / 4 * 3);
}

struct ReplayMemoryPlan {
    uint64_t total_bytes = 0;
    uint64_t ring_bytes = 0;
    uint64_t required_bytes = 0;
    bool fits = false;
};

// Admission estimate, not a promise about VBR encoder output. Runtime admission
// still checks actual vector capacities. Allow 25% bitrate headroom, metadata,
// transient packet growth, and one full save without reducing requested history.
inline ReplayMemoryPlan PlanReplayMemory(uint32_t seconds, uint32_t fps,
                                         uint32_t bitrate_kbps, uint32_t limit_mb,
                                         uint64_t available_bytes) {
    constexpr uint64_t mib = 1024 * 1024;
    ReplayMemoryPlan plan;
    plan.total_bytes = std::min(static_cast<uint64_t>(limit_mb) * mib,
                               ReplayAvailableMemoryBudget(available_bytes));
    // Production arguments are bounded, but keep arithmetic safe for callers.
    if (!seconds || !fps || !bitrate_kbps || seconds > 900 || fps > 360
        || bitrate_kbps > 200000) return plan;
    const uint64_t retained_seconds = static_cast<uint64_t>(seconds) + 5;
    const uint64_t payload = (retained_seconds * bitrate_kbps * 1000 + 7) / 8;
    const uint64_t metadata = retained_seconds * fps * 256;
    const uint64_t estimated_ring = payload + payload / 4 + metadata + 8 * mib;
    plan.required_bytes = 2 * estimated_ring;
    // Protect half for capture and allow VBR bursts to use that whole half,
    // rather than treating the startup bitrate estimate as a measured maximum.
    plan.ring_bytes = plan.total_bytes / 2;
    plan.fits = plan.required_bytes <= plan.total_bytes;
    return plan;
}

} // namespace fthr
