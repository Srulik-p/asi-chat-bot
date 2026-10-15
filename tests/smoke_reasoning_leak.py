"""Smoke test: the model's internal reasoning never reaches the customer.

Run with:
    uv run python tests/smoke_reasoning_leak.py

Uses a throwaway SQLite DB, dummy secrets and a FAKE OpenAI client (no
network). Replays the QA incident where the reply to "המייל שלי הוא ..." was
the model's English planning ("... reason login_email ... Let's call.") and
locks in the guard in _run_rounds:
  - a leaked reply is withheld, never persisted, and gets one corrective round;
  - a second leak fails the turn with the customer-safe error, not the text;
  - English replies to an English customer, and Hebrew replies carrying
    links / emails / brand names, pass untouched.
"""
import io
import json
import os
import pathlib
import tempfile
import uuid
from contextlib import redirect_stderr

DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_leak_{uuid.uuid4().hex}.db"
)

os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["WHATSAPP_API_URL"] = ""
os.environ["LOGIN_URL_BASE"] = "https://qa.sugardaddy.co.il/sign-in"
os.environ["CHAT_DEBOUNCE_SECONDS"] = "0"

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
        # Snapshot: _run_rounds keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        text = self.script[self.i]
        self.i += 1
        if isinstance(text, _Msg):
            return _Stream([], _Final(text))
        return _Stream([_Delta(text)], _Final(_Msg(content=text)))


class _FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def run(phone, message, script):
    server.client = _FakeClient(script)
    err = io.StringIO()
    with redirect_stderr(err):
        events = list(server._run_chat(phone, message))
    sent = "".join(e["text"] for e in events if e["type"] == "delta")
    return events, sent, server.client.chat.completions, err.getvalue()


LEAK = (
    "We need answer likely user provided email, asks context. Need open ticket? "
    "They had offered, user gives email potentially implies wants support. Need "
    "submit ticket reason login_email with email. Could maybe they don't "
    "explicitly agree but providing email as response suggests. Tool. Let's call."
)
server._submit_ticket_for = lambda phone, args: json.dumps(
    {"submitted": True, "status": "open", "on_account": True}, ensure_ascii=False
)


# ---------- 1) the QA incident: leak withheld, corrective round files the ticket ----------

phone = "0502220001"
reply = "תודה! העברתי את הפנייה לצוות, ונציג יחזור אליך בשעות הפעילות 🙂"
events, sent, comps, log = run(phone, "המייל שלי הוא bansnsna32@walla.co.il", [
    LEAK,
    _Msg(tool_calls=[_ToolCall(
        "t1", "submit_support_ticket",
        '{"reason":"login_email","text":"לא מגיע מייל איפוס","email":"bansnsna32@walla.co.il"}')]),
    reply,
])
assert sent == reply, sent
assert events[-1]["type"] == "done", events[-1]
assert len(comps.calls) == 3, len(comps.calls)
note = comps.calls[1]["messages"][-1]
assert note["role"] == "system" and note["content"] == server._LEAK_RETRY_NOTE, note
withheld = comps.calls[1]["messages"][-2]
assert withheld == {"role": "assistant", "content": LEAK}, "model must see what it is replacing"
assert "reply withheld" in log and "reason=internal_identifier" in log, log
stored = [m.get("content") or "" for m in db.load_history(phone)]
assert not any("Let's call" in c for c in stored), stored
assert not any(c == server._LEAK_RETRY_NOTE for c in stored), "retry note must not be persisted"
print("1. leaked reasoning withheld; corrective round calls the tool; customer gets only Hebrew")

# ---------- 2) leaks twice -> customer-safe error, nothing internal sent or stored ----------

phone2 = "0502220002"
events, sent, comps, log = run(phone2, "המייל שלי הוא a@b.co.il", [LEAK, LEAK])
assert sent == "", sent
assert events[-1]["type"] == "error", events[-1]
assert events[-1]["message"] == "אירעה תקלה זמנית. אפשר לנסות שוב בעוד רגע.", events[-1]
assert not any(m["role"] == "assistant" for m in db.load_history(phone2))
assert len(comps.calls) == 2, len(comps.calls)
assert "no retry round left" in log, log
print("2. second leak -> fixed customer-safe error, no assistant row stored")

# ---------- 2b) second leak AFTER the ticket was filed -> deterministic confirmation ----------

phone2b = "0502220012"
events, sent, comps, log = run(phone2b, "המייל שלי הוא a@b.co.il", [
    _Msg(tool_calls=[_ToolCall(
        "t1", "submit_support_ticket",
        '{"reason":"login_email","text":"לא מגיע מייל","email":"a@b.co.il"}')]),
    "Ticket submitted with on_account true. Now tell the user.",
    LEAK,
])
assert sent == server._TICKET_FILED_FALLBACK[True], sent
assert events[-1]["type"] == "done", events[-1]
assert "ticket-filed fallback" in log, log
assert db.load_history(phone2b)[-1]["content"] == sent
print("2b. second leak after a filed ticket -> fixed Hebrew confirmation, not an error")

# ---------- 3) English prose without identifiers is still caught for a Hebrew customer ----------

phone3 = "0502220003"
events, sent, comps, log = run(phone3, "לא מגיע לי מייל", [
    "The user did not get the reset email. I should suggest checking spam first.",
    "בדקת גם בתיקיית הספאם? לפעמים המייל מגיע לשם 🙂",
])
assert sent == "בדקת גם בתיקיית הספאם? לפעמים המייל מגיע לשם 🙂", sent
assert "reason=english_prose" in log, log
print("3. English planning to a Hebrew customer -> withheld (english_prose)")

# ---------- 4) English customer gets English; tool names still blocked ----------

