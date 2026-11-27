"""Temporary, bounded video proxies for indexing; audio always uses the source."""

from __future__ import annotations

import logging
import math
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import media

logger = logging.getLogger(__name__)
MiB = 1024 * 1024


class ProxyBudgetExceeded(RuntimeError):
    """Retry with a shorter time range, keeping the requested quality."""


def dimensions(width, height, resolution):
    if resolution == "original":
        ratio = 1
    else:
        long_side, short_side = {"1080p": (1920, 1080), "720p": (1280, 720)}[resolution]
        ratio = min(1, long_side / max(width, height), short_side / min(width, height))
    # H.264 needs even dimensions. Never enlarge an input.
    return tuple(max(2, math.floor(n * ratio / 2) * 2) for n in (width, height))


class ProxyCache:
    def __init__(self, directory, config, cancel, budget=None, reserve=256 * MiB):
        self.directory = Path(directory)
        self.config, self.cancel = config, cancel
        self.budget = config.cache_mb * MiB if budget is None else budget
        self.reserve = reserve
        self.entries = {}  # insertion order is the least-recently-used order
        # At 6 Mbps leave headroom for complex footage/software encoding.
        self.chunk_ms = min(60_000, max(1000, int(self.budget / 1_500_000 * 1000)))

    def source(self, video, start_ms=0, end_ms=None):
        original = Path(video["path"])
        cfg, info = self.config, video["metadata"]
        if not cfg.enabled:
            return original
        size = dimensions(info["width"], info["height"], cfg.resolution)
        resize = cfg.resolution != "original" and size != (info["width"], info["height"])
        reduce_fps = cfg.max_fps and (not info["fps"] or info["fps"] > cfg.max_fps)
        if not resize and not reduce_fps:
            return original
        full = start_ms == 0 and end_ms in (None, info.get("duration_ms"))
        key = video["fingerprint"] if full else (video["fingerprint"], start_ms, end_ms)
        if key in self.entries:
            cached = self.entries.pop(key)
            self.entries[key] = cached
            return cached
        self.cancel()
        # A job may visit hundreds of files. Keep the entire temporary cache bounded,
        # including a proxy currently being written, and leave a free-space reserve.
        while self.entries and (
            self._bytes() > self.budget // 2
            or self.budget - self._bytes() < 16 * MiB
            or self._free() < self.reserve + self.budget
        ):
            self.entries.pop(next(iter(self.entries))).unlink(missing_ok=True)
        available = min(self.budget - self._bytes(), self._free() - self.reserve)
        if available < 16 * MiB:
            raise RuntimeError(
                "Not enough free space for an analysis proxy. Free disk space or select Original."
            )
        suffix = "" if full else f"-{start_ms}-{end_ms}"
        dest = self.directory / f"{video['id']}{suffix}.mp4"
        filters = ["setpts=PTS-STARTPTS"]
        if reduce_fps:
            # Resample on the original time axis; never change playback speed.
            # A proxy frame may differ by up to one frame interval from the source.
            filters.append(f"fps={cfg.max_fps}:round=near")
        filters.extend([f"scale={size[0]}:{size[1]}", "setsar=1"])
        logger.info(
            "Preparing analysis proxy: %s · %sx%s · %s FPS",
            original.name,
            *size,
            cfg.max_fps if reduce_fps else "original",
        )
        common = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            *(["-ss", f"{start_ms / 1000:.6f}"] if start_ms else []),
            "-i",
            str(original),
            *(["-t", f"{(end_ms - start_ms) / 1000:.6f}"] if end_ms is not None else []),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "vfr",
            "-pix_fmt",
            "yuv420p",
        ]
        encoders = []
        if platform.system() == "Darwin":
            encoders.append(["-c:v", "h264_videotoolbox", "-b:v", "6M", "-allow_sw", "0"])
        encoders.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2"])
        try:
            for encoder in encoders:
                try:
                    self._encode(
                        [*common, *encoder, "-metadata:s:v:0", "rotate=0", str(dest)], dest, available
                    )
                    break
                except subprocess.CalledProcessError as exc:
                    dest.unlink(missing_ok=True)
                    if encoder is encoders[-1]:
                        raise RuntimeError(f"Could not create analysis proxy: {exc.stderr[-1500:]}") from exc
                    logger.info("Hardware proxy encoder unavailable; using software encoder")
            proxy_info = media.metadata(dest)
            if (proxy_info["width"], proxy_info["height"]) != size:
                raise ValueError("Analysis proxy dimensions do not match the requested size")
            self.entries[key] = dest
            return dest
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    def segments(self, video, start_ms=0, end_ms=None):
        """Yield bounded proxies with their original time range, starting near a checkpoint."""
        duration = video["metadata"]["duration_ms"]
        end_ms = min(duration, duration if end_ms is None else end_ms)
        if start_ms >= end_ms:
            return
        if not self.config.enabled:
            yield self.source(video), 0, duration
            return
        position = (start_ms // self.chunk_ms) * self.chunk_ms
        while position < end_ms:
            self.cancel()
            stop = min(duration, position + self.chunk_ms)
            try:
                path = (
                    self.source(video)
                    if position == 0 and stop == duration
                    else self.source(video, position, stop)
                )
            except ProxyBudgetExceeded:
                if self.chunk_ms <= 1000:
                    raise
                self.chunk_ms = max(1000, self.chunk_ms // 2)
                logger.info("Shortening analysis chunks to %.1f s; quality unchanged", self.chunk_ms / 1000)
                continue
            if path == Path(video["path"]):
                yield path, 0, duration
                return
            yield path, position, stop
            position = stop

    def sampled_frames(self, video, fps, max_side, start_ms=0):
        for path, offset, end in self.segments(video, start_ms):
            self.cancel()
            if not offset:
                frames = media.sampled_frames(path, fps, max_side, start_ms)
            else:
                frames = media.sampled_frames(
                    path, fps, max_side, max(start_ms, offset), time_offset_ms=offset
                )
            try:
                for t_ms, image in frames:
                    self.cancel()
                    if t_ms >= end:
                        break
                    yield t_ms, image, path != Path(video["path"])
            finally:
                getattr(frames, "close", lambda: None)()

    def sample_window(self, video, window, count, max_side, output):
        targets = [
            window.start_ms + (i + 0.5) * (window.end_ms - window.start_ms) / count for i in range(count)
        ]
        samples, used_proxy = [], False
        for path, offset, end in self.segments(video, window.start_ms, window.end_ms):
            self.cancel()
            selected = [t for t in targets if offset <= t < end]
            if not selected:
                continue
            try:
                if offset == 0 and end >= window.end_ms:
                    part = media.sample_window(path, window, count, max_side, output)
                else:
                    bounds = media.Window(max(window.start_ms, offset), min(window.end_ms, end))
                    part = media.sample_targets(
                        path, bounds, selected, max_side, output, time_offset_ms=offset, prefix=f"{offset}-"
                    )
                used_proxy |= path != Path(video["path"])
                samples.extend(part)
            except ValueError:
                # A short tail can fall between the proxy's frames. Keep the
                # established original-frame fallback, with source timestamps.
                if path == Path(video["path"]):
                    raise
                return media.sample_window(Path(video["path"]), window, count, max_side, output), False
        if not samples:
            raise ValueError("No decodable frames in analysis window")
        return samples, used_proxy

    def _free(self):
        return shutil.disk_usage(self.directory).free

    def _bytes(self):
        return sum(p.stat().st_size for p in self.entries.values())

    def _encode(self, command, dest, available):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
            )
            try:
                while True:
                    self.cancel()
                    if self._free() < self.reserve:
                        raise RuntimeError(
                            "Not enough free space for an analysis chunk. Free disk space and retry."
                        )
                    if dest.exists() and dest.stat().st_size > available:
                        raise ProxyBudgetExceeded("Analysis chunk exceeded the temporary disk budget")
                    try:
                        result = process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if result:
                    errors.seek(0)
                    raise subprocess.CalledProcessError(
                        result, command, stderr=errors.read().decode(errors="replace")
                    )
                if dest.stat().st_size > available:
                    raise ProxyBudgetExceeded("Analysis chunk exceeded the temporary disk budget")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
