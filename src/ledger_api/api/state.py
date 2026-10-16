from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine

from ledger_api.ratelimit import RateLimiter


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_rate_limiter(request: Request) -> RateLimiter | None:
    limiter: RateLimiter | None = request.app.state.rate_limiter
    return limiter


EngineDep = Annotated[Engine, Depends(get_engine)]
RateLimiterDep = Annotated[RateLimiter | None, Depends(get_rate_limiter)]
