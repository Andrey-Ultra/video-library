import fcntl
import hashlib
import json
import struct
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_agent import model_assets as assets
from video_agent.app import create_app
from video_agent.backends.common import ModelSetupError, load_local_model
from video_agent.config import Config, parse_config

REPO = assets.CATALOG[0]["id"]
HEADERS = {"X-Video-Agent": "local"}


def tiny_snapshot(hub, repo=REPO, revision="abc", sharded=False):
    root = hub / ("models--" + repo.replace("/", "--"))
    path = root / "snapshots" / revision
    path.mkdir(parents=True)
    (root / "refs").mkdir()
    (root / "refs/main").write_text(revision)
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "preprocessor_config.json"):
        (path / name).write_text("{}")
    header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    weight = "model-1.safetensors" if sharded else "model.safetensors"
    (path / weight).write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)
    (path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"weight": weight}}))
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    cfg = Config()
    cfg.runtime.cache_dir = str(tmp_path / "models")
    return cfg, assets.hub_directory(cfg), assets.ModelAssets(tmp_path / "state")


def test_inventory_is_offline_detects_ready_missing_and_truncated_models(setup, monkeypatch):
    cfg, hub, manager = setup
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: pytest.fail("inventory must stay offline"))
    assert manager.resources(asdict(cfg))["models"][0]["status"] == "missing"
    path = tiny_snapshot(hub)
    assert manager.resources(asdict(cfg))["models"][0]["status"] == "ready"
    file = path / "model.safetensors"
    file.write_bytes(file.read_bytes()[:-1])
    assert manager.resources(asdict(cfg))["models"][0]["status"] == "failed"


def test_consolidated_weights_work_with_stale_official_shard_index(setup):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    (path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"weight": "absent-shard.safetensors"}})
    )
    assert manager.resources(asdict(cfg))["models"][0]["status"] == "ready"
    (path / "model.safetensors").unlink()
    assert manager.resources(asdict(cfg))["models"][0]["status"] == "failed"


def test_shards_are_all_required(setup):
    _, hub, _ = setup
    path = tiny_snapshot(hub, sharded=True)
    assets.validate_snapshot(path, "visual")
    (path / "model-1.safetensors").unlink()
    with pytest.raises(ValueError, match="missing"):
        assets.validate_snapshot(path, "visual")


def test_interrupted_or_failed_verification_never_becomes_ready_from_headers(setup):
    cfg, hub, manager = setup
    tiny_snapshot(hub)
    state, lock = manager.files(hub, REPO)
    assets._write(state, {"status": "checking", "total_files": 5})
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "failed"
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert manager.status(cfg, assets.CATALOG[0])["status"] == "checking"
    assets._write(state, {"status": "failed", "repair": True, "error": "Checksum mismatch"})
    assert manager.status(cfg, assets.CATALOG[0])["error"] == "Checksum mismatch"


def remote_fixture(monkeypatch, path, corrupt=False, fail=False):
    import huggingface_hub

    files = [
        SimpleNamespace(
            rfilename=p.name,
            size=p.stat().st_size,
            lfs=SimpleNamespace(sha256=hashlib.sha256(p.read_bytes()).hexdigest()),
        )
        for p in path.iterdir()
    ]
    info = SimpleNamespace(sha=path.name, siblings=files)
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: SimpleNamespace(model_info=lambda *a, **k: info))
    calls = []

    def get(repo, filename, **kwargs):
        calls.append(kwargs)
        if fail:
            raise ConnectionError("offline")
        if corrupt and filename == "model.safetensors":
            data = (path / filename).read_bytes()
            (path / filename).write_bytes(data[:-1] + b"1")  # valid header/length, wrong hash
        return str(path / filename)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", get)
    return calls


