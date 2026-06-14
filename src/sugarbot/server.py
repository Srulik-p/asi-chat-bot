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

POST /auth/callback
    Headers: X-Webhook-Secret: <AUTH_CALLBACK_SECRET>
    Body:    { phoneNumber, user:{id,nickname,isPremium,labels:[{id,name}]}, accessToken }
    Effect:  upsert users row by phoneNumber, then push a "connected" message
             to the user via the outbound sender (best-effort, in background).

GET  /healthz
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
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
LOGIN_URL_BASE = os.getenv("LOGIN_URL_BASE", "https://qa.sugardaddy.cy/sign-in")


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
        + "אני רואה שהתחברת בהצלחה 🙂 עכשיו אני יכולה לראות את הסטטוס שלך "
        + "ולעזור לך עם כל מה שקשור לחשבון. במה אפשר לעזור?"
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
    }
]

TOOLS_ALL = TOOLS + ACCOUNT_TOOLS


def _account_status_for(phone: str) -> str:
    """Resolve get_account_status for `phone`. Returns a JSON string (tool result)."""
    user = db.get_user_by_phone(phone)
    if user is None:
        login_url = f"{LOGIN_URL_BASE}?phoneNumber={quote(phone, safe='')}"
        return json.dumps(
            {
                "logged_in": False,
                "login_url": login_url,
                "instructions": (
                    "הלקוח לא מחובר. בקש/י ממנו להתחבר דרך login_url כדי שתוכל/י "
                    "לראות את הסטטוס שלו, ושלח/י לו את הקישור כפי שהוא."
                ),
            },
            ensure_ascii=False,
        )
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


# ---------- health ----------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
