"""Read-only desktop snapshots, independent of HTTP routing.

All library-wide counts are grouped once, rather than queried for every clip on
an interface poll. Keep the wire shape stable for older desktop clients.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from .config import load_config, parse_config
from .index_storage import index_path
from .progress import estimate
from .signatures import stage_signatures
from .speech_options import speech_options
from .store import DEPENDENTS, STAGES, Store


def snapshot(state, tracker=None, library_id=None, include_queue=True, task=None):
    if include_queue:
        state.recover()
    libraries = state.libraries()
    active_id = library_id or state.active_id()
    result = {
        "speech_options": speech_options(),
        "libraries": libraries,
        "active_id": active_id,
        "videos": [],
        "job": None,
        "file_count": 0,
        "duplicate_count": 0,
        "item_count": 0,
        "progress": {"fraction": 0, "eta_s": None},
        "mcp_config": state.mcp_config(),
        "mcp_command": state.mcp_command(),
        "defaults": {profile: asdict(load_config(profile=profile)) for profile in ("mac", "quality", "cuda")},
    }
    if include_queue:
        jobs = state.queue_jobs()
        cards = [{k: v for k, v in j.items() if k != "config"} for j in jobs]
        running = next((j for j in jobs if j["status"] in {"starting", "running", "pausing"}), None)
        queue = {
            "paused": state.queue_paused(),
            "jobs": cards,
            "running": None,
            "waiting_count": sum(j["status"] == "queued" for j in jobs),
        }
        if running:
            view = snapshot(state, tracker, running["library_id"], False, running)
            current_video = next(
                (v for v in view["videos"] if any(t["status"] == "running" for t in v["stages"])), None
            )
            current_stage = (
                next((t["name"] for t in current_video["stages"] if t["status"] == "running"), None)
                if current_video
                else None
            )
            queue["running"] = {
                **{k: v for k, v in running.items() if k != "config"},
                "progress": view["progress"],
                "video_count": len(view["videos"]),
                "indexed_count": sum(not v["needs_index"] for v in view["videos"]),
                "current_file": current_video["path"] if current_video else None,
                "current_stage": current_stage,
            }
        result["queue"] = queue
    if not active_id:
        return result
    selected = state.library(active_id)
    result["active"] = selected
    job = task or state.latest_job(active_id)
    if job:
        result["job"] = {k: v for k, v in job.items() if k != "config"}
        log = state.directory / f"{job['id']}.log"
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 6000))
                result["job"]["log"] = stream.read().decode("utf-8", errors="replace")
    with state.connect() as db:
        busy = db.execute(
            "SELECT id,library_id FROM jobs WHERE status IN ('starting','running','pausing') "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        result["busy_job"] = dict(busy) if busy else None
    folder = Path(selected["path"])
    if not folder.is_dir():
        result["folder_unavailable"] = True
        return result
    if not index_path(folder).is_file():
        return result
    try:
        with Store(folder, create=False) as store:
            videos = store.videos(limit=1_000_000)
            # Old indexes do not have location records until their first refresh.
            has_locations = store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='file_locations'"
            ).fetchone()
            files = (
                [dict(r) for r in store.db.execute("SELECT path,video_id FROM file_locations")]
                if has_locations
                else []
            )
            locations = defaultdict(list)
            for file in files:
                locations[file["video_id"]].append(file["path"])
            item_counts = dict(store.db.execute("SELECT video_id,count(*) FROM items GROUP BY video_id"))
            vector_counts = dict(
                store.db.execute(
                    "SELECT i.video_id,count(*) FROM vectors v JOIN items i ON i.id=v.item_id GROUP BY i.video_id"
                )
            )
            summaries = {}
            for row in store.db.execute(
                "SELECT i.video_id,i.text FROM items i WHERE i.kind='summary' AND EXISTS("
                "SELECT 1 FROM stages s WHERE s.video_id=i.video_id AND s.name=i.stage AND s.status='complete') "
                "ORDER BY i.start_ms,i.end_ms,i.id"
            ):
                summaries.setdefault(row["video_id"], row["text"])
            counts = {}
            config = parse_config(task["config"] if task else selected["config"])
            signatures = stage_signatures(config)
            for video in videos:
                count = item_counts.get(video["id"], 0)
                vectors = vector_counts.get(video["id"], 0)
                counts[video["id"]] = (count, vectors)
                video["item_count"], video["vector_count"] = count, vectors
                video["locations"] = locations.get(video["id"], [video["path"]])
                by_name = {s["name"]: s for s in video["stages"]}
                video["needs_index"] = bool(video["error"]) or any(
                    name not in by_name
                    or by_name[name]["signature"] != signatures[name]
                    or by_name[name]["status"] != "complete"
                    for name in STAGES
                )
                video["outdated"] = [
                    name
                    for name in STAGES
                    if name in by_name and by_name[name]["signature"] != signatures[name]
                ]
                video["outdated"] = sorted(
                    set(video["outdated"])
                    | {dependent for name in video["outdated"] for dependent in DEPENDENTS[name]}
                )
                video["summary"] = summaries.get(video["id"])
            recent, unstable = (
                tracker.observe((active_id, job["id"] if job else None), videos, config, counts)
                if tracker and task
                else ({}, [])
            )
            result.update(
                videos=videos,
                file_count=len(files) if files else len(videos),
                duplicate_count=max(0, len(files) - len(videos)),
                item_count=sum(v["item_count"] for v in videos),
                progress=estimate(
                    videos,
                    config,
                    counts,
                    historical_rates=state.timing_rates(config),
                    recent_rates=recent,
                    unstable_stages=unstable,
                ),
            )
    except (FileNotFoundError, NotADirectoryError):
        result["folder_unavailable"] = True
    return result
