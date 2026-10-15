from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Request
from sqlalchemy import Engine

from ledger_api.api.database import get_engine
from ledger_api.main import app


@pytest.fixture
def client(db: Engine) -> Iterator[TestClient]:
    """Adds a fresh Idempotency-Key to any POST that has none; idempotency tests set their own."""

    def add_idempotency_key(request: Request) -> None:
        if request.method == "POST" and "idempotency-key" not in request.headers:
            request.headers["Idempotency-Key"] = str(uuid4())

    app.dependency_overrides[get_engine] = lambda: db
    client = TestClient(app)
    client.event_hooks = {"request": [add_idempotency_key], "response": []}
    yield client
    app.dependency_overrides.clear()
