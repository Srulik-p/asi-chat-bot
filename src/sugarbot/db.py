"""User store — Postgres in prod, SQLite locally.

Switches on DATABASE_URL:
  - set    → Postgres (via psycopg3, driver URL `postgresql+psycopg://...`)
  - unset  → SQLite at $USERS_DB_PATH (default ./users.db)

The phone number is private and never goes to the LLM — see
assistant.scrub_messages for the model-facing redaction layer.
"""

from __future__ import annotations

import os
import sys
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
        # The SQLite fallback is for LOCAL DEV ONLY. On Cloud Run it would boot
        # green but write everything to the instance's ephemeral disk — chats
        # split across instances and vanish on recycle — so refuse to start.
        if os.getenv("K_SERVICE"):
            raise RuntimeError(
                "DATABASE_URL is required on Cloud Run: the SQLite fallback "
                "writes to ephemeral disk and silently loses all data."
            )
        path = Path(os.getenv("USERS_DB_PATH", "users.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[db] DATABASE_URL not set — using local SQLite at {path}", file=sys.stderr)
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
    Column("gender", String),                          # payload.user.gender — nullable; drives gender-aware answers
    Column("labels", JSON),                            # payload.user.labels — list[{id,name}]
    # Always stored as "" — the callback's live site token is deliberately NOT
    # persisted (nothing reads it, and a plaintext session token per customer is
    # pure breach liability; see migration 0005). If a feature ever needs it,
    # store it encrypted with a TTL.
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
    Column("escalated_at", DateTime),    # handed to a human rep — exempt from auto-close
)


# Help-desk tickets opened for a conversation (via contact.submit_ticket or,
# for account-linked customers, contact.submit_admin_ticket).
# Keyed by phone_number so later turns can surface an open ticket as context.
# ticket_id/status come from the backend response; `raw` is a wrapper
# {"on_account": bool, "response": <full body>} recording which path filed the
# ticket, kept whole because the backend's exact response shape is not
# documented. Nothing parses `raw` programmatically — it is for ops inspection.
app_state_table = Table(
    "app_state",
    _metadata,
    Column("key", String, primary_key=True),
    Column("value", JSON),
    Column("updated_at", DateTime, nullable=False),
)

support_tickets_table = Table(
    "support_tickets",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("phone_number", String, nullable=False, index=True),
    Column("ticket_id", String),   # id returned by the help desk (may be None)
    Column("status", String),      # status returned by the help desk (or "created")
    Column("reason", String),      # reason key the ticket was filed under
    Column("raw", JSON),           # full response body, for later shape refinement
    Column("created_at", DateTime, nullable=False),
)


kb_documents_table = Table(
    "kb_documents",
    _metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("keywords", JSON, nullable=False),
    Column("summary", Text, nullable=False, default=""),
    Column("body", Text, nullable=False),
    Column("hidden", Boolean, nullable=False, default=False),
    Column("updated_at", DateTime, nullable=False),
    Column("updated_by", String),
)

