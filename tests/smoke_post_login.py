"""Smoke test: the post-login continuation pushed from /auth/callback.

Run with:
    uv run python tests/smoke_post_login.py

Uses a throwaway SQLite DB, dummy secrets, a FAKE OpenAI client and a fake
outbound sender (no network at all). Locks in the behaviour that replaced the
fixed "I see you connected" greeting:
  - a conversation with a pending request is replayed through the model, with
    the just-refreshed account status appended as the last system note, and the
    generated answer is what the customer receives;
  - the note forbids re-sending a login link and forbids stopping at "I see you
    connected";
  - a conversation that has been cold for over a day gets the extra
    "don't act on a stale request" guard, a fresh one does not;
  - no history / model failure / send failure all degrade safely (fixed
    greeting; nothing recorded that the customer never got);
  - a ticket the login was gating is filed by this push and escalates the
    conversation, and the pushed reply is recorded exactly once.
"""
import io
import json
import os
import pathlib
import tempfile
import uuid
from contextlib import redirect_stderr

DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_login_{uuid.uuid4().hex}.db"
)

os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["AUTH_CALLBACK_SECRET"] = "smoke-secret"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["WHATSAPP_API_URL"] = ""  # notifier must no-op, not call anything real
os.environ["LOGIN_URL_BASE"] = "https://qa.sugardaddy.co.il/sign-in"

from sugarbot import db  # noqa: E402

db.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from sugarbot import server  # noqa: E402

client = TestClient(server.app)
SECRET = {"X-Webhook-Secret": "smoke-secret"}


# ---------- fake OpenAI client (same shape as smoke_turn_efficiency) ----------

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
        if step.get("raise"):
            raise RuntimeError("model exploded")
        return _Stream(step.get("deltas", []), _Final(step["message"]))


class _FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def answer(text):
    """One-round script: the model replies with `text` and no tool calls."""
    return [{"deltas": [_Delta(text)], "message": _Msg(content=text, tool_calls=None)}]


_sent: list[tuple[str, str]] = []
_send_ok = True


def _fake_send(phone, message):
    _sent.append((phone, message))
    return _send_ok


server.notifier.send_message = _fake_send


def callback(phone, script, nickname="דני", premium=True, gender="male"):
    """Fire /auth/callback (TestClient runs the background push inline)."""
    server.client = _FakeClient(script)
    _sent.clear()
    err = io.StringIO()
    with redirect_stderr(err):
        r = client.post(
            "/auth/callback",
            json={
                "phoneNumber": phone,
                "user": {
                    "id": "u_" + phone[-4:],
                    "nickname": nickname,
                    "isPremium": premium,
                    "gender": gender,
                    "labels": [],
                },
                "accessToken": "tok",
            },
            headers=SECRET,
        )
    assert r.status_code == 204, (r.status_code, r.text)
    return server.client.chat.completions, err.getvalue()


# ---------- 1) no conversation yet -> fixed greeting, no model call ----------

phone1 = "0521110001"
comps, log = callback(phone1, answer("לא אמור להיקרא"))
assert comps.calls == [], "an empty conversation must not cost a model round"
assert len(_sent) == 1, _sent
assert "אנחנו רואים שהתחברת בהצלחה" in _sent[0][1], _sent[0]
assert [m["content"] for m in db.load_history(phone1)] == [_sent[0][1]]
assert "generated=False" in log, log
print("1. no history -> fixed greeting, zero model calls, recorded")

# ---------- 2) pending request -> generated answer, note shape ----------

phone2 = "0521110002"
db.append_message(phone2, "user", content="יש לי מנוי פעיל?")
db.append_message(
    phone2,
    "assistant",
    content="כדי שאוכל לראות את הסטטוס שלך, התחבר/י כאן: "
    "https://qa.sugardaddy.co.il/sign-in?phoneNumber=0521110002",
)

reply = "היי דני, אני רואה שהתחברת 🙂 יש לך מנוי פרימיום פעיל, אז הכל פתוח לך."
comps, log = callback(phone2, answer(reply))

