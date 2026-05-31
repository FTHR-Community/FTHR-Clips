// packet_buffer_pool.h
// FTHR Capture Engine - Lock-free pre-allocated packet buffer pool
//
// Eliminates per-frame malloc/free in the x264 encode path.
// Pre-allocates pool_size buffers at construction - zero heap activity
// during normal operation after warm-up.
//
// Acquire() - lock-free CAS pop from free-list, returns PooledBuffer RAII handle
// Release() - lock-free CAS push back onto free-list (called by ~PooledBuffer)
//
// ABA protection: 16-bit tag packed into the free-list atomic value.
// free_list_ is std::atomic<uint64_t> (guaranteed lock-free on MSVC x64).
// TaggedPointer is a helper struct that packs/unpacks that value - it is
// NOT stored inside the atomic, avoiding std::atomic<UserStruct> portability issues.

#pragma once
#ifndef FTHR_PACKET_BUFFER_POOL_H
#define FTHR_PACKET_BUFFER_POOL_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <cstdint>
#include <cstddef>
#include <vector>
#include <atomic>
#include <cassert>


namespace fthr {


    class PacketBufferPool {
    public:
        explicit PacketBufferPool(size_t pool_size = 128,
            size_t buffer_capacity = 512 * 1024);
        ~PacketBufferPool();

        PacketBufferPool(const PacketBufferPool&) = delete;
        PacketBufferPool& operator=(const PacketBufferPool&) = delete;


        // -----------------------------------------------------------------------
        // PooledBuffer - RAII handle returned by Acquire().
        // Returns the buffer to the pool automatically on destruction.
        // Move-only (mirrors unique_ptr semantics).
        // -----------------------------------------------------------------------
        class PooledBuffer {
        public:
            PooledBuffer() noexcept : buffer_(nullptr), pool_(nullptr) {}

            PooledBuffer(std::vector<uint8_t>* buf, PacketBufferPool* pool) noexcept
                : buffer_(buf), pool_(pool) {
            }

            ~PooledBuffer() {
                if (pool_ && buffer_)
                    pool_->Release(buffer_);
            }

            PooledBuffer(PooledBuffer&& o) noexcept
                : buffer_(o.buffer_), pool_(o.pool_) {
                o.buffer_ = nullptr;
                o.pool_ = nullptr;
            }

            PooledBuffer& operator=(PooledBuffer&& o) noexcept {
                if (this != &o) {
                    if (pool_ && buffer_) pool_->Release(buffer_);
                    buffer_ = o.buffer_;
                    pool_ = o.pool_;
                    o.buffer_ = nullptr;
                    o.pool_ = nullptr;
                }
                return *this;
            }

            PooledBuffer(const PooledBuffer&) = delete;
            PooledBuffer& operator=(const PooledBuffer&) = delete;

            std::vector<uint8_t>* operator->() const noexcept { return buffer_; }
            std::vector<uint8_t>& operator*()  const noexcept { return *buffer_; }
            explicit operator bool()           const noexcept { return buffer_ != nullptr; }

            // Relinquish ownership. Caller must call pool->Release() on the returned pointer.
            std::vector<uint8_t>* release() noexcept {
                auto* p = buffer_;
                buffer_ = nullptr;
                pool_ = nullptr;
                return p;
            }

        private:
            std::vector<uint8_t>* buffer_;
            PacketBufferPool* pool_;
        };


        // Pop one buffer from the free-list (lock-free).
        PooledBuffer Acquire();

        // Push buffer back onto the free-list (lock-free).
        // Called automatically by ~PooledBuffer.
        void Release(std::vector<uint8_t>* buffer);

        // Approximate count of available buffers. Diagnostic only - inherently racy.
        size_t GetAvailableCount() const;


    private:
        // -----------------------------------------------------------------------
        // Intrusive singly-linked list node
        // -----------------------------------------------------------------------
        struct Node {
            std::vector<uint8_t>* buffer = nullptr;
            Node* next = nullptr;
        };


        // -----------------------------------------------------------------------
        // TaggedPointer
        //
        // Packs a Node* and a 16-bit ABA counter into one uint64_t value.
        // Used as a value type only - the actual atomic is std::atomic<uint64_t>
        // which is guaranteed lock-free and trivially supported by all MSVC versions.
        //
        //  bits [63:16]  Node*     (pointer shifted left 16)
        //  bits [15: 0]  uint16_t  ABA tag
        // -----------------------------------------------------------------------
        struct TaggedPointer {
            uint64_t value;

            TaggedPointer() noexcept : value(0) {}
            explicit TaggedPointer(uint64_t v) noexcept : value(v) {}

            static TaggedPointer Pack(Node* ptr, uint16_t tag) noexcept {
                return TaggedPointer(
                    (reinterpret_cast<uint64_t>(ptr) << 16) | static_cast<uint64_t>(tag)
                );
            }

            Node* GetPointer() const noexcept {
                return reinterpret_cast<Node*>(value >> 16);
            }

            uint16_t GetTag() const noexcept {
                return static_cast<uint16_t>(value & 0xFFFFu);
            }
        };


        // -----------------------------------------------------------------------
        // Members
        // -----------------------------------------------------------------------
        const size_t pool_size_;
        const size_t buffer_capacity_;

        std::vector<Node>                 nodes_;
        std::vector<std::vector<uint8_t>> buffers_;

        // Free-list stored as raw uint64_t.
        // std::atomic<uint64_t> is lock-free on all x86-64 platforms/compilers.
        // TaggedPointer.value is packed into / unpacked from this field.
        std::atomic<uint64_t> free_list_;

        std::atomic<uint16_t> tag_counter_;
    };


} // namespace fthr

#endif // FTHR_PACKET_BUFFER_POOL_H