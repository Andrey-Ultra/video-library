"""Private native-app service. Port is allocated by the OS; parent owns its lifetime."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(args.state_dir), log_level="warning"))

    def watch_parent():
        while not server.should_exit:
            if args.parent_pid and os.getppid() != args.parent_pid:
                server.should_exit = True
                return
            time.sleep(1)

    threading.Thread(target=watch_parent, daemon=True).start()
    print(json.dumps({"url": f"http://127.0.0.1:{port}"}), flush=True)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
