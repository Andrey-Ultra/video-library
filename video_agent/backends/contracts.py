from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np


class Generator(Protocol):
    def generate(
        self, prompt: str, images: list[Path] | None = None, max_tokens: int = 384, schema: dict | None = None
    ) -> str: ...
    def close(self) -> None: ...


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> list[dict]: ...
    def close(self) -> None: ...


class TextRecognizer(Protocol):
    def recognize(self, image) -> list[dict]: ...
    def close(self) -> None: ...


class Embedder(Protocol):
    def encode(self, texts: list[str], query: bool = False) -> np.ndarray: ...
    def close(self) -> None: ...
