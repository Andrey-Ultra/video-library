from __future__ import annotations

import gc

from .common import release_mlx


class ParakeetTranscriber:
    def __init__(self, config):
        self.config = config
        self.model = None

    def transcribe(self, audio):
        from parakeet_mlx import from_pretrained

        if self.model is None:
            self.model = from_pretrained(self.config.model)
        result = self.model.transcribe(str(audio))
        return [
            {
                "start_s": s.start,
                "end_s": s.end,
                "text": s.text,
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
        segments, _ = self.model.transcribe(str(audio), language="en", word_timestamps=True, vad_filter=True)
        return [
            {
                "start_s": s.start,
                "end_s": s.end,
                "text": s.text.strip(),
                "words": [{"start_s": w.start, "end_s": w.end, "text": w.word} for w in s.words or []],
            }
            for s in segments
        ]

    def close(self):
        self.model = None
        gc.collect()
