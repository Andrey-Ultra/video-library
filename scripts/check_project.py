"""Run repeatable development checks without opening the app or user libraries."""

from __future__ import annotations

import argparse
import platform
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(command):
    print(f"\n{shlex.join(map(str, command))}", flush=True)
    subprocess.run(list(map(str, command)), cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-desktop", action="store_true", help="Build the app without launching it")
    parser.add_argument("--playback", action="store_true", help="Run AVPlayer checks using generated video")
    parser.add_argument(
        "--skip-system-ocr",
        action="store_true",
        help="Explicitly omit native Vision OCR on headless/unsupported hosts",
    )
    args = parser.parse_args()
    try:
        run([sys.executable, "-m", "ruff", "check", "--no-cache", "video_agent", "tests", "scripts"])
        pytest = [sys.executable, "-m", "pytest", "-q"]
        if args.skip_system_ocr:
            pytest += ["-k", "not vision_ocr_recognizes_english_without_model_download"]
        run(pytest)
        if platform.system() == "Darwin":
            cache = ROOT / ".cache/native-validation"
            modules = ROOT / ".cache/swift-modules"
            cache.mkdir(parents=True, exist_ok=True)
            modules.mkdir(parents=True, exist_ok=True)
            checks = {
                "menu-summary-checks": [
                    "Sources/Models.swift",
                    "Sources/FolderMenuSummary.swift",
                    "Tests/MenuSummaryChecks.swift",
                ],
                "request-checks": ["Sources/LatestRequest.swift", "Tests/RequestChecks.swift"],
            }
            for name, files in checks.items():
                binary = cache / name
                run(
                    [
                        "xcrun",
                        "swiftc",
                        "-parse-as-library",
                        "-module-cache-path",
                        modules,
                        *(ROOT / "desktop" / path for path in files),
                        "-o",
                        binary,
                    ]
                )
                run([binary])
        elif args.build_desktop or args.playback:
            parser.error("Desktop build and playback checks require macOS")
        if args.playback:
            run([sys.executable, "scripts/check_playback.py"])
        if args.build_desktop:
            run([sys.executable, "scripts/build_desktop.py"])
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
