"""Smoke test: inactivity auto-close sweep (warn -> close -> reopen).

Run with:
    uv run python tests/smoke_idle_sweep.py

Uses a throwaway SQLite DB and dummy secrets; never calls OpenAI or the
outbound sender (OUTBOUND_SEND_URL is empty so notifier no-ops). The sweep's
state machine still advances even when delivery is a no-op.
"""
import datetime as dt
import os
import pathlib
import tempfile

DB_PATH = str(pathlib.Path(tempfile.gettempdir()) / "sugarbot_smoke_idle.db")
pathlib.Path(DB_PATH).unlink(missing_ok=True)

os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["AUTH_CALLBACK_SECRET"] = "smoke-secret"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["OUTBOUND_SEND_URL"] = ""  # notifier no-ops
os.environ["INACTIVITY_WARN_HOURS"] = "24"
os.environ["INACTIVITY_CLOSE_HOURS"] = "48"

from sugarbot import db  # noqa: E402

db.init_db()

from sugarbot import server  # noqa: E402

now = dt.datetime.now(dt.timezone.utc)


def _insert(phone: str, role: str, content: str, age_hours: float) -> None:
    with db._engine.begin() as conn:
        conn.execute(
            db.messages_table.insert().values(
                phone_number=phone,
                role=role,
                content=content,
                tool_calls=None,
                tool_call_id=None,
                created_at=now - dt.timedelta(hours=age_hours),
            )
        )


# Idle conversation: last message is an assistant reply 50h old.
idle = "+972500000001"
_insert(idle, "user", "היי", 50)
_insert(idle, "assistant", "במה אפשר לעזור?", 50)

# Fresh conversation: assistant replied 1h ago -> must NOT be touched.
fresh = "+972500000002"
_insert(fresh, "user", "שאלה", 1)
_insert(fresh, "assistant", "הנה התשובה", 1)

# 0) Delivery-gated: with the outbound channel unconfigured (send returns
# False) the sweep must NOT advance state — never close an inquiry the customer
# was never warned about.
res = server._sweep_idle()
assert res == {"scanned": 1, "warned": 0, "closed": 0}, res
assert db.get_conversation_state(idle) is None, "failed delivery must not warn"
print("0. failed delivery -> no state advance:", res)

# From here on simulate a working outbound channel.
from sugarbot import notifier  # noqa: E402

notifier.send_message = lambda phone, message: True

# 1) First sweep -> warn the idle one, leave the fresh one alone.
res = server._sweep_idle()
assert res["warned"] == 1, res
assert res["closed"] == 0, res
state = db.get_conversation_state(idle)
assert state and state["last_warned_at"] is not None and state["closed_at"] is None, state
assert db.get_conversation_state(fresh) is None, "fresh conversation should be untouched"
print("1. first sweep -> warned idle, fresh untouched:", res)

# 2) Backdate the warning (both the delivered warning message and the warned-at
# marker) so the close grace has elapsed, then sweep -> close.
with db._engine.begin() as conn:
    conn.execute(
        db.messages_table.update()
        .where(db.messages_table.c.phone_number == idle)
        .values(created_at=now - dt.timedelta(hours=50))
    )
    conn.execute(
        db.conversation_state_table.update()
        .where(db.conversation_state_table.c.phone_number == idle)
        .values(last_warned_at=now - dt.timedelta(hours=50))
    )
res = server._sweep_idle()
assert res["closed"] == 1, res
state = db.get_conversation_state(idle)
assert state and state["closed_at"] is not None, state
print("2. second sweep -> closed:", res)

# 3) Closed conversation is skipped by further sweeps.
res = server._sweep_idle()
assert res["warned"] == 0 and res["closed"] == 0, res
print("3. closed conversation ignored:", res)

# 4) A new customer message reopens it (state cleared via append_message).
db.append_message(idle, "user", content="עוד שאלה")
assert db.get_conversation_state(idle) is None, "new user message must clear state"
print("4. new user message -> state cleared (reopened)")

# 5) Endpoint guard: wrong/no secret -> 401, correct -> 200 JSON.
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)
assert client.post("/maintenance/sweep-idle").status_code == 401
assert (
    client.post("/maintenance/sweep-idle", headers={"X-Internal-Secret": "wrong"}).status_code
    == 401
)
r = client.post("/maintenance/sweep-idle", headers={"X-Internal-Secret": "smoke-internal"})
assert r.status_code == 200, (r.status_code, r.text)
assert set(r.json()) == {"scanned", "warned", "closed"}, r.json()
print("5. endpoint secret guard ok:", r.json())

print("\nALL CHECKS PASSED")
