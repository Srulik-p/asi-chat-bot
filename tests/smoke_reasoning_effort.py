"""Smoke test: OPENAI_REASONING_EFFORT pass-through to chat completions.

Run with:
    uv run python tests/smoke_reasoning_effort.py

Why this knob exists: some models (e.g. gpt-5.6-terra) reject function tools
on /v1/chat/completions unless reasoning_effort is explicitly "none", while
others (gpt-5) reject "none" outright. The effort value therefore has to
travel with OPENAI_MODEL as deploy-level env config, not live in code.

Never hits the network — the OpenAI client is replaced with a fake that
records the kwargs of every completions call.
"""
import json
import os
import pathlib
import tempfile
import uuid

DB_PATH = str(
    pathlib.Path(tempfile.gettempdir()) / f"sugarbot_smoke_effort_{uuid.uuid4().hex}.db"
)

# Force a clean local environment BEFORE importing sugarbot modules.
os.environ["DATABASE_URL"] = ""
os.environ["USERS_DB_PATH"] = DB_PATH
os.environ["OPENAI_API_KEY"] = "dummy-for-smoke-test"
os.environ["OPENAI_REASONING_EFFORT"] = "none"  # exercise the configured case

from sugarbot import assistant, db, server  # noqa: E402

db.init_db()


class _Delta:
    type = "content.delta"

    def __init__(self, delta):
        self.delta = delta


class _Stream:
    def __init__(self, deltas, final):
        self._deltas, self._final = deltas, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._deltas)

    def get_final_completion(self):
        return self._final


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15
    prompt_tokens_details = None


class _Final:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = _Usage()


class _Completions:
    def __init__(self, script):
        self.script, self.i, self.calls = script, 0, []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script[self.i]
        self.i += 1
        return _Stream(step.get("deltas", []), _Final(step["message"]))


class _FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def run(phone, message, script):
    server.client = _FakeClient(script)
    events = list(server._run_chat(phone, message))
    return events, server.client.chat.completions.calls


# 1) env knob read at import; set -> every completions call carries it
assert assistant.REASONING_EFFORT == "none", assistant.REASONING_EFFORT
script = [{"message": _Msg(content="שלום"), "deltas": [_Delta("שלום")]}]
events, calls = run("+972500000700", "היי", script)
assert events and events[-1]["type"] == "done", events
assert len(calls) == 1, calls
assert calls[0]["reasoning_effort"] == "none", calls[0].keys()
assert calls[0]["model"] == assistant.MODEL and "tools" in calls[0], calls[0].keys()
print("1. OPENAI_REASONING_EFFORT=none -> chat kwargs carry reasoning_effort='none'")

# 2) unset (empty) -> the parameter is omitted entirely (gpt-5 rejects 'none')
server.REASONING_EFFORT = ""
script = [{"message": _Msg(content="שלום"), "deltas": [_Delta("שלום")]}]
events, calls = run("+972500000701", "היי", script)
assert len(calls) == 1 and "reasoning_effort" not in calls[0], calls[0].keys()
server.REASONING_EFFORT = "none"
print("2. unset knob -> reasoning_effort omitted from the request")

pathlib.Path(DB_PATH).unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
