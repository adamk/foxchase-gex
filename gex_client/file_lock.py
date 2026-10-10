"""Cross-platform, cross-process file locking used by local token storage."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import portalocker

from gex_client.secure_storage import apply_private_path_mode


_LOCK_RETRY_SECONDS = 0.1


@contextmanager
def exclusive_file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Wait until an exclusive process lock is available, then hold it."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.touch(mode=0o600, exist_ok=True)
    apply_private_path_mode(lock_path)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        while True:
            try:
                portalocker.lock(
                    lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB
                )
                break
            except portalocker.exceptions.AlreadyLocked:
                # Match flock(LOCK_EX): contention waits rather than timing out
                # or allowing refresh to proceed without the process guard.
                time.sleep(_LOCK_RETRY_SECONDS)

        try:
            yield
        finally:
            portalocker.unlock(lock_file)
