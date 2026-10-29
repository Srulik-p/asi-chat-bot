"""Smoke test: /kb admin endpoints (list, get, save with conflict check, revisions, restore).

Run with:
    uv run python tests/smoke_kb_api.py

Verifies the shared-secret gate, that admins see hidden documents, validation
(422), optimistic-concurrency 409 with a top-level current_revision_id, that a
save is served immediately by the saving instance, revision listing, and restore
(including a revision that belongs to another document).
"""
import os
import pathlib
import tempfile
import uuid

# Unique per run so concurrent executions don't race on a shared SQLite file.
DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_kb_api_{uuid.uuid4().hex}.db"
)
pathlib.Path(DB_PATH).unlink(missing_ok=True)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["INTERNAL_API_SECRET"] = "s3cret"
os.environ["KB_REFRESH_SECONDS"] = "0"

from sugarbot import db  # noqa: E402

db.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from sugarbot import kb, server  # noqa: E402

c = TestClient(server.app)
H = {"X-Internal-Secret": "s3cret"}

assert c.get("/kb").status_code == 401
assert c.get("/kb", headers={"X-Internal-Secret": "wrong"}).status_code == 401
r = c.get("/kb", headers=H)
assert r.status_code == 200
assert r.json()["version"] == 1
names = {d["name"] for d in r.json()["documents"]}
assert "pricing.md" in names and "_internal_notes.md" in names   # admins see hidden docs
assert "body" not in r.json()["documents"][0]

assert c.get("/kb/nope.md", headers=H).status_code == 404
assert c.get("/kb/pricing.md").status_code == 401
assert c.get("/kb/Bad Name.md", headers=H).status_code == 422
doc = c.get("/kb/pricing.md", headers=H).json()
assert doc["body"] and doc["latest_revision_id"] is None

bad = {"title": "", "keywords": [], "summary": "", "body": "x", "hidden": False, "saved_by": "d", "base_revision_id": None}
assert c.put("/kb/pricing.md", json=bad, headers=H).status_code == 422
assert c.put("/kb/../x.md", json={**bad, "title": "t"}, headers=H).status_code in (404, 422)
r = c.put("/kb/Bad Name.md", json={**bad, "title": "t"}, headers=H)
assert r.status_code == 422 and r.json()["detail"] == "invalid document name", r.text
assert c.put("/kb/pricing.md", json={**bad, "title": "t", "body": ""}, headers=H).status_code == 422
assert c.put("/kb/pricing.md", json={**bad, "title": "t", "keywords": ["a", " "]}, headers=H).status_code == 422
assert c.put("/kb/pricing.md", json={**bad, "title": "t", "saved_by": "  "}, headers=H).status_code == 422
assert c.put("/kb/pricing.md", json={**bad, "title": "t"}, headers={"X-Internal-Secret": "x"}).status_code == 401
assert c.get("/kb", headers=H).json()["version"] == 1   # rejected saves changed nothing

good = {**bad, "title": "מחירים", "body": "גוף 1"}
r = c.put("/kb/pricing.md", json=good, headers=H); assert r.status_code == 200, r.text
rev1 = r.json()["revision_id"]; assert r.json()["version"] == 2
assert "גוף 1" in kb.read_kb(["pricing.md"])          # saving instance serves it immediately

# Stale editor: no base, but the document already has a revision.
r = c.put("/kb/pricing.md", json={**good, "body": "גוף 2"}, headers=H)
assert r.status_code == 409 and r.json() == {"detail": "conflict", "current_revision_id": rev1}, r.text
r = c.put("/kb/pricing.md", json={**good, "body": "גוף 2", "base_revision_id": rev1}, headers=H)
assert r.status_code == 200; rev2 = r.json()["revision_id"]

revs = c.get("/kb/pricing.md/revisions", headers=H).json()["revisions"]
assert [x["id"] for x in revs] == [rev2, rev1]
assert len(c.get("/kb/pricing.md/revisions?limit=1", headers=H).json()["revisions"]) == 1
assert c.get("/kb/Bad Name.md/revisions", headers=H).status_code == 422

r = c.post("/kb/pricing.md/restore", json={"revision_id": rev1, "saved_by": "d", "base_revision_id": rev2}, headers=H)
assert r.status_code == 200
assert c.get("/kb/pricing.md", headers=H).json()["body"] == "גוף 1"
assert c.get("/kb/pricing.md/revisions", headers=H).json()["revisions"][0]["note"] == f"restore #{rev1}"
assert c.post("/kb/pricing.md/restore", json={"revision_id": 9999, "saved_by": "d", "base_revision_id": None}, headers=H).status_code == 404

r = c.put("/kb/new_topic.md", json={**good, "title": "חדש"}, headers=H); assert r.status_code == 200
new_rev = r.json()["revision_id"]
# A revision of another document cannot be restored into this one.
r = c.post("/kb/pricing.md/restore", json={"revision_id": new_rev, "saved_by": "d", "base_revision_id": None}, headers=H)
assert r.status_code == 404, r.text
# Stale base on restore -> 409.
r = c.post("/kb/pricing.md/restore", json={"revision_id": rev1, "saved_by": "d", "base_revision_id": rev1}, headers=H)
assert r.status_code == 409 and "current_revision_id" in r.json()
assert c.post("/kb/Bad Name.md/restore", json={"revision_id": 1, "saved_by": "d", "base_revision_id": None}, headers=H).status_code == 422
assert c.post("/kb/pricing.md/restore", json={"revision_id": rev1, "saved_by": "", "base_revision_id": None}, headers=H).status_code == 422

print("OK")
