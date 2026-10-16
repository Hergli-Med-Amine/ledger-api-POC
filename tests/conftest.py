import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from ledger_api import admin
from ledger_api.api.auth import issue_api_key

POSTGRES_IMAGE = "postgres:18.6-alpine"
ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        with mock.patch.dict(os.environ, {"DATABASE_URL": url}):
            command.upgrade(Config(ALEMBIC_INI), "head")
        engine = create_engine(url)
        yield engine
        engine.dispose()


@pytest.fixture(scope="session")
def reset_db(engine: Engine) -> Callable[[], None]:
    def reset() -> None:
        with engine.begin() as conn:
            # CASCADE empties every table that references these, including ones added later.
            conn.execute(text("TRUNCATE tenants, accounts CASCADE"))

    return reset


@pytest.fixture
def db(engine: Engine, reset_db: Callable[[], None]) -> Engine:
    reset_db()
    return engine


@pytest.fixture(scope="session")
def create_tenant(engine: Engine) -> Callable[[str], UUID]:
    def create(name: str) -> UUID:
        with engine.begin() as conn:
            return admin.create_tenant(conn, name)

    return create


@pytest.fixture(scope="session")
def ledger_violations(engine: Engine) -> Callable[[], list[str]]:
    """Checks the ledger's invariants against the database itself; returns what is broken."""

    def check() -> list[str]:
        with engine.connect() as conn:
            unbalanced = conn.execute(
                text("""
                    SELECT transaction_id, currency FROM postings
                    GROUP BY transaction_id, currency HAVING SUM(amount_minor) <> 0
                """)
            ).all()
            drifted = conn.execute(
                text("""
                    SELECT b.account_id FROM balances b
                    LEFT JOIN postings p ON p.account_id = b.account_id
                    GROUP BY b.account_id, b.amount_minor
                    HAVING b.amount_minor <> COALESCE(SUM(p.amount_minor), 0)
                """)
            ).all()
            negative = conn.execute(
                text("SELECT account_id FROM balances WHERE amount_minor < 0")
            ).all()
            over_refunded = conn.execute(
                text("""
                    WITH wallet_legs AS (
                        SELECT t.id, t.kind, t.refund_of, p.amount_minor
                        FROM transactions t
                        JOIN postings p ON p.transaction_id = t.id
                        JOIN accounts a ON a.id = p.account_id AND a.kind = 'wallet'
                    )
                    SELECT paid.id FROM wallet_legs paid
                    JOIN wallet_legs refund ON refund.refund_of = paid.id
                    WHERE paid.kind = 'payment'
                    GROUP BY paid.id, paid.amount_minor
                    HAVING SUM(refund.amount_minor) > -paid.amount_minor
                """)
            ).all()
        return (
            [f"unbalanced transaction {row}" for row in unbalanced]
            + [f"balance differs from postings {row}" for row in drifted]
            + [f"negative wallet {row}" for row in negative]
            + [f"payment refunded beyond its amount {row}" for row in over_refunded]
        )

    return check


@pytest.fixture
def auth_headers(db: Engine) -> Callable[..., dict[str, str]]:
    def headers(tenant_id: UUID, scopes: tuple[str, ...] = ("read", "write")) -> dict[str, str]:
        with db.begin() as conn:
            key = issue_api_key(conn, tenant_id, scopes)
        return {"Authorization": f"Bearer {key}"}

    return headers


@dataclass
class Receiver:
    """A real HTTP endpoint standing in for a tenant's webhook URL."""

    url: str
    requests: list[tuple[dict[str, str], bytes]] = field(default_factory=list)
    status: int = 200
    # Cleared: requests are recorded, then held open until it is set (a hung endpoint).
    respond: threading.Event = field(default_factory=threading.Event)
    received: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    state = Receiver(url="")
    state.respond.set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append((dict(self.headers), body))
            state.received.set()
            state.respond.wait()
            self.send_response(state.status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request: object, client_address: object) -> None:
            pass  # e.g. answering a worker that was killed mid-request

    server = Server(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}/webhooks"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    state.respond.set()
    server.shutdown()


@pytest.fixture
def webhook_tenant(
    db: Engine, create_tenant: Callable[[str], UUID], receiver: Receiver
) -> tuple[UUID, str]:
    """A tenant whose events go to the receiver; returns (tenant_id, signing secret)."""
    tenant_id = create_tenant("acme")
    with db.begin() as conn:
        admin.set_webhook(conn, tenant_id, receiver.url, "whsec_test")
    return tenant_id, "whsec_test"
