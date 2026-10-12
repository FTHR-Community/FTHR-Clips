#pragma once

#include <exception>
#include <filesystem>
#include <functional>
#include <string>
#include <system_error>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#elif defined(__linux__)
#include <cerrno>
#include <fcntl.h>
#include <linux/fs.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

namespace fthr::transactional_save {

namespace fs = std::filesystem;

inline constexpr char kPartialSuffix[] = ".partial";

enum class Failure {
    None,
    InspectDestination,
    DestinationExists,
    InspectTemporary,
    TemporaryExists,
    Writer,
    Rename,
};

struct FileOps {
    std::function<bool(const fs::path&, std::error_code&)> exists;
    std::function<bool(const fs::path&, const fs::path&, std::error_code&)>
        rename_no_replace;
    std::function<bool(const fs::path&, std::error_code&)> remove;
    // Flush a finished temporary file to disk before it is published, and the
    // containing directory after the rename, so a power loss cannot leave a
    // published name over empty data. Defaults succeed for fakes that do not
    // care about durability.
    std::function<bool(const fs::path&, std::error_code&)> sync_file =
        [](const fs::path&, std::error_code&) { return true; };
    std::function<bool(const fs::path&, std::error_code&)> sync_directory =
        [](const fs::path&, std::error_code&) { return true; };
};

struct Result {
    bool success = false;
    Failure failure = Failure::None;
    fs::path final_path;
    fs::path temporary_path;
    std::string writer_error;
    std::error_code operation_error;
    std::error_code cleanup_error;
};

using Writer = std::function<bool(const fs::path&, std::string&)>;

// A native multi-audio clip is a pair: the media file and its adjacent
// source-to-stream manifest.  Publishing the manifest first means a completed
// MP4 is never visible without its authoritative track mapping.  A crash in
// the narrow interval between the two renames can leave only an orphaned
// sidecar, which startup recovery may safely remove because the media file is
// still absent.
enum class PairFailure {
    None,
    InspectMediaDestination,
    MediaDestinationExists,
    InspectManifestDestination,
    ManifestDestinationExists,
    InspectMediaTemporary,
    MediaTemporaryExists,
    InspectManifestTemporary,
    ManifestTemporaryExists,
    Writer,
    PublishManifest,
    PublishMedia,
};

struct PairResult {
    bool success = false;
    PairFailure failure = PairFailure::None;
    fs::path media_final_path;
    fs::path media_temporary_path;
    fs::path manifest_final_path;
    fs::path manifest_temporary_path;
    std::string writer_error;
    std::error_code operation_error;
    std::error_code cleanup_error;
};

using PairWriter = std::function<bool(
    const fs::path& media_temporary_path,
    const fs::path& manifest_temporary_path,
    std::string&)>;

inline fs::path TemporaryPathFor(const fs::path& final_path) {
    fs::path temporary_path = final_path;
    temporary_path += kPartialSuffix;
    return temporary_path;
}

inline std::string Utf8Path(const fs::path& path) {
    const auto encoded = path.u8string();
    std::string text;
    text.reserve(encoded.size());
    for (const auto character : encoded)
        text.push_back(static_cast<char>(character));
    return text;
}

inline FileOps DefaultFileOps() {
    FileOps ops;
    ops.exists = [](const fs::path& path, std::error_code& error) {
        return fs::exists(path, error);
    };
    ops.remove = [](const fs::path& path, std::error_code& error) {
        (void)fs::remove(path, error);
        return !error;
    };
    ops.rename_no_replace = [](
        const fs::path& temporary_path,
        const fs::path& final_path,
        std::error_code& error) {
#if defined(_WIN32)
        if (MoveFileExW(
                temporary_path.c_str(), final_path.c_str(), MOVEFILE_WRITE_THROUGH)) {
            return true;
        }
        error = std::error_code(
            static_cast<int>(GetLastError()), std::system_category());
        return false;
#elif defined(__linux__) && defined(SYS_renameat2)
        const long result = syscall(
            SYS_renameat2,
            AT_FDCWD,
            temporary_path.c_str(),
            AT_FDCWD,
            final_path.c_str(),
            RENAME_NOREPLACE);
        if (result == 0) return true;
        error = std::error_code(errno, std::generic_category());
        return false;
#else
        (void)temporary_path;
        (void)final_path;
        error = std::make_error_code(std::errc::operation_not_supported);
        return false;
#endif
    };
#if defined(__linux__)
    const auto fsync_path = [](const fs::path& path, int flags,
                               std::error_code& error) {
        const int fd = ::open(path.c_str(), flags | O_CLOEXEC);
        if (fd < 0) {
            error = std::error_code(errno, std::generic_category());
            return false;
        }
        const bool synced = ::fsync(fd) == 0;
        if (!synced) error = std::error_code(errno, std::generic_category());
        ::close(fd);
        return synced;
    };
    ops.sync_file = [fsync_path](const fs::path& path, std::error_code& error) {
        return fsync_path(path, O_RDONLY, error);
    };
    ops.sync_directory = [fsync_path](const fs::path& path, std::error_code& error) {
        return fsync_path(path.empty() ? fs::path(".") : path,
                          O_RDONLY | O_DIRECTORY, error);
    };
#endif
    // Windows: MoveFileExW(MOVEFILE_WRITE_THROUGH) already flushes the rename.
    return ops;
}

inline void CleanupTemporary(Result& result, const FileOps& ops) {
    std::error_code error;
    if (!ops.remove(result.temporary_path, error)) {
        if (!error) error = std::make_error_code(std::errc::io_error);
        result.cleanup_error = error;
    }
}

inline Result Run(
    const fs::path& final_path,
    const Writer& writer,
    const FileOps& ops = DefaultFileOps()) {
    Result result;
    result.final_path = final_path;
    result.temporary_path = TemporaryPathFor(final_path);

    std::error_code error;
    if (ops.exists(result.final_path, error)) {
        result.failure = Failure::DestinationExists;
        return result;
    }
    if (error) {
        result.failure = Failure::InspectDestination;
        result.operation_error = error;
        return result;
    }

    error.clear();
    if (ops.exists(result.temporary_path, error)) {
        result.failure = Failure::TemporaryExists;
        return result;
    }
    if (error) {
        result.failure = Failure::InspectTemporary;
        result.operation_error = error;
        return result;
    }

    bool writer_succeeded = false;
    try {
        writer_succeeded = writer(result.temporary_path, result.writer_error);
    }
    catch (const std::exception& exception) {
        result.writer_error = exception.what();
    }
    catch (...) {
        result.writer_error = "Media writer raised an unknown exception";
    }

    if (!writer_succeeded) {
        result.failure = Failure::Writer;
        CleanupTemporary(result, ops);
        return result;
    }

    error.clear();
    if (!ops.sync_file(result.temporary_path, error)) {
        result.failure = Failure::Writer;
        result.writer_error = "Could not flush the temporary clip to disk: "
            + (error ? error : std::make_error_code(std::errc::io_error)).message();
        CleanupTemporary(result, ops);
        return result;
    }

    error.clear();
    if (!ops.rename_no_replace(result.temporary_path, result.final_path, error)) {
        result.failure = Failure::Rename;
        result.operation_error = error
            ? error
            : std::make_error_code(std::errc::io_error);
        CleanupTemporary(result, ops);
        return result;
    }

    // The clip is already published; a failed directory sync only weakens
    // durability and must not turn a finished save into an error.
    error.clear();
    (void)ops.sync_directory(result.final_path.parent_path(), error);

    result.success = true;
    result.failure = Failure::None;
    return result;
}

inline void CleanupPairTemporary(PairResult& result, const FileOps& ops) {
    std::error_code error;
    if (!ops.remove(result.media_temporary_path, error) && !result.cleanup_error) {
        result.cleanup_error = error ? error : std::make_error_code(std::errc::io_error);
    }
    error.clear();
    if (!ops.remove(result.manifest_temporary_path, error) && !result.cleanup_error) {
        result.cleanup_error = error ? error : std::make_error_code(std::errc::io_error);
    }
}

inline PairResult RunPair(
    const fs::path& media_final_path,
    const fs::path& manifest_final_path,
    const PairWriter& writer,
    const FileOps& ops = DefaultFileOps()) {
    PairResult result;
    result.media_final_path = media_final_path;
    result.manifest_final_path = manifest_final_path;
    result.media_temporary_path = TemporaryPathFor(media_final_path);
    result.manifest_temporary_path = TemporaryPathFor(manifest_final_path);

    std::error_code error;
    if (ops.exists(result.media_final_path, error)) {
        result.failure = PairFailure::MediaDestinationExists;
        return result;
    }
    if (error) {
        result.failure = PairFailure::InspectMediaDestination;
        result.operation_error = error;
        return result;
    }
    error.clear();
    if (ops.exists(result.manifest_final_path, error)) {
        result.failure = PairFailure::ManifestDestinationExists;
        return result;
    }
    if (error) {
        result.failure = PairFailure::InspectManifestDestination;
        result.operation_error = error;
        return result;
    }
    error.clear();
    if (ops.exists(result.media_temporary_path, error)) {
        result.failure = PairFailure::MediaTemporaryExists;
        return result;
    }
    if (error) {
        result.failure = PairFailure::InspectMediaTemporary;
        result.operation_error = error;
        return result;
    }
    error.clear();
    if (ops.exists(result.manifest_temporary_path, error)) {
        result.failure = PairFailure::ManifestTemporaryExists;
        return result;
    }
    if (error) {
        result.failure = PairFailure::InspectManifestTemporary;
        result.operation_error = error;
        return result;
    }

    bool writer_succeeded = false;
    try {
        writer_succeeded = writer(
            result.media_temporary_path, result.manifest_temporary_path, result.writer_error);
    }
    catch (const std::exception& exception) {
        result.writer_error = exception.what();
    }
    catch (...) {
        result.writer_error = "Media/manifest writer raised an unknown exception";
    }
    if (!writer_succeeded) {
        result.failure = PairFailure::Writer;
        CleanupPairTemporary(result, ops);
        return result;
    }

    // Manifest first: never make a completed media artifact observable without
    // its binding metadata.  The later media rename is no-replace and durable.
    error.clear();
    if (!ops.rename_no_replace(
            result.manifest_temporary_path, result.manifest_final_path, error)) {
        result.failure = PairFailure::PublishManifest;
        result.operation_error = error ? error : std::make_error_code(std::errc::io_error);
        CleanupPairTemporary(result, ops);
        return result;
    }
    error.clear();
    if (!ops.rename_no_replace(result.media_temporary_path, result.media_final_path, error)) {
        result.failure = PairFailure::PublishMedia;
        result.operation_error = error ? error : std::make_error_code(std::errc::io_error);
        // The media was not published, therefore the just-published manifest is
        // an orphan.  Best-effort removal keeps recovery deterministic.
        std::error_code manifest_cleanup_error;
        if (!ops.remove(result.manifest_final_path, manifest_cleanup_error) && !result.cleanup_error) {
            result.cleanup_error = manifest_cleanup_error
                ? manifest_cleanup_error
                : std::make_error_code(std::errc::io_error);
        }
        CleanupPairTemporary(result, ops);
        return result;
    }

    result.success = true;
    return result;
}

inline std::string DescribeFailure(const Result& result) {
    const std::string final_path = Utf8Path(result.final_path);
    const std::string temporary_path = Utf8Path(result.temporary_path);
    switch (result.failure) {
    case Failure::InspectDestination:
        return "Could not inspect final clip path '" + final_path + "': "
            + result.operation_error.message();
    case Failure::DestinationExists:
        return "Final clip already exists and was not overwritten: '" + final_path + "'";
    case Failure::InspectTemporary:
        return "Could not inspect temporary clip path '" + temporary_path + "': "
            + result.operation_error.message();
    case Failure::TemporaryExists:
        return "Temporary clip already exists and was not overwritten: '"
            + temporary_path + "'";
    case Failure::Writer:
        return result.writer_error.empty()
            ? "Media writer failed while creating temporary clip '" + temporary_path + "'"
            : result.writer_error;
    case Failure::Rename:
        return "Could not atomically finalize clip '" + final_path + "' from '"
            + temporary_path + "': " + result.operation_error.message();
    case Failure::None:
        break;
    }
    return {};
}

inline std::string DescribeFailure(const PairResult& result) {
    const std::string media = Utf8Path(result.media_final_path);
    const std::string manifest = Utf8Path(result.manifest_final_path);
    switch (result.failure) {
    case PairFailure::InspectMediaDestination:
        return "Could not inspect final clip path '" + media + "': "
            + result.operation_error.message();
    case PairFailure::MediaDestinationExists:
        return "Final clip already exists and was not overwritten: '" + media + "'";
    case PairFailure::InspectManifestDestination:
        return "Could not inspect final audio manifest path '" + manifest + "': "
            + result.operation_error.message();
    case PairFailure::ManifestDestinationExists:
        return "Final audio manifest already exists and was not overwritten: '" + manifest + "'";
    case PairFailure::InspectMediaTemporary:
        return "Could not inspect temporary clip path '" + Utf8Path(result.media_temporary_path)
            + "': " + result.operation_error.message();
    case PairFailure::MediaTemporaryExists:
        return "Temporary clip already exists and was not overwritten: '"
            + Utf8Path(result.media_temporary_path) + "'";
    case PairFailure::InspectManifestTemporary:
        return "Could not inspect temporary audio manifest path '"
            + Utf8Path(result.manifest_temporary_path) + "': "
            + result.operation_error.message();
    case PairFailure::ManifestTemporaryExists:
        return "Temporary audio manifest already exists and was not overwritten: '"
            + Utf8Path(result.manifest_temporary_path) + "'";
    case PairFailure::Writer:
        return result.writer_error.empty()
            ? "Media/manifest writer failed before publication"
            : result.writer_error;
    case PairFailure::PublishManifest:
        return "Could not publish the audio manifest '" + manifest + "': "
            + result.operation_error.message();
    case PairFailure::PublishMedia:
        return "Could not finalize clip '" + media + "' after its manifest: "
            + result.operation_error.message();
    case PairFailure::None:
        break;
    }
    return {};
}

} // namespace fthr::transactional_save
