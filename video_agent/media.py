from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".flv",
    ".mts",
    ".m2ts",
    ".ts",
    ".avi",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".mxf",
}


@dataclass(frozen=True)
class Window:
    start_ms: int
    end_ms: int


def windows(duration_ms: int, window_s: float, overlap_s: float):
    size, overlap = round(window_s * 1000), round(overlap_s * 1000)
    if duration_ms <= 0 or size <= 0 or not 0 <= overlap < size:
        raise ValueError("Invalid duration/window/overlap")
    start = 0
    while start < duration_ms:
        end = min(start + size, duration_ms)
        yield Window(start, end)
        if end == duration_ms:
            break
        start += size - overlap


def discover(folder: Path):
    def fail(error):
        raise error

    for root, dirs, names in os.walk(folder, onerror=fail):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(root, d).is_symlink())
        for name in sorted(names):
            path = Path(root, name)
            if not name.startswith(".") and path.suffix.lower() in EXTENSIONS and not path.is_symlink():
                yield path.resolve()


def fingerprint(path: Path) -> str:
    """Sampled content hash plus stat; moving a file preserves its content identity.

    This is a change detector, not a cryptographic proof of identical full files.
    """
    stat = path.stat()
    h = hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    chunk = 1024 * 1024
    with path.open("rb") as stream:
        for offset in (0, max(0, stat.st_size // 2 - chunk // 2), max(0, stat.st_size - chunk)):
            stream.seek(offset)
            h.update(stream.read(chunk))
    return h.hexdigest()


def content_hash(path: Path, full=False) -> str:
    """Fast content candidate key; full=True verifies copies before deduplication."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    chunk = 1024 * 1024
    with path.open("rb") as stream:
        if full:
            while block := stream.read(chunk):
                h.update(block)
        else:
            for offset in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
                stream.seek(offset)
                h.update(stream.read(chunk))
    return h.hexdigest()


def metadata(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(result.stdout)
    video = next((s for s in raw["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"No video stream: {path}")
    duration = raw.get("format", {}).get("duration") or video.get("duration")
    if duration is None or not math.isfinite(float(duration)) or float(duration) <= 0:
        raise ValueError(f"Unknown or invalid video duration: {path}")
    rate = video.get("avg_frame_rate", "0/1")
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    rotation = next(
        (s.get("rotation", 0) for s in video.get("side_data_list", []) if "rotation" in s),
        float(video.get("tags", {}).get("rotate", 0)),
    )
    width, height = video["width"], video["height"]
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "duration_ms": max(1, round(float(duration) * 1000)),
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": any(s["codec_type"] == "audio" for s in raw["streams"]),
        "tags": {**video.get("tags", {}), **raw.get("format", {}).get("tags", {})},
    }


def frame_at(path: Path, time_ms: int, max_side: int = 1280):
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        container.seek(int((time_ms / 1000 + origin) * 1_000_000), backward=True)
        for frame in container.decode(stream):
            if frame.time is not None and (frame.time - origin) * 1000 >= time_ms - 1:
                image = frame.to_image().convert("RGB")
                # Match FFmpeg's autorotation for camera/phone footage.
                rotation = getattr(frame, "rotation", 0)
                if rotation:
                    image = image.rotate(rotation, expand=True)
                image.thumbnail((max_side, max_side))
                return image
    raise ValueError(f"No frame at {time_ms} ms in {path.name}")


def sample_window(path: Path, window: Window, count: int, max_side: int, output: Path):
    """One sequential decode per window, with actual decoded timestamps."""
    targets = [window.start_ms + (i + 0.5) * (window.end_ms - window.start_ms) / count for i in range(count)]
    return sample_targets(path, window, targets, max_side, output)


def sample_targets(path, window, targets, max_side, output, *, time_offset_ms=0, prefix=""):
    """Decode target frames, reporting timestamps on the original source timeline."""
    import av

    count = len(targets)
    if not count:
        return []
    samples = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        container.seek(
            int((max(0, window.start_ms - time_offset_ms) / 1000 + origin) * 1_000_000), backward=True
        )
        index = 0
        last = None
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t_ms = round((frame.time - origin) * 1000) + time_offset_ms
            if t_ms < window.start_ms:
                continue
            if t_ms >= window.end_ms:
                break
            last = (t_ms, frame)
            if t_ms < targets[index]:
                continue
            image = frame.to_image().convert("RGB")
            rotation = getattr(frame, "rotation", 0)
            if rotation:
                image = image.rotate(rotation, expand=True)
            image.thumbnail((max_side, max_side))
            dest = output / f"{prefix}frame-{len(samples)}.jpg"
            image.save(dest, quality=90)
            samples.append((t_ms, dest))
            while index < count and targets[index] <= t_ms:
                index += 1
            if index == count:
                break
        if not samples and last is not None:
            t_ms, frame = last
            image = frame.to_image().convert("RGB")
            rotation = getattr(frame, "rotation", 0)
            if rotation:
                image = image.rotate(rotation, expand=True)
            image.thumbnail((max_side, max_side))
            dest = output / f"{prefix}frame-0.jpg"
            image.save(dest, quality=90)
            samples.append((t_ms, dest))
    if not samples:
        raise ValueError(f"No decodable frames in {path.name} [{window.start_ms}, {window.end_ms})")
    return samples


def sampled_frames(path: Path, fps: float, max_side: int, start_ms: int = 0, *, time_offset_ms: int = 0):
    import av

    step = 1000 / fps
    # Resume on the original sampling grid, not on the first decoded frame after
    # the checkpoint (which would introduce extra observations after every restart).
    target = math.ceil(start_ms / step) * step
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        if start_ms:
            container.seek(
                int((max(0, start_ms - time_offset_ms) / 1000 + origin) * 1_000_000), backward=True
            )
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t_ms = round((frame.time - origin) * 1000) + time_offset_ms
            if t_ms < target:
                continue
            image = frame.to_image().convert("RGB")
            rotation = getattr(frame, "rotation", 0)
            if rotation:
                image = image.rotate(rotation, expand=True)
            image.thumbnail((max_side, max_side))
            yield t_ms, image
            target = (math.floor(t_ms / step) + 1) * step


def audio_chunk(path: Path, dest: Path, start_s: float, duration_s: float):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            str(start_s),
            "-i",
            str(path),
            "-t",
            str(duration_s),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
