import os
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from ledger_api import admin

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
            conn.execute(
                text("TRUNCATE tenants, api_keys, accounts, transactions, postings, balances")
            )

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
        return (
            [f"unbalanced transaction {row}" for row in unbalanced]
            + [f"balance differs from postings {row}" for row in drifted]
            + [f"negative wallet {row}" for row in negative]
        )

    return check
