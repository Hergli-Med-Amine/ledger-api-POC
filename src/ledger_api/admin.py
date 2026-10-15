"""Provision tenants and API keys from the command line; there is deliberately no admin API.

python -m ledger_api.admin create-tenant "Acme Distribution" [--scopes read,write]
python -m ledger_api.admin issue-key TENANT_ID [--scopes read]
python -m ledger_api.admin revoke-key PREFIX
"""

import argparse
import os
from uuid import UUID

from sqlalchemy import Connection, create_engine, text

from ledger_api.api.auth import issue_api_key, revoke_api_key


def create_tenant(conn: Connection, name: str) -> UUID:
    tenant_id: UUID = conn.execute(
        text("INSERT INTO tenants (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()
    return tenant_id


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
        elif not revoke_api_key(conn, args.prefix):
            parser.exit(1, f"no active key with prefix {args.prefix}\n")


if __name__ == "__main__":
    main()
