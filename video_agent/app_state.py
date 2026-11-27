"""Persistent library selection and process-independent job supervision."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .config import parse_config
from .index_storage import CONFIG_NAME, delete_index, index_path
from .store import Store

PROJECT = Path(__file__).resolve().parent.parent


def state_directory(path=None):
    return (
        Path(
            path
            or os.environ.get(
                "VIDEO_LIBRARY_STATE_DIR", os.environ.get("VIDEO_AGENT_STATE_DIR", PROJECT / ".cache" / "app")
            )
        )
        .expanduser()
        .resolve()
    )


class LibraryBusyError(ValueError):
    pass


class AppState:
    def __init__(self, directory=None):
        self.directory = state_directory(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS libraries(id TEXT PRIMARY KEY,path TEXT UNIQUE,config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,library_id TEXT,folder TEXT,kind TEXT,config TEXT,status TEXT,
                created_at REAL,started_at REAL,finished_at REAL,pid INTEGER,cancel_requested INTEGER DEFAULT 0,
                result TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS timings(
                library_id TEXT,video_id INTEGER,stage TEXT,signature TEXT,elapsed REAL,units REAL,
                PRIMARY KEY(library_id,video_id,stage));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "position" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN position REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE jobs SET position=created_at")
            db.execute(
                "INSERT OR IGNORE INTO preferences(key,value) SELECT 'queue_paused',"
                "CASE WHEN EXISTS(SELECT 1 FROM jobs WHERE status IN ('paused','interrupted')) THEN '1' ELSE '0' END"
            )

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.directory / "app.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def libraries(self):
        with self.connect() as db:
            return [
                {**dict(r), "config": json.loads(r["config"])}
                for r in db.execute("SELECT * FROM libraries ORDER BY rowid")
            ]

    def active_id(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM preferences WHERE key='active'").fetchone()
            return row[0] if row else None

    def library(self, library_id=None):
        library_id = library_id or self.active_id()
        with self.connect() as db:
            row = db.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
        if not row:
            raise ValueError("Choose a video folder first")
        return {**dict(row), "config": json.loads(row["config"])}

    def active_folder(self):
        return Path(self.library()["path"])

    def inspect_folder(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Enter a folder path")
        folder = Path(path).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Folder does not exist or is not accessible")
        with self.connect() as db:
            registered = db.execute("SELECT id,config FROM libraries WHERE path=?", (str(folder),)).fetchone()
        saved = None
        videos = []
        if index_path(folder).is_file():
            with Store(folder, create=False) as store:
                saved = store.saved_config()
                if saved is not None:
                    videos = store.videos(limit=1_000_000)
        else:
            file = index_path(folder).parent / CONFIG_NAME
            if file.is_file():
                try:
                    saved = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ValueError(f"Could not read saved indexing settings: {file}") from exc
        config = parse_config(
            saved if saved is not None else json.loads(registered["config"]) if registered else {}
        )
        from .signatures import stage_signatures
        from .store import STAGES

        signatures = stage_signatures(config)

        def unfinished(video):
            stages = {stage["name"]: stage for stage in video["stages"]}
            return bool(video["error"]) or any(
                name not in stages
                or stages[name]["status"] != "complete"
                or stages[name]["signature"] != signatures[name]
                for name in STAGES
            )

        return {
            "path": str(folder),
            "config": asdict(config),
            "needs_resume": saved is not None and (not videos or any(unfinished(video) for video in videos)),
            "has_saved_config": saved is not None,
            "library_id": registered["id"] if registered else None,
        }

    def select(self, path):
        with self.queue_guard():
            inspected = self.inspect_folder(path)
            with self.connect() as db:
                library_id = inspected["library_id"] or uuid.uuid4().hex
                db.execute(
                    "INSERT INTO libraries VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                    (library_id, inspected["path"], json.dumps(inspected["config"])),
                )
                db.execute("INSERT OR REPLACE INTO preferences VALUES('active',?)", (library_id,))
                db.execute("INSERT OR REPLACE INTO preferences VALUES('libraries_initialized','1')")
        return self.library(library_id)

    def configure_library(self, path, raw):
        config = parse_config(raw)
        with self.queue_guard():
            inspected = self.inspect_folder(path)
            library_id = inspected["library_id"] or uuid.uuid4().hex
            self._save_folder_settings(library_id, inspected["path"], config, inspected["config"])
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO preferences VALUES('active',?)", (library_id,))
                db.execute("INSERT OR REPLACE INTO preferences VALUES('libraries_initialized','1')")
        return self.library(library_id)

    @staticmethod
    def _reset_empty_queue(db):
        if not db.execute(
            "SELECT 1 FROM jobs WHERE status IN ('queued','starting','running','pausing','paused','interrupted')"
        ).fetchone():
            db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','0')")

    def remove_library(self, library_id):
        """Remove the library and its saved index as one confirmed action."""
        return self._delete_library_data(library_id, keep_library=False)

    def reset_library_index(self, library_id):
        """Keep the registered folder/settings, erase its index, and await setup."""
        return self._delete_library_data(library_id, keep_library=True)

    def _delete_library_data(self, library_id, *, keep_library):
        with self.queue_guard():
            library = self.library(library_id)
            with self.connect() as db:
                if db.execute(
                    "SELECT 1 FROM jobs WHERE library_id=? AND status IN ('starting','running','pausing')",
                    (library_id,),
                ).fetchone():
                    raise LibraryBusyError(
                        "Pause this library’s indexing task and wait for it to stop before deleting its index."
                    )
                try:
                    warnings = delete_index(library["path"])
                except RuntimeError as exc:
                    raise LibraryBusyError(str(exc)) from exc
                db.execute("DELETE FROM jobs WHERE library_id=?", (library_id,))
                db.execute("DELETE FROM timings WHERE library_id=?", (library_id,))
                if keep_library:
                    db.execute("INSERT OR REPLACE INTO preferences VALUES('active',?)", (library_id,))
                else:
                    db.execute("DELETE FROM libraries WHERE id=?", (library_id,))
                active = db.execute("SELECT value FROM preferences WHERE key='active'").fetchone()
                if not keep_library and active and active[0] == library_id:
                    next_library = db.execute("SELECT id FROM libraries ORDER BY rowid LIMIT 1").fetchone()
                    db.execute(
                        "INSERT OR REPLACE INTO preferences VALUES('active',?)",
                        (next_library[0] if next_library else None,),
                    )
                db.execute("INSERT OR REPLACE INTO preferences VALUES('libraries_initialized','1')")
                self._reset_empty_queue(db)
        thumbnails = self.directory / "thumbnails" / library_id
        if Path(library_id).name == library_id and thumbnails.is_dir() and not thumbnails.is_symlink():
            import shutil

            try:
                shutil.rmtree(thumbnails)
            except OSError:
                warnings.append("Some cached thumbnails could not be removed.")
        if keep_library:
            return {"library": self.library(library_id), "index_deleted": True, "warnings": warnings}
        return {"removed": True, "index_deleted": True, "warnings": warnings}

    def _save_folder_settings(self, library_id, folder, config, previous):
        from .pipeline import writer_lock
        from .signatures import stage_signatures

        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM jobs WHERE library_id=? AND status IN ('starting','running','pausing')",
                (library_id,),
            ).fetchone():
                raise LibraryBusyError(
                    "Pause this library’s task and wait for checkpoints before changing indexing settings."
                )
        try:
            with Store(folder, blocking=False) as store, writer_lock(store):
                store.apply_config(asdict(config), stage_signatures(config))
        except (BlockingIOError, RuntimeError) as exc:
            raise LibraryBusyError(
                "Index is in use. Pause indexing and close active MCP requests, then retry."
            ) from exc
        with self.connect() as db:
            db.execute(
                "INSERT INTO libraries VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                (library_id, str(folder), json.dumps(asdict(config))),
            )
            if asdict(config) != asdict(parse_config(previous)):
                # Keep old job snapshots immutable; an explicit settings edit supersedes
                # waiting work. The UI enqueues a new task with the saved configuration.
                db.execute(
                    "UPDATE jobs SET status='cancelled',finished_at=? WHERE library_id=? "
                    "AND status IN ('queued','paused','interrupted')",
                    (time.time(), library_id),
                )
                self._reset_empty_queue(db)

    def save_config(self, library_id, raw):
        config = parse_config(raw)
        with self.queue_guard():
            library = self.library(library_id)
            self._save_folder_settings(library_id, library["path"], config, library["config"])
        return asdict(config)

    def update_job(self, job_id, **values):
        allowed = {"status", "started_at", "finished_at", "pid", "cancel_requested", "result", "error"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job update")
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                [*values.values(), job_id],
            )

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found")
        value = dict(row)
        for key in ("config", "result"):
            if value[key]:
                value[key] = json.loads(value[key])
        return value

    def latest_job(self, library_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE library_id=? ORDER BY created_at DESC LIMIT 1", (library_id,)
            ).fetchone()
        return self.job(row[0]) if row else None

    def _lock(self):
        stream = (self.directory / "worker.lock").open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    @contextlib.contextmanager
    def model_access(self):
        """Share the worker lock with MCP searches to avoid competing for GPU RAM."""
        with (self.directory / "worker.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    @contextlib.contextmanager
    def queue_guard(self):
        with (self.directory / "queue.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            yield

    def _runner_lock(self):
        stream = (self.directory / "runner.lock").open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    def queue_paused(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM preferences WHERE key='queue_paused'").fetchone()
            return bool(row and row[0] == "1")

    def queue_jobs(self):
        with self.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','starting','running','pausing','paused','interrupted') "
                    "ORDER BY CASE WHEN status IN ('starting','running','pausing') THEN 0 ELSE 1 END,position,created_at"
                )
            ]
        return [self.job(i) for i in ids]

    def _recover_locked(self):
        runner = self._runner_lock()
        if runner is None:
            return
        with runner:
            # Older app versions own only worker.lock; do not interrupt their work.
            memory = self._lock()
            if memory is None:
                return
            with memory, self.connect() as db:
                changed = db.execute(
                    "UPDATE jobs SET status='interrupted',finished_at=?,"
                    "error='Worker stopped. Resume the queue to continue from checkpoints.' "
                    "WHERE status IN ('starting','running','pausing')",
                    (time.time(),),
                ).rowcount
                if changed:
                    db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','1')")

    def recover(self):
        with self.queue_guard():
            self._recover_locked()
            self._dispatch_locked()

    def _dispatch_locked(self):
        if self.queue_paused():
            return
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY position,created_at LIMIT 1"
            ).fetchone()
        if not row:
            return
        runner = self._runner_lock()
        if runner is None:
            return
        try:
            with (self.directory / "queue-runner.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "video_agent.app_worker",
                        "--state",
                        str(self.directory),
                        "--job",
                        row[0],
                        "--lock-fd",
                        str(runner.fileno()),
                    ],
                    cwd=PROJECT,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(runner.fileno(),),
                    start_new_session=True,
                )
            threading.Thread(target=child.wait, daemon=True).start()
        finally:
            runner.close()

    def launch(self, library_id, kind="index"):
        if kind not in {"index", "refresh"}:
            raise ValueError("Invalid job kind")
        with self.queue_guard():
            library = self.library(library_id)
            config = parse_config(library["config"])
            self._recover_locked()
            with self.connect() as db:
                row = db.execute(
                    "SELECT id FROM jobs WHERE library_id=? AND kind=? "
                    "AND status IN ('queued','starting','running','pausing','paused','interrupted') "
                    "ORDER BY position LIMIT 1",
                    (library_id, kind),
                ).fetchone()
                job_id = row[0] if row else uuid.uuid4().hex
                if not row:
                    position = db.execute("SELECT coalesce(max(position),0)+1 FROM jobs").fetchone()[0]
                    db.execute(
                        "INSERT INTO jobs(id,library_id,folder,kind,config,status,created_at,position) "
                        "VALUES(?,?,?,?,?,'queued',?,?)",
                        (
                            job_id,
                            library_id,
                            library["path"],
                            kind,
                            json.dumps(asdict(config)),
                            time.time(),
                            position,
                        ),
                    )
            self._dispatch_locked()
        return self.job(job_id)

    def next_queued(self, runner_fd, memory):
        """Claim under the enqueue lock; release locks here to avoid a lost last enqueue."""
        with self.queue_guard(), self.connect() as db:
            # Holding model memory proves that no older worker is still processing.
            stale = db.execute(
                "UPDATE jobs SET status='interrupted',finished_at=?,error='Worker stopped. Resume the queue.' "
                "WHERE status IN ('starting','running','pausing')",
                (time.time(),),
            ).rowcount
            if stale:
                db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','1')")
            row = (
                None
                if self.queue_paused()
                else db.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY position,created_at LIMIT 1"
                ).fetchone()
            )
            if row is None:
                memory.close()
                os.close(runner_fd)
                return None
            job_id = row[0]
            db.execute(
                "UPDATE jobs SET status='starting',pid=?,cancel_requested=0,error=NULL,result=NULL,"
                "started_at=?,finished_at=NULL WHERE id=?",
                (os.getpid(), time.time(), job_id),
            )
        return self.job(job_id)

    def pause_queue(self):
        with self.queue_guard(), self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','1')")
            db.execute(
                "UPDATE jobs SET cancel_requested=1,status='pausing' WHERE status IN ('starting','running')"
            )
        return {"paused": True}

    def resume_queue(self):
        with self.queue_guard():
            self._recover_locked()
            with self.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE status='pausing'").fetchone():
                    raise ValueError("Wait for the current task to save its checkpoints")
                db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','0')")
                db.execute(
                    "UPDATE jobs SET status='queued',cancel_requested=0,error=NULL "
                    "WHERE status IN ('paused','interrupted')"
                )
            self._dispatch_locked()
        return {"paused": False}

    def pause(self, job_id):
        self.job(job_id)
        self.pause_queue()
        return self.job(job_id)

    def remove_job(self, job_id):
        with self.queue_guard(), self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status='cancelled',finished_at=? WHERE id=? "
                "AND status IN ('queued','paused','interrupted')",
                (time.time(), job_id),
            ).rowcount
            if not changed:
                raise ValueError("Only waiting or paused tasks can be removed")
            self._reset_empty_queue(db)
        return self.job(job_id)

    def move_job(self, job_id, direction):
        if direction not in {"up", "down"}:
            raise ValueError("Direction must be up or down")
        with self.queue_guard(), self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT id,position FROM jobs WHERE status IN ('queued','paused','interrupted') "
                    "ORDER BY position,created_at"
                )
            )
            ids = [r[0] for r in rows]
            if job_id not in ids:
                raise ValueError("Wait for the current task to pause before moving it")
            index = ids.index(job_id)
            other = index + (-1 if direction == "up" else 1)
            if 0 <= other < len(rows):
                ids[index], ids[other] = ids[other], ids[index]
                db.executemany(
                    "UPDATE jobs SET position=? WHERE id=?",
                    [(position, identity) for position, identity in enumerate(ids, 1)],
                )
        return self.job(job_id)

    def retry_job(self, job_id):
        with self.queue_guard():
            with self.connect() as db:
                job = self.job(job_id)
                if job["status"] != "failed":
                    raise ValueError("Only failed tasks can be retried")
                if db.execute(
                    "SELECT 1 FROM jobs WHERE library_id=? AND kind=? AND status IN "
                    "('queued','running','starting','pausing','paused','interrupted')",
                    (job["library_id"], job["kind"]),
                ).fetchone():
                    raise ValueError("This library is already in the queue")
                position = db.execute("SELECT coalesce(max(position),0)+1 FROM jobs").fetchone()[0]
                db.execute(
                    "UPDATE jobs SET status='queued',position=?,error=NULL,cancel_requested=0 WHERE id=?",
                    (position, job_id),
                )
            self._dispatch_locked()
        return self.job(job_id)

    def cancelled(self, job_id):
        return bool(self.job(job_id)["cancel_requested"])

    def learn_timings(self, library_id, store):
        from .library import indexed_config
        from .progress import units
        from .signatures import stage_signatures

        config = indexed_config(store)
        signatures = stage_signatures(config)
        samples = []
        for video in store.videos(limit=1_000_000):
            count = store.count_items(video["id"])
            for stage in video["stages"]:
                name = stage["name"]
                if stage["status"] != "complete" or stage["signature"] != signatures[name]:
                    continue
                total, _ = units(video, name, config, count, count)
                if total and stage["elapsed_s"] > 0:
                    samples.append(
                        (library_id, video["id"], name, stage["signature"], stage["elapsed_s"], total)
                    )
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO timings VALUES(?,?,?,?,?,?)", samples)

    def timing_rates(self, config):
        from statistics import median

        from .signatures import stage_signatures

        signatures = stage_signatures(config)
        with self.connect() as db:
            return {
                name: median(samples)
                for name, signature in signatures.items()
                if (
                    samples := [
                        row[0]
                        for row in db.execute(
                            "SELECT elapsed/units FROM timings WHERE stage=? AND signature=? AND units>0 AND elapsed>0",
                            (name, signature),
                        )
                    ]
                )
            }

    def mcp_config(self):
        # Resolver state is explicit, so another checkout never changes this server.
        return {
            "mcpServers": {
                "video-library": {
                    "command": sys.executable,
                    "args": [str(PROJECT / "mcp_server.py"), "--active", "--state-dir", str(self.directory)],
                }
            }
        }

    def mcp_command(self):
        config = self.mcp_config()["mcpServers"]["video-library"]
        return shlex.join(["codex", "mcp", "add", "video-library", "--", config["command"], *config["args"]])
