// FTHR Capture Engine - SaveClipQueue implementation

#include "save_clip_task.h"
#include "audio_timeline.h"

#include <algorithm>
#include <iostream>


namespace fthr {


EncodedAudioTrack TakeEncodedAudioTrackForPresentationRange(
    const AudioSourceMetadata& source,
    const EncodedAudioPacketRing& ring,
    const AudioSourcePresentationRange& range) {
    // Preserve the signed source origin. A negative start represents either
    // leading silence before a late source or AAC encoder pre-roll.
    const int64_t start_pts_samples = range.start_pts_samples;
    const int64_t end_pts_samples = std::max<int64_t>(
        start_pts_samples + 1, range.end_pts_samples);

    EncodedAudioTrack track;
    track.source = source;
    track.snapshot = ring.TakeSnapshot(start_pts_samples, end_pts_samples);
    track.presentation_start_pts_samples = start_pts_samples;
    return track;
}


// SaveClipQueue construction

SaveClipQueue::SaveClipQueue()
    : shutdown_(false)
{
}

SaveClipQueue::~SaveClipQueue() {
    Shutdown();
}


// Push
//
// Adds a task to the queue and signals the SaveClipThread.
// Thread-safe, never blocks.

bool SaveClipQueue::Push(SaveClipTask&& task) {
    std::wstring log_path = task.output_path;
    uint32_t     log_id   = task.task_id;
    size_t       log_depth = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);

        if (shutdown_) {
            std::cerr << "[SaveClipQueue] Cannot push task - queue is shutting down"
                      << std::endl;
            return false;
        }
        if (queue_.size() >= kMaxPendingTasks) {
            std::cerr << "[SaveClipQueue] Cannot push task - pending queue is full ("
                      << queue_.size() << '/' << kMaxPendingTasks << ')'
                      << std::endl;
            return false;
        }

        queue_.push(std::move(task));
        log_depth = queue_.size();
    }

    cv_.notify_one();  // Wake the worker before potentially-slow logging.
    // Console/file logging may itself block. Keep it outside the queue lock so
    // the save worker can take ownership of the accepted task immediately.
    std::wcout << L"[SaveClipQueue] Task queued: "
               << log_path
               << L" (ID: " << log_id
               << L", queue depth: " << log_depth << L")"
               << std::endl;
    return true;
}


// Wait for a queued task or shutdown. Return false once shutdown is requested
// and the queue is empty.

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
    
    out_task = std::move(queue_.front());
    queue_.pop();
    
    return true;
}


// Shutdown
//
// Signals SaveClipThread to drain remaining tasks and exit.
// After this, Pop() will return false once the queue is empty.

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



bool SaveClipQueue::IsShutdown() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return shutdown_;
}



size_t SaveClipQueue::GetQueueDepth() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queue_.size();
}


} // namespace fthr
