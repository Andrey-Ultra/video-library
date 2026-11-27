"""Regression coverage for batched reads and transactional queue recovery."""

import contextlib
import json
import os
from dataclasses import asdict

import pytest

from video_agent.app_state import AppState
from video_agent.config import load_config
from video_agent.store import STAGES, Store


def test_video_pages_fetch_stages_in_two_queries_without_losing_fields(tmp_path):
    with Store(tmp_path) as store:
        for index in range(40):
            video_id = store.register(
                tmp_path / f"clip-{index:03}.mp4", f"hash-{index}", {"duration_ms": 1000}
            )
            for stage in STAGES:
                store.prepare(video_id, stage, "pinned", provenance={"test": index})
                store.stage_status(video_id, stage, "complete", checkpoint=index, elapsed_s=index / 2)
        expected = [store.video(index) for index in range(11, 21)]
        queries = []
        store.db.set_trace_callback(queries.append)
        assert store.videos(offset=10, limit=10) == expected
        assert sum(sql.startswith("SELECT") for sql in queries) == 2
        queries.clear()
        assert len(store.videos(limit=1_000_000)) == 40
        assert sum(sql.startswith("SELECT") for sql in queries) == 2
        assert store.videos(offset=100) == []
        assert store.videos(limit=0) == []
        store.db.set_trace_callback(None)


def seed_queue(state):
    config = json.dumps(asdict(load_config()))
    with state.connect() as db:
        db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','0')")
        db.executemany(
            "INSERT INTO jobs(id,library_id,folder,kind,config,status,created_at,position) VALUES(?,?,'/unused','index',?,?,?,?)",
            [(f"job-{i}", f"library-{i}", config, "queued", i + 1, i + 1) for i in range(3)],
        )


def test_recovery_pauses_before_claiming_next_job_in_same_transaction(tmp_path):
    state = AppState(tmp_path)
    seed_queue(state)
    state.update_job("job-0", status="running")  # A crashed prior runner left this row behind.
    memory = state._lock()
    assert memory is not None
    runner_fd = os.open(tmp_path / "runner-test.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert state.next_queued(runner_fd, memory) is None
        assert state.queue_paused()
        assert state.job("job-0")["status"] == "interrupted"
        assert state.job("job-1")["status"] == state.job("job-2")["status"] == "queued"
    finally:
        memory.close()
        with contextlib.suppress(OSError):
            os.close(runner_fd)


def test_queue_and_latest_job_use_single_consistent_connection(tmp_path, monkeypatch):
    state = AppState(tmp_path)
    seed_queue(state)
    original = state.connect
    connections = []

    @contextlib.contextmanager
    def connect():
        connections.append(True)
        with original() as db:
            yield db

    monkeypatch.setattr(state, "connect", connect)
    queue = state.queue_jobs()
    assert len(queue) == 3 and len(connections) == 1
    assert isinstance(queue[0]["config"], dict) and queue[0]["result"] is None
    connections.clear()
    assert state.latest_job("library-0")["id"] == "job-0" and len(connections) == 1
    assert state.latest_job("missing") is None


def test_cancellation_does_not_decode_configuration_or_results(tmp_path):
    state = AppState(tmp_path)
    seed_queue(state)
    with state.connect() as db:
        db.execute(
            "UPDATE jobs SET config='malformed old config',result='malformed old result' WHERE id='job-0'"
        )
    assert state.cancelled("job-0") is False
    state.update_job("job-0", cancel_requested=1)
    assert state.cancelled("job-0") is True
    with pytest.raises(ValueError, match="Job not found"):
        state.cancelled("missing")


@pytest.mark.parametrize(
    "content",
    [
        "[visual]\nframes_per_window = true",
        "[ocr]\nsample_fps = true",
        "[visual]\nmodel = 123",
        'visual = "not a table"',
        '[visual]\nbackend = ["mlx"]',
    ],
)
def test_toml_invalid_types_fail_before_indexing(tmp_path, content):
    file = tmp_path / "invalid.toml"
    file.write_text(content)
    with pytest.raises(ValueError, match="Invalid type|must be a table"):
        load_config(file)


def test_valid_configs_keep_signature_after_validation_and_json_roundtrip():
    from video_agent.config import parse_config
    from video_agent.signatures import stage_signatures

    for profile in ("mac", "quality", "cuda"):
        config = load_config(profile=profile)
        before = stage_signatures(config)
        config.validate()
        assert before == stage_signatures(config) == stage_signatures(parse_config(asdict(config)))


def test_toml_cache_type_is_checked_before_resolving_paths(tmp_path):
    from video_agent.config import load_config

    config = tmp_path / "invalid-cache.toml"
    config.write_text("[runtime]\ncache_dir = true\n")
    with pytest.raises(ValueError, match="runtime.cache_dir"):
        load_config(config)
