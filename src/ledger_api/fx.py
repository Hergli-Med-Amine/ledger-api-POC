"""FX quotes: a fixed rate for one currency pair, valid briefly, usable for one conversion."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import cache
from importlib import resources
from uuid import UUID

from sqlalchemy import Connection, text

QUOTE_TTL_SECONDS = 60


class FxError(Exception):
    pass


class UnsupportedCurrencyPair(FxError):
    pass


class QuoteNotFound(FxError):
    pass


class QuoteExpired(FxError):
    pass


class QuoteAlreadyUsed(FxError):
    pass


@dataclass(frozen=True, slots=True)
class Quote:
    id: UUID
    from_currency: str
    to_currency: str
    rate: Decimal
    expires_at: datetime


def create_quote(conn: Connection, tenant_id: UUID, from_currency: str, to_currency: str) -> Quote:
    rate = _rates().get(f"{from_currency}/{to_currency}")
    if rate is None:
        raise UnsupportedCurrencyPair(f"{from_currency}/{to_currency}")
    # Expiry uses the database clock, the same clock use_quote checks against.
    row = conn.execute(
        text("""
            INSERT INTO fx_quotes (tenant_id, from_currency, to_currency, rate, expires_at)
            VALUES (:tenant_id, :from_currency, :to_currency, :rate,
                    now() + make_interval(secs => :ttl))
            RETURNING id, expires_at
        """),
        {
            "tenant_id": tenant_id,
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "ttl": QUOTE_TTL_SECONDS,
        },
    ).one()
    return Quote(row.id, from_currency, to_currency, rate, row.expires_at)


def use_quote(conn: Connection, tenant_id: UUID, quote_id: UUID) -> Quote:
    """Marks the quote used, inside the caller's transaction; a rollback makes it usable again."""
    # Row lock: of two conversions racing on one quote, the second waits, then sees used_at.
    row = conn.execute(
        text("""
            SELECT id, from_currency, to_currency, rate, expires_at, used_at,
                   expires_at <= now() AS expired
            FROM fx_quotes WHERE id = :id AND tenant_id = :tenant_id
            FOR UPDATE
        """),
        {"id": quote_id, "tenant_id": tenant_id},
    ).one_or_none()
    if row is None:
        raise QuoteNotFound(f"no quote {quote_id}")
    if row.used_at is not None:
        raise QuoteAlreadyUsed(f"quote {quote_id} was used at {row.used_at.isoformat()}")
    if row.expired:
        raise QuoteExpired(f"quote {quote_id} expired at {row.expires_at.isoformat()}")
    conn.execute(text("UPDATE fx_quotes SET used_at = now() WHERE id = :id"), {"id": quote_id})
    return Quote(row.id, row.from_currency, row.to_currency, row.rate, row.expires_at)


@cache
def _rates() -> dict[str, Decimal]:
    raw = json.loads(resources.files("ledger_api").joinpath("fx_rates.json").read_text())
    return {pair: Decimal(rate) for pair, rate in raw.items() if not pair.startswith("_")}
