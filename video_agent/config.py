from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .speech_options import PARAKEET_V3, WHISPER_LANGUAGES


def _valid_setting_type(default, value):
    # bool is an int subclass; accepting it here makes TOML counts/FPS silently
    # turn into 0/1 while the equivalent JSON settings correctly reject it.
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


@dataclass
class VisualConfig:
    backend: str = "mlx"
    model: str = "mlx-community/Qwen3-VL-2B-Instruct-4bit"
    window_s: float = 10.0
    overlap_s: float = 2.0
    frames_per_window: int = 6
    max_side: int = 448
    max_tokens: int = 384
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "VIDEO_AGENT_API_KEY"


@dataclass
class SpeechConfig:
    backend: str = "parakeet"
    model: str = PARAKEET_V3
    language: str = "auto"
    chunk_s: float = 120.0
    device: str = "cuda"
    compute_type: str = "float16"


@dataclass
class OCRConfig:
    backend: str = "vision"
    sample_fps: float = 1.0
    max_side: int = 1280
    min_confidence: float = 0.4


@dataclass
class EmbeddingConfig:
    backend: str = "mlx"
    model: str = "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ"
    batch_size: int = 8
    device: str = "cuda"


@dataclass
class HierarchyConfig:
    enabled: bool = True
    group_size: int = 8


@dataclass
class AnswerConfig:
    backend: str = "mlx-text"
    model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    max_tokens: int = 768
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "VIDEO_AGENT_API_KEY"


@dataclass
class ProxyConfig:
    resolution: str = "original"
    max_fps: int = 0
    cache_mb: int = 512

    @property
    def enabled(self):
        return self.resolution != "original" or self.max_fps != 0


@dataclass
class RuntimeConfig:
    cache_dir: str = ".cache/models"


