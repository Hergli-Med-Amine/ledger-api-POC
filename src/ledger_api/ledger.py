from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from ledger_api.money import CURRENCY_EXPONENTS, Money, UnknownCurrency

# Every function here runs inside the caller's database transaction and never commits:
# the caller decides what else (idempotency key, outbox row) commits atomically with it.


class LedgerError(Exception):
    pass


class WalletNotFound(LedgerError):
    pass


class CurrencyMismatch(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class NonPositiveAmount(LedgerError):
    pass


class WalletAlreadyExists(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class Wallet:
    id: UUID
    balance: Money


@dataclass(frozen=True, slots=True)
class StatementLine:
    posting_id: UUID
    transaction_id: UUID
    kind: str
    amount: Money
    reference: str | None
    created_at: datetime


def open_wallet(conn: Connection, tenant_id: UUID, currency: str) -> UUID:
    if currency not in CURRENCY_EXPONENTS:
        raise UnknownCurrency(currency)
    wallet_id: UUID | None = conn.execute(
        text("""
            INSERT INTO accounts (tenant_id, currency, kind)
            VALUES (:tenant_id, :currency, 'wallet')
            ON CONFLICT (tenant_id, currency) WHERE kind = 'wallet' DO NOTHING
            RETURNING id
        """),
        {"tenant_id": tenant_id, "currency": currency},
    ).scalar_one_or_none()
    if wallet_id is None:
        raise WalletAlreadyExists(f"tenant already has a {currency} wallet")
    conn.execute(
        text("INSERT INTO balances (account_id, amount_minor) VALUES (:id, 0)"),
        {"id": wallet_id},
    )
    return wallet_id


def get_balance(conn: Connection, tenant_id: UUID, wallet_id: UUID) -> Money:
    row = conn.execute(
        text("""
            SELECT b.amount_minor, a.currency
            FROM balances b JOIN accounts a ON a.id = b.account_id
            WHERE b.account_id = :wallet_id AND a.tenant_id = :tenant_id
        """),
        {"wallet_id": wallet_id, "tenant_id": tenant_id},
    ).one_or_none()
    if row is None:
        raise WalletNotFound(f"no wallet {wallet_id}")
    return Money(row.amount_minor, row.currency)


def list_wallets(conn: Connection, tenant_id: UUID) -> list[Wallet]:
    rows = conn.execute(
        text("""
            SELECT a.id, a.currency, b.amount_minor
            FROM accounts a JOIN balances b ON b.account_id = a.id
            WHERE a.tenant_id = :tenant_id
            ORDER BY a.currency
        """),
        {"tenant_id": tenant_id},
    ).all()
    return [Wallet(row.id, Money(row.amount_minor, row.currency)) for row in rows]


def statement(
    conn: Connection, tenant_id: UUID, wallet_id: UUID, limit: int, before: UUID | None
) -> list[StatementLine]:
    """Newest first. Pass the last line's posting_id as `before` to get the next page."""
    get_balance(conn, tenant_id, wallet_id)  # raises WalletNotFound for another tenant's wallet
    # Keyset pagination on the time-ordered UUIDv7 id: unlike OFFSET, rows inserted while a
    # client pages through do not shift later pages, and each page is one index range scan.
    rows = conn.execute(
        text("""
            SELECT p.id, p.transaction_id, t.kind, p.amount_minor, p.currency,
                   t.reference, t.created_at
            FROM postings p JOIN transactions t ON t.id = p.transaction_id
            WHERE p.account_id = :wallet_id AND p.id < :before
            ORDER BY p.id DESC
            LIMIT :limit
        """),
        {"wallet_id": wallet_id, "before": before or UUID(int=2**128 - 1), "limit": limit},
    ).all()
    return [
        StatementLine(
            posting_id=row.id,
            transaction_id=row.transaction_id,
            kind=row.kind,
            amount=Money(row.amount_minor, row.currency),
            reference=row.reference,
            created_at=row.created_at,
        )
        for row in rows
    ]


def top_up(
    conn: Connection,
    tenant_id: UUID,
    wallet_id: UUID,
    amount: Money,
    reference: str | None = None,
) -> UUID:
    _require_positive(amount)
    _lock_wallet(conn, tenant_id, wallet_id, amount.currency)
    funding_id = _platform_account(conn, "external_funding", amount.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "top_up",
        reference,
        [(wallet_id, amount), (funding_id, Money(-amount.amount_minor, amount.currency))],
    )
    _add_to_balance(conn, wallet_id, amount.amount_minor)
    return transaction_id


def pay(
    conn: Connection,
    tenant_id: UUID,
    wallet_id: UUID,
    amount: Money,
    reference: str | None = None,
) -> UUID:
    _require_positive(amount)
    balance = _lock_wallet(conn, tenant_id, wallet_id, amount.currency)
    # Checked only after the lock, so no concurrent payment can spend the same funds.
    if balance < amount.amount_minor:
        raise InsufficientFunds(f"balance is {balance}, payment needs {amount.amount_minor}")
    payable_id = _platform_account(conn, "supplier_payable", amount.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "payment",
        reference,
        [(wallet_id, Money(-amount.amount_minor, amount.currency)), (payable_id, amount)],
    )
    _add_to_balance(conn, wallet_id, -amount.amount_minor)
    return transaction_id


def _require_positive(amount: Money) -> None:
    if amount.amount_minor <= 0:
        raise NonPositiveAmount(f"got {amount.amount_minor}")


def _lock_wallet(conn: Connection, tenant_id: UUID, wallet_id: UUID, currency: str) -> int:
    row = conn.execute(
        text("""
            SELECT b.amount_minor, a.currency
            FROM balances b JOIN accounts a ON a.id = b.account_id
            WHERE b.account_id = :wallet_id AND a.tenant_id = :tenant_id
            FOR UPDATE OF b
        """),
        {"wallet_id": wallet_id, "tenant_id": tenant_id},
    ).one_or_none()
    # Another tenant's wallet looks the same as a missing one, so its existence is not leaked.
    if row is None:
        raise WalletNotFound(f"no wallet {wallet_id}")
    if row.currency != currency:
        raise CurrencyMismatch(f"wallet is {row.currency}, amount is {currency}")
    balance: int = row.amount_minor
    return balance


def _platform_account(conn: Connection, kind: str, currency: str) -> UUID:
    # Created on first use. ON CONFLICT makes two concurrent first uses agree on one row.
    conn.execute(
        text("""
            INSERT INTO accounts (tenant_id, currency, kind) VALUES (NULL, :currency, :kind)
            ON CONFLICT (kind, currency) WHERE tenant_id IS NULL DO NOTHING
        """),
        {"kind": kind, "currency": currency},
    )
    account_id: UUID = conn.execute(
        text("SELECT id FROM accounts WHERE tenant_id IS NULL AND kind = :kind AND currency = :c"),
        {"kind": kind, "c": currency},
    ).scalar_one()
    return account_id


def _post(
    conn: Connection,
    tenant_id: UUID,
    kind: str,
    reference: str | None,
    legs: Sequence[tuple[UUID, Money]],
) -> UUID:
    totals: Counter[str] = Counter()
    for _, money in legs:
        totals[money.currency] += money.amount_minor
    if any(totals.values()):
        raise ValueError(f"unbalanced legs: {dict(totals)}")

    transaction_id: UUID = conn.execute(
        text("""
            INSERT INTO transactions (tenant_id, kind, reference)
            VALUES (:tenant_id, :kind, :reference)
            RETURNING id
        """),
        {"tenant_id": tenant_id, "kind": kind, "reference": reference},
    ).scalar_one()
    conn.execute(
        text("""
            INSERT INTO postings (transaction_id, account_id, amount_minor, currency)
            VALUES (:transaction_id, :account_id, :amount_minor, :currency)
        """),
        [
            {
                "transaction_id": transaction_id,
                "account_id": account_id,
                "amount_minor": money.amount_minor,
                "currency": money.currency,
            }
            for account_id, money in legs
        ],
    )
    return transaction_id


def _add_to_balance(conn: Connection, account_id: UUID, delta_minor: int) -> None:
    conn.execute(
        text("UPDATE balances SET amount_minor = amount_minor + :delta WHERE account_id = :id"),
        {"delta": delta_minor, "id": account_id},
    )
