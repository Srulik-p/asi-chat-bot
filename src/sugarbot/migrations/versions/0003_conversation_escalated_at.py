"""conversation_state: escalated_at (exempt from inactivity auto-close)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_state", sa.Column("escalated_at", sa.DateTime()))


def downgrade() -> None:
    op.drop_column("conversation_state", "escalated_at")