@dataclass
class Config:
    visual: VisualConfig = field(default_factory=VisualConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    hierarchy: HierarchyConfig = field(default_factory=HierarchyConfig)
    answer: AnswerConfig = field(default_factory=AnswerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    def validate(self):
        for section in fields(self):
            settings = getattr(self, section.name)
            for setting in fields(settings):
                if not _valid_setting_type(setting.default, getattr(settings, setting.name)):
                    raise ValueError(f"Invalid type: {section.name}.{setting.name}")
        choices = {
            "visual": {"mlx", "openai", "none"},
            "speech": {"parakeet", "faster-whisper", "none"},
            "ocr": {"vision", "easyocr", "none"},
            "embedding": {"mlx", "sentence-transformers", "none"},
            "answer": {"mlx-text", "mlx", "openai", "none"},
        }
        for section, allowed in choices.items():
            if getattr(self, section).backend not in allowed:
                raise ValueError(f"{section}.backend must be one of {sorted(allowed)}")
        if not isinstance(self.proxy.resolution, str) or self.proxy.resolution not in {
            "original",
            "1080p",
            "720p",
        }:
            raise ValueError("proxy.resolution must be original, 1080p or 720p")
        if type(self.proxy.max_fps) is not int or self.proxy.max_fps not in {0, 30, 15, 5}:
            raise ValueError("proxy.max_fps must be 0 (original), 30, 15 or 5")
        if type(self.proxy.cache_mb) is not int or self.proxy.cache_mb < 16:
            raise ValueError("proxy.cache_mb must be an integer of at least 16 MB")
        if not isinstance(self.speech.language, str) or self.speech.language not in {
            "auto",
            *WHISPER_LANGUAGES,
        }:
            raise ValueError("speech.language must be 'auto' or a supported language code")
        if self.speech.backend == "parakeet" and self.speech.language != "auto":
            raise ValueError("Parakeet determines the language automatically; use speech.language = 'auto'")
        if (
            self.speech.backend == "faster-whisper"
            and self.speech.model.endswith(".en")
            and self.speech.language not in {"auto", "en"}
        ):
            raise ValueError("This Whisper model is English-only; choose a multilingual model")
        for name, value in (
            ("window_s", self.visual.window_s),
            ("sample_fps", self.ocr.sample_fps),
            ("chunk_s", self.speech.chunk_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.visual.overlap_s < self.visual.window_s:
            raise ValueError("Require 0 <= overlap_s < window_s")
        for value in (
            self.visual.frames_per_window,
            self.visual.max_side,
            self.visual.max_tokens,
            self.ocr.max_side,
            self.embedding.batch_size,
            self.answer.max_tokens,
        ):
            if not isinstance(value, int) or value < 1:
                raise ValueError(
                    "Frame counts, sizes, token limits and batch sizes must be positive integers"
                )
        if not isinstance(self.hierarchy.group_size, int) or self.hierarchy.group_size < 2:
            raise ValueError("hierarchy.group_size must be >= 2")
        if not 0 <= self.ocr.min_confidence <= 1:
            raise ValueError("ocr.min_confidence must be in [0, 1]")
        if self.visual.backend == "none" and self.hierarchy.enabled:
            raise ValueError("Disable hierarchy when visual.backend = 'none'")

    def configure_cache(self):
        cache = Path(self.runtime.cache_dir).expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(cache))
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def load_config(path: str | Path | None = None, profile: str | None = None) -> Config:
    config = Config()
    if profile == "cuda":
        config.visual.backend = "openai"
        config.visual.model = "Qwen/Qwen3-VL-8B-Instruct"
        config.answer.backend = "openai"
        config.answer.model = "Qwen/Qwen3-VL-8B-Instruct"
        config.speech.backend = "faster-whisper"
        config.speech.model = "large-v3-turbo"
        config.ocr.backend = "easyocr"
        config.embedding.backend = "sentence-transformers"
        config.embedding.model = "Qwen/Qwen3-Embedding-0.6B"
    elif profile == "quality":
        config.visual.model = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
    elif profile not in (None, "mac", "fast"):
        raise ValueError(f"Unknown profile: {profile}")
    if path is not None:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
        allowed_sections = {f.name for f in fields(config)}
        if set(raw) - allowed_sections:
            raise ValueError(f"Unknown config sections: {sorted(set(raw) - allowed_sections)}")
        for section, settings in raw.items():
            target = getattr(config, section)
            if not isinstance(settings, dict):
                raise ValueError(f"Config section [{section}] must be a table")
            allowed = {f.name for f in fields(target)}
            if set(settings) - allowed:
                raise ValueError(f"Unknown settings in [{section}]: {sorted(set(settings) - allowed)}")
            # Saved Whisper configs predating language selection always used English.
            if (
                section == "speech"
                and settings.get("backend") == "faster-whisper"
                and "language" not in settings
            ):
                target.language = "en"
            for name, value in settings.items():
                setattr(target, name, value)
    # Validate types before using path-like fields in filesystem operations.
    config.validate()
    base = Path(path).resolve().parent if path else Path(__file__).resolve().parent.parent
    config.runtime.cache_dir = str((base / config.runtime.cache_dir).expanduser().resolve())
    return config


def signature(*values) -> str:
    def convert(value):
        return asdict(value) if hasattr(value, "__dataclass_fields__") else value

    return hashlib.sha256(json.dumps([convert(v) for v in values], sort_keys=True).encode()).hexdigest()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def parse_config(raw):
    """Validate a saved config, including legacy language defaults and JSON numbers."""
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    config = load_config()
    for section, values in raw.items():
        if section not in {f.name for f in fields(config)} or not isinstance(values, dict):
            raise ValueError(f"Unknown config section: {section}")
        target = getattr(config, section)
        if section == "speech" and values.get("backend") == "faster-whisper" and "language" not in values:
            target.language = "en"
        allowed = {f.name for f in fields(target)}
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Unknown setting: {section}.{key}")
            current = getattr(target, key)
            valid = _valid_setting_type(current, value)
            if not valid:
                raise ValueError(f"Invalid type: {section}.{key}")
            if isinstance(current, float):
                value = float(value)
            setattr(target, key, value)
    config.validate()
    return config
