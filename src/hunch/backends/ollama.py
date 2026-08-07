"""backends/ollama.py — the 'ollama' backend: a LOCAL model drives this Mac.

Runs the same agent loop as the 'api' backend — HUNCH_PLAYBOOK as the system
prompt, AGENT_TOOLS as native tool schemas, tools executed in-process against the
caller's live Hunch instance via _dispatch_core — but the model is served by a
local Ollama daemon (http://127.0.0.1:11434), so there is no account, no API key,
and no per-token cost. Stdlib-only: the transport is urllib against Ollama's
/api/chat, no optional package to install.

Knobs (env):
  HUNCH_OLLAMA_MODEL    model tag (default 'qwen3.5:9b')
  HUNCH_OLLAMA_HOST     server base URL (default 'http://127.0.0.1:11434')
  HUNCH_OLLAMA_NUM_CTX  context window (default 32768 — Ollama's own default can
                        be far smaller than the model supports, which would
                        silently truncate the playbook + AX snapshots)
  HUNCH_OLLAMA_STRICT   '1' disables the fallback tool-call parser (see below)

Two measured facts about small local models shape this file (probe 2026-08-07,
qwen3.5:9b on the real playbook + a real Settings snapshot):

  • Thinking is ON by default and burned ~900 decode tokens per turn — pure
    latency in an agent loop. We send "think": false on every request.
  • With the playbook as system prompt the model sometimes IMITATES the playbook's
    prose tool descriptions instead of using the native tool channel — it prints a
    well-formed JSON call inside a markdown fence. The loop recovers those with a
    conservative fallback parser and COUNTS them (usage['fallback_tool_calls']),
    so a bench run reports how often the native channel was missed instead of
    scoring a formatting miss as a task failure. HUNCH_OLLAMA_STRICT=1 turns the
    fallback off to measure the native channel alone.
"""
import json
import os
import re
import urllib.error
import urllib.request

from .base import Backend
from ..errors import HunchError

__all__ = ["OllamaBackend"]

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = 32768

