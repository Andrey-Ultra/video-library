"""Index location and one-time migration of the old hidden folder."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

DB_NAME = "library-v2.sqlite3"
CONFIG_NAME = "config.json"
INDEX_DIRECTORY = ".video-library"
LEGACY_DIRECTORY = ".video-agent"
logger = logging.getLogger(__name__)


def index_path(folder):
    folder = Path(folder).expanduser().resolve()
    current = folder / INDEX_DIRECTORY / DB_NAME
    legacy = folder / LEGACY_DIRECTORY / DB_NAME
    return current if current.is_file() or not legacy.is_file() else legacy


@contextlib.contextmanager
def index_access(folder, exclusive=False, blocking=True):
    # Outside the index folder: the lock remains stable while that folder moves.
    # A separate file is essential; opening/closing the SQLite file itself can
    # interfere with SQLite's POSIX locks in this process.
    root = Path(tempfile.gettempdir()) / "video-library-index-locks"
    root.mkdir(exist_ok=True, mode=0o700)
    key = hashlib.sha256(str(Path(folder).expanduser().resolve()).encode()).hexdigest()
    with (root / f"{key}.lock").open("a+b") as lock:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock.fileno(), flag | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield lock
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def migrate_legacy_index(folder):
    """Move the complete directory atomically, including WAL and checkpoints.

    Read-only callers can still use the legacy location. Writers migrate only
    when no Store or older indexer has the index open. Never merge two indexes.
    """
    folder = Path(folder).expanduser().resolve()
    old, new = folder / LEGACY_DIRECTORY, folder / INDEX_DIRECTORY
    if not (old / DB_NAME).is_file():
        return new
    try:
        access = index_access(folder, exclusive=True, blocking=False)
        access.__enter__()
    except BlockingIOError:
        raise RuntimeError(
            "Index is in use. Close Video library and active MCP requests, then retry migration."
        ) from None
    try:
        # Check again under the cross-process lock; another writer may have moved it.
        if not (old / DB_NAME).is_file():
            return new
        if (new / DB_NAME).exists() or (new.exists() and any(new.iterdir())):
            raise RuntimeError(
                "Both .video-agent and .video-library contain data. "
                "Keep both folders and resolve the index conflict before indexing."
            )
        with (old / "index.lock").open("a+b") as writer:
            try:
                fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "The old index is being written. Pause indexing before migration."
                ) from None
            # Older app/MCP versions do not use index_access. Refuse to rename an
            # SQLite database that still has open handles, including WAL readers.
            assert_index_closed(old)
            if new.exists():
                new.rmdir()  # Only the verified empty destination.
            old.rename(new)
            # On non-native volumes macOS may keep directory metadata in a
            # sibling AppleDouble file. Preserve it under the matching new name.
            sidecar = folder / f"._{LEGACY_DIRECTORY}"
            destination = folder / f"._{INDEX_DIRECTORY}"
            if sidecar.is_file() and not sidecar.is_symlink() and not destination.exists():
                try:
                    with sidecar.open("rb") as stream:
                        apple_double = stream.read(4) == b"\x00\x05\x16\x07"
                    if apple_double:
                        sidecar.rename(destination)
                except OSError:
                    logger.warning("Index moved; macOS metadata file could not be renamed: %s", sidecar)
        return new
    finally:
        access.__exit__(None, None, None)


def assert_index_closed(directory):
    lsof = shutil.which("lsof")
    if not lsof and Path("/usr/sbin/lsof").is_file():
        lsof = "/usr/sbin/lsof"
    if not lsof:
        raise RuntimeError("Cannot verify open index handles: lsof is unavailable.")
    files = [
        str(p)
        for p in (Path(directory) / (DB_NAME + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
        if p.exists()
    ]
    if not files:
        return
    result = subprocess.run([lsof, "-t", "--", *files], capture_output=True, text=True, timeout=15)
    if result.returncode == 0 or result.stdout.strip():
        raise RuntimeError("Index is open in another process. Close active previews/MCP requests and retry.")
    if result.returncode != 1:
        raise RuntimeError("Could not verify that the index is closed; files were not changed.")


def _index_contents_are_known(directory):
    known = {
        DB_NAME,
        CONFIG_NAME,
        f"{DB_NAME}-wal",
        f"{DB_NAME}-shm",
        f"{DB_NAME}-journal",
        "index.lock",
        ".DS_Store",
    }
    known |= {"._" + name for name in list(known)}
    return all(
        (p.name in known or (p.name.startswith(".config-") and p.name.endswith(".tmp")))
        and p.is_file()
        and not p.is_symlink()
        for p in directory.iterdir()
    )


def _remove_missing_tolerant(function, path, error):
    # ExFAT/macOS can remove an AppleDouble sidecar when its matching file is
    # unlinked. Python 3.12 rmtree still visits its stale directory entry.
    if not isinstance(error, FileNotFoundError):
        raise error


def _remove_appledouble(path):
    try:
        if path.is_symlink():
            return
        with path.open("rb") as stream:
            if stream.read(4) != b"\x00\x05\x16\x07":
                return
        path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def cleanup_deleted_index(directory):
    directory = Path(directory)
    if not re.fullmatch(r"\.video-library-deleting-[0-9a-f]{32}", directory.name):
        raise ValueError("Cleanup is restricted to removed index folders")
    if directory.is_symlink():
        raise ValueError("Removed index folder is a symlink; it was not deleted")
    if directory.exists():
        if not directory.is_dir() or not _index_contents_are_known(directory):
            raise ValueError("Removed index folder contains unrecognized files; it was not deleted")
        assert_index_closed(directory)
        shutil.rmtree(directory, onexc=_remove_missing_tolerant)
    _remove_appledouble(directory.parent / ("._" + directory.name))


def delete_index(folder):
    """Remove only recognized index folders, never source videos or unknown files.

    Rename a closed index as a unit before unlinking it, so a crash cannot leave
    an old WAL alongside a newly created database. Returns any cleanup warnings.
    """
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Folder is unavailable. Reconnect its drive before deleting the index.")
    moved, warnings = [], []
    try:
        with index_access(folder, exclusive=True, blocking=False):
            with contextlib.ExitStack() as stack:
                directories = [
                    folder / name
                    for name in (INDEX_DIRECTORY, LEGACY_DIRECTORY)
                    if (folder / name).exists() or (folder / name).is_symlink()
                ]
                for directory in directories:
                    if directory.is_symlink() or not directory.is_dir():
                        raise ValueError("Index location is not a regular directory; nothing was deleted.")
                    if not _index_contents_are_known(directory):
                        raise ValueError("Index folder contains unrecognized files. Nothing was deleted.")
                    lock = stack.enter_context((directory / "index.lock").open("a+b"))
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    assert_index_closed(directory)
                try:
                    for directory in directories:
                        temporary = folder / f".video-library-deleting-{uuid.uuid4().hex}"
                        directory.rename(temporary)
                        moved.append((directory, temporary))
                except BaseException:
                    for directory, temporary in reversed(moved):
                        temporary.rename(directory)
                    raise
            # Close our own index.lock descriptors before deleting on non-native
            # volumes. The stable access lease still excludes active Store users.
            for directory, temporary in moved:
                try:
                    cleanup_deleted_index(temporary)
                    _remove_appledouble(folder / f"._{directory.name}")
                except (OSError, RuntimeError, ValueError):
                    logger.warning("Could not finish removed-index cleanup: %s", temporary, exc_info=True)
                    warnings.append(
                        "Library removed. Some old index cache files could not be deleted from the drive. "
                        "Close apps using this folder and retry cleanup."
                    )
    except BlockingIOError:
        raise RuntimeError(
            "Index is in use. Pause indexing and close active MCP requests, then retry."
        ) from None
    return warnings
