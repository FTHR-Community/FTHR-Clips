#include "shared_memory.h"
#include <cstring>

namespace fthr {

    SharedMemory::SharedMemory() : file_mapping_(nullptr), layout_(nullptr) {}

    SharedMemory::~SharedMemory() { Shutdown(); }

    bool SharedMemory::Initialize(const wchar_t* name) {
        file_mapping_ = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr,
            PAGE_READWRITE, 0, sizeof(SharedMemoryLayout), name);

        if (!file_mapping_)
            file_mapping_ = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name);

        if (!file_mapping_) return false;

        layout_ = (SharedMemoryLayout*)MapViewOfFile(file_mapping_,
            FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SharedMemoryLayout));

        if (!layout_) {
            CloseHandle(file_mapping_);
            file_mapping_ = nullptr;
            return false;
        }

        if (!layout_->is_initialized) {
            memset(layout_, 0, sizeof(SharedMemoryLayout));
            layout_->is_initialized = true;
        }

        return true;
    }

    void SharedMemory::Shutdown() {
        if (layout_) {
            UnmapViewOfFile(layout_);
            layout_ = nullptr;
        }
        if (file_mapping_) {
            CloseHandle(file_mapping_);
            file_mapping_ = nullptr;
        }
    }

    void SharedMemory::SendCommand(CommandType cmd, uint32_t p1, uint32_t p2, uint32_t p3) {
        if (!layout_) return;
        layout_->ui_param1 = p1;
        layout_->ui_param2 = p2;
        layout_->ui_param3 = p3;
        layout_->ui_command = cmd;
    }

    bool SharedMemory::WaitForResponse(ResponseType expected, DWORD timeout_ms) {
        if (!layout_) return false;

        DWORD start = GetTickCount();
        while (layout_->engine_response != expected) {
            if (GetTickCount() - start > timeout_ms) return false;
            Sleep(1);
        }

        layout_->engine_response = ResponseType::NONE;
        return true;
    }

}