"""Support-ticket sender (site "contact us" form).

Files a support ticket into the site's own help-desk system — the same
backend the public contact form (https://sugardaddy.co.il/he/contact-us)
posts to. Lets the assistant open a ticket on the customer's behalf instead
of telling them to go fill the web form themselves.

The form posts `multipart/form-data` to the "unauth" support endpoint.

There is a second, authenticated path: when the customer's WhatsApp number is
linked to a site account (we know their userId), `submit_admin_ticket` files
the ticket ON that account via the admin backend (JSON POST with a bearer
token), so the team sees it attached to the user instead of anonymous.
The same admin backend also serves a ticket's message thread
(`fetch_admin_ticket_messages`), which the server surfaces to the assistant
so it can quote the team's actual reply instead of a generic "it's with the
team".

The help desk exists in two environments, selected by SUPPORT_TICKET_ENV
("qa" — the default — or "prod"). The environment picks the backend host,
the Origin/Referer headers, AND the ContactUsReason optionIds — QA and prod
are separate databases, so the same reason has a *different* UUID in each.
Until launch tickets go to QA only; flip SUPPORT_TICKET_ENV=prod to go live.

Env configuration (all optional):
  SUPPORT_TICKET_ENV       "qa" (default) or "prod" — selects endpoint,
                           headers and reason-id map as one consistent set
  SUPPORT_TICKET_URL       override the full URL to POST to
  SUPPORT_TICKET_ORIGIN    override the Origin header
  SUPPORT_TICKET_REFERER   override the Referer header
  SUPPORT_TICKET_TIMEOUT   request timeout in seconds (default 10)
  SUGAR_ADMIN_API          static bearer token for the admin backend; used
                           as-is when no login credentials are set, and as
                           a fallback when login fails
  ADMIN_EMAIL              admin-panel login credentials. When both are set
  ADMIN_PASSWORD           the bot manages its own bearer: it POSTs
                           {email, password} to /auth/login, caches the
                           returned token until shortly before expiresAt,
                           and re-logins once on a 401. Unset both AND
                           SUGAR_ADMIN_API -> admin path unavailable
                           (anonymous filing only)
  ADMIN_LOGIN_URL          override the login endpoint (default: derived
                           from SUPPORT_ADMIN_URL by swapping /support
                           for /auth/login)
  SUPPORT_ADMIN_URL        override the admin support endpoint (default
                           per SUPPORT_TICKET_ENV; an empty override
                           falls back to the env default)
  SUPPORT_ADMIN_TIMEOUT    admin request timeout in seconds (default 3 —
                           deliberately short: the anonymous fallback
                           covers admin failures, and the thread fetch
                           runs on the chat hot path)

Multipart fields posted (mirrors the site frontend's FormData):
  reason               required — a ContactUsReason optionId
  text                 the message body
  sourceEmail          optional — customer email (unauth only)
  sourcePhoneNumber    optional — customer phone (unauth only)
  image                optional — file upload (not used by the tool yet)

`reason` must be an *optionId* (a UUID), not a free-text label. The known
ContactUsReason optionIds are mapped below from stable friendly keys, so the
model picks a semantic key (e.g. "technical") and we resolve the UUID here.
The authoritative list is GET <backend>/user/option/all -> ContactUsReason
(per environment); if the site rotates these ids, update REASON_IDS_BY_ENV
(or pass a raw UUID as `reason`).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import requests

SUPPORT_TICKET_ENV = os.getenv("SUPPORT_TICKET_ENV", "qa").strip().lower()
if SUPPORT_TICKET_ENV not in ("qa", "prod"):
    print(
        f"[contact] unknown SUPPORT_TICKET_ENV {SUPPORT_TICKET_ENV!r}; falling back to qa",
        file=sys.stderr,
    )
    SUPPORT_TICKET_ENV = "qa"

_ENV_DEFAULTS = {
    "qa": {
        "url": "https://backend-clients.sugarinter.media/user/support/unauth",
        "origin": "https://qa.sugardaddy.co.il",
        "referer": "https://qa.sugardaddy.co.il/he/contact-us",
    },
    "prod": {
        "url": "https://cl-backend.sugarinter.media/user/support/unauth",
        "origin": "https://sugardaddy.co.il",
        "referer": "https://sugardaddy.co.il/he/contact-us",
    },
}

SUPPORT_TICKET_URL = os.getenv("SUPPORT_TICKET_URL", _ENV_DEFAULTS[SUPPORT_TICKET_ENV]["url"])
SUPPORT_TICKET_ORIGIN = os.getenv(
    "SUPPORT_TICKET_ORIGIN", _ENV_DEFAULTS[SUPPORT_TICKET_ENV]["origin"]
)
SUPPORT_TICKET_REFERER = os.getenv(
    "SUPPORT_TICKET_REFERER", _ENV_DEFAULTS[SUPPORT_TICKET_ENV]["referer"]
)
SUPPORT_TICKET_TIMEOUT = float(os.getenv("SUPPORT_TICKET_TIMEOUT", "10"))

# Admin help-desk API — files a ticket ON a signed-in user's account. Like the
# unauth endpoints above, QA and prod are separate backends; the /auth/login
# endpoint is derived from these by _admin_login_url (…/support -> /auth/login).
_ADMIN_ENV_DEFAULTS = {
    "qa": {"url": "https://backend-admin.sugarinter.media/support"},
    "prod": {"url": "https://adm-backend.sugarinter.media/support"},
}
# Whitespace-collapsed, not just stripped: a line-wrapped copy-paste would
# otherwise break the Authorization header, and requests' InvalidHeader error
# embeds the offending value (the token) in its message.
SUGAR_ADMIN_API = "".join(os.getenv("SUGAR_ADMIN_API", "").split())
# A set-but-empty override falls back to the env default (disable the admin
# path by unsetting the token, not by blanking the URL); normalized once here
# so every consumer gets a clean, slash-free base.
SUPPORT_ADMIN_URL = (
    (os.getenv("SUPPORT_ADMIN_URL") or _ADMIN_ENV_DEFAULTS[SUPPORT_TICKET_ENV]["url"])
    .strip()
    .rstrip("/")
)
SUPPORT_ADMIN_TIMEOUT = float(os.getenv("SUPPORT_ADMIN_TIMEOUT", "3"))

# Admin-panel login credentials. When set, the bearer token is obtained from
# POST /auth/login and refreshed automatically (the static SUGAR_ADMIN_API
# tokens the panel issues expire, which used to silently demote every ticket
# to the anonymous path once the pasted token aged out).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_LOGIN_URL = (os.getenv("ADMIN_LOGIN_URL") or "").strip().rstrip("/")
if ADMIN_EMAIL and ADMIN_PASSWORD and not ADMIN_LOGIN_URL and not SUPPORT_ADMIN_URL.endswith("/support"):
    # Without this warning the creds would be silently dead (no login URL to
    # derive) and every recognized user's ticket would quietly file anonymously.
    print(
        "[contact] ADMIN_EMAIL/ADMIN_PASSWORD set but no login URL: "
        "SUPPORT_ADMIN_URL does not end in /support and ADMIN_LOGIN_URL is "
        "unset; managed bearer disabled — set ADMIN_LOGIN_URL",
        file=sys.stderr,
    )

# Managed token cache: expires_at is the login response's epoch-ms expiresAt
# (0 = unknown -> trust the token until a 401 forces a refresh); failed_until
# negative-caches a failed login (epoch seconds) so a login outage costs one
# attempt per cooldown window instead of two per chat turn. The lock keeps
# concurrent chat threads from stampeding the login endpoint.
_token_lock = threading.Lock()
_token_state = {"token": "", "expires_at": 0, "failed_until": 0.0}
_TOKEN_SLACK_MS = 60_000  # refresh this long before expiresAt
_LOGIN_COOLDOWN_S = 30.0  # don't re-attempt login for this long after a failure

# Friendly, stable key -> ContactUsReason optionId, per environment. Keys are
# what the model passes; the UUIDs are what the backend requires. QA and prod
# are separate databases — the same reason has a different optionId in each
# (both maps snapshotted from GET <backend>/user/option/all on 2026-07-14).
# The first six mirror the reason buttons on the web form; `report` /
# `admin_declined` are extra reasons the backend accepts but the form does
# not surface as buttons.
REASON_IDS_BY_ENV = {
    "qa": {
        "login_email": "7fab31ea-c7c0-488a-ad76-06363753fa0c",       # לא מצליח להתחבר עם המייל שלי
        "help_desk": "e8c0b387-6f3d-48e4-9cfa-20d1f35bb701",         # שיחה עם נציג שירות לקוחות
        "forgot_password": "0866af52-67e2-4d57-86c0-28bc19078e52",   # שכחתי סיסמה
        "technical": "8ddc112a-e53a-4236-a9be-7f7f1db729ba",         # בעיה טכנית
        "remove_account": "8cdfdc39-1b26-4ba3-89bb-92407c84e11f",    # הסר את החשבון שלי
        "other": "e3735ae8-8ec5-451d-9a4e-cf06b6c75ae0",             # אחר
        "report": "1dadbff9-e64c-450b-b63f-27ef76238b47",            # דיווח
        "admin_declined": "ea78e71e-6f01-40a3-a94a-149d80203f34",    # admin declined
        "stop_payment": "7d83d6f5-3b1b-4bae-8e69-94fc09fb85ca",      # הפסקת תשלום
        "id_verification": "bfae8355-20ba-4141-8350-2eba146d6e3c",   # אימות תעודת זהות
        "photo_verification": "cc2c5598-c3a6-4664-b6d0-038e1369658b",  # אימות תמונה
    },
    # stop_payment / id_verification / photo_verification: prod optionIds are
    # unknown until launch (snapshot them from prod /user/option/all); until
    # then resolve_reason degrades those keys to help_desk in prod.
    "prod": {
        "login_email": "4e2f8c6f-5a9b-4cf4-bf9f-b5b21d3bacbe",       # לא מצליח להתחבר עם המייל שלי
        "help_desk": "4174349b-a7d5-4986-8a57-8dd8399ac334",         # שיחה עם נציג שירות לקוחות
        "forgot_password": "7d417f68-8554-4ec3-8209-5f846a5282e8",   # שכחתי סיסמה
        "technical": "1c366a67-dc37-4df8-8ae8-3ab07595a5c8",         # בעיה טכנית
        "remove_account": "bff68c44-0d71-47ab-b3c8-538c6b71aafc",    # הסר את החשבון שלי
        "other": "87f9f0c7-109f-4d76-9162-125b606bdf6a",             # אחר
        "report": "3e6ca411-22bb-4869-b367-4685b2c06bb6",            # דיווח
        "admin_declined": "e5e0d6b6-2108-4a93-93fb-125b4a7a6b97",    # admin declined
    },
}

REASON_IDS = REASON_IDS_BY_ENV[SUPPORT_TICKET_ENV]

# The reason keys the assistant is allowed to choose (surfaced in the tool
# schema). `admin_declined` is intentionally excluded — it is an internal
# state, not a customer-initiated reason.
REASON_CHOICES = [
    "login_email",
    "help_desk",
    "forgot_password",
    "technical",
    "remove_account",
    "report",
    "stop_payment",
    "id_verification",
    "photo_verification",
    "other",
]


def looks_like_uuid(value: str) -> bool:
    """Strict canonical-form UUID check (8-4-4-4-12 hex).

    str.isalnum() is Unicode-aware, so a loose shape check would accept
    5-dash-group Hebrew/non-hex strings and forward them to the backend as
    optionIds or URL path segments."""
    if not isinstance(value, str) or len(value) != 36 or value.count("-") != 4:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def resolve_reason(reason: str) -> str | None:
    """Map a friendly reason key to its optionId. A raw UUID passes through.

    Returns None if `reason` is neither a known key nor a UUID-shaped string.
    """
    if not reason:
        return None
    reason = reason.strip()
    if reason in REASON_IDS:
        return REASON_IDS[reason]
    if reason in REASON_CHOICES:
        # Known category not yet mapped in this environment (e.g. prod ids
        # pending) — degrade to help_desk so the ticket still files.
        fallback = REASON_IDS.get("help_desk")
        if fallback:
            print(
                f"[contact] reason {reason!r} has no optionId in the active reason map; "
                "using help_desk",
                file=sys.stderr,
            )
            return fallback
    # Already a raw optionId (e.g. copied straight from /user/option/all)?
    # Note: env-blind — a UUID from the wrong environment is rejected by the
    # backend, which surfaces as a handled ok=False.
    if looks_like_uuid(reason):
        return reason
    return None


# The backend's response shape for a created ticket is not documented, so we
# probe common id/status key names at the top level and inside a few common
# wrapper objects. The full body is kept (raw) so the real shape can be
# inspected in the DB and these lists refined.
_ID_KEYS = (
    "id", "ticketId", "ticketID", "ticket_id", "_id",
    "ticketNumber", "ticketNo", "number", "uuid",
)
_STATUS_KEYS = ("status", "state", "ticketStatus")
_CONTAINERS = ("data", "ticket", "result", "payload")


def _first_key(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _extract_ticket_fields(resp) -> tuple[str | None, str | None, object]:
    """Best-effort (ticket_id, status, raw_body) from a support response."""
    try:
        body = resp.json()
    except ValueError:
        return None, None, None
    if not isinstance(body, dict):
        return None, None, body
    ticket_id = _first_key(body, _ID_KEYS)
    status = _first_key(body, _STATUS_KEYS)
    for container in _CONTAINERS:
        inner = body.get(container)
        if isinstance(inner, dict):
            ticket_id = ticket_id or _first_key(inner, _ID_KEYS)
            status = status or _first_key(inner, _STATUS_KEYS)
    return ticket_id, status, body


def _result(
    ok: bool,
    *,
    http_status: int | None = None,
    ticket_id: str | None = None,
    ticket_status: str | None = None,
    detail: str = "",
    raw: object = None,
) -> dict:
    return {
        "ok": ok,
        "http_status": http_status,
        "ticket_id": ticket_id,
        "ticket_status": ticket_status,
        "detail": detail,
        "raw": raw,
    }


def _safe_err(e: Exception) -> str:
    """Format an exception for logs/details with admin secrets masked —
    some requests exceptions (e.g. InvalidHeader) embed the header value."""
    s = f"{type(e).__name__}: {e}"
    for secret in (SUGAR_ADMIN_API, _token_state["token"], ADMIN_PASSWORD):
        if secret:
            s = s.replace(secret, "***")
    return s


def _send_ticket_request(
    kind: str,
    url: str,
    *,
    files: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float | None = None,
    admin_auth: bool = False,
) -> dict:
    """POST a help-desk submit request and parse the ticket response.

    Shared tail for both senders so their behavior can't drift. Never raises.
    With admin_auth=True the request goes through _admin_post (managed bearer
    + one re-login retry on 401) and `headers` is ignored."""
    try:
        if admin_auth:
            resp = _admin_post(url, json_body=json_body, timeout=timeout)
        else:
            resp = requests.post(
                url, files=files, json=json_body, headers=headers, timeout=timeout
            )
    except requests.RequestException as e:
        print(f"[contact] {kind} failed: {_safe_err(e)}", file=sys.stderr)
        return _result(False, detail=_safe_err(e))

    if not resp.ok:
        print(f"[contact] {kind} HTTP {resp.status_code}", file=sys.stderr)
        return _result(False, http_status=resp.status_code, detail=resp.reason)

    ticket_id, status, raw = _extract_ticket_fields(resp)
    if status == "None":
        # The help desk's initial status enum is the literal string "None"
        # (= new/unhandled). Treat it as absent so it doesn't reach the
        # customer as "סטטוס: None"; the raw body keeps the original value.
        status = None
    # Always store a status; fall back to "created" when the body omits one.
    return _result(
        True,
        http_status=resp.status_code,
        ticket_id=ticket_id,
        ticket_status=status or "created",
        detail="created",
        raw=raw,
    )


def submit_ticket(
    reason: str,
    text: str,
    *,
    source_email: str | None = None,
    source_phone: str | None = None,
    image: tuple | None = None,
) -> dict:
    """POST a support ticket. Returns a result dict; never raises.

    `reason` is a friendly key from REASON_CHOICES (or a raw optionId).
    `image`, when given, is a requests-style file tuple
    (filename, fileobj_or_bytes, content_type).

    Result dict shape:
        {"ok": bool, "http_status": int | None, "ticket_id": str | None,
         "ticket_status": str | None, "detail": str, "raw": object}
    On success ticket_id/ticket_status are parsed from the response body
    (ticket_status falls back to "created" when the body omits one). A missing
    SUPPORT_TICKET_URL or an unknown reason is a handled failure (ok=False), so
    callers (the tool dispatcher) never crash the chat turn.
    """
    if not SUPPORT_TICKET_URL:
        return _result(False, detail="SUPPORT_TICKET_URL not set")

    reason_id = resolve_reason(reason)
    if reason_id is None:
        return _result(False, detail=f"unknown reason: {reason}")

    # Send as multipart/form-data (what the web form uses). Passing every field
    # through `files` as (None, value) makes requests encode plain form fields
    # under a multipart boundary even when there is no file attachment.
    files: dict[str, tuple] = {
        "reason": (None, reason_id),
        "text": (None, text or ""),
    }
    if source_email:
        files["sourceEmail"] = (None, source_email)
    if source_phone:
        files["sourcePhoneNumber"] = (None, source_phone)
    if image is not None:
        files["image"] = image

    headers = {
        "Origin": SUPPORT_TICKET_ORIGIN,
        "Referer": SUPPORT_TICKET_REFERER,
    }

    return _send_ticket_request(
        "support ticket",
        SUPPORT_TICKET_URL,
        files=files,
        headers=headers,
        timeout=SUPPORT_TICKET_TIMEOUT,
    )


def _admin_login_url() -> str:
    """Login endpoint for the managed bearer. Explicit override wins;
    otherwise derived from SUPPORT_ADMIN_URL (…/support -> …/auth/login)."""
    if ADMIN_LOGIN_URL:
        return ADMIN_LOGIN_URL
    if SUPPORT_ADMIN_URL.endswith("/support"):
        return SUPPORT_ADMIN_URL[: -len("/support")] + "/auth/login"
    return ""


def _login_available() -> bool:
    return bool(ADMIN_EMAIL and ADMIN_PASSWORD and _admin_login_url())


def _admin_login() -> str:
    """POST /auth/login and cache the returned bearer. Returns "" on failure.

    Callers hold _token_lock. Never raises; a failure is negative-cached for
    _LOGIN_COOLDOWN_S and leaves the static-token / anonymous fallbacks to do
    their job.
    """
    def _failed(msg: str) -> str:
        print(f"[contact] admin login {msg}", file=sys.stderr)
        _token_state["failed_until"] = time.time() + _LOGIN_COOLDOWN_S
        return ""

    try:
        resp = requests.post(
            _admin_login_url(),
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=SUPPORT_ADMIN_TIMEOUT,
        )
    except requests.RequestException as e:
        return _failed(f"failed: {_safe_err(e)}")
    if not resp.ok:
        return _failed(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        return _failed("returned no token")
    expires = body.get("expiresAt")
    _token_state["token"] = token
    _token_state["expires_at"] = expires if isinstance(expires, (int, float)) else 0
    _token_state["failed_until"] = 0.0
    return token


def _admin_token(stale_token: str = "") -> str:
    """Current bearer for the admin API.

    With login credentials configured the token comes from /auth/login and is
    cached until _TOKEN_SLACK_MS before expiresAt; without them the static
    SUGAR_ADMIN_API is used as-is (its expiry shows up as a 401 -> the caller
    falls back to the anonymous path, as before).

    `stale_token` is the token a caller just saw 401: a login happens only if
    the cache still holds that exact token — if a competing thread already
    replaced it, the cached replacement is returned instead of stampeding the
    login endpoint (and risking a needless failure while a valid token sits
    in the cache).
    """
    if not _login_available():
        return SUGAR_ADMIN_API
    with _token_lock:
        token = _token_state["token"]
        expires_at = _token_state["expires_at"]
        fresh = token and (
            not expires_at or time.time() * 1000 < expires_at - _TOKEN_SLACK_MS
        )
        if fresh and token != stale_token:
            return token
        if time.time() < _token_state["failed_until"]:
            return SUGAR_ADMIN_API
        return _admin_login() or SUGAR_ADMIN_API


def _admin_post(url: str, *, json_body: dict, timeout: float):
    """POST to the admin API with the managed bearer; one re-login retry on
    401 (token revoked/expired server-side despite a fresh-looking cache).

    Raises requests.RequestException like a plain post — callers already
    handle that, including the fail-fast when no bearer could be obtained
    at all (login down, no static token): posting "Bearer " would only add
    a guaranteed-401 round trip on the chat hot path.
    """
    token = _admin_token()
    if not token:
        raise requests.RequestException("no admin bearer available")
    resp = requests.post(
        url,
        json=json_body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if resp.status_code == 401 and _login_available():
        fresh = _admin_token(stale_token=token)
        if fresh and fresh != token:
            resp = requests.post(
                url,
                json=json_body,
                headers={"Authorization": f"Bearer {fresh}"},
                timeout=timeout,
            )
    return resp


def admin_available() -> bool:
    """True when the admin (on-account) ticket path is configured — either a
    static token or login credentials.

    Reads the module globals at call time so tests (and future hot config)
    can swap SUGAR_ADMIN_API / SUPPORT_ADMIN_URL / creds after import.
    """
    return bool(SUPPORT_ADMIN_URL and (SUGAR_ADMIN_API or _login_available()))


def _messages_result(ok: bool, *, total=None, messages: list | None = None, detail: str = "") -> dict:
    return {"ok": ok, "total": total, "messages": messages or [], "detail": detail}


def fetch_admin_ticket_messages(ticket_id: str, *, limit: int = 50, page: int = 1) -> dict:
    """Fetch a ticket's message thread from the admin API. Never raises.

    Returns {"ok": bool, "total": int | None, "messages": list[dict], "detail": str}.
    `messages` are the backend's message dicts as-is (id, sentAt, text, source,
    isAdminMessage, sender, ...); callers pick the fields they need.
    """
    if not admin_available():
        return _messages_result(False, detail="admin API not configured")
    if not ticket_id:
        return _messages_result(False, detail="no ticket id")

    url = f"{SUPPORT_ADMIN_URL}/{ticket_id}/messages/list"
    try:
        resp = _admin_post(
            url,
            json_body={"pagination": {"limit": limit, "page": page}},
            timeout=SUPPORT_ADMIN_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[contact] ticket messages fetch failed: {_safe_err(e)}", file=sys.stderr)
        return _messages_result(False, detail=_safe_err(e))

    if not resp.ok:
        print(f"[contact] ticket messages HTTP {resp.status_code}", file=sys.stderr)
        return _messages_result(False, detail=f"HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        # A 200 whose shape we don't recognize is a loud failure: treating it
        # as an empty thread would silently blank every note, with no HTTP
        # error anywhere to hint why.
        print("[contact] ticket messages: unexpected response shape", file=sys.stderr)
        return _messages_result(False, detail="unexpected response shape")
    messages = [m for m in data if isinstance(m, dict)]
    return _messages_result(True, total=body.get("totalItems"), messages=messages)


def submit_admin_ticket(
    reason: str,
    text: str,
    *,
    user_id: str,
    source_phone: str,
    source_email: str | None = None,
) -> dict:
    """POST a support ticket onto a site account via the admin API.

    `user_id` is the site account id (users.external_id). Returns the same
    result dict as submit_ticket; never raises — callers fall back to the
    anonymous submit_ticket path on ok=False.
    """
    if not admin_available():
        return _result(False, detail="admin API not configured")

    reason_id = resolve_reason(reason)
    if reason_id is None:
        return _result(False, detail=f"unknown reason: {reason}")

    # Mirrors the admin panel's manual-ticket payload. sourceNickname stays
    # empty on purpose: the backend resolves the account (and its current
    # nickname) from userId, and our cached copy may be stale. status
    # "ForCustomerService" opens the ticket straight in the customer-service
    # queue (instead of the untriaged "None" state).
    payload = {
        "userId": user_id,
        "sources": ["WhatsApp"],
        "reason": reason_id,
        "text": text or "",
        "status": "ForCustomerService",
        "sourcePhoneNumber": source_phone,
        "sourceNickname": "",
        "sourceEmail": source_email or "",
        "sourceUserId": "",
        "isReplyFromCustomer": False,
    }

    return _send_ticket_request(
        "admin ticket",
        SUPPORT_ADMIN_URL,
        json_body=payload,
        timeout=SUPPORT_ADMIN_TIMEOUT,
        admin_auth=True,
    )