phone4 = "0502220004"
en = "Sure! You can cancel the renewal from the Support menu, under Cancel subscription renewal."
events, sent, comps, log = run(phone4, "Hi, how do I cancel my subscription?", [en])
assert sent == en and len(comps.calls) == 1, (sent, len(comps.calls))

phone5 = "0502220005"
events, sent, comps, log = run(phone5, "I want to talk to a person", [
    "I'll call submit_support_ticket with reason help_desk now.",
    "Of course, I've passed your request to the team and someone will get back to you.",
])
assert sent.startswith("Of course"), sent
print("4. English customer -> English reply passes; tool name in reply still withheld")

# ---------- 5) legit Hebrew replies with Latin bits pass untouched ----------

ok = (
    "בטח 🙂 אפשר לשלם גם ב-Apple Pay או Google Pay.\n"
    "התחבר/י דרך הקישור: https://qa.sugardaddy.co.il/sign-in?phoneNumber=972500000000&token=abc_def\n"
    "ואם צריך, שלח/י צילום ל-support@sugardaddy.co.il. הכינוי sweet_dream נבדק."
)
phone6 = "0502220006"
events, sent, comps, log = run(phone6, "איך משלמים?", [ok])
assert sent == ok and len(comps.calls) == 1, (sent, len(comps.calls))
assert "reply withheld" not in log, log
print("5. Hebrew reply with link / email / brand names / nickname -> sent as-is")

# ---------- 6) customer language: letterless and placeholder-only messages don't flip it ----------

assert server._customer_writes_hebrew([
    {"role": "user", "content": "אני לא מצליח להתחבר"},
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "[מייל]"},
])
assert not server._customer_writes_hebrew([
    {"role": "user", "content": "I can't log in"},
    {"role": "user", "content": "[מייל] 🙏"},
])
assert server._customer_writes_hebrew([
    {"role": "user", "content": [{"type": "text", "text": "זה הצילום"}, {"type": "image_url"}]},
])
assert server._customer_writes_hebrew([
    {"role": "user", "content": "אני לא מצליח להתחבר"},
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "ok"},
]), "a Latin 'ok' must not flip a Hebrew conversation"
assert not server._customer_writes_hebrew([
    {"role": "user", "content": "I can't log in, it shows 'משהו השתבש'"},
]), "quoted Hebrew must not flip an English conversation"
assert not server._customer_writes_hebrew([
    {"role": "user", "content": [{"type": "text", "text": "How do I cancel my plan?"}, {"type": "image_url"}]},
])
print("6. customer language follows the dominant script of the latest real message")

# ---------- 7) leak shapes the first version missed ----------

heb_reply = "בטח 🙂 פתחתי לך פנייה לצוות ונציג יחזור אליך."
for label, leak in [
    ("terse", "Tool. Let's call."),
    ("bracketed", "[Need submit ticket reason login_email now. Let's call.]"),
    ("email-glued", "Need submit_support_ticket(reason=login_email,email=a@b.co.il). Let's call."),
    ("hebrew prefix", "שלחתי לך את הlogin_url עכשיו"),
    ("hebrew plan", "הלקוח נתן מייל, בוא נקרא לכלי."),
    ("after 'ok'", "The user agreed. I should open the ticket now and tell them a rep will call."),
]:
    ph = "05022200" + str(70 + len(label) % 10) + label[:1]
    history = [("אני רוצה לדבר עם נציג", "אין בעיה 🙂 רוצה שאפתח לך פנייה?")]
    db.append_message(ph, "user", content=history[0][0])
    db.append_message(ph, "assistant", content=history[0][1])
    events, sent, comps, log = run(ph, "ok", [leak, heb_reply])
    assert sent == heb_reply, (label, sent)
    assert "reply withheld" in log, (label, log)
print("7. terse / bracketed / email-glued / Hebrew-prefixed / Hebrew-plan / post-'ok' leaks withheld")

# ---------- 8) legit Hebrew replies with a Latin list after a colon pass ----------

for ok_reply in [
    "אפשר לשלם עם: Apple Pay, Google Pay, PayPal",
    'ההודעה "Your session has expired. Please log in again." אומרת שצריך להתחבר מחדש',
    "הנתיב באתר: Settings > Account > Cancel renewal",
]:
    ph = "05022200" + str(80 + len(ok_reply) % 10)
    events, sent, comps, log = run(ph, "איך מבטלים?", [ok_reply])
    assert sent == ok_reply and "reply withheld" not in log, (ok_reply, log)
print("8. Hebrew replies quoting English / listing brands after a colon -> sent as-is")

# ---------- 9) planning text next to a tool call is not persisted ----------

phone9 = "0502220009"
events, sent, comps, log = run(phone9, "המייל שלי הוא a@b.co.il", [
    _Msg(content="Need submit ticket reason login_email. Let's call.",
         tool_calls=[_ToolCall("t1", "submit_support_ticket",
                               '{"reason":"login_email","text":"x","email":"a@b.co.il"}')]),
    heb_reply,
])
assert sent == heb_reply, sent
rows = db.load_history(phone9)
tool_row = next(m for m in rows if m["role"] == "assistant" and m.get("tool_calls"))
assert not tool_row.get("content"), tool_row
assert "preamble dropped" in log, log
print("9. leaked preamble on a tool-call round is dropped from the stored row")

# ---------- 10) the ticket note shows a Hebrew subject, not the reason key ----------

assert "login_email" not in server._REASON_SUBJECTS.values()
assert all(server._leak_reason(v, True) is None for v in server._REASON_SUBJECTS.values())
print("10. ticket-note subjects are customer-safe")

print("\nALL OK")
pathlib.Path(DB_PATH).unlink(missing_ok=True)
