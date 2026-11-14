#pragma once

#include <atomic>
#include <algorithm>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
#include <string>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace fthr {

// Aggregate payload budget across sources, including snapshots still being
// saved. A full/failed disk fails the affected source; it never changes codec
// or silently shortens requested history. Files disappear with their last ref.
inline std::atomic<uint64_t> audio_spool_bytes{0};
inline constexpr uint64_t kAudioSpoolBudget = 4ull * 1024 * 1024 * 1024;

class AudioSpoolChunk {
public:
    AudioSpoolChunk() {
#ifdef _WIN32
        wchar_t directory[MAX_PATH + 1]{};
        const DWORD length = GetTempPathW(MAX_PATH, directory);
        if (!length || length >= MAX_PATH) return;
        static std::atomic<uint64_t> sequence{0};
        for (int attempt = 0; attempt < 64; ++attempt) {
            const auto path = std::wstring(directory) + L"fthr-audio-"
                + std::to_wstring(GetCurrentProcessId()) + L"-"
                + std::to_wstring(GetTickCount64()) + L"-"
                + std::to_wstring(sequence.fetch_add(1)) + L".tmp";
            file_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
            if (file_ != INVALID_HANDLE_VALUE) break;
        }
#else
        file_ = std::tmpfile();
#endif
    }
    ~AudioSpoolChunk() {
#ifdef _WIN32
        if (file_ != INVALID_HANDLE_VALUE) CloseHandle(file_);
#else
        if (file_) std::fclose(file_);
#endif
        audio_spool_bytes.fetch_sub(bytes_);
    }
    bool Append(const std::vector<uint8_t>& data, uint64_t& offset) {
        std::lock_guard<std::mutex> lock(mutex_);
        offset = bytes_;
        uint64_t used = audio_spool_bytes.load();
        do {
            if (used > kAudioSpoolBudget || data.size() > kAudioSpoolBudget - used) return false;
        } while (!audio_spool_bytes.compare_exchange_weak(used, used + data.size()));
        bool success = false;
#ifdef _WIN32
        LARGE_INTEGER position{}; position.QuadPart = static_cast<LONGLONG>(bytes_);
        DWORD written = 0;
        success = file_ != INVALID_HANDLE_VALUE && SetFilePointerEx(file_, position, nullptr, FILE_BEGIN)
            && WriteFile(file_, data.data(), static_cast<DWORD>(data.size()), &written, nullptr)
            && written == data.size();
#else
        success = file_ && std::fseek(file_, static_cast<long>(bytes_), SEEK_SET) == 0
            && std::fwrite(data.data(), 1, data.size(), file_) == data.size();
#endif
        if (!success) { audio_spool_bytes.fetch_sub(data.size()); return false; }
        bytes_ += data.size();
        return true;
    }
    std::shared_ptr<const std::vector<uint8_t>> Read(uint64_t offset, size_t size) const {
        auto bytes = std::make_shared<std::vector<uint8_t>>(size);
        std::lock_guard<std::mutex> lock(mutex_);
#ifdef _WIN32
        LARGE_INTEGER position{}; position.QuadPart = static_cast<LONGLONG>(offset);
        DWORD read = 0;
        if (!SetFilePointerEx(file_, position, nullptr, FILE_BEGIN)
                || !ReadFile(file_, bytes->data(), static_cast<DWORD>(size), &read, nullptr)
                || read != size) return {};
#else
        if (!file_ || std::fseek(file_, static_cast<long>(offset), SEEK_SET) != 0
                || std::fread(bytes->data(), 1, size, file_) != size) return {};
#endif
        return bytes;
    }
    uint64_t size() const { return bytes_; } // writer thread only
private:
    mutable std::mutex mutex_;
    uint64_t bytes_ = 0;
#ifdef _WIN32
    HANDLE file_ = INVALID_HANDLE_VALUE;
#else
    std::FILE* file_ = nullptr;
#endif
};

class AudioSpoolPayload {
public:
    explicit AudioSpoolPayload(std::vector<uint8_t> data)
        : memory_(std::make_shared<const std::vector<uint8_t>>(std::move(data))), size_(memory_->size()) {}
    explicit AudioSpoolPayload(size_t silent_bytes) : size_(silent_bytes), silence_(true) {}
    bool is_silence() const { return silence_; }
    size_t size() const { return size_; }
    std::shared_ptr<const std::vector<uint8_t>> Read() const {
        if (silence_) return std::make_shared<const std::vector<uint8_t>>(size_, 0);
        std::shared_ptr<AudioSpoolChunk> chunk;
        uint64_t offset;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (memory_) return memory_;
            chunk = chunk_; offset = offset_;
        }
        return chunk ? chunk->Read(offset, size_) : nullptr;
    }
    bool Spill(const std::shared_ptr<AudioSpoolChunk>& chunk) {
        auto memory = Read();
        uint64_t offset = 0;
        if (!memory || !chunk->Append(*memory, offset)) return false;
        std::lock_guard<std::mutex> lock(mutex_);
        chunk_ = chunk; offset_ = offset; memory_.reset();
        return true;
    }
private:
    mutable std::mutex mutex_;
    std::shared_ptr<const std::vector<uint8_t>> memory_;
    std::shared_ptr<AudioSpoolChunk> chunk_;
    uint64_t offset_ = 0;
    size_t size_;
    bool silence_ = false;
};

class AudioSpool {
public:
    AudioSpool() : worker_([this] { Run(); }) {}
    ~AudioSpool() {
        { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
        ready_.notify_one();
        worker_.join();
    }
    std::shared_ptr<AudioSpoolPayload> Push(std::vector<uint8_t> data) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!failed_ && !stopping_ && std::all_of(data.begin(), data.end(), [](uint8_t byte) { return byte == 0; }))
            return std::make_shared<AudioSpoolPayload>(data.size());
        if (failed_ || stopping_ || queued_bytes_ + data.size() > 4 * 1024 * 1024) {
            failed_ = true; return {};
        }
        auto payload = std::make_shared<AudioSpoolPayload>(std::move(data));
        queued_bytes_ += payload->size();
        queue_.push_back(payload);
        ready_.notify_one();
        return payload;
    }
    bool healthy() const { return !failed_.load(); }
private:
    void Run() {
        std::shared_ptr<AudioSpoolChunk> chunk;
        while (true) {
            std::shared_ptr<AudioSpoolPayload> payload;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
                if (queue_.empty()) return;
                payload = queue_.front(); queue_.pop_front();
                queued_bytes_ -= payload->size();
            }
            if (failed_) continue;
            if (!chunk || chunk->size() + payload->size() > 4 * 1024 * 1024)
                chunk = std::make_shared<AudioSpoolChunk>();
            if (!payload->Spill(chunk)) failed_ = true;
        }
    }
    std::atomic<bool> failed_{false};
    mutable std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<std::shared_ptr<AudioSpoolPayload>> queue_;
    size_t queued_bytes_ = 0;
    bool stopping_ = false;
    std::thread worker_;
};

} // namespace fthr
