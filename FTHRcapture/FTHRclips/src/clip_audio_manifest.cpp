#ifndef NOMINMAX
#define NOMINMAX
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <objbase.h>
#include <bcrypt.h>

#include "clip_audio_manifest.h"

#include <fstream>
#include <iomanip>
#include <sstream>

#pragma comment(lib, "bcrypt.lib")

namespace fthr {

namespace {

bool IsManifestSafe(const std::string& value, size_t maximum) {
    return !value.empty() && value.size() <= maximum
        && SanitizeAudioSourceText(value, maximum) == value;
}

bool IsUuid(const std::string& value) {
    return AudioSourceId{value}.IsValid();
}

std::string EscapeJson(const std::string& value) {
    // All persisted fields are validated to the compact manifest alphabet. The
    // loop is still explicit so future safe-field widening cannot create JSON
    // injection from a process/device label.
    std::string escaped;
    escaped.reserve(value.size());
    for (const unsigned char character : value) {
        switch (character) {
        case '\\': escaped += "\\\\"; break;
        case '"': escaped += "\\\""; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default:
            if (character >= 0x20) escaped.push_back(static_cast<char>(character));
            break;
        }
    }
    return escaped;
}

bool Sha256File(const std::filesystem::path& path, std::string* digest,
                std::string* error) {
    if (!digest) return false;
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD object_length = 0;
    DWORD hash_length = 0;
    DWORD result_length = 0;
    NTSTATUS status = BCryptOpenAlgorithmProvider(
        &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
    if (status < 0) {
        if (error) *error = "could not initialize SHA-256 provider";
        return false;
    }
    std::vector<UCHAR> object;
    std::vector<UCHAR> hash_bytes;
    const auto close_algorithm = [&]() { if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0); };
    status = BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
        reinterpret_cast<PUCHAR>(&object_length), sizeof(object_length), &result_length, 0);
    if (status >= 0) status = BCryptGetProperty(algorithm, BCRYPT_HASH_LENGTH,
        reinterpret_cast<PUCHAR>(&hash_length), sizeof(hash_length), &result_length, 0);
    if (status < 0 || object_length == 0 || hash_length != 32) {
        close_algorithm();
        if (error) *error = "could not query SHA-256 provider parameters";
        return false;
    }
    object.resize(object_length);
    hash_bytes.resize(hash_length);
    status = BCryptCreateHash(algorithm, &hash, object.data(), object_length,
        nullptr, 0, 0);
    if (status < 0) {
        close_algorithm();
        if (error) *error = "could not create SHA-256 hash";
        return false;
    }
    std::ifstream media(path, std::ios::binary);
    if (!media) {
        BCryptDestroyHash(hash);
        close_algorithm();
        if (error) *error = "could not open temporary media for SHA-256";
        return false;
    }
    // Keep the one-megabyte read buffer on the heap. SaveClipThread has the
    // standard Windows thread stack; allocating this on the stack would turn
    // a routine manifest hash into a stack-overflow crash.
    std::vector<char> block(1024 * 1024);
    while (media.good()) {
        media.read(block.data(), static_cast<std::streamsize>(block.size()));
        const std::streamsize count = media.gcount();
        if (count <= 0) break;
        status = BCryptHashData(hash, reinterpret_cast<PUCHAR>(block.data()),
            static_cast<ULONG>(count), 0);
        if (status < 0) {
            BCryptDestroyHash(hash);
            close_algorithm();
            if (error) *error = "could not hash temporary media";
            return false;
        }
    }
    if (media.bad()) {
        BCryptDestroyHash(hash);
        close_algorithm();
        if (error) *error = "could not read temporary media for SHA-256";
        return false;
    }
    status = BCryptFinishHash(hash, hash_bytes.data(), hash_length, 0);
    BCryptDestroyHash(hash);
    close_algorithm();
    if (status < 0) {
        if (error) *error = "could not finalize media SHA-256";
        return false;
    }
    std::ostringstream text;
    text << std::hex << std::setfill('0');
    for (const UCHAR byte : hash_bytes) text << std::setw(2) << static_cast<unsigned>(byte);
    *digest = text.str();
    return true;
}

bool ValidateTrack(const EncodedAudioTrack& track, std::string* error) {
    const auto& identity = track.source.identity;
    const auto& state = track.source.state;
    const auto& format = track.snapshot.format;
    if (!track.valid() || !track.source.state.admitted || !identity.id.IsValid()) {
        if (error) *error = "audio manifest track has no admitted packet source";
        return false;
    }
    if (!IsManifestSafe(identity.display_name, 80)
            || !IsManifestSafe(identity.persistent_identity, 128)
            || (!identity.icon_reference.empty()
                && !IsManifestSafe(identity.icon_reference, 128))) {
        if (error) *error = "audio manifest track has unsafe source metadata";
        return false;
    }
    if (state.first_active_100ns < 0 || state.last_active_100ns < state.first_active_100ns
            || format.sample_rate < 8000 || format.sample_rate > 192000
            || format.channels == 0 || format.channels > 8
            || !IsManifestSafe(format.sample_format, 32)) {
        if (error) *error = "audio manifest track has invalid timing or format";
        return false;
    }
    return true;
}

}  // namespace

