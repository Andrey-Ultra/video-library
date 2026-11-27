import copy
import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from video_agent.app import create_app, snapshot
from video_agent.app_state import AppState
from video_agent.config import load_config
from video_agent.index_storage import CONFIG_NAME, INDEX_DIRECTORY
from video_agent.pipeline import item
from video_agent.signatures import stage_signatures
from video_agent.store import STAGES, Store

HEADERS = {"X-Video-Agent": "local"}


def seed_folder(folder):
    folder.mkdir()
    (folder / "source.mp4").write_bytes(b"source footage")
    cfg = load_config()
    cfg.speech.model = "mlx-community/parakeet-tdt-0.6b-v2"
    signatures = stage_signatures(cfg)
    with Store(folder) as store:
        video = store.register(
            folder / "source.mp4", "fingerprint", {"duration_ms": 600000, "has_audio": True}
        )
        for stage in STAGES:
            store.prepare(video, stage, signatures[stage])
            store.stage_status(video, stage, "complete", checkpoint=7, elapsed_s=12.5)
            if stage != "embedding":
                with store.db:
                    record = item(
                        video,
                        stage,
                        "window" if stage == "visual" else stage,
                        stage,
                        0,
                        1000,
                        stage + " evidence",
                    )
                    store.put_item(record)
                    store.db.execute(
                        "INSERT INTO vectors VALUES(?,?,?,?)",
                        (record["id"], signatures["embedding"], 1, b"1234"),
                    )
        store.stage_status(video, "ocr", "pending", checkpoint=120001, elapsed_s=23)
        store.stage_status(video, "speech", "pending", checkpoint=2, elapsed_s=17)
        store.save_config(asdict(cfg))
    return cfg


def test_new_folder_inspection_does_not_register_create_index_or_queue_work(tmp_path):
    folder = tmp_path / "new"
    folder.mkdir()
    state = AppState(tmp_path / "state")
    client = TestClient(create_app(state.directory))
    response = client.post("/api/libraries/inspect", json={"path": str(folder)}, headers=HEADERS)
    assert response.status_code == 200
    assert not response.json()["has_saved_config"] and response.json()["library_id"] is None
    assert state.libraries() == [] and state.queue_jobs() == []
    assert not (folder / INDEX_DIRECTORY).exists()
    # Cancelling the native setup sheet requires no rollback: inspection is read-only.
    with TestClient(create_app(state.directory)) as restarted:
        assert restarted.get("/api/state").json()["libraries"] == []


def test_confirmed_setup_saves_portable_config_before_any_processing(tmp_path):
    folder = tmp_path / "new"
    folder.mkdir()
    state = AppState(tmp_path / "state")
    client = TestClient(create_app(state.directory))
    cfg = asdict(load_config())
    cfg["speech"]["backend"] = "none"
    result = client.post(
        "/api/libraries/configure", json={"path": str(folder), "config": cfg}, headers=HEADERS
    )
    assert result.status_code == 200
    saved = result.json()
    assert state.active_id() == saved["id"] and not state.queue_jobs()
    file = folder / INDEX_DIRECTORY / CONFIG_NAME
    assert json.loads(file.read_text()) == cfg
    with Store(folder, create=False) as store:
        assert store.saved_config() == cfg and store.videos() == []
    other = AppState(tmp_path / "different-app-registry")
    inspected = other.inspect_folder(str(folder))
    assert inspected["has_saved_config"] and inspected["library_id"] is None
    assert inspected["config"] == cfg and other.libraries() == []
    selected = other.select(str(folder))
    assert selected["config"] == cfg  # ready to enqueue, without asking for models again


def test_partial_progress_survives_restore_in_another_app_registry(tmp_path):
    folder = tmp_path / "footage"
    cfg = seed_folder(folder)
    with Store(folder, create=False) as store:
        before = {name: store.stage(1, name) for name in STAGES}
        evidence = store.items(1)
    state = AppState(tmp_path / "new-registry")
    assert state.inspect_folder(str(folder))["has_saved_config"]
    lib = state.select(str(folder))
    assert lib["config"] == asdict(cfg)
    with Store(folder, create=False) as store:
        assert {name: store.stage(1, name) for name in STAGES} == before
        assert store.items(1) == evidence
        assert store.stage(1, "speech")["checkpoint"] == 2
        assert store.stage(1, "ocr")["checkpoint"] == 120001


