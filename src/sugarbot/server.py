"""HTTP backend: chat processing + auth callback.

Run with:
    uv run uvicorn sugarbot.server:app --reload --port 8000

Endpoints
---------
POST /chat/message
    Headers: X-Internal-Secret: <INTERNAL_API_SECRET>
    Body:    { phoneNumber, message }
    Returns: streaming NDJSON
               {"type":"delta","text":"..."}          (zero or more; final-answer
                                                       text only — tool-round
                                                       preamble is never streamed)
               {"type":"done","usage":{...}}          (exactly one, at end)
               {"type":"error","message":"...","ref":"..."}  (instead of done on
                                                       failure; message is a fixed
                                                       customer-safe Hebrew string,
                                                       ref correlates to the log line)
    Persists the user turn, runs the OpenAI tool-call loop (including read_kb),
    persists every assistant/tool turn, and streams the final reply. Turns are
    serialized per phone number.

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
    Body:    { phoneNumber, user:{id,nickname,isPremium,gender?,labels:[{id,name}]}, accessToken }
    Effect:  upsert users row by phoneNumber, then push a "connected" message
             to the user via the outbound sender (best-effort, in background).

POST /maintenance/sweep-idle
    Headers: X-Internal-Secret
    Effect:  one inactivity-sweep pass — warns conversations quiet for
             INACTIVITY_WARN_HOURS, closes them at INACTIVITY_CLOSE_HOURS.
    Returns: {"scanned": n, "warned": n, "closed": n, "truncated": bool}
    Wire a scheduler (e.g. Cloud Scheduler) to call this hourly.

GET  /healthz
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, AsyncIterator, Iterator
from urllib.parse import quote

import anyio.to_thread

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from sugarbot import contact, db, notifier
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
    redact_pii,
    repair_tool_calls,
    scrub_messages,
)

load_dotenv()

WEBHOOK_SECRET = os.getenv("AUTH_CALLBACK_SECRET", "")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")
# True on Cloud Run (K_SERVICE is injected by the platform). Used to fail fast
# on misconfiguration that local dev tolerates.
_ON_CLOUD_RUN = bool(os.getenv("K_SERVICE"))
# Base of the external sign-in URL; the user's phone is appended as a query arg.
# The QA default is for LOCAL DEV ONLY — lifespan refuses to start on Cloud Run
# without an explicit value, so a prod deploy can never silently hand customers
# QA login links (logging in on QA never fires the prod /auth/callback, which
# dead-loops the whole identification flow).
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
# Hard wall-clock deadline for one sweep pass. Cloud Run requests default to a
# 300s timeout; without this, a slow outbound channel (limit * send timeout =
# ~17 min worst case) gets the sweep killed mid-pass every run.
INACTIVITY_SWEEP_DEADLINE_SECONDS = int(os.getenv("INACTIVITY_SWEEP_DEADLINE_SECONDS", "240"))
# Model-facing history window. The full log stays in the DB for human reps; the
# model only replays the most recent slice, so token cost stays bounded and a
# months-old thread can never grow past the context window into a permanent
# context_length_exceeded failure. repair_tool_calls heals any tool pair the
# slice boundary cuts.
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "80"))
# Size of the anyio threadpool that drives sync endpoints + the streaming chat
# generator. The default (40) means ~40 concurrent conversations exhaust the
# pool and /auth/callback starts queueing behind chats.
SERVER_THREADPOOL_SIZE = int(os.getenv("SERVER_THREADPOOL_SIZE", "120"))

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
    if _ON_CLOUD_RUN and not os.getenv("LOGIN_URL_BASE"):
        raise RuntimeError(
            "LOGIN_URL_BASE must be set explicitly on Cloud Run — the built-in "
            "default points at the QA site and would send customers QA login links."
        )
    anyio.to_thread.current_default_thread_limiter().total_tokens = SERVER_THREADPOOL_SIZE
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


# ---------- per-conversation serialization ----------
# Every writer of a conversation's message log (a chat turn, the auth-callback
# "connected" push, the idle sweep) takes this lock, so rows can never
# interleave inside a tool round. An interleaved history — assistant(tool_calls)
# / stray row / tool — is rejected by the OpenAI API on every replay, bricking
# the conversation. In-process lock: correct for a single instance; run the
# backend with max-instances=1 (or move to a Postgres advisory lock before
# scaling out).

_PHONE_LOCKS: dict[str, threading.Lock] = {}
_PHONE_LOCKS_GUARD = threading.Lock()


def _phone_lock(phone: str) -> threading.Lock:
    with _PHONE_LOCKS_GUARD:
        lock = _PHONE_LOCKS.get(phone)
        if lock is None:
            lock = _PHONE_LOCKS[phone] = threading.Lock()
        return lock


# ---------- /auth/callback ----------

class UserLabel(BaseModel):
    id: str
    name: str


class AuthUser(BaseModel):
    id: str
    nickname: str
    isPremium: bool
    # Registration gender, sent by the site so the bot can tailor answers
    # (women have free access — never "you're not premium"; "I only see men"
    # is expected for a woman; correct gendered Hebrew). Optional so a callback
    # from a site version that doesn't send it yet still succeeds (bot falls
    # back to gender-neutral). Expected values: "male" / "female" (the site may
    # also send Hebrew "גבר"/"אישה"); the bot interprets flexibly.
    gender: str | None = None
    labels: list[UserLabel]


class AuthCallback(BaseModel):
    phoneNumber: str
    user: AuthUser
    accessToken: str


def _connected_message(nickname: str | None) -> str:
    """The 'I see you connected' greeting pushed right after a successful login.

    Continuation wording, not a fresh greeting: the common case is a customer
    who was mid-issue, got sent the login link, and came back — "במה אפשר
    לעזור?" would reset the conversation and force them to repeat themselves.
    """
    hi = f"היי {nickname}, " if nickname else "היי, "
    return (
        hi
        + "אנחנו רואים שהתחברת בהצלחה 🙂 עכשיו אפשר לראות את הסטטוס שלך - "
        + "ואפשר להמשיך מאיפה שעצרנו."
    )


def _notify_connected(phone: str, message: str) -> None:
    """Push the connected greeting and, on success, record it in the chat history.

    Takes the per-phone lock so the greeting row can never land in the middle
    of an in-flight chat turn's tool round (which would poison the replay)."""
    with _phone_lock(phone):
        if notifier.send_message(phone, message):
            db.append_message(phone, "assistant", content=message)


