"""Regression cases for model partition errors and request-scoped retrieval work."""

import json
import time

import numpy as np
import pytest

from video_agent import answering, backends, library, media
from video_agent.config import Config
from video_agent.hierarchy import validated_groups
from video_agent.pipeline import Indexer, item
from video_agent.progress import ProgressTracker
from video_agent.signatures import embedding_namespace, stage_signatures
from video_agent.store import Store


@pytest.mark.parametrize(
    "groups",
    [
        None,
        [],
        [None],
        [1],
        ["bad"],
        [{}],
        [{"children": "a", "description": "bad"}],
        [{"children": [{}], "description": "bad"}],
        [{"children": ["b", "a"], "description": "wrong order"}],
        [{"children": ["a", "a"], "description": "duplicate"}],
    ],
)
def test_invalid_event_partitions_are_retryable_validation_errors(groups):
    with pytest.raises(ValueError):
        validated_groups({"groups": groups}, ["a", "b"])


def test_valid_event_partition_is_preserved():
    groups = [{"children": ["a"], "description": "First."}, {"children": ["b"], "description": "Second."}]
    assert validated_groups({"groups": groups}, ["a", "b"]) == groups


def test_bad_model_responses_preserve_each_original_window_and_mark_fallback():
    calls = []

    class Broken:
        def generate(self, prompt, **kwargs):
            calls.append(prompt)
            return json.dumps({"groups": [None]})

    batch = [
        {"id": "a", "text": "A fox appears.", "payload": {"description": "A fox appears."}},
        {"id": "b", "text": "A bird flies.", "payload": {"description": "A bird flies."}},
    ]
    groups = Indexer(None, Config())._event_groups(Broken(), batch, [])
    assert len(calls) == 2
    assert [group["children"] for group in groups] == [["a"], ["b"]]
    assert [group["description"] for group in groups] == ["A fox appears.", "A bird flies."]
    assert all(group["grouping"] == "single_window_fallback" for group in groups)


def test_event_backend_failure_is_not_replaced_with_a_successful_fallback():
    class Broken:
        def generate(self, *args, **kwargs):
            raise ValueError("backend cannot decode input")

    with pytest.raises(ValueError, match="backend cannot decode"):
        Indexer(None, Config())._event_groups(Broken(), [{"id": "a"}], [])


def indexed_evidence(store, cfg, tmp_path):
    video = store.register(tmp_path / "source.mov", "fingerprint", {"duration_ms": 2000})
    for stage in ["visual", "speech", "ocr", "embedding"]:
        store.prepare(video, stage, "test")
        store.stage_status(video, stage, "complete")
    with store.db:
        for stage, kind in [("visual", "window"), ("speech", "speech"), ("ocr", "ocr")]:
            node = item(video, stage, kind, "one", 0, 1000, "A fox appears.")
            store.put_item(node)
            store.db.execute(
                "INSERT INTO vectors VALUES(?,?,?,?)",
                (node["id"], embedding_namespace(cfg), 2, np.array([1, 0], dtype="<f4").tobytes()),
            )
    return video


def test_answer_encodes_query_once_across_all_evidence_quotas_and_releases_before_answer(
    tmp_path, monkeypatch
):
    cfg = Config()
    cfg.runtime.cache_dir = str(tmp_path / "models")
    calls = []

    class Embedder:
        def encode(self, texts, query=False):
            calls.append(("encode", tuple(texts), query))
            return np.array([[1, 0]], dtype=np.float32)

        def close(self):
            calls.append(("closed",))

    class Answer:
        def generate(self, *args, **kwargs):
            return json.dumps({"answer": "A fox appears.", "evidence_ids": ["v1:visual:one"]})

        def close(self):
            pass

    monkeypatch.setattr(backends, "embedder", lambda _: Embedder())

    def answer_factory(_):
        assert calls[-1] == ("closed",)
        calls.append(("answer",))
        return Answer()

    with Store(tmp_path) as store:
        indexed_evidence(store, cfg, tmp_path)
        result = answering.answer(store, cfg, "Where is the fox?", generator_factory=answer_factory)
    assert result["sources"] and result["sources"][0]["id"] == "v1:visual:one"
    assert sum(c[0] == "encode" for c in calls) == 1
    assert sum(c[0] == "closed" for c in calls) == 1


def test_word_only_search_never_invokes_shared_query_encoder(tmp_path):
    cfg = Config()
    with Store(tmp_path) as store:
        indexed_evidence(store, cfg, tmp_path)
        result = library.search(
            store,
            cfg,
            "fox",
            lexical_only=True,
            query_vector_factory=lambda: pytest.fail("word search must not load models"),
        )
        assert result["hits"]


def test_progress_ignores_unknown_future_stage_names():
    cfg = Config()
    video = {
        "id": 1,
        "metadata": {"duration_ms": 1000},
        "stages": [
            {"name": "future-layer", "signature": "new", "status": "running"},
            {
                "name": "visual",
                "signature": stage_signatures(cfg)["visual"],
                "status": "complete",
                "checkpoint": 1,
                "elapsed_s": 1,
            },
        ],
    }
    assert ProgressTracker().observe("job", [video], cfg, {}) == ({}, [])


def test_event_model_cannot_forge_application_provenance():
    data = {"groups": [{"children": ["a"], "description": "A fox appears.", "grouping": "invented"}]}
    assert "grouping" not in validated_groups(data, ["a"])[0]


@pytest.mark.parametrize("data", [np.array([float("nan"), 0], dtype="<f4").tobytes(), b"short"])
def test_corrupt_semantic_vectors_fail_with_reindex_guidance(tmp_path, data):
    cfg = Config()
    with Store(tmp_path) as store:
        indexed_evidence(store, cfg, tmp_path)
        with store.db:
            store.db.execute("UPDATE vectors SET data=?", (data,))
        with pytest.raises(ValueError, match="reindex embeddings"):
            library.search(store, cfg, "fox", query_vector_factory=lambda: np.array([1, 0], dtype=np.float32))


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_asr_word_timestamps_never_reach_saved_json(tmp_path, monkeypatch, invalid):
    cfg = Config()
    monkeypatch.setattr(media, "audio_chunk", lambda *args: None)

    class Speech:
        def transcribe(self, audio):
            return [
                {
                    "start_s": 0,
                    "end_s": 1,
                    "text": "hello",
                    "words": [{"start_s": 0, "end_s": invalid, "text": "hello"}],
                }
            ]

    with Store(tmp_path) as store:
        video = store.register(
            tmp_path / "source.mov", "fingerprint", {"duration_ms": 2000, "has_audio": True}
        )
        store.prepare(video, "speech", "test")
        indexer = Indexer(store, cfg)
        indexer._timing = (video, "speech", 0, time.perf_counter())
        with pytest.raises(ValueError, match="non-finite word timestamps"):
            indexer._speech(store.video(video), Speech(), 0)
        assert store.items(video, stage="speech") == []
        assert store.stage(video, "speech")["checkpoint"] == 0


@pytest.mark.parametrize("ids", [[[]], [{}], [None], [True]])
def test_malformed_answer_citations_are_retryable_validation_errors(ids):
    from video_agent.answering import validate_response

    with pytest.raises(ValueError, match="evidence"):
        validate_response({"answer": "A scene", "evidence_ids": ids}, {"known": {}})
