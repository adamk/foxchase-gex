"""Permission helpers for local secret files across supported platforms.

POSIX systems use restrictive mode bits. Windows inherits the directory ACL
for files created under the user's profile; Python's POSIX mode bits do not
represent or safely configure those ACLs.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


POSIX_MODE_BITS = os.name == "posix"


def apply_private_file_mode(file_descriptor: int) -> None:
    """Apply user-only file mode where POSIX mode bits are supported."""
    if POSIX_MODE_BITS:
        os.fchmod(file_descriptor, 0o600)


def apply_private_path_mode(path: str | os.PathLike[str]) -> None:
    """Apply user-only path mode where POSIX mode bits are supported."""
    if POSIX_MODE_BITS:
        os.chmod(path, 0o600)


def has_private_mode(path: str | os.PathLike[str], disallowed_bits: int) -> bool:
    """Check POSIX permissions; defer ACL enforcement to Windows inheritance."""
    if not POSIX_MODE_BITS:
        return True
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    return not bool(mode & disallowed_bits)


def sync_directory(path: str | os.PathLike[str]) -> None:
    """Persist a directory entry on POSIX; directory handles are not portable."""
    if not POSIX_MODE_BITS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