@app.post("/auth/callback", status_code=status.HTTP_204_NO_CONTENT)
def auth_callback(
    payload: AuthCallback,
    background_tasks: BackgroundTasks,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> None:
    _check_secret(x_webhook_secret, WEBHOOK_SECRET, "AUTH_CALLBACK_SECRET")
    # payload.accessToken is accepted (site contract) but deliberately NOT
    # persisted — a plaintext live session token per customer is breach
    # liability and nothing reads it. See db.users_table / migration 0005.
    db.upsert_user(
        phone_number=payload.phoneNumber,
        external_id=payload.user.id,
        nickname=payload.user.nickname,
        is_premium=payload.user.isPremium,
        gender=payload.user.gender,
        labels=[l.model_dump() for l in payload.user.labels],
    )
    # Push "I see you connected" out-of-band so the callback returns 204 fast and
    # the (best-effort) send can't block or fail the auth flow.
    background_tasks.add_task(
        _notify_connected, payload.phoneNumber, _connected_message(payload.user.nickname)
    )


# ---------- /chat ----------

class MediaItem(BaseModel):
    """An inbound attachment (image or PDF) forwarded from WhatsApp.

    `data_url` is either a base64 data URL (`data:<mime>;base64,<...>`) or, for
    images, an https URL the model can fetch. PDFs must be base64 data URLs.
    """
    type: str  # "image" | "pdf"
    data_url: str
    filename: str | None = None  # used to label PDFs


class ChatMessageIn(BaseModel):
    phoneNumber: str
    message: str
    # Attachments the customer sent with this message. The model reads them on
    # THIS turn (vision); history keeps only a light text placeholder so we
    # don't resend bytes/tokens on every later turn.
    media: list[MediaItem] = []


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
                "status?', 'why can't I send messages?'), AND whenever you need a "
                "login link to send the customer for ANY purpose — status checks, "
                "identification for blocked/suspended accounts, or reports. The "
                "login_url field this tool returns is the ONLY valid login link; "
                "never invent one. Returns logged_in plus, if logged in, the "
                "membership details (nickname, is_premium, gender, labels); if not "
                "logged in, a login_url to send the customer so they can sign in. "
                "It does NOT return subscription dates — never invent an end/renewal "
                "date. Use gender to tailor the answer (women have free access — "
                "never tell a woman she is not premium). Takes no arguments — the "
                "customer is identified by the conversation."
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

# ---------- support-ticket tool ----------
# Files a ticket into the site's help-desk system (the same backend the public
# "contact us" form posts to), so the bot can open a ticket on the customer's
# behalf. Defined here, like the account tools, because the customer's phone is
# resolved from the conversation identity (as sourcePhoneNumber) and never
# passed by the model. The model supplies only the reason and a summary.

CONTACT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_support_ticket",
            "description": (
                "Open a support ticket in the site's help desk on the customer's "
                "behalf — the same queue the human team works from. This is how you "
                "hand the conversation to a human: call it WHENEVER the customer asks "
                "for a human representative, OR the situation needs one (refund, "
                "billing dispute, blocked-account appeal, serious report, unresolved "
                "technical issue, emotional distress, manual actions). Use "
                "reason='help_desk' for a plain 'I want to talk to a rep' request, "
                "otherwise the closest category. Pair it with escalate_to_human "
                "(which marks the conversation awaiting a rep). Do NOT use it for "
                "questions you can answer yourself from the knowledge base, and for "
                "irreversible actions (e.g. account removal) only after the customer "
                "has explicitly confirmed. Provide `text` (a clear Hebrew summary of "
                "what the customer needs, with any detail the team needs); include "
                "`email` only if the customer gave one. The customer's phone is "
                "attached automatically from the conversation — never ask for it or "
                "pass it. After a successful submit, tell the customer a human "
                "representative will get back to them during working hours."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": contact.REASON_CHOICES,
                        "description": (
                            "Ticket category: login_email (can't sign in with their "
                            "email), help_desk (talk to a rep), forgot_password, "
                            "technical (a bug/error on the site), remove_account "
                            "(delete their account), report (report a user/abuse), "
                            "other."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "Hebrew summary of the customer's issue for the team.",
                    },
                    "email": {
                        "type": "string",
                        "description": "Customer email, only if they provided one. Optional.",
                    },
                },
                "required": ["reason", "text"],
                "additionalProperties": False,
            },
        },
    },
]

