"""idempotency keys

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE idempotency_keys (
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            key text NOT NULL,
            request_hash bytea NOT NULL,
            response_code smallint NOT NULL,
            response_body jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, key)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_keys")
