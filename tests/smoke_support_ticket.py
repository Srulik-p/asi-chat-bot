"""Smoke test: support-ticket tool (contact.submit_ticket + DB + server wiring).

Run with:
    uv run python tests/smoke_support_ticket.py

Never hits the network — requests.post is monkeypatched to capture the call.
Verifies reason-key resolution, the multipart fields the backend expects, that
the conversation phone is attached as sourcePhoneNumber, ticket id/status
parsing from the response, DB persistence + open-ticket context, success/failure
handling, that the tool is registered in the server's tool list, and the
admin (on-account) path: JSON payload + bearer auth, server-side routing for
account-linked conversations, and fallback to the anonymous unauth endpoint.
"""
import json as _json
import os
import pathlib
import tempfile
import uuid

# Unique per run so concurrent executions (parallel CI shards / agents) don't
# race each other on a shared SQLite file.
DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_tickets_{uuid.uuid4().hex}.db"
)
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ.pop("SUPPORT_TICKET_ENV", None)  # exercise the default (qa)
os.environ.pop("SUGAR_ADMIN_API", None)  # admin path starts unconfigured
os.environ.pop("SUPPORT_ADMIN_URL", None)

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


def _fake_post(url, files=None, json=None, headers=None, timeout=None):
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(body=_next_body)


contact.requests.post = _fake_post
contact.SUPPORT_TICKET_URL = "https://backend.example/user/support/unauth"

# 1) env profiles: default is QA (no prod domain until launch), each env maps
#    every choosable reason to its own optionId, and QA != prod (separate DBs)
assert contact.SUPPORT_TICKET_ENV == "qa", contact.SUPPORT_TICKET_ENV
assert contact.REASON_IDS is contact.REASON_IDS_BY_ENV["qa"]
assert contact.SUPPORT_TICKET_ORIGIN == "https://qa.sugardaddy.co.il", contact.SUPPORT_TICKET_ORIGIN
assert (
    contact._ENV_DEFAULTS["qa"]["url"] != contact._ENV_DEFAULTS["prod"]["url"]
), "qa and prod must post to different backends"
# QA must map every choosable reason; prod may lag on newly added reasons
# (their prod optionIds get snapshotted at launch) — those degrade to help_desk
# via resolve_reason, checked below.
missing_qa = set(contact.REASON_CHOICES) - set(contact.REASON_IDS_BY_ENV["qa"])
assert not missing_qa, f"qa map missing reasons: {missing_qa}"
_ORIGINAL_REASONS = {
    "login_email", "help_desk", "forgot_password", "technical",
    "remove_account", "report", "other",
}
missing_prod = _ORIGINAL_REASONS - set(contact.REASON_IDS_BY_ENV["prod"])
assert not missing_prod, f"prod map missing original reasons: {missing_prod}"
for key in contact.REASON_CHOICES:
    if key not in contact.REASON_IDS_BY_ENV["prod"]:
        continue  # unmapped in prod -> covered by the help_desk fallback check
    assert (
        contact.REASON_IDS_BY_ENV["qa"][key] != contact.REASON_IDS_BY_ENV["prod"][key]
    ), f"qa/prod optionId for {key} must differ (separate databases)"

