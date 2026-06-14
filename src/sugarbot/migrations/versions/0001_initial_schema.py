"""initial schema: users + messages

Revision ID: 0001
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("phone_number", sa.String(), primary_key=True),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("nickname", sa.String()),
        sa.Column("is_premium", sa.Boolean(), nullable=False),
        sa.Column("labels", sa.JSON()),
        sa.Column("access_token", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("tool_calls", sa.JSON()),
        sa.Column("tool_call_id", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_messages_phone_number", "messages", ["phone_number"])


def downgrade() -> None:
    op.drop_index("ix_messages_phone_number", table_name="messages")
    op.drop_table("messages")
    op.drop_table("users")
