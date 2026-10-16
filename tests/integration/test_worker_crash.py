import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Engine, text

from ledger_api import ledger, outbox
from ledger_api.money import Money

if TYPE_CHECKING:
    from tests.conftest import Receiver


def start_worker(db: Engine) -> subprocess.Popen[bytes]:
    """The real worker, as its own process, so it can be killed like a crashing container."""
    env = {
        **os.environ,
        "DATABASE_URL": db.url.render_as_string(hide_password=False),
        "WORKER_POLL_SECONDS": "0.1",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "ledger_api.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for(condition: Callable[[], bool], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def sent_at(db: Engine) -> object:
    with db.connect() as conn:
        return conn.execute(text("SELECT sent_at FROM outbox")).scalar_one()


def test_event_survives_the_worker_being_killed_mid_delivery(
    db: Engine, webhook_tenant: tuple[UUID, str], receiver: Receiver
) -> None:
    tenant_id, secret = webhook_tenant
    with db.begin() as conn:
        wallet = ledger.open_wallet(conn, tenant_id, "EUR")
        ledger.top_up(conn, tenant_id, wallet, Money(10_00, "EUR"))

    # The endpoint hangs: the first worker is caught mid-delivery, holding the row lock.
    receiver.respond.clear()
    first = start_worker(db)
    assert receiver.received.wait(timeout=15)
    first.send_signal(signal.SIGKILL)
    first.wait()
    receiver.respond.set()

    # Its transaction died with it, so the event is still pending, not lost or marked sent.
    assert sent_at(db) is None

    second = start_worker(db)
    wait_for(lambda: sent_at(db) is not None)
    second.send_signal(signal.SIGTERM)
    assert second.wait(timeout=10) == 0  # SIGTERM is a clean shutdown

    # At least once: the receiver may see the same event twice, with the same id to dedupe on.
    assert len(receiver.requests) == 2
    event_ids = {headers["Ledger-Event-Id"] for headers, _ in receiver.requests}
    assert len(event_ids) == 1
    for headers, body in receiver.requests:
        assert outbox.verify_signature(secret, headers["Ledger-Signature"], body, int(time.time()))
