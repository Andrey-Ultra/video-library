"""Estimate remaining work from checkpoints and measured per-stage throughput."""

from __future__ import annotations

import math
import time

from .signatures import stage_signatures
from .store import STAGES


def units(video, name, config, item_count=0, vector_count=0):
    duration = video["metadata"].get("duration_ms", 0) / 1000
    stages = {s["name"]: s for s in video["stages"]}
    stage = stages.get(name, {})
    checkpoint = stage.get("checkpoint", 0)
    if name == "visual":
        total = (
            1
            + math.ceil(
                max(0, duration - config.visual.window_s) / (config.visual.window_s - config.visual.overlap_s)
            )
            if duration
            else 0
        )
        done = checkpoint
    elif name == "speech":
        total = math.ceil(duration / config.speech.chunk_s) if video["metadata"].get("has_audio") else 0
        done = checkpoint
    elif name == "ocr":
        total, done = (
            math.ceil(duration * config.ocr.sample_fps),
            math.ceil(checkpoint * config.ocr.sample_fps / 1000),
        )
    elif name == "hierarchy":
        # Event counts are known only after the model groups windows. Use a
        # conservative chronological-batch estimate until actual nodes exist.
        windows = units(video, "visual", config)[0]
        total = math.ceil(windows / config.hierarchy.group_size)
        level = total
        while level > config.hierarchy.group_size:
            level = math.ceil(level / config.hierarchy.group_size)
            total += level
        total += int(windows > 0)
        done = checkpoint
    else:
        total, done = item_count, vector_count
    active = config.hierarchy.enabled if name == "hierarchy" else getattr(config, name).backend != "none"
    if not active:
        return 0, 0
    return total, total if stage.get("status") == "complete" else min(done, total)


def estimate(videos, config, counts, historical_rates=None):
    rates, remaining, measured, completed_weight = {}, {}, 0.0, 0.0
    pending, unknown = False, []
    signatures = stage_signatures(config)
    for name in STAGES:
        observed_time = observed_units = todo = total_units = done_units = 0.0
        for video in videos:
            stage = next((s for s in video["stages"] if s["name"] == name), {})
            total, done = units(video, name, config, *counts.get(video["id"], (0, 0)))
            compatible = stage.get("signature") == signatures[name] and name not in video.get("outdated", [])
            if not compatible:
                done = 0
            if compatible and done and stage.get("elapsed_s", 0) > 0:
                observed_time += stage["elapsed_s"]
                observed_units += done
            todo += max(0, total - done)
            total_units += total
            done_units += done
        rate = observed_time / observed_units if observed_units else (historical_rates or {}).get(name)
        rates[name] = rate
        remaining[name] = round(todo * rate) if rate is not None else None
        if todo:
            pending = True
            if rate is None:
                unknown.append(name)
        # Avoid an almost-finished percentage while expensive unmeasured stages
        # are still pending. Each enabled stage gets equal weight until measured.
        if total_units:
            measured += 1
            completed_weight += done_units / total_units
    eta = None if unknown else sum(v or 0 for v in remaining.values())
    return {
        "fraction": completed_weight / measured if measured else 0,
        "eta_s": eta if pending else 0,
        "unmeasured_stages": unknown,
        "remaining_by_stage_s": remaining,
        "approximate": True,
        "rates": rates,
        "calculated_at": time.time(),
    }
