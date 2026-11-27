import fcntl
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_agent.app import create_app
from video_agent.app_state import AppState
from video_agent.index_storage import DB_NAME, INDEX_DIRECTORY, index_path
from video_agent.pipeline import item
from video_agent.store import Store

HEADERS = {"X-Video-Agent": "local"}


@pytest.fixture
def libraries(tmp_path):
    state_dir = tmp_path / "state"
    state = AppState(state_dir)
    result = []
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        source = folder / "source.mp4"
        source.write_bytes(b"source video must be preserved")
        with Store(folder) as store:
            video = store.register(source, name, {"duration_ms": 2000, "has_audio": True})
            store.prepare(video, "speech", "test")
            with store.db:
                store.put_item(item(video, "speech", "speech", 1, 0, 1000, "Saved transcript"))
            store.stage_status(video, "speech", "complete", checkpoint=7)
        result.append(state.select(str(folder)))
    client = TestClient(create_app(state_dir))
    return state, client, result


def erase(client, library, confirmed=True):
    return client.request(
        "DELETE", f"/api/libraries/{library['id']}", json={"confirmed": confirmed}, headers=HEADERS
    )


def test_delete_removes_library_and_index_preserves_sources_and_selects_remaining(libraries):
    state, client, (first, second) = libraries
    source = Path(second["path"]) / "source.mp4"
    before = source.stat()
    response = erase(client, second)
    assert response.status_code == 200
    assert response.json()["removed"] and response.json()["index_deleted"]
    assert state.active_id() == first["id"]
    assert [lib["id"] for lib in AppState(state.directory).libraries()] == [first["id"]]
    assert not index_path(second["path"]).exists()
    assert source.stat() == before and source.read_bytes() == b"source video must be preserved"
    added = client.post("/api/libraries", json={"path": second["path"]}, headers=HEADERS).json()
    assert added["id"] != second["id"]
    assert client.get("/api/state").json()["videos"] == []


def test_deleting_last_library_stays_empty_after_service_restart(libraries):
    state, client, libs = libraries
    for lib in libs:
        assert erase(client, lib).status_code == 200
    assert state.active_id() is None
    with TestClient(create_app(state.directory)) as restarted:
        view = restarted.get("/api/state").json()
        assert view["libraries"] == [] and view["active_id"] is None


def test_delete_requires_confirmation_and_local_header(libraries):
    state, client, (first, second) = libraries
    path = f"/api/libraries/{first['id']}"
    assert client.request("DELETE", path, json={"confirmed": True}).status_code == 403
    for value in (False, "true", 1, None):
        assert erase(client, first, value).status_code == 400
        assert index_path(first["path"]).is_file()
    assert client.delete(f"/api/libraries/{first['id']}").status_code == 403


def test_delete_index_is_pinned_to_context_folder_and_preserves_sources_and_other_index(libraries):
    state, client, (first, second) = libraries
    parent = Path(first["path"])
    source = parent / "source.mp4"
    source_stat = source.stat()
    sidecar = parent / "._.video-library"
    sidecar.write_bytes(b"\x00\x05\x16\x07" + b"metadata")
    assert state.active_id() == second["id"]
    assert erase(client, first).status_code == 200
    assert not (parent / INDEX_DIRECTORY).exists() and not sidecar.exists()
    assert source.stat() == source_stat and source.read_bytes() == b"source video must be preserved"
    assert len(state.libraries()) == 1 and state.active_id() == second["id"]
    assert index_path(second["path"]).is_file()
    with Store(first["path"]) as store:
        assert store.videos() == []  # no stale WAL resurrected the old index


def test_waiting_tasks_are_removed_only_for_target_folder(libraries):
    state, client, (first, second) = libraries
    state.pause_queue()
    a, b = state.launch(first["id"]), state.launch(second["id"])
    assert erase(client, first).status_code == 200
    assert [job["id"] for job in state.queue_jobs()] == [b["id"]]
    assert state.job(b["id"])["status"] == "queued"
    with pytest.raises(ValueError, match="Job not found"):
        state.job(a["id"])
    assert (
        client.post(f"/api/libraries/{first['id']}/jobs", json={"kind": "index"}, headers=HEADERS).status_code
        == 400
    )
    assert not index_path(first["path"]).exists()
    assert state.queue_paused()  # remaining work still respects the explicit pause


@pytest.mark.parametrize("status", ["starting", "running", "pausing"])
def test_active_target_task_blocks_both_removal_actions(libraries, status):
    state, client, (first, second) = libraries
    state.pause_queue()
    job = state.launch(first["id"])
    state.update_job(job["id"], status=status)
    for response in (erase(client, first),):
        assert response.status_code == 409 and "Pause" in response.json()["detail"]
    assert state.job(job["id"])["status"] == status
    assert index_path(first["path"]).is_file() and len(state.libraries()) == 2
    # Another library's active task does not prevent deleting this idle folder.
    assert erase(client, second).status_code == 200


def test_paused_task_can_be_deleted_with_index(libraries):
    state, client, (first, second) = libraries
    state.pause_queue()
    job = state.launch(first["id"])
    state.update_job(job["id"], status="paused")
    assert erase(client, first).status_code == 200
    assert state.queue_jobs() == [] and not index_path(first["path"]).exists()
    assert not state.queue_paused()
    with pytest.raises(ValueError):
        state.library(first["id"])


