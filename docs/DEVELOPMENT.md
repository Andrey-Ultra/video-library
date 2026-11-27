# Development

The desktop interface targets macOS 14+ on Apple Silicon. Set up the environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[mac,dev]'
python scripts/check_project.py
```

`check_project.py` runs Ruff, the full Python test suite and, on macOS, native
checks for interface state and asynchronous responses. `--playback` adds AVPlayer
checks using a short generated video; `--build-desktop` builds the app without
opening it. `--skip-system-ocr` explicitly excludes real system OCR on hosts that
cannot run Apple Vision. These checks do not restart the user's app or queues.
Downloader tests use small synthetic files and do not download model weights.

## Code map

| Area | Files |
|---|---|
| Settings and configuration compatibility | `video_agent/config.py`, `signatures.py` |
| Folder registry, sequential queue and locks | `app_state.py`, `app_worker.py` |
| SQLite, checkpoints and selective layer invalidation | `store.py`, `index_storage.py` |
| Scanning, moves and duplicates | `sync.py`, `media.py` |
| Indexing and tree construction | `pipeline.py`, pure validation in `hierarchy.py` |
| Bounded temporary video cache | `proxy.py` |
| Search and evidence-based answers | `library.py`, `answering.py` |
| Model catalog and Mac recommendations | `model_catalog.py` |
| Local model file checks without loading weights | `model_cache.py` |
| Background downloads and model removal | `model_assets.py` |
| HTTP routes / interface state snapshots | `app.py` / `view_state.py` |
| MCP | `server.py` |
| Native windows and application state | `desktop/Sources/Service.swift`, `DetailView.swift` |
| Settings, proxies and MCP connection | `SettingsView.swift`, `AnalysisProxyPanel.swift`, `MCPView.swift` |
| Protection against stale asynchronous responses | `LatestRequest.swift` |

## Compatibility rules

- Keep source videos unchanged. Temporary proxies and models are stored separately from the index.
- Preserve existing API and MCP fields and identifiers. Internal module moves must
  retain import paths used by clients and tests.
- Change a layer's signature only when its evidence-generation method changes.
  Routine refactoring must not invalidate completed indexing.
- Changing a layer invalidates only that layer and its dependents. Cover this with
  tests, especially when changing speech, trees or search vectors.
- Keep long operations off the main UI thread. Apply read responses only to the
  current request revision; closing the window invalidates pending responses.
- Index writes require the writer lock. UI and MCP reads use a consistent SQLite
  snapshot. The queue and model access use separate, coordinated locks.
- Treat model responses as untrusted data. Invalid grouping must not lose or
  reorder source observations. The fallback tree preserves individual windows and
  is marked `single_window_fallback`; backend errors must still be reported as errors.

Reproduce bugs in a temporary library first. Tests must not access the user's
folders or indexes, require large downloads, or load a heavy model. Separate
checks cover system OCR and AVPlayer.
