"""Backend registry; implementations are grouped by responsibility.

Importing this package never loads model weights or initializes Metal.
"""

from __future__ import annotations

import importlib.util

from ..config import Config
from .common import json_object, normalized
from .contracts import Embedder, Generator, TextRecognizer, Transcriber
from .embeddings import QUERY_INSTRUCTION, MLXEmbedder, SentenceEmbedder
from .generation import MLXGenerator, MLXTextGenerator, OpenAIGenerator
from .speech import ParakeetTranscriber, WhisperTranscriber
from .text_recognition import EasyOCR, IsolatedVisionOCR, VisionOCR

__all__ = [
    "Generator",
    "Transcriber",
    "TextRecognizer",
    "Embedder",
    "json_object",
    "normalized",
    "MLXGenerator",
    "MLXTextGenerator",
    "OpenAIGenerator",
    "ParakeetTranscriber",
    "WhisperTranscriber",
    "VisionOCR",
    "IsolatedVisionOCR",
    "EasyOCR",
    "MLXEmbedder",
    "SentenceEmbedder",
    "QUERY_INSTRUCTION",
    "generator",
    "answer_generator",
    "transcriber",
    "ocr",
    "embedder",
    "missing_dependencies",
]


def generator(config: Config):
    factories = {"mlx": MLXGenerator, "openai": OpenAIGenerator}
    if config.visual.backend not in factories:
        raise ValueError("Visual backend is disabled; cannot describe videos or answer questions locally")
    return factories[config.visual.backend](config.visual)


def answer_generator(config: Config):
    factories = {"mlx-text": MLXTextGenerator, "mlx": MLXGenerator, "openai": OpenAIGenerator}
    if config.answer.backend not in factories:
        raise ValueError("Local answering is disabled; use search or connect an agent over MCP")
    return factories[config.answer.backend](config.answer)


def transcriber(config: Config):
    return {"parakeet": ParakeetTranscriber, "faster-whisper": WhisperTranscriber}[config.speech.backend](
        config.speech
    )


def ocr(config: Config):
    return {"vision": IsolatedVisionOCR, "easyocr": EasyOCR}[config.ocr.backend](config.ocr)


def embedder(config: Config):
    return {"mlx": MLXEmbedder, "sentence-transformers": SentenceEmbedder}[config.embedding.backend](
        config.embedding
    )


def missing_dependencies(config: Config) -> list[str]:
    modules = ["av", "PIL", "numpy", "mcp"]
    if config.visual.backend == "mlx":
        modules.append("mlx_vlm")
    if config.answer.backend == "mlx-text":
        modules.append("mlx_lm")
    if config.speech.backend == "parakeet":
        modules.append("parakeet_mlx")
    elif config.speech.backend == "faster-whisper":
        modules.append("faster_whisper")
    if config.ocr.backend == "vision":
        modules.extend(["Vision", "objc"])
    elif config.ocr.backend == "easyocr":
        modules.append("easyocr")
    if config.embedding.backend == "mlx":
        modules.append("mlx_embeddings")
    elif config.embedding.backend == "sentence-transformers":
        modules.append("sentence_transformers")
    return [m for m in modules if importlib.util.find_spec(m) is None]
