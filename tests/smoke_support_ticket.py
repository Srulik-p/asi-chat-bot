"""Smoke test: support-ticket tool (contact.submit_ticket + DB + server wiring).

Run with:
    uv run python tests/smoke_support_ticket.py

Never hits the network — requests.post is monkeypatched to capture the call.
Verifies reason-key resolution, the multipart fields the backend expects, that
the conversation phone is attached as sourcePhoneNumber, ticket id/status
parsing from the response, DB persistence + open-ticket context, success/failure
handling, and that the tool is registered in the server's tool list.
"""
import os
import pathlib
import tempfile

DB_PATH = str(pathlib.Path(tempfile.gettempdir()) / "sugarbot_smoke_tickets.db")
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"

from sugarbot import contact  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=201, reason="Created", body=None):
        self.status_code = status_code
        self.reason = reason
        self.ok = 200 <= status_code < 300
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


# --- capture whatever submit_ticket posts, without any network ------------
_calls: list[dict] = []
_next_body = {"id": "TKT-1001", "status": "open"}


def _fake_post(url, files=None, headers=None, timeout=None):
    _calls.append({"url": url, "files": files, "headers": headers, "timeout": timeout})
    return _FakeResp(body=_next_body)


contact.requests.post = _fake_post
contact.SUPPORT_TICKET_URL = "https://backend.example/user/support/unauth"

# 1) reason key -> optionId; raw UUID passes through; junk -> None
assert contact.resolve_reason("technical") == "1c366a67-dc37-4df8-8ae8-3ab07595a5c8"
assert contact.resolve_reason("remove_account") == "bff68c44-0d71-47ab-b3c8-538c6b71aafc"
raw = "12345678-1234-1234-1234-123456789abc"
assert contact.resolve_reason(raw) == raw, "raw optionId should pass through"
assert contact.resolve_reason("not-a-reason") is None
assert contact.resolve_reason("") is None
print("1. resolve_reason: key->id, raw uuid passthrough, junk->None")

# 2) successful submit builds the right multipart fields + headers, and parses
#    ticket_id/status from the response body
_calls.clear()
res = contact.submit_ticket(
    reason="technical",
    text="לא מצליח להעלות תמונה",
    source_email="user@example.com",
    source_phone="+972501234567",
)
assert res["ok"] is True and res["http_status"] == 201, res
assert res["ticket_id"] == "TKT-1001", res
assert res["ticket_status"] == "open", res
assert res["raw"] == {"id": "TKT-1001", "status": "open"}, res
call = _calls[-1]
files = call["files"]
assert files["reason"] == (None, "1c366a67-dc37-4df8-8ae8-3ab07595a5c8"), files["reason"]
assert files["text"] == (None, "לא מצליח להעלות תמונה"), files["text"]
assert files["sourceEmail"] == (None, "user@example.com"), files.get("sourceEmail")
assert files["sourcePhoneNumber"] == (None, "+972501234567"), files.get("sourcePhoneNumber")
assert call["headers"]["Origin"] == "https://sugardaddy.co.il", call["headers"]
assert call["headers"]["Referer"] == "https://sugardaddy.co.il/he/contact-us", call["headers"]
print("2. submit_ticket -> multipart fields, headers, ticket_id/status parsed")

# 3) nested id/status containers + status fallback to "created"
_next_body = {"data": {"ticketId": "42"}}
res = contact.submit_ticket(reason="other", text="שאלה", source_phone="+972500000000")
assert res["ticket_id"] == "42" and res["ticket_status"] == "created", res
files = _calls[-1]["files"]
assert "sourceEmail" not in files and set(files) == {"reason", "text", "sourcePhoneNumber"}, files
print("3. nested id container parsed; status falls back to 'created'; optional email omitted")

# 4) unknown reason is a handled failure (no network call, no crash)
_calls.clear()
res = contact.submit_ticket(reason="bogus", text="x")
assert res["ok"] is False and res["http_status"] is None and res["ticket_id"] is None, res
assert _calls == [], "must not POST on an unknown reason"
print("4. unknown reason -> ok=False, no POST")

# 5) network error is swallowed -> ok=False
def _raise_post(*a, **k):
    raise contact.requests.ConnectionError("boom")


contact.requests.post = _raise_post
res = contact.submit_ticket(reason="help_desk", text="x", source_phone="+972500000000")
assert res["ok"] is False and res["ticket_id"] is None, res
print("5. network error -> ok=False, no raise")

contact.requests.post = _fake_post  # restore

# 6) DB persistence + open-ticket context surfaces the ticket, then expires
from sugarbot import db  # noqa: E402

db.init_db()
phone = "+972501112233"
assert db.recent_support_tickets(phone) == [], "no tickets yet"
db.add_support_ticket(phone, ticket_id="TKT-1001", status="open", reason="technical", raw={"id": "TKT-1001"})
rows = db.recent_support_tickets(phone)
assert len(rows) == 1 and rows[0]["ticket_id"] == "TKT-1001" and rows[0]["status"] == "open", rows

from sugarbot import server  # noqa: E402

note = server._open_ticket_note(phone)
assert note and "TKT-1001" in note and "open" in note, note
# a phone with no tickets gets no note
assert server._open_ticket_note("+972500000999") is None
# tickets older than the freshness window are not surfaced
import datetime as _dt  # noqa: E402

old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=server.TICKET_CONTEXT_DAYS + 1)
with db._engine.begin() as conn:
    conn.execute(
        db.support_tickets_table.update()
        .where(db.support_tickets_table.c.phone_number == phone)
        .values(created_at=old)
    )
assert server._open_ticket_note(phone) is None, "stale ticket must not surface"
print("6. DB persistence + open-ticket context (fresh surfaces, stale expires)")

# 7) /user/delete also erases tickets
db.add_support_ticket(phone, ticket_id="TKT-2002", status="open", reason="report")
counts = db.delete_user_data(phone)
assert counts["support_tickets"] == 2, counts  # the stale one + TKT-2002
assert db.recent_support_tickets(phone) == [], "tickets gone after delete"
print("7. delete_user_data erases support tickets:", counts)

# 8) server registers the tool with the expected enum, phone stays out of args
names = [t["function"]["name"] for t in server.TOOLS_ALL]
assert "submit_support_ticket" in names, names
tool = next(t for t in server.TOOLS_ALL if t["function"]["name"] == "submit_support_ticket")
params = tool["function"]["parameters"]
assert params["properties"]["reason"]["enum"] == contact.REASON_CHOICES, params
assert params["required"] == ["reason", "text"], params
assert "phone" not in params["properties"] and "sourcePhoneNumber" not in params["properties"]
print("8. server.TOOLS_ALL registers submit_support_ticket; phone not a model arg")

pathlib.Path(DB_PATH).unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
