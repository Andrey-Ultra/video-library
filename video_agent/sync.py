"""Reconcile source locations with indexed content, under the library writer lock."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from . import media


def synchronize(store):
    # Finish reading the directory before changing anything. A failed scan must not
    # look like an empty folder and erase an index on an unavailable volume.
    paths = list(media.discover(store.folder))
    before = {v["id"]: dict(v) for v in store.db.execute("SELECT * FROM videos")}
    locations = {r["path"]: dict(r) for r in store.db.execute("SELECT * FROM file_locations")}
    records, errors = [], []
    for path in paths:
        stat = path.stat()
        quick = media.content_hash(path)
        fp = media.fingerprint(path)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"File changed during refresh: {path.name}. Refresh again.")
        records.append(
            {
                "path": str(path),
                "hash": quick,
                "fingerprint": fp,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "device": stat.st_dev,
                "inode": stat.st_ino,
            }
        )
    # Add identity information to a legacy index without touching evidence/stages.
    by_path = {r["path"]: r for r in records}
    for asset in before.values():
        record = by_path.get(asset["path"])
        if not asset["content_hash"] and record and asset["fingerprint"] == record["fingerprint"]:
            asset["content_hash"] = record["hash"]
            locations.setdefault(asset["path"], {**record, "video_id": asset["id"]})

    candidates = defaultdict(list)
    for asset in before.values():
        if asset["content_hash"]:
            candidates[asset["content_hash"]].append(asset)
    # Prefer an already indexed representative when merging pre-existing copies.
    for assets in candidates.values():
        assets.sort(
            key=lambda a: (
                -store.db.execute(
                    "SELECT count(*) FROM stages WHERE video_id=? AND status='complete'", (a["id"],)
                ).fetchone()[0],
                a["id"],
            )
        )
    full_hashes = {}

    def full(path):
        if path not in full_hashes:
            full_hashes[path] = media.content_hash(Path(path), full=True)
        return full_hashes[path]

    def matches(asset, record):
        old_locations = [r for r in locations.values() if r["video_id"] == asset["id"]]
        for old in old_locations:
            if (old["device"], old["inode"]) == (record["device"], record["inode"]):
                return True  # same source, possibly renamed or touched
        # Hash only candidate copies in full. A sampled collision cannot merge
        # different files, even if the first/middle/last megabytes are identical.
        expected = asset["full_hash"]
        if not expected:
            representative = next(
                (
                    r["path"]
                    for r in old_locations
                    if r["path"] in by_path and by_path[r["path"]]["hash"] == record["hash"]
                ),
                None,
            )
            if representative:
                expected = asset["full_hash"] = full(representative)
        return bool(expected and expected == full(record["path"]))

    assigned, groups, added, changed = {}, defaultdict(list), 0, 0
    with store.db:
        # Free UNIQUE paths so swaps and renames can be applied atomically.
        for video_id in before:
            store.db.execute("UPDATE videos SET path=? WHERE id=?", (f"@sync:{video_id}", video_id))
        for record in records:
            asset = next((a for a in candidates[record["hash"]] if matches(a, record)), None)
            if asset is None:
                previous = locations.get(record["path"])
                # Reuse the ID of a changed single-location source, unless it has
                # already been assigned to surviving content at another path.
                old_id = previous["video_id"] if previous else None
                old_paths = [r for r in locations.values() if r["video_id"] == old_id]
                reusable = (
                    old_id in before
                    and old_id not in assigned
                    and len(old_paths) == 1
                    and before[old_id]["content_hash"] not in {r["hash"] for r in records}
                )
                if reusable:
                    video_id = old_id
                    for table in ("items", "stages", "work_cache"):
                        store.db.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,))
                    changed += 1
                else:
                    video_id = store.next_video_id()
                    store.db.execute(
                        "INSERT INTO videos(id,path,fingerprint,metadata,updated_at) VALUES(?,?,?,'{}',?)",
                        (video_id, record["path"], record["fingerprint"], time.time()),
                    )
                    added += 1
                asset = {
                    "id": video_id,
                    "path": record["path"],
                    "fingerprint": record["fingerprint"],
                    "metadata": "{}",
                    "content_hash": record["hash"],
                    "full_hash": None,
                    "error": None,
                }
                candidates[record["hash"]].append(asset)
                locations[record["path"]] = {**record, "video_id": video_id}
            assigned[asset["id"]] = asset
            groups[asset["id"]].append(record)
        store.db.execute("DELETE FROM file_locations")
        renamed = 0
        for record in records:
            current = Path(record["path"]).stat()
            if (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino) != (
                record["size"],
                record["mtime_ns"],
                record["device"],
                record["inode"],
            ):
                raise RuntimeError(
                    f"File changed during refresh: {Path(record['path']).name}. Refresh again."
                )
        for video_id, asset in assigned.items():
            copies = groups[video_id]
            chosen = next((r for r in copies if r["path"] == asset["path"]), copies[0])
            if video_id in before and before[video_id]["path"] != chosen["path"]:
                renamed += 1
            metadata, error = asset["metadata"], asset.get("error")
            if metadata == "{}" or error:
                try:
                    metadata, error = json.dumps(media.metadata(Path(chosen["path"]))), None
                except Exception as exc:
                    error = str(exc)
                    errors.append({"video_id": video_id, "stage": "metadata", "error": error})
            store.db.execute(
                "UPDATE videos SET path=?,fingerprint=?,metadata=?,content_hash=?,full_hash=?,"
                "present=1,error=?,updated_at=? WHERE id=?",
                (
                    chosen["path"],
                    chosen["fingerprint"],
                    metadata,
                    asset["content_hash"],
                    asset["full_hash"],
                    error,
                    time.time(),
                    video_id,
                ),
            )
            store.db.executemany(
                "INSERT INTO file_locations VALUES(?,?,?,?,?,?)",
                [(r["path"], video_id, r["size"], r["mtime_ns"], r["device"], r["inode"]) for r in copies],
            )
        removed = set(before) - set(assigned)
        store.db.executemany("DELETE FROM videos WHERE id=?", [(i,) for i in removed])
    return {
        "files": len(paths),
        "unique_videos": len(assigned),
        "added": added,
        "changed": changed,
        "removed": len(removed),
        "renamed": renamed,
        "duplicates": len(paths) - len(assigned),
        "errors": errors,
    }