# reason key -> optionId; raw UUID passes through; junk -> None
assert contact.resolve_reason("technical") == "8ddc112a-e53a-4236-a9be-7f7f1db729ba"
assert contact.resolve_reason("remove_account") == "8cdfdc39-1b26-4ba3-89bb-92407c84e11f"
assert contact.resolve_reason("stop_payment") == "7d83d6f5-3b1b-4bae-8e69-94fc09fb85ca"
assert contact.resolve_reason("id_verification") == "bfae8355-20ba-4141-8350-2eba146d6e3c"
assert contact.resolve_reason("photo_verification") == "cc2c5598-c3a6-4664-b6d0-038e1369658b"
raw = "12345678-1234-1234-1234-123456789abc"
assert contact.resolve_reason(raw) == raw, "raw optionId should pass through"
assert contact.resolve_reason("not-a-reason") is None
assert contact.resolve_reason("") is None
# A choosable reason with no optionId in the active env degrades to help_desk
# (prod until its new-reason ids are snapshotted); junk still resolves to None.
_saved_ids = contact.REASON_IDS
contact.REASON_IDS = contact.REASON_IDS_BY_ENV["prod"]
assert (
    contact.resolve_reason("stop_payment") == contact.REASON_IDS_BY_ENV["prod"]["help_desk"]
), "unmapped choosable reason must degrade to help_desk"
assert contact.resolve_reason("not-a-reason") is None
contact.REASON_IDS = _saved_ids
print("1. env profiles (qa default, qa!=prod); resolve_reason: key->id, raw uuid, junk->None, help_desk fallback")

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
assert files["reason"] == (None, "8ddc112a-e53a-4236-a9be-7f7f1db729ba"), files["reason"]
assert files["text"] == (None, "לא מצליח להעלות תמונה"), files["text"]
assert files["sourceEmail"] == (None, "user@example.com"), files.get("sourceEmail")
assert files["sourcePhoneNumber"] == (None, "+972501234567"), files.get("sourcePhoneNumber")
assert call["headers"]["Origin"] == "https://qa.sugardaddy.co.il", call["headers"]
assert call["headers"]["Referer"] == "https://qa.sugardaddy.co.il/he/contact-us", call["headers"]
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
for key in ("stop_payment", "id_verification", "photo_verification"):
    assert key in params["properties"]["reason"]["enum"], key
print("8. server.TOOLS_ALL registers submit_support_ticket; phone not a model arg; new reasons in enum")

# 9) admin path: env defaults + availability guard (no token/url -> handled failure)
assert (
    contact._ADMIN_ENV_DEFAULTS["qa"]["url"] == "https://backend-admin.sugarinter.media/support"
), contact._ADMIN_ENV_DEFAULTS
assert contact._ADMIN_ENV_DEFAULTS["prod"]["url"] == "", "prod admin host unknown until launch"
assert contact.SUGAR_ADMIN_API == "" and not contact.admin_available()
_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="x", user_id="u-1", source_phone="+972500000000"
)
assert res["ok"] is False and "not configured" in res["detail"], res
assert _calls == [], "must not POST when the admin path is unconfigured"
contact.SUGAR_ADMIN_API = "test-admin-token"
contact.SUPPORT_ADMIN_URL = "https://admin.example/support"
assert contact.admin_available()
print("9. admin env defaults (qa set, prod empty); unconfigured -> ok=False, no POST")

# 10) submit_admin_ticket: exact JSON contract + bearer header; failures handled
_calls.clear()
_next_body = {"id": "ADM-1", "status": "None"}
res = contact.submit_admin_ticket(
    reason="technical",
    text="לא מצליח להעלות תמונה",
    user_id="site-uuid-1",
    source_phone="+972501234567",
)
# The admin backend's initial status is the literal string "None" — it must be
# normalized away (fall back to "created") so it never reaches the customer.
assert res["ok"] is True and res["ticket_id"] == "ADM-1" and res["ticket_status"] == "created", res
call = _calls[-1]
assert call["url"] == "https://admin.example/support", call
assert call["files"] is None, "admin path must POST json, not multipart"
assert call["headers"] == {"Authorization": "Bearer test-admin-token"}, call["headers"]
assert call["json"] == {
    "userId": "site-uuid-1",
    "sources": ["WhatsApp"],
    "reason": "8ddc112a-e53a-4236-a9be-7f7f1db729ba",
    "text": "לא מצליח להעלות תמונה",
    "status": "None",
    "sourcePhoneNumber": "+972501234567",
    "sourceNickname": "",
    "sourceEmail": "",
    "sourceUserId": "",
    "isReplyFromCustomer": False,
}, call["json"]
_calls.clear()
res = contact.submit_admin_ticket(reason="bogus", text="x", user_id="u", source_phone="p")
assert res["ok"] is False and _calls == [], "unknown reason must not POST"
contact.requests.post = _raise_post
res = contact.submit_admin_ticket(reason="help_desk", text="x", user_id="u", source_phone="p")
assert res["ok"] is False, res
contact.requests.post = _fake_post
print("10. submit_admin_ticket -> exact JSON payload + bearer; status 'None' normalized; failures handled")

