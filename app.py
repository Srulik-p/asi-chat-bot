"""Streamlit chat UI for the Sugar Daddy customer-service assistant.

Run with:
    uv run streamlit run app.py
"""

import streamlit as st

from assistant import MODEL, SYSTEM_PROMPT, client


st.set_page_config(
    page_title="שוגר דדי — שירות לקוחות",
    page_icon="💬",
    layout="centered",
)

st.markdown(
    """
    <style>
    .stApp { direction: rtl; }
    [data-testid="stChatMessageContent"] { direction: rtl; text-align: right; }
    [data-testid="stChatInput"] textarea { direction: rtl; text-align: right; }
    [data-testid="stSidebar"] { direction: rtl; text-align: right; }
    [data-testid="stHeader"] { direction: ltr; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("שירות לקוחות שוגר דדי")
st.caption(f"מודל: {MODEL}")

if "history" not in st.session_state:
    st.session_state.history = []
if "stats" not in st.session_state:
    st.session_state.stats = []


def stream_reply(messages):
    """Yield delta chunks; stash final usage in session_state at end."""
    with client.chat.completions.stream(
        model=MODEL,
        messages=messages,
        stream_options={"include_usage": True},
    ) as s:
        for event in s:
            if event.type == "content.delta":
                yield event.delta
        final = s.get_final_completion()

    usage = getattr(final, "usage", None)
    if usage is None:
        st.session_state._last_usage = None
        return
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    st.session_state._last_usage = {
        "prompt": usage.prompt_tokens,
        "cached": cached,
        "completion": usage.completion_tokens,
        "total": usage.total_tokens,
    }


for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if user_input := st.chat_input("כתוב הודעה..."):
    st.session_state.history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *st.session_state.history,
    ]

    with st.chat_message("assistant"):
        full_text = st.write_stream(stream_reply(messages))

    st.session_state.history.append({"role": "assistant", "content": full_text})

    last_usage = st.session_state.get("_last_usage")
    if last_usage:
        st.session_state.stats.append(last_usage)

    st.rerun()

with st.sidebar:
    st.header("סטטיסטיקות")

    if st.button("🔄 שיחה חדשה", use_container_width=True):
        st.session_state.history = []
        st.session_state.stats = []
        st.session_state.pop("_last_usage", None)
        st.rerun()

    st.divider()

    if st.session_state.stats:
        last = st.session_state.stats[-1]
        cache_pct = (last["cached"] / last["prompt"] * 100) if last["prompt"] else 0

        st.subheader("תור אחרון")
        col1, col2 = st.columns(2)
        col1.metric("Prompt", last["prompt"])
        col2.metric("Cached", f"{last['cached']} ({cache_pct:.0f}%)")
        col1.metric("Completion", last["completion"])
        col2.metric("Total", last["total"])

        st.divider()
        st.subheader("מצטבר")
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
