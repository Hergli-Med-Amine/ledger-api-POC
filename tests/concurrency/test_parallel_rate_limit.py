import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from testcontainers.community.redis import RedisContainer

from ledger_api.ratelimit import RateLimited, RateLimiter

PARALLEL = 50
BURST = 10


@pytest.fixture(scope="module")
def redis_client() -> Iterator[redis.Redis]:
    with RedisContainer("redis:8.10.2-alpine") as container:
        yield container.get_client()


def test_parallel_requests_never_take_more_than_the_burst(redis_client: redis.Redis) -> None:
    # A refill this slow adds nothing during the test, so exactly BURST requests may pass.
    limiter = RateLimiter(redis_client, per_second=0.001, burst=BURST)
    barrier = threading.Barrier(PARALLEL)

    def attempt(_: int) -> bool:
        barrier.wait()
        try:
            limiter.check("key")
        except RateLimited:
            return False
        return True

    with ThreadPoolExecutor(PARALLEL) as pool:
        allowed = sum(pool.map(attempt, range(PARALLEL)))

    assert allowed == BURST
