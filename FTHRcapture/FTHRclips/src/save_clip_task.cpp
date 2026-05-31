// save_clip_task.cpp
// FTHR Capture Engine - SaveClipQueue implementation

#include "save_clip_task.h"
#include <iostream>


namespace fthr {


// ---------------------------------------------------------------------------
// SaveClipQueue construction
// ---------------------------------------------------------------------------

SaveClipQueue::SaveClipQueue()
    : shutdown_(false)
{
}

SaveClipQueue::~SaveClipQueue() {
    // Ensure shutdown was called
    Shutdown();
}


// ---------------------------------------------------------------------------
// Push
//
// Adds a task to the queue and signals the SaveClipThread.
// Thread-safe, never blocks.
// ---------------------------------------------------------------------------

void SaveClipQueue::Push(SaveClipTask&& task) {
    std::wstring log_path = task.output_path;
    uint32_t     log_id   = task.task_id;
    {
        std::lock_guard<std::mutex> lock(mutex_);

        if (shutdown_) {
            std::cerr << "[SaveClipQueue] Cannot push task - queue is shutting down"
                      << std::endl;
            return;
        }

        queue_.push(std::move(task));

        std::wcout << L"[SaveClipQueue] Task queued: "
                   << log_path
                   << L" (ID: " << log_id
                   << L", queue depth: " << queue_.size() << L")"
                   << std::endl;
    }

    cv_.notify_one();  // Wake up SaveClipThread
}


// ---------------------------------------------------------------------------
// Pop
//
// Removes and returns the next task from the queue.
// Blocks if queue is empty (waits for Push() or Shutdown()).
// Returns false if shutting down and queue is empty.
// ---------------------------------------------------------------------------

bool SaveClipQueue::Pop(SaveClipTask& out_task) {
    std::unique_lock<std::mutex> lock(mutex_);
    
    // Wait until there's a task or we're shutting down
    cv_.wait(lock, [this] {
        return !queue_.empty() || shutdown_;
    });
    
    // If shutdown and queue is empty, exit
    if (shutdown_ && queue_.empty()) {
        return false;
    }
    
    // Get the next task
    out_task = std::move(queue_.front());
    queue_.pop();
    
    return true;
}


// ---------------------------------------------------------------------------
// Shutdown
//
// Signals SaveClipThread to drain remaining tasks and exit.
// After this, Pop() will return false once the queue is empty.
// ---------------------------------------------------------------------------

void SaveClipQueue::Shutdown() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (shutdown_) return;  // Already shut down
        
        shutdown_ = true;
        std::cout << "[SaveClipQueue] Shutdown signaled. "
                  << "Remaining tasks in queue: " << queue_.size()
                  << std::endl;
    }
    
    cv_.notify_all();  // Wake up SaveClipThread
}


// ---------------------------------------------------------------------------
// IsShutdown
// ---------------------------------------------------------------------------

bool SaveClipQueue::IsShutdown() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return shutdown_;
}


// ---------------------------------------------------------------------------
// GetQueueDepth
// ---------------------------------------------------------------------------

size_t SaveClipQueue::GetQueueDepth() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queue_.size();
}


} // namespace fthr
