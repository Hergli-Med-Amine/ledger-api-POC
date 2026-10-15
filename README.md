# ledger-api

A multi-currency wallet and double-entry ledger service for B2B commerce: top-ups, invoice
payments, refunds and FX conversion, with idempotent writes and signed webhooks.

Work in progress — currently at milestone 5 (refunds and FX).

## Run locally

Requires Docker.

```sh
docker compose up --build
curl http://localhost:8000/health
```

`docker compose up` starts PostgreSQL and Redis, runs Alembic migrations once, then starts the API
on port 8000.

Create a tenant and its first API key (the key is printed once):

```sh
docker compose run --rm migrate python -m ledger_api.admin create-tenant "Acme Distribution"
```

Then, with `KEY` set to that key:

```sh
curl -X POST localhost:8000/v1/wallets -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(uuidgen)" -H 'content-type: application/json' -d '{"currency": "EUR"}'
curl -X POST localhost:8000/v1/wallets/$WALLET_ID/top-ups -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(uuidgen)" -H 'content-type: application/json' \
  -d '{"amount_minor": 5000, "currency": "EUR"}'
curl "localhost:8000/v1/wallets/$WALLET_ID/statement?limit=20" -H "Authorization: Bearer $KEY"
```

## API

The full contract is [docs/openapi.json](docs/openapi.json) (also served at `/docs` when running).
A test fails if it drifts from the code.

| Method | Path | Scope |
|---|---|---|
| `POST` | `/v1/wallets` | write |
| `GET` | `/v1/wallets` | read |
| `GET` | `/v1/wallets/{id}` | read |
| `POST` | `/v1/wallets/{id}/top-ups` | write |
| `POST` | `/v1/wallets/{id}/payments` | write |
| `GET` | `/v1/wallets/{id}/statement?limit=&cursor=` | read |
| `POST` | `/v1/wallets/{id}/payments/{payment_id}/refunds` | write |
| `POST` | `/v1/fx/quotes` | write |
| `POST` | `/v1/fx/conversions` | write |

Refunds can be partial; together they never exceed the original payment. An FX quote fixes a rate
from a static table for 60 seconds and backs exactly one conversion between the tenant's two
wallets; the bought amount is rounded half-even to the currency's minor unit
([ADR 1](docs/adr/0001-money-as-integer-minor-units.md)).

Every `POST` requires an `Idempotency-Key` header (any printable ASCII, up to 255 characters).
Retrying with the same key and body returns the original response with
`Idempotent-Replayed: true` and moves no money; the same key with a different body is `422`; while
the first request is still running, `409` ([ADR 3](docs/adr/0003-idempotency-in-postgres.md)).
Every response carries an `X-Request-Id`, also found on every log line for that request.

Amounts are integers in the currency's minor unit (`1250` EUR is €12.50). Errors are
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json`; the `type` field is
a stable identifier such as `/problems/insufficient-funds` (see `src/ledger_api/api/errors.py` for
the full list).

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Docker: integration and property tests start a
real PostgreSQL with Testcontainers.

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

## Limitations

- FX rates are a static, fictional table (`src/ledger_api/fx_rates.json`), not a live feed.
- A payment's `reference` (e.g. an invoice number) is not unique: a client that retries with a
  *new* idempotency key can pay the same invoice twice. A unique index on
  `(tenant_id, reference)` for payments would catch that server-side.
- Idempotency keys are kept forever; a production system would expire them (e.g. after 24 h).

- Statements are ordered by posting id (UUIDv7, time of creation), not commit time. A transaction
  that commits after a client has paged past its position is missed in that pass; a new pass
  shows it.

## Docs

- [CONTEXT.md](CONTEXT.md) — glossary
- [docs/adr/](docs/adr/) — architecture decision records

## Disclosure

This project is a portfolio piece, built to demonstrate correctness under failure in a payments
ledger: retries, concurrency and partial outages. It is not a production system: there are no real
bank or card integrations, FX rates come from a static fixture, and all data is fictional.

It was co-developed with Claude Code, using AI-assisted development.
