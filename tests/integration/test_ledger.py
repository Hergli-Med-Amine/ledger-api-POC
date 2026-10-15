from collections.abc import Callable
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from ledger_api import ledger
from ledger_api.money import Money


@pytest.fixture
def tenant(db: Engine, create_tenant: Callable[[str], UUID]) -> UUID:
    return create_tenant("acme")


@pytest.fixture
def eur_wallet(db: Engine, tenant: UUID) -> UUID:
    with db.begin() as conn:
        return ledger.open_wallet(conn, tenant, "EUR")


def transaction_count(engine: Engine) -> int:
    with engine.connect() as conn:
        count: int = conn.execute(text("SELECT count(*) FROM transactions")).scalar_one()
    return count


def test_top_up_then_pay_moves_balance(db: Engine, tenant: UUID, eur_wallet: UUID) -> None:
    with db.begin() as conn:
        ledger.top_up(conn, tenant, eur_wallet, Money(10_00, "EUR"))
    with db.begin() as conn:
        ledger.pay(conn, tenant, eur_wallet, Money(3_50, "EUR"))

    with db.connect() as conn:
        assert ledger.get_balance(conn, tenant, eur_wallet) == Money(6_50, "EUR")
    assert transaction_count(db) == 2


def test_payment_above_balance_is_rejected_and_writes_nothing(
    db: Engine, tenant: UUID, eur_wallet: UUID
) -> None:
    with db.begin() as conn:
        ledger.top_up(conn, tenant, eur_wallet, Money(5_00, "EUR"))

    with pytest.raises(ledger.InsufficientFunds), db.begin() as conn:
        ledger.pay(conn, tenant, eur_wallet, Money(5_01, "EUR"))

    with db.connect() as conn:
        assert ledger.get_balance(conn, tenant, eur_wallet) == Money(5_00, "EUR")
    assert transaction_count(db) == 1


def test_amount_in_another_currency_is_rejected(db: Engine, tenant: UUID, eur_wallet: UUID) -> None:
    with pytest.raises(ledger.CurrencyMismatch), db.begin() as conn:
        ledger.top_up(conn, tenant, eur_wallet, Money(100, "USD"))


@pytest.mark.parametrize("amount_minor", [0, -1])
def test_non_positive_amount_is_rejected(
    db: Engine, tenant: UUID, eur_wallet: UUID, amount_minor: int
) -> None:
    with pytest.raises(ledger.NonPositiveAmount), db.begin() as conn:
        ledger.top_up(conn, tenant, eur_wallet, Money(amount_minor, "EUR"))


def test_another_tenants_wallet_is_not_found(
    db: Engine, eur_wallet: UUID, create_tenant: Callable[[str], UUID]
) -> None:
    intruder = create_tenant("intruder")

    with pytest.raises(ledger.WalletNotFound), db.begin() as conn:
        ledger.top_up(conn, intruder, eur_wallet, Money(1_00, "EUR"))
    with pytest.raises(ledger.WalletNotFound), db.connect() as conn:
        ledger.get_balance(conn, intruder, eur_wallet)


def test_one_wallet_per_tenant_per_currency(db: Engine, tenant: UUID, eur_wallet: UUID) -> None:
    with pytest.raises(ledger.WalletAlreadyExists), db.begin() as conn:
        ledger.open_wallet(conn, tenant, "EUR")


def test_database_rejects_unbalanced_transaction_at_commit(
    db: Engine, tenant: UUID, eur_wallet: UUID
) -> None:
    conn = db.connect()
    conn.begin()
    transaction_id = conn.execute(
        text("INSERT INTO transactions (tenant_id, kind) VALUES (:t, 'top_up') RETURNING id"),
        {"t": tenant},
    ).scalar_one()
    # Accepted here: the check is deferred until commit, when all postings should exist.
    conn.execute(
        text("""
            INSERT INTO postings (transaction_id, account_id, amount_minor, currency)
            VALUES (:tx, :account, 100, 'EUR')
        """),
        {"tx": transaction_id, "account": eur_wallet},
    )

    with pytest.raises(IntegrityError, match="does not balance"):
        conn.commit()
    conn.close()
    assert transaction_count(db) == 0


def test_postings_are_append_only(db: Engine, tenant: UUID, eur_wallet: UUID) -> None:
    with db.begin() as conn:
        ledger.top_up(conn, tenant, eur_wallet, Money(1_00, "EUR"))

    with pytest.raises(IntegrityError, match="append-only"), db.begin() as conn:
        conn.execute(text("UPDATE postings SET amount_minor = amount_minor * 2"))
    with pytest.raises(IntegrityError, match="append-only"), db.begin() as conn:
        conn.execute(text("DELETE FROM postings"))


def test_posting_currency_must_match_account(db: Engine, tenant: UUID, eur_wallet: UUID) -> None:
    with pytest.raises(IntegrityError), db.begin() as conn:
        transaction_id = conn.execute(
            text("INSERT INTO transactions (tenant_id, kind) VALUES (:t, 'top_up') RETURNING id"),
            {"t": tenant},
        ).scalar_one()
        conn.execute(
            text("""
                INSERT INTO postings (transaction_id, account_id, amount_minor, currency)
                VALUES (:tx, :account, 100, 'USD')
            """),
            {"tx": transaction_id, "account": eur_wallet},
        )
