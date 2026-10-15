"""JSON log lines carrying the current request id and tenant id."""

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# One mutable dict per request, not one ContextVar per field: FastAPI runs sync dependencies in
# worker threads on a copy of the context, so a ContextVar.set() there (where the tenant becomes
# known) would be lost, while a change to this shared dict is seen by the whole request.
_request_context: ContextVar[dict[str, str] | None] = ContextVar("request_context", default=None)


def start_request(request_id: str) -> Token[dict[str, str] | None]:
    return _request_context.set({"request_id": request_id})


def end_request(token: Token[dict[str, str] | None]) -> None:
    _request_context.reset(token)


def set_tenant(tenant_id: UUID) -> None:
    context = _request_context.get()
    if context is not None:
        context["tenant_id"] = str(tenant_id)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "context", {}),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(level: int = logging.INFO) -> None:
    base_factory = logging.getLogRecordFactory()

    # Captured when the record is created, i.e. still inside the request that logged it.
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        record.context = dict(_request_context.get() or {})
        return record

    logging.setLogRecordFactory(factory)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own plain-text handlers; send its records through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # Replaced by the app's own access line, which also carries the request and tenant ids.
    logging.getLogger("uvicorn.access").disabled = True
