import subprocess
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from video_agent.app import create_app, snapshot
from video_agent.app_state import AppState
from video_agent.config import load_config
from video_agent.progress import estimate
from video_agent.signatures import stage_signatures
from video_agent.store import Store

HEADERS = {"X-Video-Agent": "local"}


def test_api_remembers_folder_and_methods_and_rejects_cross_site_writes(tmp_path):
    footage = tmp_path / "footage"
    footage.mkdir()
    directory = tmp_path / "state"
    app = create_app(directory)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/").json()["interface"] == "native"
    assert client.get("/assets/app.js").status_code == 404
    assert client.post("/api/libraries", json={"path": str(footage)}).status_code == 403
    assert (
        client.post(
            "/api/libraries", json={"path": str(footage)}, headers={**HEADERS, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "evil.test"}).status_code == 400
    assert client.post("/api/libraries", json={"path": ""}, headers=HEADERS).status_code == 400
    lib = client.post("/api/libraries", json={"path": str(footage)}, headers=HEADERS).json()
    cfg = lib["config"]
    cfg["ocr"]["sample_fps"] = 0.5
    saved = client.put(f"/api/libraries/{lib['id']}/config", json=cfg, headers=HEADERS)
    assert saved.status_code == 200
    restarted = AppState(directory)
    assert restarted.active_folder() == footage
    assert restarted.library()["config"]["ocr"]["sample_fps"] == 0.5
    cfg["visual"]["overlap_s"] = 20
    assert client.put(f"/api/libraries/{lib['id']}/config", json=cfg, headers=HEADERS).status_code == 400
    cfg["visual"]["overlap_s"] = 2
    cfg["hierarchy"]["enabled"] = "false"
    assert client.put(f"/api/libraries/{lib['id']}/config", json=cfg, headers=HEADERS).status_code == 400
    state = client.get("/api/state").json()
    assert state["mcp_config"]["mcpServers"]["video-agent"]["args"][-1] == str(directory)
    assert "--active" in state["mcp_command"]


def test_estimate_uses_same_model_measurements_and_never_invents_unknown_rates():
    cfg = load_config()
    signatures = stage_signatures(cfg)
    complete = {
        "id": 1,
        "metadata": {"duration_ms": 26000, "has_audio": True},
        "stages": [
            {
                "name": name,
                "status": "complete",
                "signature": signatures[name],
                "elapsed_s": 30,
                "checkpoint": 0,
            }
            for name in signatures
        ],
    }
    pending = {"id": 2, "metadata": complete["metadata"], "stages": []}
    result = estimate([complete, pending], cfg, {1: (10, 10), 2: (10, 0)})
    assert result["eta_s"] == 150 and result["fraction"] == pytest.approx(0.5)
    assert estimate([pending], cfg, {2: (10, 0)})["eta_s"] is None
    cfg.visual.model = "a new unmeasured model"
    assert estimate([complete, pending], cfg, {1: (10, 10), 2: (10, 0)})["eta_s"] is None


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for worker")


@pytest.fixture
def worker_library(tmp_path):
    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "source.mp4").write_bytes(b"fake media, decoded by test factories")
    state = AppState(tmp_path / "app")
    lib = state.select(str(folder))
    cfg = load_config()
    cfg.runtime.cache_dir = str(tmp_path / "models")
    cfg.speech.backend = cfg.ocr.backend = cfg.embedding.backend = "none"
    cfg.hierarchy.enabled = False
    state.save_config(lib["id"], asdict(cfg))
    return state, lib, folder


