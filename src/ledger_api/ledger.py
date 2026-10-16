from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from ledger_api import fx, outbox
from ledger_api.money import CURRENCY_EXPONENTS, Money, UnknownCurrency, convert

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


class PaymentNotFound(LedgerError):
    pass


class RefundExceedsPayment(LedgerError):
    pass


class ConversionRoundsToZero(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class Conversion:
    transaction_id: UUID
    from_wallet_id: UUID
    to_wallet_id: UUID
    sold: Money
    bought: Money
    rate: Decimal


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
    # Keyset pagination: unlike OFFSET, rows added while a client pages do not shift its pages.
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
        [(wallet_id, amount), (funding_id, Money(-amount.amount_minor, amount.currency))],
        reference=reference,
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
        [(wallet_id, Money(-amount.amount_minor, amount.currency)), (payable_id, amount)],
        reference=reference,
    )
    _add_to_balance(conn, wallet_id, -amount.amount_minor)
    return transaction_id


def refund(
    conn: Connection,
    tenant_id: UUID,
    wallet_id: UUID,
    payment_id: UUID,
    amount: Money,
    reference: str | None = None,
) -> UUID:
    _require_positive(amount)
    # Locking the payment serialises its refunds, so together they cannot exceed it.
    paid = conn.execute(
        text("""
            SELECT -p.amount_minor AS amount_minor, p.currency
            FROM transactions t JOIN postings p ON p.transaction_id = t.id
            WHERE t.id = :payment_id AND t.tenant_id = :tenant_id AND t.kind = 'payment'
              AND p.account_id = :wallet_id
            FOR UPDATE OF t
        """),
        {"payment_id": payment_id, "tenant_id": tenant_id, "wallet_id": wallet_id},
    ).one_or_none()
    if paid is None:
        raise PaymentNotFound(f"no payment {payment_id} from wallet {wallet_id}")
    if paid.currency != amount.currency:
        raise CurrencyMismatch(f"payment is {paid.currency}, refund is {amount.currency}")
    refunded: int = conn.execute(
        text("""
            SELECT COALESCE(SUM(p.amount_minor), 0)
            FROM transactions t JOIN postings p ON p.transaction_id = t.id
            WHERE t.refund_of = :payment_id AND p.account_id = :wallet_id
        """),
        {"payment_id": payment_id, "wallet_id": wallet_id},
    ).scalar_one()
    if refunded + amount.amount_minor > paid.amount_minor:
        raise RefundExceedsPayment(
            f"paid {paid.amount_minor}, already refunded {refunded}, asked {amount.amount_minor}"
        )
    _lock_wallet(conn, tenant_id, wallet_id, amount.currency)
    payable_id = _platform_account(conn, "supplier_payable", amount.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "refund",
        [(wallet_id, amount), (payable_id, Money(-amount.amount_minor, amount.currency))],
        reference=reference,
        refund_of=payment_id,
    )
    _add_to_balance(conn, wallet_id, amount.amount_minor)
    return transaction_id


def convert_currency(
    conn: Connection, tenant_id: UUID, quote_id: UUID, sell_amount_minor: int
) -> Conversion:
    """Moves money between the tenant's wallets in the quote's two currencies."""
    quote = fx.use_quote(conn, tenant_id, quote_id)
    sold = Money(sell_amount_minor, quote.from_currency)
    _require_positive(sold)
    bought = convert(sold, quote.to_currency, quote.rate)
    if bought.amount_minor == 0:
        raise ConversionRoundsToZero(f"{sell_amount_minor} {sold.currency} is too small")
    from_wallet = _wallet_for_currency(conn, tenant_id, quote.from_currency)
    to_wallet = _wallet_for_currency(conn, tenant_id, quote.to_currency)
    balances = _lock_wallets(conn, tenant_id, [from_wallet, to_wallet])
    if balances[from_wallet].amount_minor < sold.amount_minor:
        raise InsufficientFunds(
            f"balance is {balances[from_wallet].amount_minor}, conversion needs {sold.amount_minor}"
        )
    # fx_pool pays out the rounded amount, so the rounding residue stays with it (ADR 1).
    pool_from = _platform_account(conn, "fx_pool", sold.currency)
    pool_to = _platform_account(conn, "fx_pool", bought.currency)
    transaction_id = _post(
        conn,
        tenant_id,
        "conversion",
        [
            (from_wallet, Money(-sold.amount_minor, sold.currency)),
            (pool_from, sold),
            (pool_to, Money(-bought.amount_minor, bought.currency)),
            (to_wallet, bought),
        ],
        quote_id=quote_id,
    )
    _add_to_balance(conn, from_wallet, -sold.amount_minor)
    _add_to_balance(conn, to_wallet, bought.amount_minor)
    return Conversion(transaction_id, from_wallet, to_wallet, sold, bought, quote.rate)


