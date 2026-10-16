import threading
from collections import Counter
from typing import TYPE_CHECKING
from uuid import UUID

import httpx2
from sqlalchemy import Engine

from ledger_api import ledger, outbox
from ledger_api.money import Money

if TYPE_CHECKING:
    from tests.conftest import Receiver

WORKERS = 4
EVENTS = 100


def test_parallel_workers_deliver_every_event_exactly_once_when_nothing_fails(
    db: Engine, webhook_tenant: tuple[UUID, str], receiver: Receiver
) -> None:
    tenant_id, _ = webhook_tenant
    with db.begin() as conn:
        wallet = ledger.open_wallet(conn, tenant_id, "EUR")
    for _ in range(EVENTS):
        with db.begin() as conn:
            ledger.top_up(conn, tenant_id, wallet, Money(1, "EUR"))

    barrier = threading.Barrier(WORKERS)

    def work() -> None:
        with httpx2.Client(timeout=5) as client:
            barrier.wait()
            while outbox.relay_batch(db, client, limit=5) > 0:
                pass

    threads = [threading.Thread(target=work) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    deliveries = Counter(headers["Ledger-Event-Id"] for headers, _ in receiver.requests)
    assert len(deliveries) == EVENTS
    assert set(deliveries.values()) == {1}