assert len(comps.calls) == 1, comps.calls
sent_messages = comps.calls[0]["messages"]
assert sent_messages[0]["role"] == "system", sent_messages[0]["role"]
note = sent_messages[-1]
assert note["role"] == "system", note["role"]
# The note lands AFTER the replayed conversation, not beside the system prompt.
roles = [m["role"] for m in sent_messages]
assert roles[1:3] == ["user", "assistant"], roles
# ...and it carries the freshly refreshed account status, verbatim.
status = json.loads(server._account_status_for(phone2))
assert status["logged_in"] is True and status["is_premium"] is True, status
assert json.dumps(status, ensure_ascii=False) in note["content"], note["content"]
assert "get_account_status" in note["content"], note["content"]
# ...and the two rules that make this message different from the old greeting.
assert "אל תסתפק/י ב'אני רואה שהתחברת'" in note["content"], note["content"]
assert "אל תשלח/י שום קישור התחברות" in note["content"], note["content"]
assert "אל תפעל/י עליה" not in note["content"], "fresh conversation needs no stale guard"
# The customer gets the generated answer, and it is recorded once.
assert _sent == [(phone2, reply)], _sent
assert [m["content"] for m in db.load_history(phone2)][-1] == reply
assert [m["content"] for m in db.load_history(phone2)].count(reply) == 1
assert "generated=True" in log, log
print("2. pending request -> conversation replayed + status note appended, answer pushed")

# ---------- 3) cold conversation -> stale-request guard in the note ----------

import datetime as _dt  # noqa: E402

phone3 = "0521110003"
db.append_message(phone3, "user", content="חויבתי פעמיים")
db.append_message(phone3, "assistant", content="התחבר/י רגע כאן ואבדוק")
cold = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
    hours=server.POST_LOGIN_STALE_HOURS * 3
)
with db._engine.begin() as conn:
    conn.execute(
        db.messages_table.update()
        .where(db.messages_table.c.phone_number == phone3)
        .values(created_at=cold)
    )
comps, _ = callback(phone3, answer("היי, אני רואה שהתחברת - עדיין צריך שנבדוק את החיוב?"))
note3 = comps.calls[0]["messages"][-1]["content"]
assert "אל תפעל/י עליה" in note3, note3
assert "כ-3 ימים" in note3, note3
print("3. conversation cold >24h -> note carries the stale-request guard")

# ---------- 4) model failure -> fixed greeting, still delivered ----------

phone4 = "0521110004"
db.append_message(phone4, "user", content="יש לי מנוי?")
comps, log = callback(phone4, [{"raise": True, "message": None}])
assert len(_sent) == 1 and "אנחנו רואים שהתחברת בהצלחה" in _sent[0][1], _sent
assert "generated=False" in log, log
assert db.load_history(phone4)[-1]["content"] == _sent[0][1]
print("4. model failure -> fixed greeting still pushed and recorded")

# ---------- 5) send failure -> nothing recorded ----------

phone5 = "0521110005"
db.append_message(phone5, "user", content="יש לי מנוי?")
_send_ok = False
callback(phone5, answer("תשובה שלא הגיעה"))
_send_ok = True
assert len(_sent) == 1, _sent
assert [m["content"] for m in db.load_history(phone5)] == ["יש לי מנוי?"], db.load_history(phone5)
print("5. failed outbound send -> reply NOT recorded as delivered")

# ---------- 6) the ticket the login gated is filed by this push ----------

phone6 = "0521110006"
db.append_message(phone6, "user", content="אני רוצה לדווח על משתמשת בשם sweetdream")
db.append_message(phone6, "assistant", content="התחבר/י כאן ואפתח את הדיווח")
server._submit_ticket_for = lambda phone, args: json.dumps(
    {"submitted": True, "status": "open", "on_account": True}, ensure_ascii=False
)
filed = "תודה שהתחברת - פתחתי את הדיווח על sweetdream לצוות ונציג יחזור אליך 🙂"
comps, _ = callback(
    phone6,
    [
        {
            "message": _Msg(
                content=None,
                tool_calls=[
                    _ToolCall("t1", "submit_support_ticket", '{"reason":"report","text":"דיווח"}')
                ],
            )
        },
        {"deltas": [_Delta(filed)], "message": _Msg(content=filed, tool_calls=None)},
    ],
)
assert len(comps.calls) == 2, comps.calls
assert _sent == [(phone6, filed)], _sent
hist6 = db.load_history(phone6)
assert [m["role"] for m in hist6] == ["user", "assistant", "assistant", "tool", "assistant"], hist6
assert hist6[-1]["content"] == filed, hist6[-1]
state6 = db.get_conversation_state(phone6)
assert state6 and state6.get("escalated_at"), f"a ticket filed here must escalate: {state6}"
print("6. login-gated ticket filed by the push; conversation escalated; reply recorded once")

print("\nALL CHECKS PASSED")
