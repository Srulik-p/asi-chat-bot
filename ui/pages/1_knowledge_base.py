"""Knowledge-base editor — staff edit the bot's KB documents via the backend.

Reads and writes go through /kb on the backend; saves carry the revision the
editor loaded (optimistic concurrency), so a concurrent save by someone else
surfaces as a conflict instead of silently overwriting.
"""

import re

import requests
import streamlit as st
from common import API_URL, REQUEST_TIMEOUT, headers, require_staff

st.set_page_config(page_title="מאגר ידע", page_icon="📚", layout="wide")

st.markdown(
    """
    <style>
    .stApp { direction: rtl; }
    [data-testid="stHeader"] { direction: ltr; }
    [data-testid="stSidebar"] { direction: rtl; text-align: right; }
    textarea, input { direction: rtl; text-align: right; }
    </style>
    """,
    unsafe_allow_html=True,
)

require_staff()

st.title("מאגר ידע")

NAME_RE = re.compile(r"_?[a-z0-9][a-z0-9_\-]*\.md")
MAX_NAME_LEN = 64
CONFLICT_MSG = "מישהו אחר שמר את הקובץ בינתיים. טען/י מחדש ונסה/י שוב."


# ---------- backend client ----------

def _url(name: str, suffix: str = "") -> str:
    return f"{API_URL}/kb/{name}{suffix}"


