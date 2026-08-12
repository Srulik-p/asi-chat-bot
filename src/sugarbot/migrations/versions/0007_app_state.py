"""app_state: small key-value store for cross-instance runtime state.

First user: the managed admin bearer token ("admin_token"), so Cloud Run
instances share one login instead of each logging in on cold start.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_state")
