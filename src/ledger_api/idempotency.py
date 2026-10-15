"""Idempotency-Key handling, inside the same database transaction as the write it protects.

Usage, within one transaction:
    stored = begin(conn, tenant_id, key, fingerprint)
    if stored: replay it
    else: do the write, then record(...) the response before commit
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


class KeyInFlight(Exception):
    pass


class KeyReused(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def fingerprint(method: str, path: str, body: dict[str, Any]) -> bytes:
    # Canonical JSON, so key order and whitespace do not make the same request look different.
    canonical = json.dumps([method, path, body], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()


def begin(
    conn: Connection, tenant_id: UUID, key: str, request_hash: bytes
) -> StoredResponse | None:
    """Returns the response to replay, or None if the caller should do the work.

    Raises KeyInFlight while another transaction holds the same key, and KeyReused if the key
    was already used for a different request.
    """
    # The lock is the "in flight" marker. It is released at commit or rollback, so a crashed
    # request never leaves a key stuck; there is no status column to clean up.
    locked = conn.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"{tenant_id}:{key}"},
    ).scalar_one()
    if not locked:
        raise KeyInFlight(key)
    # Safe to read then insert later: no other transaction can hold this key's lock meanwhile.
    row = conn.execute(
        text("""
            SELECT request_hash, response_code, response_body FROM idempotency_keys
            WHERE tenant_id = :tenant_id AND key = :key
        """),
        {"tenant_id": tenant_id, "key": key},
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise KeyReused(key)
    return StoredResponse(row.response_code, row.response_body)


def record(
    conn: Connection,
    tenant_id: UUID,
    key: str,
    request_hash: bytes,
    response: StoredResponse,
) -> None:
    conn.execute(
        text("""
            INSERT INTO idempotency_keys
                (tenant_id, key, request_hash, response_code, response_body)
            VALUES (:tenant_id, :key, :request_hash, :code, CAST(:body AS jsonb))
        """),
        {
            "tenant_id": tenant_id,
            "key": key,
            "request_hash": request_hash,
            "code": response.status_code,
            "body": json.dumps(response.body),
        },
    )
