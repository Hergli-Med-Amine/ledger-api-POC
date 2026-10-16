# 5. Webhook signing: HMAC-SHA256 over timestamp and body

**Status:** accepted

**Context.** A receiver must be able to tell that a webhook came from us, was not modified, and
is not an old request being replayed.

**Decision.** Each tenant has a secret (`admin set-webhook`). Every delivery carries:

```
Ledger-Event-Id:  <event id, stable across retries>
Ledger-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>
```

**How a receiver verifies** (`outbox.verify_signature` is a working example):
1. Read the raw request body bytes, before any JSON parsing.
2. Split the header into `t` and `v1`.
3. Reject if `t` is more than 5 minutes from your clock.
4. Compute `HMAC-SHA256(secret, f"{t}." + body)` and compare to `v1` in constant time.
5. Dedupe on `Ledger-Event-Id`; the same event may arrive more than once.

**Why the timestamp is inside the MAC.** Otherwise an attacker could replay a captured request
with a fresh `t`. Changing `t` now breaks the signature (tested).

**Consequences.** Secrets must be stored retrievably to sign, so they are not hashed like API
keys; they sit in the database in plain text (see Limitations).
