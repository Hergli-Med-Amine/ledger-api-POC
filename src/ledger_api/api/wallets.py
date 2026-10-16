import base64
import binascii
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection

from ledger_api import ledger
from ledger_api.api.auth import Reader, Writer
from ledger_api.api.errors import InvalidCursor
from ledger_api.api.schemas import (
    MovementRequest,
    MovementResponse,
    OpenWalletRequest,
    StatementLineResponse,
    StatementResponse,
    WalletListResponse,
    WalletResponse,
    problem_responses,
)
from ledger_api.api.state import EngineDep
from ledger_api.api.writes import IdempotencyKey, idempotent_write
from ledger_api.money import Money

router = APIRouter(
    prefix="/v1/wallets", tags=["wallets"], responses=problem_responses(401, 403, 429)
)


@router.post(
    "", status_code=201, response_model=WalletResponse, responses=problem_responses(409, 422)
)
def open_wallet(
    body: OpenWalletRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> WalletResponse | JSONResponse:
    def work(conn: Connection) -> WalletResponse:
        wallet_id = ledger.open_wallet(conn, principal.tenant_id, body.currency)
        return WalletResponse(id=wallet_id, currency=body.currency, balance_minor=0)

    return idempotent_write(engine, request, principal, idempotency_key, body, work)


@router.get("")
def list_wallets(principal: Reader, engine: EngineDep) -> WalletListResponse:
    with engine.connect() as conn:
        wallets = ledger.list_wallets(conn, principal.tenant_id)
    return WalletListResponse(data=[_wallet_response(w.id, w.balance) for w in wallets])


@router.get("/{wallet_id}", responses=problem_responses(404, 422))
def get_wallet(wallet_id: UUID, principal: Reader, engine: EngineDep) -> WalletResponse:
    with engine.connect() as conn:
        balance = ledger.get_balance(conn, principal.tenant_id, wallet_id)
    return _wallet_response(wallet_id, balance)


@router.post(
    "/{wallet_id}/top-ups",
    status_code=201,
    response_model=MovementResponse,
    responses=problem_responses(404, 409, 422),
)
def top_up(
    wallet_id: UUID,
    body: MovementRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MovementResponse | JSONResponse:
    def work(conn: Connection) -> MovementResponse:
        amount = Money(body.amount_minor, body.currency)
        transaction_id = ledger.top_up(conn, principal.tenant_id, wallet_id, amount, body.reference)
        return _movement_response(
            conn, principal.tenant_id, wallet_id, transaction_id, "top_up", body
        )

    return idempotent_write(engine, request, principal, idempotency_key, body, work)


@router.post(
    "/{wallet_id}/payments",
    status_code=201,
    response_model=MovementResponse,
    responses=problem_responses(404, 409, 422),
)
def pay(
    wallet_id: UUID,
    body: MovementRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MovementResponse | JSONResponse:
    def work(conn: Connection) -> MovementResponse:
        amount = Money(body.amount_minor, body.currency)
        transaction_id = ledger.pay(conn, principal.tenant_id, wallet_id, amount, body.reference)
        return _movement_response(
            conn, principal.tenant_id, wallet_id, transaction_id, "payment", body
        )

    return idempotent_write(engine, request, principal, idempotency_key, body, work)


@router.post(
    "/{wallet_id}/payments/{payment_id}/refunds",
    status_code=201,
    response_model=MovementResponse,
    responses=problem_responses(404, 409, 422),
)
def refund(
    wallet_id: UUID,
    payment_id: UUID,
    body: MovementRequest,
    principal: Writer,
    engine: EngineDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MovementResponse | JSONResponse:
    def work(conn: Connection) -> MovementResponse:
        amount = Money(body.amount_minor, body.currency)
        transaction_id = ledger.refund(
            conn, principal.tenant_id, wallet_id, payment_id, amount, body.reference
        )
        return _movement_response(
            conn, principal.tenant_id, wallet_id, transaction_id, "refund", body
        )

    return idempotent_write(engine, request, principal, idempotency_key, body, work)


@router.get("/{wallet_id}/statement", responses=problem_responses(400, 404, 422))
def statement(
    wallet_id: UUID,
    principal: Reader,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> StatementResponse:
    before = _decode_cursor(cursor) if cursor else None
    with engine.connect() as conn:
        # One extra row tells whether another page exists without a separate COUNT.
        lines = ledger.statement(conn, principal.tenant_id, wallet_id, limit + 1, before)
    page = lines[:limit]
    return StatementResponse(
        data=[
            StatementLineResponse(
                posting_id=line.posting_id,
                transaction_id=line.transaction_id,
                kind=line.kind,
                amount_minor=line.amount.amount_minor,
                currency=line.amount.currency,
                reference=line.reference,
                created_at=line.created_at,
            )
            for line in page
        ],
        next_cursor=_encode_cursor(page[-1].posting_id) if len(lines) > limit else None,
    )


def _wallet_response(wallet_id: UUID, balance: Money) -> WalletResponse:
    return WalletResponse(
        id=wallet_id, currency=balance.currency, balance_minor=balance.amount_minor
    )


def _movement_response(
    conn: Connection,
    tenant_id: UUID,
    wallet_id: UUID,
    transaction_id: UUID,
    kind: str,
    body: MovementRequest,
) -> MovementResponse:
    balance = ledger.get_balance(conn, tenant_id, wallet_id)
    return MovementResponse(
        transaction_id=transaction_id,
        wallet_id=wallet_id,
        kind=kind,
        amount_minor=body.amount_minor,
        currency=body.currency,
        reference=body.reference,
        balance_minor=balance.amount_minor,
    )


# Cursors are opaque to clients, so the pagination key can change without breaking them.
def _encode_cursor(posting_id: UUID) -> str:
    return base64.urlsafe_b64encode(posting_id.bytes).decode().rstrip("=")


def _decode_cursor(cursor: str) -> UUID:
    try:
        return UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise InvalidCursor(cursor) from exc
