"""Smoke test: support-ticket tool (contact.submit_ticket + server wiring).

Run with:
    uv run python tests/smoke_support_ticket.py

Never hits the network — requests.post is monkeypatched to capture the call.
Verifies reason-key resolution, the multipart fields the backend expects,
that the conversation phone is attached as sourcePhoneNumber, success/failure
handling, and that the tool is registered in the server's tool list.
"""
import os

os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"

from sugarbot import contact  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=201, reason="Created"):
        self.status_code = status_code
        self.reason = reason
        self.ok = 200 <= status_code < 300


# --- capture whatever submit_ticket posts, without any network ------------
_calls: list[dict] = []


def _fake_post(url, files=None, headers=None, timeout=None):
    _calls.append({"url": url, "files": files, "headers": headers, "timeout": timeout})
    return _FakeResp()


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

# 2) successful submit builds the right multipart fields + headers
_calls.clear()
res = contact.submit_ticket(
    reason="technical",
    text="לא מצליח להעלות תמונה",
    source_email="user@example.com",
    source_phone="+972501234567",
)
assert res == {"ok": True, "status": 201, "detail": "created"}, res
call = _calls[-1]
files = call["files"]
# fields are (None, value) tuples so requests encodes them as multipart form fields
assert files["reason"] == (None, "1c366a67-dc37-4df8-8ae8-3ab07595a5c8"), files["reason"]
assert files["text"] == (None, "לא מצליח להעלות תמונה"), files["text"]
assert files["sourceEmail"] == (None, "user@example.com"), files.get("sourceEmail")
assert files["sourcePhoneNumber"] == (None, "+972501234567"), files.get("sourcePhoneNumber")
assert call["headers"]["Origin"] == "https://sugardaddy.co.il", call["headers"]
assert call["headers"]["Referer"] == "https://sugardaddy.co.il/he/contact-us", call["headers"]
assert call["url"] == "https://backend.example/user/support/unauth", call["url"]
print("2. submit_ticket -> multipart fields, headers, url, ok=True")

# 3) optional fields omitted when not given
_calls.clear()
contact.submit_ticket(reason="other", text="שאלה")
files = _calls[-1]["files"]
assert "sourceEmail" not in files and "sourcePhoneNumber" not in files, files
assert set(files) == {"reason", "text"}, files
print("3. optional email/phone omitted when absent")

# 4) unknown reason is a handled failure (no network call, no crash)
_calls.clear()
res = contact.submit_ticket(reason="bogus", text="x")
assert res["ok"] is False and res["status"] is None, res
assert _calls == [], "must not POST on an unknown reason"
print("4. unknown reason -> ok=False, no POST")

# 5) network error is swallowed -> ok=False
def _raise_post(*a, **k):
    raise contact.requests.ConnectionError("boom")


contact.requests.post = _raise_post
res = contact.submit_ticket(reason="help_desk", text="x", source_phone="+972500000000")
assert res["ok"] is False and res["status"] is None, res
print("5. network error -> ok=False, no raise")

# restore for any later import users
contact.requests.post = _fake_post

# 6) server registers the tool with the expected enum, phone stays out of args
from sugarbot import server  # noqa: E402

names = [t["function"]["name"] for t in server.TOOLS_ALL]
assert "submit_support_ticket" in names, names
tool = next(t for t in server.TOOLS_ALL if t["function"]["name"] == "submit_support_ticket")
params = tool["function"]["parameters"]
assert params["properties"]["reason"]["enum"] == contact.REASON_CHOICES, params
assert params["required"] == ["reason", "text"], params
# phone must NOT be a model-supplied argument (resolved from the conversation)
assert "phone" not in params["properties"] and "sourcePhoneNumber" not in params["properties"]
print("6. server.TOOLS_ALL registers submit_support_ticket; phone not a model arg")

print("\nALL CHECKS PASSED")
