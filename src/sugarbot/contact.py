"""Support-ticket sender (site "contact us" form).

Files a support ticket into the site's own help-desk system — the same
backend the public contact form (https://sugardaddy.co.il/he/contact-us)
posts to. Lets the assistant open a ticket on the customer's behalf instead
of telling them to go fill the web form themselves.

The form posts `multipart/form-data` to the "unauth" support endpoint. The
contract is configured by env so the exact endpoint / headers live in one
place:
  SUPPORT_TICKET_URL       full URL to POST to
                           (default: the site's unauth support endpoint)
  SUPPORT_TICKET_ORIGIN    Origin header the backend expects
  SUPPORT_TICKET_REFERER   Referer header the backend expects
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
The authoritative list is GET /user/option/all -> ContactUsReason; if the
site rotates these ids, update REASON_IDS (or pass a raw UUID as `reason`).
"""

from __future__ import annotations

import os
import sys

import requests

SUPPORT_TICKET_URL = os.getenv(
    "SUPPORT_TICKET_URL", "https://cl-backend.sugarinter.media/user/support/unauth"
)
SUPPORT_TICKET_ORIGIN = os.getenv("SUPPORT_TICKET_ORIGIN", "https://sugardaddy.co.il")
SUPPORT_TICKET_REFERER = os.getenv(
    "SUPPORT_TICKET_REFERER", "https://sugardaddy.co.il/he/contact-us"
)
SUPPORT_TICKET_TIMEOUT = float(os.getenv("SUPPORT_TICKET_TIMEOUT", "10"))

# Friendly, stable key -> ContactUsReason optionId. Keys are what the model
# passes; the UUIDs are what the backend requires. The first six mirror the
# reason buttons on the web form; `report` / `admin_declined` are extra
# reasons the backend accepts but the form does not surface as buttons.
REASON_IDS = {
    "login_email": "4e2f8c6f-5a9b-4cf4-bf9f-b5b21d3bacbe",       # לא מצליח להתחבר עם המייל שלי
    "help_desk": "4174349b-a7d5-4986-8a57-8dd8399ac334",         # שיחה עם נציג שירות לקוחות
    "forgot_password": "7d417f68-8554-4ec3-8209-5f846a5282e8",   # שכחתי סיסמה
    "technical": "1c366a67-dc37-4df8-8ae8-3ab07595a5c8",         # בעיה טכנית
    "remove_account": "bff68c44-0d71-47ab-b3c8-538c6b71aafc",    # הסר את החשבון שלי
    "other": "87f9f0c7-109f-4d76-9162-125b606bdf6a",             # אחר
    "report": "3e6ca411-22bb-4869-b367-4685b2c06bb6",            # דיווח
    "admin_declined": "e5e0d6b6-2108-4a93-93fb-125b4a7a6b97",    # admin declined
}

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
        {"ok": bool, "status": int | None, "detail": str}
    A missing SUPPORT_TICKET_URL or an unknown reason is a handled failure
    (ok=False), so callers (the tool dispatcher) never crash the chat turn.
    """
    if not SUPPORT_TICKET_URL:
        return {"ok": False, "status": None, "detail": "SUPPORT_TICKET_URL not set"}

    reason_id = resolve_reason(reason)
    if reason_id is None:
        return {"ok": False, "status": None, "detail": f"unknown reason: {reason}"}

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
        return {"ok": False, "status": None, "detail": f"{type(e).__name__}: {e}"}

    ok = resp.ok
    if not ok:
        print(f"[contact] support ticket HTTP {resp.status_code}", file=sys.stderr)
    return {"ok": ok, "status": resp.status_code, "detail": "created" if ok else resp.reason}
