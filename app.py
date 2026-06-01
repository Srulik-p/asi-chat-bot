"""Streamlit chat UI for the Sugar Daddy customer-service assistant.

Run with:
    uv run streamlit run app.py
"""

import sys

import streamlit as st

from assistant import (
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_PROMPT,
    TOOLS,
    _usage_dict,
    add_usage,
    client,
    empty_usage,
    resolve_tool_calls,
)


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

    /* Sidebar - anchor to the right side of the screen for RTL */
    [data-testid="stSidebar"] {
        direction: rtl;
        text-align: right;
        right: 0;
        left: auto;
    }
    /* When the sidebar slides out (Streamlit transforms it off-screen),
       push it to the right edge instead of the left */
    [data-testid="stSidebar"][aria-expanded="false"] {
        transform: translateX(100%);
        margin-left: 0;
    }

    /* Mobile: full-height opaque overlay drawer from the right */
    @media (max-width: 768px) {
        [data-testid="stSidebar"] {
            position: fixed;
            top: 0;
            right: 0;
            left: auto;
            height: 100vh;
            width: 85vw;
            max-width: 340px;
            background-color: var(--background-color, #0e1117);
            box-shadow: -2px 0 10px rgba(0, 0, 0, 0.4);
            z-index: 999;
        }
        /* Ensure main content fills the screen instead of leaving sidebar gutter */
        section[data-testid="stMain"] { padding-right: 0; }
    }
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
    """Yield delta chunks of the final answer; stash summed usage at end.

    The model may call read_kb to pull topic files before answering, so this
    loops over rounds. Deltas stream live for UX; the actual text saved to
    history is the final non-tool round's `msg.content` (kept separately in
    `_last_final` to prevent any pre-tool prelude text from polluting history).
    Token usage is summed across every round.
    """
    total = empty_usage()
    st.session_state._last_final = ""
    try:
        for i in range(MAX_TOOL_ROUNDS):
            kwargs: dict = {"model": MODEL, "messages": messages, "tools": TOOLS}
            if i == MAX_TOOL_ROUNDS - 1:
                # Final allowed round — forbid tools so the model MUST answer.
                kwargs["tool_choice"] = "none"

            with client.chat.completions.stream(
                **kwargs, stream_options={"include_usage": True}
            ) as s:
                for event in s:
                    if event.type == "content.delta":
                        yield event.delta
                final = s.get_final_completion()

            add_usage(total, _usage_dict(getattr(final, "usage", None)))

            msg = final.choices[0].message
            if msg.tool_calls:
                resolve_tool_calls(messages, msg)
                continue

            st.session_state._last_final = msg.content or ""
            st.session_state._last_usage = total
            return
    except Exception as e:
        # Surface a polite Hebrew apology, keep history consistent. The
        # exception itself is dropped to stderr by Streamlit's logger.
        print(f"[stream_reply] {type(e).__name__}: {e}", file=sys.stderr)
        msg = "מצטערים, אירעה תקלה. אנא נסה/י שוב או בקש/י לעבור לנציג."
        st.session_state._last_final = msg
        st.session_state._last_usage = total
        yield msg


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

    # Use the final-round msg.content (not the concatenated stream) so any
    # pre-tool prelude the model may have emitted doesn't pollute history.
    final_text = st.session_state.pop("_last_final", None) or full_text or ""
    st.session_state.history.append({"role": "assistant", "content": final_text})

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
