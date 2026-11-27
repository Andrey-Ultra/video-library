from __future__ import annotations

import contextlib
import json
import logging
import math
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import backends, media, prompts
from .backends.common import ModelSetupError
from .config import Config
from .proxy import ProxyCache
from .signatures import embedding_namespace, stage_signatures
from .store import STAGES, Store
from .sync import synchronize

logger = logging.getLogger(__name__)


def item(video_id, stage, kind, key, start_ms, end_ms, text, payload=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Empty {kind} observation")
    return {
        "id": f"v{video_id}:{stage}:{key}",
        "video_id": video_id,
        "stage": stage,
        "kind": kind,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text.strip(),
        "payload": payload or {},
    }


@contextlib.contextmanager
def writer_lock(store):
    import fcntl

    with (store.directory / "index.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another indexer is already writing to this library") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Indexer:
    def __init__(self, store: Store, config: Config, factories=None, cancel=None):
        self.store, self.config = store, config
        self.cancel = cancel or (lambda: None)
        self.factories = factories or {
            "visual": backends.generator,
            "hierarchy": backends.generator,
            "speech": backends.transcriber,
            "ocr": backends.ocr,
            "embedding": backends.embedder,
        }

    def run(self, force=False, max_videos=None):
        config, store = self.config, self.store
        config.configure_cache()
        started = time.perf_counter()
        with writer_lock(store), tempfile.TemporaryDirectory(prefix="video-library-proxies-") as temp:
            self._proxies = ProxyCache(Path(temp), config.proxy, self._check)
            sync = synchronize(store)
            selected = store.videos(limit=max_videos if max_videos is not None else 1_000_000)
            logger.info("Discovered files: %d; unique videos: %d", sync["files"], len(selected))
            jobs, errors = [], list(sync["errors"])
            signatures = stage_signatures(config)
            for video in selected:
                try:
                    video_id = video["id"]
                    if video["error"]:
                        continue
                    for stage in STAGES:
                        provenance = {
                            "settings": asdict(getattr(config, stage)),
                            "prompt_version": prompts.PROMPT_VERSION,
                            "implementation": "video-library/0.1.0",
                        }
                        if stage in {"visual", "ocr"}:
                            provenance["analysis_proxy"] = asdict(config.proxy)
                        if stage == "hierarchy":
                            provenance["generator"] = asdict(config.visual)
                        store.prepare(video_id, stage, signatures[stage], force, provenance)
                    jobs.append(video_id)
                except Exception as exc:
                    with store.db:
                        store.db.execute("UPDATE videos SET error=? WHERE id=?", (str(exc), video["id"]))
                    logger.exception("Could not prepare %s", video["path"])
                    errors.append({"path": video["path"], "stage": "metadata", "error": str(exc)})
            store.save_config(asdict(config))
            # Stage-major scheduling keeps only one large model in memory.
            for stage in ("speech", "ocr", "visual", "hierarchy", "embedding"):
                backend = None
                try:
                    for video_id in jobs:
                        self.cancel()
                        current = store.stage(video_id, stage)
                        if current["status"] == "complete":
                            continue
                        if stage in {"hierarchy", "embedding"}:
                            prerequisites = ("visual",)
                            if stage == "embedding":
                                prerequisites += ("speech", "ocr", "hierarchy")
                            if any(store.stage(video_id, s)["status"] != "complete" for s in prerequisites):
                                continue
                        active = self.enabled(stage)
                        if active and backend is None:
                            self.cancel()
                            backend = self.factories[stage](config)
                        checkpoint = current["checkpoint"]
                        previous_elapsed = current["elapsed_s"]
                        t0 = time.perf_counter()
                        self._timing = (video_id, stage, previous_elapsed, t0)
                        store.stage_status(video_id, stage, "running", error=None)
                        video = store.video(video_id)
                        logger.info(
                            "%s: %s%s",
                            Path(video["path"]).name,
                            stage,
                            f" (resuming from {checkpoint})" if checkpoint else "",
                        )
                        try:
                            if active:
                                getattr(self, f"_{stage}")(video, backend, checkpoint)
                            store.stage_status(
                                video_id,
                                stage,
                                "complete",
                                elapsed_s=previous_elapsed + time.perf_counter() - t0,
                            )
                        except KeyboardInterrupt:
                            store.stage_status(
                                video_id,
                                stage,
                                "pending",
                                elapsed_s=previous_elapsed + time.perf_counter() - t0,
                            )
                            raise
                        except Exception as exc:
                            store.stage_status(
                                video_id,
                                stage,
                                "failed",
                                error=str(exc),
                                elapsed_s=previous_elapsed + time.perf_counter() - t0,
                            )
                            if isinstance(exc, ModelSetupError):
                                raise
                            errors.append({"video_id": video_id, "stage": stage, "error": str(exc)})
                            logger.exception(
                                "%s: %s failed; restart to resume processing",
                                Path(video["path"]).name,
                                stage,
                            )
                finally:
                    if backend is not None:
                        backend.close()
            elapsed = time.perf_counter() - started
            for video_id in jobs:
                video = store.video(video_id)
                try:
                    source_unchanged = media.fingerprint(Path(video["path"])) == video["fingerprint"]
                except OSError:
                    source_unchanged = False
                if not source_unchanged:
                    message = "Source changed during indexing; run the indexer again"
                    with store.db:
                        store.db.execute("UPDATE videos SET error=? WHERE id=?", (message, video_id))
                    errors.append({"video_id": video_id, "stage": "source", "error": message})
            complete = [v for v in jobs if all(store.stage(v, s)["status"] == "complete" for s in STAGES)]
            complete = [v for v in complete if store.video(v)["error"] is None]
            duration_s = sum(store.video(v)["metadata"]["duration_ms"] / 1000 for v in jobs)
            return {
                "discovered": sync["files"],
                "sync": sync,
                "selected": len(jobs),
                "complete": len(complete),
                "elapsed_s": round(elapsed, 2),
                "video_duration_s": round(duration_s, 2),
                "processing_ratio": round(elapsed / duration_s, 3) if duration_s else None,
                "errors": errors,
            }

    def enabled(self, stage):
        if stage == "hierarchy":
            return self.config.hierarchy.enabled
        return getattr(self.config, stage).backend != "none"

    def _progress(self, checkpoint=None):
        video_id, stage, previous, started = self._timing
        elapsed = previous + time.perf_counter() - started
        self.store.db.execute(
            "UPDATE stages SET elapsed_s=? WHERE video_id=? AND name=?", (elapsed, video_id, stage)
        )
        if checkpoint is not None:
            self.store.db.execute(
                "UPDATE stages SET checkpoint=? WHERE video_id=? AND name=?", (checkpoint, video_id, stage)
            )

    def _check(self):
        self.cancel()

    def _cached(self, video_id, key, generate):
        row = self.store.db.execute(
            "SELECT payload FROM work_cache WHERE video_id=? AND stage='hierarchy' AND key=?", (video_id, key)
        ).fetchone()
        if row:
            return json.loads(row[0])
        value = generate()
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO work_cache VALUES(?,'hierarchy',?,?)",
                (video_id, key, json.dumps(value)),
            )
            count = self.store.db.execute(
                "SELECT count(*) FROM work_cache WHERE video_id=? AND stage='hierarchy'", (video_id,)
            ).fetchone()[0]
            self._progress(count)
        return value

    def _visual(self, video, backend, checkpoint):
        cfg = self.config.visual
        ranges = list(media.windows(video["metadata"]["duration_ms"], cfg.window_s, cfg.overlap_s))
        for index, window in enumerate(ranges):
            self._check()
            if index < checkpoint:
                continue
            with tempfile.TemporaryDirectory(prefix="video-library-frames-") as temp:
                samples, used_proxy = self._proxies.sample_window(
                    video, window, cfg.frames_per_window, cfg.max_side, Path(temp)
                )
                prompt = prompts.VISUAL.replace(
                    "{timestamps}", ", ".join(f"{t / 1000:.3f}" for t, _ in samples)
                )
                data = None
                for attempt in range(2):
                    raw = backend.generate(
                        prompt,
                        [p for _, p in samples],
                        max_tokens=cfg.max_tokens * (attempt + 1),
                        schema=prompts.VISUAL_SCHEMA,
                    )
                    try:
                        data = backends.json_object(raw)
                        if not isinstance(data.get("description"), str) or not data["description"].strip():
                            raise ValueError("Missing description")
                        for name in ("objects", "actions", "uncertainty"):
                            if not isinstance(data.get(name), list) or any(
                                not isinstance(v, str) for v in data[name]
                            ):
                                raise ValueError(f"Invalid {name}")
                        if not isinstance(data.get("setting"), str):
                            raise ValueError("Invalid setting")
                        break
                    except ValueError:
                        if attempt:
                            raise
                data["sampled_at_ms"] = [t for t, _ in samples]
                if used_proxy:
                    data["analysis_proxy"] = asdict(self.config.proxy)
                data["timing_precision"] = "sampled_window; actions are not precisely localized"
                text = "\n".join(
                    [
                        data["description"],
                        "Objects: " + ", ".join(data["objects"]),
                        "Actions: " + ", ".join(data["actions"]),
                        "Setting: " + data["setting"],
                        "Uncertainty: " + "; ".join(data["uncertainty"]),
                    ]
                )
                observation = item(
                    video["id"], "visual", "window", index, window.start_ms, window.end_ms, text, data
                )
                with self.store.db:
                    self.store.put_item(observation)
                    self._progress()
                    self.store.db.execute(
                        "UPDATE stages SET checkpoint=? WHERE video_id=? AND name='visual'",
                        (index + 1, video["id"]),
                    )
            logger.info(
                "  Visual window %d/%d [%0.1f–%0.1f s]",
                index + 1,
                len(ranges),
                window.start_ms / 1000,
                window.end_ms / 1000,
            )

    def _speech(self, video, backend, checkpoint):
        if not video["metadata"]["has_audio"]:
            return
        duration_s = video["metadata"]["duration_ms"] / 1000
        chunk_s = self.config.speech.chunk_s
        total = math.ceil(duration_s / chunk_s)
        for index in range(checkpoint, total):
            self._check()
            core_start, core_end = index * chunk_s, min((index + 1) * chunk_s, duration_s)
            # Context on both sides avoids cutting words. Assign sentences by midpoint
            # so context is not blindly stored twice. Approximate ASR boundaries remain evidence.
            start, end = max(0, core_start - 2), min(duration_s, core_end + 2)
            with tempfile.TemporaryDirectory(prefix="video-library-audio-") as temp:
                audio = Path(temp) / "speech.wav"
                media.audio_chunk(Path(video["path"]), audio, start, end - start)
                spans = backend.transcribe(audio)
            with self.store.db:
                self._progress()
                for pos, span in enumerate(spans):
                    a, b = start + float(span["start_s"]), start + float(span["end_s"])
                    if not all(math.isfinite(t) for t in (a, b)):
                        raise ValueError("ASR returned non-finite timestamps")
                    if not core_start <= (a + b) / 2 < core_end or not span["text"].strip():
                        continue
                    a_ms, b_ms = max(0, round(a * 1000)), min(round(b * 1000), round(duration_s * 1000))
                    if b_ms <= a_ms:
                        continue
                    words = [
                        {**w, "start_s": w["start_s"] + start, "end_s": w["end_s"] + start}
                        for w in span.get("words", [])
                    ]
                    self.store.put_item(
                        item(
                            video["id"],
                            "speech",
                            "speech",
                            f"{index}-{pos}",
                            a_ms,
                            b_ms,
                            span["text"],
                            {
                                "words": words,
                                "language": span.get("language"),
                                "chunk_language": span.get("chunk_language"),
                                "timing_precision": "estimated_asr",
                            },
                        )
                    )
                self.store.db.execute(
                    "UPDATE stages SET checkpoint=? WHERE video_id=? AND name='speech'",
                    (index + 1, video["id"]),
                )
            logger.info("  Speech chunk %d/%d", index + 1, total)

    def _ocr(self, video, backend, checkpoint):
        cfg = self.config.ocr
        duration = video["metadata"]["duration_ms"]
        for t_ms, image, used_proxy in self._proxies.sampled_frames(
            video, cfg.sample_fps, cfg.max_side, checkpoint
        ):
            self._check()
            if t_ms >= duration:
                break
            lines = backend.recognize(image)
            with self.store.db:
                self._progress()
                if lines:
                    # This is a sampled observation, not a claimed continuous subtitle interval.
                    self.store.put_item(
                        item(
                            video["id"],
                            "ocr",
                            "ocr",
                            t_ms,
                            t_ms,
                            t_ms + 1,
                            "\n".join(line["text"] for line in lines),
                            {
                                "lines": lines,
                                "sampled_at_ms": t_ms,
                                "timing_precision": (
                                    "proxy_frame_observation; source_time_approximate"
                                    if used_proxy and self.config.proxy.max_fps
                                    else "single_frame_observation"
                                ),
                                "analysis_proxy": asdict(self.config.proxy),
                            },
                        )
                    )
                self.store.db.execute(
                    "UPDATE stages SET checkpoint=? WHERE video_id=? AND name='ocr'", (t_ms + 1, video["id"])
                )
            if t_ms % 10000 < 1000 / cfg.sample_fps:
                logger.info("  OCR through %.1f / %.1f s", t_ms / 1000, duration / 1000)

    def _hierarchy(self, video, backend, checkpoint):
        # Rebuild derived nodes as one transaction; failed/partial trees are never published.
        leaves = self.store.items(video["id"], stage="visual", limit=1_000_000)
        if not leaves:
            return
        size = self.config.hierarchy.group_size
        nodes, edges, events = [], [], []
        for offset in range(0, len(leaves), size):
            self._check()
            batch = leaves[offset : offset + size]
            evidence = self._evidence(batch)
            groups = self._cached(
                video["id"], f"groups-{offset}", lambda: self._event_groups(backend, batch, evidence)
            )
            by_id = {n["id"]: n for n in batch}
            for group in groups:
                children = [by_id[c] for c in group["children"]]
                if not children:
                    raise ValueError("Empty event group")
                node = item(
                    video["id"],
                    "hierarchy",
                    "event",
                    f"event-{len(events)}",
                    children[0]["start_ms"],
                    children[-1]["end_ms"],
                    group.get("description"),
                    {"level": 1, "grouping": "model_proposed", "timing_precision": "window_bounds"},
                )
                nodes.append(node)
                events.append(node)
                edges.append((node["id"], [c["id"] for c in children]))
            logger.info("  Events: %d/%d source windows", min(offset + size, len(leaves)), len(leaves))
        current, level = events, 2
        while len(current) > size:
            parents = []
            for offset in range(0, len(current), size):
                self._check()
                batch = current[offset : offset + size]
                key = f"level-{level}-{offset}"
                node = self._cached(
                    video["id"],
                    key,
                    lambda: self._summary(video["id"], backend, batch, level, key, "episode"),
                )
                nodes.append(node)
                edges.append((node["id"], [c["id"] for c in batch]))
                parents.append(node)
            current, level = parents, level + 1
        root = self._cached(
            video["id"],
            "root",
            lambda: self._summary(video["id"], backend, current, level, "root", "summary"),
        )
        nodes.append(root)
        edges.append((root["id"], [c["id"] for c in current]))
        with self.store.db:
            self.store.db.execute("DELETE FROM items WHERE video_id=? AND stage='hierarchy'", (video["id"],))
            for node in nodes:
                self.store.put_item(node)
            for parent, children in edges:
                self.store.link(parent, children)

    def _event_groups(self, backend, batch, evidence):
        expected = [node["id"] for node in batch]
        prompt = prompts.GROUP + json.dumps(evidence)
        for attempt in range(2):
            try:
                data = backends.json_object(
                    backend.generate(
                        prompt,
                        max_tokens=max(768, len(batch) * 140) * (attempt + 1),
                        schema=prompts.group_schema(expected),
                    )
                )
                groups = data.get("groups")
                if not isinstance(groups, list) or not groups:
                    raise ValueError("Hierarchy model returned no event groups")
                flattened = [child for group in groups for child in group.get("children", [])]
                if flattened != expected:
                    raise ValueError("Hierarchy must preserve all observation IDs exactly once and in order")
                if any(
                    not group.get("children")
                    or not isinstance(group.get("description"), str)
                    or not group["description"].strip()
                    for group in groups
                ):
                    raise ValueError("Every event group needs children and a description")
                return groups
            except ValueError as exc:
                if attempt:
                    raise
                logger.warning("Retrying event grouping: %s", exc)
                prompt += (
                    "\nYour previous response was invalid. Include these IDs exactly once, in this order: "
                    + json.dumps(expected)
                )

    def _evidence(self, batch):
        return [
            {
                "id": n["id"],
                "start_s": n["start_ms"] / 1000,
                "end_s": n["end_ms"] / 1000,
                "visual_observation": n["text"],
            }
            for n in batch
        ]

    def _summary(self, video_id, backend, batch, level, key, kind):
        evidence = [
            {
                "id": n["id"],
                "description": n["text"],
                "start_s": n["start_ms"] / 1000,
                "end_s": n["end_ms"] / 1000,
            }
            for n in batch
        ]
        data = backends.json_object(
            backend.generate(
                prompts.SUMMARY + json.dumps(evidence), max_tokens=384, schema=prompts.SUMMARY_SCHEMA
            )
        )
        return item(
            video_id,
            "hierarchy",
            kind,
            key,
            batch[0]["start_ms"],
            batch[-1]["end_ms"],
            data.get("description"),
            {
                "level": level,
                "grouping": "bounded_chronological_batch",
                "timing_precision": "child_bounds",
                "scope": "visual",
            },
        )

    def _embedding(self, video, backend, checkpoint):
        namespace = embedding_namespace(self.config)
        # Every item is independently keyed, so resume does not depend on mutable ordering.
        rows = self.store.db.execute(
            "SELECT i.id,i.text FROM items i JOIN stages s ON "
            "s.video_id=i.video_id AND s.name=i.stage WHERE i.video_id=? "
            "AND s.status='complete' AND NOT EXISTS(SELECT 1 FROM vectors v "
            "WHERE v.item_id=i.id AND v.namespace=?) ORDER BY i.id",
            (video["id"], namespace),
        ).fetchall()
        size = self.config.embedding.batch_size
        for offset in range(0, len(rows), size):
            self._check()
            batch = rows[offset : offset + size]
            vectors = backend.encode([r["text"] for r in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding count does not match item count")
            vectors = backends.normalized(vectors)
            with self.store.db:
                self._progress(checkpoint + min(offset + size, len(rows)))
                self.store.db.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                    [
                        (row["id"], namespace, len(vector), vector.astype("<f4").tobytes())
                        for row, vector in zip(batch, vectors, strict=True)
                    ],
                )
            logger.info("  Embeddings %d/%d", min(offset + size, len(rows)), len(rows))
