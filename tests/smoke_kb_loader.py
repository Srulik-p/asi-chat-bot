"""Smoke test: version-aware KB loader and system prompt.

Run with:
    uv run python tests/smoke_kb_loader.py

Verifies kb.get_docs() reads visible documents from the DB, reloads when
another instance bumps kb_version, drops documents hidden via an edit, and
falls back to the markdown files (without raising) when the DB is unavailable.
"""
import os
import pathlib
import tempfile
import uuid

# Unique per run so concurrent executions don't race on a shared SQLite file.
DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_kb_loader_{uuid.uuid4().hex}.db"
)
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["KB_REFRESH_SECONDS"] = "0"

import unittest.mock as m  # noqa: E402

from sugarbot import assistant, db, kb  # noqa: E402

db.init_db()

assert "pricing.md" in kb.get_docs()
assert "_internal_notes.md" not in kb.get_docs()
assert "[קובץ לא נמצא" in kb.read_kb(["_internal_notes.md"])
assert kb.kb_version() == 1

db.save_kb_document("pricing.md", title="כותרת חדשה", keywords=["ק"], summary="", body="גוף חדש",
                    hidden=False, saved_by="t", base_revision_id=None)
kb.refresh(force=True)
assert kb.kb_version() == 2
assert "גוף חדש" in kb.read_kb(["pricing.md"])
assert "כותרת חדשה" in kb.build_index()
assert "כותרת חדשה" in assistant.system_prompt()
assert assistant.system_prompt() is assistant.system_prompt()

db.save_kb_document("pricing.md", title="כותרת חדשה", keywords=["ק"], summary="", body="גוף חדש",
                    hidden=True, saved_by="t", base_revision_id=db.get_kb_document("pricing.md")["latest_revision_id"])
kb.refresh(force=True)
assert "pricing.md" not in kb.get_docs()
assert "pricing.md" not in kb.build_index()

# DB failure falls back to files without raising.
with m.patch.object(db, "get_kb_version", side_effect=RuntimeError("db down")):
    kb.refresh(force=True)
    assert kb.kb_version() == -1
    assert "pricing.md" in kb.get_docs()
    assert "_internal_notes.md" not in kb.get_docs()
kb.refresh(force=True)
assert kb.kb_version() == 3
print("OK")
