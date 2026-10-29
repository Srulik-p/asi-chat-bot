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
