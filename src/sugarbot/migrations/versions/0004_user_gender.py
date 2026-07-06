"""users: gender (from auth callback, for gender-aware answers)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: older login rows (and callbacks from a site version that doesn't
    # yet send gender) simply carry NULL, and the bot falls back to gender-neutral.
    op.add_column("users", sa.Column("gender", sa.String()))


def downgrade() -> None:
    op.drop_column("users", "gender")
