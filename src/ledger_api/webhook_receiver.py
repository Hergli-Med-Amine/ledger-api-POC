"""Stand-in tenant endpoint for local runs: verifies each webhook's signature and logs it."""

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ledger_api import log
from ledger_api.outbox import verify_signature

logger = logging.getLogger("ledger_api.webhook_receiver")
SECRET = os.environ.get("WEBHOOK_SECRET", "")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        signature = self.headers.get("Ledger-Signature", "")
        valid = verify_signature(SECRET, signature, body, int(time.time()))
        event = json.loads(body) if valid else {}
        logger.info(
            "webhook received",
            extra={
                "fields": {
                    "event_id": self.headers.get("Ledger-Event-Id"),
                    "signature_valid": valid,
                    "type": event.get("type"),
                }
            },
        )
        self.send_response(200 if valid else 401)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # replaced by the JSON line above


if __name__ == "__main__":
    log.configure()
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
