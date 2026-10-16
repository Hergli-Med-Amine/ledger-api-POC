import json
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import Engine, text

from ledger_api import ledger, outbox
from ledger_api.money import Money

if TYPE_CHECKING:
    from tests.conftest import Receiver


@pytest.fixture
def http() -> Iterator[httpx2.Client]:
    with httpx2.Client(timeout=5) as client:
        yield client


@pytest.fixture
def wallet(db: Engine, webhook_tenant: tuple[UUID, str]) -> UUID:
    tenant_id, _ = webhook_tenant
    with db.begin() as conn:
        return ledger.open_wallet(conn, tenant_id, "EUR")


def outbox_rows(db: Engine) -> list[dict[str, object]]:
    with db.connect() as conn:
        rows = conn.execute(text("SELECT * FROM outbox ORDER BY id")).mappings().all()
    return [dict(row) for row in rows]


def test_an_event_is_written_with_each_money_movement_and_only_then(
    db: Engine,
    webhook_tenant: tuple[UUID, str],
    wallet: UUID,
    create_tenant: Callable[[str], UUID],
) -> None:
    tenant_id, _ = webhook_tenant
    with db.begin() as conn:
        top_up_id = ledger.top_up(conn, tenant_id, wallet, Money(10_00, "EUR"), "bank-ref")
    with pytest.raises(ledger.InsufficientFunds), db.begin() as conn:
        ledger.pay(conn, tenant_id, wallet, Money(99_00, "EUR"))
    # A tenant without a webhook endpoint gets no events at all.
    silent = create_tenant("silent")
    with db.begin() as conn:
        silent_wallet = ledger.open_wallet(conn, silent, "EUR")
        ledger.top_up(conn, silent, silent_wallet, Money(1_00, "EUR"))

    rows = outbox_rows(db)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "top_up.created"
    payload = rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["transaction_id"] == str(top_up_id)
    assert payload["reference"] == "bank-ref"
    assert payload["wallets"] == [
        {"wallet_id": str(wallet), "amount_minor": 10_00, "currency": "EUR"}
    ]


def test_relay_delivers_a_signed_event_once(
    db: Engine,
    webhook_tenant: tuple[UUID, str],
    wallet: UUID,
    receiver: Receiver,
    http: httpx2.Client,
) -> None:
    tenant_id, secret = webhook_tenant
    with db.begin() as conn:
        ledger.top_up(conn, tenant_id, wallet, Money(10_00, "EUR"))

    assert outbox.relay_batch(db, http) == 1
    assert outbox.relay_batch(db, http) == 0

    [(headers, body)] = receiver.requests
    assert outbox.verify_signature(secret, headers["Ledger-Signature"], body, int(time.time()))
    event = json.loads(body)
    assert event["id"] == headers["Ledger-Event-Id"]
    assert event["type"] == "top_up.created"
    assert outbox_rows(db)[0]["sent_at"] is not None


def test_failed_deliveries_back_off_exponentially_then_stop(
    db: Engine,
    webhook_tenant: tuple[UUID, str],
    wallet: UUID,
    receiver: Receiver,
    http: httpx2.Client,
) -> None:
    tenant_id, _ = webhook_tenant
    with db.begin() as conn:
        ledger.top_up(conn, tenant_id, wallet, Money(10_00, "EUR"))
    receiver.status = 500

    delays = []
    for _ in range(outbox.MAX_ATTEMPTS):
        assert outbox.relay_batch(db, http) == 1
        assert outbox.relay_batch(db, http) == 0  # not due again until the backoff passes
        with db.begin() as conn:
            delay = conn.execute(
                text("SELECT EXTRACT(EPOCH FROM next_attempt_at - now()) FROM outbox")
            ).scalar_one()
            delays.append(round(delay))
            # Skip the wait instead of sleeping through it.
            conn.execute(text("UPDATE outbox SET next_attempt_at = now()"))

    row = outbox_rows(db)[0]
    assert delays[:-1] == [outbox.backoff_seconds(n) for n in range(1, outbox.MAX_ATTEMPTS)]
    assert delays[:4] == [2, 4, 8, 16]
    assert row["attempts"] == outbox.MAX_ATTEMPTS
    assert row["dead_at"] is not None and row["sent_at"] is None
    assert row["last_error"] == "HTTP 500"
    assert outbox.relay_batch(db, http) == 0  # dead events are not retried
    assert len(receiver.requests) == outbox.MAX_ATTEMPTS
