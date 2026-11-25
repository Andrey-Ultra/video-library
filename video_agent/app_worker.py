"""Detached sequential queue runner; survives native-app and local-service exit."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time

from .app_state import AppState, parse_config
from .pipeline import Indexer, writer_lock
from .store import Store
from .sync import synchronize


def run_job(state, job):
    job_id = job["id"]
    with (state.directory / f"{job_id}.log").open("ab", buffering=0) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", force=True)
    state.update_job(job_id, status="running")

    def check():
        if state.cancelled(job_id):
            raise KeyboardInterrupt

    try:
        check()
        if not os.path.isdir(job["folder"]):
            raise FileNotFoundError("Folder is unavailable. Reconnect its drive and retry.")
        with Store(job["folder"]) as store:
            if job["kind"] == "refresh":
                with writer_lock(store):
                    result = synchronize(store)
            else:
                result = Indexer(store, parse_config(job["config"]), cancel=check).run()
            state.learn_timings(job["library_id"], store)
        state.update_job(
            job_id,
            status="failed" if result.get("errors") else "complete",
            result=json.dumps(result),
            error="Some files need attention" if result.get("errors") else None,
            finished_at=time.time(),
        )
    except KeyboardInterrupt:
        state.update_job(job_id, status="paused", finished_at=time.time())
        state.pause_queue()
    except BaseException as exc:
        logging.exception("Task failed; continuing with the next queued library")
        state.update_job(job_id, status="failed", error=str(exc), finished_at=time.time())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--job")  # Kept for compatibility with existing launchers.
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    os.set_inheritable(args.lock_fd, False)
    state = AppState(args.state)
    signal.signal(signal.SIGTERM, lambda *_: state.pause_queue())
    memory = None
    try:
        while True:
            # Wait for an MCP model search without consuming GPU memory or losing the queue.
            with state.queue_guard():
                if state.queue_paused() or not any(j["status"] == "queued" for j in state.queue_jobs()):
                    os.close(args.lock_fd)
                    args.lock_fd = None
                    return
            memory = state._lock()
            if memory is None:
                time.sleep(0.2)
                continue
            job = state.next_queued(args.lock_fd, memory)
            if job is None:
                args.lock_fd = None
                return
            run_job(state, job)
            memory.close()
            memory = None
    finally:
        if memory is not None:
            memory.close()
        if args.lock_fd is not None:
            os.close(args.lock_fd)


if __name__ == "__main__":
    main()
