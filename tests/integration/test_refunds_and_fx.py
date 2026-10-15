from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from ledger_api import fx, ledger
from ledger_api.money import Money


@pytest.fixture
def tenant(db: Engine, create_tenant: Callable[[str], UUID]) -> UUID:
    return create_tenant("acme")


@pytest.fixture
def wallets(db: Engine, tenant: UUID) -> dict[str, UUID]:
    with db.begin() as conn:
        eur = ledger.open_wallet(conn, tenant, "EUR")
        jpy = ledger.open_wallet(conn, tenant, "JPY")
        ledger.top_up(conn, tenant, eur, Money(100_00, "EUR"))
    return {"EUR": eur, "JPY": jpy}


def balance(db: Engine, tenant: UUID, wallet_id: UUID) -> int:
    with db.connect() as conn:
        return ledger.get_balance(conn, tenant, wallet_id).amount_minor


# --- refunds ---


@pytest.fixture
def payment(db: Engine, tenant: UUID, wallets: dict[str, UUID]) -> UUID:
    with db.begin() as conn:
        return ledger.pay(conn, tenant, wallets["EUR"], Money(30_00, "EUR"))


def refund(db: Engine, tenant: UUID, wallet_id: UUID, payment_id: UUID, amount: int) -> UUID:
    with db.begin() as conn:
        return ledger.refund(conn, tenant, wallet_id, payment_id, Money(amount, "EUR"))


def test_partial_refunds_up_to_the_payment_and_no_further(
    db: Engine, tenant: UUID, wallets: dict[str, UUID], payment: UUID
) -> None:
    refund(db, tenant, wallets["EUR"], payment, 10_00)
    refund(db, tenant, wallets["EUR"], payment, 20_00)
    assert balance(db, tenant, wallets["EUR"]) == 100_00

    with pytest.raises(ledger.RefundExceedsPayment):
        refund(db, tenant, wallets["EUR"], payment, 1)


def test_single_refund_larger_than_the_payment_is_rejected(
    db: Engine, tenant: UUID, wallets: dict[str, UUID], payment: UUID
) -> None:
    with pytest.raises(ledger.RefundExceedsPayment):
        refund(db, tenant, wallets["EUR"], payment, 30_01)
    assert balance(db, tenant, wallets["EUR"]) == 70_00


def test_only_payments_of_this_tenant_and_wallet_can_be_refunded(
    db: Engine,
    tenant: UUID,
    wallets: dict[str, UUID],
    payment: UUID,
    create_tenant: Callable[[str], UUID],
) -> None:
    with db.begin() as conn:
        top_up = ledger.top_up(conn, tenant, wallets["EUR"], Money(1_00, "EUR"))
    intruder = create_tenant("intruder")

    with pytest.raises(ledger.PaymentNotFound):
        refund(db, tenant, wallets["EUR"], top_up, 1_00)
    with pytest.raises(ledger.PaymentNotFound):
        refund(db, intruder, wallets["EUR"], payment, 1_00)
    with pytest.raises(ledger.PaymentNotFound):
        refund(db, tenant, wallets["JPY"], payment, 1_00)


# --- FX ---


def quote(db: Engine, tenant: UUID, from_currency: str, to_currency: str) -> fx.Quote:
    with db.begin() as conn:
        return fx.create_quote(conn, tenant, from_currency, to_currency)


def convert(db: Engine, tenant: UUID, quote_id: UUID, amount: int) -> ledger.Conversion:
    with db.begin() as conn:
        return ledger.convert_currency(conn, tenant, quote_id, amount)


def test_conversion_moves_both_wallets_at_the_quoted_rate(
    db: Engine,
    tenant: UUID,
    wallets: dict[str, UUID],
    ledger_violations: Callable[[], list[str]],
) -> None:
    q = quote(db, tenant, "EUR", "JPY")
    assert q.rate == Decimal("162.35")

    result = convert(db, tenant, q.id, 10_00)

    assert (result.sold, result.bought) == (Money(10_00, "EUR"), Money(1624, "JPY"))
    assert balance(db, tenant, wallets["EUR"]) == 90_00
    assert balance(db, tenant, wallets["JPY"]) == 1624
    assert ledger_violations() == []


def test_a_quote_converts_once(db: Engine, tenant: UUID, wallets: dict[str, UUID]) -> None:
    q = quote(db, tenant, "EUR", "JPY")
    convert(db, tenant, q.id, 1_00)

    with pytest.raises(fx.QuoteAlreadyUsed):
        convert(db, tenant, q.id, 1_00)


def test_a_failed_conversion_leaves_the_quote_usable(
    db: Engine, tenant: UUID, wallets: dict[str, UUID]
) -> None:
    q = quote(db, tenant, "EUR", "JPY")

    with pytest.raises(ledger.InsufficientFunds):
        convert(db, tenant, q.id, 100_01)
    convert(db, tenant, q.id, 100_00)

    assert balance(db, tenant, wallets["EUR"]) == 0


def test_an_expired_quote_is_rejected(db: Engine, tenant: UUID, wallets: dict[str, UUID]) -> None:
    q = quote(db, tenant, "EUR", "JPY")
    with db.begin() as conn:
        conn.execute(
            text("UPDATE fx_quotes SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": q.id},
        )

    with pytest.raises(fx.QuoteExpired):
        convert(db, tenant, q.id, 1_00)


def test_amount_that_rounds_to_zero_is_rejected(
    db: Engine, tenant: UUID, wallets: dict[str, UUID]
) -> None:
    with db.begin() as conn:
        kwd = ledger.open_wallet(conn, tenant, "KWD")
        ledger.top_up(conn, tenant, kwd, Money(1, "KWD"))
    q = quote(db, tenant, "KWD", "EUR")

    # 0.001 KWD is 0.0029985 EUR: under half a cent, so it rounds to nothing.
    with pytest.raises(ledger.ConversionRoundsToZero):
        convert(db, tenant, q.id, 1)


def test_unsupported_pair_is_rejected(db: Engine, tenant: UUID) -> None:
    with pytest.raises(fx.UnsupportedCurrencyPair):
        quote(db, tenant, "JPY", "KWD")


def test_database_allows_one_conversion_per_quote(
    db: Engine, tenant: UUID, wallets: dict[str, UUID]
) -> None:
    q = quote(db, tenant, "EUR", "JPY")
    convert(db, tenant, q.id, 1_00)

    # Bypass the code's used_at check: the UNIQUE constraint still refuses a second use.
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO transactions (tenant_id, kind, quote_id)
                VALUES (:tenant, 'conversion', :quote)
            """),
            {"tenant": tenant, "quote": q.id},
        )
