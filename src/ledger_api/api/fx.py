from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection

from ledger_api import fx, ledger
from ledger_api.api.auth import Writer
from ledger_api.api.schemas import (
    ConversionRequest,
    ConversionResponse,
    QuoteRequest,
    QuoteResponse,
    problem_responses,
)
from ledger_api.api.state import EngineDep
from ledger_api.api.writes import IdempotencyKey, idempotent_write

router = APIRouter(prefix="/v1/fx", tags=["fx"], responses=problem_responses(401, 403, 429))


@router.post(
    "/quotes", status_code=201, response_model=QuoteResponse, responses=problem_responses(409, 422)
)
def create_quote(
    body: QuoteRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> QuoteResponse | JSONResponse:
    def work(conn: Connection) -> QuoteResponse:
        quote = fx.create_quote(conn, principal.tenant_id, body.from_currency, body.to_currency)
        return QuoteResponse(
            id=quote.id,
            from_currency=quote.from_currency,
            to_currency=quote.to_currency,
            rate=quote.rate,
            expires_at=quote.expires_at,
        )

    return idempotent_write(engine, request, principal, idempotency_key, body, work)


@router.post(
    "/conversions",
    status_code=201,
    response_model=ConversionResponse,
    responses=problem_responses(404, 409, 422),
)
def convert(
    body: ConversionRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversionResponse | JSONResponse:
    def work(conn: Connection) -> ConversionResponse:
        result = ledger.convert_currency(
            conn, principal.tenant_id, body.quote_id, body.sell_amount_minor
        )
        return ConversionResponse(
            transaction_id=result.transaction_id,
            quote_id=body.quote_id,
            rate=result.rate,
            from_wallet_id=result.from_wallet_id,
            sold_amount_minor=result.sold.amount_minor,
            sold_currency=result.sold.currency,
            to_wallet_id=result.to_wallet_id,
            bought_amount_minor=result.bought.amount_minor,
            bought_currency=result.bought.currency,
        )

    return idempotent_write(engine, request, principal, idempotency_key, body, work)