def test_speech_change_resets_only_speech_and_search_immediately(tmp_path):
    folder = tmp_path / "footage"
    cfg = seed_folder(folder)
    state = AppState(tmp_path / "app")
    lib = state.select(str(folder))
    with Store(folder, create=False) as store:
        stages = {name: store.stage(1, name) for name in STAGES}
        vectors = dict(store.db.execute("SELECT item_id,data FROM vectors"))
        unchanged = {name: store.items(1, stage=name) for name in ("visual", "ocr", "hierarchy")}
    cfg.speech.model = "mlx-community/parakeet-tdt-0.6b-v3"
    state.save_config(lib["id"], asdict(cfg))
    with Store(folder, create=False) as store:
        for name in unchanged:
            assert store.stage(1, name) == stages[name]
            assert store.items(1, stage=name) == unchanged[name]
        assert store.stage(1, "speech")["status"] == "pending"
        assert store.stage(1, "speech")["checkpoint"] == 0
        assert store.stage(1, "embedding")["status"] == "pending"
        assert store.items(1, stage="speech") == []
        after = dict(store.db.execute("SELECT item_id,data FROM vectors"))
        assert after == {key: value for key, value in vectors.items() if ":speech:" not in key}
    assert json.loads((folder / INDEX_DIRECTORY / CONFIG_NAME).read_text()) == asdict(cfg)
    view = snapshot(state)
    assert view["videos"][0]["needs_index"]
    assert next(s for s in view["videos"][0]["stages"] if s["name"] == "ocr")["checkpoint"] == 120001


def test_unchanged_settings_preserve_every_checkpoint_and_waiting_job(tmp_path):
    folder = tmp_path / "footage"
    cfg = seed_folder(folder)
    state = AppState(tmp_path / "app")
    lib = state.select(str(folder))
    state.pause_queue()
    job = state.launch(lib["id"])
    with Store(folder, create=False) as store:
        before = {name: store.stage(1, name) for name in STAGES}
    state.save_config(lib["id"], asdict(cfg))
    assert state.job(job["id"])["status"] == "queued"
    with Store(folder, create=False) as store:
        assert {name: store.stage(1, name) for name in STAGES} == before


def test_active_folder_rejects_settings_change_without_mutating_index(tmp_path):
    folder = tmp_path / "footage"
    cfg = seed_folder(folder)
    state = AppState(tmp_path / "app")
    lib = state.select(str(folder))
    state.pause_queue()
    job = state.launch(lib["id"])
    state.update_job(job["id"], status="running")
    file = folder / INDEX_DIRECTORY / CONFIG_NAME
    before = file.read_bytes()
    changed = asdict(cfg)
    changed["speech"]["model"] = "changed-model"
    client = TestClient(create_app(state.directory))
    response = client.put(f"/api/libraries/{lib['id']}/config", json=changed, headers=HEADERS)
    assert response.status_code == 409
    assert file.read_bytes() == before and state.library(lib["id"])["config"] == asdict(cfg)


def test_config_change_is_pinned_to_context_folder_not_active_selection(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    cfg = seed_folder(first)
    seed_folder(second)
    state = AppState(tmp_path / "state")
    a = state.select(str(first))
    b = state.select(str(second))
    cfg.speech.model = "new-model"
    state.save_config(a["id"], asdict(cfg))
    assert state.active_id() == b["id"]
    with Store(second, create=False) as store:
        assert store.stage(1, "speech")["checkpoint"] == 2


def test_apply_failure_restores_database_config_and_json_snapshot(tmp_path, monkeypatch):
    folder = tmp_path / "footage"
    cfg = seed_folder(folder)
    with Store(folder) as store:
        before = {name: store.stage(1, name) for name in STAGES}
        original_config = copy.deepcopy(store.saved_config())
        file = store.directory / CONFIG_NAME
        original_json = file.read_bytes()
        cfg.speech.model = "replacement"
        original = store._invalidate_stage

        def fail(video_id, name, clear_vectors):
            original(video_id, name, clear_vectors)
            raise RuntimeError("interrupted settings transaction")

        monkeypatch.setattr(store, "_invalidate_stage", fail)
        with pytest.raises(RuntimeError, match="transaction"):
            store.apply_config(asdict(cfg), stage_signatures(cfg))
        assert {name: store.stage(1, name) for name in STAGES} == before
        assert store.saved_config() == original_config and file.read_bytes() == original_json
        assert not list(store.directory.glob(".config-*.tmp"))


def test_complete_saved_folder_opens_without_setup_or_unnecessary_resume(tmp_path):
    folder = tmp_path / "complete"
    seed_folder(folder)
    with Store(folder) as store:
        for stage in STAGES:
            store.stage_status(1, stage, "complete", checkpoint=7)
    state = AppState(tmp_path / "new-registry")
    inspected = state.inspect_folder(str(folder))
    assert inspected["has_saved_config"] and not inspected["needs_resume"]
    assert state.libraries() == [] and state.queue_jobs() == []
