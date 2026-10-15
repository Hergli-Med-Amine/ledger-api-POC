# 6. API keys, not user authentication

**Status:** accepted

**Context.** Clients are tenants' back-office systems, not people. There are no logins, sessions
or passwords to manage.

**Decision.** Each request carries `Authorization: Bearer lk_<prefix>_<secret>`. The secret is 256
random bits. The database stores only `SHA-256(key)`, the non-secret prefix (indexed, for lookup),
the tenant and the scopes (`read`, `write`). Authentication finds the row by prefix and compares
hashes in constant time. Keys are issued and revoked with a CLI (`python -m ledger_api.admin`);
the plain key is printed once. Every wallet query is filtered by the key's tenant, and another
tenant's wallet returns `404`, the same as a missing one.

**Why SHA-256 and not bcrypt.** Slow hashes defend low-entropy passwords against guessing. A
256-bit random secret cannot be guessed, so a slow hash would add latency to every request and no
security. A leaked database still does not reveal usable keys.

**Consequences.** A lost key cannot be recovered, only revoked and replaced. One indexed lookup
per request. No admin API exists to attack; provisioning needs database access.
