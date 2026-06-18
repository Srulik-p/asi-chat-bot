"""Streamlit chat UI — thin client of the backend chat API.

Run with:
    uv run streamlit run ui/app.py

All LLM work happens in server.py (FastAPI). This file only renders the
chat surface and talks to the backend via HTTP. An up-front phone gate
captures the per-conversation identity before the chat opens (later this
becomes the real WhatsApp sender; for now you type a test number). The
sidebar lets you switch numbers, which starts a fresh conversation.

Env:
    CHAT_API_URL          backend base URL (default http://localhost:8000)
    INTERNAL_API_SECRET   shared secret for X-Internal-Secret header
"""

import json
import os
import re
import sys

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("CHAT_API_URL", "http://localhost:8000").rstrip("/")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")
DEFAULT_PHONE = os.getenv("DEFAULT_TEST_PHONE", "0500000000")
REQUEST_TIMEOUT = 120

st.set_page_config(
    page_title="שוגר דדי - שירות לקוחות",
    page_icon="💬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    /* RTL base */
    .stApp { direction: rtl; }
    [data-testid="stChatMessageContent"] { direction: rtl; text-align: right; }
    [data-testid="stChatInput"] textarea { direction: rtl; text-align: right; }
    [data-testid="stHeader"] { direction: ltr; }

    /* Sidebar — fixed overlay anchored to the right. Without position:fixed
       the right:0 anchor is ignored and Streamlit's collapse leaves a
       squeezed strip on the right edge. */
    [data-testid="stSidebar"] {
        direction: rtl;
        text-align: right;
        position: fixed;
        top: 0;
        right: 0;
        left: auto;
        height: 100vh;
        width: 320px;
        max-width: min(85vw, 340px);
        background-color: var(--background-color, #0e1117);
        box-shadow: -2px 0 10px rgba(0, 0, 0, 0.3);
        z-index: 999;
        transition: transform 0.3s ease;
    }
    [data-testid="stSidebar"][aria-expanded="false"] { transform: translateX(100%); }
    [data-testid="stSidebar"][aria-expanded="true"]  { transform: translateX(0); }
    section[data-testid="stMain"] { padding-right: 0; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("שירות לקוחות שוגר דדי")
st.caption(f"backend: {API_URL}")


# ---------- phone ----------

def normalize_phone(raw: str) -> str | None:
    """Canonicalize an Israeli mobile to `05XXXXXXXX`, or None if invalid.

    Accepts local (`050-123 4567`) and international (`+972`, `972`) forms so
    the same person always keys to the same conversation history.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    digits = re.sub(r"^(?:\+?972)", "0", digits)
    return digits if re.fullmatch(r"05\d{8}", digits) else None


# ---------- backend client ----------

def _headers() -> dict:
    return {"X-Internal-Secret": INTERNAL_SECRET, "Content-Type": "application/json"}


def fetch_history(phone: str) -> list[dict]:
    r = requests.get(
        f"{API_URL}/chat/history",
        params={"phoneNumber": phone},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("messages", [])


def reset_history(phone: str) -> None:
    requests.post(
        f"{API_URL}/chat/reset",
        json={"phoneNumber": phone},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    ).raise_for_status()


def stream_assistant_reply(phone: str, message: str):
    """Generator yielding plain-text deltas; stashes final usage in session_state."""
    st.session_state._last_usage = None
    try:
        with requests.post(
            f"{API_URL}/chat/message",
            json={"phoneNumber": phone, "message": message},
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
            stream=True,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "delta":
                    yield ev.get("text", "")
                elif t == "done":
                    st.session_state._last_usage = ev.get("usage")
                    return
                elif t == "error":
                    print(f"[chat] backend error: {ev.get('message')}", file=sys.stderr)
                    yield "מצטערים, אירעה תקלה. אנא נסה/י שוב או בקש/י לעבור לנציג."
                    return
    except requests.RequestException as e:
        print(f"[chat] transport error: {e}", file=sys.stderr)
        yield "מצטערים, אירעה תקלה. אנא נסה/י שוב או בקש/י לעבור לנציג."


# ---------- session-state ----------

if "phone" not in st.session_state:
    st.session_state.phone = None
if "phone_confirmed" not in st.session_state:
    st.session_state.phone_confirmed = False
if "history_phone" not in st.session_state:
    st.session_state.history_phone = None
if "history" not in st.session_state:
    st.session_state.history = []
if "stats" not in st.session_state:
    st.session_state.stats = []


# ---------- phone gate ----------
# Capture the conversation identity before anything else. Mimics WhatsApp
# knowing the sender's number; the chat surface below never renders until a
# valid phone is confirmed.
if not st.session_state.phone_confirmed:
    st.write(
        "מצב בדיקה: הזן/י מספר טלפון לזיהוי השיחה. "
        "בוואטסאפ הזיהוי אוטומטי לפי מספר השולח - כאן מקלידים אותו ידנית לבדיקה."
    )
    with st.form("phone_gate"):
        raw_phone = st.text_input("מספר טלפון", value=DEFAULT_PHONE)
        if st.form_submit_button("התחל שיחה", use_container_width=True):
            normalized = normalize_phone(raw_phone)
            if normalized is None:
                st.error("מספר טלפון לא תקין — הזן/י מספר נייד ישראלי, למשל 0501234567")
            else:
                st.session_state.phone = normalized
                st.session_state.phone_confirmed = True
                st.rerun()
    st.stop()


# Refresh history from the backend when the phone changes (or first load).
if st.session_state.history_phone != st.session_state.phone:
    try:
        st.session_state.history = fetch_history(st.session_state.phone)
        st.session_state.history_phone = st.session_state.phone
    except requests.RequestException as e:
        st.error(f"לא ניתן לטעון היסטוריה מהשרת: {e}")
        st.session_state.history = []


# ---------- main chat surface ----------

for msg in st.session_state.history:
    if msg.get("role") not in ("user", "assistant"):
        continue  # hide tool/system rows from the UI
    if not msg.get("content"):
        continue  # skip tool-call-only assistant turns
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

chat_value = st.chat_input(
    "כתוב הודעה...",
    accept_file=True,
    file_type=["png", "jpg", "jpeg"],
)
if chat_value:
    phone = st.session_state.phone
    # With accept_file the value is a ChatInputValue (.text + .files); guard for
    # the plain-string form too.
    text = (getattr(chat_value, "text", None) or "").strip() or (
        chat_value if isinstance(chat_value, str) else ""
    )
    files = list(getattr(chat_value, "files", []) or [])

    with st.chat_message("user"):
        if text:
            st.markdown(text)
        for f in files:
            st.image(f)

    # The backend chat API currently accepts text only. We flag attachments so
    # the screenshot-based flows the bot asks for aren't silently dropped.
    # Real image handling (store + show to a human rep / vision) is a follow-up.
    if files:
        note = "[המשתמש צירף תמונה/צילום מסך]"
        outgoing = f"{text}\n{note}" if text else note
    else:
        outgoing = text

    st.session_state.history.append({"role": "user", "content": text or "📎 תמונה"})

    with st.chat_message("assistant"):
        full_text = st.write_stream(stream_assistant_reply(phone, outgoing))

    st.session_state.history.append({"role": "assistant", "content": full_text or ""})
    last_usage = st.session_state.get("_last_usage")
    if last_usage:
        st.session_state.stats.append(last_usage)
    st.rerun()


# ---------- sidebar ----------

with st.sidebar:
    st.header("הגדרות")
    st.caption("מספר טלפון")
    st.write(f"**{st.session_state.phone}**")
    if st.button("📱 החלף מספר", use_container_width=True):
        st.session_state.phone = None
        st.session_state.phone_confirmed = False
        st.session_state.history = []
        st.session_state.stats = []
        st.session_state.pop("_last_usage", None)
        st.rerun()

    if st.button("🔄 שיחה חדשה", use_container_width=True):
        try:
            reset_history(st.session_state.phone)
        except requests.RequestException as e:
            st.error(f"כשל באיפוס: {e}")
        st.session_state.history = []
        st.session_state.stats = []
        st.session_state.pop("_last_usage", None)
        st.rerun()

    st.divider()
    st.subheader("סטטיסטיקות")

    if st.session_state.stats:
        last = st.session_state.stats[-1]
        cache_pct = (last["cached"] / last["prompt"] * 100) if last["prompt"] else 0

        st.caption("תור אחרון")
        col1, col2 = st.columns(2)
        col1.metric("Prompt", last["prompt"])
        col2.metric("Cached", f"{last['cached']} ({cache_pct:.0f}%)")
        col1.metric("Completion", last["completion"])
        col2.metric("Total", last["total"])

        st.divider()
        st.caption("מצטבר")
        total_prompt = sum(s["prompt"] for s in st.session_state.stats)
        total_cached = sum(s["cached"] for s in st.session_state.stats)
        total_completion = sum(s["completion"] for s in st.session_state.stats)
        agg_pct = (total_cached / total_prompt * 100) if total_prompt else 0

        st.write(f"**Prompt:** {total_prompt:,}")
        st.write(f"**Cached:** {total_cached:,} ({agg_pct:.0f}%)")
        st.write(f"**Completion:** {total_completion:,}")
        st.write(f"**תורות:** {len(st.session_state.stats)}")
    else:
        st.caption("אין נתונים עדיין — שלח הודעה כדי להתחיל.")
