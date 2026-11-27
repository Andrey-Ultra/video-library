from __future__ import annotations

import base64
import os

from .common import load_local_model, release_mlx


class MLXGenerator:
    def __init__(self, config):
        self.config = config
        self.model = self.processor = None

    def generate(self, prompt, images=None, max_tokens=384, schema=None):
        from mlx_vlm import generate, load
        from mlx_vlm.prompt_utils import apply_chat_template

        if self.model is None:
            self.model, self.processor = load_local_model(load, self.config.model)
        image_paths = [str(p) for p in images or []]
        formatted = apply_chat_template(
            self.processor, self.model.config, prompt, num_images=len(image_paths)
        )
        kwargs = {}
        if schema is not None:
            from mlx_vlm.structured import build_json_schema_logits_processor

            tokenizer = getattr(self.processor, "tokenizer", self.processor)
            kwargs["logits_processors"] = [build_json_schema_logits_processor(tokenizer, schema)]
        result = generate(
            self.model,
            self.processor,
            formatted,
            image=image_paths or None,
            max_tokens=max_tokens,
            temperature=0.0,
            repetition_penalty=1.05,
            verbose=False,
            **kwargs,
        )
        return result.text if hasattr(result, "text") else str(result)

    def close(self):
        if self.model is not None:
            self.model = self.processor = None
            release_mlx()


class OpenAIGenerator:
    """A user-configured local vLLM/compatible endpoint; never an implicit cloud upload."""

    def __init__(self, config):
        self.config = config

    def generate(self, prompt, images=None, max_tokens=384, schema=None):
        import httpx

        content = [{"type": "text", "text": prompt}]
        for path in images or []:
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        headers = {}
        if key := os.environ.get(self.config.api_key_env):
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "video_observation", "strict": True, "schema": schema},
            }
        response = httpx.post(
            self.config.base_url.rstrip("/") + "/chat/completions", headers=headers, json=body, timeout=300
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def close(self):
        pass


class MLXTextGenerator:
    """A text-only instruction model for reasoning over the retrieved index."""

    def __init__(self, config):
        self.config = config
        self.model = self.tokenizer = None

    def generate(self, prompt, images=None, max_tokens=768, schema=None):
        if images:
            raise ValueError("mlx-text accepts text evidence only, not images")
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        if self.model is None:
            self.model, self.tokenizer = load_local_model(load, self.config.model)
        formatted = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        processors = make_logits_processors(repetition_penalty=1.1, repetition_context_size=128)
        if schema is not None:
            from mlx_vlm.structured import build_json_schema_logits_processor

            tokenizer = getattr(self.tokenizer, "_tokenizer", self.tokenizer)
            processors.append(build_json_schema_logits_processor(tokenizer, schema))
        return generate(
            self.model,
            self.tokenizer,
            prompt=formatted,
            max_tokens=max_tokens,
            sampler=make_sampler(temp=0.3, top_p=0.8),
            logits_processors=processors,
            prefill_step_size=512,
            verbose=False,
        )

    def close(self):
        if self.model is not None:
            self.model = self.tokenizer = None
            release_mlx()
