"""Shared fingerprints for indexing and retrieval compatibility."""

from __future__ import annotations

import importlib.metadata

from . import backends, prompts
from .config import Config, signature


def backend_version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def stage_signatures(config: Config):
    visual = signature(
        "visual-v1",
        config.visual,
        prompts.PROMPT_VERSION,
        backend_version("mlx-vlm" if config.visual.backend == "mlx" else "httpx"),
    )
    speech = signature(
        "speech-v1",
        config.speech,
        backend_version("parakeet-mlx" if config.speech.backend == "parakeet" else "faster-whisper"),
    )
    ocr = signature(
        "ocr-v1",
        config.ocr,
        backend_version("pyobjc-framework-Vision" if config.ocr.backend == "vision" else "easyocr"),
    )
    hierarchy = signature("hierarchy-v2", config.hierarchy, visual)
    embedding = embedding_namespace(config)
    return {"visual": visual, "speech": speech, "ocr": ocr, "hierarchy": hierarchy, "embedding": embedding}


def embedding_namespace(config):
    return signature(
        "embedding-v1",
        config.embedding,
        backends.QUERY_INSTRUCTION,
        backend_version("mlx-embeddings" if config.embedding.backend == "mlx" else "sentence-transformers"),
    )
