# 4. Transactional outbox, not "write then publish"

**Status:** accepted

**Context.** Other systems must learn about every money movement. Committing the movement and
then calling the webhook fails in both directions: a crash after commit loses the event, and
sending before commit can announce a movement that then rolls back.

**Decision.** `ledger._post`, which every transaction goes through, also inserts an `outbox` row
in the same database transaction. A separate worker delivers due rows: it claims a batch with
`SELECT … FOR UPDATE SKIP LOCKED`, sends each request while holding the row locks, and records
the result in the same transaction. Failures retry after `2^attempt` seconds (capped at an hour);
after 10 attempts the event is marked dead and kept for inspection.

**Consequences.**
- An event exists if and only if its movement committed.
- Delivery is **at least once**. A worker killed mid-delivery rolls back and the event is sent
  again (tested by killing a real worker process with `SIGKILL`). Each event carries a stable
  `Ledger-Event-Id`, so receivers dedupe on it.
- Several workers are safe: `SKIP LOCKED` hands them disjoint batches. Without the row lock, 4
  workers delivered some of 100 events up to 4 times (tested).
- Marking an event sent and committing *before* sending would lose it on a crash (tested: the
  kill-the-worker test fails with that ordering).
- A worker holds one database connection per batch for the duration of the HTTP calls (5 s
  timeout each). Acceptable at this scale; a lease column would avoid it.
