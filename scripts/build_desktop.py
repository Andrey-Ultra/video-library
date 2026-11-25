"""Build a genuine AppKit/SwiftUI .app using the macOS Command Line Tools."""

from __future__ import annotations

import argparse
import platform
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "Video Library.app"


def build(force=False):
    if platform.system() != "Darwin":
        raise RuntimeError("The native desktop interface currently requires macOS.")
    sources = sorted((ROOT / "desktop" / "Sources").glob("*.swift"))
    executable = APP / "Contents" / "MacOS" / "VideoLibrary"
    inputs = [*sources, ROOT / "desktop" / "Icon.swift", Path(__file__)]
    info_path = APP / "Contents" / "Info.plist"
    correct_root = info_path.exists() and plistlib.loads(info_path.read_bytes()).get(
        "VideoAgentProjectRoot"
    ) == str(ROOT)
    if (
        not force
        and executable.exists()
        and correct_root
        and all(p.stat().st_mtime <= executable.stat().st_mtime for p in inputs)
    ):
        return APP
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources = APP / "Contents" / "Resources"
    resources.mkdir(exist_ok=True)
    cache = ROOT / ".cache" / "swift-modules"
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-O",
            "-framework",
            "AVKit",
            "-target",
            f"{platform.machine()}-apple-macos14.0",
            "-module-cache-path",
            str(cache),
            *map(str, sources),
            "-o",
            str(executable),
        ],
        check=True,
    )
    info = {
        "CFBundleName": "Video Library",
        "CFBundleDisplayName": "Video Library",
        "CFBundleIdentifier": "local.video-agent.library",
        "CFBundleExecutable": "VideoLibrary",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleVersion": "2",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSDocumentsFolderUsageDescription": "Video Library reads its local Python environment and the video folders you choose, and saves their indexes.",
        "NSHumanReadableCopyright": "Local Video Library",
        "VideoAgentProjectRoot": str(ROOT),
    }
    (APP / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    # AppKit draws the small vector wordmark; no generated bitmap or external asset.
    subprocess.run(
        [
            "xcrun",
            "swift",
            "-module-cache-path",
            str(cache),
            str(ROOT / "desktop" / "Icon.swift"),
            str(resources),
        ],
        check=True,
    )
    info["CFBundleIconFile"] = "AppIcon"
    (APP / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP)], check=True, capture_output=True)
    return APP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(build(args.force))


if __name__ == "__main__":
    main()
