from __future__ import annotations

import gc
import shutil
from pathlib import Path

from ..speech_options import PARAKEET_V2, PARAKEET_V3
from .common import ModelSetupError, load_local_model, release_mlx


def check_parakeet_download_space(model):
    if model != PARAKEET_V3:
        return
    from huggingface_hub import try_to_load_from_cache
    from huggingface_hub.constants import HF_HUB_CACHE

    cached = try_to_load_from_cache(model, "model.safetensors")
    if isinstance(cached, str) and Path(cached).is_file():
        return
    directory = Path(HF_HUB_CACHE).expanduser()
    while not directory.exists():
        directory = directory.parent
    free = shutil.disk_usage(directory).free
    if free < 3 * 1024**3:
        raise ModelSetupError(
            f"Parakeet v3 needs about 3 GB of free disk space to download its model; "
            f"{free / 1024**3:.1f} GB is available. Free disk space, then add this library "
            "to the queue again. Existing indexing checkpoints are saved."
        )


class ParakeetTranscriber:
    def __init__(self, config):
        self.config = config
        self.model = None

    def transcribe(self, audio):
        if self.model is None:
            check_parakeet_download_space(self.config.model)
            from parakeet_mlx import from_pretrained

            self.model = load_local_model(from_pretrained, self.config.model)
        result = self.model.transcribe(str(audio))
        return [
            {
                "start_s": s.start,
                "end_s": s.end,
                "text": s.text,
                # Parakeet v3 does not return language IDs. Do not invent one.
                "language": "en" if self.config.model == PARAKEET_V2 else None,
                "words": [{"start_s": w.start, "end_s": w.end, "text": w.text} for w in s.tokens],
            }
            for s in result.sentences
        ]

    def close(self):
        if self.model is not None:
            self.model = None
            release_mlx()


class WhisperTranscriber:
    def __init__(self, config):
        self.config = config
        self.model = None

    def transcribe(self, audio):
        from faster_whisper import WhisperModel

        if self.model is None:
            self.model = WhisperModel(
                self.config.model, device=self.config.device, compute_type=self.config.compute_type
            )
        if not self.model.model.is_multilingual and self.config.language not in {"auto", "en"}:
            raise ValueError("This Whisper model is English-only; choose a multilingual model")
        automatic = self.config.language == "auto"
        segments, info = self.model.transcribe(
            str(audio),
            language=None if automatic else self.config.language,
            task="transcribe",
            multilingual=automatic and self.model.model.is_multilingual,
            word_timestamps=True,
            vad_filter=True,
        )
        return [
            {
                "start_s": s.start,
                "end_s": s.end,
                "text": s.text.strip(),
                # Auto can change language between decoding windows. info.language
                # describes the initial detection, not every sentence in a mixed clip.
                "language": None if automatic else self.config.language,
                "chunk_language": info.language,
                "words": [{"start_s": w.start, "end_s": w.end, "text": w.word} for w in s.words or []],
            }
            for s in segments
        ]

    def close(self):
        self.model = None
        gc.collect()
