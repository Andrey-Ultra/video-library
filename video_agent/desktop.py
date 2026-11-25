"""Launch the native desktop executable; never opens a browser or a WebView."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path


def launch(state_dir=None, rebuild=False):
    if platform.system() != "Darwin":
        raise RuntimeError(
            "The native app currently supports macOS 14+. The indexer and MCP also run on other platforms."
        )
    root = Path(__file__).resolve().parent.parent
    build = [sys.executable, str(root / "scripts" / "build_desktop.py")]
    if rebuild:
        build.append("--force")
    subprocess.run(build, check=True)
    command = ["/usr/bin/open", str(root / "Video Library.app")]
    if state_dir:
        command.extend(["--args", "--state-dir", str(Path(state_dir).expanduser().resolve())])
    subprocess.run(command, check=True)
    return 0
