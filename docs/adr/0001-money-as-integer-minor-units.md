# 1. Money as integer minor units

**Status:** accepted

**Context.** Floats cannot represent most decimal fractions exactly (`0.1 + 0.2 != 0.3`), so sums
drift and balances stop reconciling. Currencies also differ in precision: EUR has 2 decimal
places, JPY 0, KWD 3.

**Decision.** Every amount is an integer count of the currency's minor unit (`amount_minor
bigint`), with the exponent taken from an ISO 4217 table. FX rates are `Decimal` (`numeric` in
PostgreSQL). A converted amount is rounded to the target's minor unit with `ROUND_HALF_EVEN`, and
the difference between the exact and the rounded amount posts to the platform `fx_pool` account so
every transaction still sums to zero per currency.

**Consequences.** Addition and comparison are exact. Rounding happens in exactly one place (FX).
Banker's rounding avoids a systematic upward bias across many conversions. Display formatting is
the client's job. `bigint` caps an account at about 9.2 × 10^18 minor units, far above any
realistic balance.

**How the residue is booked.** Postings must balance per currency, and a conversion has two:
the seller's wallet pays `fx_pool` in the sold currency, and `fx_pool` pays the buyer's wallet the
*rounded* amount in the bought currency. The sub-minor-unit difference between the exact and the
rounded amount therefore stays with `fx_pool`, and both currencies balance exactly. The quote's
rate is kept on the transaction (`quote_id`), so the exact amount can always be recomputed.
