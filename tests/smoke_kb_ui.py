"""Smoke test: Streamlit knowledge-base editor page (ui/kb_editor.py).

Run with:
    uv run python tests/smoke_kb_ui.py

Drives the page with streamlit.testing's AppTest and a faked backend
(requests.get/put/post patched) — no server, no network.
"""
import os
import pathlib
import sys
from unittest import mock

# Env is read by ui/common.py at import. Localhost backend exempts the
# password gate; the empty UI_PASSWORD also stops a local .env from enabling it.
os.environ["CHAT_API_URL"] = "http://localhost:8000"
os.environ["INTERNAL_API_SECRET"] = "smoke-internal"
os.environ["UI_PASSWORD"] = ""

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

from streamlit.testing.v1 import AppTest  # noqa: E402

PAGE = str(ROOT / "ui" / "kb_editor.py")

DOCS = [
    {"name": "pricing.md", "title": "מחירים", "keywords": ["מחיר"], "summary": "s",
     "hidden": False, "updated_at": "t", "updated_by": "x", "latest_revision_id": 7},
    {"name": "_draft.md", "title": "טיוטה", "keywords": [], "summary": "",
     "hidden": True, "updated_at": "t", "updated_by": "x", "latest_revision_id": 3},
]
FULL = {**DOCS[0], "body": "גוף המסמך"}
REVISIONS = [
    {"id": 7, "saved_at": "2026-10-01", "saved_by": "dana", "note": "", "title": "מחירים"},
    {"id": 5, "saved_at": "2026-09-01", "saved_by": "avi", "note": "old", "title": "מחירים ישן"},
]


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.ok = status < 400
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_get(url, **kw):
    assert kw["headers"]["X-Internal-Secret"] == "smoke-internal"
    if url.endswith("/revisions"):
        return Resp(200, {"revisions": REVISIONS})
    if url.endswith("/kb"):
        return Resp(200, {"version": 1, "documents": DOCS})
    return Resp(200, FULL)


def run(put=None, post=None):
    """Return a started AppTest with the backend faked."""
    put = put or mock.Mock(return_value=Resp(200, {"revision_id": 8, "version": 2}))
    post = post or mock.Mock(return_value=Resp(200, {"revision_id": 9, "version": 3}))
    patches = (
        mock.patch("requests.get", side_effect=fake_get),
        mock.patch("requests.put", put),
        mock.patch("requests.post", post),
    )
    return patches, put, post


def start():
    at = AppTest.from_file(PAGE, default_timeout=30)
    return at.run()


def name_input(at):
    return at.text_input(key="editor_name_input")


def doc_buttons(at):
    return [b for b in at.button if (b.key or "").startswith("kb_open_")]


def submit_button(at):
    return next(b for b in at.button if b.label == "שמור שינויים")


# 1. index lists every document; the open one is primary, hidden ones say so
patches, put, post = run()
with patches[0], patches[1], patches[2]:
    at = start()
    assert not at.exception, at.exception
    docs = doc_buttons(at)
    assert [b.label for b in docs] == ["מחירים", "טיוטה"], [b.label for b in docs]
    assert [b.proto.type for b in docs] == ["primary", "tertiary"], [b.proto.type for b in docs]
    assert "מוסתר מהבוט" in docs[1].help, docs[1].help
    assert any(c.value == "הגרסה הנוכחית" for c in at.caption), [c.value for c in at.caption]

    # search narrows the index by keyword
    at.text_input(key="kb_query").set_value("מחיר").run()
    assert [b.label for b in doc_buttons(at)] == ["מחירים"], [b.label for b in doc_buttons(at)]
    at.text_input(key="kb_query").set_value("").run()

    # 2. saving is disabled while the editor name is empty
    assert submit_button(at).disabled

    # 3. save sends base_revision_id and split/stripped keywords
    name_input(at).set_value("דנה").run()
    assert not submit_button(at).disabled
    kw = next(t for t in at.text_input if t.label.startswith("מילות מפתח"))
    kw.set_value("  מחיר , תשלום,, ").run()
    submit_button(at).click().run()
    assert not at.exception, at.exception
    assert put.call_count == 1
    sent = put.call_args.kwargs["json"]
    assert sent["base_revision_id"] == 7, sent
    assert sent["keywords"] == ["מחיר", "תשלום"], sent
    assert sent["saved_by"] == "דנה", sent
    assert put.call_args.args[0].endswith("/kb/pricing.md")
    assert any("8" in t.value for t in at.toast), [t.value for t in at.toast]