def test_download_verifies_hashes_then_publishes_ready(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    calls = remote_fixture(monkeypatch, path)
    state, _ = manager.files(hub, REPO)
    assets.download(REPO, hub, state)
    assert json.loads(state.read_text()) == {"status": "ready"}
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "ready"
    assert calls and all(c["revision"] == "abc" and c["force_download"] is False for c in calls)


def test_download_hash_mismatch_is_visible_and_requests_repair(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    remote_fixture(monkeypatch, path, corrupt=True)
    state, _ = manager.files(hub, REPO)
    assets.download(REPO, hub, state)
    error = json.loads(state.read_text())
    assert error["status"] == "failed" and error["repair"]
    assert "verification" in error["error"]
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "failed"


def test_network_failure_is_persisted_and_retry_can_resume(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    remote_fixture(monkeypatch, path, fail=True)
    state, _ = manager.files(hub, REPO)
    assets.download(REPO, hub, state)
    assert json.loads(state.read_text())["status"] == "failed"
    calls = remote_fixture(monkeypatch, path)
    assets.download(REPO, hub, state)
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "ready"
    assert all(not c["force_download"] for c in calls)


def test_no_space_is_reported_before_downloading(setup, monkeypatch):
    _, hub, manager = setup
    path = tiny_snapshot(hub)
    calls = remote_fixture(monkeypatch, path)
    monkeypatch.setattr(assets.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    state, _ = manager.files(hub, REPO)
    assets.download(REPO, hub, state)
    assert not calls and "disk space" in json.loads(state.read_text())["error"]


def test_download_is_deduplicated_and_child_inherits_lease(setup, monkeypatch):
    cfg, hub, manager = setup
    spawned = []

    def launch(args, **kwargs):
        spawned.append((args, kwargs))
        fd = kwargs["pass_fds"][0]
        assert "--lock-fd" in args
        # Repeated clicks / a second service cannot start a duplicate downloader.
        assert manager.start(asdict(cfg), REPO) == {"started": False}
        assert fd >= 0
        return SimpleNamespace(wait=lambda: None)

    monkeypatch.setattr(assets.subprocess, "Popen", launch)
    assert manager.start(asdict(cfg), REPO) == {"started": True}
    assert len(spawned) == 1
    assert "--repair" not in spawned[0][0]


@pytest.mark.parametrize("memory,frames,model", [(8, 4, 0), (16, 6, 0), (24, 6, 0), (32, 8, 1), (64, 8, 1)])
def test_mac_recommendations_validate_and_choose_memory_tier(memory, frames, model):
    cfg = parse_config(assets.recommended_config({"memory_gb": memory}))
    assert cfg.visual.frames_per_window == frames
    assert cfg.visual.model == assets.CATALOG[model]["id"]
    assert cfg.speech.backend == "parakeet" and cfg.ocr.backend == "vision"
    assert cfg.visual.backend == "mlx"


def test_model_api_is_local_validated_and_does_not_change_libraries(setup, monkeypatch, tmp_path):
    cfg, _, _ = setup
    client = TestClient(create_app(tmp_path / "api"))
    assert client.post("/api/models/status", json={}).status_code == 403
    response = client.post("/api/models/status", json={"config": asdict(cfg)}, headers=HEADERS)
    assert response.status_code == 200
    assert set(response.json()["required"]) == {cfg.visual.model, cfg.speech.model, cfg.embedding.model}
    assert (
        client.post(
            "/api/models/download", json={"model": "../../arbitrary", "config": asdict(cfg)}, headers=HEADERS
        ).status_code
        == 400
    )
    assert client.get("/api/state").json()["libraries"] == []


def test_shared_model_load_error_has_actionable_message():
    def broken(model):
        raise OSError("bad weights")

    with pytest.raises(ModelSetupError, match="Indexing settings"):
        load_local_model(broken, REPO)


def test_delete_model_removes_only_its_cache_and_can_download_again(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    other = tiny_snapshot(hub, assets.CATALOG[1]["id"])
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "ready"
    assert manager.delete(asdict(cfg), REPO) == {"deleted": True}
    assert not path.exists() and other.is_dir()
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "missing"
    started = []
    monkeypatch.setattr(
        assets.subprocess, "Popen", lambda *a, **k: started.append(a) or SimpleNamespace(wait=lambda: None)
    )
    assert manager.start(asdict(cfg), REPO)["started"] and len(started) == 1


def test_delete_model_refuses_builtin_arbitrary_paths_unknown_files_and_symlinks(setup):
    cfg, hub, manager = setup
    for model in ["vision", "../outside", "/tmp/model", "other/repo"]:
        with pytest.raises(ValueError):
            manager.delete(asdict(cfg), model)
    path = tiny_snapshot(hub)
    root = path.parent.parent
    source = root / "my-video.mov"
    source.write_bytes(b"keep")
    with pytest.raises(ValueError, match="unrecognized"):
        manager.delete(asdict(cfg), REPO)
    assert source.read_bytes() == b"keep"
    source.unlink()
    outside = hub / "outside"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        manager.delete(asdict(cfg), REPO)
    assert outside.is_dir()


def test_delete_model_refuses_download_leases_and_open_weights(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    _, lease = manager.files(hub, REPO)
    lease.parent.mkdir(parents=True)
    with lease.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="downloading"):
            manager.delete(asdict(cfg), REPO)
    hf_lock = hub / ".locks" / ("models--" + REPO.replace("/", "--")) / "weights.lock"
    hf_lock.parent.mkdir(parents=True)
    with hf_lock.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="Another downloader"):
            manager.delete(asdict(cfg), REPO)
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="123"))
    with pytest.raises(ValueError, match="open in another process"):
        manager.delete(asdict(cfg), REPO)
    assert path.is_dir()


def test_delete_api_confirmation_queue_and_shared_model_lease(setup, tmp_path, monkeypatch):
    cfg, hub, _ = setup
    tiny_snapshot(hub)
    app = create_app(tmp_path / "api-delete")
    state = app.state.registry
    client = TestClient(app)
    body = {"config": asdict(cfg), "model": REPO, "confirmed": True}
    assert client.post("/api/models/delete", json=body).status_code == 403
    assert (
        client.post("/api/models/delete", json={**body, "confirmed": False}, headers=HEADERS).status_code
        == 400
    )
    folder = tmp_path / "video"
    folder.mkdir()
    lib = state.select(str(folder))
    state.pause_queue()
    job = state.launch(lib["id"])
    assert client.post("/api/models/delete", json=body, headers=HEADERS).status_code == 409
    state.update_job(job["id"], status="paused")
    with (state.directory / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert client.post("/api/models/delete", json=body, headers=HEADERS).status_code == 409
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert client.post("/api/models/delete", json=body, headers=HEADERS).status_code == 200
    assert state.library(lib["id"]) and state.job(job["id"])["status"] == "paused"


@pytest.mark.parametrize(
    "contents", ["{", "[]", "null", '{"status": []}', '{"status":"downloading","total_files":"lots"}']
)
def test_bad_download_state_is_a_retryable_model_status(setup, contents):
    cfg, hub, manager = setup
    state, _ = manager.files(hub, REPO)
    state.parent.mkdir(parents=True)
    state.write_text(contents)
    result = manager.status(cfg, assets.CATALOG[0])
    assert result["status"] == "failed" and "Retry" in result["error"]


def test_network_retry_preserves_partial_download_instead_of_forcing_full_download(setup, monkeypatch):
    cfg, hub, manager = setup
    tiny_snapshot(hub)
    state, _ = manager.files(hub, REPO)
    assets._write(state, {"status": "failed", "repair": False, "error": "offline"})
    # Missing tokenizer makes the snapshot incomplete, but it does not mean a
    # completed blob was corrupted. Keep resumable blobs for network retries.
    (assets.snapshot_path(hub, REPO) / "tokenizer.json").unlink()
    calls = []
    monkeypatch.setattr(
        assets.subprocess, "Popen", lambda args, **k: calls.append(args) or SimpleNamespace(wait=lambda: None)
    )
    assert manager.start(asdict(cfg), REPO)["started"]
    assert "--repair" not in calls[0]


@pytest.mark.parametrize("revision", ["..", ".", "../outside"])
def test_invalid_snapshot_revision_is_reported_without_reading_outside_cache(setup, revision):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    (path.parent.parent / "refs/main").write_text(revision)
    result = manager.status(cfg, assets.CATALOG[0])
    assert result["status"] == "failed" and "revision" in result["error"]


@pytest.mark.parametrize(
    "header",
    [
        [],
        {"w": []},
        {"w": {"data_offsets": [0]}},
        {"w": {"data_offsets": [-1, 4]}},
        {"w": {"data_offsets": [False, 4]}},
        {"a": {"data_offsets": [0, 3]}, "b": {"data_offsets": [2, 4]}},
    ],
)
def test_malformed_safetensor_headers_do_not_crash_inventory(setup, header):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    raw = json.dumps(header).encode()
    (path / "model.safetensors").write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 4)
    assert manager.status(cfg, assets.CATALOG[0])["status"] == "failed"


def test_corrupt_revision_can_be_repaired_by_retry(setup, monkeypatch):
    cfg, hub, manager = setup
    path = tiny_snapshot(hub)
    (path.parent.parent / "refs/main").write_text("..")
    calls = []
    monkeypatch.setattr(
        assets.subprocess, "Popen", lambda args, **k: calls.append(args) or SimpleNamespace(wait=lambda: None)
    )
    assert manager.start(asdict(cfg), REPO)["started"]
    assert "--repair" in calls[0]
