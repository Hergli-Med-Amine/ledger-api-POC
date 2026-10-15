from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ledger_api import ledger
from ledger_api.api.auth import MissingScope, Unauthenticated
from ledger_api.money import UnknownCurrency


class InvalidCursor(Exception):
    pass


# Every domain error and its RFC 9457 problem: (status, type slug, title).
PROBLEMS: dict[type[Exception], tuple[int, str, str]] = {
    Unauthenticated: (401, "unauthenticated", "Missing or invalid API key"),
    MissingScope: (403, "missing-scope", "API key lacks the required scope"),
    ledger.WalletNotFound: (404, "wallet-not-found", "Wallet not found"),
    ledger.WalletAlreadyExists: (409, "wallet-exists", "Wallet already exists for this currency"),
    InvalidCursor: (400, "invalid-cursor", "Invalid pagination cursor"),
    UnknownCurrency: (422, "unknown-currency", "Unknown currency"),
    ledger.CurrencyMismatch: (422, "currency-mismatch", "Amount currency differs from wallet's"),
    ledger.NonPositiveAmount: (422, "non-positive-amount", "Amount must be positive"),
    ledger.InsufficientFunds: (422, "insufficient-funds", "Insufficient funds"),
}


def problem(
    status: int,
    title: str,
    type_: str = "about:blank",
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"type": type_, "title": title, "status": status, **extensions}
    if detail:
        body["detail"] = detail
    return JSONResponse(
        jsonable_encoder(body),
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def install(app: FastAPI) -> None:
    for exc_type in PROBLEMS:
        app.add_exception_handler(exc_type, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    status, slug, title = PROBLEMS[type(exc)]
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return problem(status, title, f"/problems/{slug}", str(exc) or None, headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem(422, "Request validation failed", "/problems/validation", errors=exc.errors())


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    # Framework errors such as an unknown route (404) or wrong method (405).
    assert isinstance(exc, HTTPException)
    return problem(exc.status_code, HTTPStatus(exc.status_code).phrase, headers=exc.headers)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # The traceback goes to the server log; the client learns nothing about internals.
    return problem(500, "Internal Server Error")
