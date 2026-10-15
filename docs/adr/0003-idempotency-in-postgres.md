# 3. Idempotency stored in PostgreSQL, in the same transaction as the write

**Status:** accepted

**Context.** Clients retry on timeouts, so the same payment can arrive twice, possibly at the
same moment. Money must move once, and the retry must get the original answer.

**Decision.** Every `POST` requires an `Idempotency-Key` header, scoped to the tenant. Inside the
request's single database transaction:
1. `pg_try_advisory_xact_lock(tenant, key)`. Not acquired → `409`: the first request is in flight.
2. Look up the key. Stored with the same request fingerprint (method, path, canonical body) →
   replay the stored status and body. Stored with a different fingerprint → `422`.
3. Otherwise do the write and insert the key row with the response, then commit.

The key row commits with the money movement or not at all, so "money moved" and "key recorded"
cannot disagree. The advisory lock is released at commit or rollback, so a crashed request never
leaves a key stuck "in flight", and no `status` column or cleanup job is needed.

**Why both the lock and the primary key.** With the lock removed (tested), money still moved
once, because the duplicate `INSERT` on `(tenant_id, key)` fails. But 14 of 50 duplicates then
got a `500`; the lock turns that race into a clean `409`.

**Alternative rejected.** Redis: a separate store cannot commit atomically with PostgreSQL, so a
crash between the two writes would leave them disagreeing.

**Consequences.** A request that fails (e.g. insufficient funds) moved no money and is not
recorded, so retrying the same key runs it again. Keys are kept indefinitely (see Limitations).
Two different keys whose 64-bit lock hashes collide would see a spurious `409`; at this scale
that is negligible.
