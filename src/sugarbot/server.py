"""HTTP backend: chat processing + auth callback.

Run with:
    uv run uvicorn sugarbot.server:app --reload --port 8000

Endpoints
---------
POST /chat/message
    Headers: X-Internal-Secret: <INTERNAL_API_SECRET>
    Body:    { phoneNumber, message }
    Returns: streaming NDJSON
               {"type":"delta","text":"..."}    (zero or more, only on final round)
               {"type":"done","usage":{...}}    (exactly one, at end)
               {"type":"error","message":"..."}  (instead of done on failure)
    Persists the user turn, runs the OpenAI tool-call loop (including read_kb),
    persists every assistant/tool turn, and streams the final reply.

GET  /chat/history?phoneNumber=...
    Headers: X-Internal-Secret
    Returns: {"messages":[{role,content,...}, ...]}

POST /chat/reset
    Headers: X-Internal-Secret
    Body:    { phoneNumber }
    Returns: {"deleted": n}

POST /user/delete
    Headers: X-Internal-Secret
    Body:    { phoneNumber }
    Effect:  erase ALL data for the phone — chat history, cached login row,
             and conversation state (full "forget me" / account purge).
    Returns: {"messages": n, "user": n, "conversation_state": n}

POST /auth/callback
    Headers: X-Webhook-Secret: <AUTH_CALLBACK_SECRET>
    Body:    { phoneNumber, user:{id,nickname,isPremium,labels:[{id,name}]}, accessToken }
    Effect:  upsert users row by phoneNumber, then push a "connected" message
             to the user via the outbound sender (best-effort, in background).

POST /maintenance/sweep-idle
    Headers: X-Internal-Secret
    Effect:  one inactivity-sweep pass — warns conversations quiet for
             INACTIVITY_WARN_HOURS, closes them at INACTIVITY_CLOSE_HOURS.
    Returns: {"scanned": n, "warned": n, "closed": n}
    Wire a scheduler (e.g. Cloud Scheduler) to call this hourly.

GET  /healthz
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, AsyncIterator, Iterator
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from sugarbot import db, notifier
from sugarbot.assistant import (
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_PROMPT,
    TOOL_FNS,
    TOOLS,
    _usage_dict,
    add_usage,
    client,
    empty_usage,
    scrub_messages,
)

load_dotenv()

WEBHOOK_SECRET = os.getenv("AUTH_CALLBACK_SECRET", "")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")
# Base of the external sign-in URL; the user's phone is appended as a query arg.
LOGIN_URL_BASE = os.getenv("LOGIN_URL_BASE", "https://qa.sugardaddy.co.il/sign-in")
# How long cached login data stays valid. Past this we ask the user to log in
# again so we never act on stale account status/labels.
ACCOUNT_FRESHNESS_HOURS = int(os.getenv("ACCOUNT_FRESHNESS_HOURS", "72"))
# Inactivity auto-close: warn after the conversation has been waiting on the
# customer for INACTIVITY_WARN_HOURS, then close it INACTIVITY_CLOSE_HOURS after
# the last activity if still no reply. Driven by /maintenance/sweep-idle.
INACTIVITY_WARN_HOURS = int(os.getenv("INACTIVITY_WARN_HOURS", "24"))
INACTIVITY_CLOSE_HOURS = int(os.getenv("INACTIVITY_CLOSE_HOURS", "48"))
# Max conversations processed per sweep call, oldest-idle first. Bounds the
# request's wall-clock so it can't exceed the scheduler/gateway timeout (worst
# case ~= limit * OUTBOUND_SEND_TIMEOUT); the backlog drains over later calls.
INACTIVITY_SWEEP_LIMIT = int(os.getenv("INACTIVITY_SWEEP_LIMIT", "100"))

INACTIVITY_WARN_MESSAGE = (
    "היי, רק רצינו לוודא שאנחנו עדיין כאן בשבילך 🙂 אם לא נשמע ממך נסגור את "
    "הפנייה בקרוב - אפשר פשוט להמשיך לכתוב כדי שנמשיך, או לכתוב 'סגור' אם הסתדר."
)
INACTIVITY_CLOSE_MESSAGE = (
    "סגרנו את הפנייה כרגע כי לא שמענו ממך. תמיד אפשר לכתוב לנו שוב ונשמח לעזור. "
    "המשך יום נעים 🙂"
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Sugar Daddy assistant — backend", lifespan=lifespan)


_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "pwd",
    "authorization",
    "apikey",
    "api_key",
)


def _redact(obj):
    """Recursively replace values of known-sensitive keys with a sentinel.

    Preserves structure and non-sensitive values so logs stay diagnostic.
    Long strings are truncated to keep log volume bounded.
    """
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if any(f in k.lower() for f in _SENSITIVE_KEY_FRAGMENTS) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + f"…<truncated, {len(obj)} chars>"
    return obj


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: Request, exc: RequestValidationError):
    # /auth/callback carries accessToken; /chat/message carries free-text user
    # input. The payload is logged with values of known-sensitive keys masked
    # (token/secret/password/authorization/apiKey) so we can diagnose contract
    # mismatches without writing raw secrets to Cloud Logging.
    safe_errors = [
        {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    try:
        parsed = json.loads(await request.body())
        payload_repr = _redact(parsed)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload_repr = "<unparseable>"
    print(
        f"[422] {request.method} {request.url.path} payload={payload_repr} errors={safe_errors}",
        file=sys.stderr,
    )
    return JSONResponse(status_code=422, content={"detail": safe_errors})


# ---------- shared auth helpers ----------

def _check_secret(provided: str | None, expected: str, name: str) -> None:
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"server misconfigured: {name} is not set",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=f"invalid {name}")


# ---------- /auth/callback ----------

class UserLabel(BaseModel):
    id: str
    name: str


class AuthUser(BaseModel):
    id: str
    nickname: str
    isPremium: bool
    labels: list[UserLabel]


class AuthCallback(BaseModel):
    phoneNumber: str
    user: AuthUser
    accessToken: str


def _connected_message(nickname: str | None) -> str:
    """The 'I see you connected' greeting pushed right after a successful login."""
    hi = f"היי {nickname}, " if nickname else "היי, "
    return (
        hi
        + "אנחנו רואים שהתחברת בהצלחה 🙂 עכשיו אפשר לראות את הסטטוס שלך "
        + "ולעזור עם כל מה שקשור לחשבון. במה אפשר לעזור?"
    )


def _notify_connected(phone: str, message: str) -> None:
    """Push the connected greeting and, on success, record it in the chat history."""
    if notifier.send_message(phone, message):
        db.append_message(phone, "assistant", content=message)


@app.post("/auth/callback", status_code=status.HTTP_204_NO_CONTENT)
def auth_callback(
    payload: AuthCallback,
    background_tasks: BackgroundTasks,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> None:
    _check_secret(x_webhook_secret, WEBHOOK_SECRET, "AUTH_CALLBACK_SECRET")
    db.upsert_user(
        phone_number=payload.phoneNumber,
        external_id=payload.user.id,
        nickname=payload.user.nickname,
        is_premium=payload.user.isPremium,
        labels=[l.model_dump() for l in payload.user.labels],
        access_token=payload.accessToken,
    )
    # Push "I see you connected" out-of-band so the callback returns 204 fast and
    # the (best-effort) send can't block or fail the auth flow.
    background_tasks.add_task(
        _notify_connected, payload.phoneNumber, _connected_message(payload.user.nickname)
    )


# ---------- /chat ----------

class ChatMessageIn(BaseModel):
    phoneNumber: str
    message: str


class ChatResetIn(BaseModel):
    phoneNumber: str


# ---------- account-status tool ----------
# Defined here (not in assistant.py) because it needs the conversation's phone
# number and the DB — the phone is deliberately kept out of the model, so the
# model calls this tool and the backend resolves it from the conversation
# identity. Returns either the membership status (if logged in) or a login URL
# for the bot to send (if not).

ACCOUNT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_account_status",
            "description": (
                "Get the current customer's login and membership status. Call this "
                "for any question that depends on the customer's OWN account state "
                "(e.g. 'do I have a subscription?', 'am I premium?', 'what's my "
                "status?'). Returns logged_in plus, if logged in, the membership "
                "details (nickname, is_premium, labels); if not logged in, a "
                "login_url to send the customer so they can sign in. Takes no "
                "arguments — the customer is identified by the conversation."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Record that you are handing this conversation to a human "
                "representative. Call this WHENEVER you tell the customer you are "
                "forwarding to the team / that a human will get back to them "
                "(refunds, blocked appeals, double charges, serious reports, an "
                "explicit request for a human, etc.) — in addition to your message "
                "to the customer. It marks the inquiry as awaiting a rep so the "
                "system does not auto-close it while a reply is still owed. Takes "
                "no arguments — the conversation is identified automatically."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

TOOLS_ALL = TOOLS + ACCOUNT_TOOLS


def _login_is_stale(user: dict) -> bool:
    """True if the cached login is older than ACCOUNT_FRESHNESS_HOURS (or undatable)."""
    raw = user.get("updated_at")
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return True
    else:
        return True
    if dt.tzinfo is None:  # SQLite hands back naive datetimes; they are UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_hours > ACCOUNT_FRESHNESS_HOURS


def _not_logged_in(phone: str, *, stale: bool) -> str:
    login_url = f"{LOGIN_URL_BASE}?phoneNumber={quote(phone, safe='')}"
    if stale:
        instructions = (
            f"המידע השמור על הלקוח ישן (עברו יותר מ-{ACCOUNT_FRESHNESS_HOURS} שעות "
            "מההתחברות האחרונה). בקש/י ממנו להתחבר שוב כדי לרענן, ושלח/י לו את "
            "הערך של השדה login_url (כתובת ה-URL המלאה) כקישור לחיץ. אל תכתוב/י "
            "את המילה 'login_url' או סוגריים - רק את הכתובת עצמה."
        )
    else:
        instructions = (
            "הלקוח לא מחובר. בקש/י ממנו להתחבר כדי שתוכל/י לראות את הסטטוס שלו, "
            "ושלח/י לו את הערך של השדה login_url (כתובת ה-URL המלאה) כקישור לחיץ. "
            "אל תכתוב/י את המילה 'login_url' או סוגריים - רק את הכתובת עצמה."
        )
    return json.dumps(
        {"logged_in": False, "stale": stale, "login_url": login_url, "instructions": instructions},
        ensure_ascii=False,
    )


def _account_status_for(phone: str) -> str:
    """Resolve get_account_status for `phone`. Returns a JSON string (tool result).

    Login data is cached in the DB on /auth/callback. We serve it only while
    fresh (< ACCOUNT_FRESHNESS_HOURS since last login); a missing or stale row
    routes the bot back to the login step so we never act on stale labels.
    """
    user = db.get_user_by_phone(phone)
    if user is None:
        return _not_logged_in(phone, stale=False)
    if _login_is_stale(user):
        return _not_logged_in(phone, stale=True)
    return json.dumps(
        {
            "logged_in": True,
            "nickname": user["nickname"],
            "is_premium": user["is_premium"],
            "labels": user["labels"],
        },
        ensure_ascii=False,
    )


def _run_chat(phone: str, user_message: str) -> Iterator[dict]:
    """Generator that drives the OpenAI tool-call loop and yields NDJSON events.

    All conversation state lives in the DB. We persist every turn (user,
    assistant tool-calls, tool results, final assistant reply) so the
    next call can rebuild history from scratch.
    """
    db.append_message(phone, "user", content=user_message)

    # Build the LLM-facing messages list. scrub_messages redacts emails/phones/IDs
    # from user-role content only; the stored DB rows keep the raw values for
    # human-rep visibility.
    history = db.load_history(phone)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    messages = scrub_messages(messages)

    total = empty_usage()

    try:
        for i in range(MAX_TOOL_ROUNDS):
            kwargs: dict = {"model": MODEL, "messages": messages, "tools": TOOLS_ALL}
            if i == MAX_TOOL_ROUNDS - 1:
                kwargs["tool_choice"] = "none"

            with client.chat.completions.stream(
                **kwargs, stream_options={"include_usage": True}
            ) as s:
                for event in s:
                    if event.type == "content.delta":
                        yield {"type": "delta", "text": event.delta}
                final = s.get_final_completion()

            add_usage(total, _usage_dict(getattr(final, "usage", None)))
            msg = final.choices[0].message

            if msg.tool_calls:
                tc_payload = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
                db.append_message(phone, "assistant", content=msg.content, tool_calls=tc_payload)
                messages.append({"role": "assistant", "content": msg.content, "tool_calls": tc_payload})

                for tc in msg.tool_calls:
                    name = tc.function.name
                    if name == "get_account_status":
                        # Resolved from the conversation identity + DB, not the model args.
                        result = _account_status_for(phone)
                    elif name == "escalate_to_human":
                        db.mark_escalated(phone, datetime.now(timezone.utc))
                        result = json.dumps({"escalated": True}, ensure_ascii=False)
                    else:
                        fn = TOOL_FNS.get(name)
                        if fn is None:
                            result = f"כלי לא ידוע: {name}"
                        else:
                            try:
                                args = json.loads(tc.function.arguments or "{}")
                                result = fn(**args)
                            except (json.JSONDecodeError, TypeError) as e:
                                result = f"שגיאה בהפעלת הכלי {name}: {e}"
                    db.append_message(phone, "tool", tool_call_id=tc.id, content=result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue

            text = msg.content or ""
            db.append_message(phone, "assistant", content=text)
            yield {"type": "done", "usage": total}
            return
    except Exception as e:
        # Stream a structured error so the client can render a polite message.
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


def _ndjson(events: Iterator[dict]) -> Iterator[bytes]:
    for ev in events:
        yield (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")


@app.post("/chat/message")
def chat_message(
    payload: ChatMessageIn,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> StreamingResponse:
    _check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")
    return StreamingResponse(
        _ndjson(_run_chat(payload.phoneNumber, payload.message)),
        media_type="application/x-ndjson",
    )


@app.get("/chat/history")
def chat_history(
    phoneNumber: Annotated[str, Query(min_length=1)],
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict:
    _check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")
    return {"messages": db.load_history(phoneNumber)}


@app.post("/chat/reset")
def chat_reset(
    payload: ChatResetIn,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict:
    _check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")
    return {"deleted": db.clear_history(payload.phoneNumber)}


@app.post("/user/delete")
def user_delete(
    payload: ChatResetIn,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict:
    """Erase ALL stored data for a phone number — chat history, cached login
    row, and conversation state. Use for a full 'forget me' / account purge
    (unlike /chat/reset, which only clears the chat history)."""
    _check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")
    return db.delete_user_data(payload.phoneNumber)


# ---------- maintenance: inactivity auto-close ----------

def _sweep_idle() -> dict:
    """One pass of the inactivity sweep. Warns quiet conversations, then closes
    them if the warning went unanswered. Idempotent per state transition.

    Semantics: a conversation is "waiting" when its latest message is an
    assistant reply. After INACTIVITY_WARN_HOURS of silence we send a pre-close
    warning (recorded as the new latest message). The warning is then itself
    subject to the idle clock, so once (INACTIVITY_CLOSE_HOURS - WARN) more hours
    pass with no customer reply we close the inquiry. A new customer message
    clears the state (see db.append_message) and reopens the conversation.
    """
    now = datetime.now(timezone.utc)
    warn_delta = timedelta(hours=INACTIVITY_WARN_HOURS)
    close_grace = timedelta(hours=max(INACTIVITY_CLOSE_HOURS - INACTIVITY_WARN_HOURS, 0))
    # Bound the scan to conversations idle at least as long as the sooner of the
    # two transitions could fire.
    min_idle = min(warn_delta, close_grace) if close_grace else warn_delta
    convos = db.idle_assistant_conversations(now - min_idle, limit=INACTIVITY_SWEEP_LIMIT)

    warned = closed = 0
    for c in convos:
        phone = c["phone_number"]
        if c["last_warned_at"] is None:
            if now - c["last_message_at"] >= warn_delta:
                # Only advance the state machine if the warning was actually
                # delivered. Otherwise a no-op/failed outbound channel would
                # close inquiries the customer was never warned about; leaving
                # the state untouched lets the next sweep retry the warning.
                if notifier.send_message(phone, INACTIVITY_WARN_MESSAGE):
                    db.append_message(phone, "assistant", content=INACTIVITY_WARN_MESSAGE)
                    db.mark_warned(phone, now)
                    warned += 1
        else:
            if now - c["last_warned_at"] >= close_grace:
                # Same rule for the close: never mark closed unless the closing
                # message reached the customer.
                if notifier.send_message(phone, INACTIVITY_CLOSE_MESSAGE):
                    db.append_message(phone, "assistant", content=INACTIVITY_CLOSE_MESSAGE)
                    db.mark_closed(phone, now)
                    closed += 1
    return {"scanned": len(convos), "warned": warned, "closed": closed}


@app.post("/maintenance/sweep-idle")
def sweep_idle(
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict:
    """Trigger one inactivity sweep. Wire a scheduler (e.g. Cloud Scheduler) to
    call this hourly. Guarded by the internal secret."""
    _check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")
    return _sweep_idle()


# ---------- health ----------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
