from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from ledger_api import idempotency

Headers = dict[str, str]


@pytest.fixture
def tenant(create_tenant: Callable[[str], UUID]) -> UUID:
    return create_tenant("acme")


@pytest.fixture
def headers(tenant: UUID, auth_headers: Callable[..., Headers]) -> Headers:
    return auth_headers(tenant)


@pytest.fixture
def wallet_url(client: TestClient, headers: Headers) -> str:
    wallet_id = client.post("/v1/wallets", json={"currency": "EUR"}, headers=headers).json()["id"]
    client.post(
        f"/v1/wallets/{wallet_id}/top-ups",
        json={"amount_minor": 100_00, "currency": "EUR"},
        headers=headers,
    )
    return f"/v1/wallets/{wallet_id}"


def payment_count(db: Engine) -> int:
    with db.connect() as conn:
        count: int = conn.execute(
            text("SELECT count(*) FROM transactions WHERE kind = 'payment'")
        ).scalar_one()
    return count


def pay(client: TestClient, wallet_url: str, headers: Headers, key: str, amount: int) -> Any:
    return client.post(
        f"{wallet_url}/payments",
        json={"amount_minor": amount, "currency": "EUR"},
        headers={**headers, "Idempotency-Key": key},
    )


def test_retry_replays_the_stored_response_without_paying_twice(
    client: TestClient, db: Engine, headers: Headers, wallet_url: str
) -> None:
    first = pay(client, wallet_url, headers, "pay-1", 30_00)
    retry = pay(client, wallet_url, headers, "pay-1", 30_00)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert payment_count(db) == 1
    assert client.get(wallet_url, headers=headers).json()["balance_minor"] == 70_00


def test_same_key_with_a_different_body_is_rejected(
    client: TestClient, db: Engine, headers: Headers, wallet_url: str
) -> None:
    pay(client, wallet_url, headers, "pay-1", 30_00)

    response = pay(client, wallet_url, headers, "pay-1", 31_00)

    assert response.status_code == 422
    assert response.json()["type"] == "/problems/idempotency-key-reused"
    assert payment_count(db) == 1


def test_same_key_while_the_first_request_is_in_flight_is_rejected(
    client: TestClient, db: Engine, tenant: UUID, headers: Headers, wallet_url: str
) -> None:
    # Hold the key exactly as an unfinished request would: its lock taken, nothing committed.
    with db.begin() as in_flight:
        assert idempotency.begin(in_flight, tenant, "pay-1", b"any") is None

        response = pay(client, wallet_url, headers, "pay-1", 30_00)

        assert response.status_code == 409
        assert response.json()["type"] == "/problems/idempotency-key-in-flight"

    # Once that transaction ends, the key is free again.
    assert pay(client, wallet_url, headers, "pay-1", 30_00).status_code == 201


def test_failed_request_is_not_recorded_so_a_retry_runs_again(
    client: TestClient, db: Engine, headers: Headers, wallet_url: str
) -> None:
    assert pay(client, wallet_url, headers, "pay-1", 150_00).status_code == 422
    client.post(
        f"{wallet_url}/top-ups",
        json={"amount_minor": 50_00, "currency": "EUR"},
        headers=headers,
    )

    assert pay(client, wallet_url, headers, "pay-1", 150_00).status_code == 201
    assert payment_count(db) == 1


def test_keys_are_scoped_per_tenant(
    client: TestClient,
    db: Engine,
    headers: Headers,
    wallet_url: str,
    create_tenant: Callable[[str], UUID],
    auth_headers: Callable[..., Headers],
) -> None:
    other = auth_headers(create_tenant("other"))
    pay(client, wallet_url, headers, "shared-key", 10_00)

    response = client.post(
        "/v1/wallets", json={"currency": "EUR"}, headers={**other, "Idempotency-Key": "shared-key"}
    )

    assert response.status_code == 201


def test_writes_require_an_idempotency_key(
    db: Engine, headers: Headers, wallet_url: str, client: TestClient
) -> None:
    client.event_hooks = {"request": [], "response": []}  # turn off the fixture's default key

    response = client.post(
        f"{wallet_url}/payments", json={"amount_minor": 1, "currency": "EUR"}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["header", "idempotency-key"]
