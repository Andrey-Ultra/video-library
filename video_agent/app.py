"""Private API for the native desktop app; models run in a detached worker process."""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import media
from .app_state import PROJECT, AppState, parse_config
from .config import load_config
from .progress import estimate
from .signatures import stage_signatures
from .store import DB_NAME, DEPENDENTS, STAGES, Store


def snapshot(state):
    state.recover()
    libraries = state.libraries()
    active_id = state.active_id()
    result = {
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
    if not active_id:
        return result
    selected = state.library(active_id)
    result["active"] = selected
    job = state.latest_job(active_id)
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
    if not (folder / ".video-agent" / DB_NAME).is_file():
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
            counts = {}
            signatures = stage_signatures(parse_config(selected["config"]))
            for video in videos:
                count = store.count_items(video["id"])
                vectors = store.db.execute(
                    "SELECT count(*) FROM vectors v JOIN items i ON i.id=v.item_id WHERE i.video_id=?",
                    (video["id"],),
                ).fetchone()[0]
                counts[video["id"]] = (count, vectors)
                video["item_count"], video["vector_count"] = count, vectors
                video["locations"] = [f["path"] for f in files if f["video_id"] == video["id"]] or [
                    video["path"]
                ]
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
                roots = store.items(video["id"], kind="summary", complete_only=True, limit=1)
                video["summary"] = roots[0]["text"] if roots else None
            result.update(
                videos=videos,
                file_count=len(files) if files else len(videos),
                duplicate_count=max(0, len(files) - len(videos)),
                item_count=sum(v["item_count"] for v in videos),
                progress=estimate(
                    videos,
                    parse_config(selected["config"]),
                    counts,
                    historical_rates=state.timing_rates(parse_config(selected["config"])),
                ),
            )
    except (FileNotFoundError, NotADirectoryError):
        result["folder_unavailable"] = True
    return result


def create_app(directory=None):
    state = AppState(directory)

    @asynccontextmanager
    async def lifespan(_):
        state.recover()
        # Remember the existing project library on the first launch.
        if not state.libraries() and (PROJECT / "videos").is_dir():
            state.select(PROJECT / "videos")
        yield

    app = FastAPI(
        title="Video Library local service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            # A custom header requires a cross-origin preflight; this server
            # deliberately does not enable CORS. Host checks prevent DNS rebinding.
            if request.headers.get("x-video-agent") != "local":
                return Response("Local application request required", status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"http://{request.headers.get('host')}":
                return Response("Origin not allowed", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return Response(json.dumps({"detail": str(exc)}), status_code=400, media_type="application/json")

    @app.get("/")
    def home():
        return {"app": "Video Library", "interface": "native", "status": "ready"}

    @app.get("/api/state")
    def status():
        return snapshot(state)

    @app.post("/api/libraries")
    def select_folder(body: dict):
        return state.select(body.get("path", ""))

    @app.put("/api/libraries/{library_id}/config")
    def settings(library_id: str, body: dict):
        return state.save_config(library_id, body)

    @app.post("/api/libraries/{library_id}/jobs")
    def start_job(library_id: str, body: dict):
        return state.launch(library_id, body.get("kind", "index"))

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str):
        return state.pause(job_id)

    @app.get("/api/videos/{video_id}/thumbnail")
    def thumbnail(video_id: int, library_id: str | None = None):
        selected = state.library(library_id)
        with Store(selected["path"], create=False) as store:
            video = store.video(video_id)
            cache = state.directory / "thumbnails" / selected["id"]
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / f"{video_id}-{video['fingerprint']}.jpg"
            if not target.is_file():
                path = Path(video["path"])
                if not path.is_file() or media.fingerprint(path) != video["fingerprint"]:
                    raise HTTPException(404, "Source changed or unavailable. Refresh this folder.")
                frame = media.frame_at(path, min(1000, video["metadata"]["duration_ms"] // 2), 384)
                frame.save(target, format="JPEG", quality=80)
        return FileResponse(target, media_type="image/jpeg")

    @app.get("/api/libraries/{library_id}/videos/{video_id}")
    def video_details(library_id: str, video_id: int):
        with Store(state.library(library_id)["path"], create=False) as store:
            video = store.video(video_id)
            video["counts"] = dict(
                store.db.execute(
                    "SELECT kind,count(*) FROM items WHERE video_id=? GROUP BY kind", (video_id,)
                )
            )
            video["locations"] = [
                r[0]
                for r in store.db.execute(
                    "SELECT path FROM file_locations WHERE video_id=? ORDER BY path", (video_id,)
                )
            ] or [video["path"]]
            path = Path(video["path"])
            try:
                stat = path.stat()
                video["size_bytes"] = stat.st_size
                video["source_available"] = media.fingerprint(path) == video["fingerprint"]
            except OSError:
                video["source_available"] = False
            roots = store.items(video_id, kind="summary", complete_only=True, limit=1)
            video["summary"] = roots[0]["text"] if roots else None
            return video

    @app.get("/api/libraries/{library_id}/videos/{video_id}/observations")
    def observations(
        library_id: str, video_id: int, kind: str = "window", q: str = "", offset: int = 0, limit: int = 30
    ):
        from .server import validate_page

        validate_page(offset, limit)
        if kind not in {"summary", "episode", "event", "window", "speech", "ocr"}:
            raise ValueError("Invalid observation kind")
        if len(q) > 2000:
            raise ValueError("Search is too long")
        with Store(state.library(library_id)["path"], create=False) as store:
            store.video(video_id)
            where = (
                "i.video_id=? AND i.kind=? AND instr(lower(i.text),lower(?))>0 "
                "AND EXISTS(SELECT 1 FROM stages s WHERE s.video_id=i.video_id "
                "AND s.name=i.stage AND s.status='complete')"
            )
            args = (video_id, kind, q.strip())
            total = store.db.execute("SELECT count(*) FROM items i WHERE " + where, args).fetchone()[0]
            rows = store.db.execute(
                "SELECT i.* FROM items i WHERE "
                + where
                + " ORDER BY i.start_ms,i.end_ms,i.id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            return {"total": total, "offset": offset, "items": [store.unpack(r) for r in rows]}

    @app.get("/api/libraries/{library_id}/videos/{video_id}/items/{item_id}")
    def observation_details(library_id: str, video_id: int, item_id: str):
        with Store(state.library(library_id)["path"], create=False) as store:
            node = store.item(item_id)
            if node["video_id"] != video_id:
                raise ValueError("Observation does not belong to the selected video")
            node["speech_and_ocr"] = store.overlaps(video_id, node["start_ms"], node["end_ms"])
            return node

    @app.get("/api/libraries/{library_id}/videos/{video_id}/children/{item_id}")
    def observation_children(library_id: str, video_id: int, item_id: str):
        with Store(state.library(library_id)["path"], create=False) as store:
            node = store.item(item_id)
            if node["video_id"] != video_id:
                raise ValueError("Observation does not belong to the selected video")
            children = [store.item(i) for i in node["children"]]
            if any(n["video_id"] != video_id for n in children):
                raise ValueError("Invalid tree edge outside the selected video")
            return {"items": children, "total": len(children)}

    @app.get("/api/libraries/{library_id}/videos/{video_id}/frame")
    def evidence_frame(library_id: str, video_id: int, time_s: float):
        with Store(state.library(library_id)["path"], create=False) as store:
            video = store.video(video_id)
            if not math.isfinite(time_s) or not 0 <= time_s < video["metadata"]["duration_ms"] / 1000:
                raise ValueError("Frame time is outside this video")
            path = Path(video["path"])
            if not path.is_file() or media.fingerprint(path) != video["fingerprint"]:
                raise ValueError("Source changed or unavailable. Refresh the library first.")
            import io

            buffer = io.BytesIO()
            media.frame_at(path, round(time_s * 1000), 1024).save(buffer, format="JPEG", quality=85)
            return Response(buffer.getvalue(), media_type="image/jpeg")

    return app
