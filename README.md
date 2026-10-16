# ledger-api

A multi-currency wallet and double-entry ledger service for B2B commerce: top-ups, invoice
payments, refunds and FX conversion, with idempotent writes and signed webhooks.

Work in progress — currently at milestone 7 (rate limiting and deployment).

## Run locally

Requires Docker.

```sh
docker compose up --build
curl http://localhost:8000/health
```

`docker compose up` starts PostgreSQL and Redis, runs Alembic migrations once, then starts the API
on port 8000, the outbox worker, and a stand-in webhook receiver on port 9000.

Create a tenant and its first API key (the key is printed once):

```sh
docker compose run --rm migrate python -m ledger_api.admin create-tenant "Acme Distribution"
```

To receive webhooks, point the tenant at the stand-in receiver (its secret is fixed in
`docker-compose.yml`), then watch `docker compose logs -f webhook-receiver`:

```sh
docker compose run --rm migrate python -m ledger_api.admin set-webhook "$TENANT_ID" \
  http://webhook-receiver:9000/ --secret whsec_local_demo
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
Every money movement emits a signed webhook (`top_up.created`, `payment.created`,
`refund.created`, `conversion.created`), delivered at least once by the outbox worker; dedupe on
`Ledger-Event-Id`. How to verify the signature: [ADR 5](docs/adr/0005-webhook-signing.md).
Each API key may make `RATE_LIMIT_BURST` requests at once, refilled at `RATE_LIMIT_PER_SECOND`;
beyond that, `429` with `Retry-After`. If Redis is down the limit is skipped, never the request
([ADR 7](docs/adr/0007-redis-only-for-rate-limiting.md)).
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

## Deploy (GCP Cloud Run, Neon, Upstash)

Infrastructure is in [infra/terraform](infra/terraform): the API as a Cloud Run service, migrations
and the outbox relay as Cloud Run jobs (the relay runs every 5 minutes via Cloud Scheduler), images
in Artifact Registry, connection strings in Secret Manager, and a Workload Identity Federation
provider so GitHub Actions deploys without any stored key. PostgreSQL is
[Neon](https://neon.tech) and Redis is [Upstash](https://upstash.com).

One-time setup:

1. Create a GCP project with billing, a Neon database (use the direct, not `-pooler`, host) and an
   Upstash Redis database.
2. In `infra/terraform`, write `terraform.tfvars` with `project_id`, `region`,
   `github_repository` (`owner/name`) and `image`
   (`<region>-docker.pkg.dev/<project>/ledger-api/ledger-api:bootstrap`), then:
   ```sh
   terraform init
   terraform apply -target=google_artifact_registry_repository.images \
     -target=google_secret_manager_secret.database_url -target=google_secret_manager_secret.redis_url
   printf '%s' 'postgresql+psycopg://USER:PASSWORD@HOST/DB?sslmode=require' |
     gcloud secrets versions add database-url --data-file=-
   printf '%s' 'rediss://default:PASSWORD@HOST:6379' | gcloud secrets versions add redis-url --data-file=-
   gcloud auth configure-docker <region>-docker.pkg.dev
   docker build -t <image> ../.. && docker push <image>
   terraform apply
   gcloud run jobs execute ledger-migrate --region <region> --wait
   ```
3. In the GitHub repository settings, add the variables `GCP_PROJECT_ID`, `GCP_REGION`,
   `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_DEPLOYER_SERVICE_ACCOUNT` (the last two are Terraform
   outputs).

From then on every push to `main` runs CI, builds the image, runs migrations, deploys, and smoke
tests the live URL. Until the variables exist, the deploy job is skipped.

## Limitations

- FX rates are a static, fictional table (`src/ledger_api/fx_rates.json`), not a live feed.
- A payment's `reference` (e.g. an invoice number) is not unique: a client that retries with a
  *new* idempotency key can pay the same invoice twice. A unique index on
  `(tenant_id, reference)` for payments would catch that server-side.
- Webhook secrets are stored in plain text (they must be readable to sign); a production system
  would encrypt them with a key from a secret manager.
- Dead webhook events (10 failed attempts) are kept but there is no command to re-send them.
- Retry delays have no random jitter, so many events failing together retry together.
- In the cloud, webhooks are sent by a job every 5 minutes, so they arrive up to 5 minutes late;
  an always-on worker (`python -m ledger_api.worker`, a Cloud Run worker pool) would remove the
  delay at the cost of paying for an idle instance.
- Terraform state is local; a team would keep it in a GCS bucket.
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
