"""Outbound message sender.

Pushes a WhatsApp message to a customer via the Unichat whatsapp-api service
(POST /api/messages/send). Used to proactively notify a user (e.g. "I see you
connected") the moment the auth callback lands, independent of whether they
have an open chat session.

Configured by env so the deployment details live in one place:
  WHATSAPP_API_URL           base URL of the whatsapp-api service (required to send)
  WHATSAPP_API_TOKEN         JWT bearer token; must carry a `workspaces` claim
                             that includes WHATSAPP_API_WORKSPACE_ID
  WHATSAPP_API_WORKSPACE_ID  sent as x-workspace-id (required by authenticate)
  WHATSAPP_API_ACCOUNT_ID    sent as x-account-id (selects the sending number)
  WHATSAPP_API_TIMEOUT       request timeout in seconds (default 10)

Body posted: {"to": <972-format digits>, "text": <text>}

Note: whatsapp-api rejects free-form sends to chats whose last inbound message
is older than 24h (WhatsApp window) — those need a template message instead.
"""

from __future__ import annotations

import os
import sys

import requests

WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "").rstrip("/")
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN", "")
WHATSAPP_API_WORKSPACE_ID = os.getenv("WHATSAPP_API_WORKSPACE_ID", "")
WHATSAPP_API_ACCOUNT_ID = os.getenv("WHATSAPP_API_ACCOUNT_ID", "")
WHATSAPP_API_TIMEOUT = float(os.getenv("WHATSAPP_API_TIMEOUT", "10"))


def _to_e164_digits(phone: str) -> str:
    """Local IL phone to E.164 digits without '+': "0501234567" -> "972501234567".

    Mirrors whatsapp-api's toE164Digits so both sides key the same number.
    Numbers already carrying a country code pass through unchanged.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("972"):
        return digits
    if digits.startswith("0"):
        return "972" + digits[1:]
    if len(digits) == 9:
        return "972" + digits
    return digits


def send_message(phone_number: str, message: str) -> bool:
    """POST an outbound message. Returns True on success, False otherwise.

    Best-effort: never raises. Missing configuration is treated as a no-op
    (logged) so callers (e.g. the auth callback) don't fail when the outbound
    channel isn't configured yet.
    """
    missing = [
        name
        for name, value in (
            ("WHATSAPP_API_URL", WHATSAPP_API_URL),
            ("WHATSAPP_API_TOKEN", WHATSAPP_API_TOKEN),
            ("WHATSAPP_API_WORKSPACE_ID", WHATSAPP_API_WORKSPACE_ID),
            ("WHATSAPP_API_ACCOUNT_ID", WHATSAPP_API_ACCOUNT_ID),
        )
        if not value
    ]
    if missing:
        print(
            f"[notifier] {', '.join(missing)} not set; skipping outbound send",
            file=sys.stderr,
        )
        return False

    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "x-workspace-id": WHATSAPP_API_WORKSPACE_ID,
        "x-account-id": WHATSAPP_API_ACCOUNT_ID,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{WHATSAPP_API_URL}/api/messages/send",
            json={"to": _to_e164_digits(phone_number), "text": message},
            headers=headers,
            timeout=WHATSAPP_API_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        detail = ""
        body = getattr(getattr(e, "response", None), "text", "")
        if body:
            detail = f" body={body[:300]}"
        print(
            f"[notifier] outbound send failed: {type(e).__name__}: {e}{detail}",
            file=sys.stderr,
        )
        return False