def _require_positive(amount: Money) -> None:
    if amount.amount_minor <= 0:
        raise NonPositiveAmount(f"got {amount.amount_minor}")


def _lock_wallet(conn: Connection, tenant_id: UUID, wallet_id: UUID, currency: str) -> int:
    balance = _lock_wallets(conn, tenant_id, [wallet_id])[wallet_id]
    if balance.currency != currency:
        raise CurrencyMismatch(f"wallet is {balance.currency}, amount is {currency}")
    return balance.amount_minor


def _lock_wallets(conn: Connection, tenant_id: UUID, wallet_ids: list[UUID]) -> dict[UUID, Money]:
    # Ascending id order, so two transactions locking the same wallets cannot deadlock.
    rows = conn.execute(
        text("""
            SELECT b.account_id, b.amount_minor, a.currency
            FROM balances b JOIN accounts a ON a.id = b.account_id
            WHERE b.account_id = ANY(:wallet_ids) AND a.tenant_id = :tenant_id
            ORDER BY b.account_id
            FOR UPDATE OF b
        """),
        {"wallet_ids": wallet_ids, "tenant_id": tenant_id},
    ).all()
    balances = {row.account_id: Money(row.amount_minor, row.currency) for row in rows}
    # Another tenant's wallet looks the same as a missing one, so its existence is not leaked.
    for wallet_id in wallet_ids:
        if wallet_id not in balances:
            raise WalletNotFound(f"no wallet {wallet_id}")
    return balances


def _wallet_for_currency(conn: Connection, tenant_id: UUID, currency: str) -> UUID:
    wallet_id: UUID | None = conn.execute(
        text("""
            SELECT id FROM accounts
            WHERE tenant_id = :tenant_id AND currency = :currency AND kind = 'wallet'
        """),
        {"tenant_id": tenant_id, "currency": currency},
    ).scalar_one_or_none()
    if wallet_id is None:
        raise WalletNotFound(f"no {currency} wallet")
    return wallet_id


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
    legs: Sequence[tuple[UUID, Money]],
    *,
    reference: str | None = None,
    refund_of: UUID | None = None,
    quote_id: UUID | None = None,
) -> UUID:
    totals: Counter[str] = Counter()
    for _, money in legs:
        totals[money.currency] += money.amount_minor
    if any(totals.values()):
        raise ValueError(f"unbalanced legs: {dict(totals)}")

    transaction = conn.execute(
        text("""
            INSERT INTO transactions (tenant_id, kind, reference, refund_of, quote_id)
            VALUES (:tenant_id, :kind, :reference, :refund_of, :quote_id)
            RETURNING id, created_at
        """),
        {
            "tenant_id": tenant_id,
            "kind": kind,
            "reference": reference,
            "refund_of": refund_of,
            "quote_id": quote_id,
        },
    ).one()
    transaction_id: UUID = transaction.id
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
    wallet_ids = set(
        conn.execute(
            text("SELECT id FROM accounts WHERE id = ANY(:ids) AND kind = 'wallet'"),
            {"ids": [account_id for account_id, _ in legs]},
        ).scalars()
    )
    # Written in this same transaction: the event exists if and only if the money moved.
    outbox.enqueue(
        conn,
        tenant_id,
        f"{kind}.created",
        {
            "transaction_id": str(transaction_id),
            "kind": kind,
            "reference": reference,
            "refund_of": str(refund_of) if refund_of else None,
            "quote_id": str(quote_id) if quote_id else None,
            "created_at": transaction.created_at.isoformat(),
            "wallets": [
                {
                    "wallet_id": str(account_id),
                    "amount_minor": m.amount_minor,
                    "currency": m.currency,
                }
                for account_id, m in legs
                if account_id in wallet_ids
            ],
        },
    )
    return transaction_id


def _add_to_balance(conn: Connection, account_id: UUID, delta_minor: int) -> None:
    conn.execute(
        text("UPDATE balances SET amount_minor = amount_minor + :delta WHERE account_id = :id"),
        {"delta": delta_minor, "id": account_id},
    )
