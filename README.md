# ledger-api

A multi-currency wallet and double-entry ledger service for B2B commerce: top-ups, invoice
payments, refunds and FX conversion, with idempotent writes and signed webhooks.

Work in progress — currently at milestone 2 (money and ledger core, no HTTP API yet).

## Run locally

Requires Docker.

```sh
docker compose up --build
curl http://localhost:8000/health
```

`docker compose up` starts PostgreSQL and Redis, runs Alembic migrations once, then starts the API
on port 8000.

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Docker: integration and property tests start a
real PostgreSQL with Testcontainers.

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

## Docs

- [CONTEXT.md](CONTEXT.md) — glossary
- [docs/adr/](docs/adr/) — architecture decision records