@pytest.fixture
def fake_worker(monkeypatch):
    """Exercise the real detached worker/SQLite/lock with a deterministic slow model."""
    original = subprocess.Popen
    children = []
    script = """
import json,time
from pathlib import Path
from video_agent import app_worker,media
from video_agent.pipeline import Indexer
media.metadata=lambda _: {"duration_ms":26000,"has_audio":False}
media.sample_window=lambda *a: [(a[1].start_ms+1,Path("unused.jpg"))]
class Model:
    def generate(self,*args,**kwargs):
        time.sleep(0.25)
        return json.dumps({"description":"A fox eats.","objects":["fox"],"actions":["eating"],"setting":"forest","uncertainty":[]})
    def close(self): pass
class TestIndexer(Indexer):
    def __init__(self,store,config,**kwargs):
        super().__init__(store,config,factories={"visual":lambda _:Model()},**kwargs)
app_worker.Indexer=TestIndexer
app_worker.main()
"""

    def spawn(args, **kwargs):
        assert args[1:3] == ["-m", "video_agent.app_worker"]
        child = original([args[0], "-c", script, *args[3:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def checkpoint(folder):
    try:
        with Store(folder, create=False) as store:
            stage = store.stage(1, "visual")
            return stage and stage["checkpoint"] >= 1
    except FileNotFoundError:
        return False


def test_killed_worker_is_recovered_and_resumes_committed_windows(worker_library, fake_worker):
    state, lib, folder = worker_library
    first = state.launch(lib["id"])
    with pytest.raises(ValueError, match="already running"):
        state.launch(lib["id"])
    wait_for(lambda: checkpoint(folder))
    fake_worker[0].kill()
    fake_worker[0].wait(timeout=5)
    AppState(state.directory).recover()  # simulate an application restart
    assert state.job(first["id"])["status"] == "interrupted"
    with Store(folder, create=False) as store:
        saved = store.items(1, stage="visual")
        assert len(saved) >= 1
    second = state.launch(lib["id"])
    wait_for(lambda: state.job(second["id"])["status"] not in {"starting", "running"})
    assert state.job(second["id"])["status"] == "complete"
    with Store(folder, create=False) as store:
        assert store.stage(1, "visual")["checkpoint"] == 3
        assert len(store.items(1, stage="visual")) == 3
        assert store.items(1, stage="visual")[0] == saved[0]


def test_pause_leaves_checkpoints_and_ui_restart_does_not_interrupt_worker(worker_library, fake_worker):
    state, lib, folder = worker_library
    job = state.launch(lib["id"])
    wait_for(lambda: checkpoint(folder))
    restarted = AppState(state.directory)
    restarted.recover()
    assert restarted.job(job["id"])["status"] == "running"
    restarted.pause(job["id"])
    wait_for(lambda: state.job(job["id"])["status"] == "paused")
    view = snapshot(state)
    assert view["job"]["status"] == "paused" and view["videos"][0]["needs_index"]
    resumed = state.launch(lib["id"])
    wait_for(lambda: state.job(resumed["id"])["status"] == "complete")
    assert not snapshot(state)["videos"][0]["needs_index"]


def test_mcp_model_access_and_index_worker_share_memory_lock(worker_library):
    state, lib, _ = worker_library
    with state.model_access() as available:
        assert available
        with pytest.raises(ValueError, match="already running"):
            state.launch(lib["id"])
    lock = state._lock()
    assert lock
    try:
        with state.model_access() as available:
            assert not available
    finally:
        lock.close()


def test_eta_reuses_completed_measurements_across_libraries(worker_library, fake_worker):
    state, lib, folder = worker_library
    job = state.launch(lib["id"])
    wait_for(lambda: state.job(job["id"])["status"] == "complete")
    cfg = load_config()
    cfg.runtime.cache_dir = str(folder.parent / "models")
    cfg.speech.backend = cfg.ocr.backend = cfg.embedding.backend = "none"
    cfg.hierarchy.enabled = False
    rates = state.timing_rates(cfg)
    assert rates["visual"] > 0
    new = {"id": 2, "metadata": {"duration_ms": 26000, "has_audio": False}, "stages": []}
    result = estimate([new], cfg, {}, historical_rates=rates)
    assert result["eta_s"] is not None
    cfg.visual.model = "different model"
    assert "visual" not in state.timing_rates(cfg)


def test_browser_numeric_roundtrip_does_not_invalidate_unchanged_methods():
    from video_agent.app_state import parse_config

    config = load_config()

    def javascript_json(value):
        if isinstance(value, dict):
            return {key: javascript_json(v) for key, v in value.items()}
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    roundtrip = parse_config(javascript_json(asdict(config)))
    assert stage_signatures(roundtrip) == stage_signatures(config)
    roundtrip.ocr.sample_fps = 0.5
    changed = {
        key for key, value in stage_signatures(roundtrip).items() if value != stage_signatures(config)[key]
    }
    assert changed == {"ocr"}


def test_clip_details_are_pinned_to_library_and_video_and_preserve_raw_evidence(tmp_path):
    from video_agent.pipeline import item

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    state_dir = tmp_path / "app"
    registry = AppState(state_dir)
    lib = registry.select(str(first))
    with Store(first) as store:
        video = store.register(
            first / "game.mov", "source", {"duration_ms": 12000, "width": 640, "height": 360, "fps": 24}
        )
        other = store.register(first / "other.mov", "other", {"duration_ms": 1000})
        for name in ("visual", "hierarchy", "ocr"):
            store.prepare(video, name, "test")
            store.stage_status(video, name, "complete")
        with store.db:
            window = item(
                video,
                "visual",
                "window",
                0,
                0,
                10000,
                "A blue kayak.",
                {"objects": ["kayak"], "uncertainty": ["location"]},
            )
            root = item(video, "hierarchy", "summary", "root", 0, 10000, "A kayak is washed.")
            ocr = item(
                video,
                "ocr",
                "ocr",
                1001,
                1001,
                1002,
                "English text",
                {"lines": [{"text": "English text", "confidence": 0.95, "box": [0.1, 0.2, 0.3, 0.4]}]},
            )
            store.put_item(window)
            store.put_item(root)
            store.put_item(ocr)
            store.link(root["id"], [window["id"]])
    registry.select(str(second))  # the detail window must stay on the first library
    with Store(second):
        pass
    client = TestClient(create_app(state_dir))
    prefix = f"/api/libraries/{lib['id']}/videos/{video}"
    details = client.get(prefix).json()
    assert details["path"].endswith("game.mov") and details["counts"]["window"] == 1
    assert not details["source_available"]
    page = client.get(prefix + "/observations", params={"kind": "ocr", "q": "English"}).json()
    assert page["total"] == 1 and page["items"][0]["end_s"] == 1.002
    raw = client.get(prefix + "/items/" + ocr["id"]).json()
    assert raw["payload"]["lines"][0]["confidence"] == 0.95
    assert raw["payload"]["lines"][0]["box"] == [0.1, 0.2, 0.3, 0.4]
    children = client.get(prefix + "/children/" + root["id"]).json()
    assert children["items"][0]["id"] == window["id"]
    assert client.get(f"/api/libraries/{lib['id']}/videos/{other}/items/" + ocr["id"]).status_code == 400
    assert client.get(prefix + "/observations", params={"kind": "ocr", "q": "missing"}).json()["total"] == 0
    assert (
        client.get(prefix + "/observations", params={"kind": "ocr", "offset": 1, "limit": 1}).json()["items"]
        == []
    )
    assert client.get(prefix + "/frame", params={"time_s": float("nan")}).status_code == 400
    assert client.get(prefix + "/frame", params={"time_s": 15}).status_code == 400
    assert client.get(prefix + "/frame", params={"time_s": 1}).status_code == 400
