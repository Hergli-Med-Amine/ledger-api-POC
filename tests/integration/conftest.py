from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ledger_api.api.auth import issue_api_key
from ledger_api.api.database import get_engine
from ledger_api.main import app

Headers = dict[str, str]


@pytest.fixture
def client(db: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_engine] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers(db: Engine) -> Callable[..., Headers]:
    def headers(tenant_id: UUID, scopes: tuple[str, ...] = ("read", "write")) -> Headers:
        with db.begin() as conn:
            key = issue_api_key(conn, tenant_id, scopes)
        return {"Authorization": f"Bearer {key}"}

    return headers
