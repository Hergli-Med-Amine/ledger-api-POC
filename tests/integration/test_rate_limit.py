import logging
import time
from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
import redis
from fastapi.testclient import TestClient
from testcontainers.community.redis import RedisContainer

from ledger_api.api.state import get_rate_limiter
from ledger_api.main import app
from ledger_api.ratelimit import RateLimiter

PER_SECOND = 5
BURST = 3


@pytest.fixture(scope="module")
def redis_client() -> Iterator[redis.Redis]:
    with RedisContainer("redis:8.10.2-alpine") as container:
        yield container.get_client()


def limit_with(limiter: RateLimiter) -> None:
    app.dependency_overrides[get_rate_limiter] = lambda: limiter


@pytest.fixture
def headers(
    create_tenant: Callable[[str], UUID], auth_headers: Callable[..., dict[str, str]]
) -> dict[str, str]:
    return auth_headers(create_tenant("acme"))


def test_each_key_gets_a_burst_then_429_until_tokens_refill(
    client: TestClient,
    redis_client: redis.Redis,
    headers: dict[str, str],
    create_tenant: Callable[[str], UUID],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    limit_with(RateLimiter(redis_client, PER_SECOND, BURST))

    statuses = [client.get("/v1/wallets", headers=headers).status_code for _ in range(BURST + 1)]
    assert statuses == [200, 200, 200, 429]

    limited = client.get("/v1/wallets", headers=headers)
    assert limited.json()["type"] == "/problems/rate-limited"
    assert limited.headers["retry-after"] == "1"

    other_key = auth_headers(create_tenant("other"))
    assert client.get("/v1/wallets", headers=other_key).status_code == 200

    time.sleep(1 / PER_SECOND + 0.05)
    assert client.get("/v1/wallets", headers=headers).status_code == 200


def test_requests_are_allowed_when_redis_is_down(
    client: TestClient, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    unreachable = redis.Redis(port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    limit_with(RateLimiter(unreachable, PER_SECOND, BURST))

    with caplog.at_level(logging.WARNING, logger="ledger_api.ratelimit"):
        statuses = {client.get("/v1/wallets", headers=headers).status_code for _ in range(10)}

    assert statuses == {200}
    assert "rate limit unavailable, request allowed" in caplog.messages
