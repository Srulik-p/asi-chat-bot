"""Knowledge base: topic documents served from the database + a read tool.

The documents live in the `kb_documents` table (staff edit them live); the
markdown files in `knowledge_base/` are the seed for migration 0008 and the
fallback when the database is unreachable. Each file starts with YAML-ish
frontmatter:

    ---
    title: ...
    keywords: [..., ...]
    summary: ...
    ---
    # body...

`get_docs()` returns the visible (non-hidden) documents from a per-process
cache. Instances share no memory, so the cache is keyed on the DB's
`kb_version` counter: it is re-checked at most once per KB_REFRESH_SECONDS and
reloaded when another instance (or this one) saved a change. If the database
errors, `get_docs()` serves the markdown files instead (`kb_version()` is -1)
and retries the database on the next refresh window.

`build_index()` renders a compact catalog (filenames + titles + keywords)
that is injected into the cached system prompt, so the model can pick the
right file straight from its context — no separate search round-trip. The
single `read_kb` tool then returns the full body of one or more files.

Hidden documents (and, in the file fallback, files whose name starts with `_`)
are NOT indexed and NOT exposed to the model (internal notes).
"""

import os
import sys
import threading
import time
from pathlib import Path

from sugarbot import db

KB_DIR = Path(__file__).parent / "knowledge_base"

_DELIM = "---"
_DELIM_NL = "\n---\n"  # require newline on both sides so body `---` rules don't truncate


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body). Minimal parser: title/summary strings, keywords list."""
    if not text.startswith(_DELIM):
        return {}, text
    end = text.find(_DELIM_NL, len(_DELIM))
    if end == -1:
        return {}, text
    raw, body = text[len(_DELIM):end], text[end + len(_DELIM_NL):]
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key] = val
    return meta, body


_parse_frontmatter = parse_frontmatter


def _load_files() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    if not KB_DIR.exists():
        return docs
    for path in sorted(KB_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue  # internal notes — not indexed, not exposed
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            print(f"[kb] skipping {path.name}: not valid UTF-8 ({e})", file=sys.stderr)
            continue
        meta, body = _parse_frontmatter(text)
        docs[path.name] = {
            "title": meta.get("title", path.stem),
            "keywords": meta.get("keywords", []),
            "summary": meta.get("summary", ""),
            "body": body.strip(),
        }
    return docs


KB_REFRESH_SECONDS = float(os.getenv("KB_REFRESH_SECONDS", "30"))

# Cache state, guarded by _lock when written. Readers take _docs without the
# lock; it is only ever rebound to a fully built dict, never mutated.
_lock = threading.Lock()
_docs: dict[str, dict] | None = None
_version = -1  # -1: file fallback
_checked_at = 0.0
_fallback_logged = False


def refresh(force: bool = False) -> None:
    """Re-check kb_version (at most once per window unless `force`) and reload on change."""
    global _docs, _version, _checked_at, _fallback_logged
    if not force and _docs is not None and time.monotonic() - _checked_at < KB_REFRESH_SECONDS:
        return
    with _lock:
        # Another thread may have refreshed while we waited for the lock.
        if not force and _docs is not None and time.monotonic() - _checked_at < KB_REFRESH_SECONDS:
            return
        try:
            version = db.get_kb_version()
            if _docs is None or version != _version:
                loaded = db.load_kb_documents()
                _docs = {n: d for n, d in loaded.items() if not d["hidden"]}
                _version = version
            _fallback_logged = False
        except Exception as e:
            if not _fallback_logged:
                print(f"[kb] falling back to files: {e}", file=sys.stderr)
                _fallback_logged = True
            _docs = _load_files()
            _version = -1
        _checked_at = time.monotonic()


def get_docs() -> dict[str, dict]:
    """Visible documents: {filename: {title, keywords, summary, body, hidden}}."""
    refresh()
    return _docs


def kb_version() -> int:
    """The kb_version the cache currently reflects (-1 when serving the files)."""
    refresh()
    return _version


def build_index() -> str:
    """Compact catalog injected into the system prompt (static -> stays cached)."""
    lines = [
        "## אינדקס מאגר הידע",
        "",
        "להלן קבצי הנושאים במאגר הידע. כשצריך מידע עובדתי (מחירים, נהלים, "
        "הוראות, מדיניות) - השתמש בכלי `read_kb` כדי לקרוא את הקובץ/קבצים "
        "הרלוונטיים לפי שם הקובץ, ורק אז ענה. אל תמציא פרטים.",
        "",
        "פורמט: `שם_קובץ` — נושא — מילות מפתח",
        "",
    ]
    for name, d in get_docs().items():
        kw = ", ".join(d["keywords"])
        lines.append(f"- `{name}` — {d['title']} — {kw}")
    return "\n".join(lines)


def read_kb(filenames: list[str]) -> str:
    """Return the full body of one or more KB files (by filename from the index)."""
    if not filenames:
        return "[לא צוין שם קובץ. בחר/י שם קובץ מהאינדקס שבהוראות המערכת.]"
    docs = get_docs()
    parts: list[str] = []
    for name in filenames:
        d = docs.get(name.strip())
        if d is None:
            parts.append(f"[קובץ לא נמצא: {name}. בדוק/י שם קובץ באינדקס.]")
        else:
            parts.append(f"# {d['title']} ({name})\n\n{d['body']}")
    return "\n\n---\n\n".join(parts)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_kb",
            "description": (
                "קרא את התוכן המלא של קובץ נושא אחד או יותר ממאגר הידע, "
                "לפי שמות הקבצים מהאינדקס שבהוראות המערכת. השתמש בכלי לפני "
                "מענה על כל שאלה עובדתית (מחירים, אמצעי תשלום, נהלים, הוראות, מדיניות)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filenames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'שמות קבצים מהאינדקס, למשל ["payments.md", "pricing.md"]',
                    }
                },
                "required": ["filenames"],
            },
        },
    }
]
