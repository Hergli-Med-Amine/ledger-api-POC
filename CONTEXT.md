# Glossary

**Tenant** — a distributor using the platform. Owns API keys, wallets and a webhook endpoint. Every
row a tenant can see is scoped by `tenant_id`.

**Account** — something that holds money in exactly one currency. A tenant's wallet is an account;
so are the platform-owned accounts that sit on the other side of each movement
(`external_funding`, `supplier_payable`, `fx_pool`), which have no tenant.

**Wallet** — an account of kind `wallet`. One per tenant per currency. The only kind of account a
tenant reads or moves money from directly.

**Transaction** — one business event (top-up, payment, refund, conversion). Groups the postings
that record it and is written atomically: all of its postings exist or none do.

**Posting** — one signed amount, in integer minor units, against one account inside one
transaction. Positive increases the account's balance, negative decreases it. For every
transaction and every currency, its postings sum to zero.

**Balance** — the current total of an account's postings. For wallets it is stored in `balances`
and updated in the same database transaction as the postings that change it; platform accounts
have no stored balance (see ADR 2).

**Minor units** — the smallest unit of a currency as an integer: cents for EUR (exponent 2), yen
for JPY (exponent 0). All amounts are stored and computed this way.

**Quote** — a fixed FX rate from one currency to another, offered to a tenant with a short expiry.
A conversion must reference an unexpired, unused quote, and using it marks it used.

**API key** — the credential a tenant's system sends as `Authorization: Bearer lk_…`. Belongs to
one tenant, carries scopes (`read`, `write`), stored only as a hash, can be revoked.

**Statement** — a wallet's postings, newest first, paginated with an opaque cursor.

**Idempotency key** — a client-chosen string sent in the `Idempotency-Key` header on a write.
Scoped to the tenant. The first request with a key does the work; a retry with the same key and
same body gets the stored response back instead of moving money twice.

**Outbox** — a table of events written in the same transaction as the money movement they
describe. A separate worker reads it and delivers signed webhooks, so an event exists if and only
if the movement committed.
