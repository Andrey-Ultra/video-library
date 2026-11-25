from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from video_agent import answering, library, media
from video_agent.backends import json_object
from video_agent.config import Config, load_config
from video_agent.pipeline import Indexer, item
from video_agent.signatures import embedding_namespace
from video_agent.store import STAGES, Store


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.runtime.cache_dir = str(tmp_path / "models")
    return cfg


@pytest.mark.parametrize(
    "duration,expected",
    [
        (1, [(0, 1)]),
        (10000, [(0, 10000)]),
        (10001, [(0, 10000), (8000, 10001)]),
        (26000, [(0, 10000), (8000, 18000), (16000, 26000)]),
    ],
)
def test_windows_cover_tail_without_redundant_last_window(duration, expected):
    assert [(w.start_ms, w.end_ms) for w in media.windows(duration, 10, 2)] == expected


def test_discovery_ignores_appledouble_hidden_and_symlinks(tmp_path):
    (tmp_path / "a.MP4").touch()
    (tmp_path / "._a.MP4").touch()
    hidden = tmp_path / ".video-agent"
    hidden.mkdir()
    (hidden / "cached.mp4").touch()
    (tmp_path / "link.mp4").symlink_to(tmp_path / "a.MP4")
    assert list(media.discover(tmp_path)) == [(tmp_path / "a.MP4").resolve()]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: setattr(c.visual, "overlap_s", 10),
        lambda c: setattr(c.ocr, "sample_fps", 0),
        lambda c: setattr(c.speech, "chunk_s", float("nan")),
        lambda c: setattr(c.hierarchy, "group_size", 1),
    ],
)
def test_bad_sampling_settings_rejected(config, mutate):
    mutate(config)
    with pytest.raises(ValueError):
        config.validate()


