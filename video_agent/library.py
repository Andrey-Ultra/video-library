from __future__ import annotations

import heapq
import json
import re
from collections import Counter

import numpy as np

from . import backends
from .config import Config, parse_config
from .signatures import embedding_namespace
from .store import Store


def indexed_config(store: Store) -> Config:
    raw = store.saved_config()
    return parse_config(raw) if raw else Config()


def card(item):
    result = {key: item[key] for key in ("id", "video_id", "kind", "start_s", "end_s", "text")}
    if "path" in item:
        result["path"] = item["path"]
    return result


def overview(store: Store, offset=0, limit=20, video_id=None):
    videos = []
    selected = [store.video(video_id)] if video_id is not None else store.videos(offset, limit)
    for video in selected:
        roots = store.items(video["id"], kind="summary", complete_only=True, limit=1)
        videos.append(
            {
                "video_id": video["id"],
                "path": video["path"],
                "metadata": video["metadata"],
                "stages": video["stages"],
                "error": video["error"],
                "summary": card(roots[0]) if roots and not video["error"] else None,
                "windows": store.count_items(video["id"], "window"),
                "events": store.count_items(video["id"], "event"),
            }
        )
    return {
        "total": 1 if video_id is not None else store.total_videos(),
        "folder": str(store.folder),
        "offset": offset,
        "videos": videos,
        "summary_scope": "Visual summaries. Speech and OCR are independent searchable evidence layers.",
        "coverage_note": "Sampled observations. Missing events are not proof of absence.",
    }


def query_embedding(config, query, embedder_factory=backends.embedder):
    """Encode one query and release model memory before answer generation."""
    config.configure_cache()
    backend = embedder_factory(config)
    try:
        return backends.normalized(backend.encode([query], query=True))[0]
    finally:
        backend.close()


