"""Streamlit chat UI — thin client of the backend chat API.

Run with:
    uv run streamlit run ui/app.py

All LLM work happens in server.py (FastAPI). This file only renders the
chat surface and talks to the backend via HTTP. The phone-number input
in the sidebar is the per-conversation identity (later this becomes the
real WhatsApp sender; for now you type a test number).

Env:
    CHAT_API_URL          backend base URL (default http://localhost:8000)
    INTERNAL_API_SECRET   shared secret for X-Internal-Secret header
"""

import json
import os
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
    st.session_state.phone = DEFAULT_PHONE
if "history_phone" not in st.session_state:
    st.session_state.history_phone = None
if "history" not in st.session_state:
    st.session_state.history = []
if "stats" not in st.session_state:
    st.session_state.stats = []


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

if user_input := st.chat_input("כתוב הודעה..."):
    phone = st.session_state.phone
    st.session_state.history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        full_text = st.write_stream(stream_assistant_reply(phone, user_input))

    st.session_state.history.append({"role": "assistant", "content": full_text or ""})
    last_usage = st.session_state.get("_last_usage")
    if last_usage:
        st.session_state.stats.append(last_usage)
    st.rerun()


# ---------- sidebar ----------

with st.sidebar:
    st.header("הגדרות")
    new_phone = st.text_input("מספר טלפון", value=st.session_state.phone, key="phone_input")
    if new_phone and new_phone != st.session_state.phone:
        st.session_state.phone = new_phone
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
