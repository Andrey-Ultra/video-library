<p align="center">
  <img src="docs/brand/vl.svg" width="88" height="88" alt="Vl">
</p>

<h1 align="center">Video library</h1>

<p align="center">A local video library your AI agent can explore.</p>
<p align="center"><b>Early alpha · macOS · Python + SwiftUI · MCP</b></p>

Video library indexes video folders: it transcribes speech, reads on-screen text,
describes what happens, and saves the results with timestamps.
Through MCP, your agent can find relevant moments and explore your footage.
Source videos stay unchanged.

## What works

- Visual descriptions, speech transcription, and OCR.
- Event trees, summaries, keyword search, and semantic search.
- A native app for browsing libraries, playing clips, and inspecting indexed evidence.
- A folder queue with pause and resume from saved checkpoints.
- MCP tools for choosing libraries, finding clips, and checking source frames.

## Project status

A working prototype in active development, tested on a **Mac mini M4 / 16 GB**.
The current focus is English speech and English on-screen text.

The desktop app currently supports macOS; the NVIDIA profile is experimental.
Setup requires the source code; there is no standalone installer yet.
Models can make mistakes, and sparse frame sampling can miss brief events.
Video editing and export are outside the current scope.

## Run on Mac

Requires **macOS 14+, Apple Silicon, Python 3.12, FFmpeg, and Command Line Tools**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[mac]'
python main.py ui
```

Local models download on first use.
In the app: **Add folder → Add to queue**. For MCP setup: **Connect your agent**.

[Setup, CLI & MCP — Russian](docs/USAGE.md) · [Benchmarks — Russian](BENCHMARKS.md)

## Built with Codex

Developed with **OpenAI Codex**.
The author's rough estimate: **30% human, 70% Codex**.
This describes participation in development, not a measured share of code lines.

## License

[MIT](LICENSE) © 2026. See the license file for the copyright holder and full terms.
