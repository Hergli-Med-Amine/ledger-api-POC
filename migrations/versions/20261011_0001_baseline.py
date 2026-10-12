"""baseline

Empty revision that later migrations build on.

Revision ID: 0001
Revises:
Create Date: 2026-10-11
"""

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
