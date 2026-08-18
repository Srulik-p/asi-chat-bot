"""Smoke test: per-turn efficiency guarantees in the chat loop.

Run with:
    uv run python tests/smoke_turn_efficiency.py

Uses a throwaway SQLite DB, dummy secrets and a FAKE OpenAI client (no
network). Locks in the latency work:
  - a filed ticket escalates the conversation by itself (no extra
    escalate_to_human round needed), while a login-gated / failed submit
    does not;
  - the final reply is streamed downstream as ONE delta event, not one per
    model token;
  - db.load_history(limit=N) returns only the newest N rows, in order;
  - every model round logs a [chat] round line with timing + usage.
"""
import io
import json
import os
import pathlib
import tempfile
import uuid
from contextlib import redirect_stderr

DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_eff_{uuid.uuid4().hex}.db"
)

os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["WHATSAPP_API_URL"] = ""
os.environ["LOGIN_URL_BASE"] = "https://qa.sugardaddy.co.il/sign-in"

from sugarbot import db  # noqa: E402

db.init_db()

from sugarbot import server  # noqa: E402


# ---------- fake OpenAI client ----------

class _Delta:
    def __init__(self, text):
        self.type = "content.delta"
        self.delta = text


class _Stream:
    def __init__(self, deltas, final):
        self._deltas, self._final = deltas, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._deltas)

    def get_final_completion(self):
        return self._final


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id, self.type, self.function = id, "function", _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Usage:
    prompt_tokens = 100
    completion_tokens = 5
    total_tokens = 105
    prompt_tokens_details = None


class _Final:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = _Usage()


class _Completions:
    def __init__(self, script):
        self.script, self.i, self.calls = script, 0, []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script[self.i]
        self.i += 1
        return _Stream(step.get("deltas", []), _Final(step["message"]))


class _FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def run(phone, message, script):
    server.client = _FakeClient(script)
    err = io.StringIO()
    with redirect_stderr(err):
        events = list(server._run_chat(phone, message))
    return events, server.client.chat.completions, err.getvalue()


# ---------- 1) filed ticket escalates on its own ----------

server._submit_ticket_for = lambda phone, args: json.dumps(
    {"submitted": True, "status": "open", "on_account": False}, ensure_ascii=False
)
phone = "0501110001"
events, comps, log = run(phone, "אני רוצה נציג", [
    {"message": _Msg(content=None, tool_calls=[
        _ToolCall("t1", "submit_support_ticket", '{"reason":"help_desk","text":"נציג"}')])},
    {"deltas": [_Delta("מעבירה "), _Delta("לצוות "), _Delta("🙂")],
     "message": _Msg(content="מעבירה לצוות 🙂", tool_calls=None)},
])
assert len(comps.calls) == 2, len(comps.calls)
state = db.get_conversation_state(phone)
assert state and state.get("escalated_at"), f"filed ticket must escalate by itself: {state}"
tool_row = [m for m in db.load_history(phone) if m["role"] == "tool"][0]
assert json.loads(tool_row["content"]).get("escalated") is True, tool_row
print("1. submit_support_ticket success -> conversation escalated without an escalate_to_human round")

# ---------- 2) one delta event for the whole reply ----------

deltas = [e for e in events if e["type"] == "delta"]
assert len(deltas) == 1, deltas
assert deltas[0]["text"] == "מעבירה לצוות 🙂", deltas[0]
assert events[-1]["type"] == "done", events[-1]
print("2. final reply streamed as ONE delta event")

# ---------- 3) login-gated / failed submit does NOT escalate ----------

server._submit_ticket_for = lambda phone, args: json.dumps(
    {"submitted": False, "login_required": True,
     "login_url": "https://qa.sugardaddy.co.il/sign-in?phoneNumber=x"}, ensure_ascii=False
)
phone2 = "0501110002"
run(phone2, "אני רוצה נציג", [
    {"message": _Msg(content=None, tool_calls=[
        _ToolCall("t1", "submit_support_ticket", '{"reason":"help_desk","text":"נציג"}')])},
    {"message": _Msg(content="צריך להתחבר קודם, הנה הקישור", tool_calls=None)},
])
state2 = db.get_conversation_state(phone2)
assert not (state2 and state2.get("escalated_at")), f"login gate must not escalate: {state2}"

server._submit_ticket_for = lambda phone, args: json.dumps(
    {"submitted": False, "instructions": "נכשל"}, ensure_ascii=False
)
phone3 = "0501110003"
run(phone3, "אני רוצה נציג", [
    {"message": _Msg(content=None, tool_calls=[
        _ToolCall("t1", "submit_support_ticket", '{"reason":"help_desk","text":"נציג"}')])},
    {"message": _Msg(content="לא הצלחתי לפתוח, ננסה שוב", tool_calls=None)},
])
state3 = db.get_conversation_state(phone3)
assert not (state3 and state3.get("escalated_at")), f"failed submit must not escalate: {state3}"
print("3. login_required / failed submit -> NOT escalated")

# ---------- 4) explicit escalate_to_human still works (no-ticket hand-off) ----------

phone4 = "0501110004"
run(phone4, "תעביר לנציג", [
    {"message": _Msg(content=None, tool_calls=[_ToolCall("t1", "escalate_to_human", "{}")])},
    {"message": _Msg(content="מעבירה", tool_calls=None)},
])
state4 = db.get_conversation_state(phone4)
assert state4 and state4.get("escalated_at"), state4
print("4. explicit escalate_to_human still escalates")

# ---------- 5) load_history(limit=) returns newest N rows in order ----------

phone5 = "0501110005"
for i in range(7):
    db.append_message(phone5, "user" if i % 2 == 0 else "assistant", content=f"m{i}")
full = db.load_history(phone5)
assert [m["content"] for m in full] == [f"m{i}" for i in range(7)], full
tail = db.load_history(phone5, limit=3)
assert [m["content"] for m in tail] == ["m4", "m5", "m6"], tail
assert db.load_history(phone5, limit=50) == full
print("5. load_history(limit=3) -> newest 3 rows, chronological")

# ---------- 6) per-round log line with timing + usage ----------

lines = [l for l in log.splitlines() if l.startswith("[chat] round")]
assert len(lines) == 2, log
assert "round=1/" in lines[0] and "tools=submit_support_ticket" in lines[0], lines[0]
assert "prompt=100" in lines[0] and "completion=5" in lines[0], lines[0]
assert "secs=" in lines[0] and "ttft=" in lines[0], lines[0]
assert "tools=-" in lines[1], lines[1]
turn_lines = [l for l in log.splitlines() if l.startswith("[chat] turn")]
assert len(turn_lines) == 1 and "rounds=2" in turn_lines[0], log
print("6. per-round + per-turn timing/usage log lines emitted")

print("\nALL CHECKS PASSED")
