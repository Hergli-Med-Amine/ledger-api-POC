import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import Engine, text

PARALLEL = 50


@pytest.fixture
def http(server_url: str) -> Iterator[httpx2.Client]:
    with httpx2.Client(base_url=server_url, timeout=30) as client:
        yield client


@pytest.fixture
def setup_wallet(
    http: httpx2.Client,
    create_tenant: Callable[[str], UUID],
    auth_headers: Callable[..., dict[str, str]],
) -> Callable[[int], tuple[str, dict[str, str]]]:
    def setup(balance_minor: int) -> tuple[str, dict[str, str]]:
        headers = auth_headers(create_tenant("acme"))
        wallet = http.post(
            "/v1/wallets",
            json={"currency": "EUR"},
            headers={**headers, "Idempotency-Key": "open"},
        ).json()
        http.post(
            f"/v1/wallets/{wallet['id']}/top-ups",
            json={"amount_minor": balance_minor, "currency": "EUR"},
            headers={**headers, "Idempotency-Key": "fund"},
        )
        return f"/v1/wallets/{wallet['id']}", headers

    return setup


def in_parallel(send: Callable[[int], httpx2.Response]) -> list[httpx2.Response]:
    # The barrier releases all threads at once, so the requests genuinely overlap.
    barrier = threading.Barrier(PARALLEL)

    def run(i: int) -> httpx2.Response:
        barrier.wait()
        return send(i)

    with ThreadPoolExecutor(PARALLEL) as pool:
        return list(pool.map(run, range(PARALLEL)))


def payment_count(db: Engine) -> int:
    with db.connect() as conn:
        count: int = conn.execute(
            text("SELECT count(*) FROM transactions WHERE kind = 'payment'")
        ).scalar_one()
    return count


def test_same_payment_fired_in_parallel_with_one_key_pays_once(
    http: httpx2.Client,
    db: Engine,
    setup_wallet: Callable[[int], tuple[str, dict[str, str]]],
    ledger_violations: Callable[[], list[str]],
) -> None:
    wallet_url, headers = setup_wallet(100_00)
    body: dict[str, Any] = {"amount_minor": 30_00, "currency": "EUR"}

    responses = in_parallel(
        lambda _: http.post(
            f"{wallet_url}/payments", json=body, headers={**headers, "Idempotency-Key": "pay-1"}
        )
    )

    statuses = Counter(r.status_code for r in responses)
    # Each duplicate either waits out the first (409, retry later) or replays its result (201).
    assert set(statuses) <= {201, 409}, statuses
    assert len({r.json()["transaction_id"] for r in responses if r.status_code == 201}) == 1
    assert payment_count(db) == 1
    assert http.get(wallet_url, headers=headers).json()["balance_minor"] == 70_00
    assert ledger_violations() == []


def test_parallel_payments_beyond_the_balance_never_overdraw(
    http: httpx2.Client,
    db: Engine,
    setup_wallet: Callable[[int], tuple[str, dict[str, str]]],
    ledger_violations: Callable[[], list[str]],
) -> None:
    wallet_url, headers = setup_wallet(10_00)
    body: dict[str, Any] = {"amount_minor": 30, "currency": "EUR"}

    responses = in_parallel(
        lambda i: http.post(
            f"{wallet_url}/payments", json=body, headers={**headers, "Idempotency-Key": f"p{i}"}
        )
    )

    statuses = Counter(r.status_code for r in responses)
    # 1000 / 30: exactly 33 fit, the other 17 must be refused.
    assert statuses == {201: 33, 422: 17}, statuses
    assert all(
        r.json()["type"] == "/problems/insufficient-funds"
        for r in responses
        if r.status_code == 422
    )
    assert payment_count(db) == 33
    assert http.get(wallet_url, headers=headers).json()["balance_minor"] == 10
    assert ledger_violations() == []


def test_parallel_partial_refunds_never_exceed_the_payment(
    http: httpx2.Client,
    setup_wallet: Callable[[int], tuple[str, dict[str, str]]],
    ledger_violations: Callable[[], list[str]],
) -> None:
    wallet_url, headers = setup_wallet(100)
    payment = http.post(
        f"{wallet_url}/payments",
        json={"amount_minor": 30, "currency": "EUR"},
        headers={**headers, "Idempotency-Key": "pay"},
    ).json()
    refunds_url = f"{wallet_url}/payments/{payment['transaction_id']}/refunds"

    responses = in_parallel(
        lambda i: http.post(
            refunds_url,
            json={"amount_minor": 1, "currency": "EUR"},
            headers={**headers, "Idempotency-Key": f"r{i}"},
        )
    )

    assert Counter(r.status_code for r in responses) == {201: 30, 422: 20}
    assert http.get(wallet_url, headers=headers).json()["balance_minor"] == 100
    assert ledger_violations() == []


def test_one_quote_fired_in_parallel_converts_once(
    http: httpx2.Client,
    setup_wallet: Callable[[int], tuple[str, dict[str, str]]],
    ledger_violations: Callable[[], list[str]],
) -> None:
    wallet_url, headers = setup_wallet(100_00)
    http.post("/v1/wallets", json={"currency": "USD"}, headers={**headers, "Idempotency-Key": "u"})
    quote = http.post(
        "/v1/fx/quotes",
        json={"from_currency": "EUR", "to_currency": "USD"},
        headers={**headers, "Idempotency-Key": "q"},
    ).json()

    # Distinct idempotency keys, so only the quote's own single-use rule stops the duplicates.
    responses = in_parallel(
        lambda i: http.post(
            "/v1/fx/conversions",
            json={"quote_id": quote["id"], "sell_amount_minor": 10_00},
            headers={**headers, "Idempotency-Key": f"c{i}"},
        )
    )

    assert Counter(r.status_code for r in responses) == {201: 1, 409: 49}
    assert http.get(wallet_url, headers=headers).json()["balance_minor"] == 90_00
    assert ledger_violations() == []
