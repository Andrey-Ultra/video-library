"""Read-only integrity check for a completed real video library. No model loading."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from video_agent.library import indexed_config
from video_agent.media import fingerprint, windows
from video_agent.signatures import embedding_namespace
from video_agent.store import STAGES, Store


def check(folder):
    errors, videos = [], []
    with Store(folder, create=False) as store:
        config = indexed_config(store)
        namespace = embedding_namespace(config)
        for video in store.videos(limit=1_000_000):
            video_id = video["id"]
            duration = video["metadata"]["duration_ms"]
            states = {s["name"]: s["status"] for s in video["stages"]}
            if any(states.get(stage) != "complete" for stage in STAGES):
                errors.append(f"Video {video_id}: unfinished stages: {states}")
            if fingerprint(Path(video["path"])) != video["fingerprint"]:
                errors.append(f"Video {video_id}: source changed")
            items = store.items(video_id, limit=1_000_000)
            by_id = {node["id"]: node for node in items}
            visual = [node for node in items if node["kind"] == "window"]
            if config.visual.backend != "none":
                expected = [
                    (w.start_ms, w.end_ms)
                    for w in windows(duration, config.visual.window_s, config.visual.overlap_s)
                ]
                if [(n["start_ms"], n["end_ms"]) for n in visual] != expected:
                    errors.append(f"Video {video_id}: visual coverage mismatch")
            for node in items:
                if not 0 <= node["start_ms"] < node["end_ms"] <= duration:
                    errors.append(f"Item {node['id']}: timestamp outside source")
            roots = [n for n in items if n["kind"] == "summary"]
            if config.hierarchy.enabled:
                if len(roots) != 1:
                    errors.append(f"Video {video_id}: expected one summary root")
                else:
                    visited, pending = set(), [roots[0]["id"]]
                    while pending:
                        parent = pending.pop()
                        if parent in visited:
                            errors.append(f"Video {video_id}: duplicate/cyclic tree reference {parent}")
                            continue
                        visited.add(parent)
                        for row in store.db.execute(
                            "SELECT child_id FROM edges WHERE parent_id=?", (parent,)
                        ):
                            child = row[0]
                            if child not in by_id:
                                errors.append(f"Item {parent}: invalid child {child}")
                                continue
                            a, b = by_id[parent], by_id[child]
                            if not a["start_ms"] <= b["start_ms"] < b["end_ms"] <= a["end_ms"]:
                                errors.append(f"Item {child}: not contained in parent {parent}")
                            pending.append(child)
                    expected_ids = {n["id"] for n in items if n["stage"] in {"visual", "hierarchy"}}
                    if visited != expected_ids:
                        errors.append(f"Video {video_id}: hierarchy leaves some evidence unreachable")
            vectors = store.db.execute(
                "SELECT x.* FROM vectors x JOIN items i ON i.id=x.item_id WHERE i.video_id=?", (video_id,)
            ).fetchall()
            if config.embedding.backend != "none":
                if len(vectors) != len(items):
                    errors.append(f"Video {video_id}: missing embeddings")
                for row in vectors:
                    values = np.frombuffer(row["data"], dtype="<f4")
                    if (
                        row["namespace"] != namespace
                        or len(values) != row["dims"]
                        or not np.isfinite(values).all()
                        or not np.isclose(np.linalg.norm(values), 1, atol=1e-4)
                    ):
                        errors.append(f"Item {row['item_id']}: incompatible or invalid embedding")
            videos.append(
                {
                    "video_id": video_id,
                    "file": Path(video["path"]).name,
                    "duration_s": duration / 1000,
                    "windows": len(visual),
                    "events": sum(n["kind"] == "event" for n in items),
                    "episodes": sum(n["kind"] == "episode" for n in items),
                    "speech_spans": sum(n["kind"] == "speech" for n in items),
                    "ocr_observations": sum(n["kind"] == "ocr" for n in items),
                    "embeddings": len(vectors),
                    "stages": video["stages"],
                }
            )
    return {"ok": not errors, "errors": errors, "videos": videos}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", default="videos")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = check(args.folder)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    raise SystemExit(0 if report["ok"] else 1)