std::filesystem::path AudioManifestPathFor(const std::filesystem::path& media_path) {
    return std::filesystem::path(media_path.wstring() + kAudioManifestSuffix);
}

bool CreateAudioManifestTransactionId(std::string* transaction_id,
                                      std::string* error) {
    if (!transaction_id) return false;
    GUID guid{};
    if (FAILED(CoCreateGuid(&guid))) {
        if (error) *error = "could not create audio-manifest transaction UUID";
        return false;
    }
    wchar_t wide[40]{};
    if (StringFromGUID2(guid, wide, static_cast<int>(std::size(wide))) != 39) {
        if (error) *error = "could not format audio-manifest transaction UUID";
        return false;
    }
    const int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
        wide + 1, 36, nullptr, 0, nullptr, nullptr);
    if (length != 36) {
        if (error) *error = "could not encode audio-manifest transaction UUID";
        return false;
    }
    std::string value(static_cast<size_t>(length), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide + 1, 36,
            value.data(), length, nullptr, nullptr) != length || !IsUuid(value)) {
        if (error) *error = "audio-manifest transaction UUID is invalid";
        return false;
    }
    *transaction_id = std::move(value);
    return true;
}

bool WriteClipAudioManifest(
    const std::filesystem::path& media_final_path,
    const std::filesystem::path& media_temporary_path,
    const std::filesystem::path& manifest_temporary_path,
    const std::string& transaction_id,
    const std::vector<EncodedAudioTrack>& tracks,
    std::string* error) {
    if (tracks.empty() || tracks.size() > kMaxClipAudioTracks || !IsUuid(transaction_id)) {
        if (error) *error = "audio manifest needs one to fourteen tracks and a valid transaction UUID";
        return false;
    }
    std::string media_hash;
    if (!Sha256File(media_temporary_path, &media_hash, error)) return false;

    std::ostringstream json;
    json << "{\"manifest_version\":1,\"transaction_id\":\""
         << EscapeJson(transaction_id) << "\",\"media_file\":\"";
    const auto filename = media_final_path.filename().u8string();
    for (const auto character : filename) json << static_cast<char>(character);
    json << "\",\"media_sha256\":\"" << media_hash << "\",\"sources\":[";
    for (size_t index = 0; index < tracks.size(); ++index) {
        const auto& track = tracks[index];
        if (!ValidateTrack(track, error)) return false;
        if (index > 0) json << ',';
        const auto& identity = track.source.identity;
        const auto& state = track.source.state;
        const auto& format = track.snapshot.format;
        // MP4 video is stream 0; tracks are added deterministically in this
        // source order, so the corresponding audio streams start at index 1.
        json << "{\"source_uuid\":\"" << EscapeJson(identity.id.value)
             << "\",\"stream_index\":" << (index + 1)
             << ",\"source_type\":\"" << AudioSourceTypeName(identity.type)
             << "\",\"display_name\":\"" << EscapeJson(identity.display_name)
             << "\",\"persistent_identity\":\"" << EscapeJson(identity.persistent_identity)
             << "\",\"icon_reference\":";
        if (identity.icon_reference.empty()) json << "null";
        else json << "\"" << EscapeJson(identity.icon_reference) << "\"";
        json << ",\"first_active_100ns\":" << state.first_active_100ns
             << ",\"last_active_100ns\":" << state.last_active_100ns
             << ",\"sample_rate\":" << format.sample_rate
             << ",\"channels\":" << format.channels
             << ",\"sample_format\":\"" << EscapeJson(format.sample_format)
             << "\"}";
    }
    json << "]}";

    std::ofstream manifest(manifest_temporary_path, std::ios::binary | std::ios::trunc);
    if (!manifest) {
        if (error) *error = "could not open temporary audio manifest for writing";
        return false;
    }
    manifest << json.str();
    manifest.flush();
    if (!manifest.good()) {
        if (error) *error = "could not write temporary audio manifest";
        return false;
    }
    return true;
}

}  // namespace fthr
