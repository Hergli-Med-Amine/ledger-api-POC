# 7. Redis only for rate limiting, failing open

**Status:** accepted

**Context.** Each API key must be limited across every API instance, so the counter cannot live in
one process. Money state, however, must never depend on a second datastore.

**Decision.** A token bucket per API key in Redis: `RATE_LIMIT_BURST` tokens, refilled at
`RATE_LIMIT_PER_SECOND`. One Lua script reads, refills and takes a token atomically, using Redis's
clock. An empty bucket answers `429` with `Retry-After`. Nothing else uses Redis.

**Fail open.** If Redis errors or takes longer than 200 ms, the request is allowed and a warning is
logged. Losing Redis degrades to "no rate limit", never to a refused payment or a wrong balance.
The trade-off is explicit: during a Redis outage a client can exceed its limit.

**Consequences.**
- 50 simultaneous requests against a burst of 10 let exactly 10 through (tested).
- With Redis stopped under `docker compose`, requests kept succeeding and each logged a warning.
- The limit is checked after authentication, so unauthenticated floods are not limited here; that
  belongs in front of the service (load balancer or Cloud Armor).
