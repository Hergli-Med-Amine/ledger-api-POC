from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

# Strict: "100" or 100.0 is rejected rather than silently coerced into an amount.
# Capped at 2^53 - 1, the largest integer a JavaScript client can hold exactly.
AmountMinor = Annotated[StrictInt, Field(gt=0, le=2**53 - 1, examples=[1250])]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["EUR"])]


class StrictRequest(BaseModel):
    # A misspelt field ("ammount_minor") is an error, not silently ignored.
    model_config = ConfigDict(extra="forbid")


class OpenWalletRequest(StrictRequest):
    currency: Currency


class MovementRequest(StrictRequest):
    amount_minor: AmountMinor
    currency: Currency
    reference: Annotated[str | None, Field(max_length=140, examples=["INV-2026-0042"])] = None


class WalletResponse(BaseModel):
    id: UUID
    currency: str
    balance_minor: int


class WalletListResponse(BaseModel):
    data: list[WalletResponse]


class MovementResponse(BaseModel):
    transaction_id: UUID
    wallet_id: UUID
    kind: str
    amount_minor: int
    currency: str
    reference: str | None
    balance_minor: int = Field(description="Wallet balance after this transaction.")


class StatementLineResponse(BaseModel):
    posting_id: UUID
    transaction_id: UUID
    kind: str
    amount_minor: int = Field(description="Signed: positive credits the wallet, negative debits.")
    currency: str
    reference: str | None
    created_at: datetime


class StatementResponse(BaseModel):
    data: list[StatementLineResponse]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null at end.")


class Problem(BaseModel):
    """RFC 9457 problem details, returned for every error."""

    type: str = Field(examples=["/problems/insufficient-funds"])
    title: str
    status: int
    detail: str | None = None


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    # Schema given inline: FastAPI's `model` key would document it as application/json.
    content = {"application/problem+json": {"schema": Problem.model_json_schema()}}
    return {
        status: {"description": HTTPStatus(status).phrase, "content": content}
        for status in statuses
    }


class QuoteRequest(StrictRequest):
    from_currency: Currency
    to_currency: Currency


class QuoteResponse(BaseModel):
    id: UUID
    from_currency: str
    to_currency: str
    rate: Decimal = Field(
        description="Units of to_currency per unit of from_currency, as a string."
    )
    expires_at: datetime


class ConversionRequest(StrictRequest):
    quote_id: UUID
    sell_amount_minor: AmountMinor


class ConversionResponse(BaseModel):
    transaction_id: UUID
    quote_id: UUID
    rate: Decimal
    from_wallet_id: UUID
    sold_amount_minor: int
    sold_currency: str
    to_wallet_id: UUID
    bought_amount_minor: int = Field(description="Rounded half-even to the minor unit.")
    bought_currency: str
