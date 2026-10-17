"""Cheap identities for disposable, in-memory media inspection caches."""

import os


def file_identity(path):
    """Include replacement identity and timestamps, not just the filename."""
    info = os.stat(path)
    return (os.path.normcase(os.path.realpath(path)), info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
