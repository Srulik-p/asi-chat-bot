"""Knowledge base: self-describing markdown topic files + a read tool.

Each file in `knowledge_base/` starts with YAML-ish frontmatter:

    ---
    title: ...
    keywords: [..., ...]
    summary: ...
    ---
    # body...

`build_index()` renders a compact catalog (filenames + titles + keywords)
that is injected into the cached system prompt, so the model can pick the
right file straight from its context — no separate search round-trip. The
single `read_kb` tool then returns the full body of one or more files.

Files whose name starts with `_` are kept in the repo for reference but are
NOT indexed and NOT exposed to the model (internal notes).
"""

import re
from pathlib import Path

KB_DIR = Path(__file__).parent / "knowledge_base"

_NIQQUD = re.compile(r"[֑-ׇ]")  # Hebrew vowel points / cantillation


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body). Minimal parser: title/summary strings, keywords list."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw, body = text[3:end], text[end + 4:]
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


def _load() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    if not KB_DIR.exists():
        return docs
    for path in sorted(KB_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue  # internal notes — not indexed, not exposed
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        docs[path.name] = {
            "title": meta.get("title", path.stem),
            "keywords": meta.get("keywords", []),
            "summary": meta.get("summary", ""),
            "body": body.strip(),
        }
    return docs


DOCS = _load()


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
    for name, d in DOCS.items():
        kw = ", ".join(d["keywords"])
        lines.append(f"- `{name}` — {d['title']} — {kw}")
    return "\n".join(lines)


def read_kb(filenames: list[str]) -> str:
    """Return the full body of one or more KB files (by filename from the index)."""
    if isinstance(filenames, str):  # tolerate a single string
        filenames = [filenames]
    parts: list[str] = []
    for name in filenames:
        d = DOCS.get(name) or DOCS.get(name.strip())
        if d is None:
            available = ", ".join(DOCS.keys())
            parts.append(f"[קובץ לא נמצא: {name}. קבצים זמינים: {available}]")
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
