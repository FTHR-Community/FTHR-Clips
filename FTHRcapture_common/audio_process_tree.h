#pragma once
#include <cstdint>
#include <map>
#include <set>
#include <vector>

namespace fthr {

// INCLUDE process-loopback always includes descendants. Select disjoint roots
// and expose nesting as one explicit group instead of recording it twice.
template<class ParentLookup>
std::map<uint32_t, uint32_t> AudioProcessTreeOwners(
        const std::vector<uint32_t>& roots, ParentLookup parent) {
    const std::set<uint32_t> candidates(roots.begin(), roots.end());
    std::map<uint32_t, uint32_t> owners;
    for (auto root : roots) {
        uint32_t owner = root;
        uint32_t cursor = root;
        std::set<uint32_t> visited{root};
        for (int depth = 0; depth < 128; ++depth) {
            cursor = parent(cursor);
            if (!cursor) break;
            if (!visited.insert(cursor).second) {
                // PID reuse can make an old parent snapshot cyclic. Keep one
                // deterministic owner instead of discarding both groups.
                for (auto candidate : visited)
                    if (candidates.count(candidate) && candidate < owner) owner = candidate;
                break;
            }
            if (candidates.count(cursor)) owner = cursor;
        }
        owners[root] = owner;
    }
    return owners;
}

} // namespace fthr
