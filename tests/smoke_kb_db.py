"""Smoke test: knowledge-base tables, seed migration and DB helpers.

Run with:
    uv run python tests/smoke_kb_db.py

Verifies the 0008 migration seeds every markdown file (hidden flag for `_`
files, `---` rules in bodies intact), and the save/conflict/revision helpers.
"""
import os
import pathlib
import tempfile
import uuid

# Unique per run so concurrent executions don't race on a shared SQLite file.
DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_kb_{uuid.uuid4().hex}.db"
)
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"

from sugarbot import db, kb  # noqa: E402

db.init_db()

docs = db.load_kb_documents()
files = [p for p in kb.KB_DIR.glob("*.md")]
assert len(docs) == len(files), (len(docs), len(files))
assert docs["_internal_notes.md"]["hidden"] is True
assert docs["pricing.md"]["hidden"] is False

# Body with `---` rule lines survives seeding intact.
meta, body = kb.parse_frontmatter((kb.KB_DIR / "pricing.md").read_text(encoding="utf-8"))
assert docs["pricing.md"]["body"] == body.strip()
assert docs["pricing.md"]["title"] == meta["title"]
assert docs["pricing.md"]["keywords"] == meta["keywords"]

assert db.get_kb_version() == 1  # seed counts as one version
assert db.get_kb_document("pricing.md")["latest_revision_id"] is None  # seed writes no revision

rev1 = db.save_kb_document("pricing.md", title="מחירים", keywords=["מחיר"], summary="s",
                           body="גוף\n---\nעוד", hidden=False, saved_by="dana", base_revision_id=None)
assert db.get_kb_version() == 2
doc = db.get_kb_document("pricing.md")
assert doc["body"] == "גוף\n---\nעוד" and doc["latest_revision_id"] == rev1 and doc["updated_by"] == "dana"

# Stale base revision is rejected and nothing changes.
try:
    db.save_kb_document("pricing.md", title="x", keywords=[], summary="", body="y", hidden=False,
                        saved_by="eli", base_revision_id=None)
    raise AssertionError("expected KbConflict")
except db.KbConflict as e:
    assert e.current_revision_id == rev1
assert db.get_kb_document("pricing.md")["body"] == "גוף\n---\nעוד"
assert db.get_kb_version() == 2

revs = db.list_kb_revisions("pricing.md")
assert [r["id"] for r in revs] == [rev1]
assert db.get_kb_revision(rev1)["body"] == "גוף\n---\nעוד"

# New document
rev2 = db.save_kb_document("new_topic.md", title="חדש", keywords=["א"], summary="", body="ב",
                           hidden=False, saved_by="dana", base_revision_id=None)
assert "new_topic.md" in {d["name"] for d in db.list_kb_documents()}
assert "body" not in db.list_kb_documents()[0]
print("OK")
