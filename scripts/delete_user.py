#!/usr/bin/env python3
"""Erase ALL stored data for a phone number ("forget me" / account purge).

Calls the bot API's POST /user/delete, which atomically deletes the chat
history, cached login row, conversation state, and support tickets for the
phone. Accepts the number in any local/international form.

Usage:
    uv run python scripts/delete_user.py 0501234567
    uv run python scripts/delete_user.py +972501234567 --base-url http://localhost:8000
    uv run python scripts/delete_user.py 0501234567 --yes   # skip confirmation

Reads from env (or .env):
    INTERNAL_API_SECRET   required — the API's X-Internal-Secret value
    BOT_API_URL           default target when --base-url is omitted
"""

import argparse
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

PROD_URL = "https://asi-chat-bot-api-488842772722.europe-west1.run.app"


def normalize_phone(raw: str) -> str | None:
    """Canonicalize an Israeli mobile to `05XXXXXXXX`, or None if invalid.

    Same rule as the server side: history rows are keyed by the local form, so
    a `+972…` input must collapse to the identical key.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    digits = re.sub(r"^(?:\+?972)", "0", digits)
    return digits if re.fullmatch(r"05\d{8}", digits) else None


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phone", help="phone number, local or international form")
    parser.add_argument(
        "--base-url",
        default=os.getenv("BOT_API_URL", PROD_URL),
        help=f"bot API base URL (default: $BOT_API_URL or {PROD_URL})",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    phone = normalize_phone(args.phone)
    if not phone:
        print(f"invalid phone number: {args.phone!r} (expected an Israeli mobile)", file=sys.stderr)
        return 1

    secret = os.getenv("INTERNAL_API_SECRET", "")
    if not secret:
        print("INTERNAL_API_SECRET is not set (env or .env)", file=sys.stderr)
        return 1

    base_url = args.base_url.rstrip("/")
    if not args.yes:
        answer = input(f"Delete ALL data for {phone} at {base_url}? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    try:
        resp = requests.post(
            f"{base_url}/user/delete",
            json={"phoneNumber": phone},
            headers={"X-Internal-Secret": secret},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1

    counts = resp.json()
    print(f"deleted for {phone}: {json.dumps(counts)}")
    if not any(counts.values()):
        print("note: nothing was stored for this number (0 rows in every table)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
