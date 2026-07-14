"""Support-ticket sender (site "contact us" form).

Files a support ticket into the site's own help-desk system — the same
backend the public contact form (https://sugardaddy.co.il/he/contact-us)
posts to. Lets the assistant open a ticket on the customer's behalf instead
of telling them to go fill the web form themselves.

The form posts `multipart/form-data` to the "unauth" support endpoint.

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
    },
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
    "other",
]


def resolve_reason(reason: str) -> str | None:
    """Map a friendly reason key to its optionId. A raw UUID passes through.

    Returns None if `reason` is neither a known key nor a UUID-shaped string.
    """
    if not reason:
        return None
    reason = reason.strip()
    if reason in REASON_IDS:
        return REASON_IDS[reason]
    # Already a raw optionId (e.g. copied straight from /user/option/all)?
    parts = reason.split("-")
    if len(parts) == 5 and all(parts) and reason.replace("-", "").isalnum():
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

    try:
        resp = requests.post(
            SUPPORT_TICKET_URL,
            files=files,
            headers=headers,
            timeout=SUPPORT_TICKET_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[contact] support ticket failed: {type(e).__name__}: {e}", file=sys.stderr)
        return _result(False, detail=f"{type(e).__name__}: {e}")

    if not resp.ok:
        print(f"[contact] support ticket HTTP {resp.status_code}", file=sys.stderr)
        return _result(False, http_status=resp.status_code, detail=resp.reason)

    ticket_id, status, raw = _extract_ticket_fields(resp)
    # Always store a status; fall back to "created" when the body omits one.
    return _result(
        True,
        http_status=resp.status_code,
        ticket_id=ticket_id,
        ticket_status=status or "created",
        detail="created",
        raw=raw,
    )
