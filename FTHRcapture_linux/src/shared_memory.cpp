#include "shared_memory.h"
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <iostream>

namespace fthr {

bool SharedMemory::Initialize(const std::string& name) {
    name_ = "/" + name;

    shm_fd_ = shm_open(name_.c_str(), O_CREAT | O_RDWR, 0600);
    if (shm_fd_ < 0) {
        std::cerr << "[SHM] shm_open failed: " << strerror(errno) << std::endl;
        return false;
    }

    if (ftruncate(shm_fd_, sizeof(SharedMemoryLayout)) < 0) {
        std::cerr << "[SHM] ftruncate failed: " << strerror(errno) << std::endl;
        close(shm_fd_);
        shm_fd_ = -1;
        shm_unlink(name_.c_str());
        return false;
    }

    mapping_ = mmap(nullptr, sizeof(SharedMemoryLayout),
                    PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
    if (mapping_ == MAP_FAILED) {
        std::cerr << "[SHM] mmap failed: " << strerror(errno) << std::endl;
        close(shm_fd_);
        shm_fd_ = -1;
        shm_unlink(name_.c_str());
        return false;
    }

    layout_ = static_cast<SharedMemoryLayout*>(mapping_);
    memset(layout_, 0, sizeof(SharedMemoryLayout));
    std::cout << "[SHM] Created: /dev/shm" << name_ << std::endl;
    return true;
}

void SharedMemory::Shutdown() {
    if (mapping_ && mapping_ != MAP_FAILED) {
        munmap(mapping_, sizeof(SharedMemoryLayout));
        mapping_ = nullptr;
        layout_  = nullptr;
    }
    if (shm_fd_ >= 0) {
        close(shm_fd_);
        shm_fd_ = -1;
    }
    if (!name_.empty()) {
        shm_unlink(name_.c_str());
        name_.clear();
    }
}

} // namespace fthr