TOOLS_ALL = TOOLS + ACCOUNT_TOOLS + CONTACT_TOOLS


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
    # Known exception to "the phone never reaches the model": the site's sign-in
    # contract requires the phone as a query arg, so it appears inside login_url.
    # The system prompt forbids the model from quoting or reasoning about it.
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
            # May be None if the site hasn't sent it yet — bot falls back to
            # gender-neutral phrasing when absent.
            "gender": user.get("gender"),
            "labels": user["labels"],
        },
        ensure_ascii=False,
    )


def _media_content_parts(media: list["MediaItem"]) -> list[dict]:
    """Turn inbound attachments into OpenAI multimodal content parts."""
    parts: list[dict] = []
    for item in media:
        if item.type == "image":
            parts.append({"type": "image_url", "image_url": {"url": item.data_url}})
        elif item.type == "pdf":
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": item.filename or "document.pdf",
                        "file_data": item.data_url,
                    },
                }
            )
    return parts


def _media_placeholder(media: list["MediaItem"]) -> str:
    """Short Hebrew note appended to the stored user turn so later turns keep
    context about the attachment without carrying the bytes."""
    labels = {"image": "תמונה", "pdf": "קובץ PDF"}
    return "".join(f"\n[המשתמש צירף {labels.get(m.type, 'קובץ')}]" for m in media)


def _run_chat(
    phone: str, user_message: str, media: list["MediaItem"] | None = None
) -> Iterator[dict]:
    """Generator that drives the OpenAI tool-call loop and yields NDJSON events.

    All conversation state lives in the DB. We persist every turn (user,
    assistant tool-calls, tool results, final assistant reply) so the
    next call can rebuild history from scratch. Inbound images/PDFs are sent to
    the model on THIS turn only; the stored history keeps a light text
    placeholder instead of the bytes.

    Holds the per-phone lock for the whole turn: a WhatsApp double-send (or the
    auth-callback push) must not interleave rows into this turn's tool round —
    the second message waits and then sees the first turn's full history.
    """
    with _phone_lock(phone):
        yield from _run_chat_locked(phone, user_message, media)