def search(
    store: Store,
    config: Config,
    query: str,
    top_k=10,
    video_id=None,
    source="all",
    lexical_only=False,
    embedder_factory=backends.embedder,
    query_vector_factory=None,
):
    if not query.strip():
        raise ValueError("Query must not be empty")
    if source not in {"all", "visual", "speech", "ocr", "hierarchy"}:
        raise ValueError("source must be all, visual, speech, ocr, or hierarchy")
    if not 1 <= top_k <= 50:
        raise ValueError("top_k must be between 1 and 50")
    filters = ["v.present=1", "v.error IS NULL", "s.status='complete'"]
    args = []
    if video_id is not None:
        filters.append("i.video_id=?")
        args.append(video_id)
    if source != "all":
        filters.append("i.stage=?")
        args.append(source)
    where = " AND ".join(filters)
    joins = "FROM items i JOIN videos v ON v.id=i.video_id JOIN stages s ON s.video_id=i.video_id AND s.name=i.stage"
    tokens = re.findall(r"\w+", query, re.UNICODE)[:40]
    lexical = []
    if tokens:
        # Quote each token. User input is never interpreted as FTS operators or SQL.
        expression = " OR ".join('"' + token + '"' for token in tokens)
        rows = store.db.execute(
            "SELECT i.id " + joins + " JOIN items_fts f ON f.item_id=i.id "
            "WHERE items_fts MATCH ? AND " + where + " ORDER BY bm25(items_fts) LIMIT 100",
            [expression, *args],
        )
        lexical = [r[0] for r in rows]
    semantic, warning = [], None
    if not lexical_only and config.embedding.backend != "none":
        namespace = embedding_namespace(config)
        vector_rows = store.db.execute(
            "SELECT i.id,x.data,x.dims " + joins + " JOIN vectors x ON x.item_id=i.id "
            "JOIN stages es ON es.video_id=i.video_id AND es.name='embedding' "
            "WHERE " + where + " AND x.namespace=? AND es.status='complete'",
            [*args, namespace],
        )
        first = vector_rows.fetchmany(256)
        if first:
            vector = (
                query_vector_factory()
                if query_vector_factory is not None
                else query_embedding(config, query, embedder_factory)
            )
            best = []
            batch = first
            while batch:
                if any(row["dims"] != len(vector) or len(row["data"]) != len(vector) * 4 for row in batch):
                    raise ValueError(
                        "Stored embedding dimensions do not match query model; reindex embeddings"
                    )
                matrix = np.stack([np.frombuffer(row["data"], dtype="<f4") for row in batch])
                if not np.isfinite(matrix).all():
                    raise ValueError("Stored embeddings contain non-finite values; reindex embeddings")
                similarities = matrix @ vector
                for row, score in zip(batch, similarities, strict=True):
                    candidate = (float(score), row["id"])
                    if len(best) < 100:
                        heapq.heappush(best, candidate)
                    elif candidate > best[0]:
                        heapq.heapreplace(best, candidate)
                batch = vector_rows.fetchmany(256)
            semantic = [item_id for _, item_id in sorted(best, reverse=True)]
        else:
            warning = "No completed compatible semantic index; returning word matches only."
    # Reciprocal rank fusion: lexical and cosine scores are not on the same scale.
    scores, channels = {}, {}
    for name, ranked in (("words", lexical), ("meaning", semantic)):
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0) + 1 / (60 + rank)
            channels.setdefault(item_id, []).append(name)
    ranked = sorted(scores, key=scores.get, reverse=True)
    hits = []
    for item_id in ranked:
        candidate = store.item(item_id)
        # Overlap windows and repeated OCR can otherwise fill the entire result page.
        duplicate = False
        for previous in hits:
            if previous["video_id"] != candidate["video_id"] or previous["kind"] != candidate["kind"]:
                continue
            intersection = min(previous["end_s"], candidate["end_s"]) - max(
                previous["start_s"], candidate["start_s"]
            )
            shorter = min(previous["end_s"] - previous["start_s"], candidate["end_s"] - candidate["start_s"])
            if intersection > 0 and intersection / shorter > 0.8:
                duplicate = True
                break
            if (
                candidate["kind"] == "ocr"
                and candidate["text"] == previous["text"]
                and abs(candidate["start_s"] - previous["start_s"]) < 5
            ):
                duplicate = True
                break
        if duplicate:
            continue
        hits.append(
            {**card(candidate), "rank_score": round(scores[item_id], 6), "matched_by": channels[item_id]}
        )
        if len(hits) == top_k:
            break
    return {
        "query": query,
        "hits": hits,
        "warning": warning,
        "score_note": "Relative rank score, not confidence or probability.",
    }


def object_inventory(store: Store, video_id=None, limit=30):
    """Candidate discovery by observed window frequency, not claimed event counts."""
    query = (
        "SELECT i.id,i.payload FROM items i JOIN videos v ON v.id=i.video_id "
        "JOIN stages s ON s.video_id=i.video_id AND s.name='visual' "
        "WHERE i.stage='visual' AND s.status='complete' AND v.present=1 AND v.error IS NULL"
    )
    args = []
    if video_id is not None:
        query += " AND i.video_id=?"
        args.append(video_id)
    counts, examples = Counter(), {}
    for row in store.db.execute(query, args):
        objects = json.loads(row["payload"]).get("objects", [])
        for label in {obj.strip().lower() for obj in objects if obj.strip()}:
            counts[label] += 1
            examples.setdefault(label, [])
            if len(examples[label]) < 3:
                examples[label].append(row["id"])
    labels = sorted(counts, key=lambda label: (counts[label], label))[:limit]
    return {
        "objects": [
            {"label": label, "observed_windows": counts[label], "example_ids": examples[label]}
            for label in labels
        ],
        "note": "Labels are model-generated and not canonicalized. Counts are sampled windows, not visits. "
        "Rarity is a browsing aid, not proof of an unusual event.",
    }
