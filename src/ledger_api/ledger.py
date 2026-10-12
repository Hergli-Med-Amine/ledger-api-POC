from collections import Counter
from collections.abc import Sequence
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


def open_wallet(conn: Connection, tenant_id: UUID, currency: str) -> UUID:
    if currency not in CURRENCY_EXPONENTS:
        raise UnknownCurrency(currency)
    wallet_id: UUID = conn.execute(
        text("""
            INSERT INTO accounts (tenant_id, currency, kind)
            VALUES (:tenant_id, :currency, 'wallet')
            RETURNING id
        """),
        {"tenant_id": tenant_id, "currency": currency},
    ).scalar_one()
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
        raise WalletNotFound(wallet_id)
    return Money(row.amount_minor, row.currency)


def top_up(conn: Connection, tenant_id: UUID, wallet_id: UUID, amount: Money) -> UUID:
    _require_positive(amount)
    _lock_wallet(conn, tenant_id, wallet_id, amount.currency)
    funding_id = _platform_account(conn, "external_funding", amount.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "top_up",
        [(wallet_id, amount), (funding_id, Money(-amount.amount_minor, amount.currency))],
    )
    _add_to_balance(conn, wallet_id, amount.amount_minor)
    return transaction_id


def pay(conn: Connection, tenant_id: UUID, wallet_id: UUID, amount: Money) -> UUID:
    _require_positive(amount)
    balance = _lock_wallet(conn, tenant_id, wallet_id, amount.currency)
    # Checked only after the lock, so no concurrent payment can spend the same funds.
    if balance < amount.amount_minor:
        raise InsufficientFunds(wallet_id)
    payable_id = _platform_account(conn, "supplier_payable", amount.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "payment",
        [(wallet_id, Money(-amount.amount_minor, amount.currency)), (payable_id, amount)],
    )
    _add_to_balance(conn, wallet_id, -amount.amount_minor)
    return transaction_id


def _require_positive(amount: Money) -> None:
    if amount.amount_minor <= 0:
        raise NonPositiveAmount(amount.amount_minor)


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
        raise WalletNotFound(wallet_id)
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


def _post(conn: Connection, tenant_id: UUID, kind: str, legs: Sequence[tuple[UUID, Money]]) -> UUID:
    totals: Counter[str] = Counter()
    for _, money in legs:
        totals[money.currency] += money.amount_minor
    if any(totals.values()):
        raise ValueError(f"unbalanced legs: {dict(totals)}")

    transaction_id: UUID = conn.execute(
        text("INSERT INTO transactions (tenant_id, kind) VALUES (:tenant_id, :kind) RETURNING id"),
        {"tenant_id": tenant_id, "kind": kind},
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