# One fenced or bare JSON object candidate in assistant prose (non-greedy; the
# parser json.loads-validates every candidate, so a loose match is harmless).
_JSON_CANDIDATE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|(\{[^`]*\})", re.DOTALL)


class OllamaBackend(Backend):
    """A local Ollama-served model as the agent loop, tools in-process."""

    name = "ollama"
    provider = "ollama"
    extra = ""                      # stdlib-only; nothing extra to install
    default_model = os.environ.get("HUNCH_OLLAMA_MODEL", DEFAULT_MODEL)

    def __init__(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        super().__init__(hunch, auth=auth, app_id=app_id, can_use_tool=can_use_tool)
        self.messages = []          # conversation state; run() continues, reset() clears
        self._abort = False         # between-turns cancel flag (see interrupt())

    # ── availability ────────────────────────────────────────────────────────────
    @classmethod
    def deps_installed(cls):
        return True                 # urllib is the whole transport

    @classmethod
    def available(cls, app_id=None):
        """True when the local Ollama server answers. A network probe, kept short so
        `hunch doctor` and 'auto' resolution stay snappy when nothing is running."""
        try:
            with urllib.request.urlopen(cls._host() + "/api/tags", timeout=1.5):
                return True
        except Exception:
            return False

    @staticmethod
    def _host():
        return os.environ.get("HUNCH_OLLAMA_HOST", DEFAULT_HOST).rstrip("/")

    # ── request plumbing ─────────────────────────────────────────────────────────
    @staticmethod
    def _tools():
        """AGENT_TOOLS in Ollama's (OpenAI-style) function-tool shape — same names and
        schemas, so every HUNCH_PLAYBOOK reference resolves for this model too."""
        from ..agent import AGENT_TOOLS
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in AGENT_TOOLS]

    def _chat(self, body):
        """POST one /api/chat turn and return the decoded response — the single live
        transport touch-point (override in tests). Timeout is generous: a cold model
        load plus a long prefill on Apple Silicon can take minutes."""
        req = urllib.request.Request(
            self._host() + "/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", "")
            except Exception:
                pass
            if "not found" in detail.lower():
                raise HunchError(f"Ollama has no model {body['model']!r} — pull it with: "
                                 f"ollama pull {body['model']}") from e
            raise HunchError(f"ollama request failed: {detail or e}") from e
        except urllib.error.URLError as e:
            raise HunchError(f"no Ollama server at {self._host()} — start it with "
                             "`ollama serve` (or install: brew install ollama)") from e

    def _body(self, model, system):
        return {"model": model, "stream": False, "think": False,
                "tools": self._tools(),
                "options": {"num_ctx": int(os.environ.get("HUNCH_OLLAMA_NUM_CTX",
                                                          DEFAULT_NUM_CTX))},
                "messages": [{"role": "system", "content": system}] + self.messages}

    # ── tool-call recovery (the playbook-imitation failure mode) ─────────────────
    @staticmethod
    def _fallback_calls(text):
        """Recover tool calls a model wrote as prose/markdown JSON instead of using the
        native channel. Conservative: a candidate must json-parse to an object naming a
        real AGENT_TOOLS tool with dict arguments. Returns [{'name', 'arguments'}, ...]."""
        from ..agent import AGENT_TOOLS
        known = {t["name"] for t in AGENT_TOOLS}
        calls = []
        for m in _JSON_CANDIDATE.finditer(text or ""):
            try:
                obj = json.loads(m.group(1) or m.group(2))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
            args = obj.get("arguments") or obj.get("input") or obj.get("parameters")
            if name in known and isinstance(args, (dict, type(None))):
                calls.append({"name": name, "arguments": args or {}})
        return calls

    @staticmethod
    def _call_args(call):
        """A native tool_call's arguments as a dict (Ollama sends a parsed object, but
        some models/versions return a JSON string)."""
        args = (call.get("function") or {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return args if isinstance(args, dict) else {}

    def _run_tool(self, name, args, emit):
        """Dispatch one call and return its role='tool' message. PNG bytes ride in
        Ollama's images channel (base64) with a text placeholder."""
        import base64
        from .. import agent as _agent
        emit("tool", {"name": name, "input": args})
        value, is_error = _agent._dispatch_core(self._h, name, args)
        if isinstance(value, bytes):
            emit("tool_result", "[image]")
            return {"role": "tool", "tool_name": name, "content": "(screenshot attached)",
                    "images": [base64.b64encode(value).decode()]}
        text = ("error: " if is_error and not str(value).startswith("error") else "") + str(value)
        emit("tool_result", text[:200])
        return {"role": "tool", "tool_name": name, "content": text}

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def interrupt(self):
        self._abort = True          # checked between turns

    def reset(self):
        self.messages = []

    # ── run ──────────────────────────────────────────────────────────────────────
    def run(self, task, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=16000):
        from ..playbook import HUNCH_PLAYBOOK
        from ..agent import AGENT_ADDENDUM, AgentResult
        emit = on_event or (lambda kind, data: None)
        model = model or self.default_model
        system = HUNCH_PLAYBOOK + "\n" + AGENT_ADDENDUM
        if system_suffix:
            system += "\n" + system_suffix
        strict = os.environ.get("HUNCH_OLLAMA_STRICT") == "1"

        self.messages.append({"role": "user", "content": task})
        usage = {"input_tokens": 0, "output_tokens": 0, "fallback_tool_calls": 0}
        stop_reason, final_text, aborted, turn = "max_turns", "", True, 0
        self._abort = False

        for turn in range(1, max_turns + 1):
            if self._abort:                       # interrupt() between turns
                stop_reason, aborted = "interrupted", True
                emit("error", "interrupted")
                break
            resp = self._chat(self._body(model, system))
            usage["input_tokens"] += resp.get("prompt_eval_count") or 0
            usage["output_tokens"] += resp.get("eval_count") or 0
            msg = resp.get("message") or {}
            text = (msg.get("content") or "").strip()
            if text:
                emit("text", text)
            self.messages.append(msg)             # replay verbatim next turn

            calls = [{"name": (c.get("function") or {}).get("name", ""),
                      "arguments": self._call_args(c)}
                     for c in (msg.get("tool_calls") or [])]
            if not calls and not strict:
                calls = self._fallback_calls(text)
                usage["fallback_tool_calls"] += len(calls)
            if calls:
                # ALL results appended before the next request, mirroring ApiBackend
                for c in calls:
                    self.messages.append(self._run_tool(c["name"], c["arguments"], emit))
                continue

            stop_reason, final_text, aborted = "end_turn", text, False
            emit("done", final_text)
            break
        else:
            emit("error", f"aborted after max_turns={max_turns}")

        return AgentResult(text=final_text, turns=turn, stop_reason=stop_reason,
                           usage=usage, aborted=aborted)