# 11) server routing: linked account -> admin path (on_account); anonymous -> unauth
phone2 = "+972502223344"
db.upsert_user(phone2, external_id="site-uuid-9", nickname="דנה", is_premium=True, labels=[])
_calls.clear()
_next_body = {"id": "ADM-2", "status": "None"}
result = server._submit_ticket_for(phone2, {"reason": "help_desk", "text": "רוצה נציג"})
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is True, parsed
assert parsed["ticket_id"] == "ADM-2", parsed
assert len(_calls) == 1 and _calls[0]["url"] == "https://admin.example/support", _calls
assert _calls[0]["json"]["userId"] == "site-uuid-9", _calls[0]["json"]
rows = db.recent_support_tickets(phone2)
assert rows and rows[0]["ticket_id"] == "ADM-2", rows
_calls.clear()
_next_body = {"id": "TKT-3003", "status": "open"}
result = server._submit_ticket_for("+972509998877", {"reason": "technical", "text": "תקלה"})
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is False, parsed
assert len(_calls) == 1 and _calls[0]["url"] == contact.SUPPORT_TICKET_URL, _calls
assert _calls[0]["files"] is not None, "anonymous path must stay multipart"
print("11. routing: linked -> admin (on_account=true, persisted); anonymous -> unauth multipart")

# 12) admin failure -> unauth fallback (customer always gets a ticket);
#     missing token -> admin never attempted
def _admin_500_post(url, files=None, json=None, headers=None, timeout=None):
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    if url == "https://admin.example/support":
        return _FakeResp(status_code=500, reason="Server Error")
    return _FakeResp(body={"id": "TKT-4004", "status": "open"})


contact.requests.post = _admin_500_post
_calls.clear()
result = server._submit_ticket_for(phone2, {"reason": "help_desk", "text": "שוב"})
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is False, parsed
assert parsed["ticket_id"] == "TKT-4004", parsed
assert [c["url"] for c in _calls] == [
    "https://admin.example/support",
    contact.SUPPORT_TICKET_URL,
], _calls
contact.requests.post = _fake_post
contact.SUGAR_ADMIN_API = ""
_calls.clear()
_next_body = {"id": "TKT-5005", "status": "open"}
result = server._submit_ticket_for(phone2, {"reason": "help_desk", "text": "עוד"})
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is False, parsed
assert len(_calls) == 1 and _calls[0]["url"] == contact.SUPPORT_TICKET_URL, _calls
print("12. admin failure -> unauth fallback; no token -> straight to unauth")

# 13) both paths fail -> submitted=false (the model must NOT claim a ticket was
#     filed), nothing persisted
def _all_500_post(url, files=None, json=None, headers=None, timeout=None):
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(status_code=500, reason="Server Error")


contact.SUGAR_ADMIN_API = "test-admin-token"  # admin path enabled again
contact.requests.post = _all_500_post
_tickets_before = len(db.recent_support_tickets(phone2))
_calls.clear()
result = server._submit_ticket_for(phone2, {"reason": "help_desk", "text": "כלום לא עובד"})
parsed = _json.loads(result)
assert parsed["submitted"] is False, parsed
assert "ticket_id" not in parsed, "failed submit must not hand the model a ticket_id"
assert parsed["instructions"], parsed
assert [c["url"] for c in _calls] == [
    "https://admin.example/support",
    contact.SUPPORT_TICKET_URL,
], _calls
assert len(db.recent_support_tickets(phone2)) == _tickets_before, (
    "failed submit must not persist a ticket"
)
contact.requests.post = _fake_post
print("13. both paths fail -> submitted=false, no ticket_id, nothing persisted")

pathlib.Path(DB_PATH).unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
