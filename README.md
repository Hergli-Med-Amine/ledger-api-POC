# ledger-api

A multi-currency wallet and double-entry ledger service for B2B commerce: top-ups, invoice
payments, refunds and FX conversion, with idempotent writes and signed webhooks.

Work in progress — currently at milestone 3 (HTTP API and tenancy).

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
  -H 'content-type: application/json' -d '{"currency": "EUR"}'
curl -X POST localhost:8000/v1/wallets/$WALLET_ID/top-ups -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"amount_minor": 5000, "currency": "EUR"}'
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

- Statements are ordered by posting id (UUIDv7, time of creation), not commit time. A transaction
  that commits after a client has paged past its position is missed in that pass; a new pass
  shows it.
- Logs are not yet structured JSON with request and tenant ids.

## Docs

- [CONTEXT.md](CONTEXT.md) — glossary
- [docs/adr/](docs/adr/) — architecture decision records
