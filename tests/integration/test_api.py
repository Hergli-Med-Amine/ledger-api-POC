from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ledger_api.api.auth import revoke_api_key
from ledger_api.api.state import get_engine
from ledger_api.main import app

Headers = dict[str, str]


@pytest.fixture
def tenant(create_tenant: Callable[[str], UUID]) -> UUID:
    return create_tenant("acme")


@pytest.fixture
def headers(tenant: UUID, auth_headers: Callable[..., Headers]) -> Headers:
    return auth_headers(tenant)


@pytest.fixture
def wallet_id(client: TestClient, headers: Headers) -> str:
    response = client.post("/v1/wallets", json={"currency": "EUR"}, headers=headers)
    assert response.status_code == 201
    wallet_id: str = response.json()["id"]
    return wallet_id


def top_up(client: TestClient, headers: Headers, wallet_id: str, amount: int) -> Any:
    response = client.post(
        f"/v1/wallets/{wallet_id}/top-ups",
        json={"amount_minor": amount, "currency": "EUR"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def assert_problem(response: Any, status: int, type_: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == type_
    assert response.json()["status"] == status


def test_open_wallet(client: TestClient, headers: Headers) -> None:
    response = client.post("/v1/wallets", json={"currency": "EUR"}, headers=headers)

    assert response.status_code == 201
    assert response.json()["currency"] == "EUR"
    assert response.json()["balance_minor"] == 0

    again = client.post("/v1/wallets", json={"currency": "EUR"}, headers=headers)
    assert_problem(again, 409, "/problems/wallet-exists")


def test_list_wallets(client: TestClient, headers: Headers, wallet_id: str) -> None:
    client.post("/v1/wallets", json={"currency": "USD"}, headers=headers)

    response = client.get("/v1/wallets", headers=headers)

    assert response.status_code == 200
    assert [w["currency"] for w in response.json()["data"]] == ["EUR", "USD"]


def test_get_wallet_shows_balance(client: TestClient, headers: Headers, wallet_id: str) -> None:
    top_up(client, headers, wallet_id, 12_50)

    response = client.get(f"/v1/wallets/{wallet_id}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"id": wallet_id, "currency": "EUR", "balance_minor": 12_50}


def test_top_up(client: TestClient, headers: Headers, wallet_id: str) -> None:
    body = top_up(client, headers, wallet_id, 10_00)

    assert body["kind"] == "top_up"
    assert body["amount_minor"] == 10_00
    assert body["balance_minor"] == 10_00


def test_payment(client: TestClient, headers: Headers, wallet_id: str) -> None:
    top_up(client, headers, wallet_id, 10_00)
    url = f"/v1/wallets/{wallet_id}/payments"

    paid = client.post(
        url,
        json={"amount_minor": 7_00, "currency": "EUR", "reference": "INV-1"},
        headers=headers,
    )
    assert paid.status_code == 201
    assert paid.json()["reference"] == "INV-1"
    assert paid.json()["balance_minor"] == 3_00

    too_much = client.post(url, json={"amount_minor": 3_01, "currency": "EUR"}, headers=headers)
    assert_problem(too_much, 422, "/problems/insufficient-funds")


def test_statement_pages_newest_first(client: TestClient, headers: Headers, wallet_id: str) -> None:
    for amount in [1, 2, 3, 4, 5]:
        top_up(client, headers, wallet_id, amount)
    url = f"/v1/wallets/{wallet_id}/statement"

    seen: list[int] = []
    pages = 0
    params: dict[str, Any] = {"limit": 2}
    while True:
        response = client.get(url, params=params, headers=headers)
        assert response.status_code == 200
        pages += 1
        seen += [line["amount_minor"] for line in response.json()["data"]]
        if response.json()["next_cursor"] is None:
            break
        params["cursor"] = response.json()["next_cursor"]

    assert seen == [5, 4, 3, 2, 1]
    assert pages == 3

    bad = client.get(url, params={"cursor": "not-a-cursor"}, headers=headers)
    assert_problem(bad, 400, "/problems/invalid-cursor")


def test_requests_without_a_valid_key_are_rejected(
    client: TestClient, wallet_id: str, headers: Headers
) -> None:
    missing = client.get("/v1/wallets")
    assert_problem(missing, 401, "/problems/unauthenticated")
    assert missing.headers["www-authenticate"] == "Bearer"

    forged = headers["Authorization"][:-4] + "AAAA"
    assert_problem(
        client.get("/v1/wallets", headers={"Authorization": forged}),
        401,
        "/problems/unauthenticated",
    )


def test_revoked_key_is_rejected(client: TestClient, db: Engine, headers: Headers) -> None:
    prefix = headers["Authorization"].removeprefix("Bearer ").split("_")[1]
    with db.begin() as conn:
        assert revoke_api_key(conn, prefix)

    assert_problem(client.get("/v1/wallets", headers=headers), 401, "/problems/unauthenticated")


def test_read_only_key_cannot_move_money(
    client: TestClient, tenant: UUID, wallet_id: str, auth_headers: Callable[..., Headers]
) -> None:
    read_only = auth_headers(tenant, scopes=("read",))

    assert client.get(f"/v1/wallets/{wallet_id}", headers=read_only).status_code == 200
    response = client.post(
        f"/v1/wallets/{wallet_id}/top-ups",
        json={"amount_minor": 100, "currency": "EUR"},
        headers=read_only,
    )
    assert_problem(response, 403, "/problems/missing-scope")


def test_tenant_cannot_see_or_use_another_tenants_wallet(
    client: TestClient,
    headers: Headers,
    wallet_id: str,
    create_tenant: Callable[[str], UUID],
    auth_headers: Callable[..., Headers],
) -> None:
    top_up(client, headers, wallet_id, 10_00)
    other = auth_headers(create_tenant("other"))
    amount = {"amount_minor": 1, "currency": "EUR"}

    responses = [
        client.get(f"/v1/wallets/{wallet_id}", headers=other),
        client.get(f"/v1/wallets/{wallet_id}/statement", headers=other),
        client.post(f"/v1/wallets/{wallet_id}/top-ups", json=amount, headers=other),
        client.post(f"/v1/wallets/{wallet_id}/payments", json=amount, headers=other),
    ]

    for response in responses:
        assert_problem(response, 404, "/problems/wallet-not-found")
    assert client.get("/v1/wallets", headers=other).json() == {"data": []}
    assert client.get(f"/v1/wallets/{wallet_id}", headers=headers).json()["balance_minor"] == 10_00


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": "100", "currency": "EUR"},
        {"amount_minor": 1.5, "currency": "EUR"},
        {"amount_minor": 0, "currency": "EUR"},
        {"amount_minor": 2**53, "currency": "EUR"},
        {"amount_minor": 100, "currency": "eur"},
        {"amount_minor": 100, "currency": "EUR", "ammount": 1},
    ],
)
def test_malformed_amounts_are_rejected(
    client: TestClient, headers: Headers, wallet_id: str, body: dict[str, Any]
) -> None:
    response = client.post(f"/v1/wallets/{wallet_id}/top-ups", json=body, headers=headers)
    assert_problem(response, 422, "/problems/validation")


def test_unknown_currency_is_rejected(client: TestClient, headers: Headers) -> None:
    response = client.post("/v1/wallets", json={"currency": "XYZ"}, headers=headers)
    assert_problem(response, 422, "/problems/unknown-currency")


def test_unexpected_errors_hide_internals(client: TestClient, headers: Headers) -> None:
    def broken() -> None:
        raise RuntimeError("connection string with a password in it")

    app.dependency_overrides[get_engine] = broken
    response = TestClient(app, raise_server_exceptions=False).get("/v1/wallets", headers=headers)

    assert_problem(response, 500, "about:blank")
    assert "password" not in response.text


def test_refund(client: TestClient, headers: Headers, wallet_id: str) -> None:
    top_up(client, headers, wallet_id, 10_00)
    payment = client.post(
        f"/v1/wallets/{wallet_id}/payments",
        json={"amount_minor": 6_00, "currency": "EUR"},
        headers=headers,
    ).json()
    url = f"/v1/wallets/{wallet_id}/payments/{payment['transaction_id']}/refunds"

    refunded = client.post(url, json={"amount_minor": 4_00, "currency": "EUR"}, headers=headers)
    assert refunded.status_code == 201
    assert refunded.json()["kind"] == "refund"
    assert refunded.json()["balance_minor"] == 8_00

    too_much = client.post(url, json={"amount_minor": 2_01, "currency": "EUR"}, headers=headers)
    assert_problem(too_much, 422, "/problems/refund-exceeds-payment")


def test_fx_quote_and_conversion(client: TestClient, headers: Headers, wallet_id: str) -> None:
    top_up(client, headers, wallet_id, 10_00)
    client.post("/v1/wallets", json={"currency": "USD"}, headers=headers)

    quote = client.post(
        "/v1/fx/quotes",
        json={"from_currency": "EUR", "to_currency": "USD"},
        headers=headers,
    )
    assert quote.status_code == 201
    # Decimal rates travel as strings, so no client parses them into a float.
    assert quote.json()["rate"] == "1.0850"

    conversion = client.post(
        "/v1/fx/conversions",
        json={"quote_id": quote.json()["id"], "sell_amount_minor": 10_00},
        headers=headers,
    )
    assert conversion.status_code == 201
    assert conversion.json()["bought_amount_minor"] == 10_85
    assert conversion.json()["bought_currency"] == "USD"

    again = client.post(
        "/v1/fx/conversions",
        json={"quote_id": quote.json()["id"], "sell_amount_minor": 1},
        headers=headers,
    )
    assert_problem(again, 409, "/problems/quote-already-used")