def test_open_reader_or_old_writer_prevents_index_deletion(libraries):
    state, client, (first, second) = libraries
    with Store(first["path"], create=False) as store:
        assert erase(client, first).status_code == 409
        assert erase_index(client, first).status_code == 409
        assert store.items(1, stage="speech")
    with (index_path(first["path"]).parent / "index.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert erase(client, first).status_code == 409
        assert erase_index(client, first).status_code == 409
    assert index_path(first["path"]).is_file()


def test_unknown_files_or_symlink_index_are_never_deleted(libraries, tmp_path):
    state, client, (first, second) = libraries
    folder = index_path(first["path"]).parent
    video = folder / "user-video.mp4"
    video.write_bytes(b"must not delete")
    assert erase(client, first).status_code == 400
    assert video.read_bytes() == b"must not delete" and (folder / DB_NAME).is_file()
    video.unlink()
    outside = tmp_path / "outside-index"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    assert erase(client, first).status_code == 400
    assert (outside / DB_NAME).is_file() and folder.is_symlink()


def test_offline_folder_is_kept_until_its_index_can_be_deleted(libraries):
    state, client, (first, second) = libraries
    folder = Path(first["path"])
    offline = folder.with_name("offline")
    folder.rename(offline)
    assert erase(client, first).status_code == 400
    assert state.library(first["id"])
    assert (offline / INDEX_DIRECTORY / DB_NAME).is_file()


def test_legacy_and_current_indexes_are_both_removed_without_fallback(libraries):
    state, client, (first, second) = libraries
    from shutil import copytree

    folder = Path(first["path"])
    copytree(folder / INDEX_DIRECTORY, folder / ".video-agent")
    assert erase(client, first).status_code == 200
    assert not (folder / INDEX_DIRECTORY).exists() and not (folder / ".video-agent").exists()
    assert not index_path(folder).exists()


def erase_index(client, library, confirmed=True):
    return client.request(
        "DELETE",
        f"/api/libraries/{library['id']}/index",
        json={"confirmed": confirmed},
        headers=HEADERS,
    )


def test_reset_index_keeps_folder_settings_and_waits_for_setup(libraries):
    state, client, (first, second) = libraries
    config = first["config"]
    config["proxy"]["resolution"] = "720p"
    state.save_config(first["id"], config)
    source = Path(first["path"]) / "source.mp4"
    before = source.stat()
    response = erase_index(client, first)
    assert response.status_code == 200
    result = response.json()
    assert result["index_deleted"] and result["warnings"] == []
    assert result["library"]["id"] == first["id"]
    assert result["library"]["config"] == config
    assert {lib["id"] for lib in state.libraries()} == {first["id"], second["id"]}
    assert state.active_id() == first["id"]
    assert not index_path(first["path"]).parent.exists()
    assert source.stat() == before and source.read_bytes() == b"source video must be preserved"
    assert index_path(second["path"]).is_file()
    # Opening/cancelling setup must not recreate or automatically queue the erased index.
    view = TestClient(create_app(state.directory)).get("/api/state").json()
    assert view["videos"] == [] and view["queue"]["jobs"] == []
    assert not index_path(first["path"]).parent.exists()
    # Saving setup creates the new index using the same registered folder ID.
    config["ocr"]["sample_fps"] = 2
    saved = client.put(f"/api/libraries/{first['id']}/config", json=config, headers=HEADERS)
    assert saved.status_code == 200
    with Store(first["path"], create=False) as store:
        assert store.videos() == [] and store.saved_config() == config


def test_reset_index_requires_explicit_confirmation_and_local_header(libraries):
    state, client, (first, second) = libraries
    path = f"/api/libraries/{first['id']}/index"
    assert client.request("DELETE", path, json={"confirmed": True}).status_code == 403
    for value in (False, "true", 1, None):
        assert erase_index(client, first, value).status_code == 400
        assert index_path(first["path"]).is_file()


def test_reset_index_removes_only_target_waiting_tasks(libraries):
    state, client, (first, second) = libraries
    state.pause_queue()
    a, b = state.launch(first["id"]), state.launch(second["id"])
    state.update_job(a["id"], status="paused")
    assert erase_index(client, first).status_code == 200
    assert state.library(first["id"])["id"] == first["id"]
    assert [job["id"] for job in state.queue_jobs()] == [b["id"]]
    assert state.queue_paused() and not index_path(first["path"]).exists()
    with pytest.raises(ValueError, match="Job not found"):
        state.job(a["id"])


@pytest.mark.parametrize("status", ["starting", "running", "pausing"])
def test_reset_index_refuses_active_target_and_preserves_its_data(libraries, status):
    state, client, (first, second) = libraries
    state.pause_queue()
    job = state.launch(first["id"])
    state.update_job(job["id"], status=status)
    response = erase_index(client, first)
    assert response.status_code == 409
    assert state.job(job["id"])["status"] == status
    with Store(first["path"], create=False) as store:
        assert store.items(1, stage="speech")
    # An unrelated active library does not prevent resetting this idle one.
    assert erase_index(client, second).status_code == 200
    assert len(state.libraries()) == 2
