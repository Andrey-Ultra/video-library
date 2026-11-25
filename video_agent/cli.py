from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from . import answering, backends, library
from .config import is_apple_silicon, load_config
from .pipeline import Indexer
from .store import Store


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_answer(result):
    print(result["answer"])
    if result.get("sources"):
        print("\nИсточники:")
        for source in result["sources"]:
            print(f"[{source['id']}] {source['file']} {source['start']}–{source['end']}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"index", "ask", "search", "overview", "serve", "doctor", "ui"}
    if not argv or (argv[0] not in commands and argv[0] not in {"-h", "--help"}):
        argv.insert(0, "index")
    parser = argparse.ArgumentParser(description="Index a video folder and explore it locally or over MCP.")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in sorted(commands):
        sub = subs.add_parser(command)
        if command not in {"doctor", "ui"}:
            sub.add_argument("folder", nargs="?" if command in {"index", "serve"} else None, default="videos")
        if command != "serve":
            sub.add_argument("--config", type=Path)
            sub.add_argument("--profile", choices=["mac", "fast", "quality", "cuda"])
        else:
            sub.set_defaults(config=None, profile=None)
            sub.add_argument("--active", action="store_true", help="Follow the folder selected in the UI")
            sub.add_argument("--state-dir", type=Path)
        if command == "ui":
            sub.add_argument("--state-dir", type=Path)
            sub.add_argument("--rebuild", action="store_true", help="Rebuild the native macOS app")
        if command == "index":
            sub.add_argument("--force", action="store_true", help="Recompute all configured stages")
            sub.add_argument("--max-videos", type=int, help="Process only the first N discovered files")
            sub.add_argument("--chat", action="store_true", help="Ask questions after indexing")
        if command in {"search", "ask"}:
            sub.add_argument("query")
            sub.add_argument("--lexical-only", action="store_true")
            sub.add_argument("--video-id", type=int)
        if command == "search":
            sub.add_argument(
                "--source", choices=["all", "visual", "speech", "ocr", "hierarchy"], default="all"
            )
            sub.add_argument("--top-k", type=int, default=10)
        if command == "overview":
            sub.add_argument("--offset", type=int, default=0)
            sub.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        if args.command == "ui":
            from .desktop import launch

            return launch(args.state_dir, args.rebuild)
        config = load_config(args.config, args.profile)
        if args.command == "doctor":
            missing = backends.missing_dependencies(config)
            binaries = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
            print_json(
                {
                    "python": sys.version.split()[0],
                    "apple_silicon": is_apple_silicon(),
                    "binaries": binaries,
                    "missing_modules": missing,
                    "config": asdict(config),
                }
            )
            return int(bool(missing) or not all(binaries.values()))
        if args.command == "serve":
            from .server import build_server

            if args.active:
                from .app_state import AppState

                state = AppState(args.state_dir)
                build_server(state.active_folder, model_access=state.model_access, registry=state).run(
                    transport="stdio"
                )
            else:
                build_server(args.folder).run(transport="stdio")
            return 0
        if args.command == "index":
            if args.max_videos is not None and args.max_videos < 1:
                raise ValueError("--max-videos must be positive")
            missing = backends.missing_dependencies(config)
            if missing:
                raise RuntimeError(
                    f"Missing modules: {', '.join(missing)}. Install: pip install -e '.[mac]' "
                    "or pip install -e '.[cuda]'"
                )
            for binary in ("ffmpeg", "ffprobe"):
                if not shutil.which(binary):
                    raise RuntimeError(f"{binary} not found. On macOS: brew install ffmpeg")
            with Store(args.folder) as store:
                result = Indexer(store, config).run(args.force, args.max_videos)
            print_json(result)
            if result["errors"]:
                return 1
            if args.chat and result["complete"]:
                return chat(args.folder)
            return 0
        with Store(args.folder, create=False) as store:
            if args.config is None and args.profile is None:
                config = library.indexed_config(store)
            if args.command == "overview":
                from .server import validate_page

                validate_page(args.offset, args.limit)
                print_json(library.overview(store, args.offset, args.limit))
            elif args.command == "search":
                config.configure_cache()
                print_json(
                    library.search(
                        store, config, args.query, args.top_k, args.video_id, args.source, args.lexical_only
                    )
                )
            elif args.command == "ask":
                result = answering.answer(
                    store, config, args.query, args.lexical_only, video_id=args.video_id
                )
                print_answer(result)
        return 0
    except KeyboardInterrupt:
        print("\nОстановлено. Повторный запуск продолжит индексацию.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.error("%s", exc)
        return 1


def chat(folder):
    print("Вопросы по индексу. Для выхода: /exit. Каждый вопрос независимый.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if question in {"/exit", "/quit"}:
            return 0
        if not question:
            continue
        try:
            with Store(folder, create=False) as store:
                result = answering.answer(store, library.indexed_config(store), question)
                print_answer(result)
        except Exception as exc:
            logging.error("%s", exc)
