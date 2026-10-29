# Admin-Editable Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let staff edit the bot's knowledge-base topic files from the existing password-gated Streamlit UI, with changes reaching every Cloud Run instance within seconds and a revision history to roll back from.

**Architecture:** The 20 markdown files under `src/sugarbot/knowledge_base/` move into a Postgres table (`kb_documents`) seeded by an Alembic migration; the files stay in the repo as the seed and as a fallback when the table is unreachable. `kb.py` stops loading at import and instead serves a per-process cache that re-reads the table whenever an `app_state` key `kb_version` changes (checked at most every `KB_REFRESH_SECONDS`). The system prompt is rebuilt from that cache. FastAPI gains `/kb` endpoints behind the existing `X-Internal-Secret`, and the Streamlit app gains a `pages/` page that calls them.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy Core + Alembic (Postgres in prod, SQLite locally), Streamlit, repo-style smoke tests run with `uv run python tests/<file>.py`.

**Spec:** No separate design doc. The user skipped brainstorming; the design decisions are the "Design summary" below.

## Design summary

- **Goal:** non-technical staff edit KB content without a deploy.
- **Non-goals:** deleting documents (hide instead), editing `system_prompt.md`, rich-text editing, per-user admin accounts (the UI's shared `UI_PASSWORD` stays the gate; the editor types a display name that is recorded as `updated_by`).
- **Anti-goals:** the bot ever reading a hidden document; an edit silently lost because two admins saved at once; the service failing to boot when the table is missing or the DB is down.
- **Documents:** keyed by filename (`pricing.md`). Fields: `title`, `keywords` (list), `summary`, `body` (markdown without frontmatter), `hidden`. Names starting with `_` seed as `hidden=True`.
- **Revisions:** every save writes a full snapshot row to `kb_revisions`. Restore = a new save whose content is copied from a revision.
- **Propagation:** `app_state["kb_version"] = {"v": n}`; `n` increments on every save. Each process re-reads all documents when it sees a new `n`. Prompt-cache prefix invalidation on edit is accepted.
- **Concurrency:** `PUT /kb/{name}` carries `base_revision_id` (the revision the editor loaded). Server returns 409 if the document's latest revision differs. New documents send `null`.

## Global Constraints

- All customer-facing strings stay Hebrew; code identifiers and filenames stay English (existing convention).
- Document name must match `^[a-z0-9][a-z0-9_\-]*\.md$`; also allow a leading `_` (`^_?[a-z0-9][a-z0-9_\-]*\.md$`). Max 64 chars.
- `title` non-empty, `body` non-empty, `keywords` a list of non-empty strings (may be empty list), `summary` may be empty.
- `KB_REFRESH_SECONDS` env var, default `30`, integer.
- New `/kb*` endpoints use `_check_secret(x_internal_secret, INTERNAL_SECRET, "INTERNAL_API_SECRET")` exactly like `/chat/history`.
- Tests are standalone scripts under `tests/` named `smoke_*.py`, set env **before** importing `sugarbot`, use a unique temp SQLite path, and end with `print("OK")`. Follow `tests/smoke_support_ticket.py`.
- Never read `.env`. If `.env.example` exists, add `KB_REFRESH_SECONDS=30` to it.

## Review Focus

1. A body containing a line that is exactly `---` (several KB files use rule lines). Seeding must keep the whole body; the DB path must not re-parse frontmatter. Pinned in Task 1.
2. Service boot before migration 0008 ran, or DB unreachable: `kb.get_docs()` must return the file-based documents and log, not raise. Pinned in Task 2.
3. A second Cloud Run instance must serve the new body after `KB_REFRESH_SECONDS` without a restart. Pinned in Task 2 (version-change reload) .
4. Two admins editing the same file: the second save with a stale `base_revision_id` gets 409 and nothing is overwritten. Pinned in Task 3.
5. A document hidden via the UI must vanish from the index **and** from `read_kb` on the next refresh, even if the model still remembers its name. Pinned in Task 2.

---

### Task 1: Schema, seed migration, DB helpers

**Files:**
- Create: `src/sugarbot/migrations/versions/0008_kb_documents.py`
- Modify: `src/sugarbot/db.py` (tables after `support_tickets_table`, helpers after `load_admin_token`)
- Modify: `src/sugarbot/kb.py` (expose `parse_frontmatter` publicly; see Interfaces)
- Test: `tests/smoke_kb_db.py`

**Interfaces:**
- Consumes: `kb._parse_frontmatter(text) -> (meta, body)`; rename to `parse_frontmatter` and keep `_parse_frontmatter = parse_frontmatter` alias.
- Produces, in `db.py`:
  - `kb_documents_table` columns: `name String PK`, `title String NOT NULL`, `keywords JSON NOT NULL`, `summary Text NOT NULL default ''`, `body Text NOT NULL`, `hidden Boolean NOT NULL default False`, `updated_at DateTime NOT NULL`, `updated_by String NULL`.
  - `kb_revisions_table` columns: `id Integer PK autoincrement`, `name String NOT NULL index`, `title`, `keywords JSON`, `summary Text`, `body Text`, `hidden Boolean`, `saved_at DateTime NOT NULL`, `saved_by String NULL`, `note String NULL`.
  - `list_kb_documents() -> list[dict]` — every row, all columns except `body`, plus `latest_revision_id: int | None`, ordered by name.
  - `get_kb_document(name: str) -> dict | None` — all columns plus `latest_revision_id`.
  - `load_kb_documents() -> dict[str, dict]` — `{name: {title, keywords, summary, body, hidden}}` for every row (hidden included; the caller filters).
  - `save_kb_document(name, *, title, keywords, summary, body, hidden, saved_by, note=None, base_revision_id) -> int` — in one transaction: if `base_revision_id` differs from the current latest revision id for `name` (`None` when the document has no revision yet or does not exist), raise `KbConflict(current_revision_id)`; upsert the document; insert a revision snapshot; bump `kb_version`; return the new revision id. `KbConflict(Exception)` with attribute `current_revision_id`.
  - `list_kb_revisions(name: str, limit: int = 20) -> list[dict]` — `id, saved_at, saved_by, note, title`, newest first.
  - `get_kb_revision(revision_id: int) -> dict | None` — full snapshot.
  - `get_kb_version() -> int` — `app_state["kb_version"]["v"]`, `0` if unset.

- [ ] **Step 1: Write the failing test `tests/smoke_kb_db.py`**

```python
# env setup as in smoke_support_ticket.py, then:
from sugarbot import db, kb
db.init_db()

docs = db.load_kb_documents()
files = [p for p in kb.KB_DIR.glob("*.md")]
assert len(docs) == len(files), (len(docs), len(files))
assert docs["_internal_notes.md"]["hidden"] is True
assert docs["pricing.md"]["hidden"] is False

# Review Focus 1: body with `---` rule lines survives seeding intact.
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

# Review Focus 4: stale base revision is rejected and nothing changes.
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python tests/smoke_kb_db.py`
Expected: `AttributeError` on `db.load_kb_documents` (or `kb.parse_frontmatter`).

- [ ] **Step 3: Rename `_parse_frontmatter` to `parse_frontmatter` in `kb.py`, keep the underscore alias**

- [ ] **Step 4: Add the two tables and the helpers to `db.py` per the Interfaces block**

Upsert uses the same `pg_insert`/`sqlite_insert` switch as `_set_app_state`. `latest_revision_id` is `max(kb_revisions.id) where name = :name`. The version bump reuses `_set_app_state("kb_version", {"v": current + 1})` inside the same connection; refactor `_set_app_state` to accept an optional `conn` so it can participate in the transaction.

- [ ] **Step 5: Write migration `0008_kb_documents.py`**

`revision = "0008"`, `down_revision = "0007"`. `upgrade()` creates both tables, then seeds: for each `*.md` in `Path(__file__).resolve().parents[2] / "knowledge_base"` (sorted), parse with `sugarbot.kb.parse_frontmatter`, insert a row with `hidden = name.startswith("_")`, `updated_at = now UTC`, `updated_by = "seed"`, `body = body.strip()`, `keywords = meta.get("keywords", [])`, `summary = meta.get("summary", "")`, `title = meta.get("title", stem)`. Then insert `app_state` row `kb_version = {"v": 1}` (upsert, in case it exists). `downgrade()` drops both tables and deletes the `kb_version` row.

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run python tests/smoke_kb_db.py`
Expected: `OK`

- [ ] **Step 7: Run the existing smoke tests to confirm the migration chain still applies**

Run: `uv run python tests/smoke_support_ticket.py && uv run python tests/smoke_idle_sweep.py`
Expected: both print `OK`.

- [ ] **Step 8: Commit**

```bash
git add src/sugarbot/db.py src/sugarbot/kb.py src/sugarbot/migrations/versions/0008_kb_documents.py tests/smoke_kb_db.py
git commit -m "feat(kb): store knowledge base documents and revisions in the database"
```

---

### Task 2: Version-aware KB loader and system prompt

**Files:**
- Modify: `src/sugarbot/kb.py` (replace module-level `DOCS = _load()`; `build_index`, `read_kb`)
- Modify: `src/sugarbot/assistant.py:48-57` (`SYSTEM_PROMPT` constant → `system_prompt()` function) and `:226`
- Modify: `src/sugarbot/server.py:94` (import) and `:1240` (use `system_prompt()`)
- Test: `tests/smoke_kb_loader.py`

**Interfaces:**
- Consumes: `db.load_kb_documents()`, `db.get_kb_version()`, `db.save_kb_document(...)` from Task 1.
- Produces:
  - `kb.get_docs() -> dict[str, dict]` — visible documents only (`hidden=False`), same shape as the old `DOCS`. Reloads from DB when `db.get_kb_version()` differs from the cached version; checks the version at most once per `KB_REFRESH_SECONDS` (module constant read from env, default 30). On any exception from `db` (missing table, connection error), logs `[kb] falling back to files: <err>` to stderr once and returns `_load_files()` (the old `_load()` renamed), retrying the DB on the next refresh window.
  - `kb.kb_version() -> int` — the version the cache currently reflects (`-1` for file fallback).
  - `kb.refresh(force: bool = False) -> None` — the refresh step `get_docs()` calls; `force=True` ignores the time window (used by tests and by the `/kb` write endpoints so the saving instance serves the edit immediately).
  - `kb.build_index()` and `kb.read_kb(filenames)` use `get_docs()`.
  - `assistant.system_prompt() -> str` — `system_prompt.md` text (with the freshness placeholder replaced, read once at import) + `"\n\n"` + `kb.build_index()`, memoised per `kb.kb_version()`.

- [ ] **Step 1: Write the failing test `tests/smoke_kb_loader.py`**

```python
# env setup as in Task 1, plus os.environ["KB_REFRESH_SECONDS"] = "0"
from sugarbot import db, kb, assistant
db.init_db()

assert "pricing.md" in kb.get_docs()
assert "_internal_notes.md" not in kb.get_docs()            # Review Focus 5 (seeded hidden)
assert "[קובץ לא נמצא" in kb.read_kb(["_internal_notes.md"])
assert kb.kb_version() == 1

db.save_kb_document("pricing.md", title="כותרת חדשה", keywords=["ק"], summary="", body="גוף חדש",
                    hidden=False, saved_by="t", base_revision_id=None)
kb.refresh(force=True)                                       # Review Focus 3 (other instance)
assert kb.kb_version() == 2
assert "גוף חדש" in kb.read_kb(["pricing.md"])
assert "כותרת חדשה" in kb.build_index()
assert "כותרת חדשה" in assistant.system_prompt()
assert assistant.system_prompt() is assistant.system_prompt()   # memoised per version

db.save_kb_document("pricing.md", title="כותרת חדשה", keywords=["ק"], summary="", body="גוף חדש",
                    hidden=True, saved_by="t", base_revision_id=db.get_kb_document("pricing.md")["latest_revision_id"])
kb.refresh(force=True)
assert "pricing.md" not in kb.get_docs()                     # Review Focus 5 (hidden via edit)
assert "pricing.md" not in kb.build_index()

# Review Focus 2: DB failure falls back to files without raising.
import unittest.mock as m
with m.patch.object(db, "get_kb_version", side_effect=RuntimeError("db down")):
    kb.refresh(force=True)
    assert kb.kb_version() == -1
    assert "pricing.md" in kb.get_docs()
    assert "_internal_notes.md" not in kb.get_docs()
kb.refresh(force=True)
assert kb.kb_version() == 3
print("OK")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python tests/smoke_kb_loader.py`
Expected: `AttributeError: module 'sugarbot.kb' has no attribute 'get_docs'`.

- [ ] **Step 3: Implement `get_docs`, `kb_version`, `refresh`, `_load_files` in `kb.py`; rewire `build_index` and `read_kb`**

Cache state is three module globals: `_docs`, `_version`, `_checked_at` (monotonic). Use `threading.Lock` around refresh; uvicorn runs sync endpoints in a threadpool. Filter `hidden` when building `_docs` from `db.load_kb_documents()`; file fallback keeps the `_`-prefix rule.

- [ ] **Step 4: Replace `SYSTEM_PROMPT` with `system_prompt()` in `assistant.py`; update the two call sites (`assistant.py:226`, `server.py:1240`) and the import at `server.py:94`**

Keep a module-level `_BASE_PROMPT` string read once; memoise the composed prompt as `(version, text)`.

- [ ] **Step 5: Run the new test and the full smoke suite**

Run: `uv run python tests/smoke_kb_loader.py && for t in tests/smoke_*.py; do uv run python "$t" || exit 1; done`
Expected: every script prints `OK`. (Prefix with `OPENAI_REASONING_EFFORT=none` if the local env's model pairing 400s.)

- [ ] **Step 6: Commit**

```bash
git add src/sugarbot/kb.py src/sugarbot/assistant.py src/sugarbot/server.py tests/smoke_kb_loader.py
git commit -m "feat(kb): serve knowledge base from the database with version-aware reload"
```

---

### Task 3: `/kb` endpoints

**Files:**
- Modify: `src/sugarbot/server.py` (pydantic models near `ChatResetIn`; routes after `/user/delete`; docstring block at top listing endpoints)
- Test: `tests/smoke_kb_api.py`

**Interfaces:**
- Consumes: Task 1 db helpers, `kb.refresh(force=True)` from Task 2, `_check_secret`, `INTERNAL_SECRET`.
- Produces (all require `X-Internal-Secret`):
  - `GET /kb` → `{"version": int, "documents": [ {name, title, keywords, summary, hidden, updated_at, updated_by, latest_revision_id} ]}`
  - `GET /kb/{name}` → the full document dict incl. `body`; 404 if missing.
  - `PUT /kb/{name}` body `KbDocumentIn {title: str, keywords: list[str], summary: str = "", body: str, hidden: bool = False, saved_by: str, base_revision_id: int | None}` → `{"revision_id": int, "version": int}`; 422 on validation failure (pydantic `field_validator`s enforce the Global Constraints, name checked with the regex and 422 `detail="invalid document name"`); 409 `{"detail": "conflict", "current_revision_id": n}` on `KbConflict`. Calls `kb.refresh(force=True)` after saving.
  - `GET /kb/{name}/revisions?limit=20` → `{"revisions": [...]}` from `db.list_kb_revisions`.
  - `POST /kb/{name}/restore` body `KbRestoreIn {revision_id: int, saved_by: str, base_revision_id: int | None}` → same as PUT; copies the snapshot's `title/keywords/summary/body/hidden`, `note=f"restore #{revision_id}"`; 404 if the revision does not exist or belongs to another name.

- [ ] **Step 1: Write the failing test `tests/smoke_kb_api.py`**

```python
# env setup as in Task 2, plus os.environ["INTERNAL_API_SECRET"] = "s3cret"
from fastapi.testclient import TestClient
from sugarbot import db, kb, server
c = TestClient(server.app)
H = {"X-Internal-Secret": "s3cret"}

assert c.get("/kb").status_code == 401
r = c.get("/kb", headers=H); assert r.status_code == 200
names = {d["name"] for d in r.json()["documents"]}
assert "pricing.md" in names and "_internal_notes.md" in names   # admins see hidden docs
assert "body" not in r.json()["documents"][0]

assert c.get("/kb/nope.md", headers=H).status_code == 404
doc = c.get("/kb/pricing.md", headers=H).json()
assert doc["body"] and doc["latest_revision_id"] is None

bad = {"title": "", "keywords": [], "summary": "", "body": "x", "hidden": False, "saved_by": "d", "base_revision_id": None}
assert c.put("/kb/pricing.md", json=bad, headers=H).status_code == 422
assert c.put("/kb/../x.md", json={**bad, "title": "t"}, headers=H).status_code in (404, 422)
assert c.put("/kb/Bad Name.md", json={**bad, "title": "t"}, headers=H).status_code == 422

good = {**bad, "title": "מחירים", "body": "גוף 1"}
r = c.put("/kb/pricing.md", json=good, headers=H); assert r.status_code == 200, r.text
rev1 = r.json()["revision_id"]; assert r.json()["version"] == 2
assert "גוף 1" in kb.read_kb(["pricing.md"])          # saving instance serves it immediately

# Review Focus 4
r = c.put("/kb/pricing.md", json={**good, "body": "גוף 2"}, headers=H)
assert r.status_code == 409 and r.json()["current_revision_id"] == rev1
r = c.put("/kb/pricing.md", json={**good, "body": "גוף 2", "base_revision_id": rev1}, headers=H)
assert r.status_code == 200; rev2 = r.json()["revision_id"]

revs = c.get("/kb/pricing.md/revisions", headers=H).json()["revisions"]
assert [x["id"] for x in revs] == [rev2, rev1]

r = c.post("/kb/pricing.md/restore", json={"revision_id": rev1, "saved_by": "d", "base_revision_id": rev2}, headers=H)
assert r.status_code == 200
assert c.get("/kb/pricing.md", headers=H).json()["body"] == "גוף 1"
assert c.post("/kb/pricing.md/restore", json={"revision_id": 9999, "saved_by": "d", "base_revision_id": None}, headers=H).status_code == 404

r = c.put("/kb/new_topic.md", json={**good, "title": "חדש"}, headers=H); assert r.status_code == 200
print("OK")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python tests/smoke_kb_api.py`
Expected: first `/kb` with header returns 404 → assertion fails.

- [ ] **Step 3: Add `KbDocumentIn`, `KbRestoreIn`, `_validate_kb_name(name) -> None` and the five routes to `server.py`; update the endpoint docstring at the top of the file**

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python tests/smoke_kb_api.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add src/sugarbot/server.py tests/smoke_kb_api.py
git commit -m "feat(kb): admin endpoints to list, edit, and restore knowledge base documents"
```

---

### Task 4: Streamlit editor page

**Files:**
- Create: `ui/common.py` — `API_URL`, `INTERNAL_SECRET`, `REQUEST_TIMEOUT`, `headers() -> dict`, `require_staff() -> None` (the gate block currently at `ui/app.py:85-112`, verbatim behaviour).
- Modify: `ui/app.py` — import those from `ui/common.py`, delete the duplicated gate and `_headers`.
- Create: `ui/pages/1_knowledge_base.py`
- Modify: `Dockerfile` (the UI image) if it copies only `ui/app.py`; it must copy the whole `ui/` directory. Check before editing.

**Interfaces:**
- Consumes: Task 3 endpoints.
- Produces: a page titled `מאגר ידע` reachable from Streamlit's sidebar page nav.

Page behaviour (the builder decides widgets; these are the requirements):
1. Calls `require_staff()` first; nothing renders before it passes.
2. Sidebar text input `השם שלך` stored in `st.session_state.editor_name`; saving is disabled while empty.
3. Document picker: `st.selectbox` over `GET /kb`, label `f"{title}  ({name})"`, hidden ones suffixed `[מוסתר]`. Below it an expander `קובץ חדש` with a name input (`.md` appended if missing) that switches the editor to an empty document.
4. Editor: `st.form` with title, keywords (one text input, comma-separated, split and stripped on save), summary, body `st.text_area(height=500)`, `hidden` checkbox, submit `שמור`. The form remembers `latest_revision_id` from the loaded document and sends it as `base_revision_id`.
5. Save result: success toast with the revision id; 409 → `st.error("מישהו אחר שמר את הקובץ בינתיים. טען/י מחדש ונסה/י שוב.")` and a `טען מחדש` button; 422 → show `detail`.
6. Expander `היסטוריית גרסאות`: table of `saved_at, saved_by, note, title` from `/revisions`; each row has a `שחזר` button that POSTs restore with the current `latest_revision_id` as base.
7. RTL: wrap the body in the same `st.markdown` CSS block `app.py` already uses for direction, if any; otherwise add `direction: rtl` to text areas.

- [ ] **Step 1: Create `ui/common.py` and refactor `ui/app.py` to use it**

- [ ] **Step 2: Verify the chat UI still imports**

Run: `uv run python -m py_compile ui/app.py ui/common.py`
Expected: no output.

- [ ] **Step 3: Create `ui/pages/1_knowledge_base.py` per the behaviour list**

- [ ] **Step 4: Compile and run the UI against a local backend; walk through load → edit → save → 409 (second tab) → restore**

Run, two terminals:
```
INTERNAL_API_SECRET=s3cret uv run uvicorn sugarbot.server:app --port 8000
CHAT_API_URL=http://localhost:8000 INTERNAL_API_SECRET=s3cret uv run streamlit run ui/app.py
```
Expected: page `knowledge base` appears in the sidebar nav; a save shows a toast; a second tab saving the same document afterwards shows the conflict error; restore brings the old body back.

- [ ] **Step 5: Check the UI Dockerfile copies `ui/` wholesale; fix if not**

Run: `grep -n "COPY" Dockerfile`

- [ ] **Step 6: Commit**

```bash
git add ui/common.py ui/app.py ui/pages/1_knowledge_base.py Dockerfile
git commit -m "feat(ui): knowledge base editor page with revisions and restore"
```

---

### Task 5: Config and rollout notes

**Files:**
- Modify: `.env.example` (only if it exists; add `KB_REFRESH_SECONDS=30` with a one-line comment).
- Modify: `WHATSAPP_INTEGRATION.md` or the server docstring — one paragraph: KB now lives in the DB, seeded from the repo files by migration 0008; later edits to the repo files do **not** reach prod; edit via the UI.

- [ ] **Step 1: Add the env var and the note**

- [ ] **Step 2: Run the whole smoke suite one last time**

Run: `for t in tests/smoke_*.py; do uv run python "$t" || exit 1; done`
Expected: every script prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add .env.example WHATSAPP_INTEGRATION.md
git commit -m "docs(kb): document database-backed knowledge base and KB_REFRESH_SECONDS"
```

**Rollout (user-performed, after push-to-main deploys):** the server runs `alembic upgrade head` on boot, so the first new instance seeds the table. Verify with `GET /kb` through the UI page. Tune `KB_REFRESH_SECONDS` in the Cloud Run env if propagation feels slow.
