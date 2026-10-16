from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine

from ledger_api import fx, ledger
from ledger_api.money import Money, convert

# Two tenants share platform accounts; JPY has no minor unit. Conversions use tenant-a's wallets.
WALLETS = [("tenant-a", "EUR"), ("tenant-a", "JPY"), ("tenant-b", "EUR")]
EUR_A, JPY_A = 0, 1
STARTING_BALANCE = 20_000

operations = st.lists(
    st.tuples(
        # "pay_all" and "refund_rest" hit the exact boundaries (whole balance, whole remaining
        # refund) that random amounts almost never reach, where off-by-one errors hide.
        st.sampled_from(
            ["top_up", "pay", "pay_all", "refund", "refund_rest", "eur_to_jpy", "jpy_to_eur"]
        ),
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=1, max_value=10_000),
    ),
    max_size=40,
)


@dataclass
class Payment:
    wallet: int
    id: UUID
    paid: int
    refunded: int = 0


@settings(max_examples=100, deadline=None)
@given(ops=operations)
def test_random_operations_keep_the_ledger_consistent(
    engine: Engine,
    reset_db: Callable[[], None],
    create_tenant: Callable[[str], UUID],
    ledger_violations: Callable[[], list[str]],
    ops: list[tuple[str, int, int]],
) -> None:
    reset_db()
    tenants = {name: create_tenant(name) for name in {name for name, _ in WALLETS}}
    with engine.begin() as conn:
        wallets = [
            (tenants[name], ledger.open_wallet(conn, tenants[name], currency), currency)
            for name, currency in WALLETS
        ]
        # Funded so payments mostly succeed and refunds have payments; "pay_all" still hits zero.
        for tenant_id, wallet_id, currency in wallets:
            ledger.top_up(conn, tenant_id, wallet_id, Money(STARTING_BALANCE, currency))
    # A simple model of what every balance should be, checked against the database at the end.
    expected = [STARTING_BALANCE] * len(wallets)
    payments: list[Payment] = []

    for op, index, amount_minor in ops:
        if op in ("top_up", "pay", "pay_all"):
            w = index % len(wallets)
            tenant_id, wallet_id, currency = wallets[w]
            if op == "pay_all":
                op, amount_minor = "pay", max(expected[w], 1)
            amount = Money(amount_minor, currency)
            try:
                with engine.begin() as conn:
                    if op == "top_up":
                        ledger.top_up(conn, tenant_id, wallet_id, amount)
                    else:
                        payment_id = ledger.pay(conn, tenant_id, wallet_id, amount)
            except ledger.InsufficientFunds:
                # Rejected exactly when the wallet could not cover it.
                assert op == "pay"
                assert expected[w] < amount_minor
            else:
                if op == "pay":
                    assert expected[w] >= amount_minor
                    payments.append(Payment(w, payment_id, amount_minor))
                expected[w] += amount_minor if op == "top_up" else -amount_minor

        elif op in ("refund", "refund_rest") and payments:
            payment = payments[index % len(payments)]
            tenant_id, wallet_id, currency = wallets[payment.wallet]
            if op == "refund_rest":
                amount_minor = max(payment.paid - payment.refunded, 1)
            try:
                with engine.begin() as conn:
                    ledger.refund(
                        conn, tenant_id, wallet_id, payment.id, Money(amount_minor, currency)
                    )
            except ledger.RefundExceedsPayment:
                assert payment.refunded + amount_minor > payment.paid
            else:
                assert payment.refunded + amount_minor <= payment.paid
                payment.refunded += amount_minor
                expected[payment.wallet] += amount_minor

        elif op in ("eur_to_jpy", "jpy_to_eur"):
            sell, buy = (EUR_A, JPY_A) if op == "eur_to_jpy" else (JPY_A, EUR_A)
            tenant_id = wallets[sell][0]
            with engine.begin() as conn:
                quote = fx.create_quote(conn, tenant_id, wallets[sell][2], wallets[buy][2])
            bought = convert(Money(amount_minor, wallets[sell][2]), wallets[buy][2], quote.rate)
            try:
                with engine.begin() as conn:
                    ledger.convert_currency(conn, tenant_id, quote.id, amount_minor)
            except ledger.ConversionRoundsToZero:
                assert bought.amount_minor == 0
            except ledger.InsufficientFunds:
                assert expected[sell] < amount_minor
            else:
                assert expected[sell] >= amount_minor
                expected[sell] -= amount_minor
                expected[buy] += bought.amount_minor

    with engine.connect() as conn:
        actual = [ledger.get_balance(conn, t, w).amount_minor for t, w, _ in wallets]
    assert actual == expected
    assert ledger_violations() == []
