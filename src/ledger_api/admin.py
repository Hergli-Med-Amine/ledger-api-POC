"""Provision tenants and API keys from the command line; there is deliberately no admin API.

python -m ledger_api.admin create-tenant "Acme Distribution" [--scopes read,write]
python -m ledger_api.admin issue-key TENANT_ID [--scopes read]
python -m ledger_api.admin revoke-key PREFIX
python -m ledger_api.admin set-webhook TENANT_ID URL [--secret SECRET]
"""

import argparse
import os
import secrets
from uuid import UUID

from sqlalchemy import Connection, create_engine, text

from ledger_api.api.auth import issue_api_key, revoke_api_key


def create_tenant(conn: Connection, name: str) -> UUID:
    tenant_id: UUID = conn.execute(
        text("INSERT INTO tenants (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()
    return tenant_id


def set_webhook(conn: Connection, tenant_id: UUID, url: str, secret: str) -> None:
    conn.execute(
        text("UPDATE tenants SET webhook_url = :url, webhook_secret = :secret WHERE id = :id"),
        {"id": tenant_id, "url": url, "secret": secret},
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ledger_api.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-tenant", help="create a tenant and its first API key")
    create.add_argument("name")
    create.add_argument("--scopes", default="read,write")
    issue = commands.add_parser("issue-key", help="issue another API key for a tenant")
    issue.add_argument("tenant_id", type=UUID)
    issue.add_argument("--scopes", default="read,write")
    revoke = commands.add_parser("revoke-key", help="revoke a key by its prefix")
    revoke.add_argument("prefix")
    webhook = commands.add_parser("set-webhook", help="set where a tenant's events are sent")
    webhook.add_argument("tenant_id", type=UUID)
    webhook.add_argument("url")
    webhook.add_argument("--secret", help="signing secret; generated if omitted")
    args = parser.parse_args(argv)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        if args.command == "create-tenant":
            tenant_id = create_tenant(conn, args.name)
            key = issue_api_key(conn, tenant_id, args.scopes.split(","))
            print(f"tenant_id: {tenant_id}\napi_key:   {key}  (shown once; store it now)")
        elif args.command == "issue-key":
            key = issue_api_key(conn, args.tenant_id, args.scopes.split(","))
            print(f"api_key: {key}  (shown once; store it now)")
        elif args.command == "set-webhook":
            secret = args.secret or f"whsec_{secrets.token_urlsafe(32)}"
            set_webhook(conn, args.tenant_id, args.url, secret)
            print(f"webhook_secret: {secret}  (the tenant verifies signatures with it)")
        elif not revoke_api_key(conn, args.prefix):
            parser.exit(1, f"no active key with prefix {args.prefix}\n")


if __name__ == "__main__":
    main()
