"""Shared config and staff gate for the Streamlit UI (chat page + KB editor)."""

import hmac
import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("CHAT_API_URL", "http://localhost:8000").rstrip("/")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")
UI_PASSWORD = os.getenv("UI_PASSWORD", "")
REQUEST_TIMEOUT = 120


def headers() -> dict:
    return {"X-Internal-Secret": INTERNAL_SECRET, "Content-Type": "application/json"}


# Colours and fonts live in .streamlit/config.toml; this adds RTL and the few
# shapes the theme can't express.
_STYLE = """
<style>
:root {
    --kb-ink: #1A2233;
    --kb-graphite: #5E6676;
    --kb-rule: #D9DDE4;
    --kb-berry: #8C1D4E;
    --kb-amber: #A86A12;
    --kb-hover: #ECEEF2;
}
.stApp { direction: rtl; }
[data-testid="stSidebar"] { direction: rtl; text-align: right; }
textarea, input { direction: rtl; text-align: right; }
[data-testid="stHeader"], [data-testid="stToolbar"] { direction: rtl; }
[data-testid="stMainBlockContainer"] { padding-top: 4.5rem; }

/* Document index: a flat list with a berry edge on the open document. */
.st-key-kb_list button {
    border: 0;
    border-right: 3px solid transparent;
    border-radius: 0;
    min-height: 2.4rem;
    padding: 0.4rem 0.75rem;
    justify-content: flex-start;
    background: transparent;
    color: var(--kb-ink);
}
.st-key-kb_list button > div { justify-content: flex-start; width: 100%; }
.st-key-kb_list button p { text-align: right; font-weight: 400; }
.st-key-kb_list button:hover { background: var(--kb-hover); color: var(--kb-ink); }
.st-key-kb_list button[data-testid="stBaseButton-primary"] {
    background: #FFFFFF;
    border-right-color: var(--kb-berry);
}
.st-key-kb_list button[data-testid="stBaseButton-primary"] p { font-weight: 600; }
.st-key-kb_list button:focus-visible { outline: 2px solid var(--kb-berry); outline-offset: -2px; }
.st-key-kb_list [data-testid="stIconMaterial"] { color: var(--kb-amber); }

/* The open document's title is the one display moment on the page. */
.kb-doc-title {
    font-family: "Frank Ruhl Libre", serif;
    font-weight: 700;
    font-size: 2.1rem;
    line-height: 1.2;
    color: var(--kb-ink);
    margin: 0;
    padding: 0;
}
.kb-meta { color: var(--kb-graphite); font-size: 0.9rem; margin: 0.35rem 0 0; }
.kb-file { direction: ltr; unicode-bidi: isolate; font-weight: 500; color: var(--kb-ink); }
.kb-hidden { color: var(--kb-amber); font-weight: 600; }
[class*="st-key-kb_body_"] textarea { font-size: 1rem; line-height: 1.75; }
[data-testid="stCheckbox"] label { gap: 0.6rem; }
[data-testid="stCheckbox"] [data-testid="stTooltipIcon"] { margin-inline-start: 0.4rem; }
</style>
"""


def apply_style() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)


# This UI holds INTERNAL_API_SECRET and can read/impersonate/purge any
# customer's conversation by phone number. Local dev against a localhost
# backend is exempt; any other backend requires UI_PASSWORD.

_LOCAL_BACKEND = API_URL.startswith(("http://localhost", "http://127.0.0.1"))


def require_staff() -> None:
    """Stop the script run unless the staff password gate has been passed."""
    if UI_PASSWORD:
        if not st.session_state.get("ui_authed"):
            with st.form("staff_gate"):
                supplied = st.text_input("סיסמת צוות", type="password")
                if st.form_submit_button("כניסה", use_container_width=True):
                    if hmac.compare_digest(supplied, UI_PASSWORD):
                        st.session_state.ui_authed = True
                        st.rerun()
                    else:
                        st.error("סיסמה שגויה")
            st.stop()
    elif not _LOCAL_BACKEND:
        st.error(
            "UI_PASSWORD אינו מוגדר. ממשק זה חושף נתוני לקוחות (קריאה, התחזות ומחיקה "
            "לפי מספר טלפון) ולכן חסום עד שתוגדר סיסמת צוות בסביבה."
        )
        st.stop()
