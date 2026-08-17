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
import io as _io
import json as _json
import os
import pathlib
import tempfile
import time as _time
import uuid
from contextlib import redirect_stderr as _redirect_stderr

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
os.environ.pop("ADMIN_EMAIL", None)  # managed-login creds start unconfigured
os.environ.pop("ADMIN_PASSWORD", None)
os.environ.pop("ADMIN_LOGIN_URL", None)
os.environ.pop("ADMIN_API_KEY", None)  # x-api-key auth starts unconfigured

from sugarbot import contact  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=201, reason="Created", body=None, text="", headers=None):
        self.status_code = status_code
        self.reason = reason
        self.ok = 200 <= status_code < 300
        self._body = body
        self.text = text
        self.headers = headers or {}

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
assert note and "open" in note and "technical" in note, note
assert "TKT-1001" not in note, "ticket ids must stay out of the model-facing note"
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
assert "no_account" in params["properties"], "guest filing needs the explicit no_account flag"
for key in ("stop_payment", "id_verification", "photo_verification"):
    assert key in params["properties"]["reason"]["enum"], key
print("8. server.TOOLS_ALL registers submit_support_ticket; phone not a model arg; new reasons in enum")

# 9) admin path: env defaults + availability guard (no token/url -> handled failure)
assert (
    contact._ADMIN_ENV_DEFAULTS["qa"]["url"] == "https://backend-admin.sugarinter.media/support"
), contact._ADMIN_ENV_DEFAULTS
assert (
    contact._ADMIN_ENV_DEFAULTS["prod"]["url"] == "https://adm-backend.sugarinter.media/support"
), contact._ADMIN_ENV_DEFAULTS
_saved_default_url = contact.SUPPORT_ADMIN_URL
contact.SUPPORT_ADMIN_URL = contact._ADMIN_ENV_DEFAULTS["prod"]["url"]
assert (
    contact._admin_login_url() == "https://adm-backend.sugarinter.media/auth/login"
), contact._admin_login_url()
contact.SUPPORT_ADMIN_URL = _saved_default_url
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
assert contact.SUPPORT_ADMIN_TIMEOUT == 3.0, contact.SUPPORT_ADMIN_TIMEOUT
masked = contact._safe_err(Exception("boom test-admin-token boom"))
assert "test-admin-token" not in masked and "***" in masked, masked
print("9. admin env defaults (qa set, prod empty); unconfigured -> no POST; token masked in errors")

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
    "status": "ForCustomerService",
    "sourcePhoneNumber": "+972501234567",
    "sourceNickname": "",
    "sourceEmail": "",
    "sourceUserId": "",
    "isReplyFromCustomer": False,
}, call["json"]
# a meaningful echoed status passes through to DB/context untouched
_next_body = {"id": "ADM-9", "status": "ForCustomerService"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="x", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_status"] == "ForCustomerService", res
_calls.clear()
res = contact.submit_admin_ticket(reason="bogus", text="x", user_id="u", source_phone="p")
assert res["ok"] is False and _calls == [], "unknown reason must not POST"
contact.requests.post = _raise_post
res = contact.submit_admin_ticket(reason="help_desk", text="x", user_id="u", source_phone="p")
assert res["ok"] is False, res
contact.requests.post = _fake_post
print("10. submit_admin_ticket -> exact JSON payload + bearer; status 'None' normalized; failures handled")

# 10b) managed admin login: ADMIN_EMAIL/ADMIN_PASSWORD -> lazy POST /auth/login,
#      token cached until expiresAt, one re-login retry on 401, and safe
#      fallbacks when the login endpoint itself is down
_now_ms = int(_time.time() * 1000)

# login URL derives from SUPPORT_ADMIN_URL; an explicit override wins
assert contact._admin_login_url() == "https://admin.example/auth/login", contact._admin_login_url()
contact.ADMIN_LOGIN_URL = "https://other.example/auth/login"
assert contact._admin_login_url() == "https://other.example/auth/login"
contact.ADMIN_LOGIN_URL = ""

# creds alone (no static token) must enable the admin path
contact.SUGAR_ADMIN_API = ""
contact.ADMIN_EMAIL = "admin@example.com"
contact.ADMIN_PASSWORD = "pw-secret"
assert contact.admin_available(), "login creds alone must enable the admin path"

# a base that doesn't end in /support cannot derive a login URL: managed login
# is disabled (creds dead), a static token still carries the admin path
_saved_admin_url = contact.SUPPORT_ADMIN_URL
contact.SUPPORT_ADMIN_URL = "https://admin.example/api"
assert contact._admin_login_url() == "", "non-/support base must not derive a login URL"
assert not contact._login_available()
assert not contact.admin_available(), "creds without a login URL must not enable the admin path"
contact.SUGAR_ADMIN_API = "static-tok"
assert contact.admin_available(), "static token still carries the admin path"
contact.SUGAR_ADMIN_API = ""
contact.SUPPORT_ADMIN_URL = _saved_admin_url

_login_calls: list = []


def _login_aware_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(
            status_code=200,
            reason="OK",
            body={"token": "fresh-token-1", "expiresAt": _now_ms + 3_600_000},
        )
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(body=_next_body)


contact.requests.post = _login_aware_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_calls.clear()
_next_body = {"id": "ADM-20", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="x", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-20", res
assert _login_calls == [{"email": "admin@example.com", "password": "pw-secret"}], _login_calls
assert len(_calls) == 1, _calls
assert _calls[0]["headers"] == {"Authorization": "Bearer fresh-token-1"}, _calls[0]["headers"]

# second call reuses the cached token -> no extra login POST
res = contact.submit_admin_ticket(
    reason="help_desk", text="y", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and len(_login_calls) == 1, (_login_calls, res)

# expired cache -> re-login before the call
contact._token_state["expires_at"] = _now_ms - 1
res = contact.submit_admin_ticket(
    reason="help_desk", text="z", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and len(_login_calls) == 2, (_login_calls, res)
assert _calls[-1]["headers"] == {"Authorization": "Bearer fresh-token-1"}, _calls[-1]["headers"]

# a thread whose 401'd token was already replaced by a competing thread must
# reuse the cached replacement, not re-login; only a still-cached stale token
# (with no newer peer token in the DB either — see 10c) forces a fresh login
_login_calls.clear()
db.save_admin_token("", 0)
contact._token_state.update(token="fresh-token-9", expires_at=_now_ms + 3_600_000, failed_until=0)
assert contact._admin_token(stale_token="old-token") == "fresh-token-9"
assert _login_calls == [], "cache already moved past the stale token -> no re-login"
assert contact._admin_token(stale_token="fresh-token-9") == "fresh-token-1"
assert len(_login_calls) == 1, "matching stale token must force a re-login"


# a 401 mid-flight (token revoked server-side) -> one re-login + one retry
def _401_then_ok_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(
            status_code=200,
            reason="OK",
            body={"token": "fresh-token-2", "expiresAt": _now_ms + 3_600_000},
        )
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    if headers == {"Authorization": "Bearer fresh-token-2"}:
        return _FakeResp(body={"id": "ADM-21", "status": "None"})
    return _FakeResp(status_code=401, reason="Unauthorized")


contact.requests.post = _401_then_ok_post
_calls.clear()
_login_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="w", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-21", res
assert len(_login_calls) == 1, _login_calls
assert [c["headers"]["Authorization"] for c in _calls] == [
    "Bearer fresh-token-1",
    "Bearer fresh-token-2",
], _calls


# a 403 mid-flight — some backends (and Cloudflare) answer a bad/irrelevant
# token with 403 rather than 401 (seen on prod adm-backend); same treatment:
# one re-login + one retry
def _403_then_ok_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(
            status_code=200,
            reason="OK",
            body={"token": "fresh-token-3", "expiresAt": _now_ms + 3_600_000},
        )
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    if headers == {"Authorization": "Bearer fresh-token-3"}:
        return _FakeResp(body={"id": "ADM-22", "status": "None"})
    return _FakeResp(status_code=403, reason="Forbidden")


contact.requests.post = _403_then_ok_post
_calls.clear()
_login_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="w", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-22", res
assert len(_login_calls) == 1, _login_calls
assert [c["headers"]["Authorization"] for c in _calls] == [
    "Bearer fresh-token-2",
    "Bearer fresh-token-3",
], _calls


# login endpoint down -> handled ok=False (server then falls back to unauth), no loop
def _login_down_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(status_code=500, reason="Server Error")
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(status_code=401, reason="Unauthorized")


contact.requests.post = _login_down_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_calls.clear()
_login_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="v", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is False, res
assert len(_login_calls) == 1, f"exactly one login attempt per call: {_login_calls}"
assert _calls == [], f"no admin POST without a bearer: {_calls}"
# the failure is negative-cached: an immediate retry must not hit login again
res = contact.submit_admin_ticket(
    reason="help_desk", text="v2", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is False and len(_login_calls) == 1, (_login_calls, res)

# login down but a static token configured -> the ticket still files on-account
# with the static bearer (the documented fallback)
def _login_down_admin_ok_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(status_code=500, reason="Server Error")
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(body=_next_body)


contact.requests.post = _login_down_admin_ok_post
contact.SUGAR_ADMIN_API = "static-fallback-token"
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-30", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="u", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-30", res
assert len(_login_calls) == 1, _login_calls
assert _calls[-1]["headers"] == {"Authorization": "Bearer static-fallback-token"}, _calls[-1]
contact.SUGAR_ADMIN_API = ""

# malformed login responses (proxy error pages, API drift) and a raising login
# POST must all come back as handled ok=False — never an exception, never an
# admin POST with a junk bearer
for _bad_body in (["Bad Gateway"], {"user": "x"}, {"token": ""}, {"token": 42}, None):

    def _login_junk_post(url, files=None, json=None, headers=None, timeout=None, _b=_bad_body):
        if url.endswith("/auth/login"):
            return _FakeResp(status_code=200, reason="OK", body=_b)
        _calls.append(
            {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
        )
        return _FakeResp(status_code=401, reason="Unauthorized")

    contact.requests.post = _login_junk_post
    contact._token_state.update(token="", expires_at=0, failed_until=0)
    db.save_admin_token("", 0)
    _calls.clear()
    res = contact.submit_admin_ticket(
        reason="help_desk", text="t", user_id="site-uuid-1", source_phone="+972501234567"
    )
    assert res["ok"] is False, (_bad_body, res)
    assert _calls == [], f"junk login body must not produce an admin POST: {_calls}"


def _login_raises_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        raise contact.requests.ConnectionError("login boom")
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(status_code=401, reason="Unauthorized")


contact.requests.post = _login_raises_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="s", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is False, res
assert _calls == [], _calls

# the thread fetch uses the managed token too
contact.requests.post = _login_aware_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_calls.clear()
_login_calls.clear()
_next_body = {"data": [{"id": "m1", "text": "hi"}], "totalItems": 1}
res = contact.fetch_admin_ticket_messages("ADM-20")
assert res["ok"] is True and res["total"] == 1, res
assert len(_login_calls) == 1, _login_calls
assert _calls[-1]["headers"] == {"Authorization": "Bearer fresh-token-1"}, _calls[-1]["headers"]

# the managed token and the password are masked in error details
contact._token_state["token"] = "tok-live"
masked = contact._safe_err(Exception("boom tok-live pw-secret"))
assert "tok-live" not in masked and "pw-secret" not in masked and "***" in masked, masked

# 10c) DB-persisted token: a login's token is written through to the DB so a
# restarted process (or a sibling Cloud Run instance) reuses it instead of
# logging in again; 401/403 first checks the DB for a peer-refreshed token.

# the thread-fetch login above must have persisted its token
row = db.load_admin_token()
assert row == {"token": "fresh-token-1", "expires_at": _now_ms + 3_600_000}, row

# cold start (empty in-memory cache) + valid DB token -> used without a login
contact.requests.post = _login_aware_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("persisted-tok-1", _now_ms + 3_600_000)
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-30", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="p", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-30", res
assert _login_calls == [], "valid persisted token must be used without a login"
assert _calls[0]["headers"] == {"Authorization": "Bearer persisted-tok-1"}, _calls[0]["headers"]

# cold start + EXPIRED DB token -> fresh login, and the new token is persisted
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("persisted-tok-old", _now_ms - 1)
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-31", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="q", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-31", res
assert len(_login_calls) == 1, _login_calls
assert _calls[0]["headers"] == {"Authorization": "Bearer fresh-token-1"}, _calls[0]["headers"]
assert db.load_admin_token()["token"] == "fresh-token-1", "fresh login must be persisted"


# our token is refused (403) while another instance already persisted a newer
# one -> adopt the peer token from the DB, no login round trip
def _403_then_peer_ok_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(
            status_code=200,
            reason="OK",
            body={"token": "fresh-token-4", "expiresAt": _now_ms + 3_600_000},
        )
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    if headers == {"Authorization": "Bearer tok-instance-b"}:
        return _FakeResp(body={"id": "ADM-32", "status": "None"})
    return _FakeResp(status_code=403, reason="Forbidden")


contact.requests.post = _403_then_peer_ok_post
contact._token_state.update(token="tok-instance-a", expires_at=_now_ms + 3_600_000, failed_until=0)
db.save_admin_token("tok-instance-b", _now_ms + 3_600_000)
_calls.clear()
_login_calls.clear()
res = contact.submit_admin_ticket(
    reason="help_desk", text="r", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-32", res
assert _login_calls == [], "peer-refreshed DB token must be adopted without a login"
assert [c["headers"]["Authorization"] for c in _calls] == [
    "Bearer tok-instance-a",
    "Bearer tok-instance-b",
], _calls

# expiry rollover adopts a fresh peer token too (not only on 401/403), so
# sibling instances don't all stampede the login endpoint at the same moment
contact.requests.post = _login_aware_post
contact._token_state.update(token="tok-expired", expires_at=_now_ms - 1, failed_until=0)
db.save_admin_token("tok-peer", _now_ms + 3_600_000)
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-34", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="t", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-34", res
assert _login_calls == [], "expired cache + fresh peer token in DB must not log in"
assert _calls[0]["headers"] == {"Authorization": "Bearer tok-peer"}, _calls[0]["headers"]

# login cooldown also shields the DB probe: with nothing cached and a recent
# login failure, _admin_token must not do DB I/O under the lock on every call
contact._token_state.update(token="", expires_at=0, failed_until=_time.time() + 30)
db.save_admin_token("", 0)
_load_probes = []
_saved_load = db.load_admin_token
db.load_admin_token = lambda: _load_probes.append(1)
assert contact._admin_token() == contact.SUGAR_ADMIN_API
assert _load_probes == [], "cooldown must short-circuit before the DB probe"
db.load_admin_token = _saved_load
contact._token_state.update(token="", expires_at=0, failed_until=0)

# DB down -> persistence is best-effort: managed login still works
contact.requests.post = _login_aware_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)


def _db_down(*a, **k):
    raise RuntimeError("db down")


_saved_load, _saved_save = db.load_admin_token, db.save_admin_token
db.load_admin_token = _db_down
db.save_admin_token = _db_down
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-33", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="s", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-33", res
assert len(_login_calls) == 1, _login_calls
db.load_admin_token, db.save_admin_token = _saved_load, _saved_save
db.save_admin_token("", 0)
print("10c. persisted admin token: cold-start reuse, expired -> relogin, peer-refresh adoption, db-down resilience")


# 10d) a failed login logs the endpoint, the email and the response body (so a
# Cloudflare/WAF block is distinguishable from a credential rejection) — but
# NEVER the password: Cloud Logging is readable by anyone with viewer access,
# and a credential leaked there has to be rotated
def _login_403_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(
            status_code=403,
            reason="Forbidden",
            # a real API rejection: JSON, so the body itself is the diagnosis
            text='{"errorCode":"AuthTokenError","message":"[AuthGuard]\n bad pw-secret"}',
        )
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(body=_next_body)


contact.requests.post = _login_403_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
_login_calls.clear()
_err = _io.StringIO()
with _redirect_stderr(_err):
    assert contact._admin_login() == ""
_logged = _err.getvalue()
assert "pw-secret" not in _logged, f"password must never be logged: {_logged}"
assert "https://admin.example/auth/login" in _logged, _logged
assert "admin@example.com" in _logged, _logged
assert "403" in _logged and "AuthGuard" in _logged, _logged
assert "\n" not in _logged.strip(), "one log line per failure (body is collapsed)"

# a network-level login failure names the endpoint too
contact.requests.post = _login_raises_post
contact._token_state.update(token="", expires_at=0, failed_until=0)
_err = _io.StringIO()
with _redirect_stderr(_err):
    assert contact._admin_login() == ""
_logged = _err.getvalue()
assert "https://admin.example/auth/login" in _logged and "login boom" in _logged, _logged
assert "pw-secret" not in _logged, _logged
contact._token_state.update(token="", expires_at=0, failed_until=0)
print("10d. login failures log endpoint + email + body snippet; password never logged")

# 10e) ADMIN_API_KEY: the admin endpoints authenticate with a static
# x-api-key header — no login, no token lifecycle. It wins over the login
# creds when both are set, so the managed login stays in the tree but inert.
contact.ADMIN_API_KEY = "key-abc-123"
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
contact.requests.post = _login_aware_post
_calls.clear()
_login_calls.clear()
_next_body = {"id": "ADM-40", "status": "None"}
res = contact.submit_admin_ticket(
    reason="help_desk", text="k", user_id="site-uuid-1", source_phone="+972501234567"
)
assert res["ok"] is True and res["ticket_id"] == "ADM-40", res
assert _login_calls == [], "api key must not trigger a login"
assert _calls[0]["headers"] == {"x-api-key": "key-abc-123"}, _calls[0]["headers"]
assert db.load_admin_token() in (None, {"token": "", "expires_at": 0}), "no token to persist"

# the thread fetch uses the same header
_calls.clear()
_next_body = {"data": [{"id": "m1", "text": "hi"}], "totalItems": 1}
res = contact.fetch_admin_ticket_messages("ADM-40")
assert res["ok"] is True and res["total"] == 1, res
assert _calls[-1]["headers"] == {"x-api-key": "key-abc-123"}, _calls[-1]["headers"]

# a 403 is final: no token to refresh, so no retry (and no login)
_calls.clear()
_login_calls.clear()
contact.requests.post = _login_403_post  # non-login URLs return _next_body...
_next_body = None


def _apikey_403_post(url, files=None, json=None, headers=None, timeout=None):
    if url.endswith("/auth/login"):
        _login_calls.append(json)
        return _FakeResp(status_code=200, reason="OK", body={"token": "t", "expiresAt": 0})
    _calls.append(
        {"url": url, "files": files, "json": json, "headers": headers, "timeout": timeout}
    )
    return _FakeResp(status_code=403, reason="Forbidden", text='{"message":"Forbidden key-abc-123"}')


contact.requests.post = _apikey_403_post
_err = _io.StringIO()
with _redirect_stderr(_err):
    res = contact.submit_admin_ticket(
        reason="help_desk", text="k", user_id="site-uuid-1", source_phone="+972501234567"
    )
assert res["ok"] is False and res["http_status"] == 403, res
assert len(_calls) == 1, "no retry without a token lifecycle"
assert _login_calls == [], "api key mode must never log in"
assert "key-abc-123" not in _err.getvalue(), f"api key must be masked: {_err.getvalue()}"

# the api key alone enables the admin path (no creds, no static token)
_saved_email, _saved_pw = contact.ADMIN_EMAIL, contact.ADMIN_PASSWORD
contact.ADMIN_EMAIL = contact.ADMIN_PASSWORD = ""
contact.SUGAR_ADMIN_API = ""
assert contact.admin_available(), "ADMIN_API_KEY alone must enable the admin path"
assert not contact._login_available()
contact.ADMIN_EMAIL, contact.ADMIN_PASSWORD = _saved_email, _saved_pw
assert "key-abc-123" not in contact._safe_err(Exception("boom key-abc-123")), "masked"
contact.ADMIN_API_KEY = ""
print("10e. ADMIN_API_KEY: x-api-key header, no login, no retry, masked in logs")

# 10f) every admin call logs itself BEFORE sending — endpoint, full payload and
# a key fingerprint — so a prod failure can be diagnosed from the logs alone.
# The key is fingerprinted, never printed whole: Cloud Logging is readable by
# anyone with viewer access, and a leaked key means rotation.
contact.ADMIN_API_KEY = "sk-admin-abcdef1234567890-xyz"
contact.requests.post = _login_aware_post
_calls.clear()
_next_body = {"id": "ADM-50", "status": "None"}
_err = _io.StringIO()
with _redirect_stderr(_err):
    res = contact.submit_admin_ticket(
        reason="help_desk",
        text="חויבתי פעמיים",
        user_id="site-uuid-7",
        source_phone="+972501234567",
    )
_logged = _err.getvalue()
assert res["ok"] is True, res
assert contact.ADMIN_API_KEY not in _logged, f"whole key must never be logged: {_logged}"
assert "sk-adm" in _logged and "len=" in _logged, f"key fingerprint expected: {_logged}"
assert "https://admin.example/support" in _logged, _logged
assert '"userId": "site-uuid-7"' in _logged, _logged
assert "ForCustomerService" in _logged and "חויבתי פעמיים" in _logged, _logged

# a Cloudflare block is named as such: the error code and Ray ID are pulled out
# of the boilerplate HTML, and the edge is identified by its headers
_CF_HTML = (
    '<!DOCTYPE html><!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US">'
    " <![endif]--><head><title>Access denied</title></head><body>"
    '<span class="cf-error-code">1020</span> error code: 1020</body></html>'
)


def _cf_blocked_post(url, files=None, json=None, headers=None, timeout=None):
    _calls.append({"url": url, "headers": headers})
    return _FakeResp(
        status_code=403,
        reason="Forbidden",
        text=_CF_HTML,
        headers={"server": "cloudflare", "cf-ray": "9a1b2c3d4e5f-TLV"},
    )


contact.requests.post = _cf_blocked_post
_err = _io.StringIO()
with _redirect_stderr(_err):
    res = contact.submit_admin_ticket(
        reason="help_desk", text="x", user_id="site-uuid-7", source_phone="+972501234567"
    )
_logged = _err.getvalue()
assert res["ok"] is False and res["http_status"] == 403, res
assert "1020" in _logged, f"Cloudflare error code must be surfaced: {_logged}"
assert "cloudflare" in _logged and "9a1b2c3d4e5f-TLV" in _logged, _logged
contact.ADMIN_API_KEY = ""
print("10f. admin calls log endpoint+payload+key fingerprint pre-send; CF blocks name code+ray")

# 10g) the service reports the public IP it egresses from, so a whitelisted
# static NAT address can be checked against what the backend actually sees.
# Best-effort: never raises, never blocks startup on a bad network.
_get_calls: list = []


class _FakeGet:
    def __init__(self, text):
        self.text = text
        self.ok = True
        self.status_code = 200


def _fake_get(url, timeout=None):
    _get_calls.append({"url": url, "timeout": timeout})
    return _FakeGet(" 34.76.12.9\n")


contact.requests.get = _fake_get
_err = _io.StringIO()
with _redirect_stderr(_err):
    contact.log_egress_ip()
_logged = _err.getvalue()
assert "34.76.12.9" in _logged, f"egress IP must be logged: {_logged}"
assert _get_calls and _get_calls[0]["timeout"], "must use a bounded timeout"


def _get_boom(url, timeout=None):
    raise contact.requests.ConnectionError("no network")


contact.requests.get = _get_boom
_err = _io.StringIO()
with _redirect_stderr(_err):
    contact.log_egress_ip()  # must not raise
assert "egress" in _err.getvalue().lower(), _err.getvalue()
print("10g. egress IP self-report: logged on success, silent-safe on failure")

# restore the static-token setup the sections below expect
contact.ADMIN_EMAIL = ""
contact.ADMIN_PASSWORD = ""
contact._token_state.update(token="", expires_at=0, failed_until=0)
db.save_admin_token("", 0)
contact.SUGAR_ADMIN_API = "test-admin-token"
contact.requests.post = _fake_post
print("10b. managed login: lazy login+cache, expiry refresh, 401/403 retry, login-down fallback, thread fetch, masking")

# 11) server routing: linked account -> admin path (on_account); anonymous -> unauth
phone2 = "+972502223344"
db.upsert_user(phone2, external_id="site-uuid-9", nickname="דנה", is_premium=True, labels=[])
_calls.clear()
_next_body = {"id": "ADM-2", "status": "None"}
result = server._submit_ticket_for(phone2, {"reason": "help_desk", "text": "רוצה נציג"})
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is True, parsed
assert "ticket_id" not in parsed, "raw ticket id must never reach the model"
assert len(_calls) == 1 and _calls[0]["url"] == "https://admin.example/support", _calls
assert _calls[0]["json"]["userId"] == "site-uuid-9", _calls[0]["json"]
rows = db.recent_support_tickets(phone2)
assert rows and rows[0]["ticket_id"] == "ADM-2", rows
_calls.clear()
_next_body = {"id": "TKT-3003", "status": "open"}
# anonymous conversation WITHOUT no_account -> no ticket yet: ask to log in first
result = server._submit_ticket_for("+972509998877", {"reason": "technical", "text": "תקלה"})
parsed = _json.loads(result)
assert parsed["submitted"] is False and parsed.get("login_required") is True, parsed
assert parsed["login_url"].startswith(server.LOGIN_URL_BASE), parsed
assert parsed["instructions"], parsed
assert _calls == [], "must not POST before the customer had a chance to log in"
assert db.recent_support_tickets("+972509998877") == [], "nothing persisted on login gate"
# schema-loose models sometimes emit booleans as strings - "false" must fail
# closed into the gate, not bool()-truthy into a guest ticket
result = server._submit_ticket_for(
    "+972509998877", {"reason": "technical", "text": "תקלה", "no_account": "false"}
)
parsed = _json.loads(result)
assert parsed["submitted"] is False and parsed.get("login_required") is True, parsed
assert _calls == [], _calls
# customer has no account / can't log in -> explicit no_account files a guest ticket
result = server._submit_ticket_for(
    "+972509998877", {"reason": "technical", "text": "תקלה", "no_account": True}
)
parsed = _json.loads(result)
assert parsed["submitted"] is True and parsed["on_account"] is False, parsed
assert len(_calls) == 1 and _calls[0]["url"] == contact.SUPPORT_TICKET_URL, _calls
assert _calls[0]["files"] is not None, "anonymous path must stay multipart"
print("11. routing: linked -> admin; anonymous -> login gate, then unauth only with no_account")

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
assert "ticket_id" not in parsed, parsed
assert db.recent_support_tickets(phone2)[0]["ticket_id"] == "TKT-4004", "fallback ticket persisted"
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

# 14) fetch_admin_ticket_messages: URL/payload/bearer contract; failures handled
TICKET_UUID = "475d8495-6274-4cd6-b2d7-cde697f5cb55"
_THREAD_BODY = {
    "totalItems": 2,
    "page": 1,
    "data": [
        {
            "id": "m2",
            "sentAt": "2026-07-21T12:00:00.000Z",
            "text": "בדקנו - החשבון שוחרר. שלחנו קישור ל-dana@gmail.com",
            "isAdminMessage": True,
            "sender": {"nickname": "Admin", "email": "admin@test.com"},
        },
        {
            "id": "m1",
            "sentAt": "2026-07-21T10:56:45.343Z",
            "text": "הלקוח מבקש שנציג אנושי יחזור אליו",
            "source": "WhatsApp",
            "isAdminMessage": True,
            "sender": {"nickname": "Bot", "email": "bot@test.com"},
        },
    ],
}
_calls.clear()
_next_body = _THREAD_BODY
res = contact.fetch_admin_ticket_messages(TICKET_UUID)
assert res["ok"] is True and res["total"] == 2 and len(res["messages"]) == 2, res
call = _calls[-1]
assert call["url"] == f"https://admin.example/support/{TICKET_UUID}/messages/list", call["url"]
assert call["json"] == {"pagination": {"limit": 50, "page": 1}}, call["json"]
assert call["headers"] == {"Authorization": "Bearer test-admin-token"}, call["headers"]
assert call["files"] is None
contact.SUGAR_ADMIN_API = ""
_calls.clear()
res = contact.fetch_admin_ticket_messages(TICKET_UUID)
assert res["ok"] is False and _calls == [], "must not POST when admin unconfigured"
contact.SUGAR_ADMIN_API = "test-admin-token"
res = contact.fetch_admin_ticket_messages("")
assert res["ok"] is False and _calls == [], "must not POST without a ticket id"
contact.requests.post = _raise_post
res = contact.fetch_admin_ticket_messages(TICKET_UUID)
assert res["ok"] is False and res["messages"] == [], res
contact.requests.post = _fake_post
# a 200 with an unrecognized shape is a loud failure, not an empty thread
_next_body = {"totalItems": 1, "data": {"items": []}}
res = contact.fetch_admin_ticket_messages(TICKET_UUID)
assert res["ok"] is False and "shape" in res["detail"], res
# strict UUID gate: canonical hex only — Unicode/ASCII 5-dash-group junk fails
assert contact.looks_like_uuid(TICKET_UUID) and not contact.looks_like_uuid("TKT-1001")
assert not contact.looks_like_uuid("אימות-תעודת-זהות-של-לקוח")
assert not contact.looks_like_uuid("not-a-real-uuid-here")
assert contact.resolve_reason("אימות-תעודת-זהות-של-לקוח") is None
print("14. fetch_admin_ticket_messages -> URL/payload/bearer; shape drift loud; strict UUID gate")

# 15) _open_ticket_note includes the newest ticket's thread; degrades gracefully
phone3 = "+972503334455"
db.add_support_ticket(phone3, ticket_id=TICKET_UUID, status="created", reason="help_desk", raw={})
# fetch failure with NO cached render yet -> note lists the ticket, no thread
contact.requests.post = _raise_post
note = server._open_ticket_note(phone3)
assert note and "פנייה" in note and "החשבון שוחרר" not in note, note
contact.requests.post = _fake_post
# successful fetch -> thread included; the raw ticket id stays out of the note
_next_body = _THREAD_BODY
_calls.clear()
note = server._open_ticket_note(phone3)
assert note and TICKET_UUID not in note, "ticket ids must stay out of the note"
assert "הלקוח מבקש שנציג אנושי יחזור אליו" in note, note
assert "החשבון שוחרר" in note, note
assert note.index("מבקש שנציג") < note.index("החשבון שוחרר"), "thread must be chronological"
assert "admin@test.com" not in note and "Admin" not in note, "sender identity must stay out"
assert "dana@gmail.com" not in note and "[מייל]" in note, "message-body PII must be redacted"
assert "15:00" in note and "12:00" not in note, "sentAt must render in Israel time, not UTC"
# the chat-filed original (source=WhatsApp, isAdminMessage=true) must NOT be
# labeled as a team reply; the real team message keeps the צוות label
assert "נשלח מהשיחה" in note, "chat-filed message must not be labeled as team"
assert note.index("נשלח מהשיחה") < note.index("מבקש שנציג"), note
assert "צוות: בדקנו" in note, note
assert "<<<" in note and ">>>" in note and "ציטוט" in note, "thread must be fenced as quoted data"
assert "השרשור המלא" in note and "תוכן הפנייה המקורי" not in note, note
assert len(_calls) == 1 and _calls[0]["url"].endswith(f"/{TICKET_UUID}/messages/list"), _calls
# fetch failure AFTER a good render -> last good thread reused (stable note,
# no prompt-cache thrash)
contact.requests.post = _raise_post
note = server._open_ticket_note(phone3)
assert note and "החשבון שוחרר" in note, "transient failure must reuse the last good thread"
contact.requests.post = _fake_post
# a successful EMPTY fetch drops the cached render: stale thread must not
# resurrect on the next failure
_next_body = {"totalItems": 0, "page": 1, "data": []}
note = server._open_ticket_note(phone3)
assert note and "פנייה" in note and "החשבון שוחרר" not in note, note
contact.requests.post = _raise_post
note = server._open_ticket_note(phone3)
assert note and "החשבון שוחרר" not in note, "emptied thread must not resurrect from cache"
contact.requests.post = _fake_post
# non-UUID ticket ids (unauth-style) never hit the admin endpoint
_calls.clear()
note2 = server._open_ticket_note(phone2)  # all of phone2's tickets are non-UUID
assert note2 and "פנייה" in note2 and _calls == [], (note2, _calls)
assert "TKT-5005" not in note2, "ticket ids must stay out of the note"
# a newer non-UUID ticket (fallback duplicate) must not mask an older
# on-account UUID ticket's thread
phone5 = "+972505556677"
TICKET_UUID_3 = "7b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"
db.add_support_ticket(phone5, ticket_id=TICKET_UUID_3, status="created", reason="help_desk", raw={})
db.add_support_ticket(phone5, ticket_id="TKT-7777", status="created", reason="other", raw={})
_next_body = _THREAD_BODY
_calls.clear()
note = server._open_ticket_note(phone5)
assert len(_calls) == 1 and _calls[0]["url"].endswith(f"/{TICKET_UUID_3}/messages/list"), _calls
assert "החשבון שוחרר" in note, "older UUID ticket's thread must still be fetched"
# admin unconfigured -> no fetch at all
contact.SUGAR_ADMIN_API = ""
_calls.clear()
note = server._open_ticket_note(phone3)
assert note and "פנייה" in note and _calls == [], "no admin config -> no fetch"
contact.SUGAR_ADMIN_API = "test-admin-token"
print("15. note thread: labels, IL time, PII-redacted, fenced; cache reuse/drop; no UUID masking")

# 16) thread hardening: mixed-type sentAt must not crash the note; long threads
#     get a truncation marker instead of the original-message hint
phone4 = "+972504445566"
TICKET_UUID_2 = "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
db.add_support_ticket(phone4, ticket_id=TICKET_UUID_2, status="created", reason="technical", raw={})
_next_body = {
    "totalItems": 2,
    "page": 1,
    "data": [
        {"id": "x1", "sentAt": 1784632909747, "text": "הודעה עם זמן מספרי", "isAdminMessage": True},
        {"id": "x2", "sentAt": "2026-07-21T10:00:00.000Z", "text": "הודעה רגילה", "isAdminMessage": False},
    ],
}
note = server._open_ticket_note(phone4)
assert note and "הודעה רגילה" in note and "הודעה עם זמן מספרי" in note, note
# epoch-millis (11:21Z) parses to a real Israel-time stamp and sorts by parsed
# time AFTER the older ISO message (10:00Z) — a lexical sort ('1…' < '2…')
# would have put it first
assert "- [] " not in note and "[2026-07-21 14:21]" in note, note
assert note.index("הודעה רגילה") < note.index("הודעה עם זמן מספרי"), (
    "numeric sentAt must sort by parsed time, not lexically"
)
_next_body = {
    "totalItems": 7,
    "page": 1,
    "data": [
        {
            "id": f"t{i}",
            "sentAt": f"2026-07-21T0{i}:00:00.000Z",
            "text": f"הודעה מספר {i}",
            "isAdminMessage": True,
        }
        for i in range(1, 8)
    ],
}
note = server._open_ticket_note(phone4)
assert "מוצגות 5 ההודעות האחרונות מתוך 7" in note, note
assert "תוכן הפנייה המקורי" not in note, "truncated thread must not claim the original is shown"
assert "הודעה מספר 7" in note and "הודעה מספר 1" not in note, note
assert "ייתכן שחסרות" not in note, "full-page truncation must not warn about pagination"
# backend holds more messages than the page returned -> the note must not
# claim these are the newest
_next_body["totalItems"] = 60
note = server._open_ticket_note(phone4)
assert "מוצגות 5 הודעות מתוך 60" in note and "ייתכן שחסרות" in note, note
assert "ההודעות האחרונות" not in note, "paginated thread must drop the 'newest' claim"
print("16. thread hardening: numeric sentAt parsed+ordered; truncation and pagination honest")

pathlib.Path(DB_PATH).unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