kb_revisions_table = Table(
    "kb_revisions",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False, index=True),
    Column("title", String),
    Column("keywords", JSON),
    Column("summary", Text),
    Column("body", Text),
    Column("hidden", Boolean),
    Column("saved_at", DateTime, nullable=False),
    Column("saved_by", String),
    Column("note", String),
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
    # users already exists" and crash startup. That state also matches a
    # restored backup that merely lost its alembic_version table, so the drop
    # is destructive-by-default on an ambiguous signal — it therefore requires
    # an explicit opt-in via SUGARBOT_ADOPT_LEGACY_DB=1 and otherwise refuses
    # to start with instructions.
    with _engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and (tables & {"users", "messages"}):
            if os.getenv("SUGARBOT_ADOPT_LEGACY_DB") != "1":
                raise RuntimeError(
                    "users/messages tables exist but alembic_version is missing. "
                    "If this really is the legacy pre-Alembic schema, set "
                    "SUGARBOT_ADOPT_LEGACY_DB=1 to DROP and recreate them; if the "
                    "schema already matches head (e.g. a restored backup), run "
                    "'alembic stamp head' instead. Refusing to drop data on an "
                    "ambiguous signal."
                )
            print(
                "[init_db] SUGARBOT_ADOPT_LEGACY_DB=1: dropping legacy users/messages "
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
    gender: Optional[str] = None,
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
        gender=gender,
        labels=labels,
        access_token="",  # never persist the live token — see column comment
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[users_table.c.phone_number],
        set_={
            "external_id": stmt.excluded.external_id,
            "nickname": stmt.excluded.nickname,
            "is_premium": stmt.excluded.is_premium,
            "gender": stmt.excluded.gender,
            "labels": stmt.excluded.labels,
            "access_token": stmt.excluded.access_token,  # "" — clears any legacy stored token
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


def load_history(phone_number: str, limit: Optional[int] = None) -> list[dict]:
    """Return messages for `phone_number` in OpenAI chat-completion shape.

    `limit` returns only the newest N rows (still in chronological order) —
    the chat loop replays a bounded window, so it should not pull a months-old
    log over the wire every turn. None returns the full log (human-facing
    /chat/history)."""
    with _engine.connect() as conn:
        stmt = select(messages_table).where(messages_table.c.phone_number == phone_number)
        if limit is None:
            rows = conn.execute(stmt.order_by(messages_table.c.id)).mappings().all()
        else:
            rows = conn.execute(
                stmt.order_by(messages_table.c.id.desc()).limit(limit)
            ).mappings().all()
            rows.reverse()
    out: list[dict] = []
    for r in rows:
        m: dict = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            m["tool_calls"] = r["tool_calls"]
        if r["tool_call_id"]:
            m["tool_call_id"] = r["tool_call_id"]
        out.append(m)
    return out


def last_message_at(phone_number: str) -> Optional[datetime]:
    """Timestamp (UTC) of the newest message in the conversation, or None.

    Used by the post-login push to tell "the customer just asked this" apart
    from "this thread has been cold for weeks"."""
    with _engine.connect() as conn:
        row = conn.execute(
            select(messages_table.c.created_at)
            .where(messages_table.c.phone_number == phone_number)
            .order_by(messages_table.c.id.desc())
            .limit(1)
        ).first()
    return _as_utc(row[0]) if row else None


def clear_history(phone_number: str) -> int:
    with _engine.begin() as conn:
        result = conn.execute(
            delete(messages_table).where(messages_table.c.phone_number == phone_number)
        )
        return result.rowcount or 0


def delete_user_data(phone_number: str) -> dict:
    """Erase everything stored for a phone number: chat history, the cached
    login row (nickname/labels/access token), conversation lifecycle state, and
    any support tickets filed for the conversation.

    Atomic — all deletes share one transaction. Returns the per-table row
    counts removed.
    """
    with _engine.begin() as conn:
        messages = conn.execute(
            delete(messages_table).where(messages_table.c.phone_number == phone_number)
        ).rowcount or 0
        user = conn.execute(
            delete(users_table).where(users_table.c.phone_number == phone_number)
        ).rowcount or 0
        state = conn.execute(
            delete(conversation_state_table).where(
                conversation_state_table.c.phone_number == phone_number
            )
        ).rowcount or 0
        tickets = conn.execute(
            delete(support_tickets_table).where(
                support_tickets_table.c.phone_number == phone_number
            )
        ).rowcount or 0
    return {
        "messages": messages,
        "user": user,
        "conversation_state": state,
        "support_tickets": tickets,
    }


# ---------- inactivity auto-close ----------

def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a stored timestamp to tz-aware UTC (SQLite hands back naive)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def idle_assistant_conversations(idle_before: datetime, limit: int = 200) -> list[dict]:
    """Conversations awaiting a customer reply that have gone quiet.

    Returns up to `limit` entries (oldest-idle first) for conversations whose
    most-recent message is an assistant reply created at or before `idle_before`
    and that have NOT been auto-closed or handed to a human rep. Each entry
    carries the last-message timestamp and the warn state so the sweep can decide
    whether to warn or close. Timestamps are tz-aware UTC.

    Closed/escalated conversations are excluded in SQL so `limit` always yields
    actionable rows; ordering oldest-first means the most-overdue are handled
    even when the backlog exceeds one batch.
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
        )
        .select_from(
            latest.join(m, m.c.id == latest.c.mid).outerjoin(
                cs, cs.c.phone_number == m.c.phone_number
            )
        )
        .where(m.c.role == "assistant")
        .where(m.c.created_at <= idle_before)
        # outer-joined NULLs (no state row) pass; only set closed/escalated drop.
        .where(cs.c.closed_at.is_(None))
        .where(cs.c.escalated_at.is_(None))
        .order_by(m.c.created_at.asc())
        .limit(limit)
    )
    with _engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            "phone_number": r["phone_number"],
            "last_message_at": _as_utc(r["created_at"]),
            "last_warned_at": _as_utc(r["last_warned_at"]),
        }
        for r in rows
    ]


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


def mark_escalated(phone_number: str, when: datetime) -> None:
    """Flag a conversation as handed to a human rep. The inactivity sweep skips
    escalated conversations so we never auto-close a ticket a rep still owes a
    reply on. Cleared when the customer sends a new message (see append_message)."""
    _upsert_conversation_state(phone_number, escalated_at=when)


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
        "escalated_at": _as_utc(row["escalated_at"]),
    }


# ---------- help-desk tickets ----------

def add_support_ticket(
    phone_number: str,
    ticket_id: Optional[str],
    status: Optional[str],
    reason: Optional[str],
    raw: Optional[object] = None,
) -> int:
    """Persist a filed support ticket. Returns the new row's local id."""
    with _engine.begin() as conn:
        result = conn.execute(
            support_tickets_table.insert().values(
                phone_number=phone_number,
                ticket_id=ticket_id,
                status=status,
                reason=reason,
                raw=raw,
                created_at=datetime.now(timezone.utc),
            )
        )
    pk = result.inserted_primary_key
    return pk[0] if pk else 0


def recent_support_tickets(
    phone_number: str, since: Optional[datetime] = None, limit: int = 5
) -> list[dict]:
    """Most-recent-first tickets for a phone, optionally only those since `since`."""
    stmt = select(support_tickets_table).where(
        support_tickets_table.c.phone_number == phone_number
    )
    if since is not None:
        stmt = stmt.where(support_tickets_table.c.created_at >= since)
    stmt = stmt.order_by(support_tickets_table.c.id.desc()).limit(limit)
    with _engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            "id": r["id"],
            "ticket_id": r["ticket_id"],
            "status": r["status"],
            "reason": r["reason"],
            "created_at": _as_utc(r["created_at"]),
        }
        for r in rows
    ]


