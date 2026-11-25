"""Exercise real AVPlayer seek/play races without loading indexing models."""

from __future__ import annotations

import argparse
import platform
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="Use a source of at least 12 seconds; playback is muted")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("These native AVPlayer checks require macOS")
    cache = ROOT / ".cache" / "native-validation"
    modules = ROOT / ".cache" / "swift-modules"
    cache.mkdir(parents=True, exist_ok=True)
    modules.mkdir(parents=True, exist_ok=True)
    binary = cache / "playback-checks"
    sources = [ROOT / "desktop/Sources/Playback.swift", ROOT / "desktop/Tests/PlaybackChecks.swift"]
    if not binary.exists() or any(p.stat().st_mtime > binary.stat().st_mtime for p in sources):
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-parse-as-library",
                "-framework",
                "AVKit",
                "-module-cache-path",
                str(modules),
                *map(str, sources),
                "-o",
                str(binary),
            ],
            check=True,
        )
    with tempfile.TemporaryDirectory(prefix="video-agent-playback-") as temp:
        source = args.video.expanduser().resolve() if args.video else Path(temp) / "source.mp4"
        if args.video is None:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24",
                    "-t",
                    "12",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(source),
                ],
                check=True,
            )
        subprocess.run([str(binary), str(source)], check=True)


if __name__ == "__main__":
    main()
