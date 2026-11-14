// Write audio sidecars from immutable SaveClipTask metadata.
// SHA-256 binds source UUIDs to the exact MP4 bytes without persisting
// live process handles or executable paths.

#pragma once
#ifndef FTHR_CLIP_AUDIO_MANIFEST_H
#define FTHR_CLIP_AUDIO_MANIFEST_H

#include "save_clip_task.h"

#include <filesystem>
#include <string>
#include <vector>

namespace fthr {

inline constexpr wchar_t kAudioManifestSuffix[] = L".fthr-audio.json";

std::filesystem::path AudioManifestPathFor(const std::filesystem::path& media_path);

// Creates a lowercase RFC-4122-shaped UUID for one media/manifest
// transaction. It is intentionally unrelated to source IDs.
bool CreateAudioManifestTransactionId(std::string* transaction_id,
                                      std::string* error);

// Writes the manifest to `manifest_temporary_path`. The SHA-256 is computed
// over `media_temporary_path`, while media_file intentionally names the final
// public clip basename. The caller must publish both via RunPair().
bool WriteClipAudioManifest(
    const std::filesystem::path& media_final_path,
    const std::filesystem::path& media_temporary_path,
    const std::filesystem::path& manifest_temporary_path,
    const std::string& transaction_id,
    const std::vector<EncodedAudioTrack>& tracks,
    std::string* error, const std::vector<std::string>& warnings = {});

}  // namespace fthr

#endif  // FTHR_CLIP_AUDIO_MANIFEST_H
