# 2. Balances table updated with postings, under row locks

**Status:** accepted

**Context.** A payment must not succeed if it would take a wallet below zero, even when many
payments for the same wallet arrive at once. Reads of the current balance should be cheap.

**Decision.** Keep a `balances` row per account and update it in the same database transaction
that inserts the postings. Before changing anything, lock every affected balance row with
`SELECT … FOR UPDATE`, in ascending account-id order. Check for sufficient funds only after the
locks are held.

**Alternative rejected.** Computing the balance as `SUM(postings)` on every read. Correct, but the
cost grows with history, and two concurrent payments can both read the same sum, both pass the
funds check, and both commit — a classic check-then-act race.

**Consequences.** Concurrent writes to the same wallet serialise on its row lock; that is the
throughput limit for one hot wallet. A single global lock order means two transactions touching
the same accounts cannot deadlock each other. The stored balance can be verified against
`SUM(postings)` in tests.
