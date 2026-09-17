# Product version shared by the UI, bundles, installer, and build tools.
#
# verify_version_consistency.py checks consumers. The shared-memory layout
# version is independent and changes only with the IPC contract.

from __future__ import annotations

# PEP 440 / SemVer pre-release string. Keep the two in sync when releasing.
__version__ = '1.1.0-alpha'

# Numeric (major, minor, patch) triple. Windows VERSIONINFO resources cannot
# express a pre-release suffix, so the packaging code uses this and records the
# full string in the FileVersion / ProductVersion text fields.
VERSION_INFO = (1, 1, 0)

PRERELEASE = 'alpha'

# ISO 8601 release date shown in About. Update alongside __version__.
BUILD_DATE = '2026-09-17'

# FTHR's application code uses GPLv3. Separately distributed third-party
# components and project assets retain the licences listed in
# THIRD_PARTY_NOTICES.md.
SOURCE_LICENSE = 'GPL-3.0-only'
DISTRIBUTION_LICENSE = 'GPL-3.0-only + separately licensed components'

APP_NAME = 'FTHR Clips'
APP_ID = 'FTHRClips'
PUBLISHER = 'FTHR Community'


def version_string() -> str:
    """The canonical human-facing version, e.g. '1.0.0-alpha'."""
    return __version__


def windows_file_version() -> tuple[int, int, int, int]:
    """4-tuple for the Windows VS_FIXEDFILEINFO block.

    The fourth field is the build number; alpha builds pin it to 0 so two
    builds of the same source produce byte-identical version resources.
    """
    return (*VERSION_INFO, 0)
