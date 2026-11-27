import fcntl
import sqlite3
import subprocess
import sys

import pytest

from video_agent import media
from video_agent.index_storage import DB_NAME, INDEX_DIRECTORY, index_path, migrate_legacy_index
from video_agent.pipeline import item
from video_agent.store import Store


def rows(store):
    return {
        table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("videos", "stages", "items", "items_fts", "vectors", "work_cache", "settings")
    }


def legacy_index(folder):
    source = folder / "source.mp4"
    source.write_bytes(b"source")
    with Store(folder) as store:
        video = store.register(source, "fingerprint", {"duration_ms": 60000, "has_audio": True})
        store.prepare(video, "speech", "pinned-model")
        store.stage_status(video, "speech", "pending", checkpoint=3, elapsed_s=12.5)
        with store.db:
            evidence = item(video, "speech", "speech", 1, 1000, 2000, "Привет мир", {"language": "ru"})
            store.put_item(evidence)
            store.db.execute("INSERT INTO vectors VALUES(?,?,?,?)", (evidence["id"], "namespace", 1, b"1234"))
            store.db.execute("INSERT INTO work_cache VALUES(?,'hierarchy','groups-0','{}')", (video,))
        store.save_config({"speech": {"backend": "parakeet", "model": "saved-model"}})
        expected = rows(store)
        directory = store.directory
    old = folder / ".video-agent"
    directory.rename(old)
    return old, expected


def test_new_store_uses_video_library_directory(tmp_path):
    with Store(tmp_path) as store:
        assert store.directory == tmp_path / INDEX_DIRECTORY
        assert store.path == tmp_path / INDEX_DIRECTORY / DB_NAME
    assert not (tmp_path / ".video-agent").exists()


def test_legacy_index_is_readable_then_migrated_without_losing_evidence_or_checkpoints(tmp_path):
    old, expected = legacy_index(tmp_path)
    assert index_path(tmp_path) == old / DB_NAME
    with Store(tmp_path, create=False) as store:
        assert rows(store) == expected
        assert store.directory == old
    assert old.is_dir()  # read-only inspection does not rename folders
    with Store(tmp_path) as store:
        assert store.directory.name == ".video-library"
        assert rows(store) == expected
    assert not old.exists()
    assert (tmp_path / "source.mp4").read_bytes() == b"source"
    assert index_path(tmp_path) == tmp_path / INDEX_DIRECTORY / DB_NAME


def test_unclean_wal_is_moved_with_database_and_recovered(tmp_path):
    old, _ = legacy_index(tmp_path)
    script = """import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute("UPDATE stages SET checkpoint=9 WHERE name='speech'")
c.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(old / DB_NAME)], check=True)
    assert (old / f"{DB_NAME}-wal").stat().st_size > 0
    migrate_legacy_index(tmp_path)
    new = tmp_path / INDEX_DIRECTORY
    assert (new / f"{DB_NAME}-wal").is_file()
    with Store(tmp_path) as store:
        assert store.stage(1, "speech")["checkpoint"] == 9
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.items(1, stage="speech")[0]["text"] == "Привет мир"


def test_active_store_reader_prevents_rename(tmp_path):
    old, _ = legacy_index(tmp_path)
    with Store(tmp_path, create=False):
        with pytest.raises(RuntimeError, match="in use"):
            migrate_legacy_index(tmp_path)
    assert old.is_dir() and not (tmp_path / INDEX_DIRECTORY).exists()
    migrate_legacy_index(tmp_path)
    assert not old.exists()


def test_older_client_database_handle_prevents_rename(tmp_path):
    old, _ = legacy_index(tmp_path)
    client = sqlite3.connect(old / DB_NAME)
    try:
        client.execute("SELECT count(*) FROM items").fetchone()
        with pytest.raises(RuntimeError, match="open in another process"):
            migrate_legacy_index(tmp_path)
        assert old.is_dir()
    finally:
        client.close()
    migrate_legacy_index(tmp_path)


def test_old_writer_lock_prevents_rename(tmp_path):
    old, _ = legacy_index(tmp_path)
    with (old / "index.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="being written"):
            migrate_legacy_index(tmp_path)
    assert old.is_dir()


def test_conflicting_new_folder_is_never_overwritten(tmp_path):
    old, _ = legacy_index(tmp_path)
    new = tmp_path / INDEX_DIRECTORY
    new.mkdir()
    marker = new / "do-not-delete.txt"
    marker.write_text("user data")
    with pytest.raises(RuntimeError, match="Both"):
        migrate_legacy_index(tmp_path)
    assert old.is_dir() and marker.read_text() == "user data"


def test_empty_destination_allows_migration_and_both_hidden_names_are_ignored(tmp_path):
    old, _ = legacy_index(tmp_path)
    (tmp_path / INDEX_DIRECTORY).mkdir()
    migrate_legacy_index(tmp_path)
    assert not old.exists()
    (tmp_path / INDEX_DIRECTORY / "proxy.mp4").touch()
    old.mkdir()
    (old / "legacy-cache.mp4").touch()
    (tmp_path / "._.video-library").write_bytes(b"AppleDouble")
    assert list(media.discover(tmp_path)) == [tmp_path / "source.mp4"]


def test_matching_appledouble_metadata_is_preserved_under_new_name(tmp_path):
    legacy_index(tmp_path)
    sidecar = tmp_path / "._.video-agent"
    metadata = b"\x00\x05\x16\x07" + b"saved macOS metadata"
    sidecar.write_bytes(metadata)
    migrate_legacy_index(tmp_path)
    assert not sidecar.exists()
    assert (tmp_path / "._.video-library").read_bytes() == metadata


def test_readers_wait_until_new_database_schema_is_committed(tmp_path):
    import threading
    import time

    opened = threading.Event()
    finished = threading.Event()
    failures = []
    original = Store._schema

    def slow_schema(self):
        opened.set()
        time.sleep(0.15)
        original(self)

    def writer():
        try:
            from unittest.mock import patch

            with patch.object(Store, "_schema", slow_schema):
                with Store(tmp_path):
                    pass
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    assert opened.wait(5)
    assert not finished.is_set()
    # The database file exists, but readers must wait for the tables to exist too.
    with Store(tmp_path, create=False) as store:
        assert store.stage(1, "visual") is None
    thread.join(5)
    assert finished.is_set() and not failures
