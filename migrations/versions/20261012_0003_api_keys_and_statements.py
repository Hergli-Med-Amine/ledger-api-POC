"""api keys, transaction reference, statement index

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12
"""

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            prefix text NOT NULL UNIQUE,
            hash bytea NOT NULL,
            scopes text[] NOT NULL CHECK (scopes <@ ARRAY['read', 'write']),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
    """)
    op.execute("ALTER TABLE transactions ADD COLUMN reference text")
    # Serves statements: one wallet's postings, newest first, resumed from a cursor id.
    op.execute("DROP INDEX postings_account_id")
    op.execute("CREATE INDEX postings_account_id_id ON postings (account_id, id)")


def downgrade() -> None:
    op.execute("DROP INDEX postings_account_id_id")
    op.execute("CREATE INDEX postings_account_id ON postings (account_id)")
    op.execute("ALTER TABLE transactions DROP COLUMN reference")
    op.execute("DROP TABLE api_keys")
