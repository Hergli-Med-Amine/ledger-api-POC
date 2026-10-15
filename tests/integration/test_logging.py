import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ledger_api import log


@pytest.fixture
def json_logs() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    root = logging.getLogger()
    saved_handlers, saved_factory = root.handlers[:], logging.getLogRecordFactory()
    log.configure()
    output = io.StringIO()
    root.handlers[0].setStream(output)  # type: ignore[attr-defined]
    yield lambda: [json.loads(line) for line in output.getvalue().splitlines()]
    root.handlers = saved_handlers
    logging.setLogRecordFactory(saved_factory)


def test_log_lines_carry_request_and_tenant_ids_but_never_the_key(
    client: TestClient,
    create_tenant: Callable[[str], UUID],
    auth_headers: Callable[..., dict[str, str]],
    json_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    tenant = create_tenant("acme")
    headers = auth_headers(tenant)

    response = client.post("/v1/wallets", json={"currency": "EUR"}, headers=headers)

    lines = json_logs()
    access = [line for line in lines if line["message"] == "request"]
    assert access == [
        {
            "time": access[0]["time"],
            "level": "INFO",
            "logger": "ledger_api.http",
            "message": "request",
            "request_id": response.headers["x-request-id"],
            "tenant_id": str(tenant),
            "method": "POST",
            "path": "/v1/wallets",
            "status": 201,
            "duration_ms": access[0]["duration_ms"],
        }
    ]
    secret = headers["Authorization"].removeprefix("Bearer ")
    assert all(secret not in json.dumps(line) for line in lines)


def test_client_request_id_is_reused_only_when_safe(
    client: TestClient, json_logs: Callable[[], list[dict[str, Any]]]
) -> None:
    safe = client.get("/health", headers={"X-Request-Id": "abc-123"})
    unsafe = client.get("/health", headers={"X-Request-Id": 'x" injected="1'})

    assert safe.headers["x-request-id"] == "abc-123"
    assert unsafe.headers["x-request-id"] != 'x" injected="1'
    access = [line for line in json_logs() if line["message"] == "request"]
    assert [line["request_id"] for line in access] == [
        "abc-123",
        unsafe.headers["x-request-id"],
    ]
