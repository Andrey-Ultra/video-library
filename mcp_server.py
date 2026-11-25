"""Compatibility entry point for the video library MCP server."""

import sys

from video_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["serve", *sys.argv[1:]]))