def _set_app_state(key: str, value, conn=None) -> None:
    insert = pg_insert if _IS_POSTGRES else sqlite_insert
    now = datetime.now(timezone.utc)
    stmt = insert(app_state_table).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[app_state_table.c.key],
        set_={"value": value, "updated_at": now},
    )
    if conn is not None:
        conn.execute(stmt)
        return
    with _engine.begin() as conn:
        conn.execute(stmt)


def _get_app_state(key: str):
    with _engine.connect() as conn:
        row = conn.execute(
            select(app_state_table.c.value).where(app_state_table.c.key == key)
        ).first()
    return row[0] if row else None


def save_admin_token(token: str, expires_at) -> None:
    """Persist the managed admin bearer (see contact.py) so restarts and
    sibling Cloud Run instances reuse one login. An empty token clears it.

    The token is a live credential: anyone with DB access can read it, same
    as the customer PII already stored here."""
    _set_app_state("admin_token", {"token": token, "expires_at": expires_at})


def load_admin_token() -> Optional[dict]:
    """The persisted admin bearer as {"token": str, "expires_at": epoch-ms},
    or None if never saved."""
    value = _get_app_state("admin_token")
    return value if isinstance(value, dict) else None


class KbConflict(Exception):
    """The document was saved by someone else since the caller loaded it."""

    def __init__(self, current_revision_id: Optional[int]):
        super().__init__(f"kb document changed (latest revision {current_revision_id})")
        self.current_revision_id = current_revision_id


def _latest_revision_id(conn, name: str) -> Optional[int]:
    return conn.execute(
        select(func.max(kb_revisions_table.c.id)).where(kb_revisions_table.c.name == name)
    ).scalar()


def _kb_doc_dict(row, latest_revision_id: Optional[int]) -> dict:
    d = dict(row)
    d["updated_at"] = _as_utc(d["updated_at"])
    d["latest_revision_id"] = latest_revision_id
    return d


