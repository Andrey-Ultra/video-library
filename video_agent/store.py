from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .index_storage import (
    CONFIG_NAME,
    DB_NAME,
    INDEX_DIRECTORY,
    index_access,
    index_path,
    migrate_legacy_index,
)

STAGES = ("visual", "speech", "ocr", "hierarchy", "embedding")
DEPENDENTS = {
    "visual": ("hierarchy", "embedding"),
    "speech": ("embedding",),
    "ocr": ("embedding",),
    "hierarchy": ("embedding",),
    "embedding": (),
}


class Store:
    def __init__(self, folder: str | Path, create: bool = True, blocking: bool = True):
        self.readonly = not create
        self.folder = Path(folder).expanduser().resolve()
        if not self.folder.is_dir():
            raise NotADirectoryError(self.folder)
        if create:
            migrate_legacy_index(self.folder)
        # Readers must not see a newly created SQLite file until its schema is
        # committed. Downgrade to a shared lease once initialization is complete.
        self._access = index_access(self.folder, exclusive=create, blocking=blocking)
        access_lock = self._access.__enter__()
        try:
            self.path = self.folder / INDEX_DIRECTORY / DB_NAME if create else index_path(self.folder)
            self.directory = self.path.parent
            if create:
                self.directory.mkdir(exist_ok=True)
                self.db = sqlite3.connect(self.path, timeout=30)
            else:
                if not self.path.exists():
                    raise FileNotFoundError(f"Index not found. Run: python main.py '{self.folder}'")
                self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if create:
                self.db.execute("PRAGMA journal_mode=WAL")
                self._schema()
                fcntl.flock(access_lock.fileno(), fcntl.LOCK_SH)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._access.__exit__(None, None, None)
            raise

    def __enter__(self):
        if self.readonly:
            # Keep config, vectors and evidence on one consistent WAL snapshot
            # while an indexer may publish/invalidate stages in another process.
            self.db.execute("BEGIN")
        return self

    def __exit__(self, *_):
        try:
            self.db.close()
        finally:
            self._access.__exit__(None, None, None)

    def _schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS videos(
            id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            metadata TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1,
            error TEXT, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS stages(
            video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
            name TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL,
            checkpoint INTEGER NOT NULL DEFAULT 0, elapsed_s REAL NOT NULL DEFAULT 0,
            error TEXT, PRIMARY KEY(video_id, name));
        CREATE TABLE IF NOT EXISTS items(
            id TEXT PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            stage TEXT NOT NULL, kind TEXT NOT NULL, start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL,
            CHECK(start_ms >= 0 AND end_ms > start_ms));
        CREATE INDEX IF NOT EXISTS items_video_time ON items(video_id, start_ms);
        CREATE TABLE IF NOT EXISTS edges(
            parent_id TEXT REFERENCES items(id) ON DELETE CASCADE,
            child_id TEXT REFERENCES items(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, PRIMARY KEY(parent_id, child_id));
        CREATE TABLE IF NOT EXISTS vectors(
            item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            namespace TEXT NOT NULL, dims INTEGER NOT NULL, data BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS vectors_namespace ON vectors(namespace);
        CREATE INDEX IF NOT EXISTS edges_child ON edges(child_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, item_id UNINDEXED);
        CREATE TRIGGER IF NOT EXISTS items_insert AFTER INSERT ON items BEGIN
            INSERT INTO items_fts(text,item_id) VALUES(new.text,new.id);
        END;
        CREATE TRIGGER IF NOT EXISTS items_delete AFTER DELETE ON items BEGIN
            DELETE FROM items_fts WHERE item_id=old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS items_update AFTER UPDATE ON items BEGIN
            DELETE FROM items_fts WHERE item_id=old.id;
            INSERT INTO items_fts(text,item_id) VALUES(new.text,new.id);
        END;
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(stages)")}
        if "provenance" not in columns:
            self.db.execute("ALTER TABLE stages ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'")
        if "started_at" not in columns:
            self.db.execute("ALTER TABLE stages ADD COLUMN started_at REAL")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(videos)")}
        for name in ("content_hash", "full_hash"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE videos ADD COLUMN {name} TEXT")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS file_locations(
            path TEXT PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS locations_video ON file_locations(video_id);
        CREATE TABLE IF NOT EXISTS work_cache(
            video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE, stage TEXT NOT NULL,
            key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(video_id,stage,key));
        """)
        self.db.execute(
            "INSERT OR IGNORE INTO settings(key,value) "
            "SELECT 'next_video_id',cast(coalesce(max(id),0)+1 AS TEXT) FROM videos"
        )
        self.db.commit()

    def _write_config_snapshot(self, config):
        # SQLite is authoritative. JSON is an atomic, human-readable copy in the
        # same folder; file statuses/checkpoints stay in the transactional DB.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, prefix=".config-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(config, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / CONFIG_NAME)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def _config_update(self, config):
        previous = self.saved_config()
        self._write_config_snapshot(config)
        try:
            with self.db:
                yield
        except BaseException:
            # If the DB transaction fails, restore its human-readable mirror too.
            with contextlib.suppress(OSError):
                if previous is None:
                    (self.directory / CONFIG_NAME).unlink(missing_ok=True)
                else:
                    self._write_config_snapshot(previous)
            raise

    def save_config(self, config: dict):
        with self._config_update(config):
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('config',?)", (json.dumps(config),))

    def saved_config(self) -> dict | None:
        row = self.db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        if row:
            return json.loads(row[0])
        path = self.directory / CONFIG_NAME
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ValueError(f"Could not read saved indexing settings: {path}") from exc
            if not isinstance(value, dict):
                raise ValueError("Saved indexing settings must be an object")
            return value
        return None

    def apply_config(self, config, signatures):
        """Reset affected stages atomically, preserving other evidence/vectors."""
        with self._config_update(config):
            for (video_id,) in self.db.execute("SELECT id FROM videos"):
                current = {
                    r[0]: r[1]
                    for r in self.db.execute(
                        "SELECT name,signature FROM stages WHERE video_id=?", (video_id,)
                    )
                }
                changed = {name for name in STAGES if name in current and current[name] != signatures[name]}
                affected = changed | {dependent for name in changed for dependent in DEPENDENTS[name]}
                for stage in affected:
                    self._invalidate_stage(video_id, stage, clear_vectors="embedding" in changed)
                for stage in STAGES:
                    self.db.execute(
                        "INSERT OR IGNORE INTO stages(video_id,name,signature,status) VALUES(?,?,?,'pending')",
                        (video_id, stage, signatures[stage]),
                    )
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('config',?)", (json.dumps(config),))

    def register(self, path: Path, fingerprint: str, metadata: dict) -> int:
        with self.db:
            row = self.db.execute("SELECT id,fingerprint FROM videos WHERE path=?", (str(path),)).fetchone()
            if row:
                video_id = row["id"]
                if row["fingerprint"] != fingerprint:
                    self.db.execute("DELETE FROM items WHERE video_id=?", (video_id,))
                    self.db.execute("DELETE FROM stages WHERE video_id=?", (video_id,))
                    self.db.execute("DELETE FROM work_cache WHERE video_id=?", (video_id,))
                self.db.execute(
                    "UPDATE videos SET fingerprint=?,metadata=?,present=1,error=NULL,updated_at=? WHERE id=?",
                    (fingerprint, json.dumps(metadata), time.time(), video_id),
                )
            else:
                video_id = self.next_video_id()
                self.db.execute(
                    "INSERT INTO videos(id,path,fingerprint,metadata,updated_at) VALUES(?,?,?,?,?)",
                    (video_id, str(path), fingerprint, json.dumps(metadata), time.time()),
                )
        return video_id

    def next_video_id(self):
        current = self.db.execute("SELECT coalesce(max(id),0)+1 FROM videos").fetchone()[0]
        saved = self.db.execute("SELECT value FROM settings WHERE key='next_video_id'").fetchone()
        value = max(current, int(saved[0]) if saved else 1)
        self.db.execute("INSERT OR REPLACE INTO settings VALUES('next_video_id',?)", (str(value + 1),))
        return value

    def reconcile(self, paths: list[Path]):
        with self.db:
            self.db.execute("UPDATE videos SET present=0")
            self.db.executemany("UPDATE videos SET present=1 WHERE path=?", [(str(p),) for p in paths])

    @staticmethod
    def _video_row(row):
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["stages"] = []
        return result

    @staticmethod
    def _stage_row(row):
        result = dict(row)
        result["provenance"] = json.loads(result.get("provenance", "{}"))
        return result

    def video(self, video_id: int) -> dict:
        row = self.db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if row is None:
            raise ValueError(f"Video {video_id} not found")
        result = self._video_row(row)
        result["stages"] = [
            self._stage_row(stage)
            for stage in self.db.execute("SELECT * FROM stages WHERE video_id=? ORDER BY name", (video_id,))
        ]
        return result

    def videos(self, offset=0, limit=30):
        # One page query plus one stage query, regardless of the library size.
        # Repeat the page as a subquery instead of expanding an unbounded IN list
        # (SQLite limits the number of bound parameters in large libraries).
        page = "SELECT id FROM videos WHERE present=1 ORDER BY path LIMIT ? OFFSET ?"
        videos = [
            self._video_row(row)
            for row in self.db.execute(
                "SELECT * FROM videos WHERE present=1 ORDER BY path LIMIT ? OFFSET ?", (limit, offset)
            )
        ]
        if videos:
            by_id = {video["id"]: video for video in videos}
            for row in self.db.execute(
                f"SELECT * FROM stages WHERE video_id IN ({page}) ORDER BY video_id,name", (limit, offset)
            ):
                by_id[row["video_id"]]["stages"].append(self._stage_row(row))
        return videos

    def total_videos(self):
        return self.db.execute("SELECT count(*) FROM videos WHERE present=1").fetchone()[0]

    def stage(self, video_id, name):
        row = self.db.execute("SELECT * FROM stages WHERE video_id=? AND name=?", (video_id, name)).fetchone()
        if row is None:
            return None
        return self._stage_row(row)

    def _invalidate_stage(self, video_id, stage, clear_vectors):
        self.db.execute("DELETE FROM work_cache WHERE video_id=? AND stage=?", (video_id, stage))
        if stage == "embedding":
            if clear_vectors:
                self.db.execute(
                    "DELETE FROM vectors WHERE item_id IN (SELECT id FROM items WHERE video_id=?)",
                    (video_id,),
                )
        else:
            self.db.execute("DELETE FROM items WHERE video_id=? AND stage=?", (video_id, stage))
        self.db.execute("DELETE FROM stages WHERE video_id=? AND name=?", (video_id, stage))

    def invalidate(self, video_id: int, name: str):
        with self.db:
            for stage in (name, *DEPENDENTS[name]):
                self._invalidate_stage(video_id, stage, clear_vectors=name == "embedding")

    def prepare(self, video_id: int, name: str, signature: str, force: bool = False, provenance=None):
        current = self.stage(video_id, name)
        if force or (current and current["signature"] != signature):
            self.invalidate(video_id, name)
            current = None
        if current is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO stages(video_id,name,signature,status) VALUES(?,?,?,'pending')",
                    (video_id, name, signature),
                )
        if provenance is not None:
            with self.db:
                self.db.execute(
                    "UPDATE stages SET provenance=? WHERE video_id=? AND name=?",
                    (json.dumps(provenance), video_id, name),
                )
        return self.stage(video_id, name)

    def stage_status(self, video_id, name, status, checkpoint=None, elapsed_s=None, error=None):
        assignments, params = ["status=?", "error=?"], [status, error]
        assignments.append("started_at=?")
        params.append(time.time() if status == "running" else None)
        for field, value in (("checkpoint", checkpoint), ("elapsed_s", elapsed_s)):
            if value is not None:
                assignments.append(f"{field}=?")
                params.append(value)
        with self.db:
            self.db.execute(
                f"UPDATE stages SET {','.join(assignments)} WHERE video_id=? AND name=?",
                [*params, video_id, name],
            )

    def put_item(self, item: dict):
        self.db.execute(
            "INSERT INTO items(id,video_id,stage,kind,start_ms,end_ms,text,payload) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "text=excluded.text,payload=excluded.payload,end_ms=excluded.end_ms",
            (
                item["id"],
                item["video_id"],
                item["stage"],
                item["kind"],
                item["start_ms"],
                item["end_ms"],
                item["text"],
                json.dumps(item.get("payload", {}), ensure_ascii=False),
            ),
        )

    @staticmethod
    def unpack(row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["start_s"] = item["start_ms"] / 1000
        item["end_s"] = item["end_ms"] / 1000
        return item

    def items(self, video_id, stage=None, kind=None, offset=0, limit=100, complete_only=False):
        where, args = ["i.video_id=?"], [video_id]
        if stage:
            where.append("i.stage=?")
            args.append(stage)
        if kind:
            where.append("i.kind=?")
            args.append(kind)
        if complete_only:
            where.append(
                "EXISTS(SELECT 1 FROM stages s WHERE s.video_id=i.video_id "
                "AND s.name=i.stage AND s.status='complete')"
            )
        rows = self.db.execute(
            "SELECT i.* FROM items i WHERE "
            + " AND ".join(where)
            + " ORDER BY i.start_ms,i.end_ms,i.id LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [self.unpack(r) for r in rows]

    def item(self, item_id):
        row = self.db.execute(
            "SELECT i.*,v.path FROM items i JOIN videos v ON v.id=i.video_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Item {item_id} not found")
        item = self.unpack(row)
        item["children"] = [
            r[0]
            for r in self.db.execute(
                "SELECT child_id FROM edges WHERE parent_id=? ORDER BY position", (item_id,)
            )
        ]
        item["parents"] = [
            r[0] for r in self.db.execute("SELECT parent_id FROM edges WHERE child_id=?", (item_id,))
        ]
        stage = self.stage(item["video_id"], item["stage"])
        item["stage_status"] = stage["status"] if stage else "pending"
        item["provenance"] = stage["provenance"] if stage else {}
        return item

    def overlaps(self, video_id, start_ms, end_ms):
        rows = self.db.execute(
            "SELECT i.* FROM items i JOIN stages s ON "
            "s.video_id=i.video_id AND s.name=i.stage WHERE i.video_id=? "
            "AND i.start_ms<? AND i.end_ms>? AND s.status='complete' "
            "AND i.stage IN ('speech','ocr') ORDER BY i.start_ms LIMIT 100",
            (video_id, end_ms, start_ms),
        )
        return [self.unpack(r) for r in rows]

    def link(self, parent_id, children):
        self.db.executemany(
            "INSERT INTO edges(parent_id,child_id,position) VALUES(?,?,?)",
            [(parent_id, child, pos) for pos, child in enumerate(children)],
        )

    def count_items(self, video_id, kind=None):
        query = "SELECT count(*) FROM items WHERE video_id=?"
        args = [video_id]
        if kind:
            query += " AND kind=?"
            args.append(kind)
        return self.db.execute(query, args).fetchone()[0]