# 4. 409 shows the conflict text and a reload button
conflict = mock.Mock(return_value=Resp(409, {"detail": "conflict", "current_revision_id": 9}))
patches, put, post = run(put=conflict)
with patches[0], patches[1], patches[2]:
    at = start()
    name_input(at).set_value("דנה").run()
    submit_button(at).click().run()
    assert any(e.value == "מישהו אחר שמר את הקובץ בינתיים. טען/י מחדש ונסה/י שוב." for e in at.error), [e.value for e in at.error]
    assert any(b.label == "טען מחדש" for b in at.button)

# 5. list-shaped 422 is readable
bad = mock.Mock(return_value=Resp(422, {"detail": [{"loc": ["body", "title"], "msg": "String should have at least 1 character"}]}))
patches, put, post = run(put=bad)
with patches[0], patches[1], patches[2]:
    at = start()
    name_input(at).set_value("דנה").run()
    submit_button(at).click().run()
    texts = [e.value for e in at.error]
    assert any("title: String should have at least 1 character" in t for t in texts), texts

# 6. string-shaped 422 is readable
bad = mock.Mock(return_value=Resp(422, {"detail": "invalid document name"}))
patches, put, post = run(put=bad)
with patches[0], patches[1], patches[2]:
    at = start()
    name_input(at).set_value("דנה").run()
    submit_button(at).click().run()
    assert any("invalid document name" in e.value for e in at.error), [e.value for e in at.error]

# 7. restore posts with the current latest_revision_id as base
patches, put, post = run()
with patches[0], patches[1], patches[2]:
    at = start()
    name_input(at).set_value("דנה").run()
    next(b for b in at.button if b.key == "kb_restore_5").click().run()
    assert not at.exception, at.exception
    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/kb/pricing.md/restore")
    assert post.call_args.kwargs["json"] == {
        "revision_id": 5, "saved_by": "דנה", "base_revision_id": 7,
    }, post.call_args.kwargs["json"]

# 8. new-file name is validated client-side
patches, put, post = run()
with patches[0], patches[1], patches[2]:
    at = start()
    at.text_input(key="kb_new_name").set_value("Bad Name").run()
    next(b for b in at.button if b.label == "צור").click().run()
    assert any("שם קובץ לא תקין" in e.value for e in at.error), [e.value for e in at.error]
    at.text_input(key="kb_new_name").set_value("new-topic").run()
    next(b for b in at.button if b.label == "צור").click().run()
    assert not at.exception, at.exception
    assert any("new-topic.md" in m.value for m in at.markdown), [m.value for m in at.markdown]
    assert doc_buttons(at)[0].label == "new-topic.md (חדש)", [b.label for b in doc_buttons(at)]
    # a never-saved document starts with no base revision
    name_input(at).set_value("דנה").run()
    submit_button(at).click().run()
    assert put.call_args.kwargs["json"]["base_revision_id"] is None
    assert put.call_args.args[0].endswith("/kb/new-topic.md")
    assert put.call_args.kwargs["json"]["hidden"] is False

# 9. a new '_' file starts hidden from the bot
patches, put, post = run()
with patches[0], patches[1], patches[2]:
    at = start()
    at.text_input(key="kb_new_name").set_value("_notes").run()
    next(b for b in at.button if b.label == "צור").click().run()
    name_input(at).set_value("דנה").run()
    submit_button(at).click().run()
    assert put.call_args.kwargs["json"]["hidden"] is True, put.call_args.kwargs["json"]

print("OK")