def list_kb_documents() -> list[dict]:
    """Every document without its body, plus latest_revision_id, by name."""
    cols = [c for c in kb_documents_table.c if c.name != "body"]
    latest = (
        select(
            kb_revisions_table.c.name,
            func.max(kb_revisions_table.c.id).label("latest_revision_id"),
        )
        .group_by(kb_revisions_table.c.name)
        .subquery()
    )
    stmt = (
        select(*cols, latest.c.latest_revision_id)
        .select_from(
            kb_documents_table.outerjoin(
                latest, latest.c.name == kb_documents_table.c.name
            )
        )
        .order_by(kb_documents_table.c.name)
    )
    with _engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {**dict(r), "updated_at": _as_utc(r["updated_at"])} for r in rows
    ]


def get_kb_document(name: str) -> Optional[dict]:
    with _engine.connect() as conn:
        row = conn.execute(
            select(kb_documents_table).where(kb_documents_table.c.name == name)
        ).mappings().first()
        if row is None:
            return None
        return _kb_doc_dict(row, _latest_revision_id(conn, name))


def load_kb_documents() -> dict[str, dict]:
    """{name: {title, keywords, summary, body, hidden}} for every document,
    hidden ones included — the caller decides what to expose."""
    with _engine.connect() as conn:
        rows = conn.execute(select(kb_documents_table)).mappings().all()
    return {
        r["name"]: {
            "title": r["title"],
            "keywords": r["keywords"],
            "summary": r["summary"],
            "body": r["body"],
            "hidden": bool(r["hidden"]),
        }
        for r in rows
    }


def get_kb_version() -> int:
    value = _get_app_state("kb_version")
    return int(value["v"]) if isinstance(value, dict) and "v" in value else 0


def save_kb_document(
    name: str,
    *,
    title: str,
    keywords: list,
    summary: str,
    body: str,
    hidden: bool,
    saved_by: Optional[str],
    note: Optional[str] = None,
    base_revision_id: Optional[int],
) -> int:
    """Upsert a document, snapshot it as a revision and bump kb_version, all in
    one transaction. Raises KbConflict if base_revision_id is not the document's
    current latest revision id (None when it has no revision yet)."""
    insert = pg_insert if _IS_POSTGRES else sqlite_insert
    now = datetime.now(timezone.utc)
    fields = {
        "title": title,
        "keywords": keywords,
        "summary": summary,
        "body": body,
        "hidden": hidden,
    }
    with _engine.begin() as conn:
        current = _latest_revision_id(conn, name)
        if current != base_revision_id:
            raise KbConflict(current)
        stmt = insert(kb_documents_table).values(
            name=name, **fields, updated_at=now, updated_by=saved_by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[kb_documents_table.c.name],
            set_={**fields, "updated_at": now, "updated_by": saved_by},
        )
        conn.execute(stmt)
        result = conn.execute(
            kb_revisions_table.insert().values(
                name=name, **fields, saved_at=now, saved_by=saved_by, note=note
            )
        )
        revision_id = result.inserted_primary_key[0]
        row = conn.execute(
            select(app_state_table.c.value).where(app_state_table.c.key == "kb_version")
        ).first()
        version = int(row[0]["v"]) if row and isinstance(row[0], dict) else 0
        _set_app_state("kb_version", {"v": version + 1}, conn=conn)
    return revision_id


def list_kb_revisions(name: str, limit: int = 20) -> list[dict]:
    stmt = (
        select(
            kb_revisions_table.c.id,
            kb_revisions_table.c.saved_at,
            kb_revisions_table.c.saved_by,
            kb_revisions_table.c.note,
            kb_revisions_table.c.title,
        )
        .where(kb_revisions_table.c.name == name)
        .order_by(kb_revisions_table.c.id.desc())
        .limit(limit)
    )
    with _engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [{**dict(r), "saved_at": _as_utc(r["saved_at"])} for r in rows]


def get_kb_revision(revision_id: int) -> Optional[dict]:
    with _engine.connect() as conn:
        row = conn.execute(
            select(kb_revisions_table).where(kb_revisions_table.c.id == revision_id)
        ).mappings().first()
    if row is None:
        return None
    return {**dict(row), "saved_at": _as_utc(row["saved_at"])}
