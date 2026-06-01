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
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import kb


load_dotenv()

ROOT = Path(__file__).parent
SYSTEM_PROMPT_PATH = ROOT / "system_prompt.md"

MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
# Behaviour instructions + the KB index. Both are static, so the whole system
# prompt stays the cacheable prefix; the model reads topic files on demand via
# the read_kb tool instead of carrying the full knowledge base every turn.
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") + "\n\n" + kb.build_index()

client = OpenAI()  # picks up OPENAI_API_KEY from env

TOOLS = kb.TOOLS
TOOL_FNS = {"read_kb": kb.read_kb}

# Per-turn cap on chat-completion rounds. The model gets MAX_TOOL_ROUNDS-1
# rounds to call read_kb; the final round is forced to text (tool_choice="none"),
# guaranteeing the loop always terminates with a real answer.
MAX_TOOL_ROUNDS = 6

USAGE_KEYS = ("prompt", "cached", "completion", "total")


def empty_usage() -> dict:
    return {k: 0 for k in USAGE_KEYS}


def add_usage(total: dict, usage: dict) -> None:
    for k in USAGE_KEYS:
        total[k] += usage[k]


def resolve_tool_calls(messages: list[dict], msg) -> None:
    """Append an assistant turn that requested tools, then each tool result."""
    messages.append(
        {
            "role": "assistant",
            # content is None for tool-call-only turns — empty string is rejected
            # by strict validators (Azure, some proxies) when tool_calls is set.
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        }
    )
    for tc in msg.tool_calls:
        fn = TOOL_FNS.get(tc.function.name)
        if fn is None:
            result = f"כלי לא ידוע: {tc.function.name}"
        else:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = fn(**args)
            except (json.JSONDecodeError, TypeError) as e:
                result = f"שגיאה בהפעלת הכלי {tc.function.name}: {e}"
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


def reply(history: list[dict], user_message: str, stream: bool = True) -> tuple[str, dict]:
    """Send a user turn and return (assistant_text, usage_dict).

    `history` is the running list of {role, content} dicts (user + assistant
    only — the system prompt is prepended here on every call so it stays the
    stable prefix that gets cached). The model may call `read_kb` to pull topic
    files before answering, so this runs a short loop (usually 1-2 rounds) and
    sums token usage across all rounds. A tool-call round emits no visible text;
    the final answer streams to stdout as before.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]
    total = empty_usage()

    for i in range(MAX_TOOL_ROUNDS):
        # On the last allowed round, forbid tool calls so the model MUST answer
        # in text. This guarantees the loop terminates with a real reply rather
        # than an exception/apology bandaid.
        kwargs: dict = {"model": MODEL, "messages": messages, "tools": TOOLS}
        if i == MAX_TOOL_ROUNDS - 1:
            kwargs["tool_choice"] = "none"

        if stream:
            with client.chat.completions.stream(
                **kwargs, stream_options={"include_usage": True}
            ) as s:
                for event in s:
                    if event.type == "content.delta":
                        sys.stdout.write(event.delta)
                        sys.stdout.flush()
                final = s.get_final_completion()
            usage = _usage_dict(getattr(final, "usage", None))
        else:
            final = client.chat.completions.create(**kwargs)
            usage = _usage_dict(final.usage)

        add_usage(total, usage)

        msg = final.choices[0].message
        if msg.tool_calls:
            resolve_tool_calls(messages, msg)
            continue

        if stream:
            print()
        return msg.content or "", total

    # Unreachable: the final iteration uses tool_choice="none" so it can't
    # return tool_calls. Kept as a defensive fallback.
    return "", total


def _usage_dict(usage) -> dict:
    if usage is None:
        return empty_usage()
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
