# 8. Balancing invariant enforced by a deferred constraint trigger

**Status:** accepted

**Context.** For every transaction and every currency, postings must sum to zero. The application
checks this before writing, but a bug, a hand-written fix in `psql` or a future code path could
still commit an unbalanced transaction. A check that only runs in tests would not stop that.

**Decision.** A `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED` on `postings` sums the
transaction's postings per currency and raises if any sum is non-zero. Deferred means it runs at
`COMMIT`, when all postings of the transaction exist; checked per row it would reject the first
posting of every transaction. A second trigger rejects `UPDATE` and `DELETE` on `postings`, so
the ledger is append-only and a committed transaction cannot be unbalanced later.

**Alternative rejected.** A verification query run only in tests. It finds bugs the tests happen
to exercise, but production data would not be protected.

**Consequences.** Each posting adds one indexed `SUM` at commit, cheap for 2–3 postings. Errors
surface at `COMMIT`, not at the faulty `INSERT`. Tests still run the verification query, to check
the trigger as well as the code.
