"""Smoke test: a burst of inbound messages gets ONE reply.

Run with:
    uv run python tests/smoke_debounce.py

Uses a throwaway SQLite DB, dummy secrets and a FAKE OpenAI client (no
network). Locks in the inbound debounce:
  - three messages sent within the window -> one model turn, one reply, on the
    newest message's request; the earlier requests return `done` with no delta;
  - all three user rows are persisted in arrival order, before the one reply;
  - media on an EARLIER message of the burst still reaches the model;
  - a message arriving while a turn is running is answered by its own turn
    afterwards (it was not part of the burst the running turn answered);
  - a lone message still gets its reply after the quiet window.
"""
import io
import os
import pathlib
import tempfile
import threading
import time
import uuid
from contextlib import redirect_stderr

DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_debounce_{uuid.uuid4().hex}.db"
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

WINDOW = 0.4
server.CHAT_DEBOUNCE_SECONDS = WINDOW


# ---------- fake OpenAI client ----------

class _Delta:
    def __init__(self, text):
        self.type = "content.delta"
        self.delta = text


class _Stream:
    def __init__(self, deltas, final, delay):
        self._deltas, self._final, self._delay = deltas, final, delay

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        time.sleep(self._delay)
        return iter(self._deltas)

    def get_final_completion(self):
        return self._final


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
    """Answers every call with `reply-N`; `delay` simulates model latency."""

    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        text = f"reply-{len(self.calls)}"
        return _Stream([_Delta(text)], _Final(_Msg(content=text)), self.delay)


class _FakeClient:
    def __init__(self, delay=0.0):
        self.chat = type("Chat", (), {"completions": _Completions(delay)})()


def send(phone, message, out, key, media=None):
    out[key] = list(server._run_chat(phone, message, media))


def reply_of(events):
    return "".join(e["text"] for e in events if e["type"] == "delta")


def burst(phone, items, gap):
    """Send (message, media) items `gap` seconds apart, each on its own thread
    like concurrent /chat/message requests; return events keyed by index."""
    out: dict[int, list] = {}
    threads = []
    for i, (message, media) in enumerate(items):
        t = threading.Thread(target=send, args=(phone, message, out, i, media))
        t.start()
        threads.append(t)
        time.sleep(gap)
    for t in threads:
        t.join()
    return out


err = io.StringIO()
with redirect_stderr(err):

    # ---------- 1) three quick messages -> one reply ----------

    server.client = _FakeClient()
    phone = "0502220001"
    image = server.MediaItem(type="image", data_url="data:image/png;base64,AAAA")
    out = burst(phone, [("היי", None), ("שילמתי פעמיים", [image]), ("מה עושים?", None)], gap=0.1)
    comps = server.client.chat.completions

    assert len(comps.calls) == 1, f"burst must cost ONE model turn, got {len(comps.calls)}"
    assert reply_of(out[0]) == "" and reply_of(out[1]) == "", out
    assert out[0][-1]["type"] == "done" and out[1][-1]["type"] == "done", out
    assert reply_of(out[2]) == "reply-1", out[2]
    rows = [(m["role"], m["content"]) for m in db.load_history(phone)]
    assert rows == [
        ("user", "היי"),
        ("user", "שילמתי פעמיים\n[המשתמש צירף תמונה]"),
        ("user", "מה עושים?"),
        ("assistant", "reply-1"),
    ], rows
    print("1. 3 messages within the window -> 1 model turn, 1 reply, rows in order")

    # ---------- 2) media on an earlier burst message reaches the model ----------

    sent = comps.calls[0]["messages"]
    users = [m for m in sent if m["role"] == "user"]
    assert isinstance(users[1]["content"], list), users[1]
    assert users[1]["content"][0] == {"type": "text", "text": "שילמתי פעמיים"}, users[1]
    assert users[1]["content"][1]["type"] == "image_url", users[1]
    assert users[0]["content"] == "היי" and users[2]["content"] == "מה עושים?", users
    print("2. image from the 2nd message is attached to the 2nd user row")

    # ---------- 3) message during a running turn -> its own reply afterwards ----------

    server.client = _FakeClient(delay=1.0)
    phone2 = "0502220002"
    out = {}
    t1 = threading.Thread(target=send, args=(phone2, "שאלה ראשונה", out, 0))
    t1.start()
    time.sleep(WINDOW + 0.3)  # first request is now mid-turn, holding the lock
    t2 = threading.Thread(target=send, args=(phone2, "ועוד משהו", out, 1))
    t2.start()
    t1.join()
    t2.join()
    assert reply_of(out[0]) == "reply-1", out[0]
    assert reply_of(out[1]) == "reply-2", out[1]
    rows = [(m["role"], m["content"]) for m in db.load_history(phone2)]
    assert rows == [
        ("user", "שאלה ראשונה"),
        ("assistant", "reply-1"),
        ("user", "ועוד משהו"),
        ("assistant", "reply-2"),
    ], rows
    print("3. message arriving mid-turn waits for the lock and gets its own reply")

    # ---------- 4) lone message, and the quiet window is honoured ----------

    server.client = _FakeClient()
    t0 = time.monotonic()
    events = list(server._run_chat("0502220003", "מה המחיר?"))
    elapsed = time.monotonic() - t0
    assert reply_of(events) == "reply-1", events
    assert elapsed >= WINDOW, elapsed
    print(f"4. lone message answered after the {WINDOW}s quiet window ({elapsed:.2f}s)")

log = err.getvalue()
assert log.count("[chat] debounced phone=0502220001") == 2, log
assert "[chat] batched phone=0502220001 messages=3" in log, log
print("5. debounced/batched log lines emitted")

print("\nALL DEBOUNCE SMOKE TESTS PASSED")