def test_unknown_config_is_not_silently_ignored(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[visual]\nframe_per_window = 10\n")
    with pytest.raises(ValueError, match="Unknown settings"):
        load_config(path)


def test_truncated_model_json_is_not_published():
    with pytest.raises(ValueError):
        json_object('{"description":"fox"')
    assert json_object('```json\n{"description":"fox"}\n```')["description"] == "fox"


def register(store, tmp_path):
    return store.register(tmp_path / "source.mp4", "fingerprint", {"duration_ms": 26000, "has_audio": True})


def test_visual_invalidation_preserves_speech_but_removes_dependents(tmp_path):
    with Store(tmp_path) as store:
        video = register(store, tmp_path)
        for name in STAGES:
            store.prepare(video, name, "old")
        with store.db:
            for name in ("visual", "speech", "hierarchy"):
                store.put_item(item(video, name, "window", name, 0, 1000, "fox"))
        store.prepare(video, "visual", "new")
        assert store.items(video, stage="visual") == []
        assert store.items(video, stage="hierarchy") == []
        assert len(store.items(video, stage="speech")) == 1
        assert store.stage(video, "embedding") is None


def test_changed_source_removes_all_old_evidence_and_fts(tmp_path):
    with Store(tmp_path) as store:
        video = register(store, tmp_path)
        with store.db:
            store.put_item(item(video, "visual", "window", 0, 0, 1000, "eagle"))
        assert store.db.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 1
        assert store.register(tmp_path / "source.mp4", "changed", {}) == video
        assert store.items(video) == []
        assert store.db.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 0


def test_reindex_updates_fts_without_duplicates(tmp_path):
    with Store(tmp_path) as store:
        video = register(store, tmp_path)
        with store.db:
            store.put_item(item(video, "visual", "window", 0, 0, 1000, "fox"))
            store.put_item(item(video, "visual", "window", 0, 0, 1000, "bird"))
        assert store.db.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 1
        assert (
            store.db.execute("SELECT count(*) FROM items_fts WHERE items_fts MATCH 'fox'").fetchone()[0] == 0
        )


class FakeGenerator:
    calls = 0
    fail_at = None

    def generate(self, prompt, images=None, max_tokens=384, schema=None):
        if images:
            type(self).calls += 1
            if self.fail_at == self.calls:
                raise RuntimeError("interrupted backend")
            return json.dumps(
                {
                    "description": "A fox eats.",
                    "objects": ["fox"],
                    "actions": ["eating"],
                    "setting": "forest",
                    "uncertainty": [],
                }
            )
        if "OBSERVATIONS:\n" in prompt:
            observations = json.loads(prompt.split("OBSERVATIONS:\n")[1])
            assert "Welcome to the forest" not in prompt  # speech is not visual evidence
            return json.dumps(
                {
                    "groups": [
                        {
                            "children": [o["id"] for o in observations],
                            "description": "A fox visits the forest and eats.",
                        }
                    ]
                }
            )
        return '{"description":"A fox visits a forest."}'

    def close(self):
        pass


class FakeSpeech:
    def transcribe(self, audio):
        return [{"start_s": 1, "end_s": 2, "text": "Welcome to the forest", "words": []}]

    def close(self):
        pass


class FakeOCR:
    def recognize(self, image):
        return [{"text": "FOX CAMERA", "confidence": 0.99, "box": []}]

    def close(self):
        pass


class FakeEmbedder:
    def encode(self, texts, query=False):
        return np.array([[1, 0] if "fox" in text.lower() else [0, 1] for text in texts], dtype=np.float32)

    def close(self):
        pass


@pytest.fixture
def fake_media(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test source")
    monkeypatch.setattr(media, "metadata", lambda _: {"duration_ms": 26000, "has_audio": True})
    monkeypatch.setattr(media, "sample_window", lambda *a: [(a[1].start_ms + 1, Path("unused.jpg"))])
    monkeypatch.setattr(media, "sampled_frames", lambda *a: iter([(0, None), (1000, None)]))
    monkeypatch.setattr(media, "audio_chunk", lambda *a: None)
    FakeGenerator.calls, FakeGenerator.fail_at = 0, None
    return {
        "visual": lambda _: FakeGenerator(),
        "hierarchy": lambda _: FakeGenerator(),
        "speech": lambda _: FakeSpeech(),
        "ocr": lambda _: FakeOCR(),
        "embedding": lambda _: FakeEmbedder(),
    }


def test_full_pipeline_tree_search_and_idempotence(tmp_path, config, fake_media):
    with Store(tmp_path) as store:
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 1 and not result["errors"]
        assert FakeGenerator.calls == 3
        root = store.items(1, kind="summary")[0]
        event = store.item(store.item(root["id"])["children"][0])
        assert len(event["children"]) == 3
        assert store.count_items(1, "speech") == 1
        assert store.items(1, stage="ocr")[0]["end_ms"] == 1  # no invented continuous presence
        hits = library.search(store, config, "fox", embedder_factory=lambda _: FakeEmbedder())["hits"]
        assert hits and hits[0]["matched_by"] == ["words", "meaning"]
        before = store.db.execute("SELECT count(*) FROM vectors").fetchone()[0]
        assert Indexer(store, config, fake_media).run()["complete"] == 1
        assert FakeGenerator.calls == 3
        assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == before


def test_interrupted_visual_stage_resumes_and_is_hidden_from_search(tmp_path, config, fake_media):
    FakeGenerator.fail_at = 2
    with Store(tmp_path) as store:
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 0
        assert store.stage(1, "visual")["checkpoint"] == 1
        assert not library.search(store, config, "fox", source="visual", lexical_only=True)["hits"]
        FakeGenerator.fail_at = None
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 1
        assert FakeGenerator.calls == 4  # 1 successful + 1 failure + 2 resumed


def test_semantic_query_does_not_mix_embedding_models(tmp_path, config, fake_media):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        config.embedding.model = "different-model"
        hits = library.search(
            store, config, "fox", embedder_factory=lambda _: pytest.fail("wrong model loaded")
        )
        assert hits["warning"] and all(h["matched_by"] == ["words"] for h in hits["hits"])


def test_removed_source_is_excluded_from_search(tmp_path, config, fake_media):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        store.reconcile([])
        assert not library.search(store, config, "fox", lexical_only=True)["hits"]


def test_speech_and_ocr_overlap_use_half_open_intervals(tmp_path):
    with Store(tmp_path) as store:
        video = register(store, tmp_path)
        store.prepare(video, "speech", "test")
        with store.db:
            store.put_item(item(video, "speech", "speech", 0, 1000, 2000, "hello"))
        store.stage_status(video, "speech", "complete")
        assert not store.overlaps(video, 0, 1000)
        assert not store.overlaps(video, 2000, 3000)
        assert store.overlaps(video, 1000, 2000)


def test_fts_special_characters_are_safe(tmp_path, config, fake_media):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        for query in ('"', "fox OR NEAR(*", "foo' DROP TABLE items;--", "🦊"):
            library.search(store, config, query, lexical_only=True)
        assert store.count_items(1) > 0


def test_saved_config_and_read_only_mcp_tools(tmp_path, config, fake_media):
    from video_agent.server import build_server

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        assert asdict(library.indexed_config(store)) == asdict(config)
    with Store(tmp_path, create=False) as store:
        assert library.overview(store)["total"] == 1
    server = build_server(tmp_path)
    assert server.name == "video-library"


def test_embedding_setting_change_only_recomputes_embeddings(tmp_path, config, fake_media):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        config.embedding.model = "upgraded-model"
        Indexer(store, config, fake_media).run()
        assert FakeGenerator.calls == 3
        namespaces = {r[0] for r in store.db.execute("SELECT DISTINCT namespace FROM vectors")}
        assert namespaces == {embedding_namespace(config)}


def test_unreadable_changed_file_cannot_leave_searchable_old_evidence(
    tmp_path, config, fake_media, monkeypatch
):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()

        def broken(_):
            raise ValueError("corrupt video")

        monkeypatch.setattr(media, "metadata", broken)
        (tmp_path / "source.mp4").write_bytes(b"changed corrupt source")
        result = Indexer(store, config, fake_media).run()
        assert result["errors"]
        assert store.video(1)["error"] == "corrupt video"
        assert not library.search(store, config, "fox", lexical_only=True)["hits"]


def test_invalid_hierarchy_cannot_publish_an_incomplete_tree(tmp_path, config, fake_media):
    class BrokenGroups(FakeGenerator):
        def generate(self, *a, **kw):
            return '{"groups":[{"children":["invented-id"],"description":"invented"}]}'

    fake_media["hierarchy"] = lambda _: BrokenGroups()
    with Store(tmp_path) as store:
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 0
        assert store.stage(1, "hierarchy")["status"] == "failed"
        assert not store.items(1, stage="hierarchy")
        assert store.items(1, stage="visual", complete_only=True)


def test_answer_sources_have_deterministic_timestamps(tmp_path, config, fake_media):
    class AnswerModel:
        def generate(self, *a, **kw):
            return '{"answer":"A fox is visible.","evidence_ids":["v1:visual:0"]}'

        def close(self):
            pass

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        result = answering.answer(
            store, config, "fox", lexical_only=True, generator_factory=lambda _: AnswerModel()
        )
        assert result["sources"] == [
            {"id": "v1:visual:0", "file": "source.mp4", "start": "00:00:00.000", "end": "00:00:10.000"}
        ]


def test_filename_question_cannot_receive_other_videos(tmp_path, config, fake_media):
    (tmp_path / "other.mp4").write_bytes(b"another source")

    class ScopedAnswer:
        def generate(self, prompt, **kwargs):
            assert "other.mp4" not in prompt
            ids = kwargs["schema"]["properties"]["evidence_ids"]["items"]["enum"]
            return json.dumps({"answer": "A fox.", "evidence_ids": ids[:1]})

        def close(self):
            pass

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        result = answering.answer(
            store,
            config,
            "What is in source.mp4?",
            lexical_only=True,
            generator_factory=lambda _: ScopedAnswer(),
        )
        assert all(s["file"] == "source.mp4" for s in result["sources"])


def test_macos_decomposed_filename_matches_normal_question(tmp_path, config, fake_media):
    import unicodedata

    filename = "красивый ролик.mov"
    (tmp_path / unicodedata.normalize("NFD", filename)).write_bytes(b"another video")

    class ScopedAnswer:
        def generate(self, prompt, **kwargs):
            assert "source.mp4" not in prompt
            ids = kwargs["schema"]["properties"]["evidence_ids"]["items"]["enum"]
            return json.dumps({"answer": "A fox.", "evidence_ids": ids[:1]})

        def close(self):
            pass

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        result = answering.answer(
            store,
            config,
            f"Что видно в {filename}?",
            lexical_only=True,
            generator_factory=lambda _: ScopedAnswer(),
        )
        assert all(unicodedata.normalize("NFC", s["file"]) == filename for s in result["sources"])


def test_hierarchy_retries_invalid_partition_without_losing_windows(tmp_path, config, fake_media):
    class RepairableGroups(FakeGenerator):
        def __init__(self):
            self.bad = True

        def generate(self, prompt, **kwargs):
            if "OBSERVATIONS:\n" in prompt and self.bad:
                self.bad = False
                return '{"groups":[{"children":[],"description":"wrong"}]}'
            if "OBSERVATIONS:\n" in prompt:
                ids = kwargs["schema"]["properties"]["groups"]["items"]["properties"]["children"]["items"][
                    "enum"
                ]
                return json.dumps({"groups": [{"children": ids, "description": "A fox eats."}]})
            return super().generate(prompt, **kwargs)

    fake_media["hierarchy"] = lambda _: RepairableGroups()
    with Store(tmp_path) as store:
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 1
        event = store.items(1, kind="event")[0]
        assert len(store.item(event["id"])["children"]) == 3


@pytest.mark.parametrize("stage", ["speech", "ocr"])
def test_nonvisual_settings_do_not_rebuild_visual_tree(tmp_path, config, fake_media, stage):
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        if stage == "speech":
            config.speech.model = "replacement-asr"
        else:
            config.ocr.min_confidence = 0.8
        fake_media["hierarchy"] = lambda _: pytest.fail("Visual hierarchy should be reused")
        result = Indexer(store, config, fake_media).run()
        assert result["complete"] == 1


def test_answer_keeps_speech_and_visuals_when_ocr_dominates(tmp_path, config, fake_media, monkeypatch):
    original_search = answering.search

    class BalancedAnswer:
        def generate(self, prompt, **kwargs):
            evidence = json.loads(prompt.split("EVIDENCE:\n")[1].split("\nQUESTION:\n")[0])
            kinds = {hit["kind"] for hit in evidence["retrieved"]["hits"]}
            assert {"ocr", "speech", "window"} <= kinds
            return '{"answer":"Forest.","evidence_ids":["v1:speech:0-0"]}'

        def close(self):
            pass

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        dominant = [
            {**library.card(n), "path": str(tmp_path / "source.mp4")} for n in store.items(1, stage="ocr")
        ]

        def search(*args, **kwargs):
            if kwargs.get("source", "all") == "all":
                return {"hits": dominant, "warning": None}
            return original_search(*args, **kwargs)

        monkeypatch.setattr(answering, "search", search)
        answering.answer(
            store, config, "forest", lexical_only=True, generator_factory=lambda _: BalancedAnswer()
        )


def test_russian_answer_schema_prevents_chinese_term_drift(tmp_path, config, fake_media):
    import re

    class RussianAnswer:
        def generate(self, prompt, **kwargs):
            pattern = kwargs["schema"]["properties"]["answer"]["pattern"]
            assert re.fullmatch(pattern, "Используется мойка высокого давления.")
            assert not re.fullmatch(pattern, "С использованием 高压水枪.")
            return '{"answer":"Лиса ест.","evidence_ids":["v1:visual:0"]}'

        def close(self):
            pass

    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        result = answering.answer(
            store, config, "Что здесь видно?", lexical_only=True, generator_factory=lambda _: RussianAnswer()
        )
        assert result["sources"][0]["id"] == "v1:visual:0"


def test_answer_repairs_unjustified_global_absence(tmp_path, config, fake_media):
    class RepairAnswer:
        calls = 0

        def generate(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return '{"answer":"Видео не содержит животных.","evidence_ids":["v1:visual:0"]}'
            assert "Validation error" in prompt
            return '{"answer":"В найденном фрагменте лиса ест.","evidence_ids":["v1:visual:0"]}'

        def close(self):
            pass

    model = RepairAnswer()
    with Store(tmp_path) as store:
        Indexer(store, config, fake_media).run()
        result = answering.answer(
            store, config, "Лиса?", lexical_only=True, generator_factory=lambda _: model
        )
        assert model.calls == 2
        assert result["answer"] == "В найденном фрагменте лиса ест."


def test_hierarchy_resume_reuses_validated_group_cache(tmp_path, config, fake_media):
    class InterruptedTree(FakeGenerator):
        group_calls = 0
        fail_summary = True

        def generate(self, prompt, **kwargs):
            if "OBSERVATIONS:\n" in prompt:
                self.group_calls += 1
            elif self.fail_summary:
                raise RuntimeError("crashed before root summary")
            return super().generate(prompt, **kwargs)

    model = InterruptedTree()
    fake_media["hierarchy"] = lambda _: model
    with Store(tmp_path) as store:
        assert Indexer(store, config, fake_media).run()["complete"] == 0
        assert model.group_calls == 1
        assert store.stage(1, "hierarchy")["checkpoint"] == 1
        assert not store.items(1, stage="hierarchy")
        model.fail_summary = False
        assert Indexer(store, config, fake_media).run()["complete"] == 1
        assert model.group_calls == 1
        assert store.items(1, kind="summary")
