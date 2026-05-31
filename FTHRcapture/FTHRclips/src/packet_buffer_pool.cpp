// packet_buffer_pool.cpp
// FTHR Capture Engine - Packet buffer pool implementation

#include "packet_buffer_pool.h"
#include <iostream>


namespace fthr {


    // ---------------------------------------------------------------------------
    // Construction
    // ---------------------------------------------------------------------------

    PacketBufferPool::PacketBufferPool(size_t pool_size, size_t buffer_capacity)
        : pool_size_(pool_size)
        , buffer_capacity_(buffer_capacity)
        , free_list_(0)
        , tag_counter_(0)
    {
        nodes_.resize(pool_size_);
        buffers_.reserve(pool_size_);

        for (size_t i = 0; i < pool_size_; ++i) {
            buffers_.emplace_back();
            buffers_.back().reserve(buffer_capacity_);
        }

        // Build free list in reverse so node 0 is at the head
        Node* head = nullptr;
        for (int i = static_cast<int>(pool_size_) - 1; i >= 0; --i) {
            nodes_[i].buffer = &buffers_[i];
            nodes_[i].next = head;
            head = &nodes_[i];
        }

        free_list_.store(TaggedPointer::Pack(head, 0).value, std::memory_order_relaxed);

        std::cout << "[PacketBufferPool] Initialized: " << pool_size_
            << " buffers x " << buffer_capacity_ << " bytes ("
            << (pool_size_ * buffer_capacity_ / (1024 * 1024)) << " MB)"
            << std::endl;
    }

    PacketBufferPool::~PacketBufferPool() {
        // buffers_ vector owns all memory - automatic cleanup
    }


    // ---------------------------------------------------------------------------
    // Acquire - lock-free pop
    // ---------------------------------------------------------------------------

    PacketBufferPool::PooledBuffer PacketBufferPool::Acquire() {
        while (true) {
            uint64_t raw = free_list_.load(std::memory_order_acquire);
            TaggedPointer old_head(raw);
            Node* node = old_head.GetPointer();

            if (!node) {
                // Pool exhausted - emergency heap allocation
                std::cerr << "[PacketBufferPool] WARNING: pool exhausted, heap allocating"
                    << std::endl;
                auto* buf = new std::vector<uint8_t>();
                buf->reserve(buffer_capacity_);
                return PooledBuffer(buf, this);
            }

            uint16_t new_tag = static_cast<uint16_t>(old_head.GetTag() + 1u);
            uint64_t new_raw = TaggedPointer::Pack(node->next, new_tag).value;

            // CAS: swap head from old_head -> new_head
            // On failure, raw is updated to the current atomic value (ready for retry)
            if (free_list_.compare_exchange_weak(
                raw, new_raw,
                std::memory_order_release,
                std::memory_order_acquire))
            {
                node->buffer->clear();
                return PooledBuffer(node->buffer, this);
            }
            // CAS failed - raw now holds the current value; loop retries
        }
    }


    // ---------------------------------------------------------------------------
    // Release - lock-free push
    // ---------------------------------------------------------------------------

    void PacketBufferPool::Release(std::vector<uint8_t>* buffer) {
        if (!buffer) return;

        // Determine whether this is a pooled or emergency-allocated buffer
        bool is_pooled = false;
        for (auto& b : buffers_) {
            if (&b == buffer) { is_pooled = true; break; }
        }

        if (!is_pooled) {
            delete buffer;
            return;
        }

        size_t index = static_cast<size_t>(buffer - &buffers_[0]);
        assert(index < pool_size_);
        Node* node = &nodes_[index];

        while (true) {
            uint64_t raw = free_list_.load(std::memory_order_acquire);
            TaggedPointer old_head(raw);

            node->next = old_head.GetPointer();

            uint16_t new_tag = static_cast<uint16_t>(old_head.GetTag() + 1u);
            uint64_t new_raw = TaggedPointer::Pack(node, new_tag).value;

            if (free_list_.compare_exchange_weak(
                raw, new_raw,
                std::memory_order_release,
                std::memory_order_acquire))
            {
                return;
            }
            // CAS failed - raw updated; retry
        }
    }


    // ---------------------------------------------------------------------------
    // GetAvailableCount - diagnostic only, inherently racy
    // ---------------------------------------------------------------------------

    size_t PacketBufferPool::GetAvailableCount() const {
        uint64_t raw = free_list_.load(std::memory_order_relaxed);
        TaggedPointer head(raw);
        Node* node = head.GetPointer();

        size_t count = 0;
        while (node) {
            ++count;
            node = node->next;
        }
        return count;
    }


} // namespace fthr