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
    func,
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
    # Pin the Postgres session to UTC. updated_at/created_at are naive
    # TIMESTAMP columns written as datetime.now(timezone.utc); a non-UTC server
    # timezone would shift their wall-clock and skew the freshness math in
    # server._login_is_stale (which reads them back as UTC). SQLite has no
    # session timezone and already round-trips the UTC value we wrote.
    connect_args={"options": "-c timezone=utc"} if _IS_POSTGRES else {},
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


# Per-conversation lifecycle state for the inactivity auto-close sweep. Separate
# from messages so we can record "warned"/"closed" without polluting the chat
# log replay. A row appears the first time a conversation is warned; it is
# deleted whenever the customer sends a new message (which reopens the inquiry).
conversation_state_table = Table(
    "conversation_state",
    _metadata,
    Column("phone_number", String, primary_key=True),
    Column("last_warned_at", DateTime),  # when the pre-close warning was sent
    Column("closed_at", DateTime),       # when the inquiry was auto-closed
)


def init_db() -> None:
    """Bring the DB schema up to the latest Alembic revision.

    Runs `alembic upgrade head` against the same engine the app uses. Safe
    to run on every startup: Alembic uses the alembic_version table to
    skip migrations that have already been applied.
    """
    # Imported here to avoid a hard dependency at module load — db.py is also
    # used by Alembic env.py, and Alembic imports db at its own startup time.
    import sys
    import traceback
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import inspect, text

    # One-time Alembic adoption. The original schema was created ad-hoc
    # (pre-Alembic) and predates the nickname/is_premium/labels columns, so it
    # has no alembic_version table. Migration 0001 would then hit "relation
    # users already exists" and crash startup. Detect that exact state and drop
    # the legacy tables so 0001 can recreate them cleanly. Guarded on the
    # absence of alembic_version, so it runs at most once and never touches a
    # DB that Alembic already manages.
    with _engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and (tables & {"users", "messages"}):
            print(
                "[init_db] legacy pre-Alembic schema detected; dropping users/messages "
                "so migration 0001 can recreate them",
                file=sys.stderr,
            )
            cascade = " CASCADE" if _IS_POSTGRES else ""
            conn.execute(text(f"DROP TABLE IF EXISTS messages{cascade}"))
            conn.execute(text(f"DROP TABLE IF EXISTS users{cascade}"))

    # File-less Config: migrations ship inside the package, so the runtime
    # never depends on alembic.ini (that file is for the dev CLI only).
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _URL)
    try:
        command.upgrade(cfg, "head")
    except Exception:
        # Surface the real traceback in Cloud Run logs — a bare migration
        # failure here otherwise only shows up as "container failed to listen".
        print("[init_db] alembic upgrade failed:", file=sys.stderr)
        traceback.print_exc()
        raise


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
        # A new customer message reopens the inquiry: drop any warned/closed
        # state so the auto-close sweep starts the idle clock fresh.
        if role == "user":
            conn.execute(
                delete(conversation_state_table).where(
                    conversation_state_table.c.phone_number == phone_number
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


# ---------- inactivity auto-close ----------

def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a stored timestamp to tz-aware UTC (SQLite hands back naive)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def idle_assistant_conversations(idle_before: datetime) -> list[dict]:
    """Conversations awaiting a customer reply that have gone quiet.

    Returns one entry per conversation whose most-recent message is an assistant
    reply created at or before `idle_before` and that has not been auto-closed.
    Each entry carries the last-message timestamp and the warn/close state so the
    sweep can decide whether to warn or close. Timestamps are tz-aware UTC.
    """
    latest = (
        select(
            messages_table.c.phone_number,
            func.max(messages_table.c.id).label("mid"),
        )
        .group_by(messages_table.c.phone_number)
        .subquery()
    )
    m = messages_table
    cs = conversation_state_table
    stmt = (
        select(
            m.c.phone_number,
            m.c.created_at,
            cs.c.last_warned_at,
            cs.c.closed_at,
        )
        .select_from(
            latest.join(m, m.c.id == latest.c.mid).outerjoin(
                cs, cs.c.phone_number == m.c.phone_number
            )
        )
        .where(m.c.role == "assistant")
        .where(m.c.created_at <= idle_before)
    )
    with _engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    out: list[dict] = []
    for r in rows:
        if r["closed_at"] is not None:
            continue
        out.append(
            {
                "phone_number": r["phone_number"],
                "last_message_at": _as_utc(r["created_at"]),
                "last_warned_at": _as_utc(r["last_warned_at"]),
            }
        )
    return out


def _upsert_conversation_state(phone_number: str, **values) -> None:
    insert = pg_insert if _IS_POSTGRES else sqlite_insert
    stmt = insert(conversation_state_table).values(phone_number=phone_number, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[conversation_state_table.c.phone_number],
        set_=values,
    )
    with _engine.begin() as conn:
        conn.execute(stmt)


def mark_warned(phone_number: str, when: datetime) -> None:
    _upsert_conversation_state(phone_number, last_warned_at=when)


def mark_closed(phone_number: str, when: datetime) -> None:
    _upsert_conversation_state(phone_number, closed_at=when)


def get_conversation_state(phone_number: str) -> Optional[dict]:
    with _engine.connect() as conn:
        row = conn.execute(
            select(conversation_state_table).where(
                conversation_state_table.c.phone_number == phone_number
            )
        ).mappings().first()
    if not row:
        return None
    return {
        "phone_number": row["phone_number"],
        "last_warned_at": _as_utc(row["last_warned_at"]),
        "closed_at": _as_utc(row["closed_at"]),
    }
