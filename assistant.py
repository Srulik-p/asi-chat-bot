#!/usr/bin/env python3
"""Sugar Daddy customer-service assistant.

Loads the system prompt from `system_prompt.md` and sends it as the cacheable
prefix on every request. OpenAI's prompt caching kicks in automatically for
prompts >=1024 tokens — the cached portion is the longest common prefix
across requests, so keeping the system prompt static maximises cache hits
(50% input-token discount, ~5-10 min TTL).

Usage:
    python assistant.py             # interactive CLI for testing
    python assistant.py --no-stream # disable streaming
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

ROOT = Path(__file__).parent
SYSTEM_PROMPT_PATH = ROOT / "system_prompt.md"

MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

client = OpenAI()  # picks up OPENAI_API_KEY from env


def reply(history: list[dict], user_message: str, stream: bool = True) -> tuple[str, dict]:
    """Send a user turn and return (assistant_text, usage_dict).

    `history` is the running list of {role, content} dicts (user + assistant
    only — the system prompt is prepended here on every call so it stays the
    stable prefix that gets cached).
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]

    if stream:
        text_parts: list[str] = []
        with client.chat.completions.stream(
            model=MODEL,
            messages=messages,
            stream_options={"include_usage": True},
        ) as s:
            for event in s:
                if event.type == "content.delta":
                    sys.stdout.write(event.delta)
                    sys.stdout.flush()
                    text_parts.append(event.delta)
            final = s.get_final_completion()
        print()
        text = "".join(text_parts)
        usage = _usage_dict(getattr(final, "usage", None))
    else:
        resp = client.chat.completions.create(model=MODEL, messages=messages)
        text = resp.choices[0].message.content or ""
        usage = _usage_dict(resp.usage)

    return text, usage


def _usage_dict(usage) -> dict:
    if usage is None:
        return {"prompt": 0, "cached": 0, "completion": 0, "total": 0}
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return {
        "prompt": usage.prompt_tokens,
        "cached": cached,
        "completion": usage.completion_tokens,
        "total": usage.total_tokens,
    }


def chat(stream: bool = True) -> None:
    print(f"שירות לקוחות שוגר דדי (מודל: {MODEL}). הקש 'exit' ליציאה.\n")
    history: list[dict] = []
    while True:
        try:
            user_input = input("לקוח > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if user_input.lower() in {"exit", "quit", "יציאה"}:
            return
        if not user_input:
            continue

        print("נציג > ", end="" if stream else "\n", flush=True)
        try:
            text, usage = reply(history, user_input, stream=stream)
        except Exception as e:
            print(f"\n[שגיאה] {e}\n")
            continue

        if not stream:
            print(text)

        cache_hit = (usage["cached"] / usage["prompt"] * 100) if usage["prompt"] else 0
        print(
            f"  [tokens: prompt={usage['prompt']} cached={usage['cached']} "
            f"({cache_hit:.0f}%) completion={usage['completion']}]\n"
        )

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": text})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-stream", action="store_true", help="disable streaming")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        print("שגיאה: לא הוגדר OPENAI_API_KEY. צור .env מתוך .env.example.", file=sys.stderr)
        return 1

    chat(stream=not args.no_stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
