"""Offline model inventory, Mac recommendations and explicit resumable downloads.

No weights are loaded here. A detached downloader owns a file lock and persists
its state so closing the settings/app cannot turn an interrupted file into Ready.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .config import parse_config
from .model_cache import hub_directory, snapshot_path, validate_snapshot
from .model_catalog import CATALOG, IDS, mac_hardware, recommended_config, required_models


def _read_state(path):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("status") not in {
            "downloading",
            "checking",
            "ready",
            "failed",
        }:
            raise ValueError("Invalid download state")
        for key in ("completed_files", "total_files"):
            if key in value and (type(value[key]) is not int or value[key] < 0):
                raise ValueError("Invalid download progress")
        for key in ("error", "filename"):
            if key in value and value[key] is not None and not isinstance(value[key], str):
                raise ValueError("Invalid download details")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {"status": "failed", "error": "Download state could not be read. Retry the download."}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".download-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class ModelAssets:
    def __init__(self, directory):
        self.directory = Path(directory) / "model-downloads"
        self._checks = {}
        self.hardware = mac_hardware()

    def files(self, hub, repo):
        key = hashlib.sha256(f"{hub}:{repo}".encode()).hexdigest()
        return self.directory / f"{key}.json", self.directory / f"{key}.lock"

    def status(self, config, entry):
        hub = hub_directory(config)
        state, lock = self.files(hub, entry["id"])
        previous = _read_state(state)
        if lock.is_file():
            with lock.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {**entry, **previous, "status": previous.get("status", "downloading")}
        return self._cached_status(hub, entry, previous)

    def _cached_status(self, hub, entry, previous):
        if previous.get("status") == "failed" and previous.get("repair"):
            return {**entry, **previous}
        if previous.get("status") in {"downloading", "checking"}:
            return {**entry, "status": "failed", "error": "Download was interrupted. Retry to continue."}
        path = None
        try:
            path = snapshot_path(hub, entry["id"])
            # Cache header validation only while the snapshot's files stay unchanged.
            stamp = (
                tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(path.iterdir()))
                if path and path.is_dir()
                else ()
            )
            key = (str(path), stamp)
            if key not in self._checks:
                validate_snapshot(path, entry["section"])
                self._checks = {**dict(list(self._checks.items())[-20:]), key: True}
            return {**entry, "status": "ready"}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if previous.get("status") == "failed":
                return {**entry, **previous}
            if previous.get("status") in {"downloading", "checking"}:
                return {**entry, "status": "failed", "error": "Download was interrupted. Retry to continue."}
            missing = isinstance(exc, FileNotFoundError) and path is None
            return {
                **entry,
                "status": "missing" if missing else "failed",
                "error": None if missing else str(exc),
            }

    def resources(self, raw):
        config = parse_config(raw)
        return {
            "hardware": self.hardware,
            "recommended": recommended_config(self.hardware),
            "models": [self.status(config, entry) for entry in CATALOG],
            "required": required_models(config),
        }

    def start(self, raw, repo):
        config = parse_config(raw)
        if not isinstance(repo, str) or repo not in IDS:
            raise ValueError("Choose a model from the local model list")
        hub = hub_directory(config)
        state, lock = self.files(hub, repo)
        entry = next(e for e in CATALOG if e["id"] == repo)
        current = self.status(config, entry)
        if current["status"] == "ready":
            return {"started": False}
        self.directory.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"started": False}
            previous = _read_state(state)
            current = self._cached_status(hub, entry, previous)
            if current["status"] == "ready":
                return {"started": False}
            # Network failures resume partial blobs; only corrupt completed files
            # require force_download. Never discard partial work just for a retry.
            force = previous.get("repair", False) or (
                current["status"] == "failed"
                and not previous
                and (hub / ("models--" + repo.replace("/", "--"))).exists()
            )
            _write(state, {"status": "downloading", "completed_files": 0, "total_files": 0})
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "video_agent.model_assets",
                        "--model",
                        repo,
                        "--hub",
                        str(hub),
                        "--state",
                        str(state),
                        "--lock-fd",
                        str(handle.fileno()),
                        *(["--repair"] if force else []),
                    ],
                    pass_fds=(handle.fileno(),),
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError:
                _write(state, {"status": "failed", "error": "Could not start the downloader. Retry."})
                raise ValueError("Could not start the downloader. Retry.") from None
        return {"started": True}

    def delete(self, raw, repo):
        """Remove only this catalog model's HF repository, including partial blobs."""
        if not isinstance(repo, str) or repo not in IDS:
            raise ValueError("Only downloaded catalog models can be removed here")
        hub = hub_directory(parse_config(raw))
        state, lease = self.files(hub, repo)
        root = hub / ("models--" + repo.replace("/", "--"))
        self.directory.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            lock = stack.enter_context(lease.open("a+b"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("This model is downloading. Wait for the download to finish.") from None
            if root.is_symlink() or root.resolve().parent != hub.resolve():
                raise ValueError("Model cache is a symbolic link; it was not deleted")
            locks = hub / ".locks" / root.name
            if locks.is_symlink():
                raise ValueError("Model download locks use a symbolic link; nothing was deleted")
            for file in sorted(locks.glob("*.lock")):
                if file.is_symlink():
                    raise ValueError("Model download lock is a symbolic link; nothing was deleted")
                handle = stack.enter_context(file.open("a+b"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Another downloader is using this model. Wait and retry.") from None
            if root.exists():
                if not root.is_dir():
                    raise ValueError("Model cache is not a folder; nothing was deleted")
                if any(
                    p.name not in {"blobs", "snapshots", "refs", ".no_exist", ".DS_Store"}
                    and not p.name.startswith("._")
                    for p in root.iterdir()
                ):
                    raise ValueError("Model cache contains unrecognized files; nothing was deleted")
                lsof = shutil.which("lsof") or "/usr/sbin/lsof"
                try:
                    result = subprocess.run(
                        [lsof, "-t", "+D", str(root)], capture_output=True, text=True, timeout=15
                    )
                except (OSError, subprocess.SubprocessError):
                    raise ValueError(
                        "Could not check whether the model is in use; nothing was deleted"
                    ) from None
                if result.returncode == 0 or result.stdout.strip():
                    raise ValueError("This model is open in another process. Close it and retry.")
                if result.returncode != 1:
                    raise ValueError("Could not verify open model files; nothing was deleted")
                shutil.rmtree(root)
            state.unlink(missing_ok=True)
            self._checks.clear()
        return {"deleted": True}


def download(repo, hub, state, repair=False):
    entry = next(e for e in CATALOG if e["id"] == repo)
    checking = False
    try:
        from huggingface_hub import HfApi, hf_hub_download

        info = HfApi().model_info(repo, files_metadata=True)
        files = [f for f in info.siblings if "/" not in f.rfilename and not f.rfilename.startswith(".")]
        if not files or any(f.size is None or f.size < 0 for f in files):
            raise ValueError("Could not determine model download size. Retry later.")
        Path(hub).mkdir(parents=True, exist_ok=True)
        # Count only matching files for this exact revision, never old snapshots.
        root = Path(hub) / ("models--" + repo.replace("/", "--"))
        snapshot = root / "snapshots" / info.sha
        need = 0
        for file in files:
            local = snapshot / file.rfilename
            if not repair and local.is_file() and local.stat().st_size == file.size:
                continue
            partial = root / "blobs" / ((file.lfs.sha256 if file.lfs else "") + ".incomplete")
            have = min(file.size, partial.stat().st_size) if not repair and partial.is_file() else 0
            need += file.size - have
        if shutil.disk_usage(hub).free < need + 256 * 1024**2:
            raise OSError("Not enough free disk space for this model. Free space and retry.")
        path = None
        for i, file in enumerate(files):
            _write(
                state,
                {
                    "status": "downloading",
                    "completed_files": i,
                    "total_files": len(files),
                    "filename": file.rfilename,
                },
            )
            local = hf_hub_download(
                repo, file.rfilename, revision=info.sha, cache_dir=hub, force_download=repair
            )
            path = Path(local).parent
        _write(state, {"status": "checking", "completed_files": len(files), "total_files": len(files)})
        checking = True
        validate_snapshot(path, entry["section"])
        for file in files:
            local = path / file.rfilename
            if file.size is not None and local.stat().st_size != file.size:
                raise ValueError(f"Incomplete download: {file.rfilename}")
            sha = file.lfs.sha256 if file.lfs else None
            if sha:
                digest = hashlib.sha256()
                with local.open("rb") as stream:
                    while block := stream.read(4 * 1024**2):
                        digest.update(block)
                if digest.hexdigest() != sha:
                    raise ValueError(f"Downloaded file failed verification: {file.rfilename}")
        # Pin the verified snapshot as main, including when HF was called with a SHA.
        ref = path.parent.parent / "refs/main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=ref.parent, prefix=".ref-")
        with os.fdopen(fd, "w") as stream:
            stream.write(info.sha)
        os.replace(temp, ref)
        _write(state, {"status": "ready"})
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (OSError, ValueError))
            and not type(exc).__module__.startswith(("http", "huggingface"))
            else f"Download failed ({type(exc).__name__}). Check your connection and retry."
        )
        _write(state, {"status": "failed", "error": message[:500], "repair": checking})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=sorted(IDS), required=True)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    os.set_inheritable(args.lock_fd, False)
    try:
        download(args.model, Path(args.hub), Path(args.state), args.repair)
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    main()
