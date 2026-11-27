"""Estimate remaining work from checkpoints and measured per-stage throughput."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from statistics import median

from .signatures import stage_signatures
from .store import STAGES

MIN_SAMPLE_UNITS = {"speech": 2, "ocr": 10, "visual": 3, "hierarchy": 2, "embedding": 16}


class ProgressTracker:
    """Recent checkpoint throughput, scoped to one library/job/config, without index writes."""

    def __init__(self):
        self.scope = None
        self.previous = {}
        self.samples = {}
        self.lock = threading.Lock()

    def observe(self, scope, videos, config, counts):
        signatures = stage_signatures(config)
        scope = (scope, tuple(signatures.items()))
        with self.lock:
            if scope != self.scope:
                self.scope, self.previous, self.samples = scope, {}, {}
            for video in videos:
                for stage in video["stages"]:
                    name = stage["name"]
                    if (
                        name not in signatures
                        or stage.get("signature") != signatures[name]
                        or name in video.get("outdated", [])
                        or stage["status"] not in {"running", "complete"}
                    ):
                        continue
                    _, done = units(video, name, config, *counts.get(video["id"], (0, 0)))
                    elapsed = stage.get("elapsed_s", 0)
                    key = (video["id"], name)
                    previous = self.previous.get(key)
                    if (
                        previous is not None
                        and previous[0] > 0
                        and done > previous[0]
                        and elapsed > previous[1]
                    ):
                        samples = self.samples.setdefault(name, deque(maxlen=12))
                        samples.append((done - previous[0], elapsed - previous[1]))
                    # Do not count startup before the first saved checkpoint as steady throughput.
                    if previous is None or done != previous[0] or elapsed < previous[1]:
                        self.previous[key] = (done, elapsed)
            rates, unstable = {}, []
            for name, samples in self.samples.items():
                if len(samples) < 3 or sum(n for n, _ in samples) < MIN_SAMPLE_UNITS[name]:
                    continue
                values = sorted(seconds / n for n, seconds in samples)
                rates[name] = median(values)
                if values[len(values) * 3 // 4] > values[len(values) // 4] * 4:
                    unstable.append(name)
            return rates, unstable


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


def estimate(videos, config, counts, historical_rates=None, recent_rates=None, unstable_stages=()):
    rates, remaining, measured, completed_weight = {}, {}, 0.0, 0.0
    pending, unknown = False, []
    signatures = stage_signatures(config)
    for name in STAGES:
        completed_rates, partial_rates = [], []
        todo = total_units = done_units = 0.0
        for video in videos:
            stage = next((s for s in video["stages"] if s["name"] == name), {})
            total, done = units(video, name, config, *counts.get(video["id"], (0, 0)))
            compatible = stage.get("signature") == signatures[name] and name not in video.get("outdated", [])
            if not compatible:
                done = 0
            if compatible and done and stage.get("elapsed_s", 0) > 0:
                sample = stage["elapsed_s"] / done
                if stage.get("status") == "complete":
                    completed_rates.append(sample)
                elif stage.get("status") != "failed" and done >= MIN_SAMPLE_UNITS[name]:
                    partial_rates.append(sample)
            todo += max(0, total - done)
            total_units += total
            done_units += done
        rate = (historical_rates or {}).get(name)
        # A single cold start/retry is not representative of every remaining file.
        if completed_rates and (rate is None or len(completed_rates) >= 2):
            rate = median(completed_rates)
        if rate is None and partial_rates:
            rate = median(partial_rates)
        if name in (recent_rates or {}):
            rate = recent_rates[name]
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
    unstable = [name for name in unstable_stages if remaining.get(name)]
    eta = None if unknown or unstable else sum(v or 0 for v in remaining.values())
    current_stage = next(
        (stage["name"] for video in videos for stage in video["stages"] if stage["status"] == "running"), None
    )
    stage_eta = remaining.get(current_stage) if current_stage not in unstable else None
    return {
        "stage_eta_s": stage_eta,
        "stage_eta_name": current_stage,
        "stage_eta_status": "recalibrating"
        if current_stage in unstable
        else "calibrating"
        if stage_eta is None
        else "estimated",
        "fraction": completed_weight / measured if measured else 0,
        "eta_s": eta if pending else 0,
        "eta_status": "recalibrating" if unstable else "calibrating" if unknown else "estimated",
        "unmeasured_stages": unknown,
        "remaining_by_stage_s": remaining,
        "approximate": True,
        "rates": rates,
        "calculated_at": time.time(),
    }
