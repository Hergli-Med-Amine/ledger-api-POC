"""Outbox relay: python -m ledger_api.worker [--drain]. Safe to run several copies (SKIP LOCKED).

--drain delivers what is due and exits, for a scheduled job instead of an always-on process.
"""

import argparse
import os
import signal
import threading

import httpx2
from sqlalchemy import create_engine

from ledger_api import log, outbox


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ledger_api.worker")
    parser.add_argument("--drain", action="store_true", help="exit once nothing is due")
    drain = parser.parse_args().drain
    log.configure()
    engine = create_engine(os.environ["DATABASE_URL"])
    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "1"))
    stop = threading.Event()
    # SIGTERM (docker stop, Cloud Run scale-down) finishes the current batch, then exits.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with httpx2.Client(timeout=5.0, follow_redirects=False) as client:
        while not stop.is_set():
            if outbox.relay_batch(engine, client) == 0:
                if drain:
                    break
                stop.wait(poll_seconds)
    engine.dispose()


if __name__ == "__main__":
    main()
