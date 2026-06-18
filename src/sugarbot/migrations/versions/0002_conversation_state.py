"""conversation_state: inactivity auto-close tracking

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_state",
        sa.Column("phone_number", sa.String(), primary_key=True),
        sa.Column("last_warned_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("conversation_state")
