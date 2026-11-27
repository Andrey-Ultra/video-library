"""Curated model choices and conservative Apple Silicon starting points.

No model/cache/network work belongs in this module.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import asdict

from .config import load_config

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


def mac_hardware():
    def sysctl(name, fallback):
        try:
            return subprocess.check_output(["/usr/sbin/sysctl", "-n", name], text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            return fallback

    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    try:
        memory = max(0, int(sysctl("hw.memsize", "0"))) if apple else 0
    except ValueError:
        memory = 0
    return {
        "supported": apple,
        "chip": sysctl("machdep.cpu.brand_string", platform.machine()) if apple else platform.machine(),
        "memory_gb": round(memory / 1024**3),
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


def required_models(config):
    result = []
    for section, backends in (("visual", {"mlx"}), ("speech", {"parakeet"}), ("embedding", {"mlx"})):
        settings = getattr(config, section)
        if settings.backend in backends and settings.model in IDS:
            result.append(settings.model)
    return result