def _run_chat_locked(
    phone: str, user_message: str, media: list["MediaItem"] | None = None
) -> Iterator[dict]:
    media = media or []
    # Store the raw text plus a placeholder note for any attachment. The bytes
    # themselves are attached to the live request below, not persisted.
    db.append_message(phone, "user", content=user_message + _media_placeholder(media))

    # Build the LLM-facing messages list. scrub_messages redacts emails/phones/IDs
    # from user-role content only; the stored DB rows keep the raw values for
    # human-rep visibility.
    history = db.load_history(phone)
    # Replay only the most recent window to the model (full log stays in the DB).
    # A slice can cut an assistant tool_calls turn off from its tool results;
    # repair_tool_calls below drops the dangling half cleanly.
    if len(history) > HISTORY_MAX_MESSAGES:
        history = history[-HISTORY_MAX_MESSAGES:]
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    messages = scrub_messages(messages)
    # Attach this turn's media to the last (current) user message as multimodal
    # content, so the model actually sees the screenshot/receipt. Past turns keep
    # only their text placeholder (see _media_placeholder) to save tokens.
    if media and messages and messages[-1].get("role") == "user":
        messages[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": redact_pii(user_message)},
                *_media_content_parts(media),
            ],
        }
    # Heal any dangling tool-call pairs left by an earlier interrupted turn so a
    # single poisoned turn can't 400 the conversation forever.
    messages = repair_tool_calls(messages)

    total = empty_usage()
    # Safety-net for Remark 12 ("login link often not sent"): whenever
    # get_account_status resolves to a not-logged-in status with a login_url, we
    # remember it and guarantee the literal URL ends up in the reply even if the
    # model forgets to include it.
    pending_login_url: str | None = None

    try:
        for i in range(MAX_TOOL_ROUNDS):
            kwargs: dict = {"model": MODEL, "messages": messages, "tools": TOOLS_ALL}
            if i == MAX_TOOL_ROUNDS - 1:
                kwargs["tool_choice"] = "none"

            # Buffer this round's content deltas instead of yielding them live:
            # a round that ends in tool_calls may still stream preamble text
            # ("רק בודק את הסטטוס..."), and forwarding it would merge it into
            # the customer-visible reply alongside the NEXT round's real answer.
            # Only the final (no-tool-calls) round's text is flushed downstream.
            buffered: list[str] = []
            with client.chat.completions.stream(
                **kwargs, stream_options={"include_usage": True}
            ) as s:
                for event in s:
                    if event.type == "content.delta":
                        buffered.append(event.delta)
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
                    # Every tool_call MUST get a tool-result row, even on error —
                    # otherwise the assistant tool_calls turn is orphaned and the
                    # whole conversation 400s on every future replay.
                    try:
                        if name == "get_account_status":
                            # Resolved from the conversation identity + DB, not the model args.
                            result = _account_status_for(phone)
                            try:
                                parsed = json.loads(result)
                                pending_login_url = (
                                    parsed.get("login_url")
                                    if not parsed.get("logged_in")
                                    else None
                                )
                            except (json.JSONDecodeError, AttributeError):
                                pass
                        elif name == "escalate_to_human":
                            db.mark_escalated(phone, datetime.now(timezone.utc))
                            result = json.dumps({"escalated": True}, ensure_ascii=False)
                        elif name == "submit_support_ticket":
                            # phone comes from the conversation identity (kept out
                            # of the model), attached as sourcePhoneNumber.
                            args = json.loads(tc.function.arguments or "{}")
                            res = contact.submit_ticket(
                                reason=args.get("reason", ""),
                                text=args.get("text", ""),
                                source_email=args.get("email") or None,
                                source_phone=phone,
                            )
                            if res["ok"]:
                                result = json.dumps(
                                    {
                                        "submitted": True,
                                        "instructions": (
                                            "הפנייה נפתחה במערכת התמיכה. עדכן/י את הלקוח "
                                            "בקצרה שהפנייה נשלחה ושנציג אנושי יחזור אליו "
                                            "בשעות הפעילות (א'-ה' 9:00-17:00). אל תבטיח/י "
                                            "זמן מדויק מעבר לזה."
                                        ),
                                    },
                                    ensure_ascii=False,
                                )
                            else:
                                result = json.dumps(
                                    {
                                        "submitted": False,
                                        "instructions": (
                                            "פתיחת הפנייה נכשלה. אל תגיד/י ללקוח שנפתחה "
                                            "פנייה. הצע/י לנסות שוב בעוד רגע, ואם דחוף - "
                                            "העבר/י לנציג (escalate_to_human)."
                                        ),
                                    },
                                    ensure_ascii=False,
                                )
                        else:
                            fn = TOOL_FNS.get(name)
                            if fn is None:
                                result = f"כלי לא ידוע: {name}"
                            else:
                                args = json.loads(tc.function.arguments or "{}")
                                result = fn(**args)
                    except Exception as e:
                        print(
                            f"[chat] tool {name} failed for {phone}: {type(e).__name__}: {e}",
                            file=sys.stderr,
                        )
                        if name == "get_account_status":
                            # "Continue without this info" would invite a guessed
                            # account status / invented login link — forbid both.
                            result = (
                                "שגיאה זמנית בבדיקת סטטוס החשבון. אל תנחש/י סטטוס, אל תמציא/י "
                                "פרטי חשבון ואל תמציא/י קישור התחברות. אמור/אמרי ללקוח שיש תקלה "
                                "זמנית בבדיקת החשבון והצע/י לנסות שוב בעוד כמה דקות; אם העניין "
                                "דחוף - העבר/י לנציג (escalate_to_human)."
                            )
                        else:
                            result = f"שגיאה זמנית בהפעלת הכלי {name}. המשך/י לעזור ללקוח בלי המידע הזה."
                    db.append_message(phone, "tool", tool_call_id=tc.id, content=result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue

            # Final round — no tool calls. Flush the buffered answer downstream.
            for chunk in buffered:
                yield {"type": "delta", "text": chunk}
            text = msg.content or ""
            # Deterministic login-link fallback (Remark 12): if the account status
            # returned a login_url this turn, the model is talking about logging in,
            # but the literal URL is missing from the reply — append it so the
            # customer always gets a clickable link. Gated on login wording so a
            # deliberate diagnostic question (e.g. the "לא מצליח להתחבר" flow, where
            # the KB forbids auto-sending a link) is never force-fed a URL.
            if pending_login_url and pending_login_url not in text:
                mentions_login = any(
                    k in text for k in ("התחבר", "תחבר", "קישור", "לינק", "sign-in")
                )
                if mentions_login:
                    tail = ("\n\n" if text else "") + pending_login_url
                    yield {"type": "delta", "text": tail}
                    text += tail
                else:
                    print(
                        f"[chat] login_url withheld for {phone}: reply does not mention login",
                        file=sys.stderr,
                    )
            db.append_message(phone, "assistant", content=text)
            yield {"type": "done", "usage": total}
            return
    except Exception as e:
        # Log the full failure server-side (Cloud Run captures stderr) with a
        # correlation id, and stream only a fixed customer-safe message: SDK
        # exception text can carry request/org ids and payload fragments, and
        # the WhatsApp integration may forward `message` verbatim.
        ref = uuid.uuid4().hex[:12]
        print(
            f"[chat] _run_chat failed for {phone} ref={ref}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        traceback.print_exc()
        yield {"type": "error", "message": "אירעה תקלה זמנית. אפשר לנסות שוב בעוד רגע.", "ref": ref}


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
        _ndjson(_run_chat(payload.phoneNumber, payload.message, payload.media)),
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

    # Wall-clock deadline so a slow outbound channel can't push one pass beyond
    # the platform request timeout; whatever is left drains on the next run.
    deadline = time.monotonic() + INACTIVITY_SWEEP_DEADLINE_SECONDS
    warned = closed = 0
    truncated = False
    for c in convos:
        if time.monotonic() > deadline:
            truncated = True
            break
        phone = c["phone_number"]
        # A conversation only reaches here after 24h+ of silence, so a held lock
        # means it just became active again — skip it rather than block the sweep.
        lock = _phone_lock(phone)
        if not lock.acquire(blocking=False):
            continue
        try:
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
        finally:
            lock.release()
    return {"scanned": len(convos), "warned": warned, "closed": closed, "truncated": truncated}


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
async def healthz() -> dict:
    # async so health checks never queue behind the sync threadpool under load.
    return {"ok": True}
