import contextlib
import fcntl
import os
import sqlite3
import uuid

import pytest

from video_agent import index_storage
from video_agent.index_storage import DB_NAME, cleanup_deleted_index, delete_index
from video_agent.store import Store


def temporary_index(parent):
    path = parent / (".video-library-deleting-" + uuid.uuid4().hex)
    path.mkdir()
    (path / DB_NAME).write_bytes(b"old index")
    return path


def test_cleanup_tolerates_sidecar_removed_automatically_by_filesystem(tmp_path, monkeypatch):
    directory = temporary_index(tmp_path)
    (directory / ("._" + DB_NAME)).write_bytes(b"AppleDouble")
    unlink, scandir = os.unlink, os.scandir
    missing = []

    @contextlib.contextmanager
    def ordered_scandir(path):
        with scandir(path) as stream:
            entries = sorted(stream, key=lambda entry: entry.name.startswith("._"))
        yield iter(entries)

    def auto_sidecar(path, *, dir_fd=None):
        name = os.fspath(path)
        if name == DB_NAME:
            unlink("._" + DB_NAME, dir_fd=dir_fd)
        try:
            return unlink(path, dir_fd=dir_fd)
        except FileNotFoundError:
            missing.append(name)
            raise

    monkeypatch.setattr(os, "scandir", ordered_scandir)
    monkeypatch.setattr(os, "unlink", auto_sidecar)
    cleanup_deleted_index(directory)
    assert "._" + DB_NAME in missing
    assert not directory.exists()


def test_cleanup_still_reports_permission_errors(tmp_path, monkeypatch):
    directory = temporary_index(tmp_path)
    original = os.unlink

    def denied(path, *, dir_fd=None):
        if os.fspath(path) == DB_NAME:
            raise PermissionError("drive denied deletion")
        return original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", denied)
    with pytest.raises(PermissionError):
        cleanup_deleted_index(directory)
    assert (directory / DB_NAME).is_file()


def test_cleanup_never_accepts_current_index_or_unknown_source_files(tmp_path):
    with Store(tmp_path) as store:
        current = store.directory
    with pytest.raises(ValueError, match="restricted"):
        cleanup_deleted_index(current)
    assert (current / DB_NAME).is_file()
    removed = temporary_index(tmp_path)
    video = removed / "source.mov"
    video.write_bytes(b"source must remain")
    with pytest.raises(ValueError, match="unrecognized"):
        cleanup_deleted_index(removed)
    assert video.read_bytes() == b"source must remain"


def test_cleanup_does_not_remove_open_old_database(tmp_path):
    directory = temporary_index(tmp_path)
    (directory / DB_NAME).unlink()
    db = sqlite3.connect(directory / DB_NAME)
    try:
        db.execute("PRAGMA user_version").fetchone()
        with pytest.raises(RuntimeError, match="open in another process"):
            cleanup_deleted_index(directory)
    finally:
        db.close()
    assert directory.exists()


def test_writer_handles_are_closed_before_cleanup(tmp_path, monkeypatch):
    with Store(tmp_path):
        pass
    original = index_storage.cleanup_deleted_index
    calls = []

    def cleanup(directory):
        with (directory / "index.lock").open("a+b") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(directory)
        original(directory)

    monkeypatch.setattr(index_storage, "cleanup_deleted_index", cleanup)
    assert delete_index(tmp_path) == []
    assert len(calls) == 1 and not calls[0].exists()
