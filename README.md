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
- Optional temporary 1080p/720p analysis proxies with an FPS cap.
- MCP tools for choosing libraries, finding clips, and checking source frames.

## Project status

A working prototype in active development, tested on a **Mac mini M4 / 16 GB**.
Speech supports automatic recognition of 25 European languages, including English, Russian,
Ukrainian, Spanish, French, and German, with Parakeet v3. On-screen text and visual descriptions
still focus on English. Existing libraries keep their saved speech model; select v3 in the folder’s Indexing settings
to enable multilingual speech, then add the library to the queue to rebuild speech and search.

The desktop app targets **Macs with Apple Silicon**. Folder settings offer local model downloads,
a visual API server option, and suggestions based on the Mac’s chip and memory.
Models are not bundled. Experimental NVIDIA configuration remains available through the CLI.
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

Models are not bundled. In the app: **Add folder → choose models → Download → Start indexing**.
Downloads show progress, verification and retryable errors. **Suggest for this Mac** offers a starting point based on its chip and memory.
Folders with a saved index restore their settings; missing local models are offered for download before resuming.
To change methods later, right-click the folder → **Indexing settings…**. For MCP setup: **Connect your agent**.

[Setup, CLI & MCP — Russian](docs/USAGE.md) · [Benchmarks — Russian](BENCHMARKS.md)

## Built with Codex

Developed with **OpenAI Codex**.
The author's rough estimate: **30% human, 70% Codex**.
This describes participation in development, not a measured share of code lines.

## License

[MIT](LICENSE) © 2026. See the license file for the copyright holder and full terms.
