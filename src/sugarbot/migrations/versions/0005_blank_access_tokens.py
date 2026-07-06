"""users: blank stored access tokens (no longer persisted)

The auth callback used to persist the site's live session token per customer.
Nothing ever read it back, and a plaintext bearer token per user is pure
breach liability — so the app now always writes "" and this migration scrubs
the tokens that were already stored.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET access_token = ''")


def downgrade() -> None:
    # The scrubbed tokens are gone by design; nothing to restore.
    pass
