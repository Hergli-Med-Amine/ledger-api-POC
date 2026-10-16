"""Transactional outbox and signed webhook delivery, at least once (ADRs 4 and 5)."""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
from sqlalchemy import Connection, Engine, text

MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 3600
SIGNATURE_TOLERANCE_SECONDS = 300

logger = logging.getLogger("ledger_api.outbox")


def enqueue(conn: Connection, tenant_id: UUID, event_type: str, data: dict[str, Any]) -> None:
    # Only tenants with a webhook endpoint get events; the rest would never be deliverable.
    conn.execute(
        text("""
            INSERT INTO outbox (tenant_id, event_type, payload)
            SELECT id, :event_type, CAST(:payload AS jsonb) FROM tenants
            WHERE id = :tenant_id AND webhook_url IS NOT NULL
        """),
        {"tenant_id": tenant_id, "event_type": event_type, "payload": json.dumps(data)},
    )


def sign(secret: str, timestamp: int, body: bytes) -> str:
    # Signing the timestamp stops a captured request being replayed with a fresh one.
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(secret: str, header: str, body: bytes, now: int) -> bool:
    """What a receiver runs: correct HMAC over this exact body, and a recent timestamp."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
        signature = fields["v1"]
    except KeyError, ValueError:
        return False
    if abs(now - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True, slots=True)
class _Due:
    id: UUID
    tenant_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: str
    attempts: int
    url: str
    secret: str


def relay_batch(engine: Engine, client: httpx2.Client, limit: int = 50) -> int:
    """Delivers up to `limit` due events; returns how many it tried."""
    # Rows stay locked while sending, so a worker crash rolls back and they are sent again.
    with engine.begin() as conn:
        # SKIP LOCKED: concurrent workers take disjoint batches.
        rows = conn.execute(
            text("""
                SELECT o.id, o.tenant_id, o.event_type, o.payload, o.created_at, o.attempts,
                       t.webhook_url, t.webhook_secret
                FROM outbox o JOIN tenants t ON t.id = o.tenant_id
                WHERE o.sent_at IS NULL AND o.dead_at IS NULL AND o.next_attempt_at <= now()
                ORDER BY o.next_attempt_at
                LIMIT :limit
                FOR UPDATE OF o SKIP LOCKED
            """),
            {"limit": limit},
        ).all()
        for row in rows:
            due = _Due(
                row.id,
                row.tenant_id,
                row.event_type,
                row.payload,
                row.created_at.isoformat(),
                row.attempts,
                row.webhook_url,
                row.webhook_secret,
            )
            error = _send(client, due)
            _record_attempt(conn, due, error)
    return len(rows)


def backoff_seconds(attempts: int) -> int:
    return int(min(2**attempts, MAX_BACKOFF_SECONDS))


def _send(client: httpx2.Client, due: _Due) -> str | None:
    body = json.dumps(
        {
            "id": str(due.id),
            "type": due.event_type,
            "created_at": due.created_at,
            "data": due.payload,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Ledger-Event-Id": str(due.id),
        "Ledger-Signature": sign(due.secret, int(time.time()), body),
    }
    try:
        response = client.post(due.url, content=body, headers=headers)
    except httpx2.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if response.is_success else f"HTTP {response.status_code}"


def _record_attempt(conn: Connection, due: _Due, error: str | None) -> None:
    attempts = due.attempts + 1
    if error is None:
        conn.execute(
            text(
                "UPDATE outbox SET sent_at = now(), attempts = :n, last_error = NULL WHERE id = :id"
            ),
            {"id": due.id, "n": attempts},
        )
    elif attempts >= MAX_ATTEMPTS:
        conn.execute(
            text(
                "UPDATE outbox SET dead_at = now(), attempts = :n, last_error = :e WHERE id = :id"
            ),
            {"id": due.id, "n": attempts, "e": error},
        )
    else:
        conn.execute(
            text("""
                UPDATE outbox
                SET attempts = :n, last_error = :e,
                    next_attempt_at = now() + make_interval(secs => :delay)
                WHERE id = :id
            """),
            {"id": due.id, "n": attempts, "e": error, "delay": backoff_seconds(attempts)},
        )
    log = logger.info if error is None else logger.warning
    log(
        "webhook delivery",
        extra={
            "fields": {
                "event_id": str(due.id),
                "tenant_id": str(due.tenant_id),
                "attempt": attempts,
                "error": error,
                "dead": error is not None and attempts >= MAX_ATTEMPTS,
            }
        },
    )
