import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import redis
from fastapi import FastAPI, Request, Response
from sqlalchemy import create_engine

from ledger_api import log
from ledger_api.api import errors, fx, wallets
from ledger_api.ratelimit import RateLimiter

logger = logging.getLogger("ledger_api.http")
# A client's X-Request-Id is reused only if it is short and cannot break a log line.
_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.configure()
    app.state.engine = create_engine(os.environ["DATABASE_URL"])
    app.state.rate_limiter = _rate_limiter()
    yield
    app.state.engine.dispose()


def _rate_limiter() -> RateLimiter | None:
    if "REDIS_URL" not in os.environ:
        return None
    # Short timeouts: a slow or missing Redis must not hold up requests before failing open.
    client = redis.Redis.from_url(
        os.environ["REDIS_URL"], socket_timeout=0.2, socket_connect_timeout=0.2
    )
    return RateLimiter(
        client,
        per_second=float(os.environ.get("RATE_LIMIT_PER_SECOND", "20")),
        burst=int(os.environ.get("RATE_LIMIT_BURST", "40")),
    )


app = FastAPI(title="ledger-api", version="0.1.0", lifespan=lifespan)
errors.install(app)
app.include_router(wallets.router)
app.include_router(fx.router)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    client_id = request.headers.get("x-request-id", "")
    request_id = client_id if _CLIENT_REQUEST_ID.match(client_id) else str(uuid4())
    token = log.start_request(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # Last resort for bugs: full traceback in the log, nothing internal in the response.
        logger.exception("unhandled error")
        response = errors.problem(500, "Internal Server Error")
    response.headers["X-Request-Id"] = request_id
    logger.info(
        "request",
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        },
    )
    log.end_request(token)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
