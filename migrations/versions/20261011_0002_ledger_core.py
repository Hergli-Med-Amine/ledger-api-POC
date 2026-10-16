"""ledger core: tenants, accounts, transactions, postings, balances

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenants (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            name text NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE accounts (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            tenant_id uuid REFERENCES tenants (id),
            currency char(3) NOT NULL,
            kind text NOT NULL
                CHECK (kind IN ('wallet', 'external_funding', 'supplier_payable', 'fx_pool')),
            -- Wallets belong to a tenant; every other kind is platform-owned.
            CHECK ((kind = 'wallet') = (tenant_id IS NOT NULL)),
            -- Target of the postings foreign key, so a posting's currency must match its account's.
            UNIQUE (id, currency)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX accounts_one_wallet_per_currency
            ON accounts (tenant_id, currency) WHERE kind = 'wallet'
    """)
    op.execute("""
        CREATE UNIQUE INDEX accounts_one_platform_account_per_currency
            ON accounts (kind, currency) WHERE tenant_id IS NULL
    """)
    op.execute("""
        CREATE TABLE transactions (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            kind text NOT NULL CHECK (kind IN ('top_up', 'payment')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE postings (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            transaction_id uuid NOT NULL REFERENCES transactions (id),
            account_id uuid NOT NULL,
            amount_minor bigint NOT NULL CHECK (amount_minor <> 0),
            currency char(3) NOT NULL,
            FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency)
        )
    """)
    op.execute("CREATE INDEX postings_transaction_id ON postings (transaction_id)")
    op.execute("CREATE INDEX postings_account_id ON postings (account_id)")
    op.execute("""
        CREATE TABLE balances (
            account_id uuid PRIMARY KEY REFERENCES accounts (id),
            amount_minor bigint NOT NULL
        )
    """)

    # Checked at COMMIT, once all postings exist: no unbalanced transaction can commit.
    op.execute("""
        CREATE FUNCTION postings_check_balanced() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM postings
                WHERE transaction_id = NEW.transaction_id
                GROUP BY currency
                HAVING SUM(amount_minor) <> 0
            ) THEN
                RAISE EXCEPTION 'transaction % does not balance', NEW.transaction_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER postings_balanced
            AFTER INSERT ON postings
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION postings_check_balanced()
    """)

    # Postings are an append-only record; corrections are new transactions.
    op.execute("""
        CREATE FUNCTION postings_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'postings are append-only' USING ERRCODE = 'restrict_violation';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER postings_append_only
            BEFORE UPDATE OR DELETE ON postings
            FOR EACH ROW EXECUTE FUNCTION postings_reject_change()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE balances, postings, transactions, accounts, tenants")
    op.execute("DROP FUNCTION postings_check_balanced, postings_reject_change")
