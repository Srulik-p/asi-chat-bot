"""kb_documents / kb_revisions: knowledge base stored in the database.

Seeds kb_documents from the markdown files shipped in knowledge_base/ so the
first deploy behaves exactly like the file-based KB. The seed writes no
revisions; the first admin edit creates revision 1 for a document.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

KB_DIR = Path(__file__).resolve().parents[2] / "knowledge_base"


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String()),
    )
    op.create_table(
        "kb_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("title", sa.String()),
        sa.Column("keywords", sa.JSON()),
        sa.Column("summary", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("hidden", sa.Boolean()),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
        sa.Column("saved_by", sa.String()),
        sa.Column("note", sa.String()),
    )
    op.create_index("ix_kb_revisions_name", "kb_revisions", ["name"])

    # Imported here so merely loading the migration doesn't import the KB.
    from sugarbot.kb import parse_frontmatter

    documents = sa.table(
        "kb_documents",
        sa.column("name", sa.String),
        sa.column("title", sa.String),
        sa.column("keywords", sa.JSON),
        sa.column("summary", sa.Text),
        sa.column("body", sa.Text),
        sa.column("hidden", sa.Boolean),
        sa.column("updated_at", sa.DateTime),
        sa.column("updated_by", sa.String),
    )
    now = datetime.now(timezone.utc)
    rows = []
    for path in sorted(KB_DIR.glob("*.md")):
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "name": path.name,
                "title": meta.get("title", path.stem),
                "keywords": meta.get("keywords", []),
                "summary": meta.get("summary", ""),
                "body": body.strip(),
                "hidden": path.name.startswith("_"),
                "updated_at": now,
                "updated_by": "seed",
            }
        )
    if rows:
        op.bulk_insert(documents, rows)

    app_state = sa.table(
        "app_state",
        sa.column("key", sa.String),
        sa.column("value", sa.JSON),
        sa.column("updated_at", sa.DateTime),
    )
    op.execute(app_state.delete().where(app_state.c.key == "kb_version"))
    op.bulk_insert(
        app_state, [{"key": "kb_version", "value": {"v": 1}, "updated_at": now}]
    )


def downgrade() -> None:
    op.drop_index("ix_kb_revisions_name", table_name="kb_revisions")
    op.drop_table("kb_revisions")
    op.drop_table("kb_documents")
    op.execute(sa.text("DELETE FROM app_state WHERE key = 'kb_version'"))
