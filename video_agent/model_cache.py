"""Read-only cache layout and weight structure validation, without loading tensors."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path


def hub_directory(config):
    return (
        Path(
            os.environ.get("HF_HUB_CACHE")
            or Path(os.environ.get("HF_HOME") or config.runtime.cache_dir) / "hub"
        )
        .expanduser()
        .resolve()
    )


def snapshot_path(hub, repo):
    root = Path(hub) / ("models--" + repo.replace("/", "--"))
    ref = root / "refs/main"
    if not ref.is_file():
        return None
    revision = ref.read_text().strip()
    if not revision or revision in {".", ".."} or Path(revision).name != revision:
        raise ValueError("Invalid cached model revision")
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
    if (path / "model.safetensors").is_file() or not index.is_file():
        weights = ["model.safetensors"]
    else:
        manifest = json.loads(index.read_text())
        mapping = manifest.get("weight_map") if isinstance(manifest, dict) else None
        if not isinstance(mapping, dict) or any(not isinstance(name, str) for name in mapping.values()):
            raise ValueError("Invalid model weight index")
        weights = sorted(set(mapping.values()))
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
        if not isinstance(header, dict):
            raise ValueError(f"Invalid weight header: {name}")
        intervals = []
        for tensor, info in header.items():
            if tensor == "__metadata__":
                continue
            offsets = info.get("data_offsets") if isinstance(info, dict) else None
            if (
                not isinstance(offsets, list)
                or len(offsets) != 2
                or any(type(v) is not int for v in offsets)
                or not 0 <= offsets[0] <= offsets[1]
            ):
                raise ValueError(f"Invalid weight offsets: {name}")
            intervals.append(offsets)
        position = 0
        for start, end in sorted(intervals):
            if start != position:
                raise ValueError(f"Overlapping or missing weight data: {name}")
            position = end
        if not intervals or position != file.stat().st_size - 8 - size:
            raise ValueError(f"Truncated weights: {name}")
