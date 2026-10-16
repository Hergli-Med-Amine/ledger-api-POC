import hashlib
import hmac
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection, text

from ledger_api import log
from ledger_api.api.state import EngineDep, RateLimiterDep

SCOPES = frozenset({"read", "write"})


class Unauthenticated(Exception):
    pass


class MissingScope(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    key_prefix: str
    scopes: frozenset[str]


def issue_api_key(conn: Connection, tenant_id: UUID, scopes: Collection[str]) -> str:
    """Returns the key in plain text. Only its hash is stored: this is the one chance to see it."""
    if not scopes or not set(scopes) <= SCOPES:
        raise ValueError(f"scopes must be a non-empty subset of {sorted(SCOPES)}")
    # The prefix is not secret: it lets authentication find the row without scanning hashes.
    prefix = secrets.token_hex(6)
    key = f"lk_{prefix}_{secrets.token_urlsafe(32)}"
    conn.execute(
        text("""
            INSERT INTO api_keys (tenant_id, prefix, hash, scopes)
            VALUES (:tenant_id, :prefix, :hash, :scopes)
        """),
        {"tenant_id": tenant_id, "prefix": prefix, "hash": _hash(key), "scopes": sorted(scopes)},
    )
    return key


def revoke_api_key(conn: Connection, prefix: str) -> bool:
    result = conn.execute(
        text("UPDATE api_keys SET revoked_at = now() WHERE prefix = :p AND revoked_at IS NULL"),
        {"p": prefix},
    )
    return result.rowcount == 1


def authenticate(conn: Connection, key: str) -> Principal:
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != "lk":
        raise Unauthenticated
    row = conn.execute(
        text("""
            SELECT tenant_id, hash, scopes FROM api_keys
            WHERE prefix = :prefix AND revoked_at IS NULL
        """),
        {"prefix": parts[1]},
    ).one_or_none()
    # Constant-time comparison, so response timing does not reveal how much of a guess matched.
    if row is None or not hmac.compare_digest(row.hash, _hash(key)):
        raise Unauthenticated
    return Principal(row.tenant_id, parts[1], frozenset(row.scopes))


def _hash(key: str) -> bytes:
    # Fast hash suffices for 256 random bits; slow hashes exist for guessable passwords.
    return hashlib.sha256(key.encode()).digest()


_bearer = HTTPBearer(auto_error=False)


def _current_principal(
    engine: EngineDep,
    rate_limiter: RateLimiterDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise Unauthenticated
    with engine.connect() as conn:
        principal = authenticate(conn, credentials.credentials)
    log.set_tenant(principal.tenant_id)
    if rate_limiter is not None:
        rate_limiter.check(principal.key_prefix)
    return principal


def _require_read(principal: Annotated[Principal, Depends(_current_principal)]) -> Principal:
    if "read" not in principal.scopes:
        raise MissingScope("read")
    return principal


def _require_write(principal: Annotated[Principal, Depends(_current_principal)]) -> Principal:
    if "write" not in principal.scopes:
        raise MissingScope("write")
    return principal


Reader = Annotated[Principal, Depends(_require_read)]
Writer = Annotated[Principal, Depends(_require_write)]
