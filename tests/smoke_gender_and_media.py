"""Smoke test: gender round-trip, login-link safety-net, and image/PDF media.

Run with:
    uv run python tests/smoke_gender_and_media.py

Uses a throwaway SQLite DB and dummy secrets, and a FAKE OpenAI client (no
network, no real completions). Covers the QA-remark fixes:
  - Remark 7:  gender flows callback -> DB -> get_account_status
  - Remark 12: login_url is appended to the reply even if the model omits it
  - Remark 5:  attached image/PDF becomes multimodal content for the model,
               history keeps a light text placeholder; scrub handles lists
"""
import json
import os
import pathlib
import tempfile

DB_PATH = str(pathlib.Path(tempfile.gettempdir()) / "sugarbot_smoke_media.db")
pathlib.Path(DB_PATH).unlink(missing_ok=True)

os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["AUTH_CALLBACK_SECRET"] = "smoke-secret"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["OUTBOUND_SEND_URL"] = ""
os.environ["LOGIN_URL_BASE"] = "https://qa.sugardaddy.co.il/sign-in"

from sugarbot import db  # noqa: E402

db.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from sugarbot import server  # noqa: E402
from sugarbot.assistant import scrub_messages  # noqa: E402

client = TestClient(server.app)


# ---------- 1) gender round-trip (Remark 7) ----------

phone = "+972500000007"
payload = {
    "phoneNumber": phone,
    "user": {
        "id": "u_7",
        "nickname": "דנה",
        "isPremium": False,
        "gender": "female",
        "labels": [],
    },
    "accessToken": "tok_7",
}
r = client.post("/auth/callback", json=payload, headers={"X-Webhook-Secret": "smoke-secret"})
assert r.status_code == 204, (r.status_code, r.text)
st = json.loads(server._account_status_for(phone))
assert st["logged_in"] is True and st["gender"] == "female", st
print("1. gender round-trip -> get_account_status.gender =", st["gender"])

# Backward-compat: a callback WITHOUT gender still works, gender is None.
phone_ng = "+972500000008"
payload_ng = {
    "phoneNumber": phone_ng,
    "user": {"id": "u_8", "nickname": "יוסי", "isPremium": True, "labels": []},
    "accessToken": "tok_8",
}
r = client.post("/auth/callback", json=payload_ng, headers={"X-Webhook-Secret": "smoke-secret"})
assert r.status_code == 204, (r.status_code, r.text)
st_ng = json.loads(server._account_status_for(phone_ng))
assert st_ng["logged_in"] is True and st_ng["gender"] is None, st_ng
print("2. no-gender callback -> gender = None (backward compatible)")


# ---------- 3) scrub_messages handles multimodal (list) content (Remark 5) ----------

msgs = [
    {"role": "user", "content": [
        {"type": "text", "text": "המייל שלי a@b.com"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]},
]
scrubbed = scrub_messages(msgs)
text_part = scrubbed[0]["content"][0]["text"]
img_part = scrubbed[0]["content"][1]
assert "[מייל]" in text_part and "a@b.com" not in text_part, scrubbed
assert img_part["image_url"]["url"] == "data:image/png;base64,AAAA", scrubbed
print("3. scrub_messages redacts text part, passes image part through")


# ---------- fake OpenAI client so _run_chat runs with no network ----------

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
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15
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


def run(phone, message, script, media=None):
    server.client = _FakeClient(script)
    events = list(server._run_chat(phone, message, media))
    return events, server.client.chat.completions


# ---------- 4) login-link safety-net (Remark 12) ----------

# Model calls get_account_status (unknown phone -> login_url), then replies
# WITHOUT the link. The backend must append the literal URL. The tool round
# also streams preamble text — which must NOT reach the customer.
login_phone = "+972500000012"
script = [
    {"message": _Msg(content="רק בודק את הסטטוס...", tool_calls=[_ToolCall("t1", "get_account_status", "{}")]),
     "deltas": [_Delta("רק בודק את הסטטוס...")]},
    {"message": _Msg(content="כדי לראות סטטוס צריך להתחבר.", tool_calls=None),
     "deltas": [_Delta("כדי לראות סטטוס צריך להתחבר.")]},
]
events, _ = run(login_phone, "מה הסטטוס שלי?", script)
expected_url = f"https://qa.sugardaddy.co.il/sign-in?phoneNumber=%2B972500000012"
deltas = "".join(e["text"] for e in events if e.get("type") == "delta")
assert expected_url in deltas, ("link not streamed", deltas)
assert "רק בודק" not in deltas, ("tool-round preamble leaked to the customer", deltas)
final_reply = db.load_history(login_phone)[-1]
assert final_reply["role"] == "assistant" and expected_url in (final_reply["content"] or ""), final_reply
print("4. login-link safety-net -> URL appended; tool-round preamble not streamed")

# Control: when the model DOES include the URL, it is not duplicated.
login_phone2 = "+972500000013"
script2 = [
    {"message": _Msg(content=None, tool_calls=[_ToolCall("t1", "get_account_status", "{}")])},
    {"message": _Msg(content=f"התחבר/י כאן: https://qa.sugardaddy.co.il/sign-in?phoneNumber=%2B972500000013 🙂"),
     "deltas": [_Delta("...")]},
]
run(login_phone2, "מה הסטטוס שלי?", script2)
stored = db.load_history(login_phone2)[-1]["content"]
assert stored.count("sign-in?phoneNumber=%2B972500000013") == 1, ("URL duplicated", stored)
print("5. login-link not duplicated when model already sent it")

# Diagnose-first control: the model deliberately asked a clarifying question
# with no login wording (the "לא מצליח להתחבר" flow) -> the safety-net must
# NOT force-append a login link into the question.
login_phone3 = "+972500000014"
script3b = [
    {"message": _Msg(content=None, tool_calls=[_ToolCall("t1", "get_account_status", "{}")])},
    {"message": _Msg(content="מה בדיוק קורה כשאת/ה מנסה להיכנס? מה רשום על המסך?"),
     "deltas": [_Delta("מה בדיוק קורה כשאת/ה מנסה להיכנס? מה רשום על המסך?")]},
]
run(login_phone3, "לא מצליח להתחבר", script3b)
stored3 = db.load_history(login_phone3)[-1]["content"]
assert "sign-in" not in stored3, ("link force-appended into a diagnostic question", stored3)
print("5b. diagnostic reply without login wording -> link withheld")


# ---------- 6) media -> multimodal content + history placeholder (Remark 5) ----------

media_phone = "+972500000005"
img = {"type": "image", "data_url": "data:image/png;base64,ZZZZ", "filename": "receipt.png"}
script3 = [
    {"message": _Msg(content="רואה את הקבלה, מעביר לצוות.", tool_calls=None),
     "deltas": [_Delta("רואה את הקבלה, מעביר לצוות.")]},
]
_, comp = run(media_phone, "צירפתי קבלה", script3, media=[server.MediaItem(**img)])
sent_messages = comp.calls[0]["messages"]
last_user = sent_messages[-1]
assert isinstance(last_user["content"], list), last_user
assert any(p.get("type") == "image_url" for p in last_user["content"]), last_user
# History keeps a light text placeholder, not the bytes.
stored_user = db.load_history(media_phone)[0]
assert "ZZZZ" not in (stored_user["content"] or ""), stored_user
assert "[המשתמש צירף תמונה]" in stored_user["content"], stored_user
print("6. media -> multimodal to model; history keeps text placeholder")

print("\nALL CHECKS PASSED")
