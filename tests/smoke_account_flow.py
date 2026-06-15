"""Smoke test: account-status flow (DB check -> login_url -> auth/callback -> status with labels).

Run with:
    uv run python tests/smoke_account_flow.py

Uses a throwaway SQLite DB and dummy secrets; never touches the real .env
values (load_dotenv does not override pre-set vars) and never calls OpenAI
or the outbound sender.
"""
import json
import os
import pathlib
import tempfile

DB_PATH = str(pathlib.Path(tempfile.gettempdir()) / "sugarbot_smoke_users.db")
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules — they
# read env at import time.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["AUTH_CALLBACK_SECRET"] = "smoke-secret"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["OUTBOUND_SEND_URL"] = ""  # notifier must no-op, not call anything real
os.environ["LOGIN_URL_BASE"] = "https://qa.sugardaddy.co.il/sign-in"

from sugarbot import db  # noqa: E402

db.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from sugarbot import server  # noqa: E402

client = TestClient(server.app)
phone = "+972501234567"

# 1) Not in DB yet -> logged_in false + exact login URL
st = json.loads(server._account_status_for(phone))
assert st["logged_in"] is False, st
assert st["login_url"] == "https://qa.sugardaddy.co.il/sign-in?phoneNumber=%2B972501234567", st
print("1. unknown user -> logged_in=false, login_url:", st["login_url"])

# 2) Callback without/with wrong secret -> 401
payload = {
    "phoneNumber": phone,
    "user": {
        "id": "u_1",
        "nickname": "דני",
        "isPremium": True,
        "labels": [{"id": "l1", "name": "VIP"}, {"id": "l2", "name": "verified"}],
    },
    "accessToken": "tok_123",
}
r = client.post("/auth/callback", json=payload)
assert r.status_code == 401, r.status_code
r = client.post("/auth/callback", json=payload, headers={"X-Webhook-Secret": "wrong"})
assert r.status_code == 401, r.status_code
print("2. callback without/wrong secret -> 401")

# 3) Callback with secret -> 204, user upserted
r = client.post("/auth/callback", json=payload, headers={"X-Webhook-Secret": "smoke-secret"})
assert r.status_code == 204, (r.status_code, r.text)
print("3. callback with secret -> 204")

# 4) Now in DB -> logged_in true with labels
st = json.loads(server._account_status_for(phone))
assert st["logged_in"] is True, st
assert st["is_premium"] is True, st
assert st["labels"] == payload["user"]["labels"], st
assert st["nickname"] == "דני", st
print("4. after callback -> logged_in=true, labels:", st["labels"])

# 5) Re-login updates the row (premium expired, labels changed)
payload["user"]["isPremium"] = False
payload["user"]["labels"] = [{"id": "l3", "name": "expired"}]
r = client.post("/auth/callback", json=payload, headers={"X-Webhook-Secret": "smoke-secret"})
assert r.status_code == 204
st = json.loads(server._account_status_for(phone))
assert st["is_premium"] is False and st["labels"] == [{"id": "l3", "name": "expired"}], st
print("5. re-login upsert -> status/labels updated")

# 6) Stale login (>72h since last login) -> treated as logged_in false + stale flag
import datetime as _dt  # noqa: E402

stale_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=server.ACCOUNT_FRESHNESS_HOURS + 1)
with db._engine.begin() as conn:
    conn.execute(
        db.users_table.update()
        .where(db.users_table.c.phone_number == phone)
        .values(updated_at=stale_ts)
    )
st = json.loads(server._account_status_for(phone))
assert st["logged_in"] is False and st.get("stale") is True, st
assert st["login_url"].startswith("https://qa.sugardaddy.co.il/sign-in?phoneNumber="), st
print("6. stale login (>72h) -> logged_in=false, stale=true")

print("\nALL CHECKS PASSED")
