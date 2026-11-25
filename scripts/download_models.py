"""Download only the models selected by the same config as the indexer."""

import argparse

from video_agent.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--profile", choices=["mac", "fast", "quality", "cuda"], default="mac")
args = parser.parse_args()
config = load_config(args.config, args.profile)
config.configure_cache()
from huggingface_hub import snapshot_download  # noqa: E402

for section in (config.visual, config.speech, config.embedding, config.answer):
    if section.backend in {"mlx", "mlx-text", "parakeet"}:
        print(f"Downloading {section.model}", flush=True)
        snapshot_download(section.model)
