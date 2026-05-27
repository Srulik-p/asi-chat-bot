#!/usr/bin/env python3
"""Strip private details (phone numbers, emails) from WhatsApp chat exports.

Reads every *.txt in `raw dada/` and writes a sanitized copy into `cleaned/`:
- Phone numbers (any format) -> last 5 digits.
- Emails -> ***EMAIL***.
- Filenames are sanitized too.
"""

import re
import sys
from pathlib import Path

# WhatsApp wraps phone numbers with BiDi marks (LRE/PDF/RLM). They survive into
# the file as zero-width chars and would otherwise split phone matches.
BIDI_CHARS = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
BIDI_RE = re.compile(f"[{BIDI_CHARS}]")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Phone: optional +, then a digit, then >=7 more digit/space/dash/paren chars,
# ending in a digit. Letter boundaries prevent matching digits embedded in
# URL tokens like SALE1777-711470E0.
PHONE_RE = re.compile(r"(?<![A-Za-z])\+?\d[\d \t\-\(\)]{7,}\d(?![A-Za-z])")


def mask_phone(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) < 9:
        return match.group(0)
    return digits[-5:]


def clean(text: str) -> str:
    text = BIDI_RE.sub("", text)
    text = EMAIL_RE.sub("***EMAIL***", text)
    text = PHONE_RE.sub(mask_phone, text)
    return text


def main() -> int:
    root = Path(__file__).parent
    src = root / "raw dada"
    dst = root / "cleaned"

    if not src.is_dir():
        print(f"source folder not found: {src}", file=sys.stderr)
        return 1

    dst.mkdir(exist_ok=True)

    count = 0
    for f in sorted(src.glob("*.txt")):
        cleaned_text = clean(f.read_text(encoding="utf-8"))
        new_name = clean(f.name)
        (dst / new_name).write_text(cleaned_text, encoding="utf-8")
        count += 1

    print(f"cleaned {count} file(s) -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
