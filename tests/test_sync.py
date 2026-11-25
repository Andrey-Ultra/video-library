import os
import shutil

import pytest

from video_agent import media
from video_agent.pipeline import item, writer_lock
from video_agent.store import Store
from video_agent.sync import synchronize


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "metadata", lambda _: {"duration_ms": 1000, "has_audio": False})
    (tmp_path / "a.mp4").write_bytes(b"footage A")
    return tmp_path


def seed(store):
    video_id = store.videos()[0]["id"]
    store.prepare(video_id, "visual", "saved-model")
    with store.db:
        store.put_item(item(video_id, "visual", "window", 0, 0, 1000, "A fox."))
    store.stage_status(video_id, "visual", "complete", checkpoint=1)
    return video_id


def test_rename_touch_duplicates_and_last_copy_deletion_preserve_then_purge(folder):
    with Store(folder) as store, writer_lock(store):
        assert synchronize(store)["added"] == 1
        original = seed(store)
        (folder / "a.mp4").rename(folder / "renamed.mp4")
        os.utime(folder / "renamed.mp4", None)
        assert synchronize(store)["renamed"] == 1
        assert store.video(original)["path"].endswith("renamed.mp4")
        assert store.stage(original, "visual")["status"] == "complete"
        shutil.copyfile(folder / "renamed.mp4", folder / "copy.mp4")
        result = synchronize(store)
        assert result["duplicates"] == 1 and result["unique_videos"] == 1
        assert store.count_items(original) == 1
        assert store.db.execute("SELECT full_hash FROM videos").fetchone()[0]
        (folder / "renamed.mp4").unlink()
        assert synchronize(store)["removed"] == 0
        assert store.video(original)["path"].endswith("copy.mp4")
        (folder / "copy.mp4").unlink()
        assert synchronize(store)["removed"] == 1
        for table in ["items", "items_fts", "vectors", "stages", "file_locations"]:
            assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        (folder / "new.mp4").write_bytes(b"new footage")
        synchronize(store)
        assert store.videos()[0]["id"] > original  # evidence IDs are never recycled


def test_changing_one_duplicate_does_not_invalidate_the_other(folder):
    shutil.copyfile(folder / "a.mp4", folder / "b.mp4")
    with Store(folder) as store:
        synchronize(store)
        original = seed(store)
        (folder / "a.mp4").write_bytes(b"replaced content")
        synchronize(store)
        assert store.total_videos() == 2
        assert store.video(original)["path"].endswith("b.mp4")
        assert store.count_items(original) == 1
        changed = next(v for v in store.videos() if v["id"] != original)
        assert store.count_items(changed["id"]) == 0


def test_sampled_hash_collision_never_merges_different_copies(folder):
    # Difference is outside all three sampled megabytes.
    content = bytearray(8 * 1024 * 1024)
    (folder / "a.mp4").write_bytes(content)
    content[2 * 1024 * 1024] = 1
    (folder / "b.mp4").write_bytes(content)
    assert media.content_hash(folder / "a.mp4") == media.content_hash(folder / "b.mp4")
    with Store(folder) as store:
        result = synchronize(store)
        assert result["unique_videos"] == 2 and result["duplicates"] == 0


def test_swapping_names_keeps_content_ids_and_evidence(folder):
    (folder / "b.mp4").write_bytes(b"footage B")
    with Store(folder) as store:
        synchronize(store)
        before = {v["path"]: v["id"] for v in store.videos()}
        (folder / "a.mp4").rename(folder / "temp")
        (folder / "b.mp4").rename(folder / "a.mp4")
        (folder / "temp").rename(folder / "b.mp4")
        synchronize(store)
        assert store.video(before[str(folder / "a.mp4")])["path"] == str(folder / "b.mp4")
        assert store.video(before[str(folder / "b.mp4")])["path"] == str(folder / "a.mp4")


def test_first_refresh_migrates_existing_index_without_rebuilding(folder):
    with Store(folder) as store:
        video_id = store.register(folder / "a.mp4", media.fingerprint(folder / "a.mp4"), media.metadata(None))
        seed(store)
        result = synchronize(store)
        assert result["added"] == result["changed"] == result["removed"] == 0
        assert store.video(video_id)["content_hash"]
        assert store.count_items(video_id) == 1


def test_failed_scan_does_not_purge_saved_evidence(folder, monkeypatch):
    with Store(folder) as store:
        synchronize(store)
        seed(store)

        def broken(_):
            raise PermissionError("Drive unavailable")

        monkeypatch.setattr(media, "discover", broken)
        with pytest.raises(PermissionError):
            synchronize(store)
        assert store.total_videos() == store.count_items(1) == 1


def test_atomic_refresh_rolls_back_when_file_cannot_be_read(folder, monkeypatch):
    with Store(folder) as store:
        synchronize(store)
        seed(store)
        (folder / "a.mp4").rename(folder / "renamed.mp4")
        monkeypatch.setattr(
            media, "content_hash", lambda *_a, **_k: (_ for _ in ()).throw(OSError("unplugged"))
        )
        with pytest.raises(OSError):
            synchronize(store)
        assert store.video(1)["path"] == str(folder / "a.mp4")
        assert store.count_items(1) == 1
