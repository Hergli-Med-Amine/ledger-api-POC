from collections.abc import Callable
from typing import Annotated

from fastapi import Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from ledger_api import idempotency
from ledger_api.api.auth import Principal
from ledger_api.idempotency import StoredResponse

# Printable ASCII only, so a key can be stored, compared and logged without surprises.
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$")]


def idempotent_write[R: BaseModel](
    engine: Engine,
    request: Request,
    principal: Principal,
    key: str,
    body: BaseModel,
    work: Callable[[Connection], R],
) -> R | JSONResponse:
    """One transaction holds the idempotency key, the work and its stored response (ADR 3)."""
    request_hash = idempotency.fingerprint(
        request.method, request.url.path, body.model_dump(mode="json")
    )
    with engine.begin() as conn:
        stored = idempotency.begin(conn, principal.tenant_id, key, request_hash)
        if stored is not None:
            return JSONResponse(
                stored.body, status_code=stored.status_code, headers={"Idempotent-Replayed": "true"}
            )
        response = work(conn)
        idempotency.record(
            conn,
            principal.tenant_id,
            key,
            request_hash,
            StoredResponse(201, response.model_dump(mode="json")),
        )
    return response
