"""Knowledge-base editor — staff edit the bot's KB documents via the backend.

Reads and writes go through /kb on the backend; saves carry the revision the
editor loaded (optimistic concurrency), so a concurrent save by someone else
surfaces as a conflict instead of silently overwriting.
"""

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import streamlit as st
from common import API_URL, REQUEST_TIMEOUT, apply_style, headers, require_staff

st.set_page_config(page_title="מאגר ידע", page_icon=":material/menu_book:", layout="wide")
apply_style()
require_staff()

NAME_RE = re.compile(r"_?[a-z0-9][a-z0-9_\-]*\.md")
MAX_NAME_LEN = 64
CONFLICT_MSG = "מישהו אחר שמר את הקובץ בינתיים. טען/י מחדש ונסה/י שוב."
RESTORE_NOTE_RE = re.compile(r"restore #(\d+)")

try:
    _LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
except ZoneInfoNotFoundError:  # slim images may ship without tzdata
    _LOCAL_TZ = timezone.utc


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


def write_to_backend(call, url: str, payload: dict, status, success: str) -> bool:
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
    st.session_state.kb_flash = f"{success} (גרסה {r.json()['revision_id']})"
    st.session_state.kb_conflict = False
    _reload()
    return True


# ---------- formatting ----------

def _when(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def _note(rev: dict) -> str:
    m = RESTORE_NOTE_RE.fullmatch(rev.get("note") or "")
    return f"שוחזר מגרסה {m.group(1)}" if m else (rev.get("note") or "")


def _matches(doc: dict, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    haystack = " ".join([doc["name"], doc.get("title", ""), *doc.get("keywords", [])]).lower()
    return q in haystack


# ---------- session-state ----------

def _reload() -> None:
    """Drop the loaded document so the next run refetches it and resets the form."""
    st.session_state.kb_doc = None
    st.session_state.kb_load_n += 1


def _open(name: str) -> None:
    if name == st.session_state.kb_current:
        return
    st.session_state.kb_current = name
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
        "hidden": name.startswith("_"),
        "latest_revision_id": None,
    }


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
    st.session_state.kb_conflict = False
    _reload()
    if name not in st.session_state.kb_names:
        st.session_state.kb_doc = _blank_doc(name)
    st.session_state.kb_new_name = ""


st.session_state.setdefault("editor_name", "")
st.session_state.setdefault("kb_current", None)
st.session_state.setdefault("kb_doc", None)
st.session_state.setdefault("kb_load_n", 0)
st.session_state.setdefault("kb_conflict", False)

flash = st.session_state.pop("kb_flash", None)
if flash:
    st.toast(flash, icon=":material/check_circle:")


try:
    documents = list_documents()
except requests.RequestException as e:
    st.error(f"לא ניתן לטעון את מאגר הידע מהשרת: {e}")
    st.stop()

st.session_state.kb_names = [d["name"] for d in documents]
if st.session_state.kb_current is None and documents:
    visible = [d for d in documents if not d.get("hidden")]
    st.session_state.kb_current = (visible or documents)[0]["name"]
current = st.session_state.kb_current

index_col, sheet_col = st.columns([1, 3], gap="large")


# ---------- document index ----------

with index_col:
    query = st.text_input(
        "חיפוש",
        key="kb_query",
        placeholder="חיפוש לפי נושא, מילת מפתח או שם קובץ",
        label_visibility="collapsed",
        icon=":material/search:",
    )
    entries = list(documents)
    if current and current not in st.session_state.kb_names:
        entries.insert(0, {"name": current, "title": f"{current} (חדש)", "keywords": []})
    shown = [d for d in entries if _matches(d, query)]
    st.caption(f"{len(shown)} מתוך {len(entries)} מסמכים")

    with st.container(key="kb_list", height=520, border=False, gap=None):
        for d in shown:
            st.button(
                d.get("title") or d["name"],
                key=f"kb_open_{d['name']}",
                help=d["name"] + (" (מוסתר מהבוט)" if d.get("hidden") else ""),
                type="primary" if d["name"] == current else "tertiary",
                icon=":material/visibility_off:" if d.get("hidden") else None,
                width="stretch",
                on_click=_open,
                args=(d["name"],),
            )
        if not shown:
            st.caption("אין מסמכים שמתאימים לחיפוש.")

    with st.expander("מסמך חדש", icon=":material/add:"):
        st.text_input("שם הקובץ", key="kb_new_name", placeholder="my-topic.md")
        st.button("צור", on_click=_on_new_click)
        if st.session_state.get("kb_new_error"):
            st.error(st.session_state.kb_new_error)

if not current:
    with sheet_col:
        st.info("המאגר ריק. צור/י מסמך חדש מהרשימה.")
    st.stop()


# ---------- document sheet ----------

doc = st.session_state.kb_doc
if doc is None or doc["name"] != current:
    try:
        doc = fetch_document(current)
    except requests.RequestException as e:
        with sheet_col:
            st.error(f"לא ניתן לטעון את המסמך: {e}")
        st.stop()
    st.session_state.kb_doc = doc

n = st.session_state.kb_load_n
base_revision_id = doc.get("latest_revision_id")

with sheet_col:
    head_col, name_col = st.columns([3, 1], vertical_alignment="bottom")
    with head_col:
        meta = [f'<span class="kb-file">{html.escape(current)}</span>']
        if doc.get("hidden"):
            meta.append('<span class="kb-hidden">מוסתר מהבוט</span>')
        if base_revision_id is None and current not in st.session_state.kb_names:
            meta.append("טרם נשמר")
        elif doc.get("updated_by") == "seed":
            meta.append("עדיין בגרסה המקורית מהקבצים")
        elif doc.get("updated_by"):
            meta.append(
                f"נשמר לאחרונה על ידי {html.escape(doc['updated_by'])}, "
                f"{html.escape(_when(doc.get('updated_at')))}"
            )
        st.markdown(
            f'<div class="kb-doc-title" role="heading" aria-level="1">'
            f'{html.escape(doc["title"] or current)}</div>'
            f'<p class="kb-meta">{" &nbsp;|&nbsp; ".join(meta)}</p>',
            unsafe_allow_html=True,
        )
    with name_col:
        st.session_state.editor_name = st.text_input(
            "השם שלך",
            value=st.session_state.editor_name,
            key="editor_name_input",
            help="נרשם בהיסטוריית הגרסאות. חובה כדי לשמור.",
        )
    editor_name = st.session_state.editor_name.strip()

    with st.form("kb_editor", border=False):
        title_col, summary_col = st.columns(2)
        title = title_col.text_input("כותרת", value=doc["title"], key=f"kb_title_{n}")
        summary = summary_col.text_input(
            "תקציר",
            value=doc["summary"],
            key=f"kb_summary_{n}",
            help="לשימוש הצוות. הבוט לא רואה את התקציר.",
        )
        keywords_raw = st.text_input(
            "מילות מפתח",
            value=", ".join(doc["keywords"]),
            key=f"kb_keywords_{n}",
            help="מופרדות בפסיקים. הבוט בוחר איזה מסמך לקרוא לפי הכותרת ומילות המפתח.",
        )
        body = st.text_area("תוכן", value=doc["body"], height=520, key=f"kb_body_{n}")
        toggle_col, save_col = st.columns([3, 1], vertical_alignment="center")
        hidden = toggle_col.toggle(
            "מוסתר מהבוט",
            value=doc["hidden"],
            key=f"kb_hidden_{n}",
            help="מסמך מוסתר נשמר במאגר, אבל הבוט לא רואה אותו ולא יכול לקרוא אותו.",
        )
        submitted = save_col.form_submit_button(
            "שמור שינויים",
            type="primary",
            icon=":material/save:",
            disabled=not editor_name,
            width="stretch",
        )

    if not editor_name:
        st.caption("כדי לשמור, כתבו את השם שלכם בשדה \"השם שלך\" למעלה.")

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
            "השינויים נשמרו",
        ):
            st.rerun()

    # ---------- history ----------

    with st.expander("היסטוריית גרסאות", icon=":material/history:"):
        if base_revision_id is None:
            st.caption("למסמך הזה עוד אין גרסאות שמורות.")
        else:
            try:
                revisions = fetch_revisions(current)
            except requests.RequestException as e:
                st.error(f"לא ניתן לטעון היסטוריה: {e}")
                revisions = []
            for rev in revisions:
                when_col, who_col, what_col, action_col = st.columns(
                    [2, 2, 4, 2], vertical_alignment="center"
                )
                when_col.write(_when(rev["saved_at"]))
                who_col.write(rev["saved_by"] or "")
                what_col.write(_note(rev) or rev["title"])
                if rev["id"] == base_revision_id:
                    action_col.caption("הגרסה הנוכחית")
                elif action_col.button(
                    "שחזר",
                    key=f"kb_restore_{rev['id']}",
                    icon=":material/undo:",
                    disabled=not editor_name,
                    width="stretch",
                ):
                    if write_to_backend(
                        requests.post,
                        _url(current, "/restore"),
                        {
                            "revision_id": rev["id"],
                            "saved_by": editor_name,
                            "base_revision_id": base_revision_id,
                        },
                        status,
                        "הגרסה שוחזרה",
                    ):
                        st.rerun()

    if st.session_state.kb_conflict:
        with status:
            st.error(CONFLICT_MSG)
            st.button("טען מחדש", icon=":material/refresh:", on_click=_on_reload_click)
