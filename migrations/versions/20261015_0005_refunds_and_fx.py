"""refunds and FX: quotes, refund and conversion transactions

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fx_quotes (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            from_currency char(3) NOT NULL,
            to_currency char(3) NOT NULL CHECK (to_currency <> from_currency),
            rate numeric NOT NULL CHECK (rate > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at timestamptz
        )
    """)
    op.execute("ALTER TABLE transactions DROP CONSTRAINT transactions_kind_check")
    op.execute("""
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_kind_check
                CHECK (kind IN ('top_up', 'payment', 'refund', 'conversion')),
            ADD COLUMN refund_of uuid REFERENCES transactions (id),
            -- UNIQUE: a quote backs at most one conversion, even if the code had a bug.
            ADD COLUMN quote_id uuid UNIQUE REFERENCES fx_quotes (id),
            ADD CHECK ((kind = 'refund') = (refund_of IS NOT NULL)),
            ADD CHECK ((kind = 'conversion') = (quote_id IS NOT NULL))
    """)
    op.execute("CREATE INDEX transactions_refund_of ON transactions (refund_of)")


def downgrade() -> None:
    op.execute("DROP INDEX transactions_refund_of")
    op.execute("ALTER TABLE transactions DROP COLUMN quote_id, DROP COLUMN refund_of")
    op.execute("ALTER TABLE transactions DROP CONSTRAINT transactions_kind_check")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT transactions_kind_check
            CHECK (kind IN ('top_up', 'payment'))
    """)
    op.execute("DROP TABLE fx_quotes")
