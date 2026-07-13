"""support_tickets: persist help-desk tickets opened for a conversation

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phone_number", sa.String(), nullable=False, index=True),
        sa.Column("ticket_id", sa.String()),   # id returned by the help desk (may be null)
        sa.Column("status", sa.String()),      # status returned by the help desk (or "created")
        sa.Column("reason", sa.String()),      # reason key the ticket was filed under
        sa.Column("raw", sa.JSON()),           # full response body, for later shape refinement
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("support_tickets")
