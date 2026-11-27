from __future__ import annotations

import gc

import numpy as np

from .common import load_local_model, normalized, release_mlx

QUERY_INSTRUCTION = "Retrieve video observations, events, speech or on-screen text relevant to this query."


class MLXEmbedder:
    def __init__(self, config):
        self.config = config
        self.model = self.tokenizer = None

    def encode(self, texts, query=False):
        import mlx.core as mx
        from mlx_embeddings.utils import load

        if self.model is None:
            self.model, self.tokenizer = load_local_model(load, self.config.model)
        if query:
            texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {t}" for t in texts]
        inputs = self.tokenizer(texts, padding=True, truncation=True, max_length=1024, return_tensors="mlx")
        output = self.model(inputs["input_ids"], attention_mask=inputs["attention_mask"])
        # NumPy cannot consume an MLX bfloat16 buffer directly.
        return normalized(np.asarray(output.text_embeds.astype(mx.float32)))

    def close(self):
        if self.model is not None:
            self.model = self.tokenizer = None
            release_mlx()


class SentenceEmbedder:
    def __init__(self, config):
        self.config = config
        self.model = None

    def encode(self, texts, query=False):
        from sentence_transformers import SentenceTransformer

        if self.model is None:
            self.model = SentenceTransformer(self.config.model, device=self.config.device)
            self.model.max_seq_length = 1024
        if query:
            texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {t}" for t in texts]
        return normalized(self.model.encode(texts, normalize_embeddings=True))

    def close(self):
        self.model = None
        gc.collect()
