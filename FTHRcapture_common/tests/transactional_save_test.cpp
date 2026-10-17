#include "transactional_save.h"

#include <cassert>
#include <chrono>
#include <fstream>
#include <string>
#include <system_error>
#include <vector>

namespace tx = fthr::transactional_save;

namespace {

struct FakeFilesystem {
    bool final_exists = false;
    bool temporary_exists = false;
    bool rename_fails = false;
    bool cleanup_fails = false;
    std::vector<std::string> operations;

    tx::FileOps Ops() {
        tx::FileOps ops;
        ops.exists = [this](const std::filesystem::path& path, std::error_code&) {
            operations.push_back("exists:" + path.filename().string());
            return path.extension() == tx::kPartialSuffix
                ? temporary_exists
                : final_exists;
        };
        ops.rename_no_replace = [this](
            const std::filesystem::path& temporary,
            const std::filesystem::path& final,
            std::error_code& error) {
            operations.push_back(
                "rename:" + temporary.filename().string() + "->" + final.filename().string());
            if (rename_fails || final_exists || !temporary_exists) {
                error = std::make_error_code(
                    final_exists ? std::errc::file_exists : std::errc::io_error);
                return false;
            }
            temporary_exists = false;
            final_exists = true;
            return true;
        };
        ops.remove = [this](const std::filesystem::path& path, std::error_code& error) {
            operations.push_back("remove:" + path.filename().string());
            if (cleanup_fails) {
                error = std::make_error_code(std::errc::permission_denied);
                return false;
            }
            temporary_exists = false;
            return true;
        };
        return ops;
    }
};

void TestSuccessfulSave() {
    FakeFilesystem fs;
    int success_responses = 0;
    int error_responses = 0;
    const auto result = tx::Run(
        "clip.mp4",
        [&fs](const std::filesystem::path& path, std::string&) {
            assert(path.filename() == "clip.mp4.partial");
            assert(!fs.final_exists);
            fs.operations.push_back("write:" + path.filename().string());
            fs.temporary_exists = true;
            return true;
        },
        fs.Ops());
    if (result.success) ++success_responses; else ++error_responses;

    assert(result.success);
    assert(fs.final_exists);
    assert(!fs.temporary_exists);
    assert(success_responses == 1);
    assert(error_responses == 0);
    assert(fs.operations.back() == "rename:clip.mp4.partial->clip.mp4");
}

void TestWriterFailureCleansTemporary() {
    FakeFilesystem fs;
    const auto result = tx::Run(
        "clip.mp4",
        [&fs](const std::filesystem::path&, std::string& error) {
            fs.temporary_exists = true;
            error = "packet write failed";
            return false;
        },
        fs.Ops());

    assert(!result.success);
    assert(result.failure == tx::Failure::Writer);
    assert(tx::DescribeFailure(result) == "packet write failed");
    assert(!fs.final_exists);
    assert(!fs.temporary_exists);
}

void TestTrailerFailureNeverCommits() {
    FakeFilesystem fs;
    const auto result = tx::Run(
        "clip.mp4",
        [&fs](const std::filesystem::path&, std::string& error) {
            fs.temporary_exists = true;
            error = "container trailer failed";
            return false;
        },
        fs.Ops());

    assert(!result.success);
    assert(!fs.final_exists);
    assert(tx::DescribeFailure(result) == "container trailer failed");
}

void TestRenameFailureIsAnError() {
    FakeFilesystem fs;
    fs.rename_fails = true;
    const auto result = tx::Run(
        "clip.mp4",
        [&fs](const std::filesystem::path&, std::string&) {
            fs.temporary_exists = true;
            return true;
        },
        fs.Ops());

    assert(!result.success);
    assert(result.failure == tx::Failure::Rename);
    assert(!fs.final_exists);
    assert(!fs.temporary_exists);
}

void TestCleanupFailurePreservesOriginalError() {
    FakeFilesystem fs;
    fs.cleanup_fails = true;
    const auto result = tx::Run(
        "clip.mp4",
        [&fs](const std::filesystem::path&, std::string& error) {
            fs.temporary_exists = true;
            error = "disk full";
            return false;
        },
        fs.Ops());

    assert(!result.success);
    assert(tx::DescribeFailure(result) == "disk full");
    assert(result.cleanup_error == std::errc::permission_denied);
    assert(fs.temporary_exists);
}

void TestExistingDestinationIsNotDestroyed() {
    FakeFilesystem fs;
    fs.final_exists = true;
    bool writer_called = false;
    const auto result = tx::Run(
        "clip.mp4",
        [&writer_called](const std::filesystem::path&, std::string&) {
            writer_called = true;
            return true;
        },
        fs.Ops());

    assert(!result.success);
    assert(result.failure == tx::Failure::DestinationExists);
    assert(fs.final_exists);
    assert(!writer_called);
}

void TestMissingTemporaryCannotBeCommitted() {
    FakeFilesystem fs;
    const auto result = tx::Run(
        "clip.mp4",
        [](const std::filesystem::path&, std::string&) { return true; },
        fs.Ops());

    assert(!result.success);
    assert(result.failure == tx::Failure::Rename);
    assert(!fs.final_exists);
}

std::filesystem::path MakeTestDirectory() {
    const auto stamp = std::chrono::high_resolution_clock::now()
        .time_since_epoch().count();
    const auto directory = std::filesystem::temp_directory_path()
        / ("fthr-transactional-save-" + std::to_string(stamp));
    std::filesystem::create_directories(directory);
    return directory;
}

void TestDefaultFilesystemCommit() {
    const auto directory = MakeTestDirectory();
    const auto final = directory / "clip.mp4";
    const auto result = tx::Run(
        final,
        [](const std::filesystem::path& temporary, std::string& error) {
            std::ofstream output(temporary, std::ios::binary);
            output << "complete";
            output.close();
            if (!output) {
                error = "test writer could not close output";
                return false;
            }
            return true;
        });

    assert(result.success);
    assert(std::filesystem::exists(final));
    assert(!std::filesystem::exists(tx::TemporaryPathFor(final)));
    std::ifstream input(final, std::ios::binary);
    std::string contents;
    input >> contents;
    assert(contents == "complete");

    std::error_code cleanup_error;
    std::filesystem::remove(final, cleanup_error);
    std::filesystem::remove(directory, cleanup_error);
}

void TestDefaultFilesystemPreservesExistingDestination() {
    const auto directory = MakeTestDirectory();
    const auto final = directory / "clip.mp4";
    {
        std::ofstream existing(final, std::ios::binary);
        existing << "original";
    }
    bool writer_called = false;
    const auto result = tx::Run(
        final,
        [&writer_called](const std::filesystem::path&, std::string&) {
            writer_called = true;
            return true;
        });

    assert(!result.success);
    assert(result.failure == tx::Failure::DestinationExists);
    assert(!writer_called);
    std::ifstream input(final, std::ios::binary);
    std::string contents;
    input >> contents;
    assert(contents == "original");

    std::error_code cleanup_error;
    std::filesystem::remove(final, cleanup_error);
    std::filesystem::remove(directory, cleanup_error);
}

} // namespace

int main() {
    TestSuccessfulSave();
    TestWriterFailureCleansTemporary();
    TestTrailerFailureNeverCommits();
    TestRenameFailureIsAnError();
    TestCleanupFailurePreservesOriginalError();
    TestExistingDestinationIsNotDestroyed();
    TestMissingTemporaryCannotBeCommitted();
    TestDefaultFilesystemCommit();
    TestDefaultFilesystemPreservesExistingDestination();
    return 0;
}
