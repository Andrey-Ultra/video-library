import subprocess
import sys
from dataclasses import asdict

import pytest

from video_agent import media, prompts, proxy
from video_agent.app_state import parse_config
from video_agent.config import Config, ProxyConfig, signature
from video_agent.proxy import ProxyCache, dimensions
from video_agent.signatures import backend_version, stage_signatures


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def record(source):
    return {
        "id": 1,
        "path": str(source),
        "fingerprint": media.fingerprint(source),
        "metadata": media.metadata(source),
    }


@pytest.mark.parametrize(
    "size,resolution,expected",
    [
        ((3840, 2160), "1080p", (1920, 1080)),
        ((3840, 2160), "720p", (1280, 720)),
        ((2160, 3840), "720p", (720, 1280)),
        ((640, 360), "1080p", (640, 360)),
        ((3840, 1600), "1080p", (1920, 800)),
    ],
)
def test_proxy_dimensions_preserve_orientation_and_never_enlarge(size, resolution, expected):
    assert dimensions(*size, resolution) == expected


@pytest.mark.parametrize("resolution,fps", [("bad", 0), ("720p", 60), ("720p", True)])
def test_invalid_proxy_settings_rejected(resolution, fps):
    cfg = Config()
    cfg.proxy = ProxyConfig(resolution, fps)
    with pytest.raises(ValueError):
        cfg.validate()


def test_proxy_config_roundtrip_and_legacy_fingerprints():
    cfg = Config()
    before = stage_signatures(cfg)
    assert before["visual"] == signature(
        "visual-v1", cfg.visual, prompts.PROMPT_VERSION, backend_version("mlx-vlm")
    )
    assert before["ocr"] == signature("ocr-v1", cfg.ocr, backend_version("pyobjc-framework-Vision"))
    cfg.proxy = ProxyConfig("720p", 5)
    after = stage_signatures(parse_config(asdict(cfg)))
    assert {k for k in before if before[k] != after[k]} == {"visual", "ocr", "hierarchy"}
    cfg.proxy.cache_mb = 2048
    assert stage_signatures(cfg) == after  # disk budget does not alter evidence
    cfg.proxy = ProxyConfig()
    assert stage_signatures(cfg) == before


def test_real_proxy_scales_caps_fps_preserves_timeline_and_reuses_copy(source, tmp_path, monkeypatch):
    # Deterministic portable encoder, independent of the CI host's VideoToolbox access.
    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")
    folder = tmp_path / "proxy"
    folder.mkdir()
    video = record(source)
    cache = ProxyCache(folder, ProxyConfig("720p", 5), lambda: None)
    copied = cache.source(video)
    assert copied != source and cache.source(video) == copied
    info = media.metadata(copied)
    assert (info["width"], info["height"]) == (1280, 720)
    assert info["fps"] <= 5.01 and not info["has_audio"]
    samples = [t for t, _ in media.sampled_frames(copied, 5, 80)]
    assert samples == list(range(0, 2000, 200))
    assert [t for t, _ in media.sampled_frames(copied, 1, 80, 1)] == [1000]
    assert media.fingerprint(source) == video["fingerprint"]
    assert abs(info["duration_ms"] - video["metadata"]["duration_ms"]) <= 200


def test_proxy_bypasses_files_below_both_caps(source, tmp_path, monkeypatch):
    cache = ProxyCache(tmp_path, ProxyConfig("1080p", 15), lambda: None)
    monkeypatch.setattr(cache, "_encode", lambda *a: pytest.fail("unnecessary transcoding"))
    assert cache.source(record(source)) == source
    cache.config = ProxyConfig()
    assert cache.source(record(source)) == source


def test_proxy_rotation_is_applied_once(source, tmp_path, monkeypatch):
    rotated = tmp_path / "portrait.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(source),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
    )
    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")
    folder = tmp_path / "proxy"
    folder.mkdir()
    result = ProxyCache(folder, ProxyConfig("720p", 5), lambda: None).source(record(rotated))
    info = media.metadata(result)
    assert (info["width"], info["height"]) == (720, 1280)
    assert media.frame_at(result, 0).size == (720, 1280)


