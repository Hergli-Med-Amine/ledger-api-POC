"""outbox and tenant webhook endpoints

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN webhook_url text, ADD COLUMN webhook_secret text")
    op.execute("""
        CREATE TABLE outbox (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            event_type text NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            attempts integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            last_error text,
            sent_at timestamptz,
            dead_at timestamptz
        )
    """)
    # The relay's only query: pending events that are due, oldest first.
    op.execute("""
        CREATE INDEX outbox_due ON outbox (next_attempt_at)
            WHERE sent_at IS NULL AND dead_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE outbox")
    op.execute("ALTER TABLE tenants DROP COLUMN webhook_url, DROP COLUMN webhook_secret")