def list_documents() -> list[dict]:
    r = requests.get(f"{API_URL}/kb", headers=headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("documents", [])


def fetch_document(name: str) -> dict:
    r = requests.get(_url(name), headers=headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_revisions(name: str) -> list[dict]:
    r = requests.get(
        _url(name, "/revisions"),
        params={"limit": 20},
        headers=headers(),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("revisions", [])


def _validation_text(detail) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            loc = ".".join(str(p) for p in item.get("loc", []) if p != "body")
            msg = item.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(p for p in parts if p)
    return str(detail)


def _error_text(r: requests.Response) -> str:
    if r.status_code == 401:
        return "אין הרשאה לשרת (INTERNAL_API_SECRET שגוי)."
    if r.status_code == 404:
        return "הקובץ לא נמצא בשרת."
    if r.status_code == 422:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        return f"הקובץ לא נשמר: {_validation_text(detail)}"
    return f"השמירה נכשלה ({r.status_code}): {r.text[:200]}"


def write_to_backend(call, url: str, payload: dict, status) -> bool:
    """PUT/POST to the backend; on success queue a toast and force a reload."""
    try:
        r = call(url, json=payload, headers=headers(), timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        status.error(f"שגיאת תקשורת עם השרת: {e}")
        return False
    if r.status_code == 409:
        st.session_state.kb_conflict = True
        return False
    if not r.ok:
        status.error(_error_text(r))
        return False
    st.session_state.kb_flash = f"נשמר בהצלחה (גרסה {r.json()['revision_id']})"
    st.session_state.kb_conflict = False
    _reload()
    return True


# ---------- session-state ----------

def _reload() -> None:
    """Drop the loaded document so the next run refetches it and resets the form."""
    st.session_state.kb_doc = None
    st.session_state.kb_load_n += 1


def _on_pick() -> None:
    st.session_state.kb_current = st.session_state.kb_pick
    st.session_state.kb_conflict = False
    _reload()


def _on_reload_click() -> None:
    st.session_state.kb_conflict = False
    _reload()


def _blank_doc(name: str) -> dict:
    return {
        "name": name,
        "title": "",
        "keywords": [],
        "summary": "",
        "body": "",
        "hidden": False,
        "latest_revision_id": None,
    }


st.session_state.setdefault("editor_name", "")
st.session_state.setdefault("kb_current", None)
st.session_state.setdefault("kb_doc", None)
st.session_state.setdefault("kb_load_n", 0)
st.session_state.setdefault("kb_conflict", False)

st.session_state.editor_name = st.sidebar.text_input(
    "השם שלך",
    value=st.session_state.editor_name,
    key="editor_name_input",
    help="נרשם בהיסטוריית הגרסאות. חובה כדי לשמור.",
)
editor_name = st.session_state.editor_name.strip()

flash = st.session_state.pop("kb_flash", None)
if flash:
    st.toast(flash)


# ---------- picker ----------

try:
    documents = list_documents()
except requests.RequestException as e:
    st.error(f"לא ניתן לטעון את מאגר הידע מהשרת: {e}")
    st.stop()

st.session_state.kb_names = [d["name"] for d in documents]
labels = {
    d["name"]: f"{d['title']}  ({d['name']})" + (" [מוסתר]" if d.get("hidden") else "")
    for d in documents
}
if st.session_state.kb_current is None and documents:
    st.session_state.kb_current = documents[0]["name"]
current = st.session_state.kb_current
if current and current not in labels:
    labels[current] = f"{current} (חדש)"  # created in this session, not yet saved

if labels:
    names = list(labels)
    st.session_state.kb_pick = current
    st.selectbox(
        "מסמך",
        names,
        format_func=labels.__getitem__,
        key="kb_pick",
        on_change=_on_pick,
    )


def _on_new_click() -> None:
    raw = st.session_state.kb_new_name.strip()
    name = raw if raw.endswith(".md") else raw + ".md"
    if len(name) > MAX_NAME_LEN or not NAME_RE.fullmatch(name):
        st.session_state.kb_new_error = (
            "שם קובץ לא תקין: אותיות אנגליות קטנות, ספרות, '-' ו-'_' בלבד, "
            f"מתחיל באות או ספרה (או ב-'_'), עד {MAX_NAME_LEN} תווים, בסיומת .md"
        )
        return
    st.session_state.kb_new_error = None
    st.session_state.kb_current = name
    st.session_state.kb_pick = name
    st.session_state.kb_conflict = False
    _reload()
    if name not in st.session_state.kb_names:
        st.session_state.kb_doc = _blank_doc(name)
    st.session_state.kb_new_name = ""


with st.expander("קובץ חדש"):
    st.text_input("שם הקובץ", key="kb_new_name", placeholder="my-topic.md")
    st.button("צור", on_click=_on_new_click)
    if st.session_state.get("kb_new_error"):
        st.error(st.session_state.kb_new_error)

current = st.session_state.kb_current
if not current:
    st.info("אין מסמכים במאגר. צור/י קובץ חדש.")
    st.stop()


# ---------- editor ----------

doc = st.session_state.kb_doc
if doc is None or doc["name"] != current:
    try:
        doc = fetch_document(current)
    except requests.RequestException as e:
        st.error(f"לא ניתן לטעון את המסמך: {e}")
        st.stop()
    st.session_state.kb_doc = doc

n = st.session_state.kb_load_n
base_revision_id = doc.get("latest_revision_id")

st.subheader(current)
with st.form("kb_editor"):
    title = st.text_input("כותרת", value=doc["title"], key=f"kb_title_{n}")
    keywords_raw = st.text_input(
        "מילות מפתח (מופרדות בפסיקים)",
        value=", ".join(doc["keywords"]),
        key=f"kb_keywords_{n}",
    )
    summary = st.text_input("תקציר", value=doc["summary"], key=f"kb_summary_{n}")
    body = st.text_area("תוכן", value=doc["body"], height=500, key=f"kb_body_{n}")
    hidden = st.checkbox("מוסתר", value=doc["hidden"], key=f"kb_hidden_{n}")
    submitted = st.form_submit_button("שמור", disabled=not editor_name)

if not editor_name:
    st.caption("יש להזין את השם שלך בסרגל הצד כדי לשמור.")

status = st.container()

if submitted:
    if write_to_backend(
        requests.put,
        _url(current),
        {
            "title": title,
            "keywords": [k.strip() for k in keywords_raw.split(",") if k.strip()],
            "summary": summary,
            "body": body,
            "hidden": hidden,
            "saved_by": editor_name,
            "base_revision_id": base_revision_id,
        },
        status,
    ):
        st.rerun()


# ---------- history ----------

with st.expander("היסטוריית גרסאות"):
    if base_revision_id is None:
        st.caption("הקובץ עדיין לא נשמר - אין היסטוריה.")
    else:
        try:
            revisions = fetch_revisions(current)
        except requests.RequestException as e:
            st.error(f"לא ניתן לטעון היסטוריה: {e}")
            revisions = []
        for rev in revisions:
            cols = st.columns([3, 2, 3, 3, 1])
            cols[0].write(rev["saved_at"])
            cols[1].write(rev["saved_by"])
            cols[2].write(rev.get("note") or "")
            cols[3].write(rev["title"])
            if cols[4].button("שחזר", key=f"kb_restore_{rev['id']}", disabled=not editor_name):
                if write_to_backend(
                    requests.post,
                    _url(current, "/restore"),
                    {
                        "revision_id": rev["id"],
                        "saved_by": editor_name,
                        "base_revision_id": base_revision_id,
                    },
                    status,
                ):
                    st.rerun()

if st.session_state.kb_conflict:
    with status:
        st.error(CONFLICT_MSG)
        st.button("טען מחדש", on_click=_on_reload_click)
