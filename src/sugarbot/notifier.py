"""Outbound message sender.

Pushes a WhatsApp message to a customer via the site's own messaging service.
Used to proactively notify a user (e.g. "I see you connected") the moment the
auth callback lands, independent of whether they have an open chat session.

Contract is configured by env so the exact endpoint/field names live in one
place:
  OUTBOUND_SEND_URL            full URL to POST to (required to send)
  OUTBOUND_SEND_SECRET         shared secret (optional)
  OUTBOUND_SEND_SECRET_HEADER  header to carry the secret (default X-Internal-Secret)
  OUTBOUND_SEND_TIMEOUT        request timeout in seconds (default 10)

Body posted: {"phoneNumber": <phone>, "message": <text>}
"""

from __future__ import annotations

import os
import sys

import requests

OUTBOUND_SEND_URL = os.getenv("OUTBOUND_SEND_URL", "")
OUTBOUND_SEND_SECRET = os.getenv("OUTBOUND_SEND_SECRET", "")
OUTBOUND_SEND_SECRET_HEADER = os.getenv("OUTBOUND_SEND_SECRET_HEADER", "X-Internal-Secret")
OUTBOUND_SEND_TIMEOUT = float(os.getenv("OUTBOUND_SEND_TIMEOUT", "10"))


def send_message(phone_number: str, message: str) -> bool:
    """POST an outbound message. Returns True on success, False otherwise.

    Best-effort: never raises. A missing OUTBOUND_SEND_URL is treated as a
    no-op (logged) so callers (e.g. the auth callback) don't fail when the
    outbound channel isn't configured yet.
    """
    if not OUTBOUND_SEND_URL:
        print("[notifier] OUTBOUND_SEND_URL not set; skipping outbound send", file=sys.stderr)
        return False

    headers = {"Content-Type": "application/json"}
    if OUTBOUND_SEND_SECRET:
        headers[OUTBOUND_SEND_SECRET_HEADER] = OUTBOUND_SEND_SECRET

    try:
        resp = requests.post(
            OUTBOUND_SEND_URL,
            json={"phoneNumber": phone_number, "message": message},
            headers=headers,
            timeout=OUTBOUND_SEND_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[notifier] outbound send failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
