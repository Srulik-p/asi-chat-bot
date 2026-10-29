"""Streamlit staff UI entrypoint — routes between the KB editor and the chat demo.

Run with:
    uv run streamlit run ui/app.py

Env:
    CHAT_API_URL          backend base URL (default http://localhost:8000)
    INTERNAL_API_SECRET   shared secret for X-Internal-Secret header
    UI_PASSWORD           staff password gating the whole UI. REQUIRED whenever
                          the backend is not localhost — this surface can read,
                          impersonate, and purge ANY customer's conversation by
                          phone number, so it must never be reachable unauthenticated.
"""

import streamlit as st
from common import apply_style, require_staff

st.set_page_config(page_title="מאגר ידע", page_icon=":material/menu_book:", layout="wide")
apply_style()

# Gate before routing so no page (not even its title) renders unauthenticated.
require_staff()

st.navigation(
    [
        st.Page("kb_editor.py", title="מאגר ידע", icon=":material/menu_book:", default=True),
        st.Page("chat_demo.py", title="דמו צ'אט", icon=":material/forum:", url_path="chat"),
    ],
    position="top",
).run()
