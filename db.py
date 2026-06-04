"""User store — Postgres in prod, SQLite locally.

Switches on DATABASE_URL:
  - set    → Postgres (via psycopg3, driver URL `postgresql+psycopg://...`)
  - unset  → SQLite at $USERS_DB_PATH (default ./users.db)

The phone number is private and never goes to the LLM — see
assistant.scrub_messages for the model-facing redaction layer.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def _resolve_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        path = Path(os.getenv("USERS_DB_PATH", "users.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    # Heroku-style `postgres://` is rejected by SQLAlchemy 1.4+; normalise.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # Force psycopg3 driver (we install psycopg[binary], not psycopg2).
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


_URL = _resolve_url()
_IS_POSTGRES = _URL.startswith("postgresql+")

_engine = create_engine(
    _URL,
    future=True,
    pool_pre_ping=_IS_POSTGRES,  # cheap NOOP for sqlite
    pool_size=5 if _IS_POSTGRES else 0,
    max_overflow=5 if _IS_POSTGRES else 0,
)

_metadata = MetaData()

users_table = Table(
    "users",
    _metadata,
    Column("phone_number", String, primary_key=True),
    Column("external_id", String, nullable=False),    # payload.user.id
    Column("nickname", String),                        # payload.user.nickname
    Column("is_premium", Boolean, nullable=False),     # payload.user.isPremium
    Column("labels", JSON),                            # payload.user.labels — list[{id,name}]
    Column("access_token", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

# Per-conversation log. Keyed by phone_number — a row exists whether or not
# the user has signed in (the users row only appears after auth callback).
# Tool-call rounds are stored too so the OpenAI tool-call loop can be replayed.
messages_table = Table(
    "messages",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("phone_number", String, nullable=False, index=True),
    Column("role", String, nullable=False),  # user | assistant | tool | system
    Column("content", Text),                  # may be None for tool-call-only assistant turns
    Column("tool_calls", JSON),               # list[dict] when present
    Column("tool_call_id", String),           # set for role=tool
    Column("created_at", DateTime, nullable=False),
)


def init_db() -> None:
    _metadata.create_all(_engine)


def upsert_user(
    phone_number: str,
    external_id: str,
    nickname: Optional[str],
    is_premium: bool,
    labels: list[dict],
    access_token: str,
) -> None:
    # Timestamps set in Python so the INSERT always supplies them, regardless
    # of whether the table was created by an older schema without DEFAULTs.
    now = datetime.now(timezone.utc)
    insert = pg_insert if _IS_POSTGRES else sqlite_insert
    stmt = insert(users_table).values(
        phone_number=phone_number,
        external_id=external_id,
        nickname=nickname,
        is_premium=is_premium,
        labels=labels,
        access_token=access_token,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[users_table.c.phone_number],
        set_={
            "external_id": stmt.excluded.external_id,
            "nickname": stmt.excluded.nickname,
            "is_premium": stmt.excluded.is_premium,
            "labels": stmt.excluded.labels,
            "access_token": stmt.excluded.access_token,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    with _engine.begin() as conn:
        conn.execute(stmt)


def get_user_by_phone(phone_number: str) -> Optional[dict]:
    with _engine.connect() as conn:
        row = conn.execute(
            select(users_table).where(users_table.c.phone_number == phone_number)
        ).mappings().first()
        return dict(row) if row else None


def append_message(
    phone_number: str,
    role: str,
    content: Optional[str] = None,
    tool_calls: Optional[list] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    with _engine.begin() as conn:
        conn.execute(
            messages_table.insert().values(
                phone_number=phone_number,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                created_at=datetime.now(timezone.utc),
            )
        )


def load_history(phone_number: str) -> list[dict]:
    """Return messages for `phone_number` in OpenAI chat-completion shape."""
    with _engine.connect() as conn:
        rows = conn.execute(
            select(messages_table)
            .where(messages_table.c.phone_number == phone_number)
            .order_by(messages_table.c.id)
        ).mappings().all()
    out: list[dict] = []
    for r in rows:
        m: dict = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            m["tool_calls"] = r["tool_calls"]
        if r["tool_call_id"]:
            m["tool_call_id"] = r["tool_call_id"]
        out.append(m)
    return out


def clear_history(phone_number: str) -> int:
    with _engine.begin() as conn:
        result = conn.execute(
            delete(messages_table).where(messages_table.c.phone_number == phone_number)
        )
        return result.rowcount or 0
