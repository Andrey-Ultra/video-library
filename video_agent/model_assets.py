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
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from .config import load_config, parse_config

CATALOG = [
    {
        "id": "mlx-community/Qwen3-VL-2B-Instruct-4bit",
        "section": "visual",
        "name": "Qwen3 VL 2B · Fast",
        "detail": "Smaller local model. A good starting point for 8–24 GB Macs.",
    },
    {
        "id": "mlx-community/Qwen3-VL-4B-Instruct-4bit",
        "section": "visual",
        "name": "Qwen3 VL 4B · More detail",
        "detail": "Larger local model. More memory and processing time; suggested for 32 GB or more.",
    },
    {
        "id": "mlx-community/parakeet-tdt-0.6b-v3",
        "section": "speech",
        "name": "Parakeet v3 · Multilingual",
        "detail": "Automatic recognition of 25 languages, including English and Russian.",
    },
    {
        "id": "mlx-community/parakeet-tdt-0.6b-v2",
        "section": "speech",
        "name": "Parakeet v2 · English",
        "detail": "For English-only recordings. Choose v3 for Russian.",
    },
    {
        "id": "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ",
        "section": "embedding",
        "name": "Qwen3 Embedding · Multilingual",
        "detail": "Enables searching by meaning instead of exact words.",
    },
]
IDS = {entry["id"] for entry in CATALOG}


def hub_directory(config):
    return (
        Path(
            os.environ.get("HF_HUB_CACHE")
            or Path(os.environ.get("HF_HOME") or config.runtime.cache_dir) / "hub"
        )
        .expanduser()
        .resolve()
    )


def mac_hardware():
    def sysctl(name, fallback):
        try:
            return subprocess.check_output(["/usr/sbin/sysctl", "-n", name], text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            return fallback

    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    return {
        "supported": apple,
        "chip": sysctl("machdep.cpu.brand_string", platform.machine()) if apple else platform.machine(),
        "memory_gb": round(int(sysctl("hw.memsize", "0")) / 1024**3) if apple else 0,
        "cpu_cores": os.cpu_count() or 1,
    }


def recommended_config(hardware):
    cfg = load_config(profile="mac")
    memory = hardware["memory_gb"]
    if memory >= 32:
        cfg.visual.model = CATALOG[1]["id"]
        cfg.visual.frames_per_window, cfg.visual.max_side = 8, 672
        cfg.embedding.batch_size = 16
    elif memory and memory <= 8:
        cfg.visual.frames_per_window, cfg.visual.max_side = 4, 384
        cfg.embedding.batch_size = 4
    return asdict(cfg)


def snapshot_path(hub, repo):
    root = Path(hub) / ("models--" + repo.replace("/", "--"))
    ref = root / "refs/main"
    if not ref.is_file():
        return None
    revision = ref.read_text().strip()
    if not revision or Path(revision).name != revision:
        return None
    return root / "snapshots" / revision


def validate_snapshot(path, section):
    if path is None or not path.is_dir():
        raise FileNotFoundError("Model is not downloaded")
    required = ["config.json"]
    if section in {"visual", "embedding"}:
        required += ["tokenizer.json", "tokenizer_config.json"]
    if section == "visual":
        required += ["preprocessor_config.json"]
    for name in required:
        if not (path / name).is_file():
            raise ValueError(f"Model is incomplete: missing {name}")
    for file in path.glob("*.json"):
        if not file.name.startswith("."):
            json.loads(file.read_text())
    index = path / "model.safetensors.index.json"
    # MLX loaders prefer consolidated weights. Some official converted repos
    # retain a stale shard index alongside their complete model.safetensors.
    weights = (
        ["model.safetensors"]
        if (path / "model.safetensors").is_file()
        else sorted(set(json.loads(index.read_text())["weight_map"].values()))
        if index.is_file()
        else ["model.safetensors"]
    )
    if not weights:
        raise ValueError("Model has no weight files")
    for name in weights:
        if Path(name).name != name:
            raise ValueError("Invalid model weight filename")
        file = path / name
        if not file.is_file():
            raise ValueError(f"Model is incomplete: missing {name}")
        # Check the safetensors header and data length without loading any tensors.
        with file.open("rb") as stream:
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError(f"Incomplete weights: {name}")
            size = struct.unpack("<Q", raw)[0]
            if not 2 <= size <= min(100_000_000, file.stat().st_size - 8):
                raise ValueError(f"Invalid weights: {name}")
            header = json.loads(stream.read(size))
        tensors = [v for k, v in header.items() if k != "__metadata__"]
        if not tensors or max(v["data_offsets"][1] for v in tensors) != file.stat().st_size - 8 - size:
            raise ValueError(f"Truncated weights: {name}")


def required_models(config):
    result = []
    for section, backends in (("visual", {"mlx"}), ("speech", {"parakeet"}), ("embedding", {"mlx"})):
        settings = getattr(config, section)
        if settings.backend in backends and settings.model in IDS:
            result.append(settings.model)
    return result


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
        previous = json.loads(state.read_text()) if state.is_file() else {}
        if lock.is_file():
            with lock.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {**entry, **previous, "status": previous.get("status", "downloading")}
        if previous.get("status") == "failed" and previous.get("repair"):
            return {**entry, **previous}
        if previous.get("status") in {"downloading", "checking"}:
            return {**entry, "status": "failed", "error": "Download was interrupted. Retry to continue."}
        path = snapshot_path(hub, entry["id"])
        try:
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
            return {**entry, "status": "failed" if path else "missing", "error": str(exc) if path else None}

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
        if repo not in IDS:
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
            previous = json.loads(state.read_text()) if state.is_file() else {}
            force = previous.get("repair", False) or (
                current["status"] == "failed" and snapshot_path(hub, repo) is not None
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
        if repo not in IDS:
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
    try:
        download(args.model, Path(args.hub), Path(args.state), args.repair)
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    main()
