from collections.abc import Callable
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine

from ledger_api import ledger
from ledger_api.money import Money

# Two tenants and two currencies, so platform accounts are shared across tenants
# and a currency with no minor unit (JPY) is exercised.
WALLETS = [("tenant-a", "EUR"), ("tenant-a", "JPY"), ("tenant-b", "EUR")]

operations = st.lists(
    st.tuples(
        # "pay_all" pays the wallet's exact balance: random amounts almost never hit
        # that boundary, and an off-by-one in the funds check hides there.
        st.sampled_from(["top_up", "pay", "pay_all"]),
        st.integers(min_value=0, max_value=len(WALLETS) - 1),
        st.integers(min_value=1, max_value=10_000),
    ),
    max_size=30,
)


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
    expected = [0] * len(wallets)

    for op, index, amount_minor in ops:
        tenant_id, wallet_id, currency = wallets[index]
        if op == "pay_all":
            op, amount_minor = "pay", max(expected[index], 1)
        amount = Money(amount_minor, currency)
        try:
            with engine.begin() as conn:
                if op == "top_up":
                    ledger.top_up(conn, tenant_id, wallet_id, amount)
                else:
                    ledger.pay(conn, tenant_id, wallet_id, amount)
        except ledger.InsufficientFunds:
            # Rejected exactly when the wallet could not cover it.
            assert op == "pay"
            assert expected[index] < amount_minor
        else:
            expected[index] += amount_minor if op == "top_up" else -amount_minor

    with engine.connect() as conn:
        actual = [ledger.get_balance(conn, t, w).amount_minor for t, w, _ in wallets]
    assert actual == expected
    assert ledger_violations() == []
