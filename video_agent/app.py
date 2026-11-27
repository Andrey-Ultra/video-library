"""Private API for the native desktop app; models run in a detached worker process."""

from __future__ import annotations

import fcntl
import json
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import media
from .app_state import AppState, LibraryBusyError
from .model_assets import ModelAssets
from .progress import ProgressTracker
from .store import Store
from .view_state import snapshot as snapshot


def create_app(directory=None):
    state = AppState(directory)
    tracker = ProgressTracker()
    assets = ModelAssets(state.directory)

    @asynccontextmanager
    async def lifespan(_):
        state.recover()
        yield

    app = FastAPI(
        title="Video library local service",
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

    @app.exception_handler(LibraryBusyError)
    async def library_busy(_, exc):
        return Response(json.dumps({"detail": str(exc)}), status_code=409, media_type="application/json")

    @app.get("/")
    def home():
        return {"app": "Video library", "interface": "native", "status": "ready"}

    @app.get("/api/state")
    def status():
        return snapshot(state, tracker)

    @app.post("/api/models/status")
    def model_status(body: dict):
        return assets.resources(body.get("config", {}))

    @app.post("/api/models/download")
    def download_model(body: dict):
        return assets.start(body.get("config", {}), body.get("model", ""))

    @app.post("/api/models/delete")
    def delete_model(body: dict):
        if body.get("confirmed") is not True:
            raise ValueError("Confirm deleting the downloaded model first")
        with state.queue_guard(), (state.directory / "worker.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LibraryBusyError(
                    "Models are in use. Pause indexing and close active model requests first."
                ) from None
            repo = body.get("model", "")
            for job in state.queue_jobs():
                cfg = job.get("config", {})
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                if job["status"] in {"queued", "starting", "running", "pausing"} and any(
                    cfg.get(section, {}).get("model") == repo
                    and cfg.get(section, {}).get("backend") in {"mlx", "mlx-text", "parakeet"}
                    for section in ("visual", "speech", "embedding", "answer")
                ):
                    raise LibraryBusyError(
                        "This model is needed by the processing queue. Remove its waiting tasks or pause them first."
                    )
            return assets.delete(body.get("config", {}), repo)

    @app.post("/api/libraries/inspect")
    def inspect_folder(body: dict):
        return state.inspect_folder(body.get("path", ""))

    @app.post("/api/libraries/configure")
    def configure_folder(body: dict):
        return state.configure_library(body.get("path", ""), body.get("config", {}))

    @app.post("/api/libraries")
    def select_folder(body: dict):
        return state.select(body.get("path", ""))

    @app.delete("/api/libraries/{library_id}")
    def remove_library(library_id: str, body: dict):
        if body.get("confirmed") is not True:
            raise ValueError("Confirm removal of the library and its saved index first")
        return state.remove_library(library_id)

    @app.delete("/api/libraries/{library_id}/index")
    def reset_library_index(library_id: str, body: dict):
        if body.get("confirmed") is not True:
            raise ValueError("Confirm deletion of the saved index first")
        return state.reset_library_index(library_id)

    @app.put("/api/libraries/{library_id}/config")
    def settings(library_id: str, body: dict):
        return state.save_config(library_id, body)

    @app.post("/api/libraries/{library_id}/jobs")
    def start_job(library_id: str, body: dict):
        return state.launch(library_id, body.get("kind", "index"))

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str):
        return state.pause(job_id)

    @app.post("/api/queue/pause")
    def pause_queue():
        return state.pause_queue()

    @app.post("/api/queue/resume")
    def resume_queue():
        return state.resume_queue()

    @app.post("/api/jobs/{job_id}/remove")
    def remove_job(job_id: str):
        return state.remove_job(job_id)

    @app.post("/api/jobs/{job_id}/move")
    def move_job(job_id: str, body: dict):
        return state.move_job(job_id, body.get("direction"))

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        return state.retry_job(job_id)

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
