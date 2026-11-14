#pragma once
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

namespace fthr {

// Close-time update of a small, preallocated fragmented-MP4 moov. Sample bytes
// and offsets never move. Unused reserved tracks and trex entries become free
// boxes; used audio handlers receive their real source identity. This permits
// new programs during recording without a whole-file remux at Stop.
inline bool FinalizeMp4AudioHeaders(const std::filesystem::path& path,
                                    const std::map<uint32_t, std::string>& handlers,
                                    bool remove_unused = true) {
    std::fstream file(path, std::ios::in | std::ios::out | std::ios::binary);
    if (!file) return false;
    const auto be32 = [](const uint8_t* bytes) {
        return (uint32_t(bytes[0]) << 24) | (uint32_t(bytes[1]) << 16)
            | (uint32_t(bytes[2]) << 8) | uint32_t(bytes[3]);
    };
    file.seekg(0, std::ios::end);
    const uint64_t file_size = static_cast<uint64_t>(file.tellg());
    uint64_t position = 0;
    while (position + 8 <= file_size) {
        uint8_t header[16]{};
        file.seekg(static_cast<std::streamoff>(position));
        file.read(reinterpret_cast<char*>(header), 8);
        if (!file) return false;
        uint64_t size = be32(header);
        size_t prefix = 8;
        if (size == 1) {
            file.read(reinterpret_cast<char*>(header + 8), 8);
            size = (uint64_t(be32(header + 8)) << 32) | be32(header + 12);
            prefix = 16;
        } else if (size == 0) size = file_size - position;
        if (size < prefix || size > file_size - position) return false;
        if (std::memcmp(header + 4, "moov", 4) != 0) { position += size; continue; }
        if (size > 8 * 1024 * 1024) return false;
        std::vector<uint8_t> moov(static_cast<size_t>(size));
        file.seekg(static_cast<std::streamoff>(position));
        file.read(reinterpret_cast<char*>(moov.data()), static_cast<std::streamsize>(moov.size()));
        if (!file) return false;
        const auto children = [&](size_t begin, size_t end) {
            std::vector<std::pair<size_t, size_t>> boxes;
            for (size_t cursor = begin; cursor + 8 <= end;) {
                const size_t length = be32(moov.data() + cursor);
                if (length < 8 || length > end - cursor) return std::vector<std::pair<size_t, size_t>>{};
                boxes.emplace_back(cursor, cursor + length); cursor += length;
                if (cursor == end) return boxes;
            }
            return std::vector<std::pair<size_t, size_t>>{};
        };
        size_t updated = 0;
        bool video_found = false;
        const auto moov_children = children(prefix, moov.size());
        if (moov_children.empty()) return false;
        for (const auto& [begin, end] : moov_children) {
            if (std::memcmp(moov.data() + begin + 4, "trak", 4) == 0) {
                uint32_t id = 0;
                size_t handler = 0, handler_end = 0;
                for (const auto& [child, child_end] : children(begin + 8, end)) {
                    if (std::memcmp(moov.data() + child + 4, "tkhd", 4) == 0) {
                        const size_t field = child + (moov[child + 8] == 1 ? 28 : 20);
                        if (field + 4 > child_end) return false;
                        id = be32(moov.data() + field);
                    } else if (std::memcmp(moov.data() + child + 4, "mdia", 4) == 0) {
                        for (const auto& [media, media_end] : children(child + 8, child_end)) {
                            if (std::memcmp(moov.data() + media + 4, "hdlr", 4) == 0
                                    && media + 32 < media_end
                                    && std::memcmp(moov.data() + media + 16, "soun", 4) == 0) {
                                handler = media + 32; handler_end = media_end;
                            }
                        }
                    }
                }
                if (id == 1) { video_found = true; continue; }
                if (id < 2 || !handler) return false;
                const auto found = handlers.find(id);
                if (found == handlers.end()) {
                    if (remove_unused) std::memcpy(moov.data() + begin + 4, "free", 4);
                } else {
                    if (found->second.size() + 1 > handler_end - handler) return false;
                    std::memset(moov.data() + handler, ' ', handler_end - handler);
                    std::memcpy(moov.data() + handler, found->second.data(), found->second.size());
                    moov[handler_end - 1] = 0;
                    ++updated;
                }
            } else if (std::memcmp(moov.data() + begin + 4, "mvex", 4) == 0) {
                for (const auto& [child, child_end] : children(begin + 8, end)) {
                    if (std::memcmp(moov.data() + child + 4, "trex", 4) == 0 && child + 16 <= child_end) {
                        const uint32_t id = be32(moov.data() + child + 12);
                        if (remove_unused && id != 1 && !handlers.count(id))
                            std::memcpy(moov.data() + child + 4, "free", 4);
                    }
                }
            }
        }
        if (!video_found || updated != handlers.size()) return false;
        file.seekp(static_cast<std::streamoff>(position));
        file.write(reinterpret_cast<const char*>(moov.data()), static_cast<std::streamsize>(moov.size()));
        file.flush();
        return file.good();
    }
    return false;
}

} // namespace fthr
