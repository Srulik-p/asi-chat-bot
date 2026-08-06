# Connecting the bot API to WhatsApp

How to move from the Streamlit UI demo to real WhatsApp, using Meta's
**WhatsApp Business Cloud API** in front of the existing bot API. The bot API
itself needs no contract changes — it was designed to sit behind a WhatsApp
integration.

Bot API base URL (Cloud Run, prod):

```
https://asi-chat-bot-api-488842772722.europe-west1.run.app
```

---

## 1. The contract the WhatsApp side must speak

This is everything an integrator needs — whether that's our own webhook
(below) or the site team that already runs a WhatsApp number.

```
POST /chat/message
Headers: X-Internal-Secret: <INTERNAL_API_SECRET>     ← same value as the Cloud Run env var
Body:    {
           "phoneNumber": "05XXXXXXXX",
           "message": "<user text>",
           "media": [ {"type": "image" | "pdf", "data_url": "...", "filename": "..."} ]
         }
Returns: streaming NDJSON, one JSON object per line:
           {"type":"delta","text":"..."}            zero or more
           {"type":"done","usage":{...}}            exactly one, on success
           {"type":"error","message":"...","ref":"..."}  instead of done, on failure
```

Rules:

- **Accumulate all `delta` texts and send them as ONE WhatsApp message.**
  WhatsApp has no streaming; the joined deltas are the full reply.
- **`error.message` is already a customer-safe Hebrew string** — forward it to
  the user as-is. `ref` correlates to the server log line; keep it out of the
  user-facing message.
- **Phone normalization is load-bearing.** WhatsApp delivers the sender as
  `972501234567` (no `+`). Convert to local form `0501234567` exactly like
  `normalize_phone()` in `ui/app.py` (strip non-digits, replace leading
  `+972`/`972` with `0`). A different format keys a *different* conversation
  history and won't match the users row upserted by `/auth/callback`.
- Media: `data_url` is a base64 data URL (`data:<mime>;base64,...`) or, for
  images only, a publicly fetchable https URL. WhatsApp media URLs are **not**
  public (they require the Bearer token), so in practice base64 both types.

---

## 2. Meta setup (~45 min; business verification can add days)

1. Create an app at <https://developers.facebook.com> → type **Business** →
   add the **WhatsApp** product.
2. You immediately get a **free test number** that can message up to 5
   verified recipient numbers — enough to replace the UI demo today.
   Connecting the real business number requires Meta Business verification
   (days, sometimes longer).
3. Note the **Phone number ID** (WhatsApp → API Setup).
4. Create a **permanent token**: Business Settings → System Users → create a
   system user → assign the app with `whatsapp_business_messaging` permission
   → generate token. (The token shown on the API Setup page expires in 24 h —
   don't ship it.)

---

## 3. The webhook bridge

Two new routes, added to the existing FastAPI service in
`src/sugarbot/server.py` (same Cloud Run service, so push-to-main CD covers
deployment; no new infra):

### `GET /whatsapp/webhook` — Meta's one-time verification

Meta calls this when you save the webhook URL. Echo the challenge if the
token matches:

```python
@app.get("/whatsapp/webhook")
def whatsapp_verify(
    mode: Annotated[str, Query(alias="hub.mode")] = "",
    token: Annotated[str, Query(alias="hub.verify_token")] = "",
    challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> PlainTextResponse:
    if mode == "subscribe" and hmac.compare_digest(token, WHATSAPP_VERIFY_TOKEN):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403)
```

### `POST /whatsapp/webhook` — inbound messages

**ACK with 200 immediately and process in a background thread.** Meta retries
webhooks that don't answer within ~10 s, and the OpenAI tool loop regularly
takes longer — a slow handler means duplicate replies.

Per inbound message:

1. Verify the `X-Hub-Signature-256` header (HMAC-SHA256 of the raw body with
   `WHATSAPP_APP_SECRET`). Reject on mismatch.
2. Extract from the payload: `entry[0].changes[0].value.messages[0]` →
   `from` (wa_id), `text.body`, and any `image`/`document` media IDs.
   Ignore status/delivery callbacks (`value.statuses`).
3. Normalize the phone (`972…` → `05…`, see §1).
4. For each media ID: `GET https://graph.facebook.com/v21.0/<media_id>` with
   the Bearer token → returns a short-lived `url` → download **with the same
   Bearer token** → build `{"type": "image"|"pdf", "data_url": "data:<mime>;base64,<...>", "filename": ...}`.
5. Run the chat. In-process, call `_run_chat(phone, text, media)` directly and
   consume the generator (skips the HTTP hop and the internal secret).
   Concatenate `delta` texts; on `error`, use its `message`.
6. Send the reply:

```
POST https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/messages
Authorization: Bearer <WHATSAPP_TOKEN>
Content-Type: application/json

{"messaging_product": "whatsapp", "to": "<wa_id as received>", "text": {"body": "<reply>"}}
```

Also point the outbound sender in `src/sugarbot/notifier.py` at this same
send call, so the `/auth/callback` "connected" push and the idle-sweep
warnings/closures reach WhatsApp too — today they only reach the demo.

---

## 4. Configuration

Add to the Cloud Run service env (GCP project `unichat-user-auth`), and as
placeholders to `.env.example`:

| Var | Value |
| --- | --- |
| `WHATSAPP_TOKEN` | permanent system-user token from §2.4 |
| `WHATSAPP_PHONE_NUMBER_ID` | from the API Setup page |
| `WHATSAPP_VERIFY_TOKEN` | any random string you invent; must match §3 |
| `WHATSAPP_APP_SECRET` | app secret, for webhook signature verification |

Then in the Meta app dashboard (WhatsApp → Configuration):

- Webhook URL: `https://asi-chat-bot-api-488842772722.europe-west1.run.app/whatsapp/webhook`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` value
- Subscribe to the **`messages`** webhook field.

---

## 5. Test checklist

1. Add your personal number as a test recipient (API Setup page) and send the
   test number a WhatsApp message.
2. Confirm a reply arrives as a single message, in Hebrew, warm tone.
3. Confirm `GET /chat/history?phoneNumber=05...` (with the internal secret)
   shows the same conversation — proves phone normalization is right.
4. Send an image and a PDF; confirm the bot acknowledges the attachment.
5. Trigger the login flow; confirm the link arrives over WhatsApp.

---

## Notes & gotchas

- **24-hour window:** users always message the bot first, so replies stay in
  WhatsApp's free-form customer-service window. Template messages are only
  needed if we ever initiate contact after 24 h of silence (relevant for the
  idle-sweep warnings — check timing of `INACTIVITY_WARN_HOURS`).
- **Serialization:** `/chat/message` already serializes turns per phone
  number, so rapid-fire WhatsApp messages are safe.
- **Alternative for a quick demo:** Twilio's WhatsApp sandbox works in
  ~15 min with no Meta verification, but adds a paid dependency — fine for a
  throwaway demo, not the production path.
- **If the site team runs the WhatsApp number** (existing QA integration):
  hand them §1 only; §§2–4 become theirs.