def test_proxy_low_disk_fails_before_transcoding(source, tmp_path, monkeypatch):
    folder = tmp_path / "proxy"
    folder.mkdir()
    cache = ProxyCache(folder, ProxyConfig("720p", 5), lambda: None)
    monkeypatch.setattr(cache, "_free", lambda: 0)
    monkeypatch.setattr(cache, "_encode", lambda *a: pytest.fail("must not start on a full disk"))
    with pytest.raises(RuntimeError, match="free space"):
        cache.source(record(source))
    assert list(folder.iterdir()) == []


def test_proxy_cancellation_stops_child_process(tmp_path, monkeypatch):
    processes = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(proxy.subprocess, "Popen", popen)
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise KeyboardInterrupt

    cache = ProxyCache(tmp_path, ProxyConfig("720p", 5), cancel)
    with pytest.raises(KeyboardInterrupt):
        cache._encode([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path / "out.mp4", 10**8)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_old_cached_proxies_are_evicted_before_disk_budget_is_exhausted(tmp_path, monkeypatch):
    cache = ProxyCache(tmp_path, ProxyConfig("720p", 5), lambda: None, budget=32 * proxy.MiB, reserve=0)
    old = tmp_path / "old.mp4"
    with old.open("wb") as stream:
        stream.truncate(20 * proxy.MiB)
    cache.entries["old"] = old

    def encode(command, dest, available):
        assert not old.exists()
        assert available == 32 * proxy.MiB
        dest.write_bytes(b"proxy")

    monkeypatch.setattr(cache, "_encode", encode)
    monkeypatch.setattr(media, "metadata", lambda _: {"width": 1280, "height": 720})
    video = {
        "id": 1,
        "path": "original.mp4",
        "fingerprint": "new",
        "metadata": {"width": 3840, "height": 2160, "fps": 60},
    }
    assert cache.source(video).read_bytes() == b"proxy"
    assert list(cache.entries) == ["new"]


def test_cancelled_proxy_is_never_published_or_left_on_disk(tmp_path, monkeypatch):
    cache = ProxyCache(tmp_path, ProxyConfig("720p", 5), lambda: None)

    def encode(command, dest, available):
        dest.write_bytes(b"incomplete")
        raise KeyboardInterrupt

    monkeypatch.setattr(cache, "_encode", encode)
    video = {
        "id": 1,
        "path": "original.mp4",
        "fingerprint": "new",
        "metadata": {"width": 3840, "height": 2160, "fps": 60},
    }
    with pytest.raises(KeyboardInterrupt):
        cache.source(video)
    assert not cache.entries and not list(tmp_path.iterdir())


def test_chunked_video_keeps_settings_global_timestamps_and_checkpoint(source, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")
    folder = tmp_path / "chunks"
    folder.mkdir()
    cache = ProxyCache(folder, ProxyConfig("720p", 5), lambda: None)
    cache.chunk_ms = 1000
    video = record(source)
    all_times = [t for t, _, used in cache.sampled_frames(video, 2, 80) if used]
    assert all_times == [0, 600, 1000, 1600]
    assert [t for t, _, _ in cache.sampled_frames(video, 2, 80, 601)] == [1000, 1600]
    assert [t for t, _, _ in cache.sampled_frames(video, 2, 80, 2000)] == []
    segments = list(cache.segments(video))
    assert [(start, end) for _, start, end in segments] == [(0, 1000), (1000, 2000)]
    for path, _, _ in segments:
        info = media.metadata(path)
        assert (info["width"], info["height"]) == (1280, 720)
        assert info["fps"] == 5 and info["duration_ms"] == 1000
    assert media.fingerprint(source) == video["fingerprint"]


def test_visual_window_spanning_chunks_keeps_source_times_and_distinct_images(source, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")
    folder = tmp_path / "chunks"
    folder.mkdir()
    cache = ProxyCache(folder, ProxyConfig("720p", 5), lambda: None)
    cache.chunk_ms = 1000
    samples, used_proxy = cache.sample_window(record(source), media.Window(0, 2000), 4, 80, tmp_path)
    assert used_proxy and [t for t, _ in samples] == [400, 800, 1400, 1800]
    assert len({path for _, path in samples}) == 4
    assert all(path.is_file() for _, path in samples)


def test_chunk_budget_overflow_retries_shorter_ranges_without_changing_quality(tmp_path, monkeypatch):
    cache = ProxyCache(tmp_path, ProxyConfig("1080p", 30), lambda: None, budget=16 * proxy.MiB, reserve=0)
    video = {
        "id": 1,
        "path": "source.mov",
        "fingerprint": "new",
        "metadata": {"width": 3840, "height": 2160, "fps": 60, "duration_ms": 9000},
    }
    attempts = []

    def encode(command, dest, available):
        assert "scale=1920:1080" in command[command.index("-vf") + 1]
        assert "fps=30" in command[command.index("-vf") + 1]
        duration = float(command[command.index("-t") + 1]) if "-t" in command else 9
        attempts.append(duration)
        dest.write_bytes(b"partial" if duration > 3 else b"complete")
        if duration > 3:
            raise proxy.ProxyBudgetExceeded("test budget overflow")

    monkeypatch.setattr(cache, "_encode", encode)
    monkeypatch.setattr(media, "metadata", lambda _: {"width": 1920, "height": 1080})
    segments = list(cache.segments(video))
    assert attempts[0] == 9 and len(attempts) > len(segments)
    assert segments[0][1] == 0 and segments[-1][2] == 9000
    assert all(a[2] == b[1] for a, b in zip(segments, segments[1:]))
    assert all(end - start <= 3000 for _, start, end in segments)
    assert all(p.read_bytes() == b"complete" for p in cache.entries.values())
    assert set(tmp_path.iterdir()) == set(cache.entries.values())


def test_chunk_resume_starts_near_checkpoint_and_cancellation_does_not_prepare_more(tmp_path, monkeypatch):
    cache = ProxyCache(tmp_path, ProxyConfig("720p", 5), lambda: None)
    video = {"path": "source.mov", "metadata": {"duration_ms": 900000}}
    ranges = []

    def source(video, start, end):
        ranges.append((start, end))
        return tmp_path / f"{start}.mp4"

    monkeypatch.setattr(cache, "source", source)
    iterator = cache.segments(video, 721001)
    assert next(iterator)[1:] == (720000, 780000)
    iterator.close()
    assert ranges == [(720000, 780000)]


def test_ocr_chunk_resume_preserves_saved_observations_and_source_checkpoints(source, tmp_path, monkeypatch):
    import time

    from video_agent.pipeline import Indexer
    from video_agent.store import Store

    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")
    cfg = Config()
    cfg.proxy = ProxyConfig("720p", 5)
    cfg.ocr.sample_fps = 2
    cfg.ocr.max_side = 80
    folder = tmp_path / "chunks"
    folder.mkdir()
    calls = []

    class OCR:
        def recognize(self, image):
            calls.append(image.size)
            return [{"text": "saved text", "confidence": 1}]

    def pause():
        if len(calls) == 2:
            raise KeyboardInterrupt

    with Store(tmp_path) as store:
        video_id = store.register(source, media.fingerprint(source), media.metadata(source))
        store.prepare(video_id, "ocr", "test")
        indexer = Indexer(store, cfg, cancel=pause)
        indexer._timing = (video_id, "ocr", 0, time.perf_counter())
        indexer._proxies = ProxyCache(folder, cfg.proxy, indexer._check)
        indexer._proxies.chunk_ms = 1000
        with pytest.raises(KeyboardInterrupt):
            indexer._ocr(store.video(video_id), OCR(), 0)
        checkpoint = store.stage(video_id, "ocr")["checkpoint"]
        assert checkpoint == 601
        saved = store.items(video_id, stage="ocr")
        assert [item["start_ms"] for item in saved] == [0, 600]
        indexer.cancel = lambda: None
        indexer._ocr(store.video(video_id), OCR(), checkpoint)
        assert [item["start_ms"] for item in store.items(video_id, stage="ocr")] == [0, 600, 1000, 1600]
        assert all(item in store.items(video_id, stage="ocr") for item in saved)
        assert len(calls) == 4 and store.stage(video_id, "ocr")["checkpoint"] == 1601
