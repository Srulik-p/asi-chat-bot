"""HTTP server for the auth callback (and future webhooks).

Run with:
    uv run uvicorn server:app --reload --port 8000

Streamlit (app.py) is the chat UI; this is the backend the external login
flow calls into. They deploy as separate services.

Endpoint:
    POST /auth/callback
        Headers: X-Webhook-Secret: <shared secret, env AUTH_CALLBACK_SECRET>
        Body:    { phoneNumber, user: { id, firstName, lastName }, accessToken }
        Effect:  upsert into the users table keyed by phoneNumber.
        Returns: 204 No Content on success.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

import db

load_dotenv()

WEBHOOK_SECRET = os.getenv("AUTH_CALLBACK_SECRET", "")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Sugar Daddy assistant — backend", lifespan=lifespan)


class AuthUser(BaseModel):
    id: str
    firstName: str
    lastName: str


class AuthCallback(BaseModel):
    phoneNumber: str
    user: AuthUser
    accessToken: str


def _check_secret(provided: str | None) -> None:
    # Refuse if the server has no secret configured — better to 503 than
    # to silently accept anonymous writes to the users table.
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="server misconfigured: AUTH_CALLBACK_SECRET is not set",
        )
    if not provided or not hmac.compare_digest(provided, WEBHOOK_SECRET):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret")


@app.post("/auth/callback", status_code=status.HTTP_204_NO_CONTENT)
def auth_callback(
    payload: AuthCallback,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> None:
    _check_secret(x_webhook_secret)
    db.upsert_user(
        phone_number=payload.phoneNumber,
        external_id=payload.user.id,
        first_name=payload.user.firstName,
        last_name=payload.user.lastName,
        access_token=payload.accessToken,
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
