from __future__ import annotations

import gc
import json

import numpy as np


def release_mlx():
    gc.collect()
    import mlx.core as mx

    mx.clear_cache()


def json_object(text: str) -> dict:
    """Accept fenced JSON, reject incomplete/truncated model responses."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                result, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict):
                return result
    raise ValueError(f"Model did not return a complete JSON object: {text[:180]!r}")


def normalized(array):
    result = np.asarray(array, dtype=np.float32)
    if result.ndim != 2 or not np.isfinite(result).all():
        raise ValueError("Embedding backend returned invalid vectors")
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("Embedding backend returned a zero vector")
    return result / norms
