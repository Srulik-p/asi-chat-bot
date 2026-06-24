"""Smoke test: repair_tool_calls heals dangling tool-call pairs.

Reproduces the production 400 ("tool_call_ids did not have response messages")
caused by a persisted assistant turn whose tool result was never written, and
verifies the repair leaves a consistent message list.

Run with:
    uv run python tests/smoke_repair_tool_calls.py
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "dummy-for-smoke-test")

from sugarbot.assistant import repair_tool_calls  # noqa: E402


def _assistant(call_ids, content=None):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read_kb", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def _ids(messages):
    out = set()
    for m in messages:
        for tc in m.get("tool_calls", []) or []:
            out.add(tc["id"])
    return out


def _tool_ids(messages):
    return {m["tool_call_id"] for m in messages if m.get("role") == "tool"}


# 1) The exact production poison: assistant tool_call with no tool response.
poisoned = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "היי"},
    _assistant(["call_orphan"]),  # <-- no matching tool message follows
    {"role": "user", "content": "עוד הודעה"},
]
fixed = repair_tool_calls(poisoned)
# The orphaned tool-call turn (content=None) is dropped entirely.
assert _ids(fixed) == set(), fixed
assert [m["role"] for m in fixed] == ["system", "user", "user"], fixed
print("1. orphan tool_call (no response) -> dropped")

# 2) Orphan tool-call but assistant also had text -> keep the text, drop the call.
poisoned2 = [
    {"role": "user", "content": "היי"},
    _assistant(["call_x"], content="רגע בודק"),
    {"role": "user", "content": "נו?"},
]
fixed2 = repair_tool_calls(poisoned2)
assert _ids(fixed2) == set(), fixed2
assert any(m["role"] == "assistant" and m.get("content") == "רגע בודק" for m in fixed2), fixed2
assert all("tool_calls" not in m for m in fixed2), fixed2
print("2. orphan tool_call with text -> kept as plain assistant turn")

# 3) Orphan tool message (answers a call that isn't there) -> dropped.
poisoned3 = [
    {"role": "user", "content": "היי"},
    {"role": "tool", "tool_call_id": "call_ghost", "content": "result"},
    {"role": "assistant", "content": "שלום"},
]
fixed3 = repair_tool_calls(poisoned3)
assert _tool_ids(fixed3) == set(), fixed3
print("3. orphan tool message -> dropped")

# 4) A valid pair passes through unchanged (and partial poison keeps the good one).
valid = [
    {"role": "user", "content": "מחיר?"},
    _assistant(["call_good", "call_bad"]),
    {"role": "tool", "tool_call_id": "call_good", "content": "329"},
    # call_bad has no response -> must be stripped, call_good preserved
    {"role": "assistant", "content": "המחיר 329"},
]
fixed4 = repair_tool_calls(valid)
assert _ids(fixed4) == {"call_good"}, fixed4
assert _tool_ids(fixed4) == {"call_good"}, fixed4
assert any(m.get("content") == "המחיר 329" for m in fixed4), fixed4
print("4. mixed valid+orphan -> keeps the answered pair, strips the dangling one")

# 5) Fully consistent history is unchanged.
clean = [
    {"role": "user", "content": "מחיר?"},
    _assistant(["c1"]),
    {"role": "tool", "tool_call_id": "c1", "content": "329"},
    {"role": "assistant", "content": "329"},
]
assert repair_tool_calls(clean) == clean, repair_tool_calls(clean)
print("5. consistent history -> unchanged")

print("\nALL CHECKS PASSED")
