from copy import deepcopy

import pytest

from video_agent.app_state import AppState
from video_agent.config import load_config
from video_agent.progress import ProgressTracker, estimate
from video_agent.signatures import stage_signatures


@pytest.fixture
def ocr_config():
    cfg = load_config()
    cfg.visual.backend = cfg.speech.backend = cfg.embedding.backend = "none"
    cfg.hierarchy.enabled = False
    return cfg


def video(cfg, identity=1, done=0, elapsed=0, status="running"):
    return {
        "id": identity,
        "metadata": {"duration_ms": 100_000, "has_audio": False},
        "stages": [
            {
                "name": "ocr",
                "status": status,
                "checkpoint": max(0, (done - 1) * 1000 + 1) if done else 0,
                "elapsed_s": elapsed,
                "signature": stage_signatures(cfg)["ocr"],
            }
        ],
    }


def test_first_slow_frame_does_not_replace_established_throughput(ocr_config):
    cfg = ocr_config
    first = video(cfg, done=1, elapsed=28)
    result = estimate([first], cfg, {}, historical_rates={"ocr": 0.3})
    assert result["eta_s"] == 30
    assert result["rates"]["ocr"] == 0.3
    cold = estimate([first], cfg, {})
    assert cold["eta_s"] is None
    assert cold["eta_status"] == "calibrating"


def test_failed_attempts_are_not_throughput_measurements(ocr_config):
    failed = video(ocr_config, done=20, elapsed=600, status="failed")
    result = estimate([failed], ocr_config, {})
    assert result["eta_s"] is None
    assert estimate([failed], ocr_config, {}, historical_rates={"ocr": 0.3})["eta_s"] == 24


def test_completed_median_resists_one_retry_heavy_video(ocr_config):
    cfg = ocr_config
    videos = [video(cfg, i, 100, seconds, "complete") for i, seconds in enumerate([30, 32, 900], 1)]
    videos.append(video(cfg, 4, status="pending"))
    result = estimate(videos, cfg, {})
    assert result["eta_s"] == 32


def test_recent_checkpoint_speed_recovers_after_startup_and_ignores_duplicate_polls(ocr_config):
    cfg, tracker = ocr_config, ProgressTracker()
    rates, _ = tracker.observe("job-a", [video(cfg, done=1, elapsed=60)], cfg, {})
    assert rates == {}
    for done, elapsed in [(11, 63), (21, 66), (31, 69)]:
        current = video(cfg, done=done, elapsed=elapsed)
        rates, unstable = tracker.observe("job-a", [current], cfg, {})
    assert rates["ocr"] == pytest.approx(0.3)
    assert unstable == []
    result = estimate([current], cfg, {}, recent_rates=rates)
    assert result["eta_s"] == 21  # Not 69 / 31 * 69 seconds from the cold start.
    samples = deepcopy(tracker.samples)
    tracker.observe("job-a", [current], cfg, {})
    assert tracker.samples == samples
    rates, _ = tracker.observe("job-b", [current], cfg, {})
    assert rates == {}  # Another job/library must calibrate its own live speed.


def test_erratic_throughput_shows_recalibration_and_then_recovers(ocr_config):
    cfg, tracker = ocr_config, ProgressTracker()
    done, elapsed = 1, 60
    tracker.observe("job", [video(cfg, done=done, elapsed=elapsed)], cfg, {})
    for seconds in [3, 300, 3, 300]:
        done, elapsed = done + 2, elapsed + seconds
        current = video(cfg, done=done, elapsed=elapsed)
        rates, unstable = tracker.observe("job", [current], cfg, {})
    # Enough successful samples are required before presenting a live estimate.
    done, elapsed = done + 2, elapsed + 3
    current = video(cfg, done=done, elapsed=elapsed)
    rates, unstable = tracker.observe("job", [current], cfg, {})
    result = estimate([current], cfg, {}, recent_rates=rates, unstable_stages=unstable)
    assert result["eta_s"] is None and result["eta_status"] == "recalibrating"
    for _ in range(12):
        done, elapsed = done + 2, elapsed + 0.6
        current = video(cfg, done=done, elapsed=elapsed)
        rates, unstable = tracker.observe("job", [current], cfg, {})
    assert rates["ocr"] == pytest.approx(0.3)
    assert unstable == []


def test_historical_rates_use_typical_completed_video_not_retry_outlier(tmp_path, ocr_config):
    state = AppState(tmp_path)
    signature = stage_signatures(ocr_config)["ocr"]
    with state.connect() as db:
        db.executemany(
            "INSERT INTO timings VALUES(?,?,?,?,?,?)",
            [("library", i, "ocr", signature, seconds, 100) for i, seconds in enumerate([30, 32, 900])],
        )
        db.execute("INSERT INTO timings VALUES(?,?,?,?,?,?)", ("old", 1, "ocr", "old-model", 1, 100))
    assert state.timing_rates(ocr_config)["ocr"] == pytest.approx(0.32)
