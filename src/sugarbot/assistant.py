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
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from sugarbot import kb


load_dotenv()

ROOT = Path(__file__).parent
SYSTEM_PROMPT_PATH = ROOT / "system_prompt.md"

MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
# Companion to OPENAI_MODEL, passed through as reasoning_effort when set.
# The valid value depends on the model — gpt-5.6-terra only allows function
# tools on /v1/chat/completions with an explicit reasoning_effort="none",
# while gpt-5 rejects "none" — so it must travel with the model in deploy
# env, never be hardcoded. Unset/empty -> the parameter is omitted.
REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
# Behaviour instructions + the KB index. Both are static, so the whole system
# prompt stays the cacheable prefix; the model reads topic files on demand via
# the read_kb tool instead of carrying the full knowledge base every turn.
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") + "\n\n" + kb.build_index()

# Picks up OPENAI_API_KEY from env. Explicit per-request timeout: the SDK
# default is 600s, and with the sync streaming loop each hung request pins a
# server threadpool thread — a few of those and /healthz starts failing.
client = OpenAI(
    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "120")),
    max_retries=2,
)

TOOLS = kb.TOOLS
TOOL_FNS = {"read_kb": kb.read_kb}

# Per-turn cap on chat-completion rounds. The model gets MAX_TOOL_ROUNDS-1
# rounds to call read_kb; the final round is forced to text (tool_choice="none"),
# guaranteeing the loop always terminates with a real answer.
MAX_TOOL_ROUNDS = 6

USAGE_KEYS = ("prompt", "cached", "completion", "total")

# PII scrubbing: applied only to user-role content before sending to the model.
# History keeps the raw values so a human rep can read them after escalation.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?972[-\s.]?|0)\d{1,2}[-\s.]?\d{3}[-\s.]?\d{4}(?!\d)"
)
_ID_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")


def redact_pii(text: str) -> str:
    """Replace emails, Israeli phone numbers, and 9-digit IDs with Hebrew placeholders."""
    if not text:
        return text
    text = _EMAIL_RE.sub("[מייל]", text)
    text = _PHONE_RE.sub("[טלפון]", text)
    text = _ID_RE.sub("[תעודת זהות]", text)
    return text


def scrub_messages(messages: list[dict]) -> list[dict]:
    """Return a new list with PII redacted from user-role text content.

    Handles both plain string content and multimodal content (a list of parts,
    used when an image/PDF is attached): only the text parts are redacted; media
    parts pass through untouched.
    """
    out: list[dict] = []
    for m in messages:
        if m.get("role") != "user":
            out.append(m)
            continue
        content = m.get("content")
        if isinstance(content, str):
            out.append({**m, "content": redact_pii(content)})
        elif isinstance(content, list):
            parts = [
                {**p, "text": redact_pii(p["text"])}
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                else p
                for p in content
            ]
            out.append({**m, "content": parts})
        else:
            out.append(m)
    return out


def repair_tool_calls(messages: list[dict]) -> list[dict]:
    """Repair tool-call structure so the chat API doesn't 400.

    The API requires every assistant `tool_call` to be answered by a `tool`
    message with the same `tool_call_id`, every `tool` message to answer a
    tool_call, AND the tool messages to IMMEDIATELY follow the assistant turn
    that requested them. A history persisted across an interruption — a tool
    handler that raised, a client disconnect, or a concurrent writer landing a
    row between the assistant turn and its tool results — can violate either
    rule and then 400s on every replay. We rebuild a consistent list:

    - keep only tool_calls that have a matching tool response, and only tool
      messages that answer a kept tool_call;
    - emit each kept tool message directly after its owning assistant turn,
      healing interleavings (assistant(tool_calls) / stray row / tool) that a
      keep/drop pass alone would leave poisoned.

    A fully consistent history passes through unchanged.
    """
    # First tool response per id wins; duplicates and orphans are dropped.
    tool_msgs: dict[str, dict] = {}
    for m in messages:
        cid = m.get("tool_call_id")
        if m.get("role") == "tool" and cid and cid not in tool_msgs:
            tool_msgs[cid] = m

    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in tool_msgs]
            if kept:
                out.append({**m, "tool_calls": kept})
                # Answers must be adjacent — emit them here, not where they
                # happened to be stored.
                out.extend(tool_msgs[tc["id"]] for tc in kept)
            elif m.get("content"):
                # No surviving tool_calls but there is text — keep as plain turn.
                out.append({k: v for k, v in m.items() if k != "tool_calls"})
            # else: tool-call-only assistant turn with no answers -> drop entirely
        elif role == "tool":
            # Already emitted next to its assistant turn (or an orphan) — skip.
            continue
        else:
            out.append(m)
    return out


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
    messages = scrub_messages(messages)
    total = empty_usage()

    for i in range(MAX_TOOL_ROUNDS):
        # On the last allowed round, forbid tool calls so the model MUST answer
        # in text. This guarantees the loop terminates with a real reply rather
        # than an exception/apology bandaid.
        kwargs: dict = {"model": MODEL, "messages": messages, "tools": TOOLS}
        if i == MAX_TOOL_ROUNDS - 1:
            kwargs["tool_choice"] = "none"
        if REASONING_EFFORT:
            kwargs["reasoning_effort"] = REASONING_EFFORT

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
