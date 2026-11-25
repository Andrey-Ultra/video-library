"""Detached, supervised worker. Closing the UI does not interrupt indexing."""

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    # Keep the inherited descriptor open throughout this process, but do not let
    # model/OCR children inherit ownership of the application worker lock.
    os.set_inheritable(args.lock_fd, False)
    state = AppState(args.state)
    job = state.job(args.job)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: state.update_job(args.job, cancel_requested=1, status="pausing"))
    state.update_job(args.job, status="running", started_at=time.time())

    def check():
        if state.cancelled(args.job):
            raise KeyboardInterrupt

    try:
        check()
        with Store(job["folder"]) as store:
            if job["kind"] == "refresh":
                with writer_lock(store):
                    result = synchronize(store)
            else:
                result = Indexer(store, parse_config(job["config"]), cancel=check).run()
            state.learn_timings(job["library_id"], store)
        state.update_job(
            args.job,
            status="failed" if result.get("errors") else "complete",
            result=json.dumps(result),
            error="Some files need attention" if result.get("errors") else None,
            finished_at=time.time(),
        )
    except KeyboardInterrupt:
        state.update_job(args.job, status="paused", finished_at=time.time())
    except BaseException as exc:
        logging.exception("Worker failed")
        state.update_job(args.job, status="failed", error=str(exc), finished_at=time.time())
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    main()
